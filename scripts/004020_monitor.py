#!/usr/bin/env python3
"""[004020] 현대제철 PBR 0.15x / 0.25x close alert (approved in chat 2026-11-05).

Rides the existing k203780 19:00 KST slot (no new cron). PBR = Yahoo 004020.KS KRX regular-session close / BPS.
BPS: WiseReport c1010001 header (latest quarter, 지배주주) each run, config fallback (bps.value) when unreadable or
implausible (> plausible_pct off config) with a weekly cwally-ops notice. Price lines are recomputed from BPS each run.
Edge-triggered per line: low fires at PBR <= 0.15, re-arms at >= 0.16; high fires at >= 0.25, re-arms at <= 0.24.
Every new completed session since state last_day is judged in order. Modes: auto | dry | selftest.
"""
from __future__ import annotations

import copy
import json
import re
import sys
import urllib.request
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_calendar import KST  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

NAME = "004020"
CFG_PATH = ROOT / "configs" / "004020.json"
STATE_PATH = ROOT / "state" / "004020.json"
UA = "Mozilla/5.0 (cwally-alerts 004020 monitor)"


def get(url: str, referer: str | None = None) -> str:
    h = {"User-Agent": UA}
    if referer:
        h["Referer"] = referer
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=25) as r:
        return r.read().decode("utf-8", errors="ignore")


def fmt(v: float) -> str:
    return f"{v:,.0f}원"


def parse_bps(html: str) -> float | None:
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    m = re.search(r"EPS\s+-?[\d,]+\s+BPS\s+([\d,]+)", text)
    return float(m.group(1).replace(",", "")) if m else None


def bps_used(cfg: dict, raw: float | None) -> tuple[float, str, bool]:
    """(bps, basis text, auto_ok)."""
    b = cfg["bps"]
    base = float(b["value"])
    if raw and abs(raw / base - 1) * 100 <= float(b.get("plausible_pct", 30)):
        if round(raw) == round(base):
            return raw, f"{b['basis']} (WiseReport 최근 분기, 자동 확인)", True
        return raw, "WiseReport 최근 분기(자동, config 기준일 갱신 필요)", True
    return base, f"{b['basis']} (config 값, WiseReport 자동 조회 실패)", False


def yahoo_rows(cfg: dict) -> list[dict]:
    d = json.loads(get(cfg["sources"]["yahoo"].format(sym=cfg["yahoo_symbol"])))["chart"]["result"][0]
    out = {}
    for t, c in zip(d["timestamp"], d["indicators"]["quote"][0]["close"]):
        if c:
            out[datetime.fromtimestamp(t, KST).date().isoformat()] = float(c)
    return [{"day": k, "close": v} for k, v in sorted(out.items())]


def line_price(ln: dict, bps: float) -> float:
    return ln["pbr"] * bps


def hit(ln: dict, pbr: float) -> bool:
    p = round(pbr, 3)  # PBR judged at 3 decimals (22,700 / 151,227 = 0.150 counts as 0.15)
    return p <= ln["pbr"] if ln["op"] == "le" else p >= ln["pbr"]


def rearm(ln: dict, pbr: float) -> bool:
    p = round(pbr, 3)  # PBR judged at 3 decimals (22,700 / 151,227 = 0.150 counts as 0.15)
    return p >= ln["rearm_pbr"] if ln["op"] == "le" else p <= ln["rearm_pbr"]


def evaluate(cfg: dict, st: dict, rows: list[dict], bps: float, today: str, now: datetime) -> list[dict]:
    events = []
    lines = st.setdefault("lines", {})
    prev = st.get("last_pbr")
    for r in rows:
        if r["day"] <= (st.get("last_day") or ""):
            prev = r["close"] / bps
            continue
        if r["day"] > today or (r["day"] == today and (now.hour, now.minute) < (15, 30)):
            continue
        pbr = r["close"] / bps
        for ln in cfg["lines"]:
            s = lines.setdefault(ln["key"], {"armed": True, "fired_day": None})
            if s.get("armed", True) and hit(ln, pbr):
                s.update(armed=False, fired_day=r["day"])
                events.append({"line": ln, "day": r["day"], "close": r["close"], "pbr": pbr, "prev": prev})
            elif not s.get("armed", True) and rearm(ln, pbr):
                s["armed"] = True
                print(f"{NAME} {ln['key']} re-armed on {r['day']} PBR {pbr:.3f}")
        st.update(last_day=r["day"], last_close=r["close"], last_pbr=round(pbr, 4))
        prev = pbr
    return events


def message(cfg: dict, e: dict, bps: float, basis: str, stamp: str) -> tuple[str, str, int]:
    ln = e["line"]
    lp = line_price(ln, bps)
    other = [x for x in cfg["lines"] if x["key"] != ln["key"]][0]
    prev = f"{e['prev']:.2f}배" if e.get("prev") is not None else "없음"
    msg = "\n".join([
        f"{stamp} | 004020 | 현대제철 PBR",
        "이게 뭐 알림: 현대제철(004020) KRX 정규장 종가 기준 PBR이 0.15배 이하나 0.25배 이상으로 새로 들어갈 때만 1번 오는 알림입니다. 매수나 매도 지시가 아닙니다.",
        f"지금 결과/상태: {e['day']} 종가 {fmt(e['close'])}, PBR {e['pbr']:.2f}배(직전 거래일 {prev}). "
        f"BPS {fmt(bps)}, 기준 {basis}. {ln['label']} 기준가 {fmt(lp)}, 종가와 차이 {(e['close'] / lp - 1) * 100:+.1f}%. "
        f"반대쪽 {other['pbr']:.2f}배 기준가 {fmt(line_price(other, bps))}.",
        f"지금 할 일: {ln['context']} 이 알림만으로 사거나 팔지 마세요.",
        f"나중에: {ln['later']} BPS가 바뀌면 기준가도 같이 바뀝니다.",
        "익절/줄일 때: 이 알림은 가격 판단선이 아닙니다. 비중 조정은 [STEEL] 바스켓 기준을 따르세요.",
        "하지 말 것: NXT 가격으로 판단, PBR만 보고 추격 매매나 투매, 신용 및 미수.",
    ])
    return ln["title"], msg, int(ln.get("priority", 4))


def ops_message(why: str, stamp: str) -> tuple[str, str]:
    return ("[004020] 현대제철 조회 실패 | 판정 보류 (004020)", "\n".join([
        f"{stamp} | 004020 | 현대제철 PBR",
        "이게 뭐 알림: 현대제철 PBR 알림의 데이터 조회 실패 안내입니다. 조건 미충족이 아닙니다.",
        f"지금 결과/상태: {why}",
        "지금 할 일: 증권 앱에서 현대제철 KRX 종가와 BPS를 직접 확인하세요.",
        "나중에: 다음 KRX 거래일 19:00 KST에 다시 시도하고 밀린 거래일을 순서대로 판정합니다.",
        "익절/줄일 때: 이 안내로 비중을 바꾸지 마세요.",
        "하지 말 것: 조회 실패를 신호로 읽기.",
    ]))


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def run(mode: str) -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    dry = mode == "dry"
    now = datetime.now(KST)
    today, stamp = now.date().isoformat(), now.strftime("%Y-%m-%d %H:%M KST")
    st = load_state()
    sends, ops = [], []
    raw = None
    if cfg["bps"].get("auto_refresh"):
        try:
            raw = parse_bps(get(cfg["sources"]["wisereport"].format(code=cfg["code"]), "https://navercomp.wisereport.co.kr/"))
        except Exception as exc:  # noqa: BLE001
            print(f"wisereport error: {type(exc).__name__}")
    bps, basis, auto_ok = bps_used(cfg, raw)
    print(f"bps raw={raw} used={bps:.0f} basis={basis}")
    wk = now.strftime("%G-W%V")
    if cfg["bps"].get("auto_refresh") and not auto_ok and st.get("bps_fail_week") != wk:
        ops.append(ops_message(f"WiseReport BPS 자동 조회 실패(읽은 값 {raw}). config BPS {fmt(bps)}로 판정은 계속합니다.", stamp))
        st["bps_fail_week"] = wk
    try:
        rows = yahoo_rows(cfg)
    except Exception as exc:  # noqa: BLE001
        rows = []
        print(f"yahoo error: {type(exc).__name__}")
    if not rows:
        if st.get("fail_day") != today:
            ops.append(ops_message("Yahoo 004020.KS 정규장 종가를 못 읽었습니다.", stamp))
            st["fail_day"] = today
    else:
        if not st.get("last_day"):
            # first run: seed at the latest completed session, no alert for a condition already on
            done = [r for r in rows if r["day"] < today or (now.hour, now.minute) >= (15, 30)]
            seed = done[-1]
            p = seed["close"] / bps
            st.update(last_day=seed["day"], last_close=seed["close"], last_pbr=round(p, 4),
                      lines={ln["key"]: {"armed": not hit(ln, p), "fired_day": None} for ln in cfg["lines"]})
            print(f"seeded {seed['day']} close {seed['close']:.0f} PBR {p:.3f}")
        for e in evaluate(cfg, st, rows, bps, today, now):
            sends.append(message(cfg, e, bps, basis, stamp))
        lp = {ln["key"]: round(line_price(ln, bps)) for ln in cfg["lines"]}
        print(f"last {st.get('last_day')} close {st.get('last_close')} PBR {st.get('last_pbr')} lines {lp} armed "
              f"{ {k: v['armed'] for k, v in st['lines'].items()} }")
    st["bps_used"], st["bps_basis"] = round(bps), basis
    server = resolve_server(cfg.get("ntfy_server"))
    for t, m, p in sends:
        print(t)
        print(m)
        if not dry:
            publish(resolve_topic("port", cfg.get("ntfy_topic")), t, m, p, ["office"], server, config_name=NAME)
    for t, m in ops:
        print(t)
        print(m)
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
        ok = got == want
        print(("ok   " if ok else "FAIL ") + f"{name}: got={got} want={want}")
        if not ok:
            bad.append(name)

    bps = 151227.0
    now = datetime(2026, 12, 31, 19, 0, tzinfo=KST)
    st = {"last_day": "2026-11-04", "last_close": 29000.0, "last_pbr": 0.1918,
          "lines": {"low": {"armed": True, "fired_day": None}, "high": {"armed": True, "fired_day": None}}}
    n = iter(range(5, 31))

    def feed(close, b=bps):
        d = f"2026-11-{next(n):02d}"
        return [e["line"]["key"] for e in evaluate(cfg, st, [{"day": d, "close": close}], b, "2026-12-31", now)]

    check("29,000 (0.192) silent", feed(29000), [])
    check("22,700 (0.150) fires low", feed(22700), ["low"])
    check("22,000 repeat below silent", feed(22000), [])
    check("23,900 (0.158) not re-armed", (feed(23900), st["lines"]["low"]["armed"]), ([], False))
    check("24,300 (0.161) re-armed", (feed(24300), st["lines"]["low"]["armed"]), ([], True))
    check("37,900 (0.251) fires high", feed(37900), ["high"])
    check("37,000 (0.245) not re-armed", (feed(37000), st["lines"]["high"]["armed"]), ([], False))
    check("36,200 (0.239) re-armed", (feed(36200), st["lines"]["high"]["armed"]), ([], True))
    check("lines from BPS 151,227", [round(line_price(ln, bps)) for ln in cfg["lines"]], [22684, 37807])
    check("BPS 160,000 moves lines", [round(line_price(ln, 160000)) for ln in cfg["lines"]], [24000, 40000])
    check("BPS change: 23,900 fires low at BPS 160,000 (0.149)", feed(23900, 160000), ["low"])
    check("already-done day skipped", evaluate(cfg, st, [{"day": "2026-11-01", "close": 1000}], bps, "2026-12-31", now), [])
    check("intraday not judged", evaluate(cfg, st, [{"day": "2026-12-31", "close": 1000}], bps, "2026-12-31",
                                          datetime(2026, 12, 31, 14, 0, tzinfo=KST)), [])
    # BPS parsing / fallback
    html = "<td>EPS</td><td>-136</td> <td>BPS</td><td>151,227</td> PER N/A"
    check("parse BPS", parse_bps(html), 151227.0)
    check("auto BPS same", bps_used(cfg, 151227.0)[2], True)
    check("implausible BPS falls back", bps_used(cfg, 15122.0)[:1] + (bps_used(cfg, 15122.0)[2],), (151227.0, False))
    check("missing BPS falls back", bps_used(cfg, None)[0], 151227.0)
    # messages
    st2 = copy.deepcopy({"last_day": "2026-11-04", "last_pbr": 0.1918, "lines": {}})
    ev = evaluate(cfg, st2, [{"day": "2026-11-05", "close": 22600.0}], bps, "2026-12-31", now)
    t, m, p = message(cfg, ev[0], bps, "2026.06", "2026-11-05 19:00 KST")
    check("low title", t, "[004020] 현대제철 PBR 0.15배 도달 (004020)")
    keys = ["이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"]
    pos = [m.index(k + ":") for k in keys]
    check("six-item order", pos == sorted(pos), True)
    check("low body", all(k in m for k in ("종가 22,600원", "PBR 0.15배", "직전 거래일 0.19배", "BPS 151,227원", "기준가 22,684원", "분할 관망")), True)
    ev = evaluate(cfg, st2, [{"day": "2026-11-06", "close": 38000.0}], bps, "2026-12-31", now)
    t, m, p = message(cfg, ev[0], bps, "2026.06", "x")
    check("high title + context", (t, "PER 약 39배" in m, "기준가 37,807원" in m, p), ("[004020] 현대제철 PBR 0.25배 도달 (004020)", True, True, 4))
    ot, om = ops_message("x", "y")
    check("ops title routes to ops", "조회 실패" in ot and "004020" in ot, True)
    check("no middle dot / em-dash", any(c in json.dumps(cfg, ensure_ascii=False) + m + om for c in "\u00b7\u2014"), False)
    print("SELFTEST PASS" if not bad else f"SELFTEST FAIL {bad}")
    return 0 if not bad else 1


if __name__ == "__main__":
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    raise SystemExit(selftest() if mode == "selftest" else run(mode))
