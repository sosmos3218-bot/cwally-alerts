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
CFG_PATH = ROOT / "configs" / "cover0198.json"
STATE_PATH = ROOT / "state" / "cover0198.json"
EVENT_ZONES = {"STOP20", "HOLD10", "TRIM12", "TRIM20", "TRIM35"}


def yahoo_chart(ticker: str):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(ticker)
        + "?interval=1d&range=1mo"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 cwally-alerts"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode())
    result = payload["chart"]["result"][0]
    meta = result.get("meta", {})
    price = meta.get("regularMarketPrice") or meta.get("chartPreviousClose") or meta.get("previousClose")
    closes = [x for x in (result.get("indicators", {}).get("quote", [{}])[0].get("close") or []) if x is not None]
    ret20 = None
    if len(closes) > 20:
        ret20 = closes[-1] / closes[-21] - 1
    return (float(price) if price is not None else None), ret20


def zone(price, bands):
    if price is None:
        return "DATA_FAIL"
    if price <= bands["stop20"]:
        return "STOP20"
    if price <= bands["hold10"]:
        return "HOLD10"
    if price >= bands["trim35"]:
        return "TRIM35"
    if price >= bands["trim20"]:
        return "TRIM20"
    if price >= bands["trim12"]:
        return "TRIM12"
    return "WATCH"


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

    price = None
    ret20 = None
    bench20 = None
    try:
        price, ret20 = yahoo_chart(cfg["ticker"])
    except Exception as exc:
        print(f"price fetch failed: {exc}", file=sys.stderr)
    try:
        _, bench20 = yahoo_chart(cfg["bench_ticker"])
    except Exception as exc:
        print(f"bench fetch failed: {exc}", file=sys.stderr)

    z = zone(price, cfg["bands"])
    if z == "DATA_FAIL" and prev.get("zone"):
        z = prev["zone"]
        price = prev.get("price")

    live = [ev for ev in cfg.get("events", []) if ev["start"] <= today <= ev["end"]]
    live_ids = [e["id"] for e in live]
    new_live = [e for e in live if e["id"] not in (prev.get("last_event_ids") or [])]
    zone_changed = bool(prev.get("zone")) and prev.get("zone") != z and (
        z in EVENT_ZONES or prev.get("zone") in EVENT_ZONES
    )

    lag = None
    if ret20 is not None and bench20 is not None:
        lag = ret20 - bench20

    px = f"{price:,.0f}" if price is not None else "N/A"
    cost = cfg["cost"]
    pnl = f"{(price / cost - 1) * 100:+.1f}%" if price else "n/a"
    lines = [
        f"{stamp} | {mode} | {cfg['display']} {cfg['code']}",
        f"가 {px}원 | 원가 {cost:,} | {pnl} | {z}",
        f"1차 {cfg['shares_phase1']}주 | 15:20 KST 점검",
        cfg["actions"].get(z, ""),
        f"코스닥150 20일 {bench20 * 100:+.1f}% | ETF 20일 {ret20 * 100:+.1f}% | 상대 {lag * 100:+.1f}p"
        if ret20 is not None and bench20 is not None and lag is not None
        else "상대수익 n/a",
        "",
        "손절·폐기: 6,840 50% 검토 / 품질악화 2개면 전량",
        "시간매도: 2026-12 분배점검 / 2027-06 절반검토 / 2028-06 교체판단",
    ]
    if live:
        lines.append("")
        lines.append("[시간 매도]")
        for e in live:
            lines.append(f"- {e['label']} {e['start']}~{e['end']}")
            lines.append(f"  {e['check']}")
    if lag is not None and lag <= -0.08 and bench20 is not None and bench20 >= 0.12:
        lines.append("")
        lines.append("상대 부진: 급등장 미추종. 30% 순수 코스닥150으로 이동 검토.")

    send = False
    title = f"{cfg['title_prefix']} {z} {px}"
    if mode == "event":
        if zone_changed or new_live:
            send = True
            title = f"{cfg['title_prefix']} 매도점검"
        else:
            print("no cover0198 event; silent")
    else:
        send = True
        title = f"{cfg['title_prefix']} 점검 {z}"

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
        pri = 5 if z == "STOP20" or new_live else (4 if z in EVENT_ZONES or zone_changed else 3)
        ntfy_publish(server, topic, title, msg, pri, "warning,chart_with_downwards_trend")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
