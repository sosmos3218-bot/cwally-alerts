#!/usr/bin/env python3
"""SK이노베이션우 3Q 실적 후 재평가 리마인더. year-guard 2026."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish  # noqa: E402

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "ski096775q3.json"
STATE_PATH = ROOT / "state" / "ski096775q3.json"


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    if now.year != int(cfg.get("year_guard", 2026)):
        print("ski096775q3 year-guard skip")
        return 0

    if mode == "dry":
        print("DRY RUN: ski096775q3 checklist would send")
        print("[SKIQ3] 3Q 실적 후 우선주 재평가 | 체크리스트")
        return 0

    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    title = "[SKIQ3] 3Q 실적 후 우선주 재평가 | 체크리스트"
    lines = [
        f"{stamp} | SKIQ3 | SK이노베이션우 096775",
        "이게 뭐 알림: 2026년 3분기 실적 발표 후 재고이익 제외 경상 영업이익, 배당 재개 가능성, 주주환원 정책 내용을 기준으로 우선주 매력도를 다시 보는 리마인더입니다. 투자 권유 아닙니다.",
        "지금 결과: 3분기 실적은 10/29 전후 발표 예상. 재고평가이익·래깅 제외 경상 OP, SK온 지속 흑자 여부, 배당 재개 시기, 주주환원 정책 구체 내용을 DART/HTS에서 확인하세요.",
        "지금 할 일: 실적 공시와 IR 자료를 보고 위 3가지 기준으로 재평가. 괴리율이 45% 이하로 좁혀졌다면 매력 감소.",
        "나중에: 정책 발표 후 추가 확인. 유동성이 다시 좋아지면 재평가.",
        "익절/줄일 때: 괴리율 35% 이하 지속 시 매력 감소. 유동성 부족 유지.",
        "하지 말 것: 실적 내용 확인 전 추격 매수, 수천만 원 이상 규모 진입, 재고이익을 경상 이익으로 단정.",
    ]
    publish(
        topic=cfg.get("ntfy_topic", "cwally-port"),
        title=title,
        message="\n".join(lines),
        priority=4,
        tags=["chart_with_upwards_trend"],
    )
    print("ski096775q3 sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
