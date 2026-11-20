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

Total debt, on one balance-sheet date, avoids adding a total and its parts.
A reported total wins. Components are summed only when no total is present.

1. DebtCurrent + LongTermDebtNoncurrent when both are present.
2. Else LongTermDebtNoncurrent plus LongTermDebtCurrent, ShortTermBorrowings,
   and CommercialPaper.
3. Else LongTermDebtAndFinanceLeaseObligations or
   LongTermDebtAndCapitalLeaseObligations (one figure; the larger one if both
   are present). These totals already include finance leases.
4. Else LongTermDebt plus ShortTermBorrowings and CommercialPaper.
   Notes, loans, and credit lines are not added on top of this total.
5. Else DebtInstrumentCarryingAmount when it covers the components below.
6. Else the non-overlapping components on that same date:
   loans (LongTermLoansPayable or the current/noncurrent split, plus
   LoansPayableCurrent), notes (LongTermNotesPayable or NotesPayable, plus
   NotesPayableCurrent; senior notes only when they are not already inside
   that notes figure), convertible notes and convertible debt, drawn credit
   lines (LineOfCredit or LongTermLineOfCredit, plus LinesOfCreditCurrent;
   two line tags for the same drawn balance are not both added),
   OtherLongTermDebt, and SecuredDebt / UnsecuredDebt only when no named
   instrument above is present. FinanceLeaseLiability (or its current and
   noncurrent parts, or the older capital-lease tags) is included here.
   Facility capacity and unused commitments are not debt.
7. A zero value on one of those tags is real zero debt when every other debt
   tag on the date is also zero or absent.
8. When the date has no debt tag, debt is not set to 0. The row is excluded
   as debt_unconfirmed. Zero is kept when the filing reports a debt tag of 0,
   or when no debt tag is on the date and Liabilities minus accounts payable,
   accrued liabilities, contract liabilities or deferred revenue, operating
   leases, and noncurrent deferred tax is within the greater of 1,000,000 and
   2 percent of Liabilities (source liabilities_imply_no_debt).

Cash is CashAndCashEquivalentsAtCarryingValue, else Cash. The restricted-cash
roll-up is not used.
"""

from __future__ import annotations

import re
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

# Balance-sheet debt concepts. A total is preferred over its parts.
DEBT_TAGS = (
    "DebtCurrent",
    "LongTermDebtNoncurrent",
    "LongTermDebtCurrent",
    "LongTermDebt",
    "ShortTermBorrowings",
    "CommercialPaper",
    "LongTermDebtAndCapitalLeaseObligations",
    "LongTermDebtAndFinanceLeaseObligations",
    "DebtInstrumentCarryingAmount",
    "LongTermNotesPayable",
    "NotesPayable",
    "NotesPayableCurrent",
    "NotesPayableNoncurrent",
    "LoansPayable",
    "LoansPayableCurrent",
    "LongTermLoansPayable",
    "LongTermLoansPayableCurrent",
    "LongTermLoansPayableNoncurrent",
    "LongTermLineOfCredit",
    "LineOfCredit",
    "LinesOfCreditCurrent",
    "ConvertibleNotesPayable",
    "ConvertibleNotesPayableCurrent",
    "ConvertibleLongTermNotesPayable",
    "ConvertibleDebt",
    "ConvertibleDebtCurrent",
    "ConvertibleDebtNoncurrent",
    "SeniorNotes",
    "SeniorLongTermNotes",
    "UnsecuredDebt",
    "SecuredDebt",
    "OtherLongTermDebt",
    "FinanceLeaseLiability",
    "FinanceLeaseLiabilityCurrent",
    "FinanceLeaseLiabilityNoncurrent",
    "CapitalLeaseObligations",
    "CapitalLeaseObligationsCurrent",
    "CapitalLeaseObligationsNoncurrent",
)

# Operating liabilities used only to judge whether an untagged balance sheet
# has room left for debt. Other liabilities are not in this list.
NONDEBT_LIABILITY_TAGS = (
    "Liabilities",
    "AccountsPayableCurrent",
    "AccountsPayableAndAccruedLiabilitiesCurrent",
    "AccruedLiabilitiesCurrent",
    "ContractWithCustomerLiability",
    "ContractWithCustomerLiabilityCurrent",
    "ContractWithCustomerLiabilityNoncurrent",
    "DeferredRevenue",
    "DeferredRevenueCurrent",
    "DeferredRevenueNoncurrent",
    "OperatingLeaseLiability",
    "OperatingLeaseLiabilityCurrent",
    "OperatingLeaseLiabilityNoncurrent",
    "DeferredTaxLiabilitiesNoncurrent",
)

INSTANT_TAGS = {
    "Assets",
    "AssetsCurrent",
    "LiabilitiesCurrent",
    "PropertyPlantAndEquipmentNet",
    "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization",
    "CashAndCashEquivalentsAtCarryingValue",
    "Cash",
    "CommonStockSharesOutstanding",
} | set(DEBT_TAGS) | set(NONDEBT_LIABILITY_TAGS)

DEI_INSTANT = {"EntityCommonStockSharesOutstanding"}

EXTRACT_VERSION = 3

# Residual liabilities at or under this band count as no debt.
_NO_DEBT_RESIDUAL_RATIO = 0.02
_NO_DEBT_RESIDUAL_FLOOR = 1_000_000
# Share count after the balance sheet, more than this far below the prior instant.
SHARE_DECLINE_AFTER_BS = 0.10

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


def _present_map(facts: dict, tags: tuple[str, ...] | set[str], end: str, as_of: date) -> dict[str, dict]:
    present = {}
    for tag in tags:
        item = _on_date(facts, tag, end, as_of)
        if item is not None:
            present[tag] = item
    return present


def _outside_positive(present: dict[str, dict], included: set[str]) -> bool:
    return any(item["value"] > 0 for tag, item in present.items() if tag not in included)


def _covers(total: dict | None, parts: list[dict]) -> bool:
    """True when one reported total already includes the parts."""
    if total is None:
        return False
    if not parts:
        return True
    part_sum = sum(item["value"] for item in parts)
    return total["value"] + 1 >= part_sum * 0.98


def _one(item: dict | None) -> list[dict]:
    return [] if item is None else [item]


def _loan_components(present: dict[str, dict]) -> list[dict]:
    split_non = present.get("LongTermLoansPayableNoncurrent")
    split_cur = present.get("LongTermLoansPayableCurrent")
    if split_non is not None or split_cur is not None:
        parts = _one(split_non) + _one(split_cur)
    else:
        parts = _one(present.get("LongTermLoansPayable"))
    current = present.get("LoansPayableCurrent")
    if (
        current is not None
        and split_cur is None
        and not any(abs(current["value"] - item["value"]) <= 1 for item in parts)
    ):
        parts.append(current)
    total = present.get("LoansPayable")
    if _covers(total, parts):
        return _one(total)
    return parts


def _note_components(present: dict[str, dict]) -> list[dict]:
    if "LongTermNotesPayable" in present:
        parts = [present["LongTermNotesPayable"]]
    else:
        parts = _one(present.get("NotesPayableNoncurrent"))
    if "NotesPayableCurrent" in present:
        parts.append(present["NotesPayableCurrent"])
    total = present.get("NotesPayable")
    if _covers(total, parts):
        chosen = _one(total)
    else:
        chosen = parts
    seniors = [present[tag] for tag in ("SeniorLongTermNotes", "SeniorNotes") if tag in present]
    if not seniors:
        return chosen
    senior = max(seniors, key=lambda item: item["value"])
    note_sum = sum(item["value"] for item in chosen)
    senior_covers = senior["value"] >= note_sum and senior["value"] + 1 >= note_sum * 0.98
    if not chosen or senior_covers:
        return [senior]
    return chosen


def _convertible_components(present: dict[str, dict]) -> list[dict]:
    split = [present[tag] for tag in ("ConvertibleDebtCurrent", "ConvertibleDebtNoncurrent") if tag in present]
    total = present.get("ConvertibleDebt")
    if _covers(total, split):
        return _one(total)
    if split:
        return split
    note_parts = [
        present[tag]
        for tag in ("ConvertibleLongTermNotesPayable", "ConvertibleNotesPayableCurrent")
        if tag in present
    ]
    notes = present.get("ConvertibleNotesPayable")
    if _covers(notes, note_parts):
        return _one(notes)
    return note_parts


def _credit_components(present: dict[str, dict]) -> list[dict]:
    """Drawn lines only. Two tags for the same drawn balance are not added twice."""
    parts = [present[tag] for tag in ("LongTermLineOfCredit", "LinesOfCreditCurrent") if tag in present]
    line = present.get("LineOfCredit")
    if line is None:
        return parts
    if _covers(line, parts):
        return [line]
    if any(abs(line["value"] - item["value"]) <= max(1.0, 0.02 * abs(item["value"])) for item in parts):
        return parts
    return parts + [line]


def _lease_components(present: dict[str, dict]) -> list[dict]:
    parts = [
        present[tag]
        for tag in ("FinanceLeaseLiabilityCurrent", "FinanceLeaseLiabilityNoncurrent")
        if tag in present
    ]
    total = present.get("FinanceLeaseLiability")
    if _covers(total, parts):
        return _one(total)
    if parts:
        return parts
    legacy_parts = [
        present[tag]
        for tag in ("CapitalLeaseObligationsCurrent", "CapitalLeaseObligationsNoncurrent")
        if tag in present
    ]
    legacy = present.get("CapitalLeaseObligations")
    if _covers(legacy, legacy_parts):
        return _one(legacy)
    return legacy_parts


def _short_components(present: dict[str, dict], named_current: bool) -> list[dict]:
    short_tags = ("DebtCurrent", "LongTermDebtCurrent", "ShortTermBorrowings", "CommercialPaper")
    if not named_current:
        return [present[tag] for tag in short_tags if tag in present]
    if "DebtCurrent" in present:
        return []
    return [present[tag] for tag in ("ShortTermBorrowings", "CommercialPaper") if tag in present]


def _component_debt(present: dict[str, dict]) -> tuple[list[dict], str]:
    loans = _loan_components(present)
    notes = _note_components(present)
    convertible = _convertible_components(present)
    credit = _credit_components(present)
    named = loans + notes + convertible + credit
    named_current = any(
        tag in {item["concept"] for item in named}
        for tag in (
            "LoansPayableCurrent",
            "LongTermLoansPayableCurrent",
            "NotesPayableCurrent",
            "ConvertibleDebtCurrent",
            "ConvertibleNotesPayableCurrent",
            "LinesOfCreditCurrent",
        )
    )
    other = _one(present.get("OtherLongTermDebt"))
    if not any(item["value"] > 0 for item in named):
        other.extend(present[tag] for tag in ("SecuredDebt", "UnsecuredDebt") if tag in present)
    leases = _lease_components(present)
    short = _short_components(present, named_current)
    components = named + other + leases + short
    positive = [item for item in components if item["value"] > 0]
    if positive:
        source = "component_sum"
        if not named and not other and not leases:
            source = "short_term_only"
        return components, source
    if components:
        return components, "explicit_zero"
    carrying = present.get("DebtInstrumentCarryingAmount")
    if carrying is not None:
        return [carrying], "debt_instrument_carrying_amount"
    return [], ""


def _explained_nondebt(facts: dict, as_of: date, end: str) -> float | None:
    """Sum of operating liabilities. None when Liabilities itself is absent."""
    present = _present_map(facts, NONDEBT_LIABILITY_TAGS, end, as_of)
    if "Liabilities" not in present:
        return None

    def family(total: str, parts: tuple[str, ...]) -> float:
        if total in present:
            return float(present[total]["value"])
        return sum(float(present[tag]["value"]) for tag in parts if tag in present)

    if "AccountsPayableAndAccruedLiabilitiesCurrent" in present:
        payables = float(present["AccountsPayableAndAccruedLiabilitiesCurrent"]["value"])
    else:
        payables = family("AccountsPayableCurrent", ()) + family("AccruedLiabilitiesCurrent", ())
    contracts = family(
        "ContractWithCustomerLiability",
        ("ContractWithCustomerLiabilityCurrent", "ContractWithCustomerLiabilityNoncurrent"),
    )
    if contracts == 0:
        contracts = family("DeferredRevenue", ("DeferredRevenueCurrent", "DeferredRevenueNoncurrent"))
    leases = family(
        "OperatingLeaseLiability",
        ("OperatingLeaseLiabilityCurrent", "OperatingLeaseLiabilityNoncurrent"),
    )
    tax = family("DeferredTaxLiabilitiesNoncurrent", ())
    return payables + contracts + leases + tax


def _liabilities_imply_no_debt(facts: dict, as_of: date, end: str) -> tuple[bool, str]:
    liabilities = _on_date(facts, "Liabilities", end, as_of)
    if liabilities is None:
        return False, f"no debt tag on {end}; Liabilities missing"
    explained = _explained_nondebt(facts, as_of, end)
    total = float(liabilities["value"])
    covered = 0.0 if explained is None else explained
    residual = total - covered
    tolerance = max(_NO_DEBT_RESIDUAL_FLOOR, abs(total) * _NO_DEBT_RESIDUAL_RATIO)
    detail = f"liabilities {total:.0f} explained {covered:.0f} residual {residual:.0f}"
    if -tolerance <= residual <= tolerance:
        return True, detail
    return False, f"no debt tag on {end}; {detail}"


def select_debt(facts: dict, as_of: date, end: str | None = None) -> dict:
    """Debt on one balance-sheet date.

    Reported totals win over notes, loans, credit lines, and convertible
    debt. A missing tag is not zero: the caller excludes ``debt_unconfirmed``
    unless the filing reports zero or operating liabilities explain Liabilities.
    """
    end = end or balance_sheet_date(facts, as_of)
    if end is None:
        return {"ok": False, "reason": "balance_sheet_missing"}
    present = _present_map(facts, DEBT_TAGS, end, as_of)
    components: list[dict] = []
    source = ""

    def add(*tags: str) -> None:
        for tag in tags:
            if tag in present:
                components.append(present[tag])

    pair = {"DebtCurrent", "LongTermDebtNoncurrent"}
    if pair <= present.keys() and (
        present["DebtCurrent"]["value"] + present["LongTermDebtNoncurrent"]["value"] > 0
        or not _outside_positive(present, pair | {"LongTermDebt", "LongTermDebtCurrent"})
    ):
        add("DebtCurrent", "LongTermDebtNoncurrent")
        source = "current_plus_noncurrent"
    elif "LongTermDebtNoncurrent" in present and (
        present["LongTermDebtNoncurrent"]["value"] > 0
        or not _outside_positive(
            present,
            {"LongTermDebtNoncurrent", "LongTermDebtCurrent", "ShortTermBorrowings", "CommercialPaper", "LongTermDebt"},
        )
    ):
        add("LongTermDebtNoncurrent", "LongTermDebtCurrent", "ShortTermBorrowings", "CommercialPaper")
        source = "current_plus_noncurrent"
    elif "LongTermDebtAndFinanceLeaseObligations" in present or "LongTermDebtAndCapitalLeaseObligations" in present:
        lease_tags = {"LongTermDebtAndFinanceLeaseObligations", "LongTermDebtAndCapitalLeaseObligations"}
        chosen = max(
            (present[tag] for tag in lease_tags if tag in present),
            key=lambda item: item["value"],
        )
        if chosen["value"] > 0 or not _outside_positive(present, lease_tags | {"DebtCurrent"}):
            components.append(chosen)
            source = "debt_and_leases"
    if not source and "LongTermDebt" in present and (
        present["LongTermDebt"]["value"] > 0
        or not _outside_positive(
            present, {"LongTermDebt", "ShortTermBorrowings", "CommercialPaper", "LongTermDebtCurrent"}
        )
    ):
        add("LongTermDebt", "ShortTermBorrowings", "CommercialPaper")
        source = "long_term_debt_total"

    if not source:
        components, source = _component_debt(present)
        carrying = present.get("DebtInstrumentCarryingAmount")
        if carrying is not None and source not in {"", "explicit_zero"}:
            comp_sum = sum(item["value"] for item in components if item["concept"] != "DebtInstrumentCarryingAmount")
            if _covers(carrying, [item for item in components if item["concept"] != "DebtInstrumentCarryingAmount"]) and (
                not comp_sum or carrying["value"] + 1 >= comp_sum * 0.98
            ):
                components = [carrying]
                source = "debt_instrument_carrying_amount"

    if source:
        value = sum(item["value"] for item in components)
        if value == 0 and source != "explicit_zero":
            source = "explicit_zero"
        return {
            "ok": True,
            "value": value,
            "source": source,
            "end": end,
            "components": components,
        }

    implied, detail = _liabilities_imply_no_debt(facts, as_of, end)
    if implied:
        return {
            "ok": True,
            "value": 0.0,
            "source": "liabilities_imply_no_debt",
            "end": end,
            "components": [],
            "detail": detail,
        }
    return {
        "ok": False,
        "reason": "debt_unconfirmed",
        "end": end,
        "detail": detail,
        "source": "debt_unconfirmed",
    }


PPE_TAGS = (
    "PropertyPlantAndEquipmentNet",
    "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization",
)


def select_net_fixed_assets(facts: dict, as_of: date, end: str) -> dict | None:
    """Net PPE on the balance-sheet date. An older fact is not reused."""
    for tag in PPE_TAGS:
        item = _on_date(facts, tag, end, as_of)
        if item is not None:
            return item
    return None


def select_cash(facts: dict, as_of: date, end: str | None = None) -> dict:
    end = end or balance_sheet_date(facts, as_of)
    if end is None:
        return {"ok": False, "reason": "balance_sheet_missing"}
    for tag in ("CashAndCashEquivalentsAtCarryingValue", "Cash"):
        item = _on_date(facts, tag, end, as_of)
        if item is not None:
            return {"ok": True, "value": item["value"], "source": tag, "end": end, "component": item}
    return {"ok": False, "reason": "cash_missing", "end": end}


# A 10-for-1 split stays. A 500x jump is the known "three extra zeros" scale error.
SHARE_SCALE_RATIO = 500

_SHARE_SPECS = (
    ("dei", "EntityCommonStockSharesOutstanding", "instant", 3),
    ("us-gaap", "CommonStockSharesOutstanding", "instant", 2),
    ("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic", "duration", 1),
)

_DEBT_MEMBER = re.compile(r"(Note|Bond|Debenture|Debt|Preferred|Warrant|Right|Unit)", re.IGNORECASE)
_EQUITY_MEMBER = re.compile(
    r"(CommonClass|Class[A-Z]Member|Voting|Nonvoting|Ordinary|CommonStockMember|CapitalStock)",
    re.IGNORECASE,
)
_STOCK_MEMBER = re.compile(r"(Common|Ordinary|Stock|Share|Class)", re.IGNORECASE)
_EQUITY_TITLE_SKIP = ("note", "bond", "debenture", "debt", "warrant", "preferred", "depositary", "right")
_CLASS_HINT = re.compile(r"(class\s+[a-c]\b|voting|non-voting|nonvoting|series\s+[a-z0-9])", re.IGNORECASE)


def _member_key(member: str) -> str:
    return (member or "").split(":")[-1].strip()


def _is_equity_member(member: str) -> bool:
    if not member or _DEBT_MEMBER.search(member):
        return False
    return _EQUITY_MEMBER.search(member) is not None


def _is_share_class_member(member: str, dimension: str = "") -> bool:
    """Any class-axis member counts, including a custom extension name.

    A member on a different axis (a subsidiary or a product) is not a class.
    When the filing did not carry the axis name, a stock-like member name is
    enough. Notes, preferred stock, warrants, and units are not classes.
    """
    if not member or _DEBT_MEMBER.search(member):
        return False
    dim = (dimension or "").split(":")[-1]
    if dim:
        return dim.endswith("ClassOfStockAxis")
    if _is_equity_member(member):
        return True
    return _STOCK_MEMBER.search(_member_key(member)) is not None


def _is_equity_title(text: str) -> bool:
    low = re.sub(r"\s+", " ", text or "").strip().lower()
    if not low or any(word in low for word in _EQUITY_TITLE_SKIP):
        return False
    if _CLASS_HINT.search(low) is None:
        return False
    return ("common stock" in low) or ("ordinary share" in low) or ("ordinary stock" in low) or ("common share" in low)


def _title_key(text: str) -> str:
    head = (text or "").split(",")[0]
    return re.sub(r"\s+", " ", head).strip().lower()


def _candidate_from_row(row: dict, source: str, kind: str, rank: int) -> dict | None:
    try:
        value = float(row.get("value", row.get("val")))
    except (TypeError, ValueError):
        return None
    if value <= 0 or not row.get("end") or not row.get("filed"):
        return None
    start = row.get("start")
    duration_days = 0
    if kind == "duration":
        if not start:
            return None
        duration_days = (_parse_date(row["end"]) - _parse_date(start)).days
        if duration_days < 70 or duration_days > 400:
            return None
    elif not _is_instant_row(row):
        return None
    tag = source.split(":", 1)[-1]
    return {
        "tag": tag,
        "source": source,
        "kind": kind,
        "rank": rank,
        "start": start,
        "end": row["end"],
        "filed": row["filed"],
        "val": value,
        "value": value,
        "accn": str(row.get("accn") or ""),
        "form": str(row.get("form") or ""),
        "fp": str(row.get("fp") or ""),
        "fy": row.get("fy"),
        "unit": row.get("unit") or "shares",
        "member": str(row.get("member") or ""),
        "dimension": str(row.get("dimension") or ""),
        "duration_days": duration_days,
    }


def _share_candidates(facts: dict, as_of: date, extras: list[dict] | None) -> list[dict]:
    found = []
    for taxonomy, tag, kind, rank in _SHARE_SPECS:
        for row in _as_of_rows(_rows(facts, tag, taxonomy), as_of):
            item = _candidate_from_row(row, f"{taxonomy}:{tag}", kind, rank)
            if item is not None:
                found.append(item)
    for row in extras or []:
        if row.get("tag") == "Security12bTitle":
            continue
        source = str(row.get("source") or "")
        if not source and row.get("taxonomy") and row.get("tag"):
            source = f"{row['taxonomy']}:{row['tag']}"
        kind = str(row.get("kind") or "instant")
        rank = {"instant": 3, "duration": 1}.get(kind, 1)
        for taxonomy, tag, spec_kind, spec_rank in _SHARE_SPECS:
            if source == f"{taxonomy}:{tag}":
                rank = spec_rank
                kind = spec_kind
                break
        item = _candidate_from_row(row, source, kind, rank)
        if item is None:
            continue
        if item["filed"] > as_of.isoformat() or item["end"] > as_of.isoformat():
            continue
        found.append(item)
    return found


def _drop_scaled_share_outliers(candidates: list[dict]) -> list[dict]:
    """Drop a weighted-average count that is hundreds of times the latest instant count."""
    instants = [row for row in candidates if row["kind"] == "instant" and row["value"] > 0]
    if not instants:
        return candidates
    anchor = max(instants, key=lambda row: (row["end"], row["filed"], row["accn"]))
    kept = []
    for row in candidates:
        if row["kind"] != "instant" and anchor["value"] > 0:
            ratio = row["value"] / anchor["value"]
            if ratio >= SHARE_SCALE_RATIO or ratio <= 1 / SHARE_SCALE_RATIO:
                continue
        kept.append(row)
    return kept


def _equity_classes(fresh: list[dict]) -> list[dict]:
    instants = [
        row
        for row in fresh
        if row["kind"] == "instant"
        and row["source"] in {
            "dei:EntityCommonStockSharesOutstanding",
            "us-gaap:CommonStockSharesOutstanding",
        }
        and _is_share_class_member(row.get("member") or "", row.get("dimension") or "")
        and row["value"] > 0
    ]
    if not instants:
        return []
    newest = max(row["end"] for row in instants)
    newest_day = _parse_date(newest)
    by_member: dict[str, dict] = {}
    for row in instants:
        end_day = _parse_date(row["end"])
        if newest_day is None or end_day is None or (newest_day - end_day).days > 45:
            continue
        key = _member_key(row["member"])
        current = by_member.get(key)
        if current is None or (row["end"], row["filed"], row["accn"]) > (current["end"], current["filed"], current["accn"]):
            by_member[key] = row
    return [by_member[key] for key in sorted(by_member)]


def equity_titles(extras: list[dict] | None) -> list[str]:
    """Distinct common or ordinary class titles. Notes and preferred stock are not classes."""
    found: dict[str, str] = {}
    for row in extras or []:
        if row.get("tag") != "Security12bTitle":
            continue
        text = str(row.get("text") or "").strip()
        if not _is_equity_title(text):
            continue
        found.setdefault(_title_key(text), text)
    return [found[key] for key in sorted(found)]


def _class_detail(classes: list[dict]) -> str:
    parts = []
    for row in classes:
        value = row["value"]
        shown = int(value) if float(value).is_integer() else value
        parts.append(f"{row['member']} {shown} end {row['end']} source {row['source']}")
    return "; ".join(parts)


def _collapse_class_total(classes: list[dict]) -> list[dict]:
    """Drop parts when one member already equals the sum of the others."""
    if len(classes) < 3:
        return classes
    for row in classes:
        others = sum(item["value"] for item in classes if item is not row)
        if others > 0 and abs(row["value"] - others) / others <= 0.01:
            return [row]
    return classes


def _summed_class_shares(classes: list[dict], dei_fresh: bool) -> dict:
    total = sum(row["value"] for row in classes)
    newest = max(classes, key=lambda row: (row["end"], row["filed"], row["accn"]))
    return {
        "ok": True,
        "provisional": False,
        "value": total,
        "source": "class_sum",
        "kind": "instant",
        "end": newest["end"],
        "component": {
            "concept": newest["tag"],
            "start": None,
            "end": newest["end"],
            "value": total,
            "accn": newest["accn"],
            "form": newest["form"],
            "filed": newest["filed"],
            "fp": newest["fp"],
            "fy": newest["fy"],
            "member": "+".join(row["member"] for row in classes),
        },
        "classes": classes,
        "dei_stale_or_missing": not dei_fresh,
    }


def select_shares(
    facts: dict,
    as_of: date,
    max_age_days: int = 180,
    extras: list[dict] | None = None,
    sum_classes: bool = False,
) -> dict:
    """Newest fresh share count.

    An older ``dei:EntityCommonStockSharesOutstanding`` fact never wins over a
    newer count. Cover-page and common-stock instants outrank a weighted
    average when the period end is the same. Counts older than ``max_age_days``
    are rejected. Two or more positive values on the class axis, whatever the
    member names, are never reduced to one member. With ``sum_classes`` they
    are added (one listed ticker prices the whole share base). Otherwise the
    reason is ``multi_class_shares_unsplit``, as are two class titles.
    """
    candidates = _drop_scaled_share_outliers(_share_candidates(facts, as_of, extras))
    fresh = [row for row in candidates if staleness_days(row["end"], as_of) <= max_age_days]
    dei_fresh = [
        row
        for row in fresh
        if row["source"] == "dei:EntityCommonStockSharesOutstanding" and row["kind"] == "instant" and not row.get("member")
    ]
    raw_classes = _equity_classes(fresh)
    classes = _collapse_class_total(raw_classes)
    if len(raw_classes) >= 2 and len(classes) == 1:
        dropped = {_member_key(row["member"]) for row in raw_classes if row is not classes[0]}
        fresh = [row for row in fresh if _member_key(row.get("member") or "") not in dropped]
    titles = equity_titles(extras)
    if len(classes) >= 2:
        if sum_classes:
            return _summed_class_shares(classes, bool(dei_fresh))
        return {
            "ok": False,
            "reason": "multi_class_shares_unsplit",
            "detail": _class_detail(classes),
            "classes": classes,
            "dei_stale_or_missing": True,
        }
    if len(titles) >= 2:
        return {
            "ok": False,
            "reason": "multi_class_shares_unsplit",
            "detail": "titles " + " | ".join(titles),
            "dei_stale_or_missing": not dei_fresh,
        }
    if not fresh:
        if not candidates:
            return {"ok": False, "reason": "shares_outstanding_missing", "detail": "", "dei_stale_or_missing": True}
        newest = max(candidates, key=lambda row: (row["end"], row["filed"], row["accn"]))
        age = staleness_days(newest["end"], as_of)
        return {
            "ok": False,
            "reason": "stale_shares",
            "detail": f"newest {newest['source']} end {newest['end']} age_days {age}",
            "dei_stale_or_missing": True,
            "newest_end": newest["end"],
            "newest_source": newest["source"],
        }
    best_end = max(row["end"] for row in fresh)
    pool = [row for row in fresh if row["end"] == best_end]
    chosen = max(pool, key=lambda row: (row["rank"], row["duration_days"], row["filed"], row["accn"]))
    component = {
        "concept": chosen["tag"],
        "start": chosen["start"],
        "end": chosen["end"],
        "value": chosen["value"],
        "accn": chosen["accn"],
        "form": chosen["form"],
        "filed": chosen["filed"],
        "fp": chosen["fp"],
        "fy": chosen["fy"],
        "member": chosen["member"],
    }
    return {
        "ok": True,
        "provisional": False,
        "value": chosen["value"],
        "source": chosen["source"],
        "kind": "weighted_average" if chosen["kind"] == "duration" else "instant",
        "end": chosen["end"],
        "component": component,
        "dei_stale_or_missing": not dei_fresh,
    }


def large_share_decline_after_balance_sheet(
    facts: dict,
    as_of: date,
    shares_value: float,
    shares_end: str | None,
    balance_sheet_end: str | None,
) -> bool:
    """True when the share date is after the balance sheet and the count fell more than 10 percent.

    The comparison uses the latest instant common-share count on or before the
    balance-sheet date, within 120 days. A tender or repurchase after the
    statement date sets the flag. The row stays in the ranking.
    """
    if not shares_end or not balance_sheet_end or shares_end <= balance_sheet_end:
        return False
    if shares_value is None or float(shares_value) <= 0:
        return False
    bs = _parse_date(balance_sheet_end)
    if bs is None:
        return False
    priors = []
    for taxonomy, tag in (
        ("dei", "EntityCommonStockSharesOutstanding"),
        ("us-gaap", "CommonStockSharesOutstanding"),
    ):
        for row in _as_of_rows(_rows(facts, tag, taxonomy), as_of):
            if row.get("member") or not _is_instant_row(row) or row["val"] <= 0:
                continue
            end = _parse_date(row["end"])
            if end is None or end > bs or (bs - end).days > 120:
                continue
            priors.append(row)
    if not priors:
        return False
    latest = max(row["end"] for row in priors)
    prior = max(row["val"] for row in priors if row["end"] == latest)
    return (prior - float(shares_value)) / prior > SHARE_DECLINE_AFTER_BS


def latest_domestic_row(facts: dict, as_of: date) -> dict | None:
    """Latest 10-K or 10-Q fact filed on or before ``as_of``."""
    best = None
    domestic = {"10-K", "10-K/A", "10-Q", "10-Q/A"}
    for rows in (facts.get("concepts") or {}).values():
        for row in rows:
            if row.get("form") not in domestic:
                continue
            filed = str(row.get("filed") or "")
            if not filed or filed > as_of.isoformat():
                continue
            if best is None or (filed, str(row.get("accn") or "")) > (best["filed"], best["accn"]):
                best = row
    return best


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
