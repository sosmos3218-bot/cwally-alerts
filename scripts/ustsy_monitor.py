#!/usr/bin/env python3
from __future__ import annotations
import json, os, sys, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "ustsy.json"
STATE_PATH = ROOT / "state" / "ustsy.json"

MEMO = """2026-09-16 | 2Y US30

기본 70:30
- 484790 KODEX 미국30년국채액티브(H) 70% (밴드 60-80)
- 476760 ACE 미국30년국채액티브 30% (밴드 20-40)
구축 2-4주 3회 분할. 현금 0.

환율(우선)
- <=1330 : 476760 35-40%
- 1360-1430 : 70:30 유지
- 1450-1510 : 476760 25%
- >=1520 : 476760 20%
- 1300 하향 추세 : 476760 <=20% 재검토

조정
- 매일 금지. 이탈분 절반만.
- 476760 <20 or >40 시 즉시, 그 외 분기 1회.
- 금리로 둘 비중 갈이지 말 것.

금지
- FOMC/뉴스/월배당 매매, 3개월 손익 교체
- 453850 / 458250 / 472870 코어
- 레버리지·커버드콜 추가

위성
- 458250 스트립 : 기본 0%. 미 30년 5.50% 위 고점 후 20거래일 고점 하락 + 유가·인상베팅 약화 시 최대 10%(484790에서만). 5.80% 재경신 시 0%.
- 472870 엔화 : 0%. 편입 조건 없음.

2년 / 손절 없음. -15% 예정 변동.
청산 : 단기자금 변경, 원달러 1300 하향 추세 확정."""


def ntfy_publish(server: str, topic: str, title: str, message: str, priority: int, tags: str) -> None:
    payload = json.dumps({
        "topic": topic,
        "title": title,
        "message": message,
        "priority": priority,
        "tags": tags.split(","),
    }).encode()
    req = urllib.request.Request(
        server.rstrip("/"),
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(resp.read().decode())


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "memo").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    topic = os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic") or "cwally-port"
    server = os.environ.get("NTFY_SERVER") or cfg.get("ntfy_server") or "https://ntfy.sh"
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    prev = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}

    if mode == "event" and prev.get("sent_memo"):
        print("ustsy event already sent; silent")
        return 0

    title = f"{cfg.get('title_prefix', '[USTSY]')} 2Y 70:30"
    msg = f"{now} | {mode}\n\n{MEMO}"
    print(msg)
    ntfy_publish(server, topic, title, msg, 4, "memo,us")

    STATE_PATH.write_text(
        json.dumps({"updated_at": now, "last_mode": mode, "sent_memo": True}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
