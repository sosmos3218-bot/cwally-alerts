#!/usr/bin/env python3
"""SAMNIX: confirmed two-stock flows in shares, for reference only."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ntfy_util import append_ends, route_topic

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "state" / "samnix.json"
CFG_PATH = ROOT / "configs" / "samnix.json"
CODES = {"005930": "삼성전자", "000660": "SK하이닉스"}


def fmt_shares(value: float) -> str:
    return f"{value:+,.0f}주"


def fmt_krw(won: float) -> str:
    return f"{won / 100_000_000:+,.0f}억원"


def read_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch_stock_flow(code: str, day: str) -> tuple[dict | None, str]:
    import naver_api
    try:
        rows = naver_api.stock_trend(code, 10)
    except Exception as exc:
        return None, f"{code} 조회 실패: {exc}"
    row = next((r for r in rows if r.get("day") == day), None)
    if row is None:
        latest = max((str(r.get("day", "")) for r in rows), default="없음")
        return None, f"{code} 확정 수급 기준일 {day} 없음 (최신 {latest})"
    if any(row.get(k) is None for k in ("foreign", "organ", "close")):
        return None, f"{code} 외국인/기관 수급 또는 종가 누락 ({day})"
    return row, ""


def ntfy(server: str, topic: str, title: str, message: str, priority: int = 3) -> None:
    import urllib.request
    payload = json.dumps({"topic": route_topic(topic, title), "title": title,
                          "message": append_ends(message), "priority": priority,
                          "tags": ["chart_with_upwards_trend"]}).encode()
    req = urllib.request.Request(server.rstrip("/"), data=payload, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        print("ntfy ok", resp.read().decode())


def main(mode: str = "auto") -> int:
    if mode not in ("auto", "dry", "test"):
        raise ValueError(f"unsupported mode: {mode}")
    now = datetime.now(KST)
    import kr_calendar
    session = kr_calendar.session_day(now)
    if not kr_calendar.is_trading_day(session):
        print(f"{session} 휴장/주말 skip")
        return 0
    # At 15:20 KST, use the preceding trading day's confirmed row only.
    flow_day = kr_calendar.prev_trading_day(session).strftime("%Y%m%d")
    send_day = session.strftime("%Y%m%d")
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    topic = cfg.get("ntfy_topic") or "cwally-port"
    server = cfg.get("ntfy_server") or "https://ntfy.sh"
    state = read_state()

    flows = {}
    errors = []
    for code, name in CODES.items():
        row, error = fetch_stock_flow(code, flow_day)
        if error:
            errors.append(error)
        else:
            flows[name] = row
    if errors:
        reason = "; ".join(errors)
        print(f"수급 데이터 없음; quiet skip: {reason}")
        if mode == "auto":
            state["last_skip"] = {"day": send_day, "reason": reason, "at": now.isoformat()}
            save_state(state)
        return 0

    # Naver reports net shares, not KRW. Convert by each stock's close before aggregation.
    foreign = sum(r["foreign"] * r["close"] for r in flows.values())
    organ = sum(r["organ"] * r["close"] for r in flows.values())
    if foreign > 0 and organ > 0:
        signal = "외국인+기관 순매수 | 수급 우호"
        do_now = "전 거래일 수급은 우호적입니다. 당일 수급 확인 전 추격하지 마세요."
    elif foreign < 0:
        signal = "외국인 순매도 | 반등 신중"
        do_now = "전 거래일 외국인 순매도입니다. 당일 수급 확인 전 추가 매수를 보류하세요."
    else:
        signal = "수급 혼조 | 계획 유지"
        do_now = "전 거래일 수급이 혼조입니다. 기존 계획을 유지하세요."
    title = f"[SAMNIX] 참고용 {signal}"
    if mode == "test":
        title = f"[SAMNIX] 테스트 참고용 {signal}"
    label = f"{flow_day[:4]}-{flow_day[4:6]}-{flow_day[6:]} 수급 주식 수 × 해당일 종가 (추정금액)"
    lines = [
        f"{now.strftime('%Y-%m-%d %H:%M KST')} | SAMNIX | 수급 기준 {label}",
        "이게 뭐 알림: 삼성전자/하이닉스 전 거래일 확정 수급 참고용. 매매 신호가 아닙니다.",
        f"지금 상태: 외국인 합 {fmt_krw(foreign)} / 기관 합 {fmt_krw(organ)} ({label})",
        "고수 동향: 당일 상위 투자자 순매수 자료 미확보. 고정 문구로 동향을 단정하지 않습니다.",
        "수요 지표: 당일 메모리 가격/AI 수요 지표 미확보. 별도 확인이 필요합니다.",
        "평가: 전 거래일 수급 방향만 확인됨. 당일 반등 지속성은 판단 불가.",
        f"지금 할 일: {do_now}",
        "나중에: 다음 평일 15:20에 최신 확정 수급을 확인합니다.",
        "익절/줄일 때: 이 알림만으로 매매하지 말고 개별 종목 조건을 따르세요.",
        "하지 말 것: 전 거래일 추정 수급금액만 보고 당일 추격 매수.",
        "", "종목별:",
    ]
    for name, row in flows.items():
        lines.append(f"- {name} 외국인 {fmt_shares(row['foreign'])} ({fmt_krw(row['foreign'] * row['close'])}) / 기관 {fmt_shares(row['organ'])} ({fmt_krw(row['organ'] * row['close'])})")
    message = "\n".join(lines)
    print(title, "\n", message)
    if mode == "dry":
        print("DRY: ntfy/state 변경 없음")
        return 0
    if mode == "auto" and state.get("day") == send_day:
        print("already sent today; silent")
        return 0
    ntfy(server, topic, title, message)
    if mode == "test":
        print("TEST: state 변경 없음")
        return 0
    state.update({"day": send_day, "flow_day": flow_day, "updated_at": now.isoformat()})
    state.pop("last_skip", None)
    save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "auto"))
