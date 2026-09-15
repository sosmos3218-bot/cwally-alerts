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
CFG_PATH = ROOT / "configs" / "hce.json"
STATE_PATH = ROOT / "state" / "hce.json"
EVENT_ZONES = {"BUY1", "BUY2", "BUY3", "TRIM1", "TRIM2", "REVIEW"}


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
    out = []
    for ev in cfg.get("events", []):
        if ev["start"] <= today <= ev["end"]:
            out.append(ev)
    return out


def upcoming_events(cfg, today, days=21):
    lim = (datetime.strptime(today, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")
    out = []
    for ev in cfg.get("events", []):
        if today < ev["start"] <= lim:
            out.append(ev)
    return out


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
    mode = (sys.argv[1] if len(sys.argv) > 1 else "daily").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    topic = os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic") or "cwally-port"
    server = os.environ.get("NTFY_SERVER") or cfg.get("ntfy_server") or "https://ntfy.sh"
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    prev = {
        "zone": None,
        "price": None,
        "last_event_ids": [],
        "updated_at": None,
    }
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

    px = f"{price:,.0f}" if price is not None else "N/A"
    act = cfg["actions"].get(z, "")
    lines = [
        f"{stamp} | {mode} | {cfg['display']} {cfg['code']}",
        f"가 {px}원 | {z} | 비중상한 {cfg['max_equity_weight_pct']}%",
        act,
        "",
        f"매수 13.0 / 12.0 / 11.0만 | 익절 17.5~18.5 / 20~21.5만",
        "전량: 가이던스 하향 + 군산 붕괴 + 관세 15% 파괴 중 2개+",
    ]
    if live:
        lines.append("")
        lines.append("[D-window]")
        for e in live:
            lines.append(f"- {e['label']} {e['start']}~{e['end']}")
            lines.append(f"  {e['check']}")
    if soon:
        lines.append("")
        lines.append("[21일 내]")
        for e in soon:
            lines.append(f"- {e['label']} {e['start']}")

    send = False
    title = f"{cfg['title_prefix']} {z} {px}"
    if mode == "event":
        if zone_changed or new_live:
            send = True
            title = f"{cfg['title_prefix']} 구간/일정"
        else:
            print("no hce event; silent")
    else:
        send = True
        if now.weekday() == 0:
            title = f"{cfg['title_prefix']} 주간 체크"
        else:
            title = f"{cfg['title_prefix']} {z}"

    STATE_PATH.write_text(
        json.dumps(
            {
                "updated_at": stamp,
                "zone": z,
                "price": price,
                "last_event_ids": live_ids,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    msg = "\n".join(lines)
    print(msg)
    if send:
        pri = 5 if z in {"REVIEW", "BUY3", "TRIM2"} or new_live else (4 if zone_changed else 3)
        ntfy_publish(server, topic, title, msg, pri, "tractor,chart_with_upwards_trend")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
