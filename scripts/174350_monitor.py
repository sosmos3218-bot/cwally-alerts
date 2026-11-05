#!/usr/bin/env python3
"""[174350] TIGER 로우볼 split-buy bands + KOSPI trim signals (approved in chat 2026-11-05).

Rides the existing k203780 19:00 KST slot (no new cron). Closes: Yahoo 174350.KS and ^KS11 daily bars (KRX regular
session; today's bar excluded before 15:30 KST; never Naver NXT). Every new session since state last_day is judged in
order, needing both closes that day (else hold + cwally-ops once a day).
  buy1 <= 20,650 (re-arm >= 21,050), buy2 <= 19,500 (>= 19,900), buy3 <= 18,800 (>= 19,200), stop < 17,840 (>= 18,200)
  trim1: KOSPI >= 6,191 and ratio (174350 / KOSPI) < its 20-session average (incl. that day); re-arm KOSPI < 6,000
  trim2: KOSPI >= 6,509 for 5 consecutive sessions; re-arm KOSPI < 6,300. Shows KOSPI 200-day MA (reference only).
Lines are fixed numbers set 2026-11-05. Modes: auto | dry | selftest.
"""
from __future__ import annotations

import copy
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_calendar import KST  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

NAME = "174350"
CFG_PATH = ROOT / "configs" / "174350.json"
STATE_PATH = ROOT / "state" / "174350.json"


def fmt(v: float) -> str:
    return f"{v:,.0f}원"


def bars(cfg: dict, sym: str) -> dict:
    url = cfg["sources"]["yahoo"].format(sym=urllib.parse.quote(sym))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 cwally-alerts 174350"})
    with urllib.request.urlopen(req, timeout=25) as r:
        d = json.loads(r.read().decode())["chart"]["result"][0]
    out = {}
    for t, c in zip(d["timestamp"], d["indicators"]["quote"][0]["close"]):
        if c:
            out[datetime.fromtimestamp(t, KST).date().isoformat()] = float(c)
    return out


def confirmed(series: dict, now: datetime) -> dict:
    today = now.date().isoformat()
    return {k: v for k, v in series.items() if k < today or (k == today and (now.hour, now.minute) >= (15, 30))}


def hit_buy(ln: dict, close: float) -> bool:
    return close < ln["level"] if ln.get("op") == "lt" else close <= ln["level"]


def default_state(cfg: dict) -> dict:
    st = {"last_day": None, "lines": {ln["key"]: {"armed": True, "fired_day": None} for ln in cfg["buy_lines"]}}
    st["lines"]["trim1"] = {"armed": True, "fired_day": None, "pending_since": None}
    st["lines"]["trim2"] = {"armed": True, "fired_day": None}
    return st


def evaluate(cfg: dict, st: dict, etf: dict, kospi: dict) -> tuple[list[dict], bool]:
    """Judge each new session in order. Returns (events, held). held=True when a session lacked one close."""
    days = sorted(etf)
    kdays = sorted(kospi)
    lines = st["lines"]
    events, held = [], False
    t1, t2 = cfg["trim1"], cfg["trim2"]
    for d in days:
        if d <= (st.get("last_day") or ""):
            continue
        if d not in kospi:
            held = True
            break
        c, k = etf[d], kospi[d]
        both = [x for x in days if x <= d and x in kospi]
        ratios = [etf[x] / kospi[x] for x in both[-int(t1["ratio_days"]):]]
        ratio = c / k
        ravg = sum(ratios) / len(ratios)
        kd = [x for x in kdays if x <= d]
        streak = 0
        for x in reversed(kd):
            if kospi[x] >= t2["kospi_level"]:
                streak += 1
            else:
                break
        ma_n = int(t2["ma_days"])
        ma = sum(kospi[x] for x in kd[-ma_n:]) / ma_n if len(kd) >= ma_n else None
        ctx = {"day": d, "close": c, "kospi": k, "ratio": ratio, "ravg": ravg, "streak": streak, "ma200": ma}
        for ln in cfg["buy_lines"]:
            s = lines.setdefault(ln["key"], {"armed": True, "fired_day": None})
            if s.get("armed", True) and hit_buy(ln, c):
                s.update(armed=False, fired_day=d)
                events.append({"kind": "buy", "line": ln, **ctx})
            elif not s.get("armed", True) and c >= ln["rearm"]:
                s["armed"] = True
                print(f"{NAME} {ln['key']} re-armed on {d} close {c:.0f}")
        s = lines.setdefault("trim1", {"armed": True, "fired_day": None, "pending_since": None})
        if s.get("armed", True) and k >= t1["kospi_level"]:
            if ratio < ravg:
                s.update(armed=False, fired_day=d)
                events.append({"kind": "trim1", "line": t1, "pending_since": s.get("pending_since"), **ctx})
                s["pending_since"] = None
            elif not s.get("pending_since"):
                s["pending_since"] = d
        elif not s.get("armed", True) and k < t1["rearm_below"]:
            s.update(armed=True, pending_since=None)
            print(f"{NAME} trim1 re-armed on {d} KOSPI {k:.2f}")
        elif s.get("armed", True) and k < t1["kospi_level"]:
            s["pending_since"] = None
        s = lines.setdefault("trim2", {"armed": True, "fired_day": None})
        if s.get("armed", True) and streak >= int(t2["days"]):
            s.update(armed=False, fired_day=d)
            events.append({"kind": "trim2", "line": t2, **ctx})
        elif not s.get("armed", True) and k < t2["rearm_below"]:
            s["armed"] = True
            print(f"{NAME} trim2 re-armed on {d} KOSPI {k:.2f}")
        st.update(last_day=d, last_close=c, last_kospi=round(k, 2), last_ratio=round(ratio, 6), last_ratio_avg=round(ravg, 6),
                  last_streak=streak, last_ma200=round(ma, 2) if ma else None)
    return events, held


def message(cfg: dict, e: dict, stamp: str) -> tuple[str, str, int]:
    ln = e["line"]
    yld = cfg["annual_dividend"] / e["close"] * 100
    rel = "아래" if e["ratio"] < e["ravg"] else "위"
    ma = f"{e['ma200']:,.2f}" if e.get("ma200") else "계산 불가"
    common = (f"{e['day']} 174350 종가 {fmt(e['close'])}, KOSPI {e['kospi']:,.2f}. 배당수익률 {yld:.2f}%(연 {cfg['annual_dividend']}원 기준). "
              f"174350/KOSPI 비율은 20일 평균 {rel}({(e['ratio'] / e['ravg'] - 1) * 100:+.2f}%). KOSPI 200일선 {ma}(참고).")
    if e["kind"] == "buy":
        title = f"[174350] TIGER 로우볼 {ln['label']} {fmt(e['close'])} (174350)"
        what = f"TIGER 로우볼(174350) KRX 종가가 {ln['label']}({ln['band']}, {ln['weight']})에 새로 들어올 때 1번 오는 알림입니다. 매수 지시가 아닙니다."
        status = f"{ln['label']}: {common}"
        later = f"종가가 {fmt(ln['rearm'])} 이상으로 돌아와야 이 선이 다시 울립니다. 다음 선은 각각 따로 돕니다."
        trim = "줄이는 기준은 KOSPI 6,191 이상 축소1과 6,509 이상 5일 연속 축소2 알림입니다."
        dont = "한 번에 배정 전액 매수, 신용 및 미수, NXT 가격으로 판단."
    elif e["kind"] == "trim1":
        title = f"[174350] TIGER 로우볼 축소1, KOSPI {e['kospi']:,.0f} (174350/KS11)"
        what = "KOSPI 종가가 6,191 이상이고 174350/KOSPI 비율이 20일 평균 아래로 내려온 첫날 1번 오는 축소 신호입니다. 매도 지시가 아닙니다."
        pend = f" KOSPI는 {e['pending_since']}부터 6,191 이상이었지만 비율이 평균 아래로 내려온 오늘 조건이 맞았습니다." if e.get("pending_since") else ""
        status = f"{ln['label']}: {common}{pend}"
        later = "KOSPI 종가가 6,000 미만으로 내려와야 축소1이 다시 울립니다. 6,509 이상 5거래일 연속이면 축소2가 따로 옵니다."
        trim = "이번 축소는 보유분 25%까지만. 나머지는 축소2 조건에서 다시 봅니다."
        dont = "한 번에 전량 매도, 축소 자금을 단기 추격에 쓰기, 신용 및 미수."
    else:
        title = f"[174350] TIGER 로우볼 축소2, KOSPI 6,509 이상 {e['streak']}일 (174350/KS11)"
        what = "KOSPI 종가가 6,509 이상으로 5거래일 연속 머문 첫날 1번 오는 추가 축소 신호입니다. 매도 지시가 아닙니다."
        status = f"{ln['label']}: KOSPI 6,509 이상 {e['streak']}거래일 연속. {common}"
        later = "KOSPI 종가가 6,300 미만으로 내려와야 축소2가 다시 울립니다."
        trim = "추가 25% 축소 뒤 포트 비중은 10% 하단이 목표입니다."
        dont = "한 번에 전량 매도, 신용 및 미수."
    msg = "\n".join([
        f"{stamp} | 174350 | TIGER 로우볼",
        "이게 뭐 알림: " + what,
        "지금 결과/상태: " + status,
        "지금 할 일: " + ln["action"],
        "나중에: " + later + " " + cfg["lines_fixed_note"],
        "익절/줄일 때: " + trim,
        "하지 말 것: " + dont,
    ])
    return title, msg, int(ln.get("priority", 4))


def ops_message(why: str, stamp: str) -> tuple[str, str]:
    return ("[174350] TIGER 로우볼 조회 실패 | 판정 보류 (174350/KS11)", "\n".join([
        f"{stamp} | 174350 | TIGER 로우볼",
        "이게 뭐 알림: 174350 알림의 데이터 조회 실패 안내입니다. 조건 미충족이 아닙니다.",
        f"지금 결과/상태: {why}",
        "지금 할 일: 증권 앱에서 174350과 KOSPI의 KRX 종가를 직접 확인하세요.",
        "나중에: 다음 KRX 거래일 19:00 KST에 다시 시도하고 밀린 거래일을 순서대로 판정합니다.",
        "익절/줄일 때: 이 안내로 비중을 바꾸지 마세요.",
        "하지 말 것: 조회 실패를 신호로 읽기.",
    ]))


def run(mode: str) -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    dry = mode == "dry"
    now = datetime.now(KST)
    today, stamp = now.date().isoformat(), now.strftime("%Y-%m-%d %H:%M KST")
    st = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else default_state(cfg)
    sends, ops = [], []
    try:
        etf, kospi = confirmed(bars(cfg, cfg["yahoo_symbol"]), now), confirmed(bars(cfg, cfg["index_symbol"]), now)
    except Exception as exc:  # noqa: BLE001
        etf, kospi = {}, {}
        print(f"yahoo error: {type(exc).__name__}")
    held = False
    if etf and kospi:
        evs, held = evaluate(cfg, st, etf, kospi)
        sends = [message(cfg, e, stamp) for e in evs]
    if (not etf or not kospi or held) and st.get("fail_day") != today:
        ops.append(ops_message("Yahoo 174350.KS 또는 ^KS11 정규장 종가를 못 읽었거나 같은 날 둘 중 하나가 비었습니다.", stamp))
        st["fail_day"] = today
    print(f"last {st.get('last_day')} close {st.get('last_close')} KOSPI {st.get('last_kospi')} ratio/avg "
          f"{st.get('last_ratio')}/{st.get('last_ratio_avg')} streak {st.get('last_streak')} MA200 {st.get('last_ma200')}")
    print("lines:", {k: ("armed" if v.get("armed", True) else f"fired {v.get('fired_day')}") for k, v in st["lines"].items()})
    server = resolve_server(cfg.get("ntfy_server"))
    for t, m, p in sends:
        print(t)
        print(m)
        if not dry:
            publish(resolve_topic("port", cfg.get("ntfy_topic")), t, m, p, ["shield"], server, config_name=NAME)
    for t, m in ops:
        print(t)
        if not dry:
            publish(resolve_topic("ops"), t, m, 3, ["warning"], server, config_name=NAME)
    if not sends:
        print("no new condition; silent")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    st["updated_at"] = now.isoformat()
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


def selftest() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    bad = []

    def check(name, got, want):
        print(("ok   " if got == want else "FAIL ") + f"{name}: got={got} want={want}")
        if got != want:
            bad.append(name)

    # history: 30 sessions of flat ratio before the test days
    base_days = [f"2026-09-{i:02d}" for i in range(1, 31)]
    etf = {d: 20600.0 for d in base_days}
    kos = {d: 5400.0 for d in base_days}
    st = default_state(cfg)
    st["lines"]["buy1"].update(armed=False, fired_day="2026-11-05")
    st["last_day"] = base_days[-1]
    seq = iter(f"2026-10-{i:02d}" for i in range(1, 32))

    def feed(c, k=5400.0):
        d = next(seq)
        etf[d], kos[d] = float(c), float(k)
        ev, _ = evaluate(cfg, st, etf, kos)
        return [e["line"]["key"] for e in ev]

    check("seed: 20,610 inside 1차 but already fired -> silent", feed(20610), [])
    check("20,500 still no repeat", feed(20500), [])
    check("19,500 fires 2차", feed(19500), ["buy2"])
    check("18,800 fires 3차", feed(18800), ["buy3"])
    check("17,840 not < 17,840", feed(17840), [])
    check("17,800 fires 중단", feed(17800), ["stop"])
    check("19,850 re-arms 3차 only (no fire)", (feed(19850), st["lines"]["buy3"]["armed"], st["lines"]["buy2"]["armed"]), ([], True, False))
    check("21,050 re-arms 1차/2차/중단", (feed(21050), [st["lines"][k]["armed"] for k in ("buy1", "buy2", "stop")]), ([], [True, True, True]))
    check("20,650 fires 1차 again", feed(20650), ["buy1"])
    # trim1: KOSPI >= 6,191 with ratio above avg -> pending, then ratio below avg -> fire
    check("KOSPI 6,200 ratio above avg -> no trim1", feed(20650 * 6200 / 5400 * 1.05, 6200), [])
    check("trim1 pending recorded", bool(st["lines"]["trim1"]["pending_since"]), True)
    check("KOSPI 6,250 ratio below avg -> trim1", feed(20700, 6250), ["trim1"])
    check("trim1 no repeat", feed(20700, 6300), [])
    check("KOSPI 6,100 no re-arm", (feed(20700, 6100), st["lines"]["trim1"]["armed"]), ([], False))
    check("KOSPI 5,990 re-arms trim1", (feed(20700, 5990), st["lines"]["trim1"]["armed"]), ([], True))
    # trim2: 5 consecutive >= 6,509 (ratio kept above avg so trim1 stays quiet)
    hi = 26000
    out = [feed(hi, 6509 + i) for i in range(4)]
    check("trim2 not before day 5", out, [[], [], [], []])
    check("trim2 fires on day 5", feed(hi, 6520), ["trim2"])
    check("trim2 no repeat day 6", feed(hi, 6530), [])
    check("KOSPI 6,350 no re-arm", (feed(hi, 6350), st["lines"]["trim2"]["armed"]), ([], False))
    check("KOSPI 6,290 re-arms trim2", (feed(hi, 6290), st["lines"]["trim2"]["armed"]), ([], True))
    # missing KOSPI that day holds
    st2 = copy.deepcopy(st)
    etf["2026-11-02"] = 18000.0
    ev, held = evaluate(cfg, st2, etf, kos)
    check("KOSPI missing: hold, no judge", (ev, held, st2["last_day"]), ([], True, st["last_day"]))
    del etf["2026-11-02"]
    # partial bar
    s = {"2026-11-04": 1.0, "2026-11-05": 2.0}
    check("partial bar excluded at 09:00", list(confirmed(s, datetime(2026, 11, 5, 9, 0, tzinfo=KST))), ["2026-11-04"])
    check("bar counted after 15:30", list(confirmed(s, datetime(2026, 11, 5, 19, 0, tzinfo=KST))), ["2026-11-04", "2026-11-05"])
    # messages
    ctx = {"day": "2026-11-06", "close": 19400.0, "kospi": 5300.0, "ratio": 3.66, "ravg": 3.7, "streak": 0, "ma200": 4800.0}
    t, m, p = message(cfg, {"kind": "buy", "line": cfg["buy_lines"][1], **ctx}, "x")
    check("buy2 title", t, "[174350] TIGER 로우볼 2차 매수 구간 19,400원 (174350)")
    keys = ["이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"]
    check("six-item order", [m.index(k + ":") for k in keys] == sorted(m.index(k + ":") for k in keys), True)
    check("buy2 body", all(x in m for x in ("2026-11-06 174350 종가 19,400원", "배당수익률 3.27%", "20일 평균 아래", "배정 30%", "2026-11-05", "200일선 4,800.00")), True)
    t, m, p = message(cfg, {"kind": "trim1", "line": cfg["trim1"], "pending_since": "2026-12-01", **ctx}, "x")
    check("trim1 title + pending note", (t.startswith("[174350] TIGER 로우볼 축소1"), "2026-12-01부터 6,191 이상" in m, "25% 축소" in m), (True, True, True))
    t, m, p = message(cfg, {"kind": "trim2", "line": cfg["trim2"], **{**ctx, "streak": 5}}, "x")
    check("trim2 title", t, "[174350] TIGER 로우볼 축소2, KOSPI 6,509 이상 5일 (174350/KS11)")
    ot, om = ops_message("x", "y")
    check("ops title", "조회 실패" in ot and "174350" in ot, True)
    txt = CFG_PATH.read_text(encoding="utf-8") + Path(__file__).read_text(encoding="utf-8")
    check("no middle dot / em-dash", any(ch in txt for ch in ("\u00b7", "\u2014")), False)
    print("SELFTEST PASS" if not bad else f"SELFTEST FAIL {bad}")
    return 0 if not bad else 1


if __name__ == "__main__":
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    raise SystemExit(selftest() if mode == "selftest" else run(mode))
