#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from ntfy_util import route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "silicon2.json"
STATE_PATH = ROOT / "state" / "silicon2.json"
EVENT_ZONES = {"BUY1", "BUY2", "TRIM1", "TRIM2", "STOP"}
ZONE_KO = {
    "BUY1": "1차 매수 (42,000~45,000원)",
    "BUY2": "2차 매수 (38,000~40,000원)",
    "TRIM1": "1차 익절 (52,000원)",
    "TRIM2": "2차 익절 (56,000원)",
    "STOP": "손절 (종가 36,000원 이탈)",
    "CHASE": "48,000원 위. 추격 금지",
    "BETWEEN": "매수·익절 사이. 현금 유지",
    "DATA_FAIL": "시세 조회 실패",
}
DO_NOW = {
    "BUY1": "1차만 사세요. 48,000원 위에서 따라잡지 마세요.",
    "BUY2": "2차를 더 사세요. 이 구간 아래에서만 가산.",
    "TRIM1": "보유의 절반을 팔아 익절하세요.",
    "TRIM2": "남은 수량을 익절하세요. 59,000원 추격 재진입 금지.",
    "STOP": "손절하고 이 테제는 끝냅니다. 다시 사지 마세요.",
    "CHASE": "새로 사지 마세요.",
    "BETWEEN": "현금을 유지하세요.",
    "DATA_FAIL": "시세를 다시 확인하세요.",
}
TITLE_BY_ZONE = {
    "STOP": "손절 36,000 이탈",
    "BUY2": "2차 추가",
    "BUY1": "1차만 매수",
    "TRIM1": "52,000 절반 익절",
    "TRIM2": "56,000 잔량 익절",
    "CHASE": "48,000 위 추격 금지",
    "BETWEEN": "현금 유지",
    "DATA_FAIL": "시세 확인",
}


def yahoo_price(ticker: str):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(ticker)
        + "?interval=1d&range=5d"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 cwally-alerts"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode())
        result = payload["chart"]["result"][0]
        meta = result.get("meta", {})
        price = meta.get("regularMarketPrice") or meta.get("chartPreviousClose") or meta.get("previousClose")
        return float(price) if price is not None else None
    except Exception as exc:
        print(f"price fetch failed {ticker}: {exc}", file=sys.stderr)
        return None


def zone(price, bands):
    if price is None:
        return "DATA_FAIL"
    if price <= bands["stop_close"]:
        return "STOP"
    lo2, hi2 = bands["buy2"]
    lo1, hi1 = bands["buy1"]
    if price >= bands["trim2"]:
        return "TRIM2"
    if price >= bands["trim1"]:
        return "TRIM1"
    if price >= bands["chase_ban"]:
        return "CHASE"
    if lo1 <= price <= hi1:
        return "BUY1"
    if lo2 <= price <= hi2:
        return "BUY2"
    return "BETWEEN"


def active_events(cfg, today):
    return [ev for ev in cfg.get("events", []) if ev["start"] <= today <= ev["end"]]


def ntfy_publish(server, topic, title, message, priority, tags):
    payload = json.dumps(
        {"topic": route_topic(topic, title), "title": title, "message": message, "priority": priority, "tags": tags.split(",")}
    ).encode()
    if __import__("ntfy_util").dup_guard_skip_payload(server, payload):  # 하루 한 번 발송 보호 (같은 제목 오늘 이미 발송)
        return
    req = urllib.request.Request(server.rstrip("/"), data=payload, method="POST", headers={"Content-Type": "application/json"})
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
    prev = {"zone": None, "price": None, "last_event_ids": [], "updated_at": None}
    if STATE_PATH.exists():
        try:
            prev.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass

    price = yahoo_price(cfg["ticker"])
    z = zone(price, cfg["bands"])
    if z == "DATA_FAIL" and prev.get("zone"):
        z = prev["zone"]
        price = prev.get("price")

    live = active_events(cfg, today)
    live_ids = [e["id"] for e in live]
    new_live = [e for e in live if e["id"] not in (prev.get("last_event_ids") or [])]
    zone_changed = bool(prev.get("zone")) and prev.get("zone") != z and (
        z in EVENT_ZONES or prev.get("zone") in EVENT_ZONES
    )

    send = False
    title = f"[SILICON2] 257720 {TITLE_BY_ZONE.get(z, z)}"
    if mode == "event":
        if zone_changed or new_live:
            send = True
            title = f"[SILICON2] 257720 구간/일정 | {TITLE_BY_ZONE.get(z, z)}"
        else:
            print("no silicon2 event; silent")
    else:
        send = True
        title = f"[SILICON2] 257720 주간 체크 | {TITLE_BY_ZONE.get(z, z)}" if now.weekday() == 0 else f"[SILICON2] 257720 {TITLE_BY_ZONE.get(z, z)}"

    px = f"{price:,.0f}원" if price is not None else "시세 없음"
    cap = cfg["max_equity_weight_pct"]
    lines = [
        f"{stamp} | SILICON2 | {cfg['display']} {cfg['code']}",
        f"이게 뭐 알림: 실리콘투 스윙(약 12주) 매매 가이드입니다. 주식 한도는 총자산 {cap}%입니다.",
        f"지금 상태: {px} | {ZONE_KO.get(z, z)}",
        f"지금 할 일: {DO_NOW.get(z, '')}",
        "나중에: 2차는 38,000~40,000원에서만. 11월 3분기 체크를 보고 연장 여부를 결정하세요.",
        "익절: 52,000원에서 절반, 56,000원에서 남은 양.",
        "줄일 때: 종가 36,000원 이탈, 또는 분기 영업이익률 15% 미만, 또는 유럽매출·총이익률·재고일수 중 2개 악화.",
        f"하지 말 것: 48,000원 위 추격매수, 59,000원 추격 재진입, 신용/미수, 총자산 {cap}% 초과.",
    ]
    if live:
        lines.append("")
        lines.append("3분기 체크:")
        for e in live:
            lines.append(f"- {e['label']} {e['start']}~{e['end']}: {e['check']}")

    STATE_PATH.write_text(
        json.dumps({"updated_at": stamp, "zone": z, "price": price, "last_event_ids": live_ids}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    msg = "\n".join(lines)
    print(msg)
    if send:
        pri = 5 if z in {"STOP", "BUY2", "TRIM2"} or new_live else (4 if zone_changed else 3)
        ntfy_publish(server, topic, title, msg, pri, "lipstick,chart_with_upwards_trend")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
