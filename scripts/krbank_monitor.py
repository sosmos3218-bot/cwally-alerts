#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "krbank.json"
STATE_PATH = ROOT / "state" / "krbank.json"

sys.path.insert(0, str(ROOT / "scripts"))
from ntfy_util import publish  # noqa: E402


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    force = mode in {"force", "manual", "dispatch"}

    prev = {"updated_at": None, "last_alert_date": None, "last_mode": None, "sent": False}
    if STATE_PATH.exists():
        try:
            prev.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass

    if not force and prev.get("sent"):
        print("krbank already sent; silent")
        return 0

    m = cfg["mark"]
    x = cfg["exit"]
    title = "[KRBANK] 우리·하나 2027·PBR 0.9가 만기"
    lines = [
        f"{stamp} | KRBANK",
        "이게 뭐 알림: 우리금융·하나금융 보유 만기 메모입니다. 달력이 아니라 환원·CET1·PBR 조건입니다.",
        (
            f"지금 상태: 9/23 종가 기준 우리 {m['woori_px']:,}원 PBR {m['woori_pbr']} / "
            f"하나 {m['hana_px']:,}원 PBR {m['hana_pbr']}. "
            f"목표 우리 {m['woori_tp']:,} / 하나 {m['hana_tp']:,}. "
            "초저평가는 끝났고 남은 것은 환원율 45~50% 재평가다."
        ),
        (
            "지금 할 일: 하나는 우리보다 짧게 보다. "
            "시세가 목표면 ISA에 넣고 배당을 받는다. "
            "중국 H주 전액 환헤지는 하지 않는다."
        ),
        (
            "나중에: 기본 시계는 2026 4분기~2027 환원 가이던스 확인까지. "
            "그 다음에도 들고 가려면 저PBR이 아니라 안정 배당주로만 본다."
        ),
        (
            f"익절/줄일 때: 우리 {x['woori_trim_lo']:,}~{x['woori_trim_hi']:,}원, "
            f"하나 {x['hana_trim_lo']:,}~{x['hana_trim_hi']:,}원, 또는 PBR {x['pbr_done']}배 안착. "
            "CET1이 13%대 아래로 깨지고 자사주가 멈추거나 현금배당을 줄이면 더 일찍 줄인다."
        ),
        (
            "하지 말 것: 영구 보유로 두기. "
            "하나와 우리를 같은 만기로 두기. "
            "환원율이 꺾였는데 저PBR만 보고 추격. 신용/미수."
        ),
    ]
    msg = "\n".join(lines)
    print(msg)
    publish("cwally-port", title, msg, priority=4, tags=["bank", "memo"])
    STATE_PATH.write_text(
        json.dumps(
            {
                "updated_at": stamp,
                "last_alert_date": now.date().isoformat(),
                "last_mode": mode,
                "sent": True,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
