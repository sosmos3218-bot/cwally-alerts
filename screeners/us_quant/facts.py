"""Select EDGAR facts for the screens.

Duration bands (days, end minus start):
- quarter: 70 to 125
- year: 330 to 400
Anything longer than a quarter and shorter than a year is treated as year-to-date.

EBIT is trailing twelve months of OperatingIncomeLoss.
When the longest fact ending on the latest period end covers about a year, that
value is used (a fiscal year, or a tagged twelve-month total).
Otherwise TTM = current year-to-date + prior fiscal year - prior matching year-to-date.
If that bridge is incomplete, the last four quarter facts are summed when the
gaps between them stay inside 70 to 140 days. Fewer than four quarters is incomplete.

Total debt, on one balance-sheet date, avoids adding a total and its parts:
- DebtCurrent + LongTermDebtNoncurrent when either tag is present.
  If DebtCurrent is absent, add LongTermDebtCurrent, ShortTermBorrowings, and CommercialPaper.
- Else LongTermDebt, plus ShortTermBorrowings and CommercialPaper (current maturities
  stay inside LongTermDebt).
- Else LongTermDebtAndCapitalLeaseObligations.
- If no debt tag is present, debt is 0 and the source is no_debt_tag.

Cash is CashAndCashEquivalentsAtCarryingValue, else Cash. The restricted-cash
roll-up is not used.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

KEEP_FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A"}

DURATION_TAGS = {
    "OperatingIncomeLoss",
    "NetIncomeLoss",
    "NetCashProvidedByUsedInOperatingActivities",
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
    "GrossProfit",
    "CostOfRevenue",
    "CostOfGoodsAndServicesSold",
    "CostOfGoodsSold",
    "ProceedsFromIssuanceOfCommonStock",
    "ProceedsFromIssuanceOfShares",
    "ProceedsFromIssuanceOfPreferredStockStock",
    "WeightedAverageNumberOfDilutedSharesOutstanding",
    "WeightedAverageNumberOfSharesOutstandingBasic",
}

INSTANT_TAGS = {
    "Assets",
    "AssetsCurrent",
    "LiabilitiesCurrent",
    "PropertyPlantAndEquipmentNet",
    "CashAndCashEquivalentsAtCarryingValue",
    "Cash",
    "DebtCurrent",
    "LongTermDebtNoncurrent",
    "LongTermDebtCurrent",
    "LongTermDebt",
    "ShortTermBorrowings",
    "CommercialPaper",
    "LongTermDebtAndCapitalLeaseObligations",
    "CommonStockSharesOutstanding",
}

DEI_INSTANT = {"EntityCommonStockSharesOutstanding"}

EXTRACT_VERSION = 1

REVENUE_PRIORITY = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
]

GROSS_COST_PRIORITY = ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"]


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _fact_row(tag: str, unit: str, raw: dict) -> dict | None:
    form = str(raw.get("form") or "")
    if form not in KEEP_FORMS:
        return None
    end = _parse_date(raw.get("end"))
    filed = _parse_date(raw.get("filed"))
    if end is None or filed is None:
        return None
    if "val" not in raw:
        return None
    start = _parse_date(raw.get("start"))
    try:
        val = float(raw["val"])
    except (TypeError, ValueError):
        return None
    return {
        "tag": tag,
        "unit": unit,
        "start": start.isoformat() if start else None,
        "end": end.isoformat(),
        "filed": filed.isoformat(),
        "val": val,
        "accn": str(raw.get("accn") or ""),
        "form": form,
        "fp": str(raw.get("fp") or ""),
        "fy": raw.get("fy"),
    }


def extract_companyfacts(payload: dict) -> dict:
    """Trim a companyfacts document to the tags the screens read."""
    concepts: dict[str, list[dict]] = {}
    groups = [
        ("us-gaap", DURATION_TAGS | INSTANT_TAGS, {"USD", "shares"}),
        ("dei", DEI_INSTANT, {"shares"}),
    ]
    facts = payload.get("facts") or {}
    for taxonomy, tags, units_ok in groups:
        block = facts.get(taxonomy) or {}
        for tag in tags:
            node = block.get(tag)
            if not node:
                continue
            rows = []
            for unit, items in (node.get("units") or {}).items():
                if unit not in units_ok:
                    continue
                for raw in items:
                    row = _fact_row(tag, unit, raw)
                    if row is not None:
                        rows.append(row)
            if rows:
                concepts[f"{taxonomy}:{tag}"] = _dedupe(rows)
    forms = sorted({row["form"] for rows in concepts.values() for row in rows})
    accessions = sorted({row["accn"] for rows in concepts.values() for row in rows if row["accn"]})
    filed = [row["filed"] for rows in concepts.values() for row in rows]
    return {
        "extract_version": EXTRACT_VERSION,
        "cik": int(payload.get("cik") or 0),
        "entity_name": str(payload.get("entityName") or ""),
        "forms_seen": forms,
        "seen_accn": accessions,
        "latest_filed": max(filed) if filed else None,
        "concepts": concepts,
    }


def _dedupe(rows: list[dict]) -> list[dict]:
    """One value per start/end/tag. The latest filed amendment wins."""
    best: dict[tuple, dict] = {}
    for row in rows:
        key = (row["tag"], row["start"], row["end"], row["unit"])
        current = best.get(key)
        if current is None or _better(row, current):
            best[key] = row
    return sorted(best.values(), key=lambda item: (item["end"], item["start"] or "", item["accn"]))


def _better(row: dict, current: dict) -> bool:
    if row["filed"] != current["filed"]:
        return row["filed"] > current["filed"]
    row_amend = row["form"].endswith("/A")
    cur_amend = current["form"].endswith("/A")
    if row_amend != cur_amend:
        return row_amend
    return row["accn"] > current["accn"]


def _rows(facts: dict, concept: str, taxonomy: str = "us-gaap") -> list[dict]:
    return list((facts.get("concepts") or {}).get(f"{taxonomy}:{concept}") or [])


def _as_of_rows(rows: list[dict], as_of: date, domestic_only: bool = True) -> list[dict]:
    kept = []
    for row in rows:
        filed = _parse_date(row["filed"])
        end = _parse_date(row["end"])
        if filed is None or end is None or filed > as_of or end > as_of:
            continue
        if domestic_only and row["form"] not in {"10-K", "10-K/A", "10-Q", "10-Q/A"}:
            continue
        kept.append(row)
    return kept


def _days(row: dict) -> int | None:
    start = _parse_date(row.get("start"))
    end = _parse_date(row.get("end"))
    if start is None or end is None:
        return None
    return (end - start).days


def _is_year(row: dict) -> bool:
    days = _days(row)
    return days is not None and 330 <= days <= 400


def _is_quarter(row: dict) -> bool:
    days = _days(row)
    return days is not None and 70 <= days <= 125


def _component(row: dict) -> dict:
    return {
        "concept": row["tag"],
        "start": row["start"],
        "end": row["end"],
        "value": row["val"],
        "accn": row["accn"],
        "form": row["form"],
        "filed": row["filed"],
        "fp": row["fp"],
        "fy": row["fy"],
    }


def select_ttm_ebit(facts: dict, as_of: date) -> dict:
    rows = _as_of_rows(_rows(facts, "OperatingIncomeLoss"), as_of)
    rows = [row for row in rows if _days(row) is not None]
    if not rows:
        return {"ok": False, "reason": "ebit_missing"}
    latest_end = max(row["end"] for row in rows)
    at_latest = [row for row in rows if row["end"] == latest_end]
    chosen = max(at_latest, key=lambda row: _days(row) or 0)
    days = _days(chosen) or 0
    if days >= 330:
        return {
            "ok": True,
            "provisional": False,
            "value": chosen["val"],
            "method": "longest_duration_at_latest_end",
            "period_end": chosen["end"],
            "accession": chosen["accn"],
            "components": [_component(chosen)],
        }
    bridge = _ytd_bridge(rows, chosen)
    if bridge is not None:
        return bridge
    quarters = _sum_quarters(rows)
    if quarters is not None:
        return quarters
    return {
        "ok": False,
        "reason": "ebit_provisional",
        "detail": "trailing twelve months is incomplete",
        "period_end": latest_end,
    }


def _ytd_bridge(rows: list[dict], ytd: dict) -> dict | None:
    ytd_days = _days(ytd) or 0
    if ytd_days < 70:
        return None
    years = [row for row in rows if _is_year(row) and row["end"] < ytd["end"]]
    if not years:
        return None
    fiscal = max(years, key=lambda row: row["end"])
    year_earlier = _parse_date(ytd["end"])
    if year_earlier is None:
        return None
    year_earlier = year_earlier - timedelta(days=365)
    candidates = []
    for row in rows:
        end = _parse_date(row["end"])
        days = _days(row)
        if end is None or days is None:
            continue
        if abs((end - year_earlier).days) <= 20 and abs(days - ytd_days) <= 20:
            candidates.append(row)
    if not candidates:
        return None
    prior = min(candidates, key=lambda row: abs((_parse_date(row["end"]) - year_earlier).days))
    if not (prior["end"] < fiscal["end"] < ytd["end"]):
        return None
    value = ytd["val"] + fiscal["val"] - prior["val"]
    return {
        "ok": True,
        "provisional": False,
        "value": value,
        "method": "ytd_bridge",
        "period_end": ytd["end"],
        "accession": ytd["accn"],
        "components": [_component(ytd), _component(fiscal), _component(prior)],
    }


def _sum_quarters(rows: list[dict]) -> dict | None:
    by_end: dict[str, dict] = {}
    for row in rows:
        if not _is_quarter(row):
            continue
        current = by_end.get(row["end"])
        if current is None or _better(row, current):
            by_end[row["end"]] = row
    ordered = [by_end[key] for key in sorted(by_end)]
    if len(ordered) < 4:
        return None
    last = ordered[-4:]
    ends = [_parse_date(row["end"]) for row in last]
    if any(item is None for item in ends):
        return None
    for prev, nxt in zip(ends, ends[1:]):
        gap = (nxt - prev).days
        if gap < 70 or gap > 140:
            return None
    value = sum(row["val"] for row in last)
    return {
        "ok": True,
        "provisional": False,
        "value": value,
        "method": "sum_four_quarters",
        "period_end": last[-1]["end"],
        "accession": last[-1]["accn"],
        "components": [_component(row) for row in last],
    }


def _is_instant_row(row: dict) -> bool:
    if not row.get("start"):
        return True
    days = _days(row)
    return days is not None and days <= 1


def select_instant(facts: dict, tag: str, as_of: date, taxonomy: str = "us-gaap") -> dict | None:
    rows = [row for row in _as_of_rows(_rows(facts, tag, taxonomy), as_of) if _is_instant_row(row)]
    if not rows:
        return None
    latest_end = max(row["end"] for row in rows)
    at_end = [row for row in rows if row["end"] == latest_end]
    chosen = max(at_end, key=lambda row: (row["filed"], row["accn"]))
    return _component(chosen) | {"value": chosen["val"]}


def balance_sheet_date(facts: dict, as_of: date) -> str | None:
    assets = select_instant(facts, "Assets", as_of)
    if assets is None:
        current = select_instant(facts, "AssetsCurrent", as_of)
        return None if current is None else current["end"]
    return assets["end"]


def _on_date(facts: dict, tag: str, end: str, as_of: date, taxonomy: str = "us-gaap") -> dict | None:
    rows = [
        row
        for row in _as_of_rows(_rows(facts, tag, taxonomy), as_of)
        if row["end"] == end and _is_instant_row(row)
    ]
    if not rows:
        return None
    chosen = max(rows, key=lambda row: (row["filed"], row["accn"]))
    return _component(chosen) | {"value": chosen["val"]}


def select_debt(facts: dict, as_of: date, end: str | None = None) -> dict:
    end = end or balance_sheet_date(facts, as_of)
    if end is None:
        return {"ok": False, "reason": "balance_sheet_missing"}
    present = {}
    for tag in (
        "DebtCurrent",
        "LongTermDebtNoncurrent",
        "LongTermDebtCurrent",
        "LongTermDebt",
        "ShortTermBorrowings",
        "CommercialPaper",
        "LongTermDebtAndCapitalLeaseObligations",
    ):
        item = _on_date(facts, tag, end, as_of)
        if item is not None:
            present[tag] = item
    components = []

    def add(tag: str) -> None:
        if tag in present:
            components.append(present[tag])

    if "DebtCurrent" in present or "LongTermDebtNoncurrent" in present:
        add("DebtCurrent")
        add("LongTermDebtNoncurrent")
        if "DebtCurrent" not in present:
            add("LongTermDebtCurrent")
            add("ShortTermBorrowings")
            add("CommercialPaper")
        source = "current_plus_noncurrent"
    elif "LongTermDebt" in present:
        add("LongTermDebt")
        add("ShortTermBorrowings")
        add("CommercialPaper")
        source = "long_term_debt_total"
    elif "LongTermDebtAndCapitalLeaseObligations" in present:
        add("LongTermDebtAndCapitalLeaseObligations")
        source = "debt_and_leases"
    elif present:
        for tag in ("LongTermDebtCurrent", "ShortTermBorrowings", "CommercialPaper"):
            add(tag)
        source = "short_term_only"
    else:
        return {
            "ok": True,
            "value": 0.0,
            "source": "no_debt_tag",
            "end": end,
            "components": [],
        }
    return {
        "ok": True,
        "value": sum(item["value"] for item in components),
        "source": source,
        "end": end,
        "components": components,
    }


def select_cash(facts: dict, as_of: date, end: str | None = None) -> dict:
    end = end or balance_sheet_date(facts, as_of)
    if end is None:
        return {"ok": False, "reason": "balance_sheet_missing"}
    for tag in ("CashAndCashEquivalentsAtCarryingValue", "Cash"):
        item = _on_date(facts, tag, end, as_of)
        if item is not None:
            return {"ok": True, "value": item["value"], "source": tag, "end": end, "component": item}
    return {"ok": False, "reason": "cash_missing", "end": end}


def select_shares(facts: dict, as_of: date) -> dict:
    """Cover-page or common-stock shares. Weighted averages are not outstanding shares."""
    for taxonomy, tag in (
        ("dei", "EntityCommonStockSharesOutstanding"),
        ("us-gaap", "CommonStockSharesOutstanding"),
    ):
        item = select_instant(facts, tag, as_of, taxonomy=taxonomy)
        if item is not None and item["value"] > 0:
            return {"ok": True, "provisional": False, "value": item["value"], "source": f"{taxonomy}:{tag}", "component": item}
    return {"ok": False, "reason": "shares_outstanding_missing"}


def select_point(facts: dict, tag: str, as_of: date, end: str) -> dict | None:
    return _on_date(facts, tag, end, as_of)


def annual_facts(facts: dict, tag: str, as_of: date) -> list[dict]:
    rows = [row for row in _as_of_rows(_rows(facts, tag), as_of) if _is_year(row)]
    rows.sort(key=lambda row: row["end"])
    # One fact per fiscal year end.
    by_end: dict[str, dict] = {}
    for row in rows:
        current = by_end.get(row["end"])
        if current is None or _better(row, current):
            by_end[row["end"]] = row
    ordered = [by_end[key] for key in sorted(by_end)]
    return ordered


def instant_on_or_before(facts: dict, tag: str, as_of: date, end: str, taxonomy: str = "us-gaap") -> dict | None:
    """Latest instant on or before `end` (used when a tag is missing on the exact year end)."""
    rows = [
        row
        for row in _as_of_rows(_rows(facts, tag, taxonomy), as_of)
        if _is_instant_row(row) and row["end"] <= end
    ]
    if not rows:
        return None
    # Prefer the exact date, else the closest prior date within 7 days.
    exact = [row for row in rows if row["end"] == end]
    pool = exact or [row for row in rows if 0 <= (_parse_date(end) - _parse_date(row["end"])).days <= 7]
    if not pool:
        return None
    chosen = max(pool, key=lambda row: (row["end"], row["filed"], row["accn"]))
    return _component(chosen) | {"value": chosen["val"]}


def long_term_debt_value(facts: dict, as_of: date, end: str) -> tuple[float, dict | None, str]:
    """Long-term debt for the leverage signal. Missing tags are zero."""
    for tag in ("LongTermDebtNoncurrent", "LongTermDebt", "LongTermDebtAndCapitalLeaseObligations"):
        item = instant_on_or_before(facts, tag, as_of, end)
        if item is not None:
            return item["value"], item, tag
    return 0.0, None, "no_debt_tag"


def revenue_pair(facts: dict, as_of: date, end_t: str, end_t1: str) -> tuple[str, dict, dict] | None:
    for tag in REVENUE_PRIORITY:
        years = {row["end"]: row for row in annual_facts(facts, tag, as_of)}
        if end_t in years and end_t1 in years:
            return tag, years[end_t], years[end_t1]
    return None


def gross_profit_value(facts: dict, as_of: date, end: str, revenue: float) -> tuple[float, dict] | None:
    years = {row["end"]: row for row in annual_facts(facts, "GrossProfit", as_of)}
    if end in years:
        row = years[end]
        return row["val"], _component(row)
    for tag in GROSS_COST_PRIORITY:
        cost_years = {row["end"]: row for row in annual_facts(facts, tag, as_of)}
        if end in cost_years:
            row = cost_years[end]
            return revenue - row["val"], _component(row) | {"derived": f"revenue_minus_{tag}"}
    return None


def issuance_value(facts: dict, as_of: date, end: str) -> tuple[float | None, dict | None]:
    for tag in ("ProceedsFromIssuanceOfCommonStock", "ProceedsFromIssuanceOfShares"):
        years = {row["end"]: row for row in annual_facts(facts, tag, as_of)}
        if end in years:
            row = years[end]
            return row["val"], _component(row)
    return None, None


def staleness_days(period_end: str, as_of: date) -> int:
    end = _parse_date(period_end)
    if end is None:
        return 10**6
    return (as_of - end).days
