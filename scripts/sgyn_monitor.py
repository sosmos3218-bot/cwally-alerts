#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "sgyn.json"
STATE_PATH = ROOT / "state" / "sgyn.json"


def ntfy_publish(server: str, topic: str, title: str, message: str, priority: int, tags: str) -> None:
    payload = json.dumps(
        {
            "topic": topic,
            "title": title,
            "message": message,
            "priority": priority,
            "tags": tags.split(","),
        }
    ).encode()
    import urllib.request

    req = urllib.request.Request(
        server.rstrip("/"),
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(resp.read().decode())


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "event").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    topic = "cwally-port"
    server = "https://ntfy.sh"
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    force = os.environ.get("FORCE_ALERT") == "1"

    prev = {"updated_at": None, "last_mode": None, "last_sent_date": None, "baseline_sent": False}
    if STATE_PATH.exists():
        try:
            prev.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass

    thursday = now.weekday() == 3
    first = not prev.get("baseline_sent")
    send = False
    kind = "silent"
    if force or first:
        send = True
        kind = "기준"
    elif mode == "weekly" or (mode == "event" and thursday):
        if prev.get("last_sent_date") != today:
            send = True
            kind = "주간"

    regions = cfg["regions"]
    th = cfg["thresholds"]
    order = " → ".join(regions[k]["label"] for k in cfg["break_order"])
    if kind == "기준":
        title = "[SGYN] 12개월 유지 체크 | 추격 금지"
    else:
        title = "[SGYN] 동탄부터 확인 | 추격 금지"

    lines = [
        f"{stamp} | SGYN | 경기남부 4권역",
        "이게 뭐 알림: 동탄·광교웰빙·광교본체·수지 집값 유지가 깨지는지 보는 주 1회 체크리스트입니다.",
        f"지금 상태: 깨질 순서 {order}. 기본값은 동탄 조정, 나머지 3곳은 거래 감소+가격 방어.",
        "지금 할 일: 아래 5개를 권역별로 보고, 2개월 연속 깨질 선이 나온 권역만 조정으로 보세요.",
        f"- 토허 신청 {th['permit_break_pct']}% / 2개월",
        f"- 생애최초 건수 {th['first_break_pct']}% 또는 비중 {th['first_share_break_pp']}%p",
        f"- 30대 비중 깨질: 동탄 {regions['dongtan']['share30_break']}% / 웰빙 {regions['wellbeing']['share30_break']}% / 본체 {regions['gwanggyo']['share30_break']}% / 수지 {regions['suji']['share30_break']}%",
        f"- 전세 3개월 {th['jeonse_break_3m_pct']}%",
        f"- 정보통신 30대 고용 YoY {th['ict30_break_yoy_pct']}% (2분기). 동탄은 반도체 성과급이 먼저.",
        "나중에: 다음 주 목요일 15:20. 깨질 선이 없으면 추격하지 말고 유지로 봅니다.",
        "익절/줄일 때: 동탄 토허·30대가 먼저 깨지면 동탄 국평 선. 웰빙타운은 구축·저층. 광교 본체·수지는 호가 공백이 먼저.",
        "하지 말 것: 네 권역을 같은 시장으로 묶기. 웰빙타운을 광교 대장주와 같이 추격. 특례대출·전세가 같이 깨질 전에 서울 소형 프레임 사기.",
    ]

    STATE_PATH.write_text(
        json.dumps(
            {
                "updated_at": stamp,
                "last_mode": mode,
                "last_sent_date": today if send else prev.get("last_sent_date"),
                "baseline_sent": True if send else bool(prev.get("baseline_sent")),
                "kind": kind if send else "silent",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    msg = "\n".join(lines)
    print(msg)
    if not send:
        print("sgyn silent")
        return 0
    ntfy_publish(server, topic, title, msg, 4 if kind == "기준" else 3, "house,warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
