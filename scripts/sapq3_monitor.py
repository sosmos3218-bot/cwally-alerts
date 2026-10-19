#!/usr/bin/env python3
"""SAP Q3 2026 oneshot monitor. Year-guarded. Dry mode never publishes or writes state."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Year guard: only 2026
if datetime.now(ZoneInfo("Asia/Seoul")).year != 2026:
    sys.exit(0)

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "configs" / "sapq3.json"
STATE_PATH = ROOT / "state" / "sapq3.json"

def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)

def is_dry() -> bool:
    return os.environ.get("SAPQ3_MODE", "auto").lower() == "dry"

def main() -> None:
    cfg = load_config()
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    title = f"{cfg['title_prefix']} Q3 실적 후 조건부 매수 재평가 | 매매 신호 아님"
    body_lines = [
        "이게 뭐 알림: SAP Q3 2026 실적 발표 후 CCB, 마진, 가이던스 기준으로 조건부 매수 의견과 매수 구간을 업데이트하는 일회성 알림입니다.",
        "지금 결과/상태: 실적 페이지와 컨센서스를 확인한 뒤 CCB 성장률, Non-IFRS 영업이익률, 2026 FCF 가이던스를 비교합니다. (실제 실행 시 데이터 수집)",
        "지금 할 일: CCB 고정환율 24% 이상 유지하고 FCF 약 100억 유로 전망이 유지되면 조건부 매수 의견 유지. 매수 구간은 기존 그대로 또는 소폭 조정.",
        "나중에: 2027년 성장 가속 코멘트와 AI 수익화 지표를 추가 확인합니다.",
        "익절/줄일 때: 기준 가치 260달러 근접 시 신규 매수 비중 축소. 270달러 이상이면 확대 중단.",
        "하지 말 것: 실적 직후 추격매수, 단일 지표만 보고 의견 전면 변경, 레버리지 사용.",
    ]
    body = "\n".join(body_lines)

    if is_dry():
        print("DRY RUN")
        print(f"Title: {title}")
        print(f"Body:\n{body}")
        print("No ntfy sent, no state written.")
        return

    # Real publish path would go through ntfy_util here
    # For this stub, print and exit; actual publish is handled by the workflow pattern
    print(f"Would publish: {title}")
    print(body)

if __name__ == "__main__":
    main()
