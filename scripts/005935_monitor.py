#!/usr/bin/env python3
"""Samsung Electronics Preferred 005935 discount rate monitor.

Based on 2026-10-13 analysis: long-term dividend + relative value.
Entry: discount >= 30%, Exit: <= 18%, Hold: 25-29%.
Uses existing morning slot + oneshot around Oct 29. No new cron.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from naver_api import stock_trend  # noqa: E402
from ntfy_util import publish, resolve_server  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "005935.json"
STATE_PATH = ROOT / "state" / "005935.json"

def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def get_price(code: str) -> float | None:
    try:
        rows = stock_trend(code, 1)
        if rows and rows[0].get("close"):
            return float(rows[0]["close"])
    except Exception:
        pass
    return None

def run(force: bool = False) -> int:
    cfg = load_json(CFG_PATH)
    state = load_json(STATE_PATH)
    now = datetime.now(KST)
    if now.weekday() >= 5 and not force:  # weekend, unless oneshot
        return 0
    pref = get_price(cfg["ticker"])
    common = get_price(cfg["ticker_common"])
    if pref is None or common is None or common <= 0:
        print("price fetch failed")
        return 0
    discount = (common - pref) / common * 100
    prev_discount = state.get("last_discount")
    events = []
    action = "유지"
    if discount >= cfg["entry_discount"]:
        action = "확대 검토"
        if prev_discount is None or prev_discount < cfg["entry_discount"]:
            events.append(f"괴리율 {discount:.1f}%로 진입 구간(30%이상) 도달")
    elif discount <= cfg["exit_discount"]:
        action = "축소 검토"
        if prev_discount is None or prev_discount > cfg["exit_discount"]:
            events.append(f"괴리율 {discount:.1f}%로 청산 구간(18%이하) 도달")
    else:
        action = "유지"
    # Weekly summary on Monday or force (oneshot)
    iso_week = now.isocalendar()
    week_key = f"{iso_week[0]}-W{iso_week[1]:02d}"
    weekly_due = (now.weekday() == 0 and state.get("last_weekly") != week_key) or force
    if not events and not weekly_due:
        state.update({"last_discount": discount, "last_pref": pref, "last_common": common, "updated": now.isoformat()})
        save_state(state)
        return 0
    title = f"{cfg['title_prefix']} 괴리율 {discount:.1f}% | {action}" + (" (공시 원샷)" if force else "")
    lines = [
        f"이게 뭐 알림: 삼성전자우 005935 괴리율 점검. 배당/장기 보유 + 상대가치 관점." + (" 10/29 공시 전후 원샷." if force else ""),
        f"지금 결과/상태: 우선주 {pref:,.0f}원, 보통주 {common:,.0f}원, 괴리율 {discount:.1f}%",
        f"현재 구간: 진입 30%이상 / 유지 25-29% / 청산 18%이하. 현재 판단: {action}",
        "지금 할 일: " + ("비중 확대 검토" if action == "확대 검토" else "비중 축소 검토" if action == "축소 검토" else "기존 비중 유지, 공시 확인"),
        "나중에: 10월 29일 실적 공시 후 재점검. 괴리율 2-3%p 변동 시 재확인.",
        "익절/줄일 때: 18% 이하 축소, 15% 이하 강한 축소.",
        "하지 말 것: 괴리율만으로 즉시 매매. 실적과 함께 확인.",
    ]
    if events:
        lines.insert(2, "바뀐 점: " + "; ".join(events))
    try:
        publish("cwally-port", title, "\n".join(lines), 3, ["chart_with_upwards_trend"], resolve_server(cfg.get("ntfy_server")))
        print("sent:", title)
    except Exception as e:
        print("publish failed", e)
        return 1
    state.update({
        "last_discount": discount,
        "last_pref": pref,
        "last_common": common,
        "last_weekly": week_key if weekly_due else state.get("last_weekly"),
        "updated": now.isoformat()
    })
    save_state(state)
    return 0

if __name__ == "__main__":
    force = len(sys.argv) > 1  # oneshot passes arg
    raise SystemExit(run(force=force))
