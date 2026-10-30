#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from ntfy_util import route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "iren.json"
STATE_PATH = ROOT / "state" / "iren.json"
UA = "cwally-alerts/iren (alerts@local)"
TAG_KO = {
    "REVIEW": "$30 아래. 재검토",
    "JPM": "JPM 목표 $65 도달",
    "CONSENSUS": "콘센서스 $80 도달",
    "WATCH": "관찰 구간",
}
DO_NOW = {
    "REVIEW": "보유 없음, 관망 유지, 추가매수 보류. Horizon 2~4 인수와 채굴 종료 일정을 먼저 확인하세요.",
    "JPM": "JPM 목표가 근처입니다. 계약 인수 공시가 없으면 추격 매수 보류.",
    "CONSENSUS": "컨센서스 목표가 근처입니다. 추격 매수 보류, 근거 재확인.",
    "WATCH": "새 뉴스와 일정만 보고, 오늘 새로 사지 마세요.",
}
TITLE_BY_TAG = {
    "REVIEW": "$30 하회 재검토",
    "JPM": "$65 근처 추격 금지",
    "CONSENSUS": "$80 근처 추격 금지",
    "WATCH": "관찰 유지",
}


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
        tm = re.search(r"<title[^>]*>(.*?)</title>", block, re.S | re.I)
        title = unescape(re.sub(r"<[^>]+>", " ", tm.group(1) if tm else "")).strip()
        href = ""
        hm = re.search(r"<link[^>]*href=\"([^\"]+)\"", block, re.I)
        if hm:
            href = unescape(hm.group(1))
        if title:
            items.append({"id": href or title, "title": re.sub(r"\s+", " ", title), "url": href, "src": "sec"})
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
        {"topic": route_topic(topic, title), "title": title, "message": message, "priority": priority, "tags": tags.split(",")}
    ).encode()
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
    prev = {"price": None, "last_event_ids": [], "seen_ids": [], "updated_at": None}
    if STATE_PATH.exists():
        try:
            prev.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass

    price = yahoo_price(cfg["ticker"])
    # SEC filings moved to the [IREN-PX] whitelist (2026-10-30): this alert keeps news + schedule only, so
    # every 8-K no longer arrives twice. fetch_sec is kept for reference but not called.
    news = fetch_yahoo_news(cfg["ticker"])
    items = news
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

    px = f"${price:,.2f}" if price is not None else "시세 없음"
    lv = cfg["levels"]
    tag = "WATCH"
    if price is not None and price <= lv["review"]:
        tag = "REVIEW"
    elif price is not None and price >= lv["consensus"]:
        tag = "CONSENSUS"
    elif price is not None and price >= lv["jpm"]:
        tag = "JPM"

    send = False
    title = f"[IREN] {TITLE_BY_TAG.get(tag, tag)}"
    pri = 3
    tags = "zap,chart_with_upwards_trend"
    if mode == "event":
        if hot or new_live:
            send = True
            title = f"[IREN] 새 뉴스/일정 | {TITLE_BY_TAG.get(tag, tag)}" if hot else f"[IREN] 일정 | {TITLE_BY_TAG.get(tag, tag)}"
            # 뉴스 헤드라인(RSS)이면 P2, 일정만이면 P3. SEC 공시는 [IREN-PX] 화이트리스트가 따로 보낸다.
            pri = 2 if hot else 3
            tags = "warning,zap" if hot else "calendar,zap"
        else:
            print("no iren event; silent")
    else:
        send = True
        pri = 2
        title = f"[IREN] 주간 체크 | {TITLE_BY_TAG.get(tag, tag)}" if now.weekday() == 0 else f"[IREN] {TITLE_BY_TAG.get(tag, tag)}"

    lines = [
        f"{stamp} | IREN | {cfg['display']}",
        "이게 뭐 알림: IREN(아이렌 AI 데이터센터) 관심 종목(보유 없음)의 일정과 뉴스 검토입니다. 계약 인수, 전력, 채굴 종료가 핵심입니다. SEC 공시는 [IREN-PX], 실적은 [IREN-EARN] 이 따로 보냅니다.",
        f"지금 결과/상태: {px} | {TAG_KO.get(tag, tag)}. 계약 ARR $40억 / 가동 $10억 / 갭 $30억.",
        f"지금 할 일: {DO_NOW.get(tag, '')}",
        "나중에: MS Horizon 2~4 인도(올 4분기 목표, 유예는 27년 2분기 초), NVDA $34억은 27년 램프. 이 공시가 나오기 전에 추격하지 마세요.",
        "익절/줄일 때: 보유 없음이라 해당 없음. 경고 신호는 $30 하회, Horizon 미인수, ERCOT/스위트워터 지연, 채굴 종료 후 현금흐름 공백.",
        "하지 말 것: 공시 없이 추격 매수, 신용/미수, 이 알림만 보고 주문. 투자 권유 아님.",
    ]
    if live:
        lines += ["", "지금 진행 중인 일정:"]
        for e in live:
            lines.append(f"- {e['label']} {e['start']}~{e['end']}: {e['check']}")
    if soon:
        lines += ["", "21일 안 일정:"]
        for e in soon:
            lines.append(f"- {e['label']} {e['start']}")
    if hot:
        lines += ["", "새 키워드 공시/뉴스:"]
        for it in hot[:6]:
            lines.append(f"- [{it['src']}] {it['title']}")
            if it.get("url"):
                lines.append(f"  {it['url']}")

    STATE_PATH.write_text(
        json.dumps(
            {"updated_at": stamp, "price": price, "tag": tag, "last_event_ids": live_ids, "seen_ids": seen[-120:]},
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
