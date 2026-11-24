# US quant screen

Rule-based records for US-listed common stocks. The files are screening outputs. They are not instructions, alerts, or portfolio data. Korea is not fetched. The metric functions take plain numbers so a later KR adapter can supply the same inputs.

Run from the repo root:

```bash
python3 -m pytest screeners/us_quant/tests -q
python3 -m screeners.us_quant selftest
python3 -m screeners.us_quant run --as-of YYYY-MM-DD --run-date YYYY-MM-DD
```

Thresholds live in `config.json`: market-cap floor, prefilter ratio, Magic Formula top N, enterprise-value guards, F-score basis (`annual` only; `ttm` is reserved and refused), F-score list minimum, insider lookback, and staleness windows (including `max_share_age_days`, default 180).

Caches stay in `screeners/cache/us_quant/` (gitignored). A second run for the same as-of date reads that cache and does not refetch EDGAR or prices. Fundamentals refresh only when the index shows a 10-K, 10-Q, or amendment accession that is not already in the companyfacts extract. Compact JSON and CSV go to `screeners/output/us/<as-of date>/` and are copied to `screeners/output/us/latest/`. `trade_date` in those files is the price session: the most common last close among the fetched bars, with the later date winning a tie. The folder stays the as-of date, which is the filing cutoff. `run_date` is the Asia/Seoul date of the run. `generated_at` is the timestamp.

## Universe

- Exchanges in the SEC ticker file: Nasdaq, NYSE, and TXSE. NYSE American is not a separate value in that file.
- One row per CIK. The primary ticker is the common listing with the highest close times volume.
- Ticker shape drops preferred and unit-style suffixes with two or more letters after a hyphen (`BAC-PB`). A single letter class (`BRK-B`) stays in the listing file.
- Names containing ETF, ETN, Fund, Warrant, Preferred, Debenture, Notes, Acquisition Corp, Closed-End, or Unit are left out.
- Market cap floor is `min_market_cap` (default $100,000,000). The share-count prefilter keeps names at or above `market_cap_prefilter_ratio` times that floor so a stale share count does not drop every split. The published market cap uses the later companyfacts share count times the close. Names still under the floor are left out of the outputs.
- More than one common ticker for the same CIK in `company_tickers_exchange.json` is `multi_class_shares_unsplit`, including when one ticker has no price. Those issuers are not ranked, and market cap is not summed across listed tickers. Companyfacts drops class-axis members, so when the undimensioned `dei:EntityCommonStockSharesOutstanding` fact is missing or older than `max_share_age_days` the latest 10-Q or 10-K instance is read. Two common or ordinary `dei:Security12bTitle` values use the same reason. One listed ticker with two or more cover-page share values on the class axis is different: every member is summed, including a custom extension name, and a single member is never used alone.

SIC exclusions (inclusive), from the SEC financial-statement SUB table, with a submissions lookup when SUB has no row:

| Group | SIC | What it covers |
| --- | --- | --- |
| utilities | 4900-4999 | Major group 49: electric, gas, and sanitary services |
| financials | 6000-6799 | Division H: banks, credit, brokers, insurance, real estate, holding and investment offices, including REITs (6798) |

Foreign filers are issuers whose forms include 20-F or 40-F and do not include a 10-K or 10-Q. Funds that never file a 10-K or 10-Q are left out the same way.

## Magic Formula

- EBIT is trailing twelve months of `us-gaap:OperatingIncomeLoss` from 10-K and 10-Q facts filed on or before the as-of date.
- If the longest fact ending on the latest period is about a year, that value is EBIT.
- Otherwise EBIT = current year-to-date + prior fiscal year - the prior year-to-date of the same length. The prior year-to-date must end within 20 days of one year earlier.
- If that bridge is missing, EBIT is the sum of the last four quarter-length facts when each gap is 70 to 140 days. Anything shorter is incomplete and the issuer is listed under excluded.
- Enterprise value = market cap + total debt - cash.
- Market cap = close times shares. The newest count wins. An older `dei:EntityCommonStockSharesOutstanding` fact is never kept ahead of a newer one. A count older than `max_share_age_days` (default 180) is rejected. When that cover-page fact is stale or missing, the screen uses the latest 10-Q or 10-K cover value, then `us-gaap:CommonStockSharesOutstanding`, then `us-gaap:WeightedAverageNumberOfSharesOutstandingBasic` from the latest filing, each with the same age cap. If none of those is fresh, the issuer is excluded as `stale_shares`. `shares_source`, `shares_kind` (`instant` or `weighted_average`), and `shares_end` are stored on the row. A weighted-average value hundreds of times the latest instant count is ignored. When the cover fact has two or more class-axis values and the issuer has one listed ticker, `shares_source` is `class_sum` and the share count is the sum of those members. `large_buyback_after_bs` is true when that share date is after the balance-sheet date and the count is more than 10 percent below the prior instant count. The row stays in the ranking.
- Debt, on the latest balance-sheet date, uses one total when the filing reports one: `DebtCurrent` + `LongTermDebtNoncurrent` when both are present; else `LongTermDebtNoncurrent` plus `LongTermDebtCurrent`, `ShortTermBorrowings`, and `CommercialPaper` (commercial paper is not added again when short-term borrowings are at least that large). Else the screen takes the larger of two constructions. One is `LongTermDebtAndFinanceLeaseObligations` or `LongTermDebtAndCapitalLeaseObligations` (the larger one if both exist) plus the current portion on that same date: the matching `Current` tag, else `LongTermDebtCurrent`, else `DebtCurrent`, plus short-term borrowings and commercial paper counted once. `DebtCurrent` is not added on top of borrowings and paper when it already covers them, unless `LongTermDebt` shows it is only the current slice of long-term debt. The other construction is `LongTermDebt` plus those same short tags. The lease tag is usually the noncurrent line, so the current portion has to be added; `LongTermDebt` alone misses finance leases that are only in the lease tag. A current-maturity tag within 2 percent of short-term borrowings or commercial paper is the same balance, because issuance costs sit between the two tags. The screen keeps the borrowings carrying amount and does not add the current-maturity tag again. When `LongTermDebt` equals `LongTermDebtNoncurrent` plus `LongTermDebtCurrent`, a short-term line that matches that current slice is already inside `LongTermDebt` and is not added on top. A lease tag equal to `LongTermDebt` is not treated as that proof, because both tags can be the noncurrent line. `debt_source` is `lt_and_lease_plus_current` when the lease construction adds a current piece. `DebtAndCapitalLeaseObligations` replaces the result only when it is larger. A smaller figure is not used as a cap, because that tag often leaves out commercial paper. Notes, loans, and credit lines are not added on top of those totals. When no total is present, the screen sums non-overlapping components from that same date: loans (`LongTermLoansPayable` or the current/noncurrent split, plus `LoansPayableCurrent`), notes (`LongTermNotesPayable` or `NotesPayable`, plus `NotesPayableCurrent`; `SeniorNotes` only when they are not already inside the notes figure), convertible notes and convertible debt, drawn credit (`LineOfCredit` or `LongTermLineOfCredit`, plus `LinesOfCreditCurrent`; two tags for the same drawn balance are kept once), `OtherLongTermDebt`, and `SecuredDebt` / `UnsecuredDebt` only when no named instrument above is present. `FinanceLeaseLiability` (or its current and noncurrent parts) is included on this component path. `DebtInstrumentCarryingAmount` is used when it covers that component sum. Facility capacity is not debt. A reported zero is zero (`explicit_zero`). No debt tag on the date is not zero: the issuer is excluded as `debt_unconfirmed`, unless `Liabilities` minus accounts payable, accrued liabilities, contract liabilities or deferred revenue, operating leases, and noncurrent deferred tax is within the greater of 1,000,000 and 2 percent of `Liabilities` (`liabilities_imply_no_debt`).
- When `large_buyback_after_bs` is true and the balance sheet reports an obligation to repurchase shares (`ObligationToRepurchaseShares`, including the Current and Noncurrent tags, or the same words under ShareRepurchaseObligation or LiabilityForShareRepurchase), that amount is subtracted from cash. The cover share count is already after the repurchase and the cash fact is not. The obligation is not also added to debt. Companyfacts omits extension concepts, so the amount is read from the latest 10-Q or 10-K instance. `repurchase_obligation` stores the amount subtracted.
- `one_time_ebit` is a flag, not an exclusion. It is true for a revenue spike (one quarter inside year-to-date revenue is at least 80 percent of that revenue, at least 20,000,000, and the latest quarter is at most 10 percent) or for discontinued operations of at least 50,000,000 and at least 25 percent of operating income on the same period. The name stays ranked. `one_time_ebit_reason` is `revenue_spike`, `discontinued_ops`, or both.
- Cash is `CashAndCashEquivalentsAtCarryingValue`, else `Cash`. The restricted-cash total is not used.
- Net working capital = `AssetsCurrent` - `LiabilitiesCurrent`.
- Net fixed assets = `PropertyPlantAndEquipmentNet` on that balance-sheet date, else `PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization`. If neither tag is on that date, the issuer is excluded (`net_fixed_assets_missing`). An older equipment fact is not carried forward.
- Return on capital = EBIT / (net working capital + net fixed assets). Non-positive enterprise value or capital drops the row.
- Near-zero enterprise value is dropped before the top N is cut. `magic.min_enterprise_value` (default 50,000,000), `magic.min_ev_to_market_cap` (default 0.1), and `magic.max_earnings_yield` (default 1.0) each exclude the issuer. The excluded table `reason` is `enterprise_value_below_min`, `ev_to_market_cap_below_min`, or `earnings_yield_above_max`, and `detail` keeps the figures.
- Earnings yield and return on capital are ranked high-to-low. Equal values break ties by ticker ascending. Combined rank is the sum. The output keeps `magic.top_n` rows (default 30) and every input used in the ratios.

## F-Score

Annual 10-K facts only. Each test is 1 or 0. The listed file keeps scores at or above `fscore.min_score` (default 8).

1. ROA = net income / prior-year assets, point if positive.
2. Operating cash flow / prior-year assets, point if positive.
3. Point if ROA rose.
4. Point if cash flow / assets exceeds ROA.
5. Long-term debt / assets fell. The debt tag is `LongTermDebtNoncurrent`, else `LongTermDebt`, else `LongTermDebtAndCapitalLeaseObligations`. A missing tag is 0.
6. Current ratio rose.
7. Point if common-stock issuance proceeds are not positive. If that tag is absent, point if common shares did not rise.
8. Gross margin rose. Gross profit is `GrossProfit`, else revenue minus cost of revenue.
9. Asset turnover (revenue / prior-year assets) rose.

Revenue uses the first concept that exists for both years: `RevenueFromContractWithCustomerExcludingAssessedTax`, `Revenues`, `SalesRevenueNet`, `SalesRevenueGoodsNet`, then the including-tax contract revenue tag. Net income is `NetIncomeLoss`. Cash flow is `NetCashProvidedByUsedInOperatingActivities`.

## Insider list

EDGAR daily index rows (and the quarterly master index, which is the compilation of those days) supply Form 4 and 4/A filenames. When the daily index for the as-of date is not published yet, the current-filings Atom feed (`browse-edgar?action=getcurrent&type=4&output=atom`, then `4/A`, paginated) fills that gap. A filing is kept only when its acceptance time is at or before 23:59:59 America/New_York on the as-of date. That cutoff, the latest Form 4 filing date, and the latest acceptance time are stored on every output. The master index has no acceptance time. When none of the rows carry one, `form4_latest_acceptance` is `unknown (master.idx)` rather than empty. The same as-of reuses the saved feed page so a later EDGAR update does not change the file. Issuer entries win over the reporting-owner copy of the same accession. The submission text is parsed for `ownershipDocument` transactions with code `P` and acquired code `A`. Dollars are shares times price. The lookback is calendar days of the transaction date, default 30. Rows are summed by issuer inside the same universe: distinct reporting owners, total dollars, director / officer / 10% owner, latest transaction date.

Intersection is the Magic Formula top N or an F-score at the list minimum, and also an open-market purchase in the window.

`diff` compares ticker sets with the newest earlier as-of folder that is already in `screeners/output/us`. `entered` and `exited` are the only changes. The first saved date has no prior folder, so every current ticker is `entered`. The commit step fetches and rebases before that comparison is saved, so the baseline is the newest trade date already on the branch.

`screeners/output/us/VERIFICATION.md` is rewritten on every run, and a copy is stored in the as-of folder. It carries the trade date, run date, and generated-at time. The weekday GitHub cron is `30 22 * * 0-4` (07:30 Asia/Seoul). GitHub may start that job 4 to 6 hours later. No external scheduler was added.

## Data limits

- Prices are Yahoo daily closes. A missing close drops that ticker onto `price_failures`. Each symbol keeps the last bar on or before the as-of date. `trade_date` is the most common of those bar dates, so a run on a later calendar day still names the session the closes belong to. The newest session sometimes has a null daily close while `regularMarketPrice` is already stamped at 16:00 America/New_York; that value is used only when its timestamp falls on the as-of date, and `price_source` says so.
- Cash is cash and cash equivalents only. Short-term marketable securities stay inside enterprise value.
- Return on capital is large when net working capital is thin. That is the arithmetic of this definition.
- Share frames are a prefilter. Issuers with no frame share count are not priced.
- A balance sheet with no debt tag is excluded (`debt_unconfirmed`) unless the filing reports zero or operating liabilities explain total liabilities. Missing net equipment on the balance-sheet date drops the issuer from the Magic Formula list.
- Two listed tickers are not given one combined market cap. One listed ticker sums every class-axis cover value instead of keeping a single member.
- Point-in-time: facts with `filed` after the as-of date are ignored.
- A statement older than `max_statement_age_days` (default 160) or an annual period older than `max_annual_age_days` (default 540) is stale. A share count older than `max_share_age_days` (default 180) is stale.
- Form 4 amendments that change the share count or price can remain beside the original filing. Exact duplicates are collapsed.

EDGAR requests send `SEC_USER_AGENT` when it is set. The fallback is `cwally-alerts research bot admin@cwally-alerts.local` because EDGAR returns 403 when the agent has no contact token. The client stays under 10 requests per second (`sec_max_per_second`, default 7).
