#!/usr/bin/env python3
"""SAMNIX: 삼전닉스 외국인·기관 수급 + 고수 순매수 요약 + 반등 평가."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ntfy_util import append_ends, route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "state" / "samnix.json"
CFG_PATH = ROOT / "configs" / "samnix.json"
CODES = {"005930": "삼성전자", "000660": "SK하이닉스"}


def fmt_krw(won: float) -> str:
    sign = "+" if won > 0 else ""
    if abs(won) >= 1_000_000_000_000:
        return f"{sign}{won / 1_000_000_000_000:.2f}조"
    if abs(won) >= 100_000_000:
        return f"{sign}{won / 100_000_000:.0f}억"
    return f"{sign}{won:,.0f}원"


def fetch_stock_flow(code: str, day: str) -> dict | None:
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import naver_api
        return naver_api.stock_trend(code, day)
    except Exception as exc:
        print(f"{code} flow fail: {exc}")
        return None


def ntfy(server: str, topic: str, title: str, message: str, priority: int = 3) -> None:
    message = append_ends(message)
    import urllib.request
    payload = json.dumps(
        {"topic": route_topic(topic, title), "title": title, "message": message, "priority": priority, "tags": ["chart_with_upwards_trend"]}
    ).encode()
    req = urllib.request.Request(server.rstrip("/"), data=payload, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(resp.read().decode())


def main() -> int:
    now = datetime.now(KST)
    sys.path.insert(0, str(ROOT / "scripts"))
    import kr_calendar

    session = kr_calendar.session_day(now)
    if not kr_calendar.is_trading_day(session):
        print(f"{session} 휴장/주말 skip")
        return 0
    day = session.strftime("%Y%m%d")

    cfg = {}
    if CFG_PATH.exists():
        try:
            cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cfg = {}
    topic = cfg.get("ntfy_topic") or "cwally-port"
    server = cfg.get("ntfy_server") or "https://ntfy.sh"
    explain = cfg.get("explain") or "삼전닉스 수급·고수·수요 요약. 매수 신호 아님."

    flows = {}
    for code, name in CODES.items():
        fl = fetch_stock_flow(code, day)
        if fl:
            flows[name] = fl

    # High-performer summary is based on recent public reports (not live scrape)
    high_note = "키움·미래에셋 상위 1%는 급락 후 반등 구간에 삼전닉스 재매수 경향. 최신 보도는 당일 지연 가능."

    # Memory/AI demand placeholder (extend later with real indicators)
    demand_note = "메모리 가격·AI 수요는 최근 보도 기준 견조. 세부 지표는 추후 확장."

    if not flows:
        print("수급 데이터 없음; quiet skip")
        return 0

    fgn_total = sum(f.get("외국인", 0) for f in flows.values())
    inst_total = sum(f.get("기관합계", 0) for f in flows.values())

    if fgn_total > 0 and inst_total > 0:
        title = "[SAMNIX] 외국인+기관 매수 | 반등 지지"
        do_now = "수급이 지지 중입니다. 기존 보유는 유지, 신규 추격은 자제하세요."
        eval_txt = "반등 지속 가능성 보통 이상. 외국인 매수 전환이 이어지면 강화."
    elif fgn_total < 0:
        title = "[SAMNIX] 외국인 매도 지속 | 반등 약함"
        do_now = "외국인 매도가 남아 있습니다. 추가 매수는 보류하고 기존 포지션만 관리하세요."
        eval_txt = "반등 지속 가능성 낮음. 외국인 매도 진정 확인 전 관망."
    else:
        title = "[SAMNIX] 수급 혼조 | 계획 유지"
        do_now = "뚜렷한 방향 없음. 기존 계획대로 두세요."
        eval_txt = "반등 지속 가능성 불확실. 추가 지표 확인 필요."

    lines = [
        f"{now.strftime('%Y-%m-%d %H:%M KST')} | SAMNIX",
        f"이게 뭐 알림: {explain}",
        f"지금 상태: 외국인 합 {fmt_krw(fgn_total)} / 기관 합 {fmt_krw(inst_total)}",
        f"고수 동향: {high_note}",
        f"수요 지표: {demand_note}",
        f"평가: {eval_txt}",
        f"지금 할 일: {do_now}",
        "나중에: 내일 같은 시간에 다시 보냅니다.",
        "익절/줄일 때: 이 알림으로 매매하지 마세요. 개별 종목 알림을 따르세요.",
        "하지 말 것: 수급 숫자만 보고 추격 매수.",
        "",
        "종목별:",
    ]
    for name, fl in flows.items():
        lines.append(f"- {name} 외국인 {fmt_krw(fl.get('외국인', 0))} / 기관 {fmt_krw(fl.get('기관합계', 0))}")

    msg = "\n".join(lines)
    print(msg)

    prev = {}
    if STATE_PATH.exists():
        try:
            prev = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            prev = {}
    if prev.get("day") == day:
        print("already sent today; silent")
        return 0

    ntfy(server, topic, title, msg, 3)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps({"day": day, "updated_at": now.isoformat()}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
