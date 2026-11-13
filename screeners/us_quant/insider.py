"""Form 4 open-market purchases (transaction code P)."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from xml.etree import ElementTree

_DOC = re.compile(r"<ownershipDocument\b.*?</ownershipDocument>", re.IGNORECASE | re.DOTALL)
_NS = re.compile(r"\sxmlns(?::\w+)?=\"[^\"]*\"")


def _local(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _text_map(node: ElementTree.Element) -> dict[str, str]:
    """First text value for each local tag, depth-first. Nested values use a path."""
    found: dict[str, str] = {}

    def walk(item: ElementTree.Element, path: str) -> None:
        name = _local(item.tag)
        here = f"{path}/{name}" if path else name
        text = (item.text or "").strip()
        if text and here not in found:
            found[here] = text
        for child in list(item):
            walk(child, here)

    walk(node, "")
    return found


def _num(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return None


def _flag(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def parse_form4(text: str) -> list[dict]:
    """Return purchase rows (code P, acquired) from one submission."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    rows = []
    for match in _DOC.finditer(text):
        blob = _NS.sub("", match.group(0))
        try:
            root = ElementTree.fromstring(blob)
        except ElementTree.ParseError:
            continue
        rows.extend(_one_document(root))
    return rows


def _one_document(root: ElementTree.Element) -> list[dict]:
    flat_issuer = _first_block(root, "issuer")
    owners = []
    for node in root.iter():
        if _local(node.tag) != "reportingOwner":
            continue
        info = _text_map(node)
        owners.append(
            {
                "owner_cik": _digits(info.get("reportingOwner/reportingOwnerId/rptOwnerCik")),
                "owner_name": info.get("reportingOwner/reportingOwnerId/rptOwnerName") or "",
                "director": _flag(info.get("reportingOwner/reportingOwnerRelationship/isDirector")),
                "officer": _flag(info.get("reportingOwner/reportingOwnerRelationship/isOfficer")),
                "ten_percent": _flag(info.get("reportingOwner/reportingOwnerRelationship/isTenPercentOwner")),
                "officer_title": info.get("reportingOwner/reportingOwnerRelationship/officerTitle") or "",
            }
        )
    issuer_cik = _digits(flat_issuer.get("issuerCik"))
    symbol = (flat_issuer.get("issuerTradingSymbol") or "").strip().upper()
    parsed = []
    for node in root.iter():
        if _local(node.tag) not in {"nonDerivativeTransaction", "derivativeTransaction"}:
            continue
        info = _text_map(node)
        code = (info.get(f"{_local(node.tag)}/transactionCoding/transactionCode") or "").strip().upper()
        acquired = (info.get(f"{_local(node.tag)}/transactionAmounts/transactionAcquiredDisposedCode/value") or "").strip().upper()
        when = info.get(f"{_local(node.tag)}/transactionDate/value") or ""
        shares = _num(info.get(f"{_local(node.tag)}/transactionAmounts/transactionShares/value"))
        price = _num(info.get(f"{_local(node.tag)}/transactionAmounts/transactionPricePerShare/value"))
        if code != "P" or acquired not in {"A", ""}:
            continue
        if shares is None or price is None or shares <= 0 or price <= 0:
            continue
        trade_day = _parse_date(when)
        if trade_day is None:
            continue
        parsed.append(
            {
                "issuer_cik": issuer_cik,
                "symbol": symbol,
                "trade_date": trade_day.isoformat(),
                "shares": shares,
                "price": price,
                "value": shares * price,
                "owners": owners,
            }
        )
    return parsed


def _first_block(root: ElementTree.Element, name: str) -> dict[str, str]:
    for node in root.iter():
        if _local(node.tag) == name:
            # Flat map of descendants using the local name only (issuer fields are unique).
            out = {}
            for child in node.iter():
                key = _local(child.tag)
                text = (child.text or "").strip()
                if text and key not in out:
                    out[key] = text
            return out
    return {}


def _digits(value: str | None) -> str:
    raw = re.sub(r"\D", "", value or "")
    if not raw:
        return ""
    return raw.zfill(10)[-10:]


def _parse_date(value: str) -> date | None:
    value = (value or "")[:10]
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def in_window(trade_date: str, as_of: date, lookback_days: int) -> bool:
    day = _parse_date(trade_date)
    if day is None:
        return False
    start = as_of - timedelta(days=lookback_days)
    return start <= day <= as_of


def aggregate_purchases(trades: list[dict], as_of: date, lookback_days: int) -> dict[str, dict]:
    """Aggregate code-P rows by issuer CIK."""
    grouped: dict[str, dict] = {}
    for trade in trades:
        if not in_window(trade["trade_date"], as_of, lookback_days):
            continue
        cik = trade.get("issuer_cik") or ""
        if not cik:
            continue
        bucket = grouped.setdefault(
            cik,
            {
                "issuer_cik": cik,
                "symbols": set(),
                "owners": {},
                "total_value": 0.0,
                "transaction_count": 0,
                "latest_trade_date": "",
                "accessions": set(),
            },
        )
        if trade.get("symbol"):
            bucket["symbols"].add(trade["symbol"])
        if trade.get("accn"):
            bucket["accessions"].add(trade["accn"])
        bucket["total_value"] += float(trade["value"])
        bucket["transaction_count"] += 1
        if trade["trade_date"] > bucket["latest_trade_date"]:
            bucket["latest_trade_date"] = trade["trade_date"]
        for owner in trade.get("owners") or []:
            key = owner.get("owner_cik") or owner.get("owner_name") or ""
            if not key:
                continue
            slot = bucket["owners"].setdefault(
                key,
                {"owner_cik": owner.get("owner_cik") or "", "director": False, "officer": False, "ten_percent": False, "titles": set()},
            )
            slot["director"] = slot["director"] or bool(owner.get("director"))
            slot["officer"] = slot["officer"] or bool(owner.get("officer"))
            slot["ten_percent"] = slot["ten_percent"] or bool(owner.get("ten_percent"))
            title = (owner.get("officer_title") or "").strip()
            if title:
                slot["titles"].add(title)
    out = {}
    for cik, bucket in grouped.items():
        roles = []
        if any(item["director"] for item in bucket["owners"].values()):
            roles.append("director")
        if any(item["officer"] for item in bucket["owners"].values()):
            roles.append("officer")
        if any(item["ten_percent"] for item in bucket["owners"].values()):
            roles.append("ten_percent_owner")
        titles = sorted({title for item in bucket["owners"].values() for title in item["titles"]})[:8]
        out[cik] = {
            "issuer_cik": cik,
            "distinct_insiders": len(bucket["owners"]),
            "total_value_usd": bucket["total_value"],
            "roles": roles,
            "officer_titles": titles,
            "latest_trade_date": bucket["latest_trade_date"],
            "transaction_count": bucket["transaction_count"],
            "symbols": sorted(bucket["symbols"]),
            "accessions": sorted(bucket["accessions"]),
        }
    return out
