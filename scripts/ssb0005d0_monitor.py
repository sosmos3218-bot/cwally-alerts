#!/usr/bin/env python3
"""0005D0 condition watch. Weekday 15:20 KST only. Silent unless a condition changes."""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "ssb0005d0.json"
STATE_PATH = ROOT / "state" / "ssb0005d0.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}
SKIP = ("MOU", "업무협약", "맞손", "공동개발", "목표주가", "투자의견", "리포트")


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return dict(fallback)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        fallback.update(data)
        return fallback
    except json.JSONDecodeError:
        return fallback


def http_get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def num(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "").strip()
    if not text or text in {"-", "N/A"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def quote(symbol: str) -> dict | None:
    url = f"https://m.stock.naver.com/api/stock/{symbol}/basic"
    payload = json.loads(http_get(url))
    price = num(payload.get("closePrice") or payload.get("nowPrice"))
    value = num(payload.get("accumulatedTradingValue") or payload.get("accTradePrice"))
    volume = num(payload.get("accumulatedTradingVolume") or payload.get("accTradeVolume"))
    if price is None:
        return None
    return {"price": price, "value": value, "volume": volume}


def history(symbol: str) -> list[dict]:
    url = (
        "https://api.stock.naver.com/chart/domestic/item/"
        + symbol
        + "/day?startDateTime=20260801&endDateTime=20261231"
    )
    raw = json.loads(http_get(url))
    rows = raw if isinstance(raw, list) else raw.get("result") or raw.get("priceInfos") or []
    out = []
    for row in rows:
        price = num(row.get("closePrice") or row.get("close"))
        value = num(row.get("accumulatedTradingValue") or row.get("tradingValue"))
        day = str(row.get("localDate") or row.get("date") or "")[:10]
        if price is not None:
            out.append({"date": day, "price": price, "value": value})
    return out


def notices(code: str) -> list[dict]:
    url = f"https://stock.naver.com/api/domestic/detail/notice?itemCode={code}&startIdx=0&pageSize=15"
    raw = json.loads(http_get(url))
    rows = raw if isinstance(raw, list) else raw.get("result") or raw.get("list") or []
    items = []
    for row in rows:
        title = (row.get("title") or "").strip()
        date = (row.get("datetime") or row.get("date") or "")[:10]
        nid = str(row.get("no") or f"{code}:{date}:{title}")
        if title:
            items.append({"id": f"{code}:{nid}", "code": code, "date": date, "title": title})
    return items


def classify(title: str, role: str, rules: dict) -> str | None:
    text = compact(title)
    if any(compact(k) in text for k in SKIP):
        return None
    kinds = []
    if role == "delay_or_sample":
        if any(compact(k) in text for k in rules.get("down_delay") or []):
            kinds.append("DELAY")
        if any(compact(k) in text for k in rules.get("up_sample") or []):
            kinds.append("SAMPLE")
    if role == "earnings" and any(compact(k) in text for k in rules.get("down_earn") or []):
        kinds.append("EARN")
    if not kinds:
        return None
    return "+".join(dict.fromkeys(kinds))


def publish(topic: str, title: str, message: str, priority: int) -> None:
    if topic not in {"cwally-port", "cwally-ops"}:
        topic = "cwally-port"
    payload = json.dumps(
        {
            "topic": topic,
            "title": title,
            "message": message,
            "priority": priority,
            "tags": ["chart_with_upwards_trend"],
        }
    ).encode()
    req = urllib.request.Request(
        "https://ntfy.sh",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(resp.read().decode())
