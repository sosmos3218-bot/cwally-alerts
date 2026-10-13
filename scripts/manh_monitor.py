#!/usr/bin/env python3
"""One-shot MANH investment attractiveness analysis summary."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ntfy_util import publish  # type: ignore

def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode == "dry":
        print("DRY RUN")
        return 0

    title = "[MANH] 현시점 투자매력 분석"
    message = """현 시점 MANH는 고품질 공급망 SW 프랜차이즈로 장기 보유 매력 있으나, 밸류에이션 프리미엄으로 단기 매수 매력은 제한적.

- 주가 ~$218 (10/12 종가), MS Overweight PT $250 업그레이드 후 +6%
- TTM 매출 $1.13B (+6.5%), 영업마진 ~25%, 클라우드 +26% (Q2), RPO +23%
- Trailing PE ~62x / Forward ~37x, 컨센서스 목표가 근처
- 강점: 고수익성, 현금창출, AI 수혜 내러티브 전환
- 약점: 밸류 부담, 전체 성장 중저단, 10/27 Q3 실적 확인 필요
- 판단: 장기 퀴리티 OK, 신규 진입은 실적 확인 후 또는 조정 시. 투자 권유 아님.

다음: Q3 클라우드/부킹/RPO 확인."""

    publish(
        topic="cwally-port",
        title=title,
        message=message.strip(),
        priority=3,
        tags=["chart_with_upwards_trend"],
    )
    print("published")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
