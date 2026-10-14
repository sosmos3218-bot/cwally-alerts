#!/usr/bin/env python3
"""SK이노베이션 우선주 096775 괴리율 점검 (상대가치 포지션).

Slot: existing weekday 09:00 morning. No new cron.
Sends on zone change or weekly summary.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_calendar import is_trading_day, prev_trading_day  # noqa: E402
from naver_api import stock_trend  # noqa: E402
from ntfy_util import publish, resolve_server  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "096775.json"
STATE_PATH = ROOT / "state" / "096775.json"
CLOSE_READY = (20, 30)

def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}

def save_state(state: dict, now: datetime) -> None:
    state["updated_at"] = now.isoformat(timespec="seconds")
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE_PATH)

def expected_session(now: datetime) -> date:
    d = now.date()
    if is_trading_day(d) and (now.hour, now.minute) >= CLOSE_READY:
        return d
    return prev_trading_day(d)

def fetch_close(code: str, session: date) -> float | None:
    rows = stock_trend(code, 5)
    key = session.strftime("%Y%m%d")
    for r in rows:
        if r.get("day") == key and r.get("close") is not None:
            return float(r["close"])
    return None

def zone_of(pct: float, rules: dict) -> str:
    if pct >= rules["zone_high"]:
        return "high"
    if pct >= rules["zone_watch"]:
        return "watch"
    if pct >= rules["zone_review"]:
        return "review"
    if pct >= rules["zone_exit"]:
        return "exit"
    return "low"

ZONE_KO = {
    "high": "상대가치 매력 높음 (50% 이상)",
    "watch": "보통 수준 (45~50%)",
    "review": "1차 재평가 구간 (40~45%)",
    "exit": "적극 축소 고려 (35~40%)",
    "low": "상대가치 약화 (35% 미만)",
}

def main() -> int:
    now = datetime.now(KST)
    if not is_trading_day(now.date()):
        print("not trading day")
        return 0
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = read_json(STATE_PATH)
    session = expected_session(now)
    try:
        pref = fetch_close(cfg["ticker"], session)
        common = fetch_close(cfg["ticker_common"], session)
    except Exception as exc:
        print(f"fetch fail: {exc}")
        return 0
    if pref is None or common is None or common <= 0:
        print("no close")
        return 0
    discount = (1 - pref / common) * 100
    z = zone_of(discount, cfg["rules"])
    prev_z = state.get("zone")
    prev_d = state.get("discount_pct")
    iso = now.isocalendar()
    week_key = f"{iso[0]}-W{iso[1]:02d}"
    weekly = not (state.get("weekly_sent") or {}).get(week_key)
    changed = prev_z is not None and z != prev_z
    big_move = prev_d is not None and abs(discount - prev_d) >= cfg["rules"]["change_alert_pct"]
    if not (changed or weekly or big_move):
        print(f"no event: {discount:.1f}% {z}")
        return 0
    title = f"{cfg['title_prefix']} 괴리율 {discount:.1f}% | {ZONE_KO[z]}"
    lines = [
        "이게 뭐 알림: SK이노베이션 우선주(096775) 상대가치(괴리율) 점검.",
        f"지금 결과/상태: 우선주 {pref:,.0f}원, 보통주 {common:,.0f}원 ({session:%Y-%m-%d}). 괴리율 {discount:.1f}%. 구간: {ZONE_KO[z]}",
        "지금 할 일: " + (
            "50% 이상이면 분할 매수 또는 보유 유지 검토." if z == "high" else
            "40% 근접이면 부분 익절 또는 비중 축소 검토." if z in ("review", "exit") else
            "35% 이하이면 상대가치 포지션 정리 검토." if z == "low" else
            "보유 유지. 정책 발표와 실적 모니터링."
        ),
        "나중에: 주주환원 정책 발표(연말~내년 초)와 3분기 실적 확인.",
        "익절/줄일 때: 괴리율 40% 부근 1차 재평가, 35% 이하 적극 축소.",
        "하지 말 것: 괴리율만 보고 기계적 전량 매도, 유동성 무시하고 시장가 주문.",
    ]
    dry = "--dry" in sys.argv
    if dry:
        print("[dry]", title, *lines, sep="\n")
        return 0
    try:
        publish(cfg["ntfy_topic"], title, "\n".join(lines), 3, ["chart_with_upwards_trend"], resolve_server(cfg.get("ntfy_server")))
        state["zone"] = z
        state["discount_pct"] = discount
        state["pref"] = pref
        state["common"] = common
        state.setdefault("weekly_sent", {})[week_key] = True
        save_state(state, now)
        print("sent")
    except Exception as exc:
        print(f"publish fail: {exc}")
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
