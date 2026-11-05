"""코스피, 코스닥 보통주만 남긴다. 우선주, 스팩, ETF, ETN 은 뺀다."""

from __future__ import annotations

import re

PREFERRED_RE = re.compile(r"\d*우[B-Z]?$")


def is_preferred_name(name: str) -> bool:
    compact = str(name or "").replace(" ", "")
    if "우선주" in compact:
        return True
    return PREFERRED_RE.search(compact) is not None


def is_spac_name(name: str) -> bool:
    return "스팩" in str(name or "").replace(" ", "")


def is_etf_name(name: str) -> bool:
    text = str(name or "").upper().replace(" ", "")
    return "ETF" in text or "ETN" in text


def keep_listing_row(row: dict, markets: set[str]) -> bool:
    market = str(row.get("market") or row.get("Market") or "")
    if market not in markets:
        return False
    name = str(row.get("name") or row.get("Name") or "")
    if is_preferred_name(name) or is_spac_name(name) or is_etf_name(name):
        return False
    code = str(row.get("code") or row.get("Code") or "")
    return code.isdigit() and len(code) <= 6
