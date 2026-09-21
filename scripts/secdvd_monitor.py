#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ntfy_util import publish, resolve_server, resolve_topic

ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "secdvd.json"
STATE_PATH = ROOT / "state" / "secdvd.json"
KST = timezone(timedelta(hours=9))

WINDOWS = {
    ("2026-09-23", "am"): {
        "title": "[연휴 전 마지막 거래] 수급 기록·추격 매수 금지",
        "body": (
            "이게 뭐 알림\n"
            "삼성전자 3분기 현금배당 일정 알림. 오늘이 추석 휴장 전 마지막 거래일.\n\n"
            "지금 상태\n"
            "24·25일 국내장 휴장. 28일 월 재개=배당 매수 마감. 29일 화 배당락. 30일 수 기준일.\n"
            "21일 기준(잠정): 삼성 274,000원 / 외인 +1.07조 기관 +1.13조. 하이닉스 1,868,000원 / 외인 -0.81조.\n\n"
            "지금 할 일\n"
            "외인·기관 순매수를 기록하되, 28일 전에 이벤트/추세로 단정하지 마세요.\n\n"
            "나중에\n"
            "28일 09:00 매수 마감 알림, 29일 15:20 판정 알림.\n\n"
            "익절/줄일 때\n"
            "오늘 수급으로 판정하지 말 것. 29일 배당락 후 외인·기관이 같이 팔면 배당 이벤트.\n\n"
            "하지 말 것\n"
            "주당 4,604원을 확정처럼 쓰지 말 것. 추격매수·신용·미수 금지. 투자 권유 아님."
        ),
    },
    ("2026-09-28", "am"): {
        "title": "[오늘 매수 마감] 배당 자격 마지막 날",
        "body": (
            "이게 뭐 알림\n"
            "삼성전자 3분기 현금배당 자격 마지막 날. 오늘 살 것만 30일 기준일 주주.\n\n"
            "지금 상태\n"
            "추석 후 재개일. 미 24·25일 2거래일+미중 회담이 한번에 반영. 주당 금액은 10월 말 이사회 전.\n\n"
            "지금 할 일\n"
            "배당이 필요하면 오늘 장중 매수. 외인·기관 순매수를 21+22+23+오늘로 누적.\n\n"
            "나중에\n"
            "29일 15:20 수급으로 이벤트/추세/유보 판정.\n\n"
            "익절/줄일 때\n"
            "29일 외인·기관이 동반 매도면 배당 캐치로 본다.\n\n"
            "하지 말 것\n"
            "연휴 갥 때문에 추격매수 금지. 오늘 수급만으로 업황 추세라 단정하지 말 것. 투자 권유 아님."
        ),
    },
    ("2026-09-29", "pm"): {
        "title": "[배당락] 수급 보고 이벤트/추세 판정",
        "body": (
            "이게 뭐 알림\n"
            "삼성 배당락 당일 수급 판정일. 기준일은 내일 30일.\n\n"
            "지금 상태\n"
            "A 이벤트: 28일까지 삼성 외인+기관 순매수였는데 29일 둘 다 순매도 + 하이닉스 외인도 팔면.\n"
            "B 추세: 29일에도 삼성 외인+기관 순매수 + 하이닉스 외인 매도 중단/순매수.\n"
            "C 유보: 외인·기관 방향이 갈리거나 28일 갥만 크면 30일까지.\n\n"
            "지금 할 일\n"
            "거래소 15:30 수급으로 A/B/C 중 하나만 고르세요.\n\n"
            "나중에\n"
            "10월 말 이사회 주당 확정. 월간 수출 확정의 반도체 물량.\n\n"
            "익절/줄일 때\n"
            "A면 배당 이벤트로 본다. B여야 수급을 업황으로 올린다.\n\n"
            "하지 말 것\n"
            "하루 숫자로 추격매수 금지. 주당 추정액을 확정처럼 쓰지 말 것. 투자 권유 아님."
        ),
    },
}


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def slot(now: datetime) -> str:
    if now.hour < 12:
        return "am"
    return "pm"


def main() -> None:
    cfg = load_json(CFG_PATH, {})
    now = datetime.now(KST)
    if now.year != int(cfg.get("year_guard", 2026)):
        print("year-guard silent")
        return
    if now.date().isoformat() > str(cfg.get("expire_date", "2026-09-30")):
        print("expired silent")
        return
    key = (now.date().isoformat(), slot(now))
    if key not in WINDOWS:
        print(f"no window {key}")
        return
    state = load_json(STATE_PATH, {"last_sent_key": "", "sent": []})
    sent_key = f"{key[0]}-{key[1]}"
    if state.get("last_sent_key") == sent_key or sent_key in state.get("sent", []):
        print(f"already sent {sent_key}")
        return
    win = WINDOWS[key]
    publish(
        resolve_topic("port", cfg.get("ntfy_topic")),
        f"{cfg.get('title_prefix', '[SEC]')} {win['title']}",
        win["body"],
        priority=4,
        tags=["calendar", "chart_with_upwards_trend"],
        server=resolve_server(cfg.get("ntfy_server")),
    )
    state["last_sent_key"] = sent_key
    state.setdefault("sent", []).append(sent_key)
    state["last_sent_kst"] = now.isoformat(timespec="minutes")
    save_json(STATE_PATH, state)
    print(f"sent {sent_key}")


if __name__ == "__main__":
    main()
