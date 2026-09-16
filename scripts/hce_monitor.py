#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "hce.json"
STATE_PATH = ROOT / "state" / "hce.json"
EVENT_ZONES = {"BUY1", "BUY2", "BUY3", "TRIM1", "TRIM2", "REVIEW"}
ZONE_KO = {
    "BUY1": "1차 매수 구간 (12.8~13.2만)",
    "BUY2": "2차 매수 구간 (11.8~12.2만)",
    "BUY3": "3차 소량 구간 (10.8~11.2만)",
    "OBSERVE": "관측 구간 (13.8~14.5만)",
    "TRIM1": "1차 익절 구간 (17.5~18.5만)",
    "TRIM2": "2차 익절 구간 (20만 이상)",
    "REVIEW": "줄일 재검토 (12만 아래)",
    "ABOVE": "매수가 위. 추격 금지",
    "BETWEEN": "매수·익절 사이. 현금 유지",
    "DATA_FAIL": "시세 조회 실패",
}
DO_NOW = {
    "BUY1": "1차만 사고, 남은 1/3은 남겼두세요.",
    "BUY2": "실적·군산 논거가 그대로면 2차를 사세요.",
    "BUY3": "극소량만. 3분기 낙제와 군산 지연이 같이 있으면 사지 말세요.",
    "OBSERVE": "14만 전후에서 한번에 다 사지 말세요.",
    "TRIM1": "보유의 20~30%를 팔아 익절하세요.",
    "TRIM2": "보유의 30~50%를 더 줄이세요.",
    "REVIEW": "단기는 줄이고, 중기 논거(실적·군산)를 다시 보세요.",
    "ABOVE": "실적·군산 확인 전에 비중을 늘리지 말세요.",
    "BETWEEN": "내려온 구간입니다. 현금을 유지하세요.",
    "DATA_FAIL": "시세를 다시 확인하세요.",
}
TITLE_BY_ZONE = {
    "REVIEW": "12만 하회 재검토",
    "BUY3": "3차 극소량만",
    "BUY2": "2차 추가 검토",
    "BUY1": "1차만 매수",
    "TRIM1": "20~30% 익절",
    "TRIM2": "30~50% 익절",
    "OBSERVE": "14만 전후 추격 금지",
    "ABOVE": "추격 금지",
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
        meta = payload["chart"]["result"][0].get("meta", {})
        price = meta.get("regularMarketPrice") or meta.get("previousClose")
        return float(price) if price is not None else None
    except Exception as exc:
        print(f"price fetch failed {ticker}: {exc}", file=sys.stderr)
        return None


def zone(price, bands):
    if price is None:
        return "DATA_FAIL"
    if price <= bands["review"]:
        return "REVIEW"
    lo3, hi3 = bands["buy3"]
    lo2, hi2 = bands["buy2"]
    lo1, hi1 = bands["buy1"]
    t1lo, t1hi = bands["trim1"]
    t2lo, t2hi = bands["trim2"]
    olo, ohi = bands["observe"]
    if t2lo <= price:
        return "TRIM2"
    if t1lo <= price <= t1hi or (t1hi < price < t2lo):
        return "TRIM1" if price <= t1hi else "ABOVE"
    if lo3 <= price <= hi3:
        return "BUY3"
    if lo2 <= price <= hi2:
        return "BUY2"
    if lo1 <= price <= hi1:
        return "BUY1"
    if olo <= price <= ohi:
        return "OBSERVE"
    if price > ohi:
        return "ABOVE"
    return "BETWEEN"


def active_events(cfg, today):
    return [ev for ev in cfg.get("events", []) if ev["start"] <= today <= ev["end"]]


def upcoming_events(cfg, today, days=21):
    lim = (datetime.strptime(today, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")
    return [ev for ev in cfg.get("events", []) if today < ev["start"] <= lim]


def ntfy_publish(server, topic, title, message, priority, tags):
    payload = json.dumps(
        {"topic": topic, "title": title, "message": message, "priority": priority, "tags": tags.split(",")}
    ).encode()
    req = urllib.request.Request(server.rstrip("/"), data=payload, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(resp.read().decode())


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "daily").lower()
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
    soon = upcoming_events(cfg, today)
    live_ids = [e["id"] for e in live]
    new_live = [e for e in live if e["id"] not in (prev.get("last_event_ids") or [])]
    zone_changed = prev.get("zone") and prev.get("zone") != z and (
        z in EVENT_ZONES or prev.get("zone") in EVENT_ZONES
    )

    send = True
    if mode == "event" and not (zone_changed or new_live):
        print("no hce event; silent")
        send = False

    px = f"{price:,.0f}원" if price is not None else "시세 없음"
    cap = cfg["max_equity_weight_pct"]
    lines = [
        f"{stamp} | HCE | {cfg['display']}",
        f"이게 뭐 알림: HD현대건설기계({cfg['code']}) 매매 가이드입니다. 주식 한도는 총자산 {cap}%입니다.",
        f"지금 상태: {px} | {ZONE_KO.get(z, z)}",
        f"지금 할 일: {DO_NOW.get(z, '')}",
        "나중에: 매수는 13.0 / 12.0 / 11.0만 근처에서만. 실적·군산 일정을 보고 다음 트랜차를 정하세요.",
        "익절: 17.5~18.5만에서 20~30%, 20~21.5만에서 30~50% 줄입니다.",
        "줄일 때: 종가 12만 하회, 또는 가이던스 하향+군산 봉괴+관세 15% 파괴 중 2개 이상이면 전량 검토.",
        f"하지 말 것: 추격매수, 신용/미수, 총자산 {cap}% 초과. 군산 완공만 보고 비중 확대 금지.",
    ]
    if live:
        lines.append("")
        lines.append("지금 진행 중인 일정:")
        for e in live:
            lines.append(f"- {e['label']} {e['start']}~{e['end']}: {e['check']}")
    if soon:
        lines.append("")
        lines.append("21일 안 일정:")
        for e in soon:
            lines.append(f"- {e['label']} {e['start']}")

    STATE_PATH.write_text(
        json.dumps({"updated_at": stamp, "zone": z, "price": price, "last_event_ids": live_ids}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    msg = "\n".join(lines)
    print(msg)
    if send:
        title = f"[HCE] {TITLE_BY_ZONE.get(z, z)}"
        if zone_changed:
            title = f"[HCE] 구간 변화 | {TITLE_BY_ZONE.get(z, z)}"
        elif new_live:
            title = f"[HCE] 일정 창 | {TITLE_BY_ZONE.get(z, z)}"
        elif mode != "event" and now.weekday() == 0:
            title = f"[HCE] 주간 체크 | {TITLE_BY_ZONE.get(z, z)}"
        pri = 5 if z in {"REVIEW", "BUY3", "TRIM2"} or new_live else (4 if zone_changed else 3)
        ntfy_publish(server, topic, title, msg, pri, "tractor,chart_with_upwards_trend")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
