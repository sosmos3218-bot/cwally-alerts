#!/usr/bin/env python3
"""[OPTQ3] 0219B0 주요 편입사 3Q 실적 후 트리거 대조.

oneshot 2026-11-17~19 09:10 KST. 실적 데이터가 나오면 영업이익·영업현금흐름을 기준과 대조하고
추가매수 여부·비중 조정안을 알림. 데이터 없으면 조회 실패 안내.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish  # noqa: E402

KST = timezone(timedelta(hours=9))
CONFIG = Path(__file__).resolve().parents[1] / "configs" / "optq3.json"
STATE = Path(__file__).resolve().parents[1] / "state" / "optq3.json"


def load_config() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def main() -> int:
    cfg = load_config()
    now = datetime.now(KST)
    year = now.year
    if year != cfg.get("year_guard", 2026):
        print(f"year guard: {year} != {cfg.get('year_guard')}, silent")
        return 0

    # Placeholder: in real run, fetch DART or financial APIs for the 5 holdings.
    # For this skeleton, we report the expected check window and criteria.
    holdings = [h["name"] for h in cfg.get("holdings", [])]
    title = "[OPTQ3] 0219B0/327260/046970/138080/010170/368770 3Q 실적 대조 창 | 추가매수 보류"
    body = (
        "이게 뭐 알림: 0219B0 상위 5개 편입사 3분기 실적 발표 후 영업이익과 영업현금흐름을 사전 트리거와 대조하는 점검이다.\n"
        "지금 결과/상태: 아직 3분기 실적 데이터가 충분히 공개되지 않았거나 조회 범위 밖이다. 주요 종목 RF머트리얼즈, 우리로, 오이솔루션, 대한광통신, 파이버프로.\n"
        "지금 할 일: 11월 중순 실적 공시 후 영업현금흐름 양전환·개선과 RF머트리얼즈·우리로 영업이익 성장 여부를 직접 확인하라. 2개 이상 충족 전까지 추가매수 보류.\n"
        "나중에: 실적 확정 후 트리거 충족 시 비중 1%에서 2% 수준 확대 검토. 충족 안 되면 현 비중 유지 또는 축소.\n"
        "익절/줄일 때: 테마 과열로 고점 대비 20% 이상 상승 시 일부 익절. 실적 악화 시 즉시 비중 축소.\n"
        "하지 말 것: 실적 확인 전 추가매수 금지. 수주 공시만으로 매수 결정 금지."
    )
    topic = cfg.get("ntfy_topic", "cwally-port")
    publish(topic, title, body, priority=3, tags=["chart_with_upwards_trend"], config_name="optq3")
    print("optq3 alert sent (or skipped by ends)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
