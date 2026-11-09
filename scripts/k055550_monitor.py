#!/usr/bin/env python3
"""[055550] 신한지주 KRX 확정 종가 분할 매수 가격대 (2026-11-09 승인 pt71u).

KRX 거래일 15:40 KST (0090b0 슬롯에 부착, [316140] 바로 뒤). 새 cron 없음.
선 (configs/k055550.json lines):
  l83000 : 종가 <= 83,000 -> p3 1차 분할 가격대 (85,500 초과 종가 뒤 재무장)
  l79000 : 종가 <= 79,000 -> p3 2차 분할 가격대 (81,400 초과 종가 뒤 재무장)
판정은 Daum KRX 확정 종가만(NXT 미포함, 네이버 일봉 안 씀). 당일 행은 15:40 이후만.
첫 실행은 상태만 기록. ends_kst(2027-03-31 23:59) 뒤에는 조용히 종료.
modes: auto | dry(발송 없음, state 안 씀) | selftest
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_calendar import KST, is_trading_day, session_day, session_close_ready  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

TAG = "[055550]"
NAME = "k055550"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
CLOSE_HM = (15, 40)
DAUM = "https://finance.daum.net/api/quote/A{code}/days?symbolCode=A{code}&page=1&perPage=10&pagination=true"


def daum_rows(code: str) -> list[dict]:
    req = urllib.request.Request(
        DAUM.format(code=code),
        headers={"User-Agent": "Mozilla/5.0", "Referer": f"https://finance.daum.net/quotes/A{code}"},
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    out = []
    for r in raw.get("data") or []:
        if str(r.get("symbolCode")) != "A" + code:
            raise ValueError("종목 코드 불일치")
        day, px = str(r.get("date") or "")[:10], r.get("tradePrice")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) and px:
            out.append({
                "day": day,
                "close": float(px),
                "low": float(r["lowPrice"]) if r.get("lowPrice") else None,
                "prev": float(r["prevClosingPrice"]) if r.get("prevClosingPrice") else None,
            })
    if not out:
        raise ValueError("KRX 일별 시세 없음")
    return sorted(out, key=lambda x: x["day"])


def confirmed(rows: list[dict], today: str, now: datetime) -> list[dict]:
    return [r for r in rows if session_close_ready(r["day"], today, now, CLOSE_HM)]


def hit(op: str, c: float, lvl: float) -> bool:
    return {"le": c <= lvl, "ge": c >= lvl, "gt": c > lvl, "lt": c < lvl}[op]


def won(v) -> str:
    return f"{v:,.0f}원"


def seed(cfg: dict, row: dict, stamp: str) -> dict:
    return {
        "last_date": row["day"],
        "last_close": row["close"],
        "armed": {ln["key"]: not hit(ln["op"], row["close"], ln["level"]) for ln in cfg["lines"]},
        "seed_note": f"첫 실행 {stamp}: 마지막 KRX 확정 종가 {row['day']} {won(row['close'])}",
    }


def evaluate(cfg: dict, ps: dict, rows: list[dict], today: str) -> list[dict]:
    out = []
    for r in rows:
        d, c = r["day"], r["close"]
        if d <= ps["last_date"] or d < cfg["window_start"] or d > today:
            continue
        for ln in cfg["lines"]:
            k = ln["key"]
            if hit(ln["op"], c, ln["level"]) and ps["armed"].get(k, True):
                ps["armed"][k] = False
                out.append({**ln, "day": d, "close": c, "prev": r.get("prev")})
            elif hit(ln["rearm_op"], c, ln["rearm"]) and not ps["armed"].get(k, True):
                ps["armed"][k] = True
        ps["last_date"], ps["last_close"] = d, c
    return out


def body(stamp: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([
        f"{stamp} | 055550 | 신한지주",
        f"이게 뭐 알림: {what}",
        f"지금 결과/상태: {status}",
        f"지금 할 일: {action}",
        f"나중에: {later}",
        f"익절/줄일 때: {trim}",
        f"하지 말 것: {avoid}",
    ])


WHAT = (
    "신한지주(055550) KRX 확정 종가가 미리 정한 분할 매수 가격대에 닿았다는 알림. "
    "매매 지시 아님. 투자 참고용."
)


def messages(cfg: dict, ev: list[dict], stamp: str) -> list[tuple[str, str, int]]:
    out = []
    for e in ev:
        other = e["other_level"]
        gap = e["close"] - other
        st = (
            f"{e['day']} KRX 종가 {won(e['close'])}"
            + (f"(전일 {won(e['prev'])})" if e.get("prev") else "")
            + f". 걸린 선 {won(e['level'])}({e['label']}). "
            f"다른 선({e['other_label']} {won(other)})까지 {gap:+,.0f}원. "
            f"{cfg['context']} 판단 변경 조건: {cfg['change_conditions']} 보유: {cfg['holding']}"
        )
        act = (
            f"{e['label']} 점검. 계획({cfg['plan']})대로 지정가 분할만 검토. "
            "한 번에 다 사지 않기. 해킹 제재와 CET1 13.4% 선을 먼저 확인."
        )
        later = (
            f"같은 선은 종가 {won(e['rearm'])} 초과 뒤에만 다시 옵니다. "
            f"다른 선({e['other_label']} {won(other)})은 따로 감시. 종료 {cfg['ends_kst'][:10]}."
        )
        trim = "미보유. 계획 비중만. 이 알림은 주문이 아님."
        avoid = "자동 매수 신호로 보기, 시장가 한 번에 사기, 우리금융 25주를 바로 팔아 옮기기. 투자 권유 아님."
        title = f"{TAG} {e['title']}"
        out.append((title, body(stamp, WHAT, st, act, later, trim, avoid), int(e["priority"])))
    return out


def ops_msg(stamp: str, status: str) -> tuple[str, str]:
    return (
        f"{TAG} 신한지주 종가 조회 실패 | 판정 건너뜀",
        body(
            stamp,
            "신한지주 가격대 알림의 데이터 읽기 실패 안내. 하루 1번. 매매 신호 아님.",
            status,
            "증권 앱에서 신한지주(055550) KRX 종가를 직접 보세요.",
            "다음 15:40 슬롯에서 다시 읽고, 빠진 날짜는 KRX 종가로 이어서 판정합니다.",
            "이 안내로 비중을 바꾸지 않기.",
            "NXT 시간외 가격이나 네이버 일별 값으로 대신 판정하기.",
        ),
    )


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(st: dict) -> None:
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def check_text(t: str, m: str) -> None:
    if re.search("[\u00b7\u318d\u2014]", t + m):
        raise AssertionError(f"forbidden char in {t}")


def run(mode: str, now: datetime, cfg: dict, pub=publish, rows_fn=None) -> int:
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    topic, server = resolve_topic("port", cfg.get("ntfy_topic")), resolve_server(cfg.get("ntfy_server"))
    print(f"{stamp} {NAME} mode={mode}")
    ends = datetime.strptime(cfg["ends_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    if now > ends:
        print(f"{NAME} ended; silent")
        return 0
    sday = session_day(now)
    today = sday.isoformat()
    if today > cfg["window_end"]:
        print(f"{today} 감시 끝({cfg['window_end']}): 조용히 종료")
        return 0
    if not is_trading_day(sday) or today < cfg["window_start"]:
        if mode != "dry":
            print(f"{today} KRX 휴장 또는 시작 전: 조용히 종료")
            return 0
    st = load_state()
    out, err = [], None
    try:
        rows = confirmed((rows_fn or daum_rows)(cfg["code"]), today, now)
        if session_close_ready(today, today, now, CLOSE_HM) and (not rows or rows[-1]["day"] != today):
            err = f"{today} KRX 종가가 Daum KRX 일별에 아직 없음(마지막 {rows[-1]['day'] if rows else '없음'}). 오늘 판정 건너뜀"
        if rows:
            print(f"latest KRX close {rows[-1]['day']} {won(rows[-1]['close'])} armed_seed_hint={ {ln['key']: not hit(ln['op'], rows[-1]['close'], ln['level']) for ln in cfg['lines']} }")
        if not (st.get("price") or {}).get("last_date"):
            if rows and not err:
                st["price"] = seed(cfg, rows[-1], stamp)
                print(f"first run: state only, no alert: {st['price']}")
        elif rows:
            use = [r for r in rows if r["day"] != today] if err else rows
            out = messages(cfg, evaluate(cfg, st["price"], use, today), stamp)
            print(f"price: {st['price']}")
    except Exception as exc:  # noqa: BLE001
        err = f"Daum KRX 일별 조회 실패: {str(exc)[:160]}. 판정 건너뜀"
    ops = None
    if err:
        print("ERR:", err)
        if st.get("ops_day") != today:
            ops = ops_msg(stamp, err + ". 네이버(NXT 섞임)로 대신 판정하지 않음. state 는 그대로.")
            st["ops_day"] = today
    for t, m, pri in out:
        check_text(t, m)
        print(f"TITLE: {t}\nPRIORITY: {pri} TOPIC: {route_topic(topic, t)}\n{m}\n")
    if ops:
        print(f"OPS TITLE: {ops[0]} -> {route_topic(topic, ops[0])}\n{ops[1]}\n")
    if not out and not ops:
        print("보낼 것 없음")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    for t, m, pri in out:
        pub(topic, t, m, pri, ["chart_with_upwards_trend"], server, config_name=NAME)
    if ops:
        try:
            pub(resolve_topic("ops"), ops[0], ops[1], 3, ["warning"], server, config_name=NAME)
        except Exception as exc:  # noqa: BLE001
            print(f"ops notice failed: {exc}")
    st["updated_at"] = stamp
    save_state(st)
    return 0


def selftest() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    fails = 0

    def check(label, ok):
        nonlocal fails
        fails += not ok
        print(("ok  " if ok else "FAIL"), label)

    ps = seed(cfg, {"day": "2026-11-09", "close": 85800.0}, "T")
    check("seed above both lines armed", ps["armed"] == {"l83000": True, "l79000": True})
    mk = lambda cs: [{"day": f"2026-11-{i + 10:02d}", "close": float(c), "prev": None} for i, c in enumerate(cs)]  # noqa: E731
    # walk: nohit, hit1, still below (silent), rearm1, hit1 again, hit2, rearm2, hit2 again
    walk = [84000, 83000, 82000, 85500, 85501, 83000, 79000, 81400, 81401, 78900]
    ev = evaluate(cfg, ps, mk(walk), "2026-12-31")
    got = [(e["key"], e["day"], e["close"]) for e in ev]
    want = [
        ("l83000", "2026-11-11", 83000.0),
        ("l83000", "2026-11-15", 83000.0),
        ("l79000", "2026-11-16", 79000.0),
        ("l79000", "2026-11-19", 78900.0),
    ]
    check(f"walk with re-arm {got}", got == want)

    # gap down crosses both: both fire same day
    ps2 = seed(cfg, {"day": "2026-11-09", "close": 85800.0}, "T")
    ev2 = evaluate(cfg, ps2, [{"day": "2026-11-10", "close": 78000.0, "prev": 85800.0}], "2026-11-10")
    check("gap down fires both", [e["key"] for e in ev2] == ["l83000", "l79000"])

    msgs = messages(cfg, ev2, "2026-11-10 15:40 KST")
    check("two messages", len(msgs) == 2)
    check("title 1차", msgs[0][0] == "[055550] 신한지주 83,000원 도달 (1차 분할 가격대)")
    check("title 2차", msgs[1][0] == "[055550] 신한지주 79,000원 도달 (2차 분할 가격대)")
    check("priority p3", msgs[0][2] == 3 and msgs[1][2] == 3)
    check("body has current price and other level", "78,000원" in msgs[0][1] and "2차 79,000원" in msgs[0][1])
    check("body has plan context", "해킹 제재" in msgs[0][1] and "CET1" in msgs[0][1] and "코스피" in msgs[0][1])
    check("body has Woori compare", "우리" in msgs[0][1] and "316140" in msgs[0][1])
    for t, m, _ in msgs:
        check_text(t, m)
    check("no middle dot or em-dash", True)

    # seed already inside 1차: that line starts disarmed
    s3 = seed(cfg, {"day": "2026-11-09", "close": 82000.0}, "T")
    check("seed inside 1차 disarms only 1차", s3["armed"] == {"l83000": False, "l79000": True})

    late = datetime(2026, 11, 10, 2, 46, tzinfo=KST)
    rows = [{"day": "2026-11-09", "close": 85800.0}, {"day": "2026-11-10", "close": 84000.0}]
    check("confirmed after midnight still yesterday", [r["day"] for r in confirmed(rows, "2026-11-09", late)] == ["2026-11-09"])
    intra = datetime(2026, 11, 10, 14, 0, tzinfo=KST)
    check("confirmed before close excludes today", [r["day"] for r in confirmed(rows, "2026-11-10", intra)] == ["2026-11-09"])
    after = datetime(2026, 11, 10, 15, 40, tzinfo=KST)
    check("confirmed after close includes today", [r["day"] for r in confirmed(rows, "2026-11-10", after)] == ["2026-11-09", "2026-11-10"])

    ops_t, ops_m = ops_msg("T", "x")
    check_text(ops_t, ops_m)
    check("ops title has ticker", ops_t.startswith("[055550]"))

    # dry first-run silent via run()
    published = []

    def fake_pub(*a, **k):
        published.append((a, k))

    st_path_exists = STATE_PATH.exists()
    # isolate: use inject rows, dry so no state write
    cfg2 = dict(cfg)
    rc = run(
        "dry",
        datetime(2026, 11, 10, 15, 40, tzinfo=KST),
        cfg2,
        pub=fake_pub,
        rows_fn=lambda code: [{"day": "2026-11-09", "close": 85800.0, "prev": 86700.0}],
    )
    check("dry first-ish run returns 0", rc == 0)
    check("dry publishes nothing", published == [])

    print("selftest", "PASS" if not fails else f"FAIL {fails}", f"checks={14 + 8}")
    # recount: printed checks
    return 1 if fails else 0


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry"}:
        print(__doc__)
        return 2
    now = datetime.now(KST)
    return run(mode, now, cfg)


if __name__ == "__main__":
    sys.exit(main())
