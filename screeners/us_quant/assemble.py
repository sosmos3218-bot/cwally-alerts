"""Turn trimmed facts and a close into screen rows."""

from __future__ import annotations

from datetime import date

from screeners.us_quant.facts import (
    annual_facts,
    balance_sheet_date,
    gross_profit_value,
    instant_on_or_before,
    issuance_value,
    long_term_debt_value,
    revenue_pair,
    select_cash,
    select_debt,
    select_point,
    select_shares,
    select_ttm_ebit,
    staleness_days,
)
from screeners.us_quant.fscore import score_f
from screeners.us_quant.magic import earnings_yield, rank_magic, return_on_capital


def _annual_end_gap(newer: str, older: str) -> int:
    from datetime import datetime

    a = datetime.strptime(newer, "%Y-%m-%d").date()
    b = datetime.strptime(older, "%Y-%m-%d").date()
    return (a - b).days


def build_magic_inputs(facts: dict, as_of: date, price: float, limits: dict) -> dict:
    """Return ok plus a row of inputs, or a reason the issuer is left out."""
    ebit = select_ttm_ebit(facts, as_of)
    if not ebit.get("ok"):
        return {"ok": False, "reason": ebit.get("reason") or "ebit_missing", "detail": ebit.get("detail") or ""}
    if ebit.get("provisional"):
        return {"ok": False, "reason": "ebit_provisional", "detail": ebit.get("method") or ""}
    age = staleness_days(ebit["period_end"], as_of)
    if age > int(limits["max_statement_age_days"]):
        return {"ok": False, "reason": "stale_statement", "detail": f"period_end {ebit['period_end']} age_days {age}"}

    end = balance_sheet_date(facts, as_of)
    if end is None:
        return {"ok": False, "reason": "balance_sheet_missing"}
    if staleness_days(end, as_of) > int(limits["max_statement_age_days"]):
        return {"ok": False, "reason": "stale_balance_sheet", "detail": end}

    current_assets = select_point(facts, "AssetsCurrent", as_of, end)
    current_liab = select_point(facts, "LiabilitiesCurrent", as_of, end)
    if current_assets is None or current_liab is None:
        return {"ok": False, "reason": "working_capital_missing", "detail": end}
    fixed = select_point(facts, "PropertyPlantAndEquipmentNet", as_of, end)
    fixed_value = 0.0 if fixed is None else fixed["value"]
    fixed_source = "unreported_as_zero" if fixed is None else "PropertyPlantAndEquipmentNet"

    cash = select_cash(facts, as_of, end)
    if not cash.get("ok"):
        return {"ok": False, "reason": cash.get("reason") or "cash_missing", "detail": end}
    debt = select_debt(facts, as_of, end)
    if not debt.get("ok"):
        return {"ok": False, "reason": debt.get("reason") or "debt_missing"}
    shares = select_shares(facts, as_of)
    if not shares.get("ok") or shares.get("provisional"):
        return {"ok": False, "reason": "shares_outstanding_missing"}

    market_cap = price * float(shares["value"])
    try:
        ey, enterprise = earnings_yield(float(ebit["value"]), market_cap, float(debt["value"]), float(cash["value"]))
    except ZeroDivisionError:
        return {"ok": False, "reason": "enterprise_value_non_positive"}
    if enterprise <= 0:
        return {"ok": False, "reason": "enterprise_value_non_positive", "detail": enterprise}
    try:
        roc, nwc, capital = return_on_capital(
            float(ebit["value"]),
            float(current_assets["value"]),
            float(current_liab["value"]),
            float(fixed_value),
        )
    except ZeroDivisionError:
        return {"ok": False, "reason": "capital_non_positive"}
    if capital <= 0:
        return {"ok": False, "reason": "capital_non_positive", "detail": capital}

    return {
        "ok": True,
        "earnings_yield": ey,
        "roc": roc,
        "market_cap": market_cap,
        "enterprise_value": enterprise,
        "ebit": float(ebit["value"]),
        "ebit_method": ebit["method"],
        "ebit_period_end": ebit["period_end"],
        "ebit_accession": ebit["accession"],
        "ebit_components": ebit["components"],
        "total_debt": float(debt["value"]),
        "debt_source": debt["source"],
        "debt_components": debt["components"],
        "cash": float(cash["value"]),
        "cash_source": cash["source"],
        "cash_component": cash["component"],
        "shares": float(shares["value"]),
        "shares_source": shares["source"],
        "shares_component": shares["component"],
        "current_assets": float(current_assets["value"]),
        "current_assets_component": current_assets,
        "current_liabilities": float(current_liab["value"]),
        "current_liabilities_component": current_liab,
        "net_working_capital": nwc,
        "net_fixed_assets": float(fixed_value),
        "net_fixed_assets_source": fixed_source,
        "net_fixed_assets_component": fixed,
        "capital": capital,
        "balance_sheet_end": end,
        "filing_accession": ebit["accession"],
        "fiscal_period_end": ebit["period_end"],
    }


def build_fscore_inputs(facts: dict, as_of: date, max_annual_age_days: int) -> dict:
    income = annual_facts(facts, "NetIncomeLoss", as_of)
    if len(income) < 2:
        return {"ok": False, "reason": "annual_income_missing"}
    year_t = income[-1]
    year_t1 = income[-2]
    gap = _annual_end_gap(year_t["end"], year_t1["end"])
    if gap < 280 or gap > 450:
        return {"ok": False, "reason": "annual_gap", "detail": f"{year_t1['end']} -> {year_t['end']}"}
    if staleness_days(year_t["end"], as_of) > max_annual_age_days:
        return {"ok": False, "reason": "stale_annual", "detail": year_t["end"]}

    assets_t = instant_on_or_before(facts, "Assets", as_of, year_t["end"])
    assets_t1 = instant_on_or_before(facts, "Assets", as_of, year_t1["end"])
    year_t2_end = income[-3]["end"] if len(income) >= 3 else None
    assets_t2 = instant_on_or_before(facts, "Assets", as_of, year_t2_end) if year_t2_end else None
    if assets_t2 is None:
        earlier = []
        for row in (facts.get("concepts") or {}).get("us-gaap:Assets") or []:
            if row.get("start"):
                continue
            if row["end"] >= year_t1["end"] or row["filed"] > as_of.isoformat():
                continue
            if row["form"] not in {"10-K", "10-K/A", "10-Q", "10-Q/A"}:
                continue
            gap = _annual_end_gap(year_t1["end"], row["end"])
            if 280 <= gap <= 450:
                earlier.append(row)
        if earlier:
            pick = min(earlier, key=lambda row: (abs(_annual_end_gap(year_t1["end"], row["end"]) - 365), row["end"]))
            assets_t2 = {**pick, "value": pick["val"], "concept": pick["tag"]}
    if assets_t is None or assets_t1 is None or assets_t2 is None:
        return {"ok": False, "reason": "annual_assets_missing"}

    cfo_years = {row["end"]: row for row in annual_facts(facts, "NetCashProvidedByUsedInOperatingActivities", as_of)}
    if year_t["end"] not in cfo_years:
        return {"ok": False, "reason": "missing_cfo_t"}
    cfo = cfo_years[year_t["end"]]

    ca_t = instant_on_or_before(facts, "AssetsCurrent", as_of, year_t["end"])
    cl_t = instant_on_or_before(facts, "LiabilitiesCurrent", as_of, year_t["end"])
    ca_t1 = instant_on_or_before(facts, "AssetsCurrent", as_of, year_t1["end"])
    cl_t1 = instant_on_or_before(facts, "LiabilitiesCurrent", as_of, year_t1["end"])
    if not all((ca_t, cl_t, ca_t1, cl_t1)):
        return {"ok": False, "reason": "missing_liquidity"}

    rev = revenue_pair(facts, as_of, year_t["end"], year_t1["end"])
    if rev is None:
        return {"ok": False, "reason": "missing_revenue"}
    rev_tag, rev_t, rev_t1 = rev
    gp_t = gross_profit_value(facts, as_of, year_t["end"], rev_t["val"])
    gp_t1 = gross_profit_value(facts, as_of, year_t1["end"], rev_t1["val"])
    if gp_t is None or gp_t1 is None:
        return {"ok": False, "reason": "missing_gross_profit"}

    ltd_t, ltd_t_item, ltd_t_tag = long_term_debt_value(facts, as_of, year_t["end"])
    ltd_t1, ltd_t1_item, ltd_t1_tag = long_term_debt_value(facts, as_of, year_t1["end"])
    issuance, issuance_item = issuance_value(facts, as_of, year_t["end"])
    shares_t = select_shares_near(facts, as_of, year_t["end"])
    shares_t1 = select_shares_near(facts, as_of, year_t1["end"])

    raw = {
        "ni_t": year_t["val"],
        "ni_t1": year_t1["val"],
        "assets_t": assets_t["value"],
        "assets_t1": assets_t1["value"],
        "assets_t2": assets_t2["value"],
        "cfo_t": cfo["val"],
        "ltd_t": ltd_t,
        "ltd_t1": ltd_t1,
        "ca_t": ca_t["value"],
        "cl_t": cl_t["value"],
        "ca_t1": ca_t1["value"],
        "cl_t1": cl_t1["value"],
        "gp_t": gp_t[0],
        "rev_t": rev_t["val"],
        "gp_t1": gp_t1[0],
        "rev_t1": rev_t1["val"],
        "issuance_t": issuance,
        "shares_t": None if shares_t is None else shares_t["value"],
        "shares_t1": None if shares_t1 is None else shares_t1["value"],
    }
    scored = score_f(raw)
    if not scored.get("ok"):
        return {"ok": False, "reason": scored.get("reason") or "fscore_incomplete"}
    return {
        "ok": True,
        "f_score": scored["f_score"],
        "flags": scored["flags"],
        "ratios": scored["ratios"],
        "eq_basis": scored["eq_basis"],
        "fiscal_year_end": year_t["end"],
        "prior_year_end": year_t1["end"],
        "filing_accession": year_t["accn"],
        "prior_filing_accession": year_t1["accn"],
        "inputs": raw,
        "sources": {
            "ni_t": _src(year_t),
            "ni_t1": _src(year_t1),
            "assets_t": _src(assets_t),
            "assets_t1": _src(assets_t1),
            "assets_t2": _src(assets_t2),
            "cfo_t": _src(cfo),
            "ltd_t": ltd_t_item or {"concept": ltd_t_tag, "value": ltd_t, "end": year_t["end"]},
            "ltd_t1": ltd_t1_item or {"concept": ltd_t1_tag, "value": ltd_t1, "end": year_t1["end"]},
            "ca_t": _src(ca_t),
            "cl_t": _src(cl_t),
            "ca_t1": _src(ca_t1),
            "cl_t1": _src(cl_t1),
            "rev_t": _src(rev_t) | {"concept": rev_tag},
            "rev_t1": _src(rev_t1) | {"concept": rev_tag},
            "gp_t": gp_t[1],
            "gp_t1": gp_t1[1],
            "issuance_t": issuance_item,
            "shares_t": shares_t,
            "shares_t1": shares_t1,
        },
    }


def select_shares_near(facts: dict, as_of: date, end: str) -> dict | None:
    for taxonomy, tag in (
        ("dei", "EntityCommonStockSharesOutstanding"),
        ("us-gaap", "CommonStockSharesOutstanding"),
    ):
        item = instant_on_or_before(facts, tag, as_of, end, taxonomy=taxonomy)
        if item is not None and item["value"] > 0:
            return item
    return None


def _src(row: dict) -> dict:
    if row is None:
        return {}
    if "concept" in row and "value" in row and "accn" in row:
        return {
            "concept": row.get("concept") or row.get("tag"),
            "value": row.get("value", row.get("val")),
            "end": row.get("end"),
            "start": row.get("start"),
            "accn": row.get("accn"),
            "filed": row.get("filed"),
            "form": row.get("form"),
            "fp": row.get("fp"),
            "fy": row.get("fy"),
        }
    return {
        "concept": row.get("tag"),
        "value": row.get("val"),
        "end": row.get("end"),
        "start": row.get("start"),
        "accn": row.get("accn"),
        "filed": row.get("filed"),
        "form": row.get("form"),
        "fp": row.get("fp"),
        "fy": row.get("fy"),
    }


def rank_ready(rows: list[dict], top_n: int) -> list[dict]:
    return rank_magic(rows, top_n)


# select_instant is used by tests that want the same date rule as the balance sheet.
__all__ = ["build_fscore_inputs", "build_magic_inputs", "rank_ready"]
