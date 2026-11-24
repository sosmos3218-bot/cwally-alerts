"""Daily closes from the Yahoo chart endpoint. Failures stay off the lists."""

from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

from screeners.us_quant.http_util import yahoo_get

NY = ZoneInfo("America/New_York")


def chart_url(symbol: str, start: date, end: date) -> str:
    period1 = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
    period2 = int(datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp()) + 86400
    return (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{quote(symbol)}?interval=1d&period1={period1}&period2={period2}"
    )


def parse_chart(payload: dict, as_of: date) -> dict | None:
    """Last complete regular-session close on or before as_of."""
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not result:
        return None
    stamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    meta = result.get("meta") or {}
    chosen = None
    as_of_volume = 0
    as_of_close_missing = False
    for index, stamp in enumerate(stamps):
        session = datetime.fromtimestamp(int(stamp), NY).date()
        if session > as_of:
            continue
        close = closes[index] if index < len(closes) else None
        volume = volumes[index] if index < len(volumes) else None
        if session == as_of and close is None:
            as_of_close_missing = True
            as_of_volume = int(volume or 0)
            continue
        if close is None:
            continue
        chosen = {
            "price": float(close),
            "price_date": session.isoformat(),
            "volume": int(volume or 0),
            "price_source": "yahoo_chart_close",
        }
    if as_of_close_missing:
        stamped = _regular_session_price(meta, as_of, as_of_volume)
        if stamped is not None:
            return stamped
    return chosen


def _regular_session_price(meta: dict, as_of: date, volume: int) -> dict | None:
    """Use the close print when the daily bar exists but its close is still null."""
    market_time = meta.get("regularMarketTime")
    market_price = meta.get("regularMarketPrice")
    if market_time is None or market_price is None:
        return None
    session = datetime.fromtimestamp(int(market_time), NY).date()
    if session != as_of:
        return None
    return {
        "price": float(market_price),
        "price_date": session.isoformat(),
        "volume": volume,
        "price_source": "yahoo_regular_market_price",
    }


def common_close_date(price_dates: list[str], fallback: date) -> str:
    """Most common last-bar date. The later date wins a tie.

    Each fetched symbol contributes one close, the last bar on or before the
    as-of day. The trade date is that shared session, not the calendar day
    the screen happened to run.
    """
    counts = Counter(day for day in price_dates if day)
    if not counts:
        return fallback.isoformat()
    return max(counts, key=lambda day: (counts[day], day))


def fetch_close(symbol: str, as_of: date, getter=yahoo_get) -> dict | None:
    start = as_of - timedelta(days=14)
    end = as_of + timedelta(days=1)
    raw = getter(chart_url(symbol, start, end))
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parse_chart(payload, as_of)
