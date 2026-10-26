#!/usr/bin/env python3
"""[PCOR-PX] Procore close-based plan lines (approved in chat 2026-10-26).

  - close <= 48.00 -> '3차 분할 구간 진입' (priority 3)
  - close <  46.00 -> '46달러 지지선 이탈' (priority 4)

Each line fires once per crossing. Re-arm needs a 1% buffer (ratesbell ten_break_buffer, 8593079):
a triggered line goes back to armed only when a completed close is above price x 1.01 (48.48 / 46.46);
between the line and the buffer the previous state is kept. The first run (no state) only records
the current close, it never sends. Dedupe by the bar's own US/Eastern date (state last_date).

Runs on the existing morning 09:00 KST slot (after the US close). No new cron.
Modes: auto | force (ignore dedupe, still respects state arm) | dry (print only, no ntfy, no state) | selftest.
Not investment advice.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
NY = ZoneInfo("America/New_York")
CFG = ROOT / "configs" / "pcorpx.json"
STATE = ROOT / "state" / "pcorpx.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


def load_cfg() -> dict:
    return json.loads(CFG.read_text(encoding="utf-8"))


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(st: dict, dry: bool) -> None:
    if dry:
        return
    st["updated_at"] = datetime.now(KST).isoformat(timespec="seconds")
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch_bars(cfg: dict, now_ny: datetime) -> list[tuple[date, float]]:
    """Completed daily bars (US/Eastern date, close), ascending. Today's live bar is excluded."""
    req = urllib.request.Request(cfg["sources"]["bars"], headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        res = json.loads(resp.read().decode())["chart"]["result"][0]
    out: dict[date, float] = {}
    for t, c in zip(res.get("timestamp") or [], res["indicators"]["quote"][0].get("close") or []):
        if c is not None:
            out[datetime.fromtimestamp(t, NY).date()] = float(c)
    meta = res.get("meta") or {}
    rmt, rmp = meta.get("regularMarketTime"), meta.get("regularMarketPrice")
    if rmt and rmp:
        mt = datetime.fromtimestamp(rmt, NY)
        if mt.date() not in out and mt.hour * 60 + mt.minute >= 16 * 60:
            out[mt.date()] = float(rmp)
    today = now_ny.date()
    after_close = now_ny.hour * 60 + now_ny.minute >= 16 * 60 + 10
    return sorted((d, c) for d, c in out.items() if d < today or (d == today and after_close))


# ---------- core (pure) ----------

def hit(level: dict, close: float) -> bool:
    p = float(level["price"])
    return close <= p if level["op"] == "le" else close < p


def rearm_at(level: dict, buf_pct: float) -> float:
    return round(float(level["price"]) * (1 + buf_pct / 100), 4)


def step(levels: list[dict], buf_pct: float, armed: dict | None, close: float) -> tuple[dict, list[str]]:
    """One completed close. armed[key] True = watching (not triggered). Returns (new armed, fired keys).
    armed None = first run: init from this close, nothing fires."""
    close = round(float(close), 4)
    if armed is None:
        return {lv["key"]: not hit(lv, close) for lv in levels}, []
    new, fired = dict(armed), []
    for lv in levels:
        k = lv["key"]
        if new.get(k, True):
            if hit(lv, close):
                new[k] = False
                fired.append(k)
        elif close > rearm_at(lv, buf_pct):
            new[k] = True  # quiet re-arm
    return new, fired


def message(cfg: dict, fired: list[str], close: float, day: str, prev_close: float | None) -> tuple[str, str, int]:
    lv = {x["key"]: x for x in cfg["levels"]}
    buf = float(cfg.get("rearm_buffer_pct", 1.0))
    top = max((lv[k] for k in fired), key=lambda x: x["priority"])
    labels = " + ".join(lv[k]["label"] for k in fired)
    act = "추가 매수 멈춤" if "break46" in fired else "계획한 3차만 분할"
    title = f"[PCOR-PX] 종가 {close:.2f}달러 {top['label']} | {act}"
    if "break46" in fired:
        todo = "46달러 아래 이탈입니다. 남은 분할은 멈추고 실적(11/4 아침 KST 판정)과 이탈 이유부터 확인. 오늘 추가 매수 금지."
    else:
        todo = "3차 분할 구간입니다. 계획한 3차 금액만 나눠서 검토. 한 번에 다 넣지 않기."
    chg = f", 전 거래일 대비 {(close / prev_close - 1) * 100:+.1f}%" if prev_close else ""
    ref = cfg.get("reference", {})
    lines = [
        f"{datetime.now(KST):%Y-%m-%d %H:%M} KST | PCOR | Procore",
        "이게 뭐 알림: " + cfg["explain"],
        f"지금 결과/상태: {labels}. 미국 종가 {close:.2f}달러({day} 미 동부{chg}). 선 48달러(3차 분할), 46달러(지지선). 실제 52주 저점 {ref.get('w52_low')}달러({ref.get('w52_low_date')}).",
        "지금 할 일: " + todo,
        "나중에: " + " ".join(f"{lv[k]['price']:.0f}달러 선은 종가 {rearm_at(lv[k], buf):.2f}달러 위 회복 뒤에만 다시 울림." for k in fired),
        "익절/줄일 때: 3분기 판정이 청산이거나 새 대규모 전환사채/증자가 나오면 분할 중단, 보유분 재평가.",
        "하지 말 것: 장중 급락 추격 물타기, 계획 금액 초과, 신용/미수, 이 알림만 보고 주문. 투자 권유 아님.",
    ]
    return title, "\n".join(lines), int(top["priority"])


# ---------- selftest ----------

def selftest() -> int:
    cfg = load_cfg()
    L, buf = cfg["levels"], float(cfg["rearm_buffer_pct"])
    fails = 0

    def check(label, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(("ok  " if ok else "FAIL"), label, got, "" if ok else f"want {want}")

    # first run never fires, even below both lines
    check("init 63", step(L, buf, None, 63.0), ({"buy3": True, "break46": True}, []))
    check("init 45", step(L, buf, None, 45.0), ({"buy3": False, "break46": False}, []))
    check("init 48 exact", step(L, buf, None, 48.0)[1], [])
    A = {"buy3": True, "break46": True}
    check("48.01 nothing", step(L, buf, A, 48.01)[1], [])
    check("48.00 buy3 (<=)", step(L, buf, A, 48.0)[1], ["buy3"])
    check("46.00 buy3 only (46 is strict <)", step(L, buf, A, 46.0)[1], ["buy3"])
    check("45.99 gap both", step(L, buf, A, 45.99)[1], ["buy3", "break46"])
    # walk: fire once, hold inside buffer, re-arm above buffer, fire again
    walk = [(47.5, ["buy3"]), (47.0, []), (48.3, []), (48.48, []), (47.9, []), (48.49, []), (48.0, ["buy3"]),
            (45.9, ["break46"]), (46.3, []), (45.5, []), (46.46, []), (45.9, []), (46.47, []), (45.99, ["break46"]),
            (49.0, []), (47.0, ["buy3"]), (60.0, []), (45.0, ["buy3", "break46"])]
    st = dict(A)
    for i, (c, want) in enumerate(walk):
        st, f = step(L, buf, st, c)
        check(f"walk{i} {c}", f, want)
    check("float noise 48.000001 rounds to 48 (fires)", step(L, buf, A, 48.000001)[1], ["buy3"])
    check("float noise 48.0001 above 48", step(L, buf, A, 48.0001)[1], [])
    check("rearm prices", (rearm_at(L[0], buf), rearm_at(L[1], buf)), (48.48, 46.46))
    t, body, p = message(cfg, ["buy3", "break46"], 45.5, "2026-11-04", 49.0)
    check("combined priority 4", p, 4)
    check("no middle dot / em-dash", any(ch in t + body for ch in "\u00b7\u2014"), False)
    print(t)
    print("selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


# ---------- main ----------

def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode == "selftest":
        return selftest()
    dry = mode == "dry"
    cfg = load_cfg()
    now = datetime.now(KST)
    print(f"{now.isoformat(timespec='seconds')} pcorpx mode={mode}")
    if dry:
        print("DRY RUN (no ntfy, no state write)")
    vu = cfg.get("valid_until")
    if vu and now.date() > date.fromisoformat(vu):
        print(f"expired (valid_until {vu}); silent")
        return 0
    st = load_state()
    try:
        bars = fetch_bars(cfg, now.astimezone(NY))
    except Exception as exc:
        print(f"bars fetch failed: {exc}")
        if not st.get("fail_notified"):
            title = "[PCOR-PX] 시세 조회 실패 | 매매 신호 아님"
            msg = f"PCOR 일봉 조회 실패: {exc}. 다음 09:00 실행에서 다시 봅니다."
            if dry:
                print(f"DRY RUN would send (ops): {title}\n{msg}")
            else:
                publish(resolve_topic("ops"), title, msg, 3)
                st["fail_notified"] = True
                save_state(st, dry)
        return 0
    if not bars:
        print("no completed bars")
        return 0
    st["fail_notified"] = False
    L, buf = cfg["levels"], float(cfg.get("rearm_buffer_pct", 1.0))
    last_d, last_c = bars[-1]
    if (now.date() - last_d).days > int(cfg.get("stale_days", 6)):
        print(f"stale bar {last_d}; skip")
        return 0
    armed = st.get("armed") if st.get("initialized") else None
    if armed is None:
        armed, _ = step(L, buf, None, last_c)
        st.update({"initialized": True, "armed": armed, "last_date": str(last_d), "last_close": round(last_c, 4),
                   "init_note": f"initialized from {last_d} close {last_c:.2f}, no send"})
        print(f"init from {last_d} close {last_c:.2f}: {armed} (no send)")
        save_state(st, dry)
        return 0
    prev_date = st.get("last_date")
    new_bars = [(d, c) for d, c in bars if prev_date is None or str(d) > prev_date]
    if mode == "force" and not new_bars:
        new_bars = bars[-1:]
    if not new_bars:
        print(f"no new bar after {prev_date}; armed={armed}; last close {last_c:.2f} ({last_d})")
        return 0
    fired_all: list[str] = []
    prev_close = st.get("last_close")
    for d, c in new_bars:
        armed, fired = step(L, buf, armed, c)
        print(f"bar {d} close {c:.2f} -> fired={fired} armed={armed}")
        fired_all += [k for k in fired if k not in fired_all]
    d, c = new_bars[-1]
    if len(new_bars) > 1:
        prev_close = new_bars[-2][1]
    for lv in L:
        print(f"  line {lv['price']:.2f} ({lv['label']}): {'armed' if armed[lv['key']] else 'triggered'}, distance {(c / lv['price'] - 1) * 100:+.1f}%, rearm above {rearm_at(lv, buf):.2f}")
    if fired_all:
        title, msg, prio = message(cfg, fired_all, c, str(d), prev_close)
        if dry:
            print(f"DRY RUN would send (p{prio}): {title}\n{msg}")
        else:
            publish(resolve_topic("port"), title, msg, prio, tags=["chart_with_downwards_trend"])
    else:
        print("no crossing; silent")
    st.update({"armed": armed, "last_date": str(d), "last_close": round(c, 4)})
    save_state(st, dry)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
