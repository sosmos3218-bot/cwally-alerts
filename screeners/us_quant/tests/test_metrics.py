"""Formula tests. No network."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from screeners.us_quant.assemble import build_fscore_inputs, build_magic_inputs
from screeners.us_quant.cover import instance_name, parse_repurchase_obligations, parse_share_document, stamp_filing
from screeners.us_quant.edgar_index import dedupe_rows, parse_master, quarter_labels
from screeners.us_quant.facts import (
    extract_companyfacts,
    large_share_decline_after_balance_sheet,
    one_time_ebit_flag,
    select_debt,
    select_repurchase_obligation,
    select_shares,
    select_ttm_ebit,
)
from screeners.us_quant.fscore import score_f
from screeners.us_quant.insider import aggregate_purchases, parse_form4
from screeners.us_quant.magic import apply_magic_guards, earnings_yield, rank_magic, return_on_capital
from screeners.us_quant.pipeline import form4_acceptance_value, listed_multi_class, refresh_diff
from screeners.us_quant.prices import common_close_date, parse_chart
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
    lease_total = {
        "concepts": {
            "us-gaap:Assets": [_inst("Assets", end, 1000)],
            "us-gaap:DebtCurrent": [_inst("DebtCurrent", end, 491)],
            "us-gaap:LongTermDebtAndCapitalLeaseObligations": [
                _inst("LongTermDebtAndCapitalLeaseObligations", end, 4688)
            ],
        }
    }
    leased = select_debt(lease_total, AS_OF)
    # 4,688 is the noncurrent lease line. 491 is the current portion (Micron's
    # DebtCurrent, equal to the current finance lease). They are added once.
    assert leased["value"] == 5179
    assert leased["source"] == "lt_and_lease_plus_current"
    already_total = _debt_facts(
        end,
        {
            "LongTermDebtAndCapitalLeaseObligations": 5179,
            "DebtCurrent": 491,
            "DebtAndCapitalLeaseObligations": 5179,
        },
    )
    capped = select_debt(already_total, AS_OF)
    assert capped["value"] == 5179
    assert capped["source"] == "debt_and_leases"


def _debt_facts(end, values, filed="2026-11-05"):
    concepts = {"us-gaap:Assets": [_inst("Assets", end, 1000, filed=filed)]}
    for tag, val in values.items():
        concepts[f"us-gaap:{tag}"] = [_inst(tag, end, val, filed=filed)]
    return {"concepts": concepts}


def test_notes_loans_and_credit_lines_are_debt_when_no_total_is_tagged():
    """10-Q amounts that the LongTermDebt tags do not carry."""
    end = "2026-09-30"
    coll = _debt_facts(
        end,
        {
            "LoansPayableCurrent": 60_500_000,
            "LongTermLoansPayable": 782_007_000,
            "ConvertibleLongTermNotesPayable": 238_996_000,
        },
    )
    got = select_debt(coll, AS_OF)
    assert got["ok"] is True
    assert got["value"] == 1_081_503_000
    assert got["source"] == "component_sum"

    irwd = _debt_facts(end, {"LongTermLineOfCredit": 325_000_000})
    irwd["concepts"]["us-gaap:LongTermDebt"] = [
        _inst("LongTermDebt", "2025-09-30", 199_506_000, filed="2025-11-10")
    ]
    assert select_debt(irwd, AS_OF)["value"] == 325_000_000

    amn = _debt_facts(end, {"LongTermNotesPayable": 743_370_000, "LongTermLineOfCredit": 0})
    assert select_debt(amn, AS_OF)["value"] == 743_370_000

    yelp = _debt_facts(end, {"LongTermLineOfCredit": 75_000_000})
    assert select_debt(yelp, AS_OF)["value"] == 75_000_000

    mgtx = _debt_facts(
        end,
        {
            "LongTermNotesPayable": 121_900_000,
            "FinanceLeaseLiability": 115_000,
            "FinanceLeaseLiabilityCurrent": 41_000,
            "FinanceLeaseLiabilityNoncurrent": 74_000,
        },
    )
    got = select_debt(mgtx, AS_OF)
    assert got["value"] == 122_015_000
    assert "FinanceLeaseLiabilityCurrent" not in {item["concept"] for item in got["components"]}

    same_line = _debt_facts(
        end,
        {"LineOfCredit": 340_000_000, "LongTermLineOfCredit": 340_000_000, "FinanceLeaseLiabilityCurrent": 570_000},
    )
    assert select_debt(same_line, AS_OF)["value"] == 340_570_000

    covered = _debt_facts(end, {"LongTermDebt": 100, "LongTermNotesPayable": 100, "ShortTermBorrowings": 3})
    total = select_debt(covered, AS_OF)
    assert total["value"] == 103
    assert total["source"] == "long_term_debt_total"

    zero_total = _debt_facts(end, {"LongTermDebt": 0, "LongTermNotesPayable": 50})
    assert select_debt(zero_total, AS_OF)["value"] == 50
    current_zero = _debt_facts(end, {"DebtCurrent": 0, "LongTermDebtNoncurrent": 100})
    paired = select_debt(current_zero, AS_OF)
    assert paired["value"] == 100
    assert paired["source"] == "current_plus_noncurrent"


def test_missing_debt_tag_is_excluded_unless_the_filing_shows_none():
    end = "2026-09-30"
    bare = {
        "concepts": {
            "us-gaap:Assets": [_inst("Assets", end, 1000)],
            "us-gaap:Liabilities": [_inst("Liabilities", end, 500_000_000)],
        }
    }
    missing = select_debt(bare, AS_OF)
    assert missing["ok"] is False
    assert missing["reason"] == "debt_unconfirmed"
    implied = {
        "concepts": {
            "us-gaap:Assets": [_inst("Assets", end, 1000)],
            "us-gaap:Liabilities": [_inst("Liabilities", end, 100)],
            "us-gaap:AccountsPayableCurrent": [_inst("AccountsPayableCurrent", end, 70)],
            "us-gaap:OperatingLeaseLiability": [_inst("OperatingLeaseLiability", end, 30)],
        }
    }
    zero = select_debt(implied, AS_OF)
    assert zero["ok"] is True
    assert zero["value"] == 0
    assert zero["source"] == "liabilities_imply_no_debt"
    explicit = _debt_facts(end, {"LongTermDebt": 0})
    reported = select_debt(explicit, AS_OF)
    assert reported["ok"] is True
    assert reported["value"] == 0
    assert reported["source"] == "explicit_zero"


def test_hon_and_man_debt_include_the_current_portion():
    """SEC companyfacts on the latest balance sheet. Dollars, not millions.

    HON 10-Q 0000773840-26-000143, filed 2026-10-22, instant 2026-09-30:
    LongTermDebtAndCapitalLeaseObligations 10,386,000,000,
    LongTermDebtAndCapitalLeaseObligationsCurrent 3,783,000,000,
    ShortTermBorrowings 2,424,000,000 (commercial paper and short-term borrowings),
    LongTermDebt 14,169,000,000 which equals 10,386 + 3,783.
    Total 16,593,000,000.

    MAN 10-Q 0001193125-26-441750, filed 2026-10-30, instant 2026-09-30:
    LongTermDebtAndCapitalLeaseObligations 562,800,000 (long-term debt),
    DebtCurrent 540,600,000 (short-term borrowings and current maturities).
    The capitalization table total is 1,103,400,000.
    """
    hon = _debt_facts(
        "2026-09-30",
        {
            "LongTermDebt": 14_169_000_000,
            "LongTermDebtAndCapitalLeaseObligations": 10_386_000_000,
            "LongTermDebtAndCapitalLeaseObligationsCurrent": 3_783_000_000,
            "ShortTermBorrowings": 2_424_000_000,
        },
        filed="2026-10-22",
    )
    got = select_debt(hon, AS_OF)
    assert got["ok"] is True
    assert got["value"] == 16_593_000_000
    assert got["source"] == "lt_and_lease_plus_current"
    assert {item["concept"] for item in got["components"]} == {
        "LongTermDebtAndCapitalLeaseObligations",
        "LongTermDebtAndCapitalLeaseObligationsCurrent",
        "ShortTermBorrowings",
    }

    man = _debt_facts(
        "2026-09-30",
        {
            "LongTermDebtAndCapitalLeaseObligations": 562_800_000,
            "DebtCurrent": 540_600_000,
        },
        filed="2026-10-30",
    )
    got = select_debt(man, AS_OF)
    assert got["value"] == 1_103_400_000
    assert got["source"] == "lt_and_lease_plus_current"


def test_yum_current_maturities_match_short_term_borrowings():
    """YUM 10-Q 0001041061-26-000210, instant 2026-09-30. Dollars.

    Balance sheet: short-term borrowings 2,141,000,000 and long-term debt
    9,459,000,000. Total debt is 11,600,000,000.
    Note 12: current maturities of long-term debt 2,146,000,000 less a
    5,000,000 issuance-cost contra equal short-term borrowings 2,141,000,000.
    The note subtotal 11,653,000,000 less long-term issuance costs 48,000,000
    and that current contra is the same 11,600,000,000. Finance lease
    obligations of 144,000,000 sit inside the long-term debt line.
    LongTermDebt and LongTermDebtNoncurrent are absent on this date.
    The old sum 9,459 + 2,146 + 2,141 = 13,746 counted the current line twice.
    """
    facts = _debt_facts(
        "2026-09-30",
        {
            "LongTermDebtAndCapitalLeaseObligations": 9_459_000_000,
            "LongTermDebtCurrent": 2_146_000_000,
            "ShortTermBorrowings": 2_141_000_000,
            "FinanceLeaseLiabilityNoncurrent": 144_000_000,
        },
        filed="2026-11-06",
    )
    got = select_debt(facts, AS_OF)
    assert got["value"] == 11_600_000_000
    assert got["source"] == "lt_and_lease_plus_current"
    assert {item["concept"] for item in got["components"]} == {
        "LongTermDebtAndCapitalLeaseObligations",
        "ShortTermBorrowings",
    }
    noncurrent_path = _debt_facts(
        "2026-09-30",
        {
            "LongTermDebtNoncurrent": 9_459_000_000,
            "LongTermDebtCurrent": 2_146_000_000,
            "ShortTermBorrowings": 2_141_000_000,
        },
        filed="2026-11-06",
    )
    other = select_debt(noncurrent_path, AS_OF)
    assert other["value"] == 11_600_000_000
    assert "LongTermDebtCurrent" not in {item["concept"] for item in other["components"]}


def test_lease_tag_equal_to_long_term_debt_can_still_be_the_noncurrent_line():
    """ITT 10-Q filed 2026-11-05, instant 2026-10-03. Dollars.

    LongTermDebt and LongTermDebtAndCapitalLeaseObligations are both
    2,806,100,000. That match is the noncurrent line, not a total that
    already includes current maturities. DebtAndCapitalLeaseObligations is
    3,513,400,000, equal to 2,806,100,000 plus DebtCurrent 707,300,000.
    DebtCurrent is commercial paper 702,200,000 plus short-term borrowings
    2,500,000 plus LongTermDebtCurrent 2,600,000.
    """
    facts = _debt_facts(
        "2026-10-03",
        {
            "DebtCurrent": 707_300_000,
            "LongTermDebtCurrent": 2_600_000,
            "LongTermDebt": 2_806_100_000,
            "ShortTermBorrowings": 2_500_000,
            "CommercialPaper": 702_200_000,
            "LongTermDebtAndCapitalLeaseObligations": 2_806_100_000,
            "DebtAndCapitalLeaseObligations": 3_513_400_000,
        },
        filed="2026-11-05",
    )
    got = select_debt(facts, AS_OF)
    assert got["value"] == 3_513_400_000
    assert "LongTermDebtCurrent" in {item["concept"] for item in got["components"]}


def test_short_line_matching_current_is_not_added_above_long_term_debt():
    """LongTermDebt equals noncurrent plus the current slice.

    A short-term borrowings figure that matches the slice is that same
    balance. The total stays at LongTermDebt.
    """
    facts = _debt_facts(
        "2026-09-30",
        {
            "LongTermDebt": 100,
            "LongTermDebtNoncurrent": 80,
            "LongTermDebtCurrent": 20,
            "ShortTermBorrowings": 20,
        },
    )
    got = select_debt(facts, AS_OF)
    assert got["value"] == 100
    assert {item["concept"] for item in got["components"]} == {
        "LongTermDebtNoncurrent",
        "ShortTermBorrowings",
    }


def test_smaller_all_in_tag_does_not_drop_commercial_paper():
    """NXPI-like. DebtAndCapitalLeaseObligations equals the noncurrent line plus the current portion.

    Commercial paper is outside that tag, so the smaller tag does not replace the sum.
    """
    facts = _debt_facts(
        "2026-09-27",
        {
            "LongTermDebtAndCapitalLeaseObligations": 9_979_000_000,
            "LongTermDebtAndCapitalLeaseObligationsCurrent": 999_000_000,
            "DebtCurrent": 999_000_000,
            "CommercialPaper": 2_000_000_000,
            "DebtAndCapitalLeaseObligations": 10_978_000_000,
        },
        filed="2026-10-22",
    )
    got = select_debt(facts, AS_OF)
    assert got["value"] == 12_978_000_000
    assert "CommercialPaper" in {item["concept"] for item in got["components"]}
    assert "DebtCurrent" not in {item["concept"] for item in got["components"]}


def test_commercial_paper_inside_short_term_borrowings_is_not_added_twice():
    """PEP-like: short-term debt obligations are 9,223 and commercial paper 4,800 is inside them."""
    end = "2026-09-05"
    facts = _debt_facts(
        end,
        {
            "LongTermDebtAndCapitalLeaseObligations": 42_658_000_000,
            "ShortTermBorrowings": 9_223_000_000,
            "CommercialPaper": 4_800_000_000,
        },
        filed="2026-10-08",
    )
    got = select_debt(facts, AS_OF)
    assert got["value"] == 51_881_000_000
    assert "CommercialPaper" not in {item["concept"] for item in got["components"]}
    separate = _debt_facts(
        end,
        {
            "LongTermDebtAndCapitalLeaseObligations": 100,
            "ShortTermBorrowings": 10,
            "CommercialPaper": 40,
        },
    )
    both = select_debt(separate, AS_OF)
    assert both["value"] == 150


def test_repurchase_obligation_lines_cash_up_with_the_later_share_count():
    """ABUS-like. Cover shares are after the repurchase. Cash is the 9/30 balance sheet.

    ObligationToRepurchaseSharesCurrent is 230,000,000 on 2026-09-30.
    Cash is 264,191,000. Shares on 2026-11-06 are 153,292,826.
    Shares on 2026-09-30 are 199,275,907. Price 4.02.
    Enterprise value is about 582,046,161 and the earnings yield about 25.3%.
    """
    end = "2026-09-30"
    concepts = {
        "us-gaap:OperatingIncomeLoss": [
            _dur("OperatingIncomeLoss", "2025-10-01", end, 147_160_000, form="10-K", filed="2026-11-10", fp="FY")
        ],
        "us-gaap:Assets": [_inst("Assets", end, 339_698_000, filed="2026-11-10")],
        "us-gaap:AssetsCurrent": [_inst("AssetsCurrent", end, 339_567_000, filed="2026-11-10")],
        "us-gaap:LiabilitiesCurrent": [_inst("LiabilitiesCurrent", end, 241_933_000, filed="2026-11-10")],
        "us-gaap:PropertyPlantAndEquipmentNet": [_inst("PropertyPlantAndEquipmentNet", end, 1_000_000, filed="2026-11-10")],
        "us-gaap:CashAndCashEquivalentsAtCarryingValue": [
            _inst("CashAndCashEquivalentsAtCarryingValue", end, 264_191_000, filed="2026-11-10")
        ],
        "us-gaap:LongTermDebt": [_inst("LongTermDebt", end, 0, filed="2026-11-10")],
        "abus:ObligationToRepurchaseSharesCurrent": [
            _inst("ObligationToRepurchaseSharesCurrent", end, 230_000_000, filed="2026-11-10")
        ],
        "us-gaap:CommonStockSharesOutstanding": [
            _inst("CommonStockSharesOutstanding", end, 199_275_907, filed="2026-11-10", unit="shares")
        ],
        "dei:EntityCommonStockSharesOutstanding": [
            _inst("EntityCommonStockSharesOutstanding", "2026-11-06", 153_292_826, filed="2026-11-10", unit="shares")
        ],
    }
    built = build_magic_inputs({"concepts": concepts}, AS_OF, price=4.02, limits=LIMITS)
    assert built["ok"] is True
    assert built["shares"] == 153_292_826
    assert built["large_buyback_after_bs"] is True
    assert built["repurchase_obligation"] == 230_000_000
    assert built["total_debt"] == 0
    assert built["cash"] == 34_191_000
    assert abs(built["enterprise_value"] - 582_046_160.52) < 1
    assert abs(built["earnings_yield"] - 0.252831) < 0.0001
    untouched = build_magic_inputs(
        {"concepts": {key: value for key, value in concepts.items() if "Obligation" not in key}},
        AS_OF,
        price=4.02,
        limits=LIMITS,
    )
    assert untouched["cash"] == 264_191_000
    assert abs(untouched["enterprise_value"] - 352_046_160.52) < 1


def test_repurchase_obligation_parser_keeps_the_undimensioned_amount_once():
    xml = """<?xml version="1.0"?>
    <xbrl xmlns:abus="http://example.com/abus" xmlns:us-gaap="http://fasb.org/us-gaap/2025" xmlns:srt="http://fasb.org/srt/2025">
      <context id="c-bs"><period><instant>2026-09-30</instant></period></context>
      <context id="c-program">
        <entity><segment>
          <explicitMember dimension="srt:ShareRepurchaseProgramAxis">abus:ModifiedDutchAuctionMember</explicitMember>
        </segment></entity>
        <period><instant>2026-09-30</instant></period>
      </context>
      <context id="c-later">
        <entity><segment>
          <explicitMember dimension="us-gaap:SubsequentEventTypeAxis">us-gaap:SubsequentEventMember</explicitMember>
        </segment></entity>
        <period><instant>2026-10-01</instant></period>
      </context>
      <abus:ObligationToRepurchaseSharesCurrent contextRef="c-bs" unitRef="usd">230000000</abus:ObligationToRepurchaseSharesCurrent>
      <abus:ObligationToRepurchaseSharesCurrent contextRef="c-program" unitRef="usd">230000000</abus:ObligationToRepurchaseSharesCurrent>
      <abus:ObligationToRepurchaseSharesCurrent contextRef="c-later" unitRef="usd">230000000</abus:ObligationToRepurchaseSharesCurrent>
    </xbrl>
    """
    rows = stamp_filing(parse_repurchase_obligations(xml), "0001447028-26-000064", "10-Q", "2026-11-10")
    assert len(rows) == 2
    plain = [row for row in rows if not row.get("member")]
    assert len(plain) == 1
    assert plain[0]["val"] == 230_000_000
    assert select_repurchase_obligation({"concepts": {}}, AS_OF, "2026-09-30", rows) == 230_000_000


def test_one_time_ebit_flags_a_revenue_spike_and_discontinued_operations():
    end = "2026-09-30"
    spike = {
        "concepts": {
            "us-gaap:Revenues": [
                _dur("Revenues", "2026-01-01", end, 180_439_000, fp="Q3"),
                _dur("Revenues", "2026-01-01", "2026-03-31", 179_126_000, fp="Q1"),
                _dur("Revenues", "2026-04-01", "2026-06-30", 1_014_000, fp="Q2"),
                _dur("Revenues", "2026-07-01", end, 299_000, fp="Q3"),
            ]
        }
    }
    ebit = {
        "period_end": end,
        "components": [{"concept": "OperatingIncomeLoss", "start": "2026-01-01", "end": end, "value": 152_450_000}],
    }
    flagged = one_time_ebit_flag(spike, AS_OF, ebit)
    assert flagged["one_time_ebit"] is True
    assert flagged["one_time_ebit_reason"] == "revenue_spike"
    mixed = {
        "concepts": {
            "us-gaap:IncomeLossFromDiscontinuedOperationsNetOfTax": [
                _dur("IncomeLossFromDiscontinuedOperationsNetOfTax", "2026-01-01", end, 1_061_000_000, fp="Q3")
            ]
        }
    }
    hon = {
        "period_end": end,
        "components": [{"concept": "OperatingIncomeLoss", "start": "2026-01-01", "end": end, "value": 2_991_000_000}],
    }
    flagged = one_time_ebit_flag(mixed, AS_OF, hon)
    assert flagged["one_time_ebit"] is True
    assert flagged["one_time_ebit_reason"] == "discontinued_ops"
    steady = one_time_ebit_flag({"concepts": {}}, AS_OF, ebit)
    assert steady["one_time_ebit"] is False


def test_extract_keeps_the_current_lease_debt_tag():
    payload = {
        "cik": 773840,
        "entityName": "HONEYWELL INTERNATIONAL INC",
        "facts": {
            "us-gaap": {
                "LongTermDebtAndCapitalLeaseObligationsCurrent": {
                    "units": {
                        "USD": [
                            {
                                "end": "2026-09-30",
                                "val": 3_783_000_000,
                                "accn": "0000773840-26-000143",
                                "fy": 2026,
                                "fp": "Q3",
                                "form": "10-Q",
                                "filed": "2026-10-22",
                            }
                        ]
                    }
                }
            }
        },
    }
    got = extract_companyfacts(payload)
    assert got["extract_version"] == 4
    assert got["concepts"]["us-gaap:LongTermDebtAndCapitalLeaseObligationsCurrent"][0]["val"] == 3_783_000_000


def test_net_fixed_assets_uses_the_successor_tag_or_drops_the_row():
    from screeners.us_quant.facts import select_net_fixed_assets

    end = "2026-09-03"
    successor = "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization"
    facts = {
        "concepts": {
            "us-gaap:Assets": [_inst("Assets", end, 1000)],
            "us-gaap:PropertyPlantAndEquipmentNet": [_inst("PropertyPlantAndEquipmentNet", "2020-05-28", 30)],
            f"us-gaap:{successor}": [_inst(successor, end, 63310)],
        }
    }
    got = select_net_fixed_assets(facts, AS_OF, end)
    assert got["concept"] == successor
    assert got["value"] == 63310
    missing = {"concepts": {"us-gaap:Assets": [_inst("Assets", end, 1000)]}}
    assert select_net_fixed_assets(missing, AS_OF, end) is None


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


def test_purchase_total_does_not_depend_on_arrival_order():
    base = {
        "issuer_cik": "0000000001",
        "symbol": "X",
        "owners": [{"owner_cik": "0000000002", "director": True, "officer": False, "ten_percent": False, "officer_title": ""}],
    }
    trades = []
    for index, accn in enumerate(("b", "a", "c")):
        trades.append({**base, "trade_date": "2026-11-01", "shares": 1, "price": 0.1, "value": 0.1 + index * 1e-12, "accn": accn})
    forward = aggregate_purchases(trades, AS_OF, 30)
    backward = aggregate_purchases(list(reversed(trades)), AS_OF, 30)
    assert forward == backward
    assert forward["0000000001"]["accessions"] == ["a"]


def test_trade_date_follows_the_common_close():
    """A later calendar day with no new bar keeps the session the closes share."""
    assert common_close_date(["2026-11-20", "2026-11-20", "2026-11-19"], date(2026, 11, 23)) == "2026-11-20"
    assert common_close_date(["2026-11-20", "2026-11-21", "2026-11-21"], date(2026, 11, 23)) == "2026-11-21"
    assert common_close_date([], date(2026, 11, 23)) == "2026-11-23"


def test_missing_session_close_uses_regular_session_price():
    payload = {
        "chart": {
            "result": [
                {
                    "timestamp": [1794407400, 1794493800],
                    "indicators": {"quote": [{"close": [10.0, None], "volume": [100, 250]}]},
                    "meta": {"regularMarketTime": 1794517201, "regularMarketPrice": 10.5},
                }
            ]
        }
    }
    got = parse_chart(payload, date(2026, 11, 12))
    assert got["price"] == 10.5
    assert got["price_date"] == "2026-11-12"
    assert got["price_source"] == "yahoo_regular_market_price"
    assert got["volume"] == 250


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


def _share_fact(tag, end, val, filed, accn, form="10-Q", start=None, fp="Q3", fy=2026):
    row = _inst(tag, end, val, form=form, filed=filed, fp=fp, fy=fy, accn=accn, unit="shares")
    if start:
        row = _dur(tag, start, end, val, form=form, filed=filed, fp=fp, fy=fy, accn=accn)
        row["unit"] = "shares"
    return row


def test_newer_share_count_beats_an_old_dei_fact():
    """MA-like: 2010 cover count loses to the 2026 basic share count."""
    facts = {
        "concepts": {
            "dei:EntityCommonStockSharesOutstanding": [
                _share_fact("EntityCommonStockSharesOutstanding", "2010-10-27", 122530193, "2010-11-02", "0001193125-10-243917", fy=2010)
            ],
            "us-gaap:CommonStockSharesOutstanding": [
                _share_fact("CommonStockSharesOutstanding", "2026-09-30", 871176114, "2026-10-29", "0001141391-26-000124")
            ],
            "us-gaap:WeightedAverageNumberOfSharesOutstandingBasic": [
                _share_fact(
                    "WeightedAverageNumberOfSharesOutstandingBasic",
                    "2026-09-30",
                    883000000,
                    "2026-10-29",
                    "0001141391-26-000124",
                    start="2026-01-01",
                ),
                _share_fact(
                    "WeightedAverageNumberOfSharesOutstandingBasic",
                    "2026-09-30",
                    875000000,
                    "2026-10-29",
                    "0001141391-26-000124",
                    start="2026-07-01",
                ),
            ],
        }
    }
    got = select_shares(facts, date(2026, 11, 18), max_age_days=180)
    assert got["ok"] is True
    assert got["value"] == 871176114
    assert got["source"] == "us-gaap:CommonStockSharesOutstanding"
    assert got["end"] == "2026-09-30"
    assert got["kind"] == "instant"
    only_average = {
        "concepts": {
            "dei:EntityCommonStockSharesOutstanding": facts["concepts"]["dei:EntityCommonStockSharesOutstanding"],
            "us-gaap:WeightedAverageNumberOfSharesOutstandingBasic": facts["concepts"][
                "us-gaap:WeightedAverageNumberOfSharesOutstandingBasic"
            ],
        }
    }
    averaged = select_shares(only_average, date(2026, 11, 18), max_age_days=180)
    assert averaged["ok"] is True
    assert averaged["value"] == 883000000
    assert averaged["source"] == "us-gaap:WeightedAverageNumberOfSharesOutstandingBasic"
    assert averaged["kind"] == "weighted_average"
    assert averaged["dei_stale_or_missing"] is True


def test_stale_share_count_is_excluded():
    """TBLA-like: the 2024 cover count is too old, and a 1000x weighted average is ignored."""
    facts = {
        "concepts": {
            "dei:EntityCommonStockSharesOutstanding": [
                _share_fact("EntityCommonStockSharesOutstanding", "2024-10-31", 336208207, "2024-11-07", "0001140361-24-045484", fy=2024)
            ],
            "us-gaap:WeightedAverageNumberOfSharesOutstandingBasic": [
                _share_fact(
                    "WeightedAverageNumberOfSharesOutstandingBasic",
                    "2026-09-30",
                    274600849000,
                    "2026-11-04",
                    "0001840502-26-000018",
                    start="2026-07-01",
                ),
                _share_fact(
                    "WeightedAverageNumberOfSharesOutstandingBasic",
                    "2026-03-31",
                    282244774,
                    "2026-05-06",
                    "0001840502-26-000009",
                    start="2026-01-01",
                    fp="Q1",
                ),
            ],
        }
    }
    got = select_shares(facts, date(2026, 11, 18), max_age_days=180)
    assert got["ok"] is False
    assert got["reason"] == "stale_shares"
    assert "274600849000" not in got["detail"]
    assert got["dei_stale_or_missing"] is True
    only_old = {"concepts": {"dei:EntityCommonStockSharesOutstanding": facts["concepts"]["dei:EntityCommonStockSharesOutstanding"]}}
    old = select_shares(only_old, date(2026, 11, 18), max_age_days=180)
    assert old["reason"] == "stale_shares"
    assert "2024-10-31" in old["detail"]
    assert "dei:EntityCommonStockSharesOutstanding" in old["detail"]


def test_multi_class_from_share_facts_and_titles():
    old = _share_fact("EntityCommonStockSharesOutstanding", "2010-10-27", 122530193, "2010-11-02", "0001193125-10-243917", fy=2010)
    facts = {"concepts": {"dei:EntityCommonStockSharesOutstanding": [old]}}
    extras = [
        {
            "tag": "EntityCommonStockSharesOutstanding",
            "taxonomy": "dei",
            "source": "dei:EntityCommonStockSharesOutstanding",
            "kind": "instant",
            "start": None,
            "end": "2026-10-26",
            "filed": "2026-10-29",
            "val": 864657109,
            "accn": "0001141391-26-000124",
            "form": "10-Q",
            "member": "us-gaap:CommonClassAMember",
        },
        {
            "tag": "EntityCommonStockSharesOutstanding",
            "taxonomy": "dei",
            "source": "dei:EntityCommonStockSharesOutstanding",
            "kind": "instant",
            "start": None,
            "end": "2026-10-26",
            "filed": "2026-10-29",
            "val": 6519005,
            "accn": "0001141391-26-000124",
            "form": "10-Q",
            "member": "us-gaap:CommonClassBMember",
        },
        {
            "tag": "Security12bTitle",
            "text": "Class A Common Stock, par value $0.0001 per share",
            "member": "us-gaap:CommonClassAMember",
        },
        {"tag": "Security12bTitle", "text": "2.1% Notes due 2027", "member": "ma:NotesMember"},
    ]
    got = select_shares(facts, date(2026, 11, 18), max_age_days=180, extras=extras)
    assert got["ok"] is False
    assert got["reason"] == "multi_class_shares_unsplit"
    assert "CommonClassAMember" in got["detail"]
    assert "CommonClassBMember" in got["detail"]
    assert "122530193" not in got["detail"]
    titles_only = [
        {"tag": "Security12bTitle", "text": "Class A Common Stock"},
        {"tag": "Security12bTitle", "text": "Class B Common Stock"},
    ]
    titled = select_shares(facts, date(2026, 11, 18), max_age_days=180, extras=titles_only)
    assert titled["reason"] == "multi_class_shares_unsplit"
    assert "Class A Common Stock" in titled["detail"]
    assert listed_multi_class([{"ticker": "MA"}, {"ticker": "MA"}]) is False
    assert listed_multi_class([{"ticker": "GOOG"}, {"ticker": "GOOGL"}]) is True
    assert listed_multi_class([{"ticker": "BRK-A"}]) is False


def test_custom_class_member_is_not_used_alone():
    """AOS-like cover: Class A plus a custom member. One member is not the share count."""
    facts = {
        "concepts": {
            "dei:EntityCommonStockSharesOutstanding": [
                _share_fact(
                    "EntityCommonStockSharesOutstanding",
                    "2015-08-05",
                    75732188,
                    "2015-08-10",
                    "0001193125-15-284570",
                    fy=2015,
                )
            ]
        }
    }
    extras = []
    for member, val in (
        ("us-gaap:CommonClassAMember", 25860239),
        ("aos:CommonStockClassUndefinedMember", 108468717),
    ):
        extras.append(
            {
                "tag": "EntityCommonStockSharesOutstanding",
                "taxonomy": "dei",
                "source": "dei:EntityCommonStockSharesOutstanding",
                "kind": "instant",
                "start": None,
                "end": "2026-10-27",
                "filed": "2026-10-29",
                "val": val,
                "accn": "0000091142-26-000104",
                "form": "10-Q",
                "member": member,
                "dimension": "us-gaap:StatementClassOfStockAxis",
            }
        )
    blocked = select_shares(facts, date(2026, 11, 19), max_age_days=180, extras=extras)
    assert blocked["ok"] is False
    assert blocked["reason"] == "multi_class_shares_unsplit"
    assert "CommonClassAMember" in blocked["detail"]
    assert "CommonStockClassUndefinedMember" in blocked["detail"]
    summed = select_shares(facts, date(2026, 11, 19), max_age_days=180, extras=extras, sum_classes=True)
    assert summed["ok"] is True
    assert summed["value"] == 25860239 + 108468717
    assert summed["source"] == "class_sum"
    assert summed["value"] != 25860239
    xml = """<?xml version="1.0"?>
    <xbrl xmlns:dei="http://xbrl.sec.gov/dei/2025" xmlns:us-gaap="http://fasb.org/us-gaap/2025" xmlns:aos="http://www.aosmith.com/20260930" xmlns:xbrldi="http://xbrl.org/2006/xbrldi">
      <context id="c-a">
        <entity><identifier>91142</identifier>
          <segment><xbrldi:explicitMember dimension="us-gaap:StatementClassOfStockAxis">us-gaap:CommonClassAMember</xbrldi:explicitMember></segment>
        </entity>
        <period><instant>2026-10-27</instant></period>
      </context>
      <context id="c-b">
        <entity><identifier>91142</identifier>
          <segment><xbrldi:explicitMember dimension="us-gaap:StatementClassOfStockAxis">aos:CommonStockClassUndefinedMember</xbrldi:explicitMember></segment>
        </entity>
        <period><instant>2026-10-27</instant></period>
      </context>
      <dei:EntityCommonStockSharesOutstanding contextRef="c-a" unitRef="shares">25860239</dei:EntityCommonStockSharesOutstanding>
      <dei:EntityCommonStockSharesOutstanding contextRef="c-b" unitRef="shares">108468717</dei:EntityCommonStockSharesOutstanding>
    </xbrl>
    """
    parsed = {row["member"]: row["val"] for row in parse_share_document(xml)}
    assert parsed["us-gaap:CommonClassAMember"] == 25860239
    assert parsed["aos:CommonStockClassUndefinedMember"] == 108468717


def test_share_decline_after_the_balance_sheet_is_flagged():
    facts = {
        "concepts": {
            "dei:EntityCommonStockSharesOutstanding": [
                _share_fact("EntityCommonStockSharesOutstanding", "2026-11-06", 153292826, "2026-11-10", "new")
            ],
            "us-gaap:CommonStockSharesOutstanding": [
                _share_fact("CommonStockSharesOutstanding", "2026-09-30", 199275907, "2026-11-10", "bs")
            ],
        }
    }
    assert large_share_decline_after_balance_sheet(facts, date(2026, 11, 19), 153292826, "2026-11-06", "2026-09-30") is True
    assert large_share_decline_after_balance_sheet(facts, date(2026, 11, 19), 190000000, "2026-11-06", "2026-09-30") is False
    assert large_share_decline_after_balance_sheet(facts, date(2026, 11, 19), 153292826, "2026-09-30", "2026-09-30") is False


def test_near_zero_enterprise_value_is_guarded():
    guards = {"min_enterprise_value": 50000000, "min_ev_to_market_cap": 0.1, "max_earnings_yield": 1.0}
    tiny = {"enterprise_value": 16300000, "market_cap": 400000000, "earnings_yield": 9.76}
    reason, detail = apply_magic_guards(tiny, guards)
    assert reason == "enterprise_value_below_min"
    assert "16300000" in detail
    assert "9.76" in detail
    ratio = {"enterprise_value": 80000000, "market_cap": 1000000000, "earnings_yield": 0.4}
    assert apply_magic_guards(ratio, guards)[0] == "ev_to_market_cap_below_min"
    hot = {"enterprise_value": 200000000, "market_cap": 400000000, "earnings_yield": 2.5}
    assert apply_magic_guards(hot, guards)[0] == "earnings_yield_above_max"
    fine = {"enterprise_value": 500000000, "market_cap": 600000000, "earnings_yield": 0.08}
    assert apply_magic_guards(fine, guards) is None
    assert apply_magic_guards(tiny, {}) is None


def test_cover_document_reads_class_members():
    xml = """<?xml version="1.0"?>
    <xbrl xmlns:dei="http://xbrl.sec.gov/dei/2024" xmlns:xbrldi="http://xbrl.org/2006/xbrldi">
      <context id="c-a">
        <entity>
          <identifier>1141391</identifier>
          <segment>
            <xbrldi:explicitMember dimension="us-gaap:StatementClassOfStockAxis">us-gaap:CommonClassAMember</xbrldi:explicitMember>
          </segment>
        </entity>
        <period><instant>2026-10-26</instant></period>
      </context>
      <context id="c-b">
        <entity>
          <identifier>1141391</identifier>
          <segment>
            <xbrldi:explicitMember dimension="us-gaap:StatementClassOfStockAxis">us-gaap:CommonClassBMember</xbrldi:explicitMember>
          </segment>
        </entity>
        <period><instant>2026-10-26</instant></period>
      </context>
      <dei:EntityCommonStockSharesOutstanding contextRef="c-a" unitRef="shares">864657109</dei:EntityCommonStockSharesOutstanding>
      <dei:EntityCommonStockSharesOutstanding contextRef="c-b" unitRef="shares">6519005</dei:EntityCommonStockSharesOutstanding>
    </xbrl>
    """
    rows = parse_share_document(xml)
    by_member = {row["member"]: row["val"] for row in rows}
    assert by_member["us-gaap:CommonClassAMember"] == 864657109
    assert by_member["us-gaap:CommonClassBMember"] == 6519005
    assert rows[0]["end"] == "2026-10-26"
    assert rows[0]["source"] == "dei:EntityCommonStockSharesOutstanding"
    html = """
    <html><body>
    <ix:nonFraction name="dei:EntityCommonStockSharesOutstanding" contextRef="c1" scale="0" unitRef="shares">883,000,000</ix:nonFraction>
    </body></html>
    <context id="c1"><period><instant>2026-09-30</instant></period></context>
    """
    parsed = parse_share_document(html)
    assert parsed[0]["val"] == 883000000
    assert parsed[0]["end"] == "2026-09-30"
    picked = instance_name(
        [
            {"name": "R1.htm", "size": "10"},
            {"name": "exhibit31.htm", "size": "99"},
            {"name": "ma-20260930.htm", "size": "50"},
            {"name": "ma-20260930_htm.xml", "size": "20"},
        ]
    )
    assert picked == "ma-20260930_htm.xml"


def test_form4_acceptance_marker_when_the_index_has_no_time():
    rows = [{"form": "4", "filed": "2026-11-18", "accn": "0000000001-26-000001"}]
    assert form4_acceptance_value(rows) == "unknown (master.idx)"
    rows.append({"form": "4", "filed": "2026-11-18", "accepted": "2026-11-18T21:55:03-05:00"})
    assert form4_acceptance_value(rows) == "2026-11-18T21:55:03-05:00"
    assert form4_acceptance_value([{"form": "10-Q", "filed": "2026-11-18"}]) is None


def test_refresh_diff_uses_the_newest_prior_trade_date(tmp_path: Path):
    def dump(folder: Path, trade: str, tickers: list[str], previous):
        folder.mkdir()
        meta = {
            "trade_date": trade,
            "run_date": trade,
            "previous_trade_date": previous,
            "code_version": "abc",
            "price_as_of": trade,
            "min_market_cap": 100000000,
            "magic_top_n": 30,
            "insider_lookback_days": 30,
            "fscore_basis": "annual",
            "fscore_min_score": 8,
        }
        payload = {"meta": meta, "rows": [{"ticker": ticker, "name": ticker} for ticker in tickers]}
        text = json.dumps(payload)
        for name in ("magic_formula", "fscore", "insider_buying", "intersection", "diff"):
            (folder / f"{name}.json").write_text(text, encoding="utf-8")
        (folder / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
        (folder / "VERIFICATION.md").write_text(
            f"Trade date `{trade}`. Run date `{trade}`. Generated at `{trade}`.\n"
            f"Code `abc`. Previous trade date for diff `{previous}`.\n",
            encoding="utf-8",
        )

    dump(tmp_path / "2026-11-12", "2026-11-12", ["OLD"], None)
    dump(tmp_path / "2026-11-18", "2026-11-18", ["NEW"], None)
    assert refresh_diff(tmp_path) == "2026-11-12"
    meta = json.loads((tmp_path / "2026-11-18" / "metadata.json").read_text(encoding="utf-8"))
    assert meta["previous_trade_date"] == "2026-11-12"
    diff = json.loads((tmp_path / "2026-11-18" / "diff.json").read_text(encoding="utf-8"))
    changes = {(row["change"], row["ticker"]) for row in diff["rows"] if row["list_name"] == "magic_formula"}
    assert ("entered", "NEW") in changes
    assert ("exited", "OLD") in changes
    note = (tmp_path / "VERIFICATION.md").read_text(encoding="utf-8")
    assert "Previous trade date for diff `2026-11-12`" in note
    # A later folder can carry an earlier price-session trade_date. The diff
    # baseline is the previous folder, not the price session inside the file.
    dump(tmp_path / "2026-11-23", "2026-11-20", ["LATE"], None)
    assert refresh_diff(tmp_path) == "2026-11-18"
    late = json.loads((tmp_path / "2026-11-23" / "metadata.json").read_text(encoding="utf-8"))
    assert late["trade_date"] == "2026-11-20"
    assert late["previous_trade_date"] == "2026-11-18"
