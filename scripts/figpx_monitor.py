#!/usr/bin/env python3
"""[FIG-PX] Figma (FIG, NYSE) close-based zones (approved in chat 2026-10-26).

  - close <= 22.00 -> '매수 검토 구간 진입'      (priority 4)
  - close <= 16.60 -> '약세 시나리오 가치 도달'  (priority 5)
  - close >= 28.40 -> '기본 시나리오 가치 도달'  (priority 3)

Each zone fires once when a completed close ENTERS it. It re-arms (quietly) only after a close
leaves the zone (le: close above the line, ge: close below the line). No buffer.
The first run (no state) only records the current close, it never sends.
Dedupe by the bar's own US/Eastern date (state last_date). Same pattern as pcorpx_monitor.py.

Runs on the existing morning 09:00 KST slot (after the US close). No new cron.
Modes: auto | force (ignore date dedupe, still respects arm state) | dry (print only, no ntfy, no state)
       | confirm (install-confirmation send, no state) | selftest.
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
CFG = ROOT / "configs" / "figpx.json"
STATE = ROOT / "state" / "figpx.json"
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
    if level["op"] == "le":
        return close <= p
    if level["op"] == "ge":
        return close >= p
    return close < p  # "lt"


def step(levels: list[dict], armed: dict | None, close: float) -> tuple[dict, list[str]]:
    """One completed close. armed[key] True = watching (outside the zone). Returns (new armed, fired keys).
    armed None = first run: init from this close, nothing fires."""
    close = round(float(close), 4)
    if armed is None:
        return {lv["key"]: not hit(lv, close) for lv in levels}, []
    new, fired = dict(armed), []
    for lv in levels:
        k = lv["key"]
        inside = hit(lv, close)
        if new.get(k, True):
            if inside:
                new[k] = False
                fired.append(k)
        elif not inside:
            new[k] = True  # left the zone: quiet re-arm
    return new, fired


def lines_text(cfg: dict) -> str:
    return "매수 검토 22달러 이하, 약세 시나리오 16.6달러 이하, 기본 시나리오 가치 28.4달러 이상"


def message(cfg: dict, fired: list[str], close: float, day: str, prev_close: float | None) -> tuple[str, str, int]:
    lv = {x["key"]: x for x in cfg["levels"]}
    top = max((lv[k] for k in fired), key=lambda x: x["priority"])
    labels = " + ".join(lv[k]["label"] for k in fired)
    if "bear166" in fired:
        act = "매수 논리 재점검"
        todo = "약세 시나리오 값($16.6)까지 내려왔습니다. 추가 매수 전에 매수 논리부터 재점검(NDR 125% 미만 여부, 매출 가이던스 하향 여부 등). 오늘 일괄 매수 금지."
    elif "buy22" in fired:
        act = "분할매수 검토"
        todo = "$20~22 분할매수 검토 구간 (사업 전망 유지 전제). 실적 악화로 인한 하락이면 기준 재계산 필요. 계획 금액을 나눠서만 검토."
    else:
        act = "비중 점검, 추격 금지"
        todo = "기본 시나리오 12개월 가치 $28.4에 도달했습니다. 추가 매수보다 비중과 목표 재점검."
    title = f"[FIG-PX] 종가 {close:.2f}달러 {top['label']} | {act}"
    chg = f", 전 거래일 대비 {(close / prev_close - 1) * 100:+.1f}%" if prev_close else ""
    notes = " ".join(lv[k]["note"] for k in fired)
    lines = [
        f"{datetime.now(KST):%Y-%m-%d %H:%M} KST | FIG | 피그마",
        "이게 뭐 알림: " + cfg["explain"],
        f"지금 결과/상태: {labels}. 미국 종가 {close:.2f}달러({day} 미 동부{chg}). {notes}",
        "지금 할 일: " + todo,
        f"나중에: 같은 구간은 종가가 구간을 벗어난 뒤 다시 들어올 때만 또 울립니다. 선: {lines_text(cfg)}.",
        "익절/줄일 때: 28.4달러 이상은 기본 시나리오 가치 도달이라 추가 매수보다 일부 정리와 비중 점검이 먼저입니다.",
        "하지 말 것: 장중 급락 추격 물타기, 계획 금액 초과, 신용/미수, 이 알림만 보고 주문. 투자 권유 아님.",
    ]
    return title, "\n".join(lines), int(top["priority"])


def confirm_message(cfg: dict, close: float | None, day: str | None) -> tuple[str, str]:
    px = f"{close:.2f}달러({day} 미 동부 종가)" if close is not None else "조회 실패(구간 미확인)"
    title = "[FIG-PX] 설치 확인 | 매매 신호 아님"
    lines = [
        "[테스트] FIG 알림 설정 완료",
        "이게 뭐 알림: " + cfg["explain"],
        f"지금 결과/상태: FIG 현재 종가 {px}. 선 3개: 22달러 이하 매수 검토(우선순위 4), 16.6달러 이하 약세 시나리오(5), 28.4달러 이상 기본 시나리오 가치(3).",
        "지금 할 일: 없음. 평일 09:00 KST morning 에서 전일 미국 종가로 판정하고, 구간에 새로 들어갈 때만 보냅니다. 실적 전날(11/4 09:00)과 체크리스트(11/5 08:00)는 [FIG-EARN] 으로 옵니다.",
        "나중에: 같은 구간은 종가가 구간을 벗어난 뒤 다시 들어올 때만 또 울립니다.",
        "익절/줄일 때: 28.4달러 이상 알림이 오면 비중 점검.",
        "하지 말 것: 이 설치 확인을 매수/매도 신호로 읽기.",
    ]
    return title, "\n".join(lines)


# ---------- selftest ----------

def selftest() -> int:
    cfg = load_cfg()
    L = cfg["levels"]
    fails = 0

    def check(label, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(("ok  " if ok else "FAIL"), label, got, "" if ok else f"want {want}")

    A = {"buy22": True, "bear166": True, "base284": True}
    check("init 24.64", step(L, None, 24.64), (A, []))
    check("init 21 no send", step(L, None, 21.0), ({"buy22": False, "bear166": True, "base284": True}, []))
    check("init 30 no send", step(L, None, 30.0)[1], [])
    check("22.01 nothing", step(L, A, 22.01)[1], [])
    check("22.00 buy (<=)", step(L, A, 22.0)[1], ["buy22"])
    check("16.60 gap buy+bear", step(L, A, 16.6)[1], ["buy22", "bear166"])
    check("28.39 nothing", step(L, A, 28.39)[1], [])
    check("28.40 base (>=)", step(L, A, 28.4)[1], ["base284"])
    walk = [(21.5, ["buy22"]), (21.0, []), (22.0, []), (22.01, []), (21.9, ["buy22"]),
            (16.5, ["bear166"]), (16.0, []), (16.61, []), (16.6, ["bear166"]), (25.0, []),
            (28.5, ["base284"]), (29.0, []), (28.39, []), (28.4, ["base284"]), (20.0, ["buy22"])]
    st = dict(A)
    for i, (c, want) in enumerate(walk):
        st, f = step(L, st, c)
        check(f"walk{i} {c}", f, want)
    for fired in (["buy22"], ["buy22", "bear166"], ["base284"]):
        t, body, p = message(cfg, fired, 16.5 if "bear166" in fired else 21.0, "2026-11-05", 23.0)
        check(f"no middle dot / em-dash {fired}", any(ch in t + body for ch in "\u00b7\u2014"), False)
        print(p, t)
    t, body = confirm_message(cfg, 24.64, "2026-10-23")
    check("confirm no middle dot / em-dash", any(ch in t + body for ch in "\u00b7\u2014"), False)
    check("priorities", message(cfg, ["buy22", "bear166"], 16.5, "d", None)[2], 5)
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
    print(f"{now.isoformat(timespec='seconds')} figpx mode={mode}")
    if dry:
        print("DRY RUN (no ntfy, no state write)")
    if mode == "confirm":  # install-confirmation send (AGENTS.md): no state
        try:
            bars = fetch_bars(cfg, now.astimezone(NY))
            d, c = bars[-1]
            title, msg = confirm_message(cfg, c, str(d))
        except Exception as exc:
            print(f"bars fetch failed: {exc}")
            title, msg = confirm_message(cfg, None, None)
        print(title)
        print(msg)
        publish(resolve_topic("port"), title, msg, 3, tags=["white_check_mark"])
        return 0
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
            title = "[FIG-PX] 시세 조회 실패 | 매매 신호 아님"
            msg = f"FIG 일봉 조회 실패: {exc}. 다음 09:00 실행에서 다시 봅니다."
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
    L = cfg["levels"]
    last_d, last_c = bars[-1]
    if (now.date() - last_d).days > int(cfg.get("stale_days", 6)):
        print(f"stale bar {last_d}; skip")
        return 0
    armed = st.get("armed") if st.get("initialized") else None
    if armed is None:
        armed, _ = step(L, None, last_c)
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
        armed, fired = step(L, armed, c)
        print(f"bar {d} close {c:.2f} -> fired={fired} armed={armed}")
        fired_all += [k for k in fired if k not in fired_all]
    d, c = new_bars[-1]
    if len(new_bars) > 1:
        prev_close = new_bars[-2][1]
    for lv in L:
        print(f"  line {lv['price']:.2f} ({lv['label']}): {'armed' if armed[lv['key']] else 'inside zone'}, distance {(c / lv['price'] - 1) * 100:+.1f}%")
    if fired_all:
        title, msg, prio = message(cfg, fired_all, c, str(d), prev_close)
        if dry:
            print(f"DRY RUN would send (p{prio}): {title}\n{msg}")
        else:
            tag = "chart_with_upwards_trend" if fired_all == ["base284"] else "chart_with_downwards_trend"
            publish(resolve_topic("port"), title, msg, prio, tags=[tag])
    else:
        print("no zone entry; silent")
    st.update({"armed": armed, "last_date": str(d), "last_close": round(c, 4)})
    save_state(st, dry)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
