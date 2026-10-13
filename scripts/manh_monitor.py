#!/usr/bin/env python3
"""Post-Q3 2026 MANH investment attractiveness re-evaluation."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ntfy_util import publish  # type: ignore

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "manh.json"

def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode == "dry":
        print("DRY RUN")
        return 0

    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")

    # Only send after earnings window (script is oneshot, but guard anyway)
    if now.date().isoformat() < cfg.get("earnings_date", "2026-10-27"):
        print("before earnings; silent")
        return 0

    base = cfg.get("baseline_from_analysis", {})
    watches = base.get("key_watch", [])

    title = "[MANH] Q3 2026 실적 후 투자매력 재평가"
    lines = [
        f"시점: {stamp}",
        "Q3 2026 실적(10/27 장후) 확인 후 다음 기준으로 재평가.",
        "",
        "기준 항목 (이전 분석 baseline 대비):",
        f"- 클라우드 매출 성장: Q2 +26% → 25%+ 유지/가속 여부",
        f"- RPO 성장: +23% → 유지 또는 가속",
        f"- 부킹: 강세 지속 (넷 뉴 + 온프렘미 전환)",
        f"- AI 관련: 에이전트 수익/계약 기여 가시화, 고객 피드백",
        f"- 가이던스: 상향 또는 보수화 여부",
        "",
        f"이전 기준 (10/13): 주가 ~${base.get('price', 218)}, Trailing PE ~{base.get('trailing_pe', 62)}x, MS PT ${base.get('ms_pt', 250)}",
        "",
        "재평가 방향:",
        "- 클라우드/부킹 강세 + 가이던스 상향 → 매력 상향 (리레이팅 여지)",
        "- 성장 둔화 또는 가이던스 보수 → 밸류 부담 가중, 신중",
        "- AI 수익 기여 확인 시 내러티브 강화",
        "",
        "실적 수치와 콘퍼런스콜 코멘트를 확인한 뒤 위 기준으로 판단. 투자 권유 아님."
    ]
    message = "\n".join(lines)

    publish(
        topic="cwally-port",
        title=title,
        message=message,
        priority=3,
        tags=["chart_with_upwards_trend"],
    )
    print("published post-earnings re-eval")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
