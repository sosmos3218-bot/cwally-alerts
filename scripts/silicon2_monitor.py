#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "silicon2.json"
STATE_PATH = ROOT / "state" / "silicon2.json"
EVENT_ZONES = {"BUY1", "BUY2", "TRIM1", "TRIM2", "STOP"}


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
        {
            "topic": topic,
            "title": title,
            "message": message,
            "priority": priority,
            "tags": tags.split(","),
        }
    ).encode()
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
    topic = os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic") or "cwally-port"
    server = os.environ.get("NTFY_SERVER") or cfg.get("ntfy_server") or "https://ntfy.sh"
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

    px = f"{price:,.0f}" if price is not None else "N/A"
    lines = [
        f"{stamp} | {mode} | {cfg['display']} {cfg['code']}",
        f"가 {px}원 | {z} | 비중상한 {cfg['max_equity_weight_pct']}%",
        cfg["actions"].get(z, ""),
        "",
        "매수 42,000~45,000 / 38,000~40,000 | 익절 52,000 / 56,000",
        "손절 종가 36,000 | OPM 15% 미만 시 테제 종료",
    ]
    if live:
        lines.append("")
        lines.append("[3Q 체크]")
        for e in live:
            lines.append(f"- {e['label']} {e['start']}~{e['end']}")
            lines.append(f"  {e['check']}")

    send = False
    title = f"{cfg['title_prefix']} {z} {px}"
    if mode == "event":
        if zone_changed or new_live:
            send = True
            title = f"{cfg['title_prefix']} 구간/일정"
        else:
            print("no silicon2 event; silent")
    else:
        send = True
        title = f"{cfg['title_prefix']} 주간 체크" if now.weekday() == 0 else f"{cfg['title_prefix']} {z}"

    STATE_PATH.write_text(
        json.dumps(
            {"updated_at": stamp, "zone": z, "price": price, "last_event_ids": live_ids},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
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
