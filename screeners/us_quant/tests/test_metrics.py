"""Formula tests. No network."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from screeners.us_quant.assemble import build_fscore_inputs, build_magic_inputs
from screeners.us_quant.edgar_index import dedupe_rows, parse_master, quarter_labels
from screeners.us_quant.facts import select_debt, select_ttm_ebit
from screeners.us_quant.fscore import score_f
from screeners.us_quant.insider import aggregate_purchases, parse_form4
from screeners.us_quant.magic import earnings_yield, rank_magic, return_on_capital
from screeners.us_quant.output_io import hash_folder, write_table
from screeners.us_quant.universe import foreign_filer, is_common_listing, sic_block_reason

AS_OF = date(2026, 11, 12)
LIMITS = {"max_statement_age_days": 160, "max_annual_age_days": 540, "max_price_age_days": 7}


def _dur(tag, start, end, val, form="10-Q", filed="2026-11-01", fp="Q3", fy=2026, accn=None):
    return {
        "tag": tag,
        "unit": "USD",
        "start": start,
        "end": end,
        "filed": filed,
        "val": val,
        "accn": accn or f"{tag}-{end}-{fp}",
        "form": form,
        "fp": fp,
        "fy": fy,
    }


def _inst(tag, end, val, form="10-Q", filed="2026-11-01", fp="Q3", fy=2026, accn=None, unit="USD"):
    return {
        "tag": tag,
        "unit": unit,
        "start": None,
        "end": end,
        "filed": filed,
        "val": val,
        "accn": accn or f"{tag}-{end}",
        "form": form,
        "fp": fp,
        "fy": fy,
    }


def test_earnings_yield_and_return_on_capital():
    ey, enterprise = earnings_yield(100, 1000, 200, 50)
    assert enterprise == 1150
    assert ey == 100 / 1150
    roc, nwc, capital = return_on_capital(100, 300, 100, 300)
    assert nwc == 200
    assert capital == 500
    assert roc == 0.2


def test_rank_tie_break_is_ticker_order():
    rows = [
        {"ticker": "BBB", "earnings_yield": 0.2, "roc": 0.1},
        {"ticker": "AAA", "earnings_yield": 0.2, "roc": 0.1},
        {"ticker": "CCC", "earnings_yield": 0.5, "roc": 0.05},
    ]
    ranked = rank_magic(rows, top_n=2)
    assert [row["ticker"] for row in ranked] == ["AAA", "CCC"]
    assert ranked[0]["combined_rank"] == 3
    assert ranked[0]["ey_rank"] == 2
    assert ranked[0]["roc_rank"] == 1
    again = rank_magic(list(reversed(rows)), top_n=2)
    assert [(row["ticker"], row["combined_rank"]) for row in again] == [
        (row["ticker"], row["combined_rank"]) for row in ranked
    ]


def test_ttm_bridge_matches_the_four_quarter_identity():
    facts = {
        "concepts": {
            "us-gaap:OperatingIncomeLoss": [
                _dur("OperatingIncomeLoss", "2026-01-01", "2026-09-30", 300, fp="Q3", fy=2026),
                _dur("OperatingIncomeLoss", "2026-07-01", "2026-09-30", 100, fp="Q3", fy=2026),
                _dur("OperatingIncomeLoss", "2025-01-01", "2025-12-31", 400, form="10-K", filed="2026-02-15", fp="FY", fy=2025),
                _dur("OperatingIncomeLoss", "2025-01-01", "2025-09-30", 280, form="10-Q", filed="2025-11-01", fp="Q3", fy=2025),
            ]
        }
    }
    got = select_ttm_ebit(facts, AS_OF)
    assert got["ok"] is True
    assert got["method"] == "ytd_bridge"
    assert got["value"] == 420
    assert [item["accn"] for item in got["components"]] == [
        "OperatingIncomeLoss-2026-09-30-Q3",
        "OperatingIncomeLoss-2025-12-31-FY",
        "OperatingIncomeLoss-2025-09-30-Q3",
    ]


def test_debt_does_not_add_a_total_and_its_parts():
    end = "2026-09-30"
    facts = {
        "concepts": {
            "us-gaap:Assets": [_inst("Assets", end, 1000)],
            "us-gaap:DebtCurrent": [_inst("DebtCurrent", end, 5)],
            "us-gaap:LongTermDebtNoncurrent": [_inst("LongTermDebtNoncurrent", end, 20)],
            "us-gaap:LongTermDebt": [_inst("LongTermDebt", end, 25)],
            "us-gaap:LongTermDebtCurrent": [_inst("LongTermDebtCurrent", end, 4)],
        }
    }
    got = select_debt(facts, AS_OF)
    assert got["value"] == 25
    assert got["source"] == "current_plus_noncurrent"
    only_total = {
        "concepts": {
            "us-gaap:Assets": [_inst("Assets", end, 1000)],
            "us-gaap:LongTermDebt": [_inst("LongTermDebt", end, 30)],
            "us-gaap:ShortTermBorrowings": [_inst("ShortTermBorrowings", end, 3)],
            "us-gaap:LongTermDebtCurrent": [_inst("LongTermDebtCurrent", end, 4)],
        }
    }
    other = select_debt(only_total, AS_OF)
    assert other["value"] == 33
    assert other["source"] == "long_term_debt_total"


def _healthy_inputs():
    return {
        "ni_t": 10,
        "ni_t1": 5,
        "assets_t": 100,
        "assets_t1": 80,
        "assets_t2": 70,
        "cfo_t": 12,
        "ltd_t": 10,
        "ltd_t1": 20,
        "ca_t": 40,
        "cl_t": 20,
        "ca_t1": 30,
        "cl_t1": 20,
        "issuance_t": 0,
        "gp_t": 30,
        "rev_t": 50,
        "gp_t1": 20,
        "rev_t1": 40,
    }


def test_fscore_all_nine_and_each_flip():
    perfect = score_f(_healthy_inputs())
    assert perfect["ok"] is True
    assert perfect["f_score"] == 9
    flips = {
        "f_roa": ({"ni_t": -1}, 7),
        "f_cfo": ({"cfo_t": -1}, 7),
        "f_droa": ({"ni_t": 1}, 8),
        "f_accrual": ({"cfo_t": 1}, 8),
        "f_dlever": ({"ltd_t": 40}, 8),
        "f_dliquid": ({"ca_t": 20}, 8),
        "f_eqoffer": ({"issuance_t": 5}, 8),
        "f_dmargin": ({"gp_t": 10}, 8),
        "f_dturn": ({"rev_t": 20}, 8),
    }
    for flag, (change, total) in flips.items():
        raw = _healthy_inputs()
        raw.update(change)
        scored = score_f(raw)
        assert scored["flags"][flag] == 0, flag
        # A negative ROA also fails the ROA increase. Negative cash flow also fails the accrual test.
        assert scored["f_score"] == total, flag
    missing = _healthy_inputs()
    del missing["ni_t"]
    assert score_f(missing)["reason"] == "missing_ni_t"
    no_issue = _healthy_inputs()
    del no_issue["issuance_t"]
    no_issue["shares_t"] = 10
    no_issue["shares_t1"] = 10
    scored = score_f(no_issue)
    assert scored["flags"]["f_eqoffer"] == 1
    assert scored["eq_basis"] == "shares_not_increased"
    no_issue["shares_t"] = 11
    assert score_f(no_issue)["flags"]["f_eqoffer"] == 0


def test_magic_and_fscore_from_facts():
    end = "2026-09-30"
    concepts = {
        "us-gaap:OperatingIncomeLoss": [
            _dur("OperatingIncomeLoss", "2026-01-01", "2026-09-30", 300, fp="Q3"),
            _dur("OperatingIncomeLoss", "2025-01-01", "2025-12-31", 400, form="10-K", filed="2026-02-15", fp="FY", fy=2025),
            _dur("OperatingIncomeLoss", "2025-01-01", "2025-09-30", 280, filed="2025-11-01", fp="Q3", fy=2025),
        ],
        "us-gaap:Assets": [_inst("Assets", end, 1000)],
        "us-gaap:AssetsCurrent": [_inst("AssetsCurrent", end, 400)],
        "us-gaap:LiabilitiesCurrent": [_inst("LiabilitiesCurrent", end, 150)],
        "us-gaap:PropertyPlantAndEquipmentNet": [_inst("PropertyPlantAndEquipmentNet", end, 300)],
        "us-gaap:CashAndCashEquivalentsAtCarryingValue": [_inst("CashAndCashEquivalentsAtCarryingValue", end, 50)],
        "us-gaap:DebtCurrent": [_inst("DebtCurrent", end, 20)],
        "us-gaap:LongTermDebtNoncurrent": [_inst("LongTermDebtNoncurrent", end, 80)],
        "dei:EntityCommonStockSharesOutstanding": [
            _inst("EntityCommonStockSharesOutstanding", end, 10, unit="shares")
        ],
    }
    built = build_magic_inputs({"concepts": concepts}, AS_OF, price=15, limits=LIMITS)
    assert built["ok"] is True
    assert built["ebit"] == 420
    assert built["market_cap"] == 150
    assert built["enterprise_value"] == 200
    assert built["earnings_yield"] == 420 / 200
    assert built["net_working_capital"] == 250
    assert built["capital"] == 550
    assert built["roc"] == 420 / 550
    assert built["shares_source"] == "dei:EntityCommonStockSharesOutstanding"

    annual = {}
    for year, ni, cfo, rev, gp, assets, ca, cl, ltd, issuance in (
        (2023, 4, 4, 30, 12, 70, 20, 15, 20, 0),
        (2024, 5, 6, 40, 20, 80, 30, 20, 20, 0),
        (2025, 10, 12, 50, 30, 100, 40, 20, 10, 0),
    ):
        yend = f"{year}-12-31"
        ystart = f"{year}-01-01"
        filed = f"{year + 1}-02-20"
        annual.setdefault("us-gaap:NetIncomeLoss", []).append(
            _dur("NetIncomeLoss", ystart, yend, ni, form="10-K", filed=filed, fp="FY", fy=year)
        )
        annual.setdefault("us-gaap:NetCashProvidedByUsedInOperatingActivities", []).append(
            _dur("NetCashProvidedByUsedInOperatingActivities", ystart, yend, cfo, form="10-K", filed=filed, fp="FY", fy=year)
        )
        annual.setdefault("us-gaap:Revenues", []).append(
            _dur("Revenues", ystart, yend, rev, form="10-K", filed=filed, fp="FY", fy=year)
        )
        annual.setdefault("us-gaap:GrossProfit", []).append(
            _dur("GrossProfit", ystart, yend, gp, form="10-K", filed=filed, fp="FY", fy=year)
        )
        annual.setdefault("us-gaap:Assets", []).append(_inst("Assets", yend, assets, form="10-K", filed=filed, fp="FY", fy=year))
        annual.setdefault("us-gaap:AssetsCurrent", []).append(_inst("AssetsCurrent", yend, ca, form="10-K", filed=filed, fp="FY", fy=year))
        annual.setdefault("us-gaap:LiabilitiesCurrent", []).append(
            _inst("LiabilitiesCurrent", yend, cl, form="10-K", filed=filed, fp="FY", fy=year)
        )
        annual.setdefault("us-gaap:LongTermDebtNoncurrent", []).append(
            _inst("LongTermDebtNoncurrent", yend, ltd, form="10-K", filed=filed, fp="FY", fy=year)
        )
        annual.setdefault("us-gaap:ProceedsFromIssuanceOfCommonStock", []).append(
            _dur("ProceedsFromIssuanceOfCommonStock", ystart, yend, issuance, form="10-K", filed=filed, fp="FY", fy=year)
        )
    scored = build_fscore_inputs({"concepts": annual}, AS_OF, 540)
    assert scored["ok"] is True, scored
    assert scored["f_score"] == 9
    assert scored["sources"]["ni_t"]["accn"].endswith("2025-FY") or "2025" in scored["sources"]["ni_t"]["accn"]
    assert scored["sources"]["ni_t"]["value"] == 10
    assert scored["filing_accession"]


def test_open_market_purchases_ignore_sales():
    xml = """
    <ownershipDocument>
      <issuer><issuerCik>0000320193</issuerCik><issuerTradingSymbol>AAPL</issuerTradingSymbol></issuer>
      <reportingOwner>
        <reportingOwnerId><rptOwnerCik>0001234567</rptOwnerCik><rptOwnerName>Jane Doe</rptOwnerName></reportingOwnerId>
        <reportingOwnerRelationship><isDirector>1</isDirector><isOfficer>1</isOfficer><isTenPercentOwner>0</isTenPercentOwner><officerTitle>CFO</officerTitle></reportingOwnerRelationship>
      </reportingOwner>
      <nonDerivativeTable>
        <nonDerivativeTransaction>
          <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
          <transactionDate><value>2026-11-01</value></transactionDate>
          <transactionAmounts>
            <transactionShares><value>100</value></transactionShares>
            <transactionPricePerShare><value>10.5</value></transactionPricePerShare>
            <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
          </transactionAmounts>
        </nonDerivativeTransaction>
        <nonDerivativeTransaction>
          <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
          <transactionDate><value>2026-11-02</value></transactionDate>
          <transactionAmounts>
            <transactionShares><value>50</value></transactionShares>
            <transactionPricePerShare><value>11</value></transactionPricePerShare>
            <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
          </transactionAmounts>
        </nonDerivativeTransaction>
      </nonDerivativeTable>
    </ownershipDocument>
    """
    rows = parse_form4(xml)
    assert len(rows) == 1
    assert rows[0]["value"] == 1050
    grouped = aggregate_purchases(rows, AS_OF, 30)
    bucket = grouped["0000320193"]
    assert bucket["distinct_insiders"] == 1
    assert bucket["total_value_usd"] == 1050
    assert bucket["roles"] == ["director", "officer"]
    assert bucket["officer_titles"] == ["CFO"]
    old = aggregate_purchases(rows, date(2026, 12, 15), 30)
    assert old == {}


def test_index_parses_both_date_styles_and_dedupes():
    text = """Last Data Received:    November 10, 2026
CIK|Company Name|Form Type|Date Filed|Filename
100|EXAMPLE INC|4|2026-11-10|edgar/data/100/0000000001-26-000001.txt
100|EXAMPLE INC|4|20261110|edgar/data/100/0000000001-26-000001.txt
100|EXAMPLE INC|10-Q|20261105|edgar/data/100/0000000001-26-000009.txt
"""
    rows, received = parse_master(text)
    assert received == date(2026, 11, 10)
    assert len(dedupe_rows(rows)) == 2
    assert rows[0]["accn"] == "0000000001-26-000001"
    assert quarter_labels(date(2026, 9, 15), date(2026, 11, 1)) == [(2026, 3), (2026, 4)]


def test_universe_filters():
    exchanges = ["Nasdaq", "NYSE", "TXSE"]
    assert is_common_listing({"ticker": "BRK-B", "name": "BERKSHIRE HATHAWAY INC", "exchange": "NYSE"}, exchanges)
    assert not is_common_listing({"ticker": "BAC-PB", "name": "BANK OF AMERICA", "exchange": "NYSE"}, exchanges)
    assert not is_common_listing({"ticker": "SPY", "name": "SPDR S&P 500 ETF", "exchange": "NYSE"}, exchanges)
    ranges = [
        {"name": "utilities", "sic_from": 4900, "sic_to": 4999},
        {"name": "financials", "sic_from": 6000, "sic_to": 6799},
    ]
    assert sic_block_reason(4911, ranges) == "utilities"
    assert sic_block_reason(6798, ranges) == "financials"
    assert sic_block_reason(7372, ranges) is None
    assert foreign_filer({"20-F", "6-K"}, ["20-F", "40-F"], ["10-K", "10-Q"]) is True
    assert foreign_filer({"10-K", "10-Q"}, ["20-F", "40-F"], ["10-K", "10-Q"]) is False


def test_output_hash_is_stable(tmp_path: Path):
    meta = {
        "run_date": "2026-11-13",
        "trade_date": "2026-11-12",
        "price_as_of": "2026-11-12",
        "code_version": "abc",
        "min_market_cap": 100000000,
        "magic_top_n": 30,
        "insider_lookback_days": 30,
        "fscore_basis": "annual",
        "fscore_min_score": 8,
        "data_sources": ["SEC EDGAR companyfacts"],
        "config": {"min_market_cap": 100000000},
        "counts": {},
        "previous_trade_date": None,
        "insider_filings_through": "2026-11-10",
    }
    rows = [{"ticker": "ZZZ", "name": "Zed", "earnings_yield": 0.125}]
    write_table(tmp_path, "magic_formula", meta, rows, ["ticker", "name", "earnings_yield"])
    first = hash_folder(tmp_path)
    write_table(tmp_path, "magic_formula", meta, list(reversed(rows)), ["ticker", "name", "earnings_yield"])
    assert hash_folder(tmp_path) == first
    payload = json.loads((tmp_path / "magic_formula.json").read_text(encoding="utf-8"))
    assert payload["meta"]["trade_date"] == "2026-11-12"
    assert payload["rows"][0]["run_date"] == "2026-11-13"
    assert payload["rows"][0]["earnings_yield"] == 0.125
