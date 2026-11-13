"""Magic Formula arithmetic and ranks.

Earnings yield = EBIT / enterprise value.
Enterprise value = market cap + total debt - cash.
Return on capital = EBIT / (net working capital + net fixed assets).
Net working capital = current assets - current liabilities.
Net fixed assets = net property, plant and equipment.

Ranks use the stored (rounded) ratios. A higher ratio gets a better rank
(a smaller rank number). Equal ratios break ties by ticker ascending.
Combined rank is the sum of the two ranks. A lower sum sorts first.
"""

from __future__ import annotations


def earnings_yield(ebit: float, market_cap: float, debt: float, cash: float) -> tuple[float, float]:
    enterprise = market_cap + debt - cash
    if enterprise == 0:
        raise ZeroDivisionError("enterprise value is zero")
    return ebit / enterprise, enterprise


def return_on_capital(
    ebit: float,
    current_assets: float,
    current_liabilities: float,
    net_fixed_assets: float,
) -> tuple[float, float, float]:
    nwc = current_assets - current_liabilities
    capital = nwc + net_fixed_assets
    if capital == 0:
        raise ZeroDivisionError("capital is zero")
    return ebit / capital, nwc, capital


def _ordinal(rows: list[dict], field: str) -> dict[str, int]:
    ordered = sorted(rows, key=lambda row: (-float(row[field]), str(row["ticker"])))
    return {str(row["ticker"]): index for index, row in enumerate(ordered, start=1)}


def rank_magic(rows: list[dict], top_n: int) -> list[dict]:
    """Add ey_rank, roc_rank, combined_rank. Return the top_n rows."""
    if not rows:
        return []
    ey_rank = _ordinal(rows, "earnings_yield")
    roc_rank = _ordinal(rows, "roc")
    ranked = []
    for row in rows:
        ticker = str(row["ticker"])
        item = dict(row)
        item["ey_rank"] = ey_rank[ticker]
        item["roc_rank"] = roc_rank[ticker]
        item["combined_rank"] = ey_rank[ticker] + roc_rank[ticker]
        ranked.append(item)
    ranked.sort(key=lambda row: (int(row["combined_rank"]), str(row["ticker"])))
    limit = len(ranked) if top_n < 0 else top_n
    return ranked[:limit]
