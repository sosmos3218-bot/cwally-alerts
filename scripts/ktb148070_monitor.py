#!/usr/bin/env python3
"""One-shot post-BOK MPC alert for 148070 KIWOOM 국고채10년.
Sends structured update with pre-meeting view and recommendation.
Year-guarded for 2026.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Add parent for ntfy_util
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.ntfy_util import publish

CFG_PATH = Path(__file__).resolve().parents[1] / "configs" / "ktb148070.json"

def main():
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    if cfg.get("event_year") != 2026:
        print("Year guard: not 2026, silent exit")
        return

    title = "[KTB148070] 금통위 후 재평가 | 소액 분할 유지"
    body = """이게 뭐 알림
10/22 금통위 후 KIWOOM 국고채10년(148070) 분석 갱신 알림.

지금 결과/상태
기준금리 3.00%. 시장 동결 전망 우세, 11월 추가 인상 가능성 열림.
10/14 기준 공식 YTM 4.245%, Duration 7.583년.
10/19 10년물 4.205%.

지금 할 일
소액 분할매수 유지. 적극 매수 보류. 공식 최신 YTM/듀레이션 확인 후 비중 조정.

나중에
추가 인상 신호 약화 + 물가 둔화 확인 시 매수 확대 검토.
10년물 4.4% 재상승 시 가격 매력 재평가 (물가 악화면 주의).

익절/줄일 때
자금 사용 시점 가까워지거나 긴축 강화 시 비중 축소.

하지 말 것
안전하게 연 4% 받는 상품으로 보지 말 것. 원금 손실 가능. 단기 차익 베팅 금지.

(투자 권유 아님. 개인 판단 필요.)
"""
    publish(
        topic=cfg["ntfy_topic"],
        title=title,
        message=body,
        priority=3,
        tags=["chart_with_upwards_trend"],
    )
    print("Published ktb148070 alert")

if __name__ == "__main__":
    main()
