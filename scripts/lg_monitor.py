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
CFG_PATH = ROOT / "configs" / "lg.json"
STATE_PATH = ROOT / "state" / "lg.json"
EVENT_ZONES = {"STOP", "SHOCK", "BUY3", "TARGET1", "TARGET2"}


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
    if price <= bands["stop_close"]:
        return "STOP"
    if price >= bands["target2"]:
        return "TARGET2"
    if price >= bands["target1"]:
        return "TARGET1"
    if bands["buy3_lo"] <= price < bands["buy3_hi"]:
        return "BUY3"
    if bands["buy2_lo"] <= price < bands["buy2_hi"]:
        return "BUY2"
    if bands["buy1_lo"] <= price <= bands["buy1_hi"]:
        return "BUY1"
    if price >= bands["watch"]:
        return "WATCH"
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
    prev = {
        "zone": None,
        "price": None,
        "below_hits": 0,
        "stop_sent": False,
        "shock_sent": False,
        "last_event_ids": [],
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

    stop_line = float(cfg.get("stop_close") or cfg["bands"]["stop_close"])
    need = int(cfg.get("persist_hits") or 2)
    below_hits = int(prev.get("below_hits") or 0)
    if price is not None:
        if price <= stop_line:
            below_hits += 1
        else:
            below_hits = 0

    persist_stop = price is not None and below_hits >= need
    if persist_stop:
        z = "STOP"

    live = active_events(cfg, today)
    live_ids = [e["id"] for e in live]
    new_live = [e for ev in live if ev["id"] not in (prev.get("last_event_ids") or [])]
    new_live = [e for e in live if e["id"] not in (prev.get("last_event_ids") or [])]

    shock = False
    drop_pct = None
    prev_px = prev.get("price")
    shock_need = float(cfg.get("shock_drop_pct") or 7)
    if live and price is not None and prev_px:
        drop_pct = (float(prev_px) - float(price)) / float(prev_px) * 100.0
        if drop_pct >= shock_need:
            shock = True
            z = "SHOCK"

    zone_changed = bool(prev.get("zone")) and prev.get("zone") != z and (
        z in EVENT_ZONES or prev.get("zone") in EVENT_ZONES
    )

    first_persist = persist_stop and not prev.get("stop_sent")
    first_shock = shock and not prev.get("shock_sent")

    px = f"{price:,.0f}" if price is not None else "N/A"
    lines = [
        f"{stamp} | {mode} | {cfg['display']} {cfg['code']}",
        f"가 {px}원 | {z} | 비중상한 {cfg['max_equity_weight_pct']}%",
        cfg["actions"].get(z, ""),
        f"손절 {stop_line:,.0f} | 하회연속 {below_hits}/{need}",
        "매수 11.0~11.3 / 10.5~11.0 / 10.0~10.5만 | 익절 13.0 / 14.5만",
        "조건: 10.0만 하회 지속 또는 자회사 실적 쇼크 → 신규분 정리, 재평가",
    ]
    if drop_pct is not None:
        lines.append(f"직전대비 {drop_pct:+.1f}%")
    if live:
        lines.append("")
        lines.append("[실적 창]")
        for e in live:
            lines.append(f"- {e['label']} {e['start']}~{e['end']}")
            lines.append(f"  {e['check']}")

    send = False
    title = f"{cfg['title_prefix']} {z} {px}"
    if first_persist:
        send = True
        title = f"{cfg['title_prefix']} 손절 10만 하회 지속"
    elif first_shock:
        send = True
        title = f"{cfg['title_prefix']} 자회사 실적 쇼크"
    elif mode == "event":
        if zone_changed or new_live:
            send = True
            title = f"{cfg['title_prefix']} 구간/일정"
        else:
            print("no lg event; silent")
    else:
        send = True
        title = f"{cfg['title_prefix']} 체크"

    STATE_PATH.write_text(
        json.dumps(
            {
                "updated_at": stamp,
                "zone": z,
                "price": price,
                "below_hits": below_hits,
                "stop_sent": bool(persist_stop),
                "shock_sent": bool(shock),
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
        if persist_stop:
            pri = 5
        elif shock or zone_changed or new_live:
            pri = 4
        else:
            pri = 3
        ntfy_publish(server, topic, title, msg, pri, "warning,chart_with_downwards_trend")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
