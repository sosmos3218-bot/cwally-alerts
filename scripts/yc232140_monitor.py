#!/usr/bin/env python3
"""YC(232140) buy-zone watch. Weekday 15:20 KST slot. Silent unless zone changes."""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from ntfy_util import route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "yc232140.json"
STATE_PATH = ROOT / "state" / "yc232140.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return dict(fallback)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        fallback.update(data)
        return fallback
    except json.JSONDecodeError:
        return fallback


def http_get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def num(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "").replace("%", "").strip()
    if not text or text in {"-", "N/A"}:
        return None
    sign = -1 if text.startswith("-") else 1
    text = text.lstrip("+-")
    try:
        return sign * float(text)
    except ValueError:
        return None


def quote(symbol: str) -> dict:
    payload = json.loads(http_get(f"https://m.stock.naver.com/api/stock/{symbol}/basic"))
    price = num(payload.get("closePrice"))
    if price is None:
        raise ValueError(f"no price {symbol}")
    return {
        "price": price,
        "chg": num(payload.get("compareToPreviousClosePrice")),
        "pct": num(payload.get("fluctuationsRatio")),
        "name": payload.get("stockName") or symbol,
    }


def publish(topic: str, title: str, message: str, priority: int) -> None:
    if topic not in {"cwally-port", "cwally-ops"}:
        topic = "cwally-port"
    payload = json.dumps(
        {
            "topic": route_topic(topic, title),
            "title": title,
            "message": message,
            "priority": priority,
            "tags": ["chart_with_upwards_trend"],
        }
    ).encode()
    req = urllib.request.Request(
        "https://ntfy.sh",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(resp.read().decode())


def write_state(prev: dict, **updates) -> None:
    prev.update(updates)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(prev, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def zone_of(price: float, bands: dict) -> str:
    if price <= float(bands["kill"]):
        return "KILL"
    if price <= float(bands["zone3"]):
        return "ZONE3"
    if price <= float(bands["zone2"]):
        return "ZONE2"
    if price <= float(bands["zone1"]):
        return "ZONE1"
    if price > float(bands["no_chase"]):
        return "NO_CHASE"
    return "WAIT"


def main() -> int:
    dry = "--dry-run" in sys.argv
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    if now.year != int(cfg.get("year_guard") or 2026) or today > cfg.get("valid_until", "2026-11-20"):
        print("year guard or expired; silent")
        return 0

    prev = load_json(
        STATE_PATH,
        {"updated_at": None, "last_zone": None, "last_fail_date": None, "last_action": None, "price": None, "errors": []},
    )
    try:
        row = quote(cfg["code"])
    except Exception as exc:
        if prev.get("last_fail_date") != today and not dry:
            publish(
                "cwally-ops",
                "[232140] 와이씨 조회 실패 | 재실행 필요",
                "\n".join([
                    f"{stamp} | 와이씨 232140",
                    "이게 뭐 알림: 시세 조회가 실패했습니다. 조건 미충족이 아닙니다.",
                    f"지금 상태: {exc}",
                    "지금 할 일: 다음 평일 15:20 재실행을 기다립니다.",
                    "나중에: 시세가 오면 구간을 다시 봅니다.",
                    "익절/줄일 때: 해당 없음.",
                    "하지 말 것: 실패를 관망으로 보지 말 것.",
                ]),
                4,
            )
        write_state(prev, updated_at=stamp, last_fail_date=today, last_action="DATA_FAIL", errors=[str(exc)])
        print("price fetch failed", exc)
        return 1

    price = row["price"]
    bands = cfg["bands"]
    zone = zone_of(price, bands)
    old = prev.get("last_zone")
    write_state(prev, updated_at=stamp, price=price, last_zone=zone, last_action="ALERT" if zone != old and zone != "WAIT" else "WATCH", errors=[])
    if zone == old or zone == "WAIT":
        print(f"no zone change; silent price={price:.0f} zone={zone}")
        return 0

    px = f"{price:,.0f}원"
    weights = cfg["weights"]
    if zone == "KILL":
        title = "[232140] 와이씨 11,200 이탈 | 매수 중단"
        action = "11,200원 이하입니다. 분할 매수를 중단하고 재검토합니다."
        priority = 4
    elif zone == "ZONE3":
        title = "[232140] 와이씨 12,400 도달 | 3차 25%"
        action = f"3차 구간입니다. 의도 비중의 {weights['zone3']}%만 봅니다."
        priority = 4
    elif zone == "ZONE2":
        title = "[232140] 와이씨 14,200 도달 | 2차 35%"
        action = f"2차 구간입니다. 의도 비중의 {weights['zone2']}%만 봅니다."
        priority = 3
    elif zone == "ZONE1":
        title = "[232140] 와이씨 15,500 도달 | 1차 40%"
        action = f"1차 구간입니다. 의도 비중의 {weights['zone1']}%만 봅니다. 17,300원 위 추격은 하지 않습니다."
        priority = 3
    else:
        title = "[232140] 와이씨 17,300 초과 | 추격 금지"
        action = "17,300원을 넘었습니다. 사지 않습니다."
        priority = 3
    lines = [
        f"{stamp} | 와이씨 232140 분할 구간",
        "이게 뭐 알림: 삼성 테스터 계약 기반 분할 매수 구간입니다. 15:20 가격이라 확정 종가가 아닙니다.",
        f"지금 상태: {px} | 구간 {zone} | 기준 16,890원(10/2)",
        f"지금 할 일: {action}",
        "나중에: 3분기 매출 1,300억 초과면 1차를 앞당기고, 1,050억 미달이면 인도 미확인으로 중단합니다. 실적은 이 알림이 조회하지 않습니다.",
        "익절/줄일 때: 11,200원 이탈이면 분할을 중단합니다. 목표가 평균 21,375원은 참고지 익절 지시가 아닙니다.",
        "하지 말 것: 17,300원 위 추격, 신용, 미수. 15:20 가격을 확정 종가로 보기. 계약 190억을 이미 반영된 서프라이즈로 보기.",
    ]
    msg = "\n".join(lines)
    print(msg)
    if not dry:
        publish("cwally-port", title, msg, priority)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
