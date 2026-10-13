#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "jpfy27.json"
STATE_PATH = ROOT / "state" / "jpfy27.json"


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"last_sent": None}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    now = datetime.now(KST)
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = load_state()

    ends = cfg.get("ends_kst")
    if ends:
        try:
            end_dt = datetime.strptime(ends, "%Y-%m-%d %H:%M").replace(tzinfo=KST)
            if now > end_dt:
                print("past ends_kst; silent")
                return 0
        except ValueError:
            pass

    if now.weekday() >= 5:
        print("weekend; skip")
        return 0

    title = "[JPFY27] 예산·세법 모니터링 | 재원 구체성 확인 필요"
    lines = [
        f"{now.strftime('%Y-%m-%d %H:%M KST')} | JPFY27 | FY2027 예산·식품세",
        "이게 뭐 알림: 다카이치 정권 2027계정연도 예산안 최종 편성(연말)과 식품소비세 인하 법안 통과 전망, BOJ 금리 경로와 결합한 엔화·JGB·닛케이 중기 전망 주기 점검.",
        "지금 상태: 예산 요구 143조 엔(역대 최고), 전략투자 프레임워크 약 14조 엔, 식품세 8%→1%(2년, 연 5조 엔 규모) 법안 국회 제출. 재원=적자국채 금지 강조하나 구체성 부족. 10년 JGB ~3%, USD/JPY 157~158대.",
        "지금 할 일: 연말 예산안 신규 국채 발행 규모와 재원 조달 상세를 확인. BOJ 추가 인상 속도와 결합해 시나리오 업데이트.",
        "나중에: 예산안 확정(연말), 법안 처리(12월 12일 임시국회 목표), BOJ 다음 회의 결과 후 전망 재조정.",
        "익절/줄일 때: 재원 명확화·발행 통제 확인 시 엔 약세·장기금리 부담 완화 기대로 전략산업 비중 유지. 재원 공백 확대 시 엔·JGB 변동성 대비 축소.",
        "하지 말 것: 재원 미확정 상태에서 대규모 재정 확장=무조건 주가 상승으로 단정. 엔 약세 지속 가정만으로 레버리지.",
    ]
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server()
    publish(topic, title, "\n".join(lines), 3, ["jp", "chart_with_upwards_trend"], server)

    state["last_sent"] = now.isoformat()
    save_state(state)
    print("sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
