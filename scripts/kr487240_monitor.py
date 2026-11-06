#!/usr/bin/env python3
"""[487240] KODEX AI전력핵심설비 종가 감시. 19:00 k203780 체인 같은 단계([036530] SNT홀딩스 바로 뒤).

같은 종목의 15:40 [487240] k487240(25,000/22,000/20,700+20일선)과 별개 조건.

1) support: 확정 종가 >= 19,000원이 3거래일 연속이고 그날 거래량 < 직전 20거래일 평균이면 p3 1번(지지 확인).
   종가가 19,000원 미만으로 마감한 뒤에야 다시 무장.
2) low: 확정 종가 < 19,000원이면 p4 1번(하단 이탈). 종가가 19,800원 넘게 회복한 뒤에야 다시 무장.
3) recovery: 확정 종가 > 22,680원(2026-11-05 종가)이면 p3 1번만(회복). 다시 무장 안 함.
판정 날짜: 정규장 확정 종가(scripts/krx_close.py). 당일 행은 15:40 KST 이후에만 확정으로 본다.
  (AGENTS.md: 네이버 일봉 closePrice 는 시간외가 섞여 종가로 쓰지 않음.)
  state price.last_date 보다 새 날짜만 날짜순으로 판정.
  19:00 에 당일 행이 없으면 price.pending 에 남긴 뒤 다음 실행에서 이어서 판정.
첫 실행(state 에 last_date 없음): 마지막 확정 종가로 상태만 기록, 발송 없음.
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
import k079900_monitor as k79  # noqa: E402
from kr_calendar import is_trading_day, prev_trading_day, session_day, session_close_ready  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = k79.KST
TAG = "[487240]"
NAME = "kr487240"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
CLOSE_HM = (15, 40)


def fetch_rows(cfg: dict) -> list[dict]:
    import krx_close
    rows = [{"day": b.day, "close": b.close, "volume": b.volume} for b in krx_close.regular_bars(cfg["code"], 45)]
    if not rows:
        raise RuntimeError("시세 0건")
    return rows


def confirmed(rows: list[dict], today: str, now: datetime) -> list[dict]:
    return [r for r in rows if session_close_ready(r["day"], today, now, CLOSE_HM)]


def expected_day(sday: date, now: datetime) -> str:
    late = (now.hour, now.minute) >= CLOSE_HM or now.date() > sday
    if is_trading_day(sday) and late:
        return sday.isoformat()
    return prev_trading_day(sday).isoformat()


def lag_days(last: str, expect: str) -> int:
    if last >= expect:
        return 0
    n, d = 0, date.fromisoformat(expect)
    lo = date.fromisoformat(last)
    while d > lo:
        if is_trading_day(d):
            n += 1
        d = date.fromordinal(d.toordinal() - 1)
    return n


def avg_vol(rows: list[dict], i: int, n: int) -> float | None:
    prior = rows[max(0, i - n):i]
    vols = [r.get("volume") for r in prior]
    if len(prior) < n or any(v is None for v in vols):
        return None
    return sum(vols) / n


def seed_state(cfg: dict, rows: list[dict], stamp: str) -> dict:
    r = rows[-1]
    s, lo, rc = cfg["support"], cfg["low"], cfg["recovery"]
    streak = 0
    for x in reversed(rows):
        if x["close"] >= s["level"]:
            streak += 1
        else:
            break
    i = len(rows) - 1
    av = avg_vol(rows, i, s["vol_days"])
    vol = r.get("volume")
    support_hit = streak >= s["days"] and av is not None and vol is not None and vol < av
    return {
        "last_date": r["day"], "last_close": r["close"], "last_volume": vol,
        "support_armed": not support_hit,
        "above_streak": streak,
        "low_armed": not (r["close"] < lo["level"]),
        "recovery_sent": r["day"] if r["close"] > rc["level"] else None,
        "pending": None,
        "seed_note": (
            f"첫 실행 {stamp}: 마지막 확정 종가 {r['day']} {r['close']:,.0f}원, "
            f"거래량 {vol or 0:,.0f}주"
            + (f", 직전 20거래일 평균 {av:,.0f}주" if av else "")
        ),
    }


def evaluate(cfg: dict, ps: dict, rows: list[dict], today: str) -> list[dict]:
    s, lo, rc = cfg["support"], cfg["low"], cfg["recovery"]
    out = []
    for i, row in enumerate(rows):
        d, c = row["day"], row["close"]
        if d <= ps["last_date"] or d < cfg["daily_window"]["start"] or d > today or d > cfg["daily_window"]["end"]:
            continue
        vol = row.get("volume")
        av = avg_vol(rows, i, s["vol_days"])
        if c >= s["level"]:
            ps["above_streak"] = int(ps.get("above_streak") or 0) + 1
        else:
            ps["above_streak"] = 0
            ps["support_armed"] = True
        if (
            ps.get("support_armed", True)
            and ps["above_streak"] >= s["days"]
            and av is not None and vol is not None and vol < av
        ):
            ps["support_armed"] = False
            out.append({"kind": "support", "day": d, "close": c, "volume": vol, "avg": av,
                        "streak": ps["above_streak"]})
        if c < lo["level"] and ps.get("low_armed", True):
            ps["low_armed"] = False
            out.append({"kind": "low", "day": d, "close": c, "volume": vol, "avg": av})
        elif c > lo["rearm_above"] and not ps.get("low_armed", True):
            ps["low_armed"] = True
        if c > rc["level"] and not ps.get("recovery_sent"):
            ps["recovery_sent"] = d
            out.append({"kind": "recovery", "day": d, "close": c, "volume": vol, "avg": av})
        ps["last_date"], ps["last_close"], ps["last_volume"] = d, c, vol
    return out


def body(stamp: str, hdr: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([
        f"{stamp} | 487240 KODEX AI전력핵심설비 | {hdr}",
        f"이게 뭐 알림: {what}",
        f"지금 결과/상태: {status}",
        f"지금 할 일: {action}",
        f"나중에: {later}",
        f"익절/줄일 때: {trim}",
        f"하지 말 것: {avoid}",
    ])


def dist(c: float, lv: float) -> str:
    return f"{(c / lv - 1) * 100:+.1f}% ({c - lv:+,.0f}원)"


def vol_line(e: dict) -> str:
    vol, av = e.get("volume"), e.get("avg")
    if vol is None or av is None or av == 0:
        return "거래량 비교 불가."
    return f"거래량 {vol:,.0f}주, 직전 20거래일 평균 {av:,.0f}주의 {vol / av:.2f}배."


def ctx(cfg: dict) -> str:
    return cfg.get("context") or ""


def messages(cfg: dict, ev: list[dict], stamp: str) -> list[tuple[str, str, int]]:
    s, lo, rc, out = cfg["support"], cfg["low"], cfg["recovery"], []
    for e in ev:
        c = e["close"]
        if e["kind"] == "support":
            out.append((f"{TAG} KODEX AI전력핵심설비 | 종가 {c:,.0f}원, 19,000원 지지 확인", body(
                stamp, "19,000원 3일 연속 + 거래량 평균 미만",
                "KODEX AI전력핵심설비(487240) 정규장 확정 종가가 19,000원 이상으로 3거래일 연속이고 "
                "그날 거래량이 직전 20거래일 평균보다 작을 때 오는 알림. 매매 지시 아님.",
                f"{e['day']} 종가 {c:,.0f}원 (판정 거래일 {e['day']}). 19,000원 대비 {dist(c, s['level'])}. "
                f"{e['streak']}거래일 연속 19,000원 이상. {vol_line(e)} {ctx(cfg)}",
                "지지 확인으로 보고 분할 매수 계획을 다시 점검. 한 번에 다 사지 말고 지정가로 나눠서.",
                f"이 알림은 한 번만 옵니다. 종가가 19,000원 미만으로 마감한 뒤 다시 같은 조건이면 다시 알림. 감시는 2027-02-26 까지.",
                "종가 19,000원 미만이면 하단 이탈 알림이 따로 옵니다.",
                "장중 가격만 보고 사기, 신용 및 미수. 투자 권유 아님."), s["priority"]))
        elif e["kind"] == "low":
            out.append((f"{TAG} KODEX AI전력핵심설비 | 종가 {c:,.0f}원, 19,000원 하단 이탈", body(
                stamp, "19,000원 하단 이탈",
                "KODEX AI전력핵심설비(487240) 정규장 확정 종가가 19,000원 미만으로 마감하면 오는 알림. 매매 지시 아님.",
                f"{e['day']} 종가 {c:,.0f}원 (판정 거래일 {e['day']}). 19,000원 대비 {dist(c, lo['level'])}. "
                f"{vol_line(e)} {ctx(cfg)}",
                "하단 이탈. 추가 매수 멈춤, 보유 비중과 손절 계획을 다시 확인.",
                f"이 알림은 한 번만 옵니다. 종가가 {lo['rearm_above']:,}원 넘게 회복한 뒤 다시 19,000원 미만이면 다시 알림. 감시는 2027-02-26 까지.",
                "이탈 구간이라 추가 매수보다 줄이는 쪽 먼저.",
                "이탈 당일 물타기, 기준을 미루며 버티기. 투자 권유 아님."), lo["priority"]))
        else:
            out.append((f"{TAG} KODEX AI전력핵심설비 | 종가 {c:,.0f}원, 22,680원 회복", body(
                stamp, "22,680원 회복",
                "KODEX AI전력핵심설비(487240) 정규장 확정 종가가 22,680원(2026-11-05 종가)을 넘으면 1번만 오는 알림. 매매 지시 아님.",
                f"{e['day']} 종가 {c:,.0f}원 (판정 거래일 {e['day']}). 22,680원 대비 {dist(c, rc['level'])}. "
                f"{vol_line(e)} {ctx(cfg)}",
                "11/5 종가 위로 회복. 보유 계획과 분할 매수 계획을 다시 점검.",
                "이 알림은 다시 오지 않습니다. 19,000원 지지와 이탈 감시는 2027-02-26 까지 계속.",
                "오른 김에 일부 차익을 둘지 계획대로 판단.",
                "회복 다음 날 추격 매수, 신용 및 미수. 투자 권유 아님."), rc["priority"]))
    return out


def ops_msg(stamp: str, status: str) -> tuple[str, str]:
    return (f"{TAG} KODEX AI전력핵심설비 시세 조회 실패 | 원문 확인 필요", body(
        stamp, "데이터 읽기 실패", "487240 종가 감시의 데이터 읽기 실패 안내. 하루 1번. 매매 신호 아님.", status,
        "Daum 금융이나 네이버 증권에서 487240 일별 시세를 직접 보세요.",
        "다음 거래일 19:00 에 다시 읽고, 데이터 날짜로 이어서 판정합니다.",
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
    assert title.startswith(TAG), title


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
            st["price"]["last_judged_at"] = stamp
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
                ops = ops_msg(stamp, f"487240 정규장 일별 시세가 {expect} 까지 와야 하는데 마지막 행이 {rows[-1]['day']} ({lag}거래일 지연). 다음 실행에서 이어서 판정.")
                st["ops_day"] = today
        print(f"price: {st['price']}")
    except Exception as exc:  # noqa: BLE001
        if st.get("ops_day") != today:
            ops = ops_msg(stamp, f"487240 시세 읽기 실패: {str(exc)[:160]}. state 는 그대로.")
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
    base = [(f"2026-10-{d:02d}", 21000.0, 1000.0) for d in range(1, 31)]

    def mk(trip):
        return [{"day": d, "close": float(c), "volume": float(v)} for d, c, v in base + trip]

    def days(n, start=date(2026, 11, 9)):  # first trading day after seed 2026-11-06
        out, d = [], start
        while len(out) < n:
            if is_trading_day(d):
                out.append(d.isoformat())
            d = date.fromordinal(d.toordinal() + 1)
        return out

    def st0():
        return {"last_date": "2026-11-06", "last_close": 20490.0, "last_volume": 5815656.0,
                "support_armed": True, "above_streak": 3, "low_armed": True, "recovery_sent": None}

    kinds = lambda ev: [(e["kind"], e["day"]) for e in ev]  # noqa: E731
    D = days(40)
    # support: 3일 연속 >= 19,000 + 거래량 < 평균. 거래량 많으면 안 보내고, 다음 날 평균 미만이면 보냄. 이탈 뒤 재무장.
    # base 거래량 1000, 평균 약 1000. vol 500 이면 미만, 2000 이면 초과.
    ps = st0()
    # streak already 3 from seed; day D[0] close 20000 vol 2000 (above avg) -> no fire; D[1] vol 500 -> fire
    ev = evaluate(cfg, ps, mk([(D[0], 20000, 2000), (D[1], 20100, 500), (D[2], 18500, 500),
                               (D[3], 20000, 500), (D[4], 20100, 500), (D[5], 20200, 500)]), D[5])
    assert kinds(ev) == [("support", D[1]), ("low", D[2]), ("support", D[5])], (ev, kinds(ev))
    assert not ps["support_armed"] and ps["above_streak"] == 3 and ps["low_armed"] is True, ps  # 20,000 > 19,800 rearm
    # low rearm above 19,800 then fire again
    ps = st0()
    ev = evaluate(cfg, ps, mk([(D[0], 18500, 500), (D[1], 19800, 500), (D[2], 18500, 500)]), D[2])
    assert [k for k in kinds(ev) if k[0] == "low"] == [("low", D[0])], ev  # 19,800 exactly: rearm needs > 19,800
    ps = st0()
    ev = evaluate(cfg, ps, mk([(D[0], 18500, 500), (D[1], 19900, 500), (D[2], 19801, 500), (D[3], 18500, 500)]), D[3])
    assert [k for k in kinds(ev) if k[0] == "low"] == [("low", D[0]), ("low", D[3])], ev
    # recovery one-time > 22680
    ps = st0()
    ev = evaluate(cfg, ps, mk([(D[0], 22680, 500), (D[1], 22700, 500), (D[2], 23000, 500)]), D[2])
    assert [k for k in kinds(ev) if k[0] == "recovery"] == [("recovery", D[1])], ev
    assert ps["recovery_sent"] == D[1]
    # seed: already support-hit -> disarmed; already above recovery -> marked; already below low -> disarmed
    rows_hi = mk([(f"2026-11-{d:02d}", 20000, 500) for d in (3, 4, 5, 6)])
    # need 20 prior for avg: base has Oct days
    sd = seed_state(cfg, mk([(f"2026-11-0{d}", 20000, 500) for d in (3, 4, 5)] + [("2026-11-06", 20000, 500)]), "T")
    assert sd["support_armed"] is False and sd["low_armed"] and sd["recovery_sent"] is None, sd
    sd = seed_state(cfg, mk([("2026-11-06", 18500, 500)]), "T")
    assert sd["low_armed"] is False and sd["support_armed"] and sd["above_streak"] == 0, sd
    sd = seed_state(cfg, mk([("2026-11-06", 23000, 500)]), "T")
    assert sd["recovery_sent"] == "2026-11-06", sd
    # confirmed / expected / lag
    r = mk([("2026-11-05", 22680, 1000), ("2026-11-06", 20490, 1000)])
    assert confirmed(r, "2026-11-06", datetime(2026, 11, 6, 15, 30, tzinfo=KST))[-1]["day"] == "2026-11-05"
    assert confirmed(r, "2026-11-06", datetime(2026, 11, 6, 19, 0, tzinfo=KST))[-1]["day"] == "2026-11-06"
    assert expected_day(date(2026, 11, 6), datetime(2026, 11, 6, 19, 0, tzinfo=KST)) == "2026-11-06"
    assert lag_days("2026-11-05", "2026-11-06") == 1
    # run pending / catch-up / ops / dry
    sent = []
    pub = lambda t, ti, m, p, tags, srv, config_name=None: sent.append((t, ti, p))  # noqa: E731
    st = {"price": st0()}
    run("auto", datetime(2026, 11, 9, 19, 0, tzinfo=KST), cfg, pub, lambda c: mk([("2026-11-06", 20490, 1000)]), st, save=False)
    assert sent == [] and st["price"]["pending"] == "2026-11-09", st
    run("auto", datetime(2026, 11, 10, 19, 0, tzinfo=KST), cfg, pub,
        lambda c: mk([("2026-11-06", 20490, 1000), (D[0], 20000, 500), (D[1], 20100, 500)]), st, save=False)
    assert any(s[1].startswith("[487240] KODEX AI전력핵심설비 | 종가") and "지지 확인" in s[1] for s in sent), sent
    st2 = {"price": st0()}
    run("auto", datetime(2026, 11, 10, 19, 0, tzinfo=KST), cfg, pub, lambda c: mk([("2026-11-06", 20490, 1000)]), st2, save=False)
    assert sent[-1][0] == "cwally-ops" and "조회 실패" in sent[-1][1], sent
    n = len(sent)
    run("auto", datetime(2026, 11, 10, 20, 0, tzinfo=KST), cfg, pub, lambda c: mk([("2026-11-06", 20490, 1000)]), st2, save=False)
    assert len(sent) == n

    def boom(c):
        raise RuntimeError("x")
    run("auto", datetime(2026, 11, 11, 19, 0, tzinfo=KST), cfg, pub, boom, st, save=False)
    run("auto", datetime(2026, 11, 11, 20, 0, tzinfo=KST), cfg, pub, boom, st, save=False)
    assert [s[0] for s in sent[n:]] == ["cwally-ops"]
    n = len(sent)
    run("auto", datetime(2027, 3, 1, 19, 0, tzinfo=KST), cfg, pub, boom, st, save=False)
    run("dry", datetime(2026, 11, 12, 19, 0, tzinfo=KST), cfg, pub, boom, {"price": st0()}, save=False)
    assert len(sent) == n
    msgs = messages(cfg, [
        {"kind": "support", "day": "2026-11-10", "close": 20000.0, "volume": 500.0, "avg": 1000.0, "streak": 4},
        {"kind": "low", "day": "2026-11-11", "close": 18500.0, "volume": 500.0, "avg": 1000.0},
        {"kind": "recovery", "day": "2026-11-12", "close": 23000.0, "volume": 500.0, "avg": 1000.0},
    ], "T")
    assert [m[2] for m in msgs] == [3, 4, 3]
    assert all(m[0].startswith("[487240]") for m in msgs)
    assert "19,000원 대비" in msgs[0][1] and "거래량 500주" in msgs[0][1] and "데이터센터" in msgs[0][1]
    assert "LS ELECTRIC" in msgs[0][1] or "상위 편입" in msgs[0][1]
    o = ops_msg("T", "x")
    assert "조회 실패" in o[0]
    for t, m, _ in msgs + [(*o, 3)]:
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
