#!/usr/bin/env python3
"""[036570] 엔씨소프트 종가 분할매수 감시. 19:00 k203780 체인(truentrust 바로 뒤).

1) buy1: 확정 종가 <= 175,000원 -> p3 1차 분할매수 검토 (계획 비중 50%). 종가 > 181,000원 뒤 재무장.
2) buy2: 확정 종가 <= 160,000원 -> p3 2차 분할매수 검토 (30%). 종가 > 166,000원 뒤 재무장.
3) buy3: 확정 종가 <= 145,000원 -> p4 3차 분할매수 검토 (20%), 추가 하락 시 논리 재점검. 종가 > 151,000원 뒤 재무장.
같은 날 여러 선이 걸리면 가장 깊은 선(가장 낮은 가격) 한 통만 보내고, 같이 걸린 선을 본문에 적음.
판정: 정규장 확정 종가(scripts/krx_close.py). 당일 행은 15:40 KST 이후만. 네이버 일봉 안 씀.
첫 실행(state 에 last_date 없음): 마지막 확정 종가로 상태만 기록, 발송 없음.
19:00 에 당일 행이 없으면 price.pending 에 남기고 다음 실행에서 이어서 판정.
modes: auto | dry(발송 없음, state 안 씀) | selftest ; --as-of "YYYY-MM-DD HH:MM" (dry 전용)
"""
from __future__ import annotations

import json
import sys
import traceback
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_calendar import is_trading_day, prev_trading_day, session_day, session_close_ready  # noqa: E402
import k079900_monitor as k79  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = k79.KST
TAG = "[036570]"
NAME = "k036570"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
CLOSE_HM = (15, 40)
OPS = {"le": lambda v, x: v <= x, "lt": lambda v, x: v < x, "ge": lambda v, x: v >= x, "gt": lambda v, x: v > x}


def won(v) -> str:
    return f"{v:,.0f}원" if v is not None else "?"


def dist(c: float, lv: float) -> str:
    return f"{(c / lv - 1) * 100:+.1f}% ({c - lv:+,.0f}원)"


def fetch_rows(cfg: dict) -> list[dict]:
    import krx_close
    rows = [{"day": b.day, "close": float(b.close), "volume": b.volume} for b in krx_close.regular_bars(cfg["code"], 45)]
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


def seed_state(cfg: dict, rows: list[dict], stamp: str) -> dict:
    r = rows[-1]
    armed = {ln["key"]: not OPS[ln["op"]](r["close"], ln["level"]) for ln in cfg["lines"]}
    return {
        "last_date": r["day"],
        "last_close": r["close"],
        "last_volume": r.get("volume"),
        "armed": armed,
        "pending": None,
        "seed_note": f"첫 실행 {stamp}: 마지막 확정 종가 {r['day']} {won(r['close'])}, 거래량 {r.get('volume') or 0:,.0f}주",
    }


def evaluate(cfg: dict, ps: dict, rows: list[dict], today: str) -> list[dict]:
    out = []
    armed = ps.setdefault("armed", {})
    for row in rows:
        d, c = row["day"], row["close"]
        if d <= ps["last_date"] or d < cfg["daily_window"]["start"] or d > today or d > cfg["daily_window"]["end"]:
            continue
        for ln in cfg["lines"]:
            k = ln["key"]
            if OPS[ln["op"]](c, ln["level"]) and armed.get(k, True):
                armed[k] = False
                out.append({**ln, "day": d, "close": c, "volume": row.get("volume")})
            elif OPS[ln["rearm_op"]](c, ln["rearm"]) and not armed.get(k, True):
                armed[k] = True
        ps["last_date"], ps["last_close"], ps["last_volume"] = d, c, row.get("volume")
    return out


def body(stamp: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([
        f"{stamp} | 036570 엔씨소프트 | 분할매수 종가 선",
        f"이게 뭐 알림: {what}",
        f"지금 결과/상태: {status}",
        f"지금 할 일: {action}",
        f"나중에: {later}",
        f"익절/줄일 때: {trim}",
        f"하지 말 것: {avoid}",
    ])


def checklist_text(cfg: dict) -> str:
    items = ", ".join(cfg.get("checklist") or [])
    return f"점검 항목: {items}. {cfg.get('start_condition')}"


def distances(cfg: dict, c: float) -> str:
    parts = [f"{ln['title'].split(',')[0]} {won(ln['level'])} 대비 {dist(c, ln['level'])}" for ln in cfg["lines"]]
    return "; ".join(parts)


def messages(cfg: dict, ev: list[dict], stamp: str) -> list[tuple[str, str, int]]:
    by_day: dict[str, list[dict]] = defaultdict(list)
    for e in ev:
        by_day[e["day"]].append(e)
    out = []
    what = "엔씨소프트(036570) 정규장 확정 종가가 미리 정한 분할매수 선에 닿았다는 알림. 매매 지시 아님. 자동 주문이 아님."
    low52 = cfg.get("week52_low")
    for day, hits in by_day.items():
        deep = min(hits, key=lambda x: x["level"])
        c = deep["close"]
        others = [h for h in hits if h["key"] != deep["key"]]
        extra = ""
        if others:
            extra = " 같은 날 같이 도달: " + ", ".join(h["title"] for h in sorted(others, key=lambda x: x["level"])) + "."
        title = f"{TAG} 엔씨소프트 036570 | 종가 {c:,.0f}원, {deep['title']}"
        if others:
            title += " (상위 선도 도달)"
        status = (
            f"{day} 종가 {won(c)} (판정 거래일 {day}). {distances(cfg, c)}. "
            f"52주 저점 {won(low52)}(참고). 걸린 선: {deep['title']}.{extra}"
        )
        action = (
            f"{deep['title']} 기준으로 분할매수 계획을 점검. {checklist_text(cfg)} "
            "한 번에 다 사지 말고 지정가로 나눠서."
        )
        later = (
            f"같은 선은 종가 {won(deep['rearm'])} 초과 뒤에만 다시 옵니다. "
            "다른 선은 각각 재무장됩니다. "
            f"감시는 {cfg['ends_kst'][:10]} 까지."
        )
        trim = "미보유. 이 알림은 주문이 아님. 추가 하락 시(특히 3차) 논리를 다시 점검."
        avoid = "자동 매수 신호로 보기, 시장가 한 번에 사기, 장중 가격만 보고 사기, 신용 및 미수. 투자 권유 아님."
        out.append((title, body(stamp, what, status, action, later, trim, avoid), int(deep["priority"])))
    return out


def ops_msg(stamp: str, status: str) -> tuple[str, str]:
    return (f"{TAG} 엔씨소프트 036570 시세 조회 실패 | 원문 확인 필요", body(
        stamp, "엔씨소프트 분할매수 알림의 데이터 읽기 실패 안내. 하루 1번. 매매 신호 아님.", status,
        "Daum 금융이나 네이버 증권에서 036570 일별 시세를 직접 보세요.",
        "다음 거래일 19:00 에 다시 읽고, 데이터 날짜로 이어서 판정합니다.",
        "이 안내로 계획을 바꾸지 않기.", "읽기 실패를 가격 신호로 읽기. 네이버 일봉으로 대신 판정하기."))


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
    assert "036570" in title, title


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
                ops = ops_msg(stamp, f"036570 정규장 일별 시세가 {expect} 까지 와야 하는데 마지막 행이 {rows[-1]['day']} ({lag}거래일 지연). 다음 실행에서 이어서 판정.")
                st["ops_day"] = today
        print(f"price: {st['price']}")
    except Exception as exc:  # noqa: BLE001
        if st.get("ops_day") != today:
            ops = ops_msg(stamp, f"036570 시세 읽기 실패: {str(exc)[:160]}. state 는 그대로.")
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
    fails = 0

    def check(label, ok):
        nonlocal fails
        fails += not ok
        print(("ok  " if ok else "FAIL"), label)

    def mk(pairs):
        return [{"day": d, "close": float(c), "volume": 1000.0} for d, c in pairs]

    def st0(close=179000.0, day="2026-11-13"):
        return {"last_date": day, "last_close": close, "last_volume": 158358.0,
                "armed": {"buy1": True, "buy2": True, "buy3": True}}

    sd = seed_state(cfg, mk([("2026-11-13", 179000)]), "T")
    check("seed above 175k all armed", sd["armed"] == {"buy1": True, "buy2": True, "buy3": True})
    sd2 = seed_state(cfg, mk([("2026-11-13", 175000)]), "T")
    check("seed at 175k disarms buy1 only", sd2["armed"] == {"buy1": False, "buy2": True, "buy3": True})

    ev = evaluate(cfg, st0(), mk([("2026-11-16", 180000), ("2026-11-17", 175000)]), "2026-11-17")
    check("buy1 fires at 175000", [(e["key"], e["day"]) for e in ev] == [("buy1", "2026-11-17")])

    ev = evaluate(cfg, st0(), mk([("2026-11-16", 160000)]), "2026-11-16")
    check("buy2 at 160000 also fires buy1", [e["key"] for e in ev] == ["buy1", "buy2"])

    ev = evaluate(cfg, st0(), mk([("2026-11-16", 145000)]), "2026-11-16")
    check("buy3 at 145000 fires all three", [e["key"] for e in ev] == ["buy1", "buy2", "buy3"])

    # re-arm: stay below rearm, then exceed, then hit again
    ps = st0()
    ev = evaluate(cfg, ps, mk([
        ("2026-11-16", 175000),  # fire buy1
        ("2026-11-17", 174000),  # no refire
        ("2026-11-18", 181000),  # exactly 181000: no rearm (needs >)
        ("2026-11-19", 181001),  # rearm
        ("2026-11-20", 174000),  # fire again
    ]), "2026-11-20")
    check("buy1 re-arm only above 181000", [(e["key"], e["day"]) for e in ev] == [("buy1", "2026-11-16"), ("buy1", "2026-11-20")])

    ps = st0()
    ev = evaluate(cfg, ps, mk([
        ("2026-11-16", 160000),
        ("2026-11-17", 166000),
        ("2026-11-18", 166001),
        ("2026-11-19", 159000),
    ]), "2026-11-19")
    check("buy2 re-arm only above 166000",
          [(e["key"], e["day"]) for e in ev if e["key"] == "buy2"] == [("buy2", "2026-11-16"), ("buy2", "2026-11-19")])

    ps = st0()
    ev = evaluate(cfg, ps, mk([
        ("2026-11-16", 144000),
        ("2026-11-17", 151000),
        ("2026-11-18", 151001),
        ("2026-11-19", 144000),
    ]), "2026-11-19")
    check("buy3 re-arm only above 151000",
          [(e["key"], e["day"]) for e in ev if e["key"] == "buy3"] == [("buy3", "2026-11-16"), ("buy3", "2026-11-19")])

    # combined message: one day, three lines -> one message, deepest, p4
    ev = evaluate(cfg, st0(), mk([("2026-11-16", 144000)]), "2026-11-16")
    msgs = messages(cfg, ev, "T")
    check("multi-line one message", len(msgs) == 1)
    check("deepest title and p4", msgs[0][2] == 4 and "3차 분할매수 검토 (20%)" in msgs[0][0] and "상위 선도 도달" in msgs[0][0])
    check("mentions other rungs", "1차 분할매수 검토 (계획 비중 50%)" in msgs[0][1] and "2차 분할매수 검토 (30%)" in msgs[0][1])
    check("ticker in title", msgs[0][0].startswith("[036570]") and "036570" in msgs[0][0][8:])
    check("body has close, distances, 52w, checklist, start condition",
          all(x in msgs[0][1] for x in ("144,000원", "175,000원 대비", "160,000원 대비", "145,000원 대비",
                                        "176,100원", "아이온2 매출 안정 여부", "4분기 잠정실적 영업이익률",
                                        "신작 일정", "시작 조건", "자동 주문이 아님")))

    m1 = messages(cfg, evaluate(cfg, st0(), mk([("2026-11-16", 175000)]), "2026-11-16"), "T")
    check("single buy1 title p3", m1[0][2] == 3 and "1차 분할매수 검토 (계획 비중 50%)" in m1[0][0] and "상위 선도" not in m1[0][0])

    # confirmed / expected
    r = mk([("2026-11-13", 179000), ("2026-11-16", 181700)])
    check("before 15:40 excludes today", confirmed(r, "2026-11-16", datetime(2026, 11, 16, 11, 30, tzinfo=KST))[-1]["day"] == "2026-11-13")
    check("after 15:40 includes today", confirmed(r, "2026-11-16", datetime(2026, 11, 16, 19, 0, tzinfo=KST))[-1]["day"] == "2026-11-16")

    sent = []
    pub = lambda t, ti, m, p, tags, srv, config_name=None: sent.append((t, ti, p))  # noqa: E731
    st = {"price": st0()}
    run("dry", datetime(2026, 11, 17, 19, 0, tzinfo=KST), cfg, pub,
        lambda c: mk([("2026-11-13", 179000), ("2026-11-16", 175000), ("2026-11-17", 174000)]), st, save=False)
    check("dry no publish", sent == [])
    check("dry does not write last_date into live file", load_state().get("price", {}).get("last_date") == "2026-11-13")

    run("auto", datetime(2026, 11, 16, 19, 0, tzinfo=KST), cfg, pub,
        lambda c: mk([("2026-11-13", 179000)]), {"price": st0()}, save=False)
    check("pending when today's row missing", True)  # asserted below via last call st3
    st3 = {"price": st0()}
    run("auto", datetime(2026, 11, 16, 19, 0, tzinfo=KST), cfg, pub, lambda c: mk([("2026-11-13", 179000)]), st3, save=False)
    check("pending set", st3["price"].get("pending") == "2026-11-16")
    check("no port send when pending", all(s[0] != "cwally-port" or "조회 실패" in s[1] for s in sent[-1:]))

    n = len(sent)
    run("auto", datetime(2026, 11, 16, 19, 0, tzinfo=KST), cfg, pub, lambda c: (_ for _ in ()).throw(RuntimeError("x")), st3, save=False)
    check("ops once on fetch fail", sent[n][0] == "cwally-ops" and "조회 실패" in sent[n][1])
    run("auto", datetime(2026, 11, 16, 20, 0, tzinfo=KST), cfg, pub, lambda c: (_ for _ in ()).throw(RuntimeError("x")), st3, save=False)
    check("ops not repeated same day", len(sent) == n + 1)

    for t, m, _ in msgs + m1 + [(*ops_msg("T", "x"), 3)]:
        check_text(t, m)
    check("ops routes to cwally-ops", route_topic("cwally-port", ops_msg("T", "x")[0]) == "cwally-ops")
    check("alert stays on port", route_topic("cwally-port", msgs[0][0]) == "cwally-port")

    print("selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


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
