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
CFG_PATH = ROOT / "configs" / "0220wl.json"
STATE_PATH = ROOT / "state" / "0220wl.json"
EVENT_ZONES = {"STOP20", "HOLD10", "TRIM12", "TRIM20", "TRIM35"}


def yahoo_chart(ticker: str):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(ticker)
        + "?interval=1d&range=2mo"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 cwally-alerts"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode())
    result = payload["chart"]["result"][0]
    meta = result.get("meta", {})
    price = meta.get("regularMarketPrice") or meta.get("chartPreviousClose") or meta.get("previousClose")
    quote = (result.get("indicators", {}).get("quote", [{}]) or [{}])[0]
    closes = [x for x in (quote.get("close") or []) if x is not None]
    volumes = [x for x in (quote.get("volume") or []) if x is not None]
    ret20 = None
    if len(closes) > 20:
        ret20 = closes[-1] / closes[-21] - 1
    elif len(closes) > 5:
        ret20 = closes[-1] / closes[0] - 1
    avg5 = sum(volumes[-5:]) / 5 if len(volumes) >= 5 else None
    avg20 = sum(volumes[-20:]) / min(20, len(volumes)) if volumes else None
    return (float(price) if price is not None else None), ret20, avg5, avg20


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
    prev = {
        "zone": None,
        "price": None,
        "common_price": None,
        "ratio": None,
        "quality_flags": [],
        "last_event_ids": [],
        "updated_at": None,
    }
    if STATE_PATH.exists():
        try:
            prev.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass

    price = ret20 = avg5 = avg20 = None
    common = bench20 = cav5 = cav20 = None
    try:
        price, ret20, avg5, avg20 = yahoo_chart(cfg["ticker"])
    except Exception as exc:
        print(f"price fetch failed: {exc}", file=sys.stderr)
    try:
        common, bench20, cav5, cav20 = yahoo_chart(cfg["bench_ticker"])
    except Exception as exc:
        print(f"bench fetch failed: {exc}", file=sys.stderr)

    z = zone(price, cfg["bands"])
    if z == "DATA_FAIL" and prev.get("zone"):
        z = prev["zone"]
        price = prev.get("price")
        common = prev.get("common_price") if common is None else common

    ratio = (price / common) if price and common else None
    lag = (ret20 - bench20) if ret20 is not None and bench20 is not None else None
    vol_ratio = (avg5 / avg20) if avg5 and avg20 else None

    qcfg = cfg["quality"]
    flags = []
    if ratio is not None and ratio < qcfg["gap_ratio_min"]:
        flags.append("GAP")
    if lag is not None and lag <= qcfg["rel_lag"]:
        flags.append("LAG")
    if vol_ratio is not None and vol_ratio <= qcfg["vol_dry"]:
        flags.append("VOL")

    if z == "STOP20" and len(flags) >= qcfg["hits_exit"]:
        action_key = "EXIT"
    elif len(flags) >= qcfg["hits_exit"]:
        action_key = "QUALITY2"
    else:
        action_key = z

    live = [ev for ev in cfg.get("events", []) if ev["start"] <= today <= ev["end"]]
    live_ids = [e["id"] for e in live]
    new_live = [e for e in live if e["id"] not in (prev.get("last_event_ids") or [])]
    zone_changed = bool(prev.get("zone")) and prev.get("zone") != z and (
        z in EVENT_ZONES or prev.get("zone") in EVENT_ZONES
    )
    prev_flags = prev.get("quality_flags") or []
    quality_new = bool(set(flags) - set(prev_flags)) or (
        len(flags) >= qcfg["hits_exit"] and len(prev_flags) < qcfg["hits_exit"]
    )

    px = f"{price:,.0f}" if price is not None else "N/A"
    cx = f"{common:,.0f}" if common is not None else "N/A"
    cost = cfg["cost"]
    pnl = f"{(price / cost - 1) * 100:+.1f}%" if price else "n/a"
    ratio_s = f"{ratio:.3f}" if ratio is not None else "n/a"
    lines = [
        f"{stamp} | {mode} | {cfg['display']} {cfg['code']}",
        f"가 {px}원 | 원가 {cost:,} | {pnl} | {z}",
        f"보통 {cx}원 | 비율 {ratio_s} | 품질 {','.join(flags) if flags else '없음'}",
        cfg["actions"].get(action_key, ""),
    ]
    if ret20 is not None and bench20 is not None and lag is not None:
        lines.append(
            f"우선 20일 {ret20 * 100:+.1f}% | 보통 20일 {bench20 * 100:+.1f}% | 상대 {lag * 100:+.1f}p"
        )
    else:
        lines.append("상대수익 n/a")
    if vol_ratio is not None:
        lines.append(f"거래량 5일/20일 {vol_ratio:.2f}")
    lines += [
        "",
        "품질: GAP 비율<0.32 / LAG 상대-8%p / VOL 거래량35%",
        "폐기: 품질 2개 + 2,030원 | 손절 후 30일 재매수 금지",
        "시간: 2026-10 배당전환 / 2026-12 상대 / 2027-06 12개월 / 2028-06 교체",
    ]
    if live:
        lines.append("")
        lines.append("[시간 점검]")
        for e in live:
            lines.append(f"- {e['label']} {e['start']}~{e['end']}")
            lines.append(f"  {e['check']}")

    send = False
    title = f"{cfg['title_prefix']} {z} {px}"
    if mode == "event":
        if zone_changed or new_live or quality_new or action_key in {"QUALITY2", "EXIT"}:
            send = True
            if action_key == "EXIT":
                title = f"{cfg['title_prefix']} 폐기점검"
            elif new_live:
                title = f"{cfg['title_prefix']} 시간점검"
            elif quality_new or action_key == "QUALITY2":
                title = f"{cfg['title_prefix']} 품질악화"
            else:
                title = f"{cfg['title_prefix']} 매도점검"
        else:
            print("no 0220wl event; silent")
    else:
        send = True
        title = f"{cfg['title_prefix']} 점검 {z}"

    STATE_PATH.write_text(
        json.dumps(
            {
                "updated_at": stamp,
                "zone": z,
                "price": price,
                "common_price": common,
                "ratio": ratio,
                "quality_flags": flags,
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
        pri = 5 if action_key in {"STOP20", "EXIT"} or new_live else (
            4 if z in EVENT_ZONES or zone_changed or quality_new else 3
        )
        ntfy_publish(server, topic, title, msg, pri, "warning,chart_with_downwards_trend")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
