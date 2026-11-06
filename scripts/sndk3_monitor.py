#!/usr/bin/env python3
"""[005930][000660] SNDK switch 3/3 one-time memo (2026-11-09 09:00 KST, catch-up 11/10)."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "sndk3.json"
STATE_PATH = ROOT / "state" / "sndk3.json"


def build(cfg: dict, stamp: str) -> tuple[str, list[str]]:
    f = cfg["facts"]
    title = f"{cfg['title_prefix']} SNDK 스위치 3/3 | 같은 방향 관찰 전환"
    lines = [
        f"{stamp} | 1회 메모, 후속 발송 없음. 매수 또는 매도 신호가 아닙니다.",
        "이게 뭐: SNDK 실적 체크리스트의 스위치 3개가 모두 켜졌습니다. 이제 삼성전자(005930)와 SK하이닉스(000660)를 SNDK와 같은 방향으로 관찰합니다.",
        (
            f"스위치 1 켜짐: SNDK 매출 {f['sndk_rev_busd']}억달러(가이던스 {f['sndk_guide_busd']}억), "
            f"non-GAAP 이익률 {f['sndk_gm_nongaap']}%."
        ),
        (
            f"스위치 2 켜짐: 데이터센터 매출 {f['sndk_dc_busd']}억달러, Q4 {f['sndk_dc_q4_busd']}억 대비 "
            f"+{f['sndk_dc_qoq_pct']}%. 증가분 약 60%가 물량."
        ),
        f"스위치 3 켜짐: {f['switch3_basis']}.",
        f"반대 근거: {f['switch3_counter']}.",
        "지금 할 일: 실제 진입과 축소는 기존 [005930], [000660] 계획선 알림을 따르세요. 이 메모로 비중을 바로 올리지 마세요.",
        "꺼지는 때: 서버 D램 계약가가 꺾이거나 HBM 품절이 풀리면 스위치 3이 꺼진 것으로 다시 보세요.",
        f"SNDK 실적 발표일: {f['sndk_report_date']}. 출처: https://investor.sandisk.com/",
    ]
    return title, lines


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = {"notified": False}
    if STATE_PATH.exists():
        try:
            state.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    title, lines = build(cfg, stamp)
    print(f"{stamp} mode={mode} today={today}")

    if mode == "dry":
        print(title)
        print("\n".join(lines))
        print("dry-run; no ntfy")
        return 0

    if mode != "force":
        if now.year != int(cfg["year_guard"]) or not (cfg["send_date"] <= today <= cfg["window_end"]):
            print("outside send window; silent")
            return 0
        if state.get("notified"):
            print("already sent; silent")
            return 0

    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    publish(topic, title, "\n".join(lines), 4, ["chart_with_upwards_trend"], server)
    state["notified"] = True
    state["sent_at"] = now.isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
