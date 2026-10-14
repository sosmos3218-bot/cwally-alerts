#!/usr/bin/env python3
"""LG Energy Solution zone-entry monitor for mid-long term levels. Afternoon slot only."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from naver_api import stock_trend  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG = ROOT / "configs" / "lgeshold.json"
STATE = ROOT / "state" / "lgeshold.json"


def zone(price: float, z: dict) -> str:
    if price <= z["stop"]:
        return "stop"
    if z["buy_low"] <= price <= z["buy_high"]:
        return "buy"
    if price >= z["target2"]:
        return "target2"
    if price >= z["target1"]:
        return "target1"
    return "neutral"


def body(cfg: dict, price: float, zname: str, now: datetime) -> list[str]:
    z = cfg["zones"]
    label = {
        "stop": "손절/재검토 구간 진입",
        "buy": "추가매수 구간 진입",
        "target1": "1년 목표 구간 진입",
        "target2": "2년 목표 구간 진입",
    }.get(zname, zname)
    return [
        f"이게 뭐 알림: LG에너지솔루션 373220 {label}.",
        f"지금 결과/상태: 현재가 {price:,.0f}원. 기준: 손절 {z['stop']:,} / 매수 {z['buy_low']:,}-{z['buy_high']:,} / 목표1 {z['target1']:,} / 목표2 {z['target2']:,}. 조회 {now:%Y-%m-%d %H:%M} KST.",
        "지금 할 일: 구간 진입을 확인하세요. 공시와 ESS 수주 동향을 함께 보고 판단하세요.",
        "나중에: 중장기(1-2년) 관점. 11월 확정 실적과 북미 ESS 진행을 확인하세요.",
        "익절/줄일 때: 목표 구간에서는 일부 익절을 검토하고, 손절 구간에서는 비중 재평가하세요.",
        "하지 말 것: 이 알림만으로 추격매수나 전량 매도하지 마세요. 투자 권유가 아닙니다.",
    ]


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode not in {"auto", "dry"}:
        raise SystemExit(f"unknown mode: {mode}")
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {"last_zone": "neutral"}
    now = datetime.now(KST)
    try:
        rows = stock_trend(cfg["ticker"], 1)
        if not rows or rows[0].get("close") is None:
            raise ValueError("no price")
        price = float(rows[0]["close"])
    except Exception as exc:
        title = "[LGESH] 주가 조회 실패 | 판단 유보"
        lines = [
            "이게 뭐 알림: LG에너지솔루션 구간 감시.",
            f"지금 결과/상태: 조회 실패({type(exc).__name__}). {now:%Y-%m-%d %H:%M} KST.",
            "지금 할 일: 직접 시세를 확인하기 전 판단을 보류하세요.",
            "나중에: 다음 평일 15:20에 다시 확인합니다.",
            "익절/줄일 때: 조회 실패만으로 비중을 바꾸지 마세요.",
            "하지 말 것: 빈 응답을 조건 미충족으로 보지 마세요.",
        ]
        if mode == "dry":
            print(title, *lines, "DRY RUN; no ntfy", sep="\n")
            return 0
        publish(resolve_topic("port"), title, "\n".join(lines), 3, ["warning"], resolve_server(cfg["ntfy_server"]))
        return 0

    cur = zone(price, cfg["zones"])
    prev = state.get("last_zone", "neutral")
    print(f"price={price:.0f} zone={cur} prev={prev}")
    if cur == prev or cur == "neutral":
        if mode == "dry":
            print("no zone entry; DRY RUN")
        return 0

    title = f"[LGESH] {cur} 구간 진입 | 확인"
    lines = body(cfg, price, cur, now)
    if mode == "dry":
        print(title, *lines, "DRY RUN; no ntfy, no state write", sep="\n")
        return 0
    publish(resolve_topic("port", cfg.get("ntfy_topic")), title, "\n".join(lines), 4, ["chart_with_upwards_trend"], resolve_server(cfg["ntfy_server"]))
    state["last_zone"] = cur
    state["last_price"] = price
    state["updated_at"] = now.isoformat(timespec="seconds")
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
