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
CFG_PATH = ROOT / "configs" / "yencarry.json"
STATE_PATH = ROOT / "state" / "yencarry.json"

TITLE = "[YENCARRY] 한방 청산 말고 레벨만 대응"


def body(now: str) -> str:
    return "\n".join(
        [
            f"{now} | YENCARRY | 원·엔·미국채·코스피",
            "이게 뭐 알림: 엔캐리(엔화 빌려 해외투자)가 한방에 터질지 보는 행동 메모입니다.",
            "지금 상태: 달러/엔 155, 원달러 1,369원, 미국 10년 5.0%, 일본 10년 3.0%·30년 4.1%, 코스피 6,718(+1.4%). 외국인은 6거래일 약 12조 순매도.",
            "한 줄 결론: 일본 보험사가 미국채를 한꺼번에 던질 확률은 낮습니다. 천천히 옮기는 중입니다. 원달러 1,200원 급락은 기본 그림이 아닙니다.",
            "",
            "지금 할 일:",
            "- 엔: 155에서 추격하지 마세요. 152 아래면 엔 강세 확인, 160 위면 접습니다.",
            "- 원: 엔이 오른다고 원도 오른다고 같이 베팅하지 마세요. 1,340원 아래는 달러 보유를 줄이고, 1,400원 근처면 달러를 조금 삽니다.",
            "- 미국채: 초장기 비중만 줄이세요. 폭락에 숏하지 마세요.",
            "- 코스피: 추격·레버리지 금지. 외국인 매도가 멈추는지 먼저 봅니다.",
            "나중에: 연준 결과 본 뒤. 볼 숫자 네 개만 — 일본 30년 4.3%/4.5%, 달러/엔 152/160, 원달러 1,340/1,400, 외국인 순매수.",
            "익절/줄일 때: 엔 롱은 160 재돌파면 접기. 원달러가 1,340 아래로 가면 달러 보유를 줄이기.",
            "하지 말 것: 엔캐리 한 방으로 원·코스피·미국채가 같이 무너진다고 몰빵. 엔 강세=원 강세로 같은 방향 베팅.",
        ]
    )


def main() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    now_s = now.strftime("%Y-%m-%d %H:%M KST")
    state = {"sent": False, "updated_at": None}
    if STATE_PATH.exists():
        try:
            state.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    if state.get("sent"):
        print("already sent; skip")
        return 0
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    msg = body(now_s)
    print(msg)
    publish(topic, TITLE, msg, 4, ["jp", "chart_with_upwards_trend"], server)
    STATE_PATH.write_text(
        json.dumps({"sent": True, "updated_at": now.isoformat(), "title": TITLE}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
