"""US listing filters. SIC exclusions are the documented Magic Formula map."""

from __future__ import annotations

import re

_PREF = re.compile(r"-[A-Z]{2,}$")
_NAME_DROP = re.compile(
    r"\b(ETF|ETN|Fund|Warrant|Preferred|Debenture|Notes|Acquisition Corp|Closed-End|Closed End|Unit)\b",
    re.IGNORECASE,
)


def is_common_listing(row: dict, exchanges: list[str]) -> bool:
    ticker = str(row.get("ticker") or "").strip()
    name = str(row.get("name") or "")
    if row.get("exchange") not in exchanges:
        return False
    if not ticker or any(ch in ticker for ch in "$^/"):
        return False
    if _PREF.search(ticker):
        return False
    if _NAME_DROP.search(name):
        return False
    return True


def sic_block_reason(sic: int | None, ranges: list[dict]) -> str | None:
    if sic is None:
        return None
    for item in ranges:
        if int(item["sic_from"]) <= int(sic) <= int(item["sic_to"]):
            return str(item["name"])
    return None


def foreign_filer(forms: set[str], foreign_forms: list[str], domestic_forms: list[str]) -> bool:
    """True when the filer uses 20-F/40-F and has no domestic 10-K or 10-Q."""
    forms_u = {str(item).upper() for item in forms}
    foreign = {item.upper() for item in foreign_forms}
    domestic = {item.upper() for item in domestic_forms}
    return bool(forms_u & foreign) and not (forms_u & domestic)


def normalize_form(value: str) -> str:
    return str(value or "").strip().upper()
