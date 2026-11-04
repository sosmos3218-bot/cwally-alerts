#!/usr/bin/env python3
"""[036530] SNT홀딩스 종가 감시. 19:00 k203780 체인 같은 단계([010140] 삼성중공업 감시 바로 뒤). 보유 없음, 분할 매수 계획 보조.

1) buy1: 확정 종가 <= 42,000원 이면 p3 1번(1차 매수 구간: 목표 비중의 1/3, 42,000원 이하 지정가, 배당락 전 탐색 매수).
   종가가 44,000원 넘게 회복한 뒤에야 다시 무장.
2) stop: 확정 종가 <= 38,000원 첫날 p3 1번(38,000원 이탈 경고). 종가 <= 38,000원 이 20거래일 연속이면 p4 1번
   (4주 유지, 1차분 손절 검토). 종가 > 38,000원 이 한 번이라도 나오면 연속 일수 0, 두 알림 모두 다시 무장.
판정 날짜: 정규장 확정 종가(scripts/krx_close.py, Daum days, 실패 시 네이버 15:30 분봉). 당일 행은 15:40 KST 이후에만 확정으로 본다.
  (AGENTS.md 2026-11-03 규칙: 네이버 일봉 closePrice 는 시간외가 섞여 종가로 쓰지 않음.)
  state price.last_date 보다 새 날짜만 날짜순으로 판정하고(밀린 날짜 이어서), 판정한 날짜를 last_date 에 남긴다.
  19:00 에 당일(KRX 거래일) 행이 아직 없으면 그날은 판정하지 않고 price.pending 에 남긴 뒤 다음 실행에서 이어서 판정.
  기대 날짜보다 stale_trading_days(2) 거래일 이상 밀리면 cwally-ops 에 하루 1번 안내.
첫 실행(state 에 last_date 없음): 마지막 확정 종가로 상태만 기록, 발송 없음(이미 구간 안이면 그 조건은 발송한 것으로 처리).
읽기 실패는 cwally-ops 하루 1번(제목에 조회 실패). 감시 기간(daily_window, 2027-03-31 까지) 밖, KRX 휴장일은 조용히 끝남.
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
import k079900_monitor as k79  # noqa: E402  (읽기 전용 재사용: KST)
from kr_calendar import is_trading_day, prev_trading_day, session_day  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = k79.KST
TAG = "[036530]"
NAME = "sntholdings"
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
    b, s = cfg["buy1"], cfg["stop"]
    streak = 0
    for x in reversed(rows):
        if x["close"] <= s["level"]:
            streak += 1
        else:
            break
    return {"last_date": r["day"], "last_close": r["close"], "last_volume": r.get("volume"),
            "buy1_armed": not r["close"] <= b["level"],
            "warn_armed": streak == 0,
            "hold_armed": streak < s["days"],
            "below_streak": streak,
            "pending": None,
            "seed_note": f"첫 실행 {stamp}: 마지막 확정 종가 {r['day']} {r['close']:,.0f}원, 거래량 {r.get('volume') or 0:,.0f}주"}


def evaluate(cfg: dict, ps: dict, rows: list[dict], today: str) -> list[dict]:
    b, s = cfg["buy1"], cfg["stop"]
    out = []
    for row in rows:
        d, c = row["day"], row["close"]
        if d <= ps["last_date"] or d < cfg["daily_window"]["start"] or d > today or d > cfg["daily_window"]["end"]:
            continue
        if c <= b["level"] and ps.get("buy1_armed", True):
            ps["buy1_armed"] = False
            out.append({"kind": "buy1", "day": d, "close": c})
        elif c > b["rearm_above"] and not ps.get("buy1_armed", True):
            ps["buy1_armed"] = True
        if c <= s["level"]:
            ps["below_streak"] = int(ps.get("below_streak") or 0) + 1
            if ps.get("warn_armed", True):
                ps["warn_armed"] = False
                out.append({"kind": "warn", "day": d, "close": c})
            if ps["below_streak"] >= s["days"] and ps.get("hold_armed", True):
                ps["hold_armed"] = False
                out.append({"kind": "hold", "day": d, "close": c, "streak": ps["below_streak"]})
        else:
            ps["below_streak"] = 0
            ps["warn_armed"] = True
            ps["hold_armed"] = True
        ps["last_date"], ps["last_close"], ps["last_volume"] = d, c, row.get("volume")
    return out


def body(stamp: str, hdr: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([f"{stamp} | 036530 SNT홀딩스 | {hdr}", f"이게 뭐 알림: {what}", f"지금 결과/상태: {status}",
                      f"지금 할 일: {action}", f"나중에: {later}", f"익절/줄일 때: {trim}", f"하지 말 것: {avoid}"])


def dist(c: float, lv: float) -> str:
    return f"{(c / lv - 1) * 100:+.1f}% ({c - lv:+,.0f}원)"


def messages(cfg: dict, ev: list[dict], stamp: str) -> list[tuple[str, str, int]]:
    b, s, out = cfg["buy1"], cfg["stop"], []
    for e in ev:
        c = e["close"]
        if e["kind"] == "buy1":
            under = c <= s["level"]
            action = ("1차 계획: 목표 비중의 1/3, 42,000원 이하 지정가, 배당락 전 탐색 매수. 한 번에 다 사지 말고 지정가로 나눠서."
                      if not under else
                      "종가가 이미 38,000원 이하라 1차 매수는 보류. 같이 온 38,000원 이탈 경고를 먼저 보고 계획을 다시 점검.")
            out.append((f"{TAG} SNT홀딩스 1차 매수 구간", body(
                stamp, "42,000원 1차 매수 구간", "SNT홀딩스(036530, KOSPI) 정규장 확정 종가가 42,000원 이하로 내려오면 오는 알림. 보유 없음, 분할 매수 계획 보조. 매매 지시 아님.",
                f"{e['day']} 종가 {c:,.0f}원 (판정 거래일 {e['day']}). 42,000원 대비 {dist(c, b['level'])}, 38,000원 대비 {dist(c, s['level'])}.",
                action,
                f"이 알림은 한 번만 옵니다. 종가가 {b['rearm_above']:,}원 넘게 회복한 뒤 다시 42,000원 이하로 오면 다시 알림. 감시는 2027-03-31 까지.",
                "종가 38,000원 이하면 이탈 경고, 20거래일 연속이면 1차분 손절 검토 알림이 따로 옵니다.",
                "장중 가격만 보고 사기, 남은 2/3 까지 한 번에 채우기, 신용 및 미수. 투자 권유 아님."), b["priority"]))
        elif e["kind"] == "warn":
            out.append((f"{TAG} SNT홀딩스 38,000원 이탈", body(
                stamp, "38,000원 이탈 경고", "SNT홀딩스(036530) 정규장 확정 종가가 38,000원 이하로 처음 마감한 날 오는 경고. 매매 지시 아님.",
                f"{e['day']} 종가 {c:,.0f}원 (판정 거래일 {e['day']}). 38,000원 대비 {dist(c, s['level'])}. 38,000원 이하 연속 1거래일째.",
                "추가 매수 멈춤. 1차분을 샀다면 손절 기준(38,000원 이하 20거래일 연속)을 다시 확인.",
                f"종가 38,000원 이하가 {s['days']}거래일 연속이면 1차분 손절 검토 알림(p4)이 옵니다. 종가가 38,000원을 넘으면 연속 일수 0, 경고 다시 무장. 감시는 2027-03-31 까지.",
                "아직 손절 확정 아님. 4주 유지 알림을 기준으로 판단.",
                "이탈 당일 물타기, 기준을 미루며 버티기. 투자 권유 아님."), s["warn_priority"]))
        else:
            out.append((f"{TAG} SNT홀딩스 38,000원 4주 유지, 1차분 손절 검토", body(
                stamp, "38,000원 이하 20거래일 연속", "SNT홀딩스(036530) 정규장 확정 종가가 38,000원 이하로 20거래일 연속 마감하면 오는 알림. 매매 지시 아님.",
                f"{e['day']} 종가 {c:,.0f}원 (판정 거래일 {e['day']}). 38,000원 이하 {e['streak']}거래일 연속, 38,000원 대비 {dist(c, s['level'])}.",
                "1차분 손절 검토. 계획대로 정리 또는 축소를 판단하세요.",
                "이 알림은 한 번만 옵니다. 종가가 38,000원을 넘은 뒤 다시 20거래일 연속 이하이면 다시 알림. 감시는 2027-03-31 까지.",
                "손절 검토 구간이라 추가 매수보다 줄이는 쪽 먼저.",
                "손절 구간에서 물타기, 기준을 미루며 버티기. 투자 권유 아님."), s["hold_priority"]))
    return out


def ops_msg(stamp: str, status: str) -> tuple[str, str]:
    return (f"{TAG} SNT홀딩스 시세 조회 실패 | 원문 확인 필요", body(
        stamp, "데이터 읽기 실패", "036530 종가 감시의 데이터 읽기 실패 안내. 하루 1번. 매매 신호 아님.", status,
        "Daum 금융이나 네이버 증권에서 036530 일별 시세를 직접 보세요.", "다음 거래일 19:00 에 다시 읽고, 데이터 날짜로 이어서 판정합니다.",
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
                ops = ops_msg(stamp, f"036530 정규장 일별 시세가 {expect} 까지 와야 하는데 마지막 행이 {rows[-1]['day']} ({lag}거래일 지연). 다음 실행에서 이어서 판정.")
                st["ops_day"] = today
        print(f"price: {st['price']}")
    except Exception as exc:  # noqa: BLE001
        if st.get("ops_day") != today:
            ops = ops_msg(stamp, f"036530 시세 읽기 실패: {str(exc)[:160]}. state 는 그대로.")
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
    base = [(f"2026-10-{d:02d}", 45000) for d in range(1, 31)]

    def mk(trip):
        return [{"day": d, "close": float(c), "volume": 1000.0} for d, c in base + trip]

    def days(n, start=date(2026, 11, 4)):
        out, d = [], start
        while len(out) < n:
            if is_trading_day(d):
                out.append(d.isoformat())
            d = date.fromordinal(d.toordinal() + 1)
        return out
    st0 = lambda: {"last_date": "2026-11-03", "last_close": 43700.0, "buy1_armed": True, "warn_armed": True, "hold_armed": True, "below_streak": 0}  # noqa: E731
    kinds = lambda ev: [(e["kind"], e["day"]) for e in ev]  # noqa: E731
    D = days(60)
    # buy1: 42,000 이하 1번, 44,000 이하 회복은 재무장 안 함, 44,000 정확히도 안 됨, 44,000 초과 뒤 재발송
    ps = st0()
    ev = evaluate(cfg, ps, mk([(D[0], 42001), (D[1], 42000), (D[2], 41000), (D[3], 44000), (D[4], 41500), (D[5], 44010), (D[6], 42000)]), D[6])
    assert kinds(ev) == [("buy1", D[1]), ("buy1", D[6])], ev
    assert ps["last_date"] == D[6] and not ps["buy1_armed"], ps
    # stop warn: 38,000 이하 첫날 1번(38,000 정확히 포함), 이하 유지 중 반복 없음, 38,000 초과 뒤 재무장, 38,001 은 이하 아님
    ps = st0()
    ev = evaluate(cfg, ps, mk([(D[0], 38001), (D[1], 38000), (D[2], 37000), (D[3], 38001), (D[4], 37900)]), D[4])
    assert [k for k in kinds(ev) if k[0] == "warn"] == [("warn", D[1]), ("warn", D[4])], ev
    assert ps["below_streak"] == 1 and not ps["warn_armed"] and ps["hold_armed"], ps
    # 43,700 에서 바로 37,000 으로 갭 하락: buy1 과 warn 이 같은 날 둘 다, buy1 본문에 보류 문구
    ps = st0()
    ev = evaluate(cfg, ps, mk([(D[0], 37000)]), D[0])
    assert kinds(ev) == [("buy1", D[0]), ("warn", D[0])], ev
    m = messages(cfg, [ev[0]], "T")[0]
    assert "1차 매수는 보류" in m[1] and m[2] == 3, m
    # hold: 20거래일 연속 이하면 p4 1번(19일째는 없음), 21일째 반복 없음
    ps = st0()
    ev = evaluate(cfg, ps, mk([(D[i], 37000) for i in range(19)]), D[18])
    assert [k for k, _ in kinds(ev)] == ["buy1", "warn"] and ps["below_streak"] == 19, ev
    ev = evaluate(cfg, ps, mk([(D[i], 37000) for i in range(21)]), D[20])
    assert kinds(ev) == [("hold", D[19])] and ev[0]["streak"] == 20 and ps["below_streak"] == 21, ev
    # 38,000 초과 하루로 연속 리셋, 둘 다 재무장: 다시 경고 1번, 20일 다시 세야 p4
    seq = [(D[i], 37000) for i in range(21)] + [(D[21], 38500)] + [(D[i], 37500) for i in range(22, 42)]
    ps = st0()
    ev = evaluate(cfg, ps, mk(seq), D[41])
    assert [k for k in kinds(ev) if k[0] != "buy1"] == [("warn", D[0]), ("hold", D[19]), ("warn", D[22]), ("hold", D[41])], ev
    # 19일 이하 뒤 리셋되면 p4 없음
    ps = st0()
    ev = evaluate(cfg, ps, mk([(D[i], 37000) for i in range(19)] + [(D[19], 38100)] + [(D[i], 37000) for i in range(20, 39)]), D[38])
    assert "hold" not in [k for k, _ in kinds(ev)] and ps["below_streak"] == 19, ev
    # 밀린 날짜 이어서(catch-up): 한 번에 여러 날 판정, 날짜순
    ps = st0()
    ev = evaluate(cfg, ps, mk([("2026-11-03", 43700), (D[0], 41900), (D[1], 37900), (D[2], 37800)]), D[2])
    assert kinds(ev) == [("buy1", D[0]), ("warn", D[1])] and ps["below_streak"] == 2 and ps["last_date"] == D[2], ev
    # seed 날짜 이하, 종료일 뒤는 판정 안 함
    assert evaluate(cfg, st0(), mk([("2026-11-03", 30000)]), "2026-11-04") == []
    assert evaluate(cfg, st0(), mk([("2027-04-01", 30000)]), "2027-04-01") == []
    # seed: 이미 구간 안이면 발송한 것으로
    sd = seed_state(cfg, mk([("2026-11-02", 37000), ("2026-11-03", 37500)]), "T")
    assert sd["buy1_armed"] is False and sd["warn_armed"] is False and sd["hold_armed"] is True and sd["below_streak"] == 2, sd
    sd = seed_state(cfg, mk([("2026-11-03", 41000)]), "T")
    assert sd["buy1_armed"] is False and sd["warn_armed"] and sd["hold_armed"] and sd["below_streak"] == 0, sd
    sd = seed_state(cfg, mk([("2026-11-03", 43700)]), "T")
    assert sd["buy1_armed"] and sd["warn_armed"] and sd["hold_armed"], sd
    # 확정 종가: 15:40 전 당일 행 제외, 19:00 포함
    r = mk([("2026-11-03", 43700), ("2026-11-04", 41000)])
    assert confirmed(r, "2026-11-04", datetime(2026, 11, 4, 15, 30, tzinfo=KST))[-1]["day"] == "2026-11-03"
    assert confirmed(r, "2026-11-04", datetime(2026, 11, 4, 19, 0, tzinfo=KST))[-1]["day"] == "2026-11-04"
    assert expected_day(date(2026, 11, 4), datetime(2026, 11, 4, 19, 0, tzinfo=KST)) == "2026-11-04"
    assert expected_day(date(2026, 11, 4), datetime(2026, 11, 4, 10, 0, tzinfo=KST)) == "2026-11-03"
    assert lag_days("2026-11-03", "2026-11-04") == 1 and lag_days("2026-11-02", "2026-11-04") == 2
    # run(): 19:00 당일 행 없으면 pending, 발송 없음; 다음 날 이어서 판정; 실패는 ops 1번, 같은 날 두 번 안 보냄
    sent = []
    pub = lambda t, ti, m, p, tags, srv, config_name=None: sent.append((t, ti, p))  # noqa: E731
    st = {"price": st0()}
    run("auto", datetime(2026, 11, 4, 19, 0, tzinfo=KST), cfg, pub, lambda c: mk([("2026-11-03", 43700)]), st, save=False)
    assert sent == [] and st["price"]["pending"] == "2026-11-04", st
    run("auto", datetime(2026, 11, 5, 19, 0, tzinfo=KST), cfg, pub,
        lambda c: mk([("2026-11-03", 43700), ("2026-11-04", 41900), ("2026-11-05", 41800)]), st, save=False)
    assert [(s[0], s[1], s[2]) for s in sent] == [("cwally-port", "[036530] SNT홀딩스 1차 매수 구간", 3)] and st["price"]["last_date"] == "2026-11-05", sent
    assert st["price"]["pending"] is None
    st2 = {"price": st0()}
    run("auto", datetime(2026, 11, 5, 19, 0, tzinfo=KST), cfg, pub, lambda c: mk([("2026-11-03", 43700)]), st2, save=False)
    assert sent[-1][0] == "cwally-ops" and "조회 실패" in sent[-1][1], sent
    n = len(sent)
    run("auto", datetime(2026, 11, 5, 20, 0, tzinfo=KST), cfg, pub, lambda c: mk([("2026-11-03", 43700)]), st2, save=False)
    assert len(sent) == n

    def boom(c):
        raise RuntimeError("x")
    run("auto", datetime(2026, 11, 6, 19, 0, tzinfo=KST), cfg, pub, boom, st, save=False)
    run("auto", datetime(2026, 11, 6, 20, 0, tzinfo=KST), cfg, pub, boom, st, save=False)
    assert [s[0] for s in sent[n:]] == ["cwally-ops"] and "조회 실패" in sent[n][1], sent
    # 기간 밖(2027-04-01), 휴장: 조용히
    n = len(sent)
    run("auto", datetime(2027, 4, 1, 19, 0, tzinfo=KST), cfg, pub, boom, st, save=False)
    run("auto", datetime(2026, 11, 7, 19, 0, tzinfo=KST), cfg, pub, boom, st, save=False)
    assert len(sent) == n
    # dry: 발송 없음
    run("dry", datetime(2026, 11, 10, 19, 0, tzinfo=KST), cfg, pub, boom, {"price": st0()}, save=False)
    assert len(sent) == n
    # 문구 규칙, 제목 형식, 우선순위, 토픽
    msgs = messages(cfg, [{"kind": "buy1", "day": "2026-11-04", "close": 41900.0},
                          {"kind": "warn", "day": "2026-11-05", "close": 37900.0},
                          {"kind": "hold", "day": "2026-12-03", "close": 37000.0, "streak": 20}], "T")
    assert [m[0] for m in msgs] == ["[036530] SNT홀딩스 1차 매수 구간", "[036530] SNT홀딩스 38,000원 이탈",
                                    "[036530] SNT홀딩스 38,000원 4주 유지, 1차분 손절 검토"], msgs
    assert [m[2] for m in msgs] == [3, 3, 4]
    assert "목표 비중의 1/3" in msgs[0][1] and "42,000원 이하 지정가" in msgs[0][1] and "배당락 전 탐색 매수" in msgs[0][1] and "41,900원" in msgs[0][1]
    o = ops_msg("T", "x")
    assert o[0] == "[036530] SNT홀딩스 시세 조회 실패 | 원문 확인 필요", o[0]
    for t, m, _ in msgs + [(*o, 3)]:
        check_text(t, m)
        assert ("조회 실패" in t) == (t == o[0]), t
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
