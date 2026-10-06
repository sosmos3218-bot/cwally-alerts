#!/usr/bin/env python3
"""Shared Naver mobile/stock API helpers.

The legacy finance.naver.com pages (investorDealTrendDay, news_notice, news_news,
interestDailyQuote, frgn, dart) return 410 / redirect since 2026-09. Use these
JSON endpoints instead:
  m.stock.naver.com/api/index/KOSPI/trend?bizdate=YYYYMMDD   (market investor flow, 억원)
  m.stock.naver.com/api/stock/{code}/trend?pageSize=N         (per-stock close/volume/foreign/organ)
  m.stock.naver.com/front-api/marketIndex/prices?category=bond&reutersCode=KR3YT=RR
  stock.naver.com/api/domestic/detail/notice?itemCode={code}  (exchange/DART notices incl. contents)
  m.stock.naver.com/api/news/stock/{code}                     (news)
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from html import unescape

UA = {"User-Agent": "Mozilla/5.0 (cwally-alerts; +mailto:sosmos3218@gmail.com)"}

# legacy finance.naver marketindexCd -> reuters code
YIELD_CODES = {
    "IRR_GOVT03Y": "KR3YT=RR",
    "IRR_GOVT05Y": "KR5YT=RR",
    "IRR_GOVT10Y": "KR10YT=RR",
    "IRR_GOVT20Y": "KR20YT=RR",
    "IRR_GOVT30Y": "KR30YT=RR",
}


def get_json(url: str, timeout: int = 20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def num(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "").replace("+", "").replace("%", ""))
    except ValueError:
        return None


def html_text(html: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html or "", flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def flatten_text(obj) -> str:
    """Join title/contents/body strings of a JSON payload into plain text."""
    out: list[str] = []

    def walk(v):
        if isinstance(v, dict):
            for k, x in v.items():
                if isinstance(x, str) and k in {"title", "contents", "body", "comment", "summary", "datetime", "bizdateSeq"}:
                    out.append(html_text(x) if "<" in x else x)
                else:
                    walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(obj)
    return "\n".join(out)


def kospi_trend(day: str, market: str = "KOSPI") -> dict | None:
    """Market investor net buy for day (YYYYMMDD) in won. None if that day is not posted."""
    raw = get_json(f"https://m.stock.naver.com/api/index/{market}/trend?bizdate={day}")
    if not isinstance(raw, dict) or str(raw.get("bizdate")) != day:
        return None
    eok = 100_000_000
    vals = {
        "개인": num(raw.get("personalValue")),
        "외국인": num(raw.get("foreignValue")),
        "기관합계": num(raw.get("institutionalValue")),
    }
    if any(v is None for v in vals.values()):
        return None
    return {k: v * eok for k, v in vals.items()}


def stock_trend(code: str, size: int = 10) -> list[dict]:
    raw = get_json(f"https://m.stock.naver.com/api/stock/{code}/trend?pageSize={size}")
    rows = raw if isinstance(raw, list) else []
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        out.append({
            "day": str(r.get("bizdate") or ""),
            "close": num(r.get("closePrice")),
            "volume": num(r.get("accumulatedTradingVolume")),
            "foreign": num(r.get("foreignerPureBuyQuant")),
            "organ": num(r.get("organPureBuyQuant")),
            "individual": num(r.get("individualPureBuyQuant")),
        })
    return out


def bond_close(code: str) -> tuple[float | None, str | None]:
    """Latest close yield for legacy code (IRR_GOVT03Y) or reuters code (KR3YT=RR)."""
    reuters = YIELD_CODES.get(code, code)
    raw = get_json(
        "https://m.stock.naver.com/front-api/marketIndex/prices?category=bond&page=1&reutersCode="
        + urllib.parse.quote(reuters)
    )
    rows = (raw or {}).get("result") or []
    for r in rows:
        v = num(r.get("closePrice"))
        if v is not None:
            return v, str(r.get("localTradedAt") or "")[:10]
    return None, None


def bond_series(code: str, pages: int = 2) -> list[tuple[str, float]]:
    """Oldest->newest (date, close)."""
    reuters = YIELD_CODES.get(code, code)
    out = {}
    for page in range(1, pages + 1):
        raw = get_json(
            "https://m.stock.naver.com/front-api/marketIndex/prices?category=bond&page="
            + str(page) + "&reutersCode=" + urllib.parse.quote(reuters)
        )
        for r in (raw or {}).get("result") or []:
            v = num(r.get("closePrice"))
            d = str(r.get("localTradedAt") or "")[:10]
            if v is not None and d:
                out[d] = v
    return sorted(out.items())


def notices(code: str, size: int = 30) -> list[dict]:
    """[{title, datetime, text, url}] newest first."""
    raw = get_json(f"https://stock.naver.com/api/domestic/detail/notice?itemCode={code}&page=1&pageSize={size}")
    out = []
    for r in raw if isinstance(raw, list) else []:
        if not isinstance(r, dict):
            continue
        out.append({
            "id": str(r.get("no") or r.get("bizdateSeq") or r.get("title")),
            "title": str(r.get("title") or "").strip(),
            "datetime": str(r.get("datetime") or ""),
            "text": html_text(str(r.get("contents") or "")),
            "url": f"https://m.stock.naver.com/domestic/stock/{code}/notice",
        })
    return out


def news(code: str, size: int = 20) -> list[dict]:
    raw = get_json(f"https://m.stock.naver.com/api/news/stock/{code}?pageSize={size}&page=1")
    out = []
    for block in raw if isinstance(raw, list) else []:
        for it in (block or {}).get("items") or []:
            out.append({
                "title": html_text(str(it.get("title") or "")),
                "datetime": str(it.get("datetime") or ""),
                "body": html_text(str(it.get("body") or "")),
                "url": f"https://n.news.naver.com/mnews/article/{it.get('officeId')}/{it.get('articleId')}",
            })
    return out
