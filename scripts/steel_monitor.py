#!/usr/bin/env python3
from __future__ import annotations
import json, os, sys, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "state" / "steel_zones.json"
KEYS = ("POSCO", "HYUNDAI", "POONGSAN")
EVENT_ZONES = {"BUY2", "BUY3", "REVIEW", "TARGET1", "TARGET2", "STRETCH"}
ACTIONS = {"BUY1": "1차 40% 매수 구간", "BUY2": "2차 35% 추가 매수", "BUY3": "3차 25% 추가 매수", "REVIEW": "비중 축소/재검토 구간", "TARGET1": "1차 익절(현대·풍산 일부 정리)", "TARGET2": "2차 익절(전체 40~50% 축소)", "STRETCH": "스트레치 구간. 추가 정리 검토", "ABOVE_BUY1": "1차 상단. 추격 금지", "BETWEEN": "내려온 구간. 현금 유지", "DATA_FAIL": "시세 조회 실패"}

def yahoo_price(ticker):
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(ticker) + "?interval=1d&range=5d"
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
    if price is None: return "DATA_FAIL"
    if price <= bands["review"]: return "REVIEW"
    lo3, hi3 = bands["buy3"]; lo2, hi2 = bands["buy2"]; lo1, hi1 = bands["buy1"]
    if lo3 <= price <= hi3: return "BUY3"
    if lo2 <= price <= hi2: return "BUY2"
    if lo1 <= price <= hi1: return "BUY1"
    if price >= bands["stretch"]: return "STRETCH"
    if price >= bands["target2"]: return "TARGET2"
    if price >= bands["target1"]: return "TARGET1"
    if price > hi1: return "ABOVE_BUY1"
    return "BETWEEN"

def ntfy_publish(server, topic, title, message, priority, tags):
    payload = json.dumps({"topic": topic, "title": title, "message": message, "priority": priority, "tags": tags.split(",")}).encode()
    req = urllib.request.Request(server.rstrip("/"), data=payload, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(resp.read().decode())

def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "weekly").lower()
    cfg = json.loads((ROOT / "configs" / "steel.json").read_text(encoding="utf-8"))
    topic = os.environ.get("NTFY_TOPIC") or "cwally-port"
    server = os.environ.get("NTFY_SERVER") or "https://ntfy.sh"
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    prev = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {"zones": {}, "prices": {}}
    prev_zones = prev.get("zones", {})
    rows, new_zones, new_prices, changes = [], {}, {}, []
    for key in KEYS:
        meta, bands = cfg["weights"][key], cfg["bands"][key]
        price = yahoo_price(meta["ticker"])
        z = zone(price, bands)
        rows.append((key, meta, bands, price, z))
        if z == "DATA_FAIL" and key in prev_zones:
            new_zones[key] = prev_zones[key]; new_prices[key] = prev.get("prices", {}).get(key); continue
        new_zones[key] = z; new_prices[key] = price
        old = prev_zones.get(key)
        if old and old != z and (z in EVENT_ZONES or old in EVENT_ZONES):
            kind = "진입" if z in EVENT_ZONES and old not in EVENT_ZONES else ("이탈" if old in EVENT_ZONES and z not in EVENT_ZONES else "이동")
            changes.append((meta["name"], old, z, kind))
    STATE_PATH.write_text(json.dumps({"updated_at": now, "zones": new_zones, "prices": new_prices}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [f"{now} | {mode} | 철강58 60/25/15", ""]
    for key, meta, bands, price, z in rows:
        px = f"{price:,.0f}" if price is not None else "N/A"
        lines.append(f"- {meta['name']} {meta['weight_pct']}% | {px}원 | {z} | {ACTIONS.get(z,'')} | TP2 {bands['target2']:,}")
    title = "[철강58] 주간 체크"
    if mode == "event":
        if not prev_zones or not changes:
            print("no steel event; silent"); return 0
        lines += ["", "구간 변화:"] + [f"- [{k}] {n}: {o} → {z}" for n,o,z,k in changes]
        title = "[철강58] 구간 변화"
    elif mode == "quarterly":
        title = "[철강58] 분기 리밸런스"
        lines += ["", "스프레드/리튬/판가/탄약수주 확인. +40% 종목 원비중 복원."]
    msg = "\n".join(lines)
    print(msg)
    pri = 5 if any(z in {"REVIEW","BUY3"} for *_, z in rows) else (4 if mode != "weekly" else 3)
    ntfy_publish(server, topic, title, msg, pri, "factory,chart_with_upwards_trend")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
