#!/usr/bin/env python3
"""[068270] 셀트리온 KRX 확정 종가 205,000원 이하 1회 알림 (2026-11-25 사용자 요청).

KRX 거래일 15:40 KST (0090b0 슬롯에 부착, [055550] 바로 뒤). 새 cron 없음.
선 (configs/k068270.json lines):
  l205000 : 종가 <= 205,000 -> p3 1회 (정상화 PER 30배 가격, 2차 분할 검토). 재무장 없음.
판정은 Daum KRX 확정 종가만(NXT 미포함, 네이버 일봉 안 씀). 당일 행은 15:40 이후만.
첫 실행은 config baseline(2026-11-25 종가)으로 상태를 만들고, 그 뒤 종가부터 판정한다.
ends_kst(2027-03-10 23:59) 뒤에는 조용히 종료.
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

TAG = "[068270]"
NAME = "k068270"
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


def seed(cfg: dict, stamp: str) -> dict:
    b = cfg["baseline"]
    return {
        "last_date": b["day"],
        "last_close": float(b["close"]),
        "armed": {ln["key"]: not hit(ln["op"], float(b["close"]), ln["level"]) for ln in cfg["lines"]},
        "fired": {},
        "seed_note": f"첫 실행 {stamp}: config baseline {b['day']} {won(b['close'])} 으로 시작, 그 뒤 종가부터 판정",
    }


def evaluate(cfg: dict, ps: dict, rows: list[dict], today: str) -> list[dict]:
    out = []
    ps.setdefault("fired", {})
    for r in rows:
        d, c = r["day"], r["close"]
        if d <= ps["last_date"] or d < cfg["window_start"] or d > today:
            continue
        for ln in cfg["lines"]:
            k = ln["key"]
            if ln.get("once") and ps["fired"].get(k):
                continue
            if hit(ln["op"], c, ln["level"]) and ps["armed"].get(k, True):
                ps["armed"][k] = False
                if ln.get("once"):
                    ps["fired"][k] = d
                out.append({**ln, "day": d, "close": c, "prev": r.get("prev")})
            elif not ln.get("once") and ln.get("rearm_op") and hit(ln["rearm_op"], c, ln["rearm"]) and not ps["armed"].get(k, True):
                ps["armed"][k] = True
            elif ln.get("once") and not ps["armed"].get(k, True) and not hit(ln["op"], c, ln["level"]):
                # seeded below the line (should not happen with an above-line baseline): arm on first close above
                ps["armed"][k] = True
        ps["last_date"], ps["last_close"] = d, c
    return out


def per(px: float, eps: float) -> str:
    return f"{px / eps:.1f}배"


def body(stamp: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([
        f"{stamp} | 068270 | 셀트리온",
        f"이게 뭐 알림: {what}",
        f"지금 결과/상태: {status}",
        f"지금 할 일: {action}",
        f"나중에: {later}",
        f"익절/줄일 때: {trim}",
        f"하지 말 것: {avoid}",
    ])


WHAT = (
    "셀트리온(068270) KRX 확정 종가가 정상화 PER 30배 가격(205,000원) 이하로 내려왔다는 1회 알림. "
    "11/16 [CELL3QR] 2차 보류의 가격 사유가 풀렸는지 보는 용도. 매매 지시 아님. 투자 참고용."
)


def messages(cfg: dict, ev: list[dict], stamp: str) -> list[tuple[str, str, int]]:
    out = []
    e3, ey, er = (cfg["eps"][k]["eps"] for k in ("q3a", "ytda", "reported"))
    for e in ev:
        c = e["close"]
        st = (
            f"{e['day']} KRX 종가 {won(c)}"
            + (f"(전일 {won(e['prev'])})" if e.get("prev") else "")
            + f". 걸린 선 {won(e['level'])}({e['label']}). "
            f"2026E PER: 정상화 3Q A 기준 EPS {e3:,}원 {per(c, e3)}, "
            f"정상화 9개월 누적 A 기준 EPS {ey:,}원 {per(c, ey)}, 보고 기준 EPS {er:,}원 {per(c, er)}. "
            f"9개월 누적 A 기준 30배 가격은 {won(ey * 30)}, 28배는 {won(ey * 28)}. "
            f"{cfg['context']} 보유: {cfg['holding']}"
        )
        act = (
            "2차 분할 1회분만 지정가로 검토. 한 번에 다 사지 않기. "
            f"9개월 누적 A 기준 PER이 30배를 넘으면({won(ey * 30)} 초과) 보수 기준으로는 아직 비쌈. "
            "4Q 잠정(1~2월) 전에는 목표 비중을 다 채우지 않기."
        )
        later = (
            "이 알림은 1회뿐이라 다시 오지 않음. "
            f"판단 변경 조건: {cfg['change_conditions']} 종료 {cfg['ends_kst'][:10]}."
        )
        trim = "이 알림은 매도 신호가 아님. 보유분 비중은 4Q 잠정 확인 뒤 판단."
        avoid = "자동 매수 신호로 보기, 시장가 한 번에 사기, NXT 시간외 가격으로 다시 판정하기. 투자 권유 아님."
        out.append((f"{TAG} {e['title']}", body(stamp, WHAT, st, act, later, trim, avoid), int(e["priority"])))
    return out


def ops_msg(stamp: str, status: str) -> tuple[str, str]:
    return (
        f"{TAG} 셀트리온 종가 조회 실패 | 판정 건너뜀",
        body(
            stamp,
            "셀트리온 205,000원 종가 알림의 데이터 읽기 실패 안내. 하루 1번. 매매 신호 아님.",
            status,
            "증권 앱에서 셀트리온(068270) KRX 종가를 직접 보세요.",
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


def run(mode: str, now: datetime, cfg: dict, pub=publish, rows_fn=None, state=None) -> int:
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
    st = load_state() if state is None else state
    if all((st.get("price") or {}).get("fired", {}).get(ln["key"]) for ln in cfg["lines"] if ln.get("once")) and (st.get("price") or {}).get("fired"):
        print(f"already fired {st['price']['fired']}; silent")
        return 0
    out, err = [], None
    try:
        rows = confirmed((rows_fn or daum_rows)(cfg["code"]), today, now)
        if session_close_ready(today, today, now, CLOSE_HM) and (not rows or rows[-1]["day"] != today):
            err = f"{today} KRX 종가가 Daum KRX 일별에 아직 없음(마지막 {rows[-1]['day'] if rows else '없음'}). 오늘 판정 건너뜀"
        if rows:
            print(f"latest KRX close {rows[-1]['day']} {won(rows[-1]['close'])}")
        if not (st.get("price") or {}).get("last_date"):
            st["price"] = seed(cfg, stamp)
            print(f"first run: seeded from baseline: {st['price']}")
        if rows:
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
        pub(topic, t, m, pri, ["chart_with_downwards_trend"], server, config_name=NAME)
    if ops:
        try:
            pub(resolve_topic("ops"), ops[0], ops[1], 3, ["warning"], server, config_name=NAME)
        except Exception as exc:  # noqa: BLE001
            print(f"ops notice failed: {exc}")
    st["updated_at"] = stamp
    if state is None:
        save_state(st)
    return 0


def selftest() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    fails = 0

    def check(label, ok):
        nonlocal fails
        fails += not ok
        print(("ok  " if ok else "FAIL"), label)

    ps = seed(cfg, "T")
    check("seed from baseline 11-25 217,000 armed", ps["last_date"] == "2026-11-25" and ps["armed"] == {"l205000": True})
    mk = lambda cs: [{"day": f"2026-12-{i + 1:02d}", "close": float(c), "prev": None} for i, c in enumerate(cs)]  # noqa: E731
    # walk: above, touch 205,000 (fire), below silent, back above, below again silent (once)
    ev = evaluate(cfg, ps, mk([210000, 205000, 200000, 215000, 199000]), "2026-12-31")
    check(f"fires once at 205,000 {[(e['day'], e['close']) for e in ev]}", [(e["day"], e["close"]) for e in ev] == [("2026-12-02", 205000.0)])
    check("fired recorded", ps["fired"] == {"l205000": "2026-12-02"})

    ps2 = seed(cfg, "T")
    old = [{"day": "2026-11-23", "close": 202000.0, "prev": None}, {"day": "2026-11-25", "close": 217000.0, "prev": None}]
    check("pre-baseline rows ignored (11/23 202,000)", evaluate(cfg, ps2, old, "2026-11-25") == [])

    ps3 = seed(cfg, "T")
    check("205,001 does not fire", evaluate(cfg, ps3, [{"day": "2026-11-26", "close": 205001.0, "prev": 217000.0}], "2026-11-26") == [])
    ev3 = evaluate(cfg, ps3, [{"day": "2026-11-27", "close": 198000.0, "prev": 205001.0}], "2026-11-27")
    msgs = messages(cfg, ev3, "2026-11-27 15:40 KST")
    check("one message", len(msgs) == 1)
    check("title", msgs[0][0] == "[068270] 셀트리온 종가 205,000원 이하 (정상화 PER 30배, 2차 분할 검토 가격)")
    check("priority p3", msgs[0][2] == 3)
    m = msgs[0][1]
    check("body has both normalized PERs", "6,834원 29.0배" in m and "6,327원 31.3배" in m and "5,496원 36.0배" in m)
    check("body has ytd 30x and 28x prices", "189,810원" in m and "177,156원" in m)
    check("body has context", "ctq4" in m and "짐펜트라" in m and "2차 보류" in m)
    for t, mm, _ in msgs:
        check_text(t, mm)
    check("no middle dot or em-dash", True)

    late = datetime(2026, 11, 27, 2, 46, tzinfo=KST)
    rows = [{"day": "2026-11-26", "close": 210000.0}, {"day": "2026-11-27", "close": 200000.0}]
    check("confirmed after midnight still yesterday", [r["day"] for r in confirmed(rows, "2026-11-26", late)] == ["2026-11-26"])
    intra = datetime(2026, 11, 27, 14, 0, tzinfo=KST)
    check("confirmed before close excludes today", [r["day"] for r in confirmed(rows, "2026-11-27", intra)] == ["2026-11-26"])

    ops_t, ops_m = ops_msg("T", "x")
    check_text(ops_t, ops_m)
    check("ops title has ticker", ops_t.startswith("[068270]"))

    published = []

    def fake_pub(*a, **k):
        published.append((a, k))

    st = {}
    rc = run("auto", datetime(2026, 11, 27, 15, 45, tzinfo=KST), cfg, pub=fake_pub,
             rows_fn=lambda code: [{"day": "2026-11-26", "close": 210000.0, "prev": 217000.0},
                                   {"day": "2026-11-27", "close": 204000.0, "prev": 210000.0}], state=st)
    check("auto run sends once", rc == 0 and len(published) == 1)
    rc2 = run("auto", datetime(2026, 11, 30, 15, 45, tzinfo=KST), cfg, pub=fake_pub,
              rows_fn=lambda code: [{"day": "2026-11-30", "close": 190000.0, "prev": 204000.0}], state=st)
    check("second run after fire is silent", rc2 == 0 and len(published) == 1)

    published.clear()
    rc3 = run("dry", datetime(2026, 11, 27, 15, 45, tzinfo=KST), cfg, pub=fake_pub,
              rows_fn=lambda code: [{"day": "2026-11-27", "close": 204000.0, "prev": 210000.0}], state={})
    check("dry publishes nothing", rc3 == 0 and published == [])

    print("selftest", "PASS" if not fails else f"FAIL {fails}")
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
