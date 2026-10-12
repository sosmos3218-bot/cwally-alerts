#!/usr/bin/env python3
"""Samsung E&A (028050) pre-earnings monitoring reminder."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.ntfy_util import publish  # type: ignore

TOPIC = "cwally-port"
TITLE = "[SENA] 실적 전 모니터링 | 수주·마진 확인"

BODY = """이게 뭐 알림: 삼성E&A 3분기 실적(예상 10/22~23) 전 모니터링 리마인더.

지금 결과/상태: 현재 증권사 기대치 기준 수주 16조·마진 10% 시나리오 대기 중. 상향/유지/하향 결합 표 준비됨.

지금 할 일: 수주 공시·가이던스 코멘트, 마진 우려 뉴스, 현대건설 대비 상대 PER/ROE 확인. 이벤트 발생 시 시나리오 표와 대조.

나중에: 10/22~23 실적·가이던스 발표 후 표의 임계값과 실제 숫자 대조해 시나리오 확정.

익절/줄일 때: 가이던스가 16조·마진 10% 상회 시 상향 가중치, 하회 시 재평가.

하지 말 것: 추격매수, 신용, 미수. 매수 지시 아님."""

def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "dry":
        print("DRY RUN")
        print(TITLE)
        print(BODY)
        return
    publish(TOPIC, TITLE, BODY)

if __name__ == "__main__":
    main()
