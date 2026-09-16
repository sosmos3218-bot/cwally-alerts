#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "iren.json"
STATE_PATH = ROOT / "state" / "iren.json"
UA = "cwally-alerts/iren (alerts@local)"


def yahoo_price(ticker: str):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(ticker)
        + "?interval=1d&range=5d"
    )
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode())
        meta = payload["chart"]["result"][0].get("meta", {})
        price = meta.get("regularMarketPrice") or meta.get("previousClose")
        return float(price) if price is not None else None
    except Exception as exc:
        print(f"price fetch failed: {exc}", file=sys.stderr)
        return None


def fetch_sec(cik: str):
    url = (
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK="
        + cik
        + "&type=&dateb=&owner=include&count=15&output=atom"
    )
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/atom+xml"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            xml = resp.read().decode("utf-8", errors="ignore")
    except Exception as exc:
        print(f"sec fetch failed: {exc}", file=sys.stderr)
        return []
    items = []
    for block in re.findall(r"<entry>(.*?)</entry>", xml, re.S | re.I):
        title = unescape(re.sub(r"<[^>]+>", " ", re.search(r"<title[^>]*>(.*?)</title>", block, re.S | re.I).group(1) if re.search(r"<title[^>]*>(.*?)</title>", block, re.S | re.I) else "")).strip()
        href = ""
        hm = re.search(r"<link[^>]*href=\"([^\"]+)\"", block, re.I)
        if hm:
            href = unescape(hm.group(1))
        eid = href or title
        if title:
            items.append({"id": eid, "title": re.sub(r"\s+", " ", title), "url": href, "src": "sec"})
    return items[:15]


def fetch_yahoo_news(ticker: str):
    url = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=" + urllib.parse.quote(ticker) + "&region=US&lang=en-US"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            xml = resp.read().decode("utf-8", errors="ignore")
    except Exception as exc:
        print(f"yahoo news failed: {exc}", file=sys.stderr)
        return []
    items = []
    for block in re.findall(r"<item>(.*?)</item>", xml, re.S | re.I):
        tm = re.search(r"<title[^>]*>(.*?)</title>", block, re.S | re.I)
        lm = re.search(r"<link[^>]*>(.*?)</link>", block, re.S | re.I)
        title = unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", tm.group(1) if tm else "")).strip()
        href = unescape(re.sub(r"<[^>]+>", "", lm.group(1) if lm else "")).strip()
        if title:
            items.append({"id": href or title, "title": re.sub(r"\s+", " ", title), "url": href, "src": "yahoo"})
    return items[:15]


def hot_hit(title: str, keywords):
    t = title.lower()
    return any(k.lower() in t for k in keywords)


def active_events(cfg, today):
    return [ev for ev in cfg.get("events", []) if ev["start"] <= today <= ev["end"]]


def upcoming_events(cfg, today, days=21):
    lim = (datetime.strptime(today, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")
    return [ev for ev in cfg.get("events", []) if today < ev["start"] <= lim]


def ntfy_publish(server, topic, title, message, priority, tags):
    payload = json.dumps(
        {"topic": topic, "title": title, "message": message, "priority": priority, "tags": tags.split(",")}
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
    prev = {"price": None, "last_event_ids": [], "seen_ids": [], "updated_at": None}
    if STATE_PATH.exists():
        try:
            prev.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass

    price = yahoo_price(cfg["ticker"])
    filings = fetch_sec(cfg["cik"])
    news = fetch_yahoo_news(cfg["ticker"])
    items = filings + news
    seen = list(prev.get("seen_ids") or [])
    seen_set = set(seen)
    new_items = [it for it in items if it["id"] not in seen_set]
    hot = [it for it in new_items if hot_hit(it["title"], cfg["keywords"])]
    for it in reversed(new_items):
        seen.append(it["id"])

    live = active_events(cfg, today)
    soon = upcoming_events(cfg, today)
    live_ids = [e["id"] for e in live]
    new_live = [e for e in live if e["id"] not in (prev.get("last_event_ids") or [])]

    px = f"${price:,.2f}" if price is not None else "N/A"
    lv = cfg["levels"]
    tag = "WATCH"
    if price is not None and price <= lv["review"]:
        tag = "REVIEW"
    elif price is not None and price >= lv["consensus"]:
        tag = "CONSENSUS"
    elif price is not None and price >= lv["jpm"]:
        tag = "JPM"

    lines = [
        f"{stamp} | {mode} | {cfg['display']}",
        f"가 {px} | {tag} | 계약ARR $40억 / 가동 $10억 / 갭 $30억",
        "MS $97억 Horizon1 인수완료 · 2-4는 Q4 목표(유예 27Q2초)",
        "NVDA $34억은 27년 램프($7억 ARR, 26 ARR 미포함)",
        "전력: Childress 실행 vs Sweetwater ERCOT 조건부",
    ]
    if live:
        lines += ["", "[D-window]"]
        for e in live:
            lines.append(f"- {e['label']} {e['start']}~{e['end']}")
            lines.append(f"  {e['check']}")
    if soon:
        lines += ["", "[21일 내]"]
        for e in soon:
            lines.append(f"- {e['label']} {e['start']}")
    if hot:
        lines += ["", "[신규 키워드]"]
        for it in hot[:6]:
            lines.append(f"- [{it['src']}] {it['title']}")
            if it.get("url"):
                lines.append(f"  {it['url']}")
    lines += ["", "체크: H2 인수 / H3-4 유예 / ERCOT / 27년 $/W / 채굴종료 FCF"]

    send = False
    title = f"{cfg['title_prefix']} {tag} {px}"
    pri = 3
    tags = "zap,chart_with_upwards_trend"
    if mode == "event":
        if hot or new_live:
            send = True
            title = f"{cfg['title_prefix']} 공시/일정" if hot else f"{cfg['title_prefix']} 일정"
            pri = 5 if hot else 4
            tags = "warning,zap" if hot else "calendar,zap"
        else:
            print("no iren event; silent")
    else:
        send = True
        title = f"{cfg['title_prefix']} 주간 체크" if now.weekday() == 0 else f"{cfg['title_prefix']} {tag}"

    STATE_PATH.write_text(
        json.dumps(
            {
                "updated_at": stamp,
                "price": price,
                "tag": tag,
                "last_event_ids": live_ids,
                "seen_ids": seen[-120:],
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
        ntfy_publish(server, topic, title, msg, pri, tags)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
