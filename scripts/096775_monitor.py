#!/usr/bin/env python3
"""SK이노베이션 우선주 096775: 보통주(096770) 대비 괴리율 점검.

Slot: existing weekday 09:00 morning bundle. No new cron.
Sends to cwally-port only when zone changes, disparity moves >= change_alert_pct,
or weekly summary. Lookup problems go to cwally-ops.

Zones (configs/096775.json):
  high   : disparity >= 50%   (매력 구간, 비중 확대 검토)
  watch  : 45% <= disparity < 50%
  review : 40% <= disparity < 45%  (재검토)
  exit   : disparity < 40% (35% 이하 지속 시 청산 검토)
  change_alert_pct : |Δ괴리율| >= 2.0%p 시 알림

Modes: auto (default), dry (no state, no send).
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

ZONE_KO = {
    "high": "매력 구간(50%+)",
    "watch": "관찰(45~50%)",
    "review": "재검토(40~45%)",
    "exit": "청산 검토(<40%)",
}


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


def fetch_close(code: str, session: date) -> tuple[float | None, str | None]:
    rows = stock_trend(code, 5)
    key = session.strftime("%Y%m%d")
    for r in rows:
        if r.get("day") == key and r.get("close") is not None:
            return r["close"], r.get("day")
    return None, None


def zone_of(disp: float, rules: dict) -> str:
    if disp >= rules["zone_high"]:
        return "high"
    if disp >= rules["zone_watch"]:
        return "watch"
    if disp >= rules["zone_review"]:
        return "review"
    return "exit"


def evaluate(cfg: dict, state: dict, disp: float, prev_disp: float | None) -> dict:
    rules = cfg["rules"]
    zone = zone_of(disp, rules)
    events: list[str] = []
    prev_zone = state.get("zone")
    if prev_zone and zone != prev_zone:
        events.append(f"구간 변경: {ZONE_KO.get(prev_zone, prev_zone)} -> {ZONE_KO[zone]}")
    delta = None
    if prev_disp is not None:
        delta = disp - prev_disp
        if abs(delta) >= rules["change_alert_pct"]:
            events.append(f"괴리율 변동 {delta:+.1f}%p (기준 {rules['change_alert_pct']}%p)")
    iso = datetime.now(KST).isocalendar()
    iso_week = f"{iso[0]}-W{iso[1]:02d}"
    weekly_due = not (state.get("weekly_sent") or {}).get(iso_week)
    return {
        "zone": zone,
        "events": events,
        "delta": delta,
        "weekly_due": weekly_due,
        "iso_week": iso_week,
    }


def todo_line(zone: str) -> str:
    if zone == "high":
        return "비중 확대 검토. 유동성 확인하고 지정가 분할만. 정책 발표 전엔 과도한 확대 금지."
    if zone == "watch":
        return "관찰 유지. 45% 아래로 내려가면 재검토. 신규 매수는 보류."
    if zone == "review":
        return "40%대면 비중 축소 검토. 35% 이하 지속 시 청산 검토."
    return "35% 이하 지속 시 청산 검토. 신규 매수 금지."


def build_message(cfg: dict, disp: float, pref: float, common: float, session: date, res: dict, kind: str) -> tuple[str, list[str]]:
    rules = cfg["rules"]
    zone = res["zone"]
    title = f"{cfg['title_prefix']} {ZONE_KO[zone]} | 괴리율 {disp:.1f}%"
    status = [
        f"우선주 {pref:,.0f}원, 보통주 {common:,.0f}원 ({session:%Y-%m-%d} 완료 세션)",
        f"괴리율 {disp:.1f}% ({ZONE_KO[zone]}, 기준 high {rules['zone_high']}% / review {rules['zone_review']}% / exit {rules['zone_exit']}%)",
    ]
    if res.get("delta") is not None:
        status.append(f"전일 대비 {res['delta']:+.1f}%p")
    lines = [
        "이게 뭐 알림: SK이노베이션 우선주 096775 보통주(096770) 대비 괴리율 점검. 1~2년 상대가치 포지션.",
        "지금 결과/상태:",
        *[f"- {s}" for s in status],
    ]
    if res["events"]:
        lines.append("바뀐 점:")
        lines += [f"- {e}" for e in res["events"]]
    lines += [
        "지금 할 일: " + todo_line(zone),
        "나중에: 주주환원 정책 발표(연내~내년 초)와 3분기 실적 확인. 정책이 우선주 우대면 기준 상향, 보통주 중심이면 하향.",
        "익절/줄일 때: 괴리율 40% 이하로 좁혀지면 비중 축소, 35% 이하 지속 시 청산 검토.",
        "하지 말 것: 괴리율만 보고 시장가 매수, 유동성 무시, 정책 발표 전 과도한 확대.",
        f"출처: Naver m.stock API (trend)",
    ]
    return title, lines


def send(cfg: dict, title: str, lines: list[str], dry: bool, ops: bool = False, priority: int = 3) -> bool:
    topic = "cwally-ops" if ops else "cwally-port"
    if dry:
        print(f"[dry] topic={topic}", title, *lines, "dry-run; no ntfy", sep="\n")
        return True
    try:
        publish(topic, title, "\n".join(lines), priority,
                ["warning"] if ops else ["chart_with_upwards_trend"],
                resolve_server(cfg.get("ntfy_server")))
        return True
    except Exception as exc:
        print(f"publish failed: {type(exc).__name__}")
        return False


def problem(cfg: dict, state: dict, now: datetime, key: str, detail: str, dry: bool) -> None:
    day = now.strftime("%Y-%m-%d")
    ops_daily = state.setdefault("ops_daily", {})
    if ops_daily.get(key) == day and not dry:
        print(f"ops already sent today: {key}")
        return
    title = f"{cfg['title_prefix']} {key} 조회 실패 | 판단 유보"
    lines = [
        "이게 뭐 알림: SK이노베이션 우선주 096775 괴리율 점검의 데이터 문제 안내.",
        f"지금 결과/상태: {detail}. {now:%Y-%m-%d %H:%M} KST. 이번 실행의 판단은 막았습니다.",
        "지금 할 일: 없음. 다음 거래일에 자동 재시도합니다.",
        "나중에: 이틀 이상 계속되면 Naver API 변경 여부를 확인하세요.",
        "익절/줄일 때: 이 알림만으로 판단하지 마세요.",
        "하지 말 것: 누락된 숫자를 0이나 예전 값으로 보고 매매.",
    ]
    if send(cfg, title, lines, dry, ops=True, priority=2) and not dry:
        ops_daily[key] = day


def run(mode: str) -> int:
    now = datetime.now(KST)
    cfg = read_json(CFG_PATH)
    if not cfg:
        print("config missing")
        return 1
    state = read_json(STATE_PATH)
    dry = mode == "dry"
    session = expected_session(now)
    pref, pref_day = fetch_close(cfg["ticker"], session)
    common, common_day = fetch_close(cfg["ticker_common"], session)
    if pref is None or common is None or pref_day != common_day:
        problem(cfg, state, now, "시세", f"우선주/보통주 {session:%Y-%m-%d} 종가 불일치 또는 누락", dry)
        return 0
    disp = (1 - pref / common) * 100
    prev_disp = state.get("disparity")
    res = evaluate(cfg, state, disp, prev_disp)
    should_send = bool(res["events"]) or res["weekly_due"]
    if should_send:
        kind = "event" if res["events"] else "weekly"
        title, lines = build_message(cfg, disp, pref, common, session, res, kind)
        if send(cfg, title, lines, dry) and not dry:
            state["disparity"] = disp
            state["zone"] = res["zone"]
            state["last_session"] = session.isoformat()
            weekly = state.setdefault("weekly_sent", {})
            weekly[res["iso_week"]] = True
            save_state(state, now)
    else:
        print(f"no change: disp={disp:.1f}% zone={res['zone']}")
    return 0


if __name__ == "__main__":
    mode_arg = sys.argv[1] if len(sys.argv) > 1 else "auto"
    if mode_arg not in {"auto", "dry"}:
        print(f"unknown mode: {mode_arg}")
        sys.exit(2)
    sys.exit(run(mode_arg))
