#!/usr/bin/env python3
"""[089860] 롯데렌탈 종가 감시. 19:00 k203780 체인 같은 단계([0219B0] [0163Y0] 가격 감시와 같은 시간).

1) 분할 지정가 검토선: 확정 종가 <= 45,000원 이면 p4 1번. 종가가 47,000원 넘게 회복한 뒤에야 다시 무장.
2) 신규 매수 근거 없음 구간: 확정 종가 > 55,000원 이 처음 나오면 p3 1번만(다시 무장 안 함, config above.enabled).
판정 날짜: 네이버 일별 시세(localTradedAt)의 확정 종가. 당일 행은 15:40 KST 이후에만 확정으로 본다.
  state price.last_date 보다 새 날짜만 날짜순으로 판정하고, 판정한 날짜를 last_date 에 남긴다.
  19:00 에 당일(KRX 거래일) 행이 아직 없으면 그날은 판정하지 않고 price.pending 에 남긴 뒤 다음 실행에서 이어서 판정.
  기대 날짜보다 stale_trading_days(2) 거래일 이상 밀리면 cwally-ops 에 하루 1번 안내.
첫 실행(state 에 last_date 없음): 마지막 확정 종가로 상태만 기록, 발송 없음.
읽기 실패는 cwally-ops 하루 1번(제목에 조회 실패). 감시 기간(daily_window) 밖, KRX 휴장일은 조용히 끝남.
modes: auto | dry(발송 없음, state 안 씀) | selftest ; --as-of "YYYY-MM-DD HH:MM" (dry 전용)
"""
from __future__ import annotations

import json
import sys
import traceback
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import k079900_monitor as k79  # noqa: E402  (읽기 전용 재사용: http, num, KST)
from kr_calendar import is_trading_day, prev_trading_day, session_day  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = k79.KST
TAG = "[089860]"
NAME = "lotterental"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
CLOSE_HM = (15, 40)


def fetch_rows(cfg: dict) -> list[dict]:
    raw = json.loads(k79.http(cfg["price_source"]))
    rows = []
    for r in raw if isinstance(raw, list) else []:
        d = str(r.get("localTradedAt") or "")[:10]
        c = k79.num(r.get("closePrice"))
        if len(d) == 10 and c:
            rows.append({"day": d, "close": c})
    rows.sort(key=lambda x: x["day"])
    if not rows:
        raise RuntimeError("시세 0건")
    return rows


def confirmed(rows: list[dict], today: str, now: datetime) -> list[dict]:
    late = (now.hour, now.minute) >= CLOSE_HM or now.date().isoformat() > today
    return [r for r in rows if r["day"] < today or (r["day"] == today and late)]


def expected_day(sday: date, now: datetime) -> str:
    """이번 실행에서 있어야 할 마지막 확정 거래일."""
    late = (now.hour, now.minute) >= CLOSE_HM or now.date() > sday
    if is_trading_day(sday) and late:
        return sday.isoformat()
    return prev_trading_day(sday).isoformat()


def lag_days(last: str, expect: str) -> int:
    """last 다음부터 expect 까지 KRX 거래일 수(0 이면 최신)."""
    if last >= expect:
        return 0
    n, d = 0, date.fromisoformat(expect)
    lo = date.fromisoformat(last)
    while d > lo:
        if is_trading_day(d):
            n += 1
        d = date.fromordinal(d.toordinal() - 1)
    return n


def seed_state(cfg: dict, rows: list[dict], stamp: str) -> dict:
    r = rows[-1]
    return {"last_date": r["day"], "last_close": r["close"],
            "buy_armed": not r["close"] <= cfg["buy"]["level"],
            "above_sent": None if r["close"] <= cfg["above"]["level"] else r["day"],
            "pending": None, "seed_note": f"첫 실행 {stamp}: 마지막 확정 종가 {r['day']} {r['close']:,.0f}원"}


def evaluate(cfg: dict, ps: dict, rows: list[dict], today: str) -> list[dict]:
    b, a = cfg["buy"], cfg["above"]
    out = []
    for row in rows:
        d, c = row["day"], row["close"]
        if d <= ps["last_date"] or d < cfg["daily_window"]["start"] or d > today:
            continue
        if c <= b["level"] and ps.get("buy_armed", True):
            ps["buy_armed"] = False
            out.append({"kind": "buy", "day": d, "close": c})
        elif c > b["rearm_above"] and not ps.get("buy_armed", True):
            ps["buy_armed"] = True
        if a.get("enabled") and c > a["level"] and not ps.get("above_sent"):
            ps["above_sent"] = d
            out.append({"kind": "above", "day": d, "close": c})
        ps["last_date"], ps["last_close"] = d, c
    return out


def body(stamp: str, hdr: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([f"{stamp} | 089860 롯데렌탈 | {hdr}", f"이게 뭐 알림: {what}", f"지금 결과/상태: {status}",
                      f"지금 할 일: {action}", f"나중에: {later}", f"익절/줄일 때: {trim}", f"하지 말 것: {avoid}"])


WHAT = ("롯데렌탈(089860, KOSPI) 확정 종가가 45,000원(2026E PER 약 12배, PBR 약 1.0배) 이하로 내려오면 오는 알림. "
        "미보유, 11/2 종가 51,100원 기준 신규 매수 보류 중. 매매 지시 아님.")


def dist(c: float, lv: float) -> str:
    return f"{(c / lv - 1) * 100:+.1f}% ({c - lv:+,.0f}원)"


def messages(cfg: dict, ev: list[dict], stamp: str) -> list[tuple[str, str, int]]:
    b, a, out = cfg["buy"], cfg["above"], []
    for e in ev:
        c = e["close"]
        if e["kind"] == "buy":
            out.append((f"{TAG} 롯데렌탈 | 종가 {c:,.0f}원, 45,000원 분할 지정가 검토선 도달", body(
                stamp, "45,000원 검토선", WHAT,
                f"{e['day']} 종가 {c:,.0f}원 (판정 거래일 {e['day']}). 45,000원 대비 {dist(c, b['level'])}.",
                f"{cfg['reminder']} 한 번에 다 사지 말고 나눠서 지정가로.",
                f"이 알림은 한 번만 옵니다. 종가가 {b['rearm_above']:,}원 넘게 회복한 뒤 다시 45,000원 이하로 오면 다시 알림. 감시는 2027-02-26 까지.",
                "미보유라 줄일 수량 없음. 산다면 55,000원 위는 신규 매수 근거 없음 구간.",
                "3분기 실적과 배당 확인 없이 사기, 장중 가격만 보고 사기, 신용 및 미수. 투자 권유 아님."), b["priority"]))
        else:
            out.append((f"{TAG} 롯데렌탈 | 종가 {c:,.0f}원, 55,000원 위 신규 매수 근거 없음 구간", body(
                stamp, "55,000원 위", "롯데렌탈(089860) 확정 종가가 55,000원을 넘으면 1번만 오는 안내. 매매 지시 아님.",
                f"{e['day']} 종가 {c:,.0f}원 (판정 거래일 {e['day']}). 45,000원 대비 {dist(c, b['level'])}, 55,000원 대비 {dist(c, a['level'])}.",
                "신규 매수 근거 없음 구간. 할 일 없음, 계획대로 보류.",
                "이 안내는 다시 오지 않습니다. 45,000원 검토선 감시는 2027-02-26 까지 계속.",
                "미보유라 해당 없음.", "오른다고 추격 매수하기."), a["priority"]))
    return out


def ops_msg(stamp: str, status: str) -> tuple[str, str]:
    return (f"{TAG} 롯데렌탈 시세 조회 실패 | 원문 확인 필요", body(
        stamp, "데이터 읽기 실패", "089860 종가 감시의 데이터 읽기 실패 안내. 하루 1번. 매매 신호 아님.", status,
        "네이버 증권에서 089860 일별 시세를 직접 보세요.", "다음 거래일 19:00 에 다시 읽고, 데이터 날짜로 이어서 판정합니다.",
        "이 안내로 계획을 바꾸지 않기.", "읽기 실패를 가격 신호로 읽기."))


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(st: dict) -> None:
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def check_text(title: str, msg: str) -> None:
    assert "\u00b7" not in title + msg and "\u318d" not in title + msg and "\u2014" not in title + msg, title


def run(mode: str, now: datetime, cfg: dict, pub=publish, rows_fn=None, st: dict | None = None, save=True) -> int:
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    sday = session_day(now)
    today = sday.isoformat()
    st = load_state() if st is None else st
    topic, server = resolve_topic("port", cfg.get("ntfy_topic")), resolve_server(cfg.get("ntfy_server"))
    print(f"{stamp} {NAME} mode={mode} session={today}")
    d0, d1 = cfg["daily_window"]["start"], cfg["daily_window"]["end"]
    if not (d0 <= today <= d1):
        print(f"{today} 감시 기간 밖({d0} ~ {d1}): 조용히 종료")
        return 0
    if not is_trading_day(sday):
        print(f"{today} KRX 휴장: 조용히 종료")
        return 0
    out, ops = [], None
    try:
        rows = confirmed((rows_fn or fetch_rows)(cfg), today, now)
        if not rows:
            raise RuntimeError("확정 종가 0건")
        if not (st.get("price") or {}).get("last_date"):
            st["price"] = seed_state(cfg, rows, stamp)
            print(f"first run: state only, no alert: {st['price']}")
        else:
            ps = st["price"]
            before = ps["last_date"]
            ev = evaluate(cfg, ps, rows, today)
            out = messages(cfg, ev, stamp)
            expect = expected_day(sday, now)
            ps["pending"] = expect if ps["last_date"] < expect else None
            ps["last_judged_at"] = stamp
            judged = [r["day"] for r in rows if before < r["day"] <= today]
            print(f"judged trading dates: {judged or '없음(새 확정 종가 없음)'}; expected {expect}; latest row {rows[-1]['day']}")
            lag = lag_days(ps["last_date"], expect)
            if lag >= cfg.get("stale_trading_days", 2) and st.get("ops_day") != today:
                ops = ops_msg(stamp, f"089860 네이버 일별 시세가 {expect} 까지 와야 하는데 마지막 행이 {rows[-1]['day']} ({lag}거래일 지연). 다음 실행에서 이어서 판정.")
                st["ops_day"] = today
        print(f"price: {st['price']}")
    except Exception as exc:  # noqa: BLE001
        if st.get("ops_day") != today:
            ops = ops_msg(stamp, f"089860 네이버 시세 읽기 실패: {str(exc)[:160]}. state 는 그대로.")
            st["ops_day"] = today
    for title, msg, pri in out:
        check_text(title, msg)
        assert route_topic(topic, title) == topic, title
        print(f"TITLE: {title}\nPRIORITY: {pri} TOPIC: {route_topic(topic, title)}\n{msg}\n")
    if ops:
        check_text(*ops)
        print(f"OPS TITLE: {ops[0]} -> {route_topic(resolve_topic('ops'), ops[0])}\n{ops[1]}\n")
    if not out and not ops:
        print("보낼 것 없음")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    for title, msg, pri in out:
        pub(topic, title, msg, pri, ["moneybag"], server, config_name=NAME)
    if ops:
        try:
            pub(resolve_topic("ops"), ops[0], ops[1], 3, ["warning"], server, config_name=NAME)
        except Exception as exc:  # noqa: BLE001
            print(f"ops notice failed: {exc}")
    if save:
        save_state(st)
    return 0


def selftest() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    mk = lambda pairs: [{"day": d, "close": float(c)} for d, c in pairs]  # noqa: E731
    # 45,000 이하 1번, 47,000 이하 회복은 재무장 안 함, 47,000 초과 뒤 재발송
    ps = {"last_date": "2026-11-02", "buy_armed": True, "above_sent": None}
    rows = mk([("2026-11-02", 44000), ("2026-11-03", 45000), ("2026-11-04", 44000), ("2026-11-05", 47000),
               ("2026-11-06", 44500), ("2026-11-09", 47100), ("2026-11-10", 45000)])
    ev = evaluate(cfg, ps, rows, "2026-11-10")
    assert [(e["kind"], e["day"]) for e in ev] == [("buy", "2026-11-03"), ("buy", "2026-11-10")], ev
    assert ps["last_date"] == "2026-11-10" and not ps["buy_armed"], ps
    # 55,000 초과는 1번만
    ps2 = {"last_date": "2026-11-02", "buy_armed": True, "above_sent": None}
    ev2 = evaluate(cfg, ps2, mk([("2026-11-03", 55000), ("2026-11-04", 55100), ("2026-11-05", 50000), ("2026-11-06", 56000)]), "2026-11-06")
    assert [(e["kind"], e["day"]) for e in ev2] == [("above", "2026-11-04")], ev2
    # 감시 시작 전, seed 날짜 이하는 판정 안 함
    ps3 = {"last_date": "2026-11-02", "buy_armed": True}
    assert evaluate(cfg, ps3, mk([("2026-10-30", 40000), ("2026-11-02", 40000)]), "2026-11-03") == []
    # 확정 종가: 15:40 전 당일 행 제외, 19:00 포함
    r = mk([("2026-11-02", 51100), ("2026-11-03", 44000)])
    assert [x["day"] for x in confirmed(r, "2026-11-03", datetime(2026, 11, 3, 15, 0, tzinfo=KST))] == ["2026-11-02"]
    assert [x["day"] for x in confirmed(r, "2026-11-03", datetime(2026, 11, 3, 19, 0, tzinfo=KST))] == ["2026-11-02", "2026-11-03"]
    assert expected_day(date(2026, 11, 3), datetime(2026, 11, 3, 19, 0, tzinfo=KST)) == "2026-11-03"
    assert expected_day(date(2026, 11, 3), datetime(2026, 11, 3, 10, 0, tzinfo=KST)) == "2026-11-02"
    assert lag_days("2026-11-02", "2026-11-03") == 1 and lag_days("2026-11-03", "2026-11-03") == 0
    # run(): 19:00 에 당일 행 없으면 pending, 발송 없음; 다음 날 이어서 판정; 시세 실패는 ops 1번(조회 실패), 같은 날 두 번 안 보냄
    sent = []
    pub = lambda t, ti, m, p, tags, srv, config_name=None: sent.append((t, ti, p))  # noqa: E731
    st = {"price": {"last_date": "2026-11-02", "last_close": 51100.0, "buy_armed": True, "above_sent": None}}
    run("auto", datetime(2026, 11, 3, 19, 0, tzinfo=KST), cfg, pub, lambda c: mk([("2026-11-02", 51100)]), st, save=False)
    assert sent == [] and st["price"]["pending"] == "2026-11-03", st
    run("auto", datetime(2026, 11, 4, 19, 0, tzinfo=KST), cfg, pub,
        lambda c: mk([("2026-11-02", 51100), ("2026-11-03", 44900), ("2026-11-04", 44800)]), st, save=False)
    assert [(s[0], s[2]) for s in sent] == [("cwally-port", 4)] and "44,900원" in sent[0][1] and st["price"]["last_date"] == "2026-11-04", sent
    assert st["price"]["pending"] is None
    def boom(c):
        raise RuntimeError("x")
    run("auto", datetime(2026, 11, 5, 19, 0, tzinfo=KST), cfg, pub, boom, st, save=False)
    run("auto", datetime(2026, 11, 5, 20, 0, tzinfo=KST), cfg, pub, boom, st, save=False)
    assert [s[0] for s in sent[1:]] == ["cwally-ops"] and "조회 실패" in sent[1][1], sent
    # 기간 밖, 휴장: 조용히
    n = len(sent)
    run("auto", datetime(2027, 3, 2, 19, 0, tzinfo=KST), cfg, pub, boom, st, save=False)
    run("auto", datetime(2026, 11, 7, 19, 0, tzinfo=KST), cfg, pub, boom, st, save=False)
    assert len(sent) == n
    # 문구 규칙, 제목 형식
    msgs = messages(cfg, [{"kind": "buy", "day": "2026-11-04", "close": 44800.0}, {"kind": "above", "day": "2026-11-04", "close": 55500.0}], "T")
    assert msgs[0][0] == "[089860] 롯데렌탈 | 종가 44,800원, 45,000원 분할 지정가 검토선 도달", msgs[0][0]
    assert "11/5 3분기 실적(영업이익 900억원 이상, 배당 숫자 상향) 확인 후 분할 지정가, 일반계좌" in msgs[0][1]
    assert "-0.4%" in msgs[0][1] and "신규 매수 근거 없음 구간" in msgs[1][0] and msgs[1][2] == 3
    for t, m, _ in msgs + [(*ops_msg("T", "x"), 3)]:
        check_text(t, m)
        assert route_topic("cwally-port", t) == ("cwally-ops" if "조회 실패" in t else "cwally-port"), t
    print("selftest ok")
    return 0


def main() -> int:
    args = sys.argv[1:]
    mode = (args[0] if args and not args[0].startswith("--") else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry"}:
        print(f"usage: {NAME}_monitor.py <auto|dry|selftest> [--as-of 'YYYY-MM-DD HH:MM']")
        return 2
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    if "--as-of" in args:
        if mode != "dry":
            print("--as-of 는 dry 에서만")
            return 2
        now = datetime.strptime(args[args.index("--as-of") + 1], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    try:
        return run(mode, now, cfg)
    except Exception as exc:  # noqa: BLE001
        print(traceback.format_exc(), file=sys.stderr)
        if mode != "auto":
            return 1
        try:
            t, m = ops_msg(now.strftime("%Y-%m-%d %H:%M KST"), f"스크립트 오류 {type(exc).__name__}: {str(exc)[:200]}. state 는 그대로.")
            publish(resolve_topic("ops"), t, m, 3, ["warning"], resolve_server(cfg.get("ntfy_server")), config_name=NAME)
        except Exception:  # noqa: BLE001
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
