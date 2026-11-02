"""ntfy 요약. 기본은 꺼져 있고 이 모듈은 발송하지 않습니다.

제목은 [TREND] 로 시작하고, 관련 종목코드를 넣습니다.
종목이 없으면 지수 티커 KOSPI 를 넣습니다.
본문 순서는 저장소 알림 문안과 같습니다.
"""

from __future__ import annotations

from pathlib import Path

from trend_monitor.config import Config


def _tickers(payload: dict) -> list[str]:
    found = []
    for row in payload.get("holdings") or []:
        if row.get("signal") != "hold" and row.get("ticker") not in found:
            found.append(row["ticker"])
    for row in payload.get("candidates") or []:
        if row.get("status") == "ok" and row.get("ticker") not in found:
            found.append(row["ticker"])
    return found


def build_title(payload: dict) -> str:
    tickers = _tickers(payload)
    tail = " ".join(tickers) if tickers else "KOSPI"
    sells = sum(1 for row in payload.get("holdings") or [] if row.get("signal") != "hold")
    fresh = sum(1 for row in payload.get("candidates") or [] if row.get("status") == "ok")
    if sells or fresh:
        action = f"매도 {sells} 신규 {fresh}"
    else:
        action = "보유 유지, 신규 없음"
    prefix = "[TREND] DEMO" if payload.get("demo") else "[TREND]"
    return f"{prefix} {action} | {tail}"


def build_body(payload: dict, cfg: Config) -> str:
    day = payload["trade_date"]
    sells = [row for row in payload["holdings"] if row["signal"] != "hold"]
    oks = [row for row in payload["candidates"] if row["status"] == "ok"]
    sell_text = ", ".join(f"{row['ticker']} {row['signal']} {row['sell_qty']}주" for row in sells) or "없음"
    new_text = ", ".join(f"{row['ticker']} {row['qty']}주" for row in oks[:5]) or "없음"
    demo = "DEMO. 실매매 신호가 아닙니다. " if payload.get("demo") else ""
    lines = [
        f"이게 뭐 알림: {demo}추세 추종 일일 점검입니다. 종가 기준으로 보유와 신규 돌파를 계산했습니다. 기준일 {day}.",
        f"지금 결과/상태: 매도 {len(sells)}건 ({sell_text}). 신규 적정 {len(oks)}건 ({new_text}). 시장 폭 {payload['risk'].get('breadth_pct_above_ma50')}%.",
        "지금 할 일: 매도 수량은 다음 거래일에 줄이고, 신규는 status 가 ok 인 수량만 봅니다.",
        "나중에: 클라이맥스 다음은 평단과 이평 중 높은 값이 방어선입니다. 상태 저장은 사람이 --apply-state 를 준 때만 합니다.",
        "익절/줄일 때: 손절, 패닉, 이평 이탈은 잔량 전부. 클라이맥스만 절반.",
        "하지 말 것: 장중 저가로 끊지 마세요. 돌파가보다 5% 넘게 오른 종목은 사지 마세요. 미수, 신용은 쓰지 마세요.",
    ]
    end = "종료일 없음" if not cfg.ends_kst else f"{cfg.ends_kst} KST"
    note = f" ({cfg.ends_note})" if cfg.ends_note else ""
    lines.append(f"꺼지는 때: {end}{note}")
    return "\n".join(lines)


def build_message(payload: dict, cfg: Config) -> tuple[str, str]:
    return build_title(payload), build_body(payload, cfg)


def maybe_publish(payload: dict, cfg: Config, allow_send: bool = False, sender=None) -> dict:
    """allow_send 가 아니면 네트워크를 호출하지 않습니다. CLI 는 이 값을 넘기지 않습니다."""
    title, body = build_message(payload, cfg)
    if not cfg.ntfy_enabled or not allow_send:
        return {"sent": False, "reason": "ntfy 가 꺼져 있습니다.", "title": title, "body": body}
    if cfg.ntfy_topic not in {"cwally-port", "cwally-ops"}:
        return {"sent": False, "reason": "허용되지 않은 토픽입니다.", "title": title, "body": body}
    if sender is None:
        sender = _repo_publish
    sender(cfg.ntfy_topic, title, body, cfg.ntfy_priority, cfg)
    return {"sent": True, "reason": "", "title": title, "body": body}


def _repo_publish(topic: str, title: str, body: str, priority: int, cfg: Config) -> None:
    import sys

    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from scripts.ntfy_util import publish

    publish(
        topic,
        title,
        body,
        priority=priority,
        tags=["chart_with_upwards_trend"],
        config={
            "name": "trend",
            "title_prefix": "[TREND]",
            "ends_kst": cfg.ends_kst,
            "ends_note": cfg.ends_note,
        },
        append_end=False,
    )
