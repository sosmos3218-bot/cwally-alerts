#!/usr/bin/env python3
"""[203780] TIGER 미국나스닥바이오 종가 34,700원 1차 검토선. 19:00 k203780 체인(sjm 바로 뒤).

확정 종가 <= 34,700원 -> p4 1차 검토선 도달. 종가 > 36,000원 뒤에만 재무장.
iNAV, 괴리, 호가는 조건이 아님. 판정은 정규장 확정 종가(scripts/krx_close.py)만.
당일 행은 15:40 KST 이후만. 네이버 일봉 안 씀.
첫 실행(state 에 last_date 없음): 마지막 확정 종가로 상태만 기록, 발송 없음.
19:00 에 당일 행이 없으면 price.pending 에 남기고 다음 실행에서 이어서 판정.
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
from kr_calendar import is_trading_day, prev_trading_day, session_day, session_close_ready  # noqa: E402
import k079900_monitor as k79  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = k79.KST
TAG = "[203780]"
NAME = "k203780r"
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
        f"{stamp} | 203780 TIGER 미국나스닥바이오 | 34,700원 1차 검토선",
        f"이게 뭐 알림: {what}",
        f"지금 결과/상태: {status}",
        f"지금 할 일: {action}",
        f"나중에: {later}",
        f"익절/줄일 때: {trim}",
        f"하지 말 것: {avoid}",
    ])


def messages(cfg: dict, ev: list[dict], stamp: str) -> list[tuple[str, str, int]]:
    out = []
    what = "TIGER 미국나스닥바이오(203780) 정규장 확정 종가가 1차 검토선 34,700원 이하로 내려왔다는 알림. 지금 신규 매수 신호가 아님. 자동 주문이 아님."
    for e in ev:
        c = e["close"]
        title = f"{TAG} TIGER 미국나스닥바이오 | 종가 {c:,.0f}원, {e['title']}"
        status = (
            f"{e['day']} 정규장 종가 {won(c)} (판정 거래일 {e['day']}). "
            f"34,700원 대비 {dist(c, 34700)}. "
            "조건은 정규장 종가뿐이며 iNAV, 괴리, 호가는 쓰지 않음."
        )
        action = "1차 검토선에 닿았으니 분할 지정가 여부를 직접 점검하세요. 선을 올려서 추격하지 마세요."
        later = (
            f"같은 선은 종가 {won(e['rearm'])} 초과 뒤에만 다시 옵니다. "
            f"감시는 {cfg['ends_kst'][:10]} 까지."
        )
        trim = "대기 판단의 검토선입니다. 이 알림은 주문이 아닙니다."
        avoid = "자동 매수 신호로 보기, 시장가 한 번에 사기, 장중 가격만 보고 추격하기, 검토선을 올려 따라가기. 투자 권유 아님."
        out.append((title, body(stamp, what, status, action, later, trim, avoid), int(e["priority"])))
    return out


def ops_msg(stamp: str, status: str) -> tuple[str, str]:
    return (
        f"[OPS] {TAG} TIGER 미국나스닥바이오 시세 조회 실패",
        body(
            stamp,
            "203780 1차 검토선 알림의 데이터 읽기 실패 안내. 하루 1번. 매매 신호 아님.",
            status,
            "증권사나 거래소 화면에서 203780 정규장 종가를 직접 확인하세요.",
            "다음 거래일 19:00 에 다시 읽고, 데이터 날짜로 이어서 판정합니다.",
            "이 안내로 계획을 바꾸지 않기.",
            "읽기 실패를 가격 신호로 읽기. 네이버 일봉으로 대신 판정하기.",
        ),
    )


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(st: dict) -> None:
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def check_text(title: str, msg: str) -> None:
    blob = title + msg
    assert "\u00b7" not in blob and "\u318d" not in blob and "\u2014" not in blob, title
    if title.startswith("[OPS]"):
        assert title.startswith("[OPS] [203780]"), title
        assert "조회 실패" in title, title
    else:
        assert title.startswith(TAG), title
        assert "203780" in title, title


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
                ops = ops_msg(stamp, f"203780 정규장 일별 시세가 {expect} 까지 와야 하는데 마지막 행이 {rows[-1]['day']} ({lag}거래일 지연). 다음 실행에서 이어서 판정.")
                st["ops_day"] = today
        print(f"price: {st['price']}")
    except Exception as exc:  # noqa: BLE001
        if st.get("ops_day") != today:
            ops = ops_msg(stamp, f"203780 시세 읽기 실패: {str(exc)[:160]}. state 는 그대로.")
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

    def st0(close=36915.0, day="2026-11-26"):
        return {"last_date": day, "last_close": close, "last_volume": 31092.0,
                "armed": {"review34700": True}}

    check("config one line 34700 / rearm 36000 / p4",
          len(cfg["lines"]) == 1 and cfg["lines"][0]["level"] == 34700
          and cfg["lines"][0]["rearm"] == 36000 and cfg["lines"][0]["priority"] == 4
          and cfg["lines"][0]["op"] == "le" and cfg["lines"][0]["rearm_op"] == "gt")
    check("title_prefix and topics",
          cfg["title_prefix"] == "[203780]" and cfg["ntfy_topic"] == "cwally-port"
          and cfg.get("ntfy_ops_topic") == "cwally-ops")
    check("ends 2027-06-30", cfg["ends_kst"] == "2027-06-30 23:59")

    sd = seed_state(cfg, mk([("2026-11-26", 36915)]), "T")
    check("seed above 34700 armed", sd["armed"] == {"review34700": True})
    sd2 = seed_state(cfg, mk([("2026-11-26", 34700)]), "T")
    check("seed at 34700 disarmed, no implied send", sd2["armed"] == {"review34700": False})

    ev = evaluate(cfg, st0(), mk([("2026-11-27", 34700)]), "2026-11-27")
    check("fires at 34700", [(e["key"], e["day"], e["close"]) for e in ev] == [("review34700", "2026-11-27", 34700.0)])
    ev = evaluate(cfg, st0(), mk([("2026-11-27", 34701)]), "2026-11-27")
    check("silent at 34701", ev == [])
    ev = evaluate(cfg, st0(), mk([("2026-11-27", 34699)]), "2026-11-27")
    check("fires at 34699", [e["key"] for e in ev] == ["review34700"])

    ps = st0()
    ev = evaluate(cfg, ps, mk([
        ("2026-11-27", 34700),
        ("2026-11-30", 34600),
        ("2026-12-01", 36000),
        ("2026-12-02", 36001),
        ("2026-12-03", 34700),
    ]), "2026-12-03")
    check("re-arm only above 36000",
          [(e["key"], e["day"]) for e in ev] == [("review34700", "2026-11-27"), ("review34700", "2026-12-03")])

    ev = evaluate(cfg, st0(), mk([("2026-11-27", 34700)]), "2026-11-27")
    msgs = messages(cfg, ev, "T")
    check("one message p4 and ticker", len(msgs) == 1 and msgs[0][2] == 4 and msgs[0][0].startswith("[203780]"))
    check("title wording", "종가 34,700원, 34,700원 1차 검토선 도달" in msgs[0][0])
    check("body has close and no chase", all(x in msgs[0][1] for x in ("34,700원", "36,000원", "올려서 추격하지 마세요")))
    check("body says no inav/premium/orderbook gate", "iNAV, 괴리, 호가는 쓰지 않음" in msgs[0][1])

    r = mk([("2026-11-26", 36915), ("2026-11-27", 36270)])
    check("before 15:40 excludes today",
          confirmed(r, "2026-11-27", datetime(2026, 11, 27, 15, 26, tzinfo=KST))[-1]["day"] == "2026-11-26")
    check("after 15:40 includes today",
          confirmed(r, "2026-11-27", datetime(2026, 11, 27, 19, 0, tzinfo=KST))[-1]["day"] == "2026-11-27")

    sent = []
    pub = lambda t, ti, m, p, tags, srv, config_name=None: sent.append((t, ti, p))  # noqa: E731
    st = {"price": st0()}
    run("dry", datetime(2026, 11, 27, 19, 0, tzinfo=KST), cfg, pub,
        lambda c: mk([("2026-11-26", 36915), ("2026-11-27", 36270)]), st, save=False)
    check("dry no publish when close above line", sent == [])
    check("dry does not write last_date into live file", load_state().get("price", {}).get("last_date") is None)

    st_first = {}
    run("dry", datetime(2026, 11, 27, 19, 0, tzinfo=KST), cfg, pub,
        lambda c: mk([("2026-11-26", 36915), ("2026-11-27", 34700)]), st_first, save=False)
    check("first run silent even if close at line", sent == [] and st_first["price"]["armed"]["review34700"] is False)

    st3 = {"price": st0()}
    run("auto", datetime(2026, 11, 27, 19, 0, tzinfo=KST), cfg, pub, lambda c: mk([("2026-11-26", 36915)]), st3, save=False)
    check("pending set when today's row missing", st3["price"].get("pending") == "2026-11-27")

    n = len(sent)
    run("auto", datetime(2026, 11, 27, 19, 0, tzinfo=KST), cfg, pub, lambda c: (_ for _ in ()).throw(RuntimeError("x")), st3, save=False)
    check("ops once on fetch fail", sent[n][0] == "cwally-ops" and sent[n][1].startswith("[OPS] [203780]") and "조회 실패" in sent[n][1])
    run("auto", datetime(2026, 11, 27, 20, 0, tzinfo=KST), cfg, pub, lambda c: (_ for _ in ()).throw(RuntimeError("x")), st3, save=False)
    check("ops not repeated same day", len(sent) == n + 1)

    ops_t, ops_m = ops_msg("T", "x")
    check_text(ops_t, ops_m)
    check_text(*msgs[0][:2])
    check("ops routes to cwally-ops", route_topic("cwally-port", ops_t) == "cwally-ops")
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
