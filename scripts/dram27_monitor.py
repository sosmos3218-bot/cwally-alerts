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
CFG = ROOT / "configs" / "dram27.json"
STATE = ROOT / "state" / "dram27.json"


def load_state() -> dict:
    base = {"updated_at": None, "sent": {}}
    if not STATE.exists():
        return base
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return base
    base.update(data)
    base["sent"] = dict(base.get("sent") or {})
    return base


def save_state(st: dict, now: datetime) -> None:
    st["updated_at"] = now.isoformat()
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def phase_for(now: datetime, mode: str) -> str | None:
    day = now.strftime("%Y-%m-%d")
    if mode == "force":
        if day >= "2026-10-29":
            return "pce"
        if day >= "2026-10-14":
            return "cpi"
        return "weekly_pre"
    if day == "2026-10-14" and now.hour >= 21:
        return "cpi"
    if day == "2026-10-29" and now.hour >= 21:
        return "pce"
    if day in {"2026-10-13", "2026-10-14"} and now.hour < 21:
        return "pre_cpi"
    if day in {"2026-10-28", "2026-10-29"} and now.hour < 21:
        return "pre_pce"
    if now.weekday() == 0 and day < "2026-10-14":
        return "weekly_pre"
    if now.weekday() == 0 and day < "2026-10-29":
        return "weekly_mid"
    return None


def copy_for(phase: str) -> tuple[str, list[str], int]:
    common_tail = [
        "익절/줄일 때: 이 알림으로 삼성전자·SK하이닉스 비중을 줄이거나 늘리지 마세요. 기존 구간 규칙을 따르세요.",
        "하지 말 것: 2027년 6월을 확장 종료일로 확정. BNK 분기 급락을 키움 경로로 읽기. 월가 한 번으로 D램 계약가가 바뀐다고 보기. 신용·미수·추격매수. 투자 권유 아님.",
    ]
    if phase in {"weekly_pre", "pre_cpi"}:
        return (
            "[DRAM27] 10/14 CPI 전 | 에너지만 보고 계약가는 보류",
            [
                "이게 뭐 알림: 9월 CPI 에너지가 BNK 수요 둔화·키움 가격 저항에 먼저 닿는지 보는 점검입니다. 매수 신호가 아닙니다.",
                "지금 상태: CPI는 10월 14일 21:30 KST. 유안타는 2027년 영업이익 590.5조원, HBM이 범용 생산을 잠식해 수급이 늦게 풀린다는 연간 가정. 키움은 346.4조원으로 올해보다 감소, 4분기 D램 가격 상승 3%. BNK만 2027년 분기 ASP -1/-8/-15/-30%.",
                "지금 할 일: 발표 후 헤드라인 에너지와 근원 에너지만 분리해 읽으세요. 에너지가 헤드라인을 올려도 HBM 웨이퍼 비중은 바뀌지 않습니다.",
                "나중에: 같은 구간은 다시 안 보냅니다. 다음은 10월 29일 21:30 PCE. 만료 10월 30일.",
            ]
            + common_tail,
            3,
        )
    if phase == "cpi":
        return (
            "[DRAM27] CPI 에너지 | BNK·키움 수요 가정부터",
            [
                "이게 뭐 알림: 9월 CPI 에너지가 어느 D램 가정에 먼저 닿는지 정리한 점검입니다. 이 알림은 공식 숫자를 대신 읽지 않습니다.",
                "지금 상태: 에너지 재가속은 BNK 세트·서버 원가 부담과 키움 4분기 가격 저항에 먼저 닿습니다. 유안타 HBM 잠식과 마이크론 2027 물량 75% 배정은 10월 물가로 재협상되지 않습니다.",
                "지금 할 일: BLS 원문에서 에너지, 주거, 근원 서비스를 분리하세요. 에너지만 올랐다고 2027년 4분기 ASP -30%를 확정하지 마세요.",
                "나중에: 10월 29일 PCE에서 실질소비가 같이 꺾이는지 보세요. 근원이 안정이면 수요 저항 가정은 약해집니다.",
            ]
            + common_tail,
            4,
        )
    if phase in {"weekly_mid", "pre_pce"}:
        return (
            "[DRAM27] 10/29 PCE 전 | 소비·에너지만",
            [
                "이게 뭐 알림: 9월 PCE가 키움 소비 약세와 BNK 세트 수요에 먼저 닿는지 보는 점검입니다.",
                "지금 상태: PCE는 10월 29일 21:30 KST. 에너지와 실질소비·저축률이 본체입니다. HBM 웨이퍼 배분은 공급 측이라 이 발표로 안 바뀝니다.",
                "지금 할 일: 발표 후 에너지 기여와 실질소비만 갈라서 보세요. 근원은 금리 경로, 잠식 비율은 별개입니다.",
                "나중에: 10월 29일 22:00 일회성 후 만료. 같은 문구는 다시 안 보냅니다.",
            ]
            + common_tail,
            3,
        )
    return (
        "[DRAM27] PCE 에너지·소비 | 키움·BNK 수요부터",
        [
            "이게 뭐 알림: 9월 PCE 에너지와 실질소비가 키움·BNK 수요 가정에 먼저 닿는지 보는 마지막 점검입니다.",
            "지금 상태: 에너지가 소비까지 번지면 키움 소비 약세와 BNK 세트 수요 둔화가 먼저 힘을 얻습니다. 유안타 잠식은 웨이퍼 비중과 배정 계약이 깨져야 깨집니다.",
            "지금 할 일: BEA 원문에서 에너지, 실질소비, 저축률만 확인하세요. 이 숫자로 2027년 6월 매도 시점을 정하지 마세요.",
            "나중에: 이 점검은 10월 30일 만료. 다음 변경 조건은 HBM 웨이퍼 비중과 계약 재협상입니다.",
        ]
        + common_tail,
        4,
    )


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    st = load_state()
    if now.year != int(cfg.get("event_year") or 2026):
        print("dram27 year-guard silent")
        return 0
    expire = datetime.strptime(cfg["expire_date"], "%Y-%m-%d").date()
    if now.date() > expire and mode != "force":
        print("dram27 expired silent")
        return 0
    if now.weekday() >= 5 and mode not in {"force", "dry"}:
        print("dram27 weekend silent")
        return 0
    phase = phase_for(now, mode)
    print(now.isoformat(), "phase", phase, "mode", mode)
    if not phase:
        save_state(st, now)
        print("dram27 silent")
        return 0
    if st["sent"].get(phase) and mode != "force":
        print("already sent", phase)
        save_state(st, now)
        return 0
    title, lines, pri = copy_for(phase)
    body = [now.strftime("%Y-%m-%d %H:%M KST") + " | DRAM27", *lines]
    print(title)
    print("\n".join(body))
    if mode == "dry":
        save_state(st, now)
        return 0
    publish(
        resolve_topic("port", cfg.get("ntfy_topic")),
        title[:180],
        "\n".join(body),
        pri,
        ["chart_with_upwards_trend"],
        resolve_server(cfg.get("ntfy_server")),
    )
    st["sent"][phase] = now.isoformat()
    save_state(st, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
