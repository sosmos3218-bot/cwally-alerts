# Verification of three US screen rows

Trade date `2026-11-12`. Run label `2026-11-13`. Code `01d380697968c74519ae239499906ab4c608a647`.

The three names are a seeded sample of the Magic Formula rows that also have an F-score of 8 or 9. Pool: `OTEX`, `CVSA`, `LAUR`, `MU`, `CCSI`. `random.Random(20261113).sample` returned `CVSA`, `OTEX`, `MU`.

Each companyfacts document was fetched again from `data.sec.gov` and passed through the same selectors. Every figure below matched that live response. Market cap equals the printed close times the share count.

Filings below are EDGAR accessions. Concept names are `us-gaap` unless marked `dei`.

## CVSA, Covista Inc., CIK 0000730464

Close `119` on `2026-11-12` (`yahoo_chart_close`). Shares `33,255,241` (`dei:EntityCommonStockSharesOutstanding`, end `2026-10-23`, accession `0001104659-26-120213`, filed `2026-10-29`). Market cap `3,957,373,679`.

EBIT is a year-to-date bridge of `OperatingIncomeLoss`, all USD:

| Piece | Period | Value | Form | Accession | Filed |
| --- | --- | --- | --- | --- | --- |
| Current quarter | 2026-07-01 to 2026-09-30 | 98,027,000 | 10-Q Q1 | 0001104659-26-120213 | 2026-10-29 |
| Prior fiscal year | 2025-07-01 to 2026-06-30 | 383,376,000 | 10-K FY | 0001104659-26-092044 | 2026-08-06 |
| Prior-year quarter | 2025-07-01 to 2025-09-30 | 85,477,000 | 10-Q Q1 | 0001104659-26-120213 | 2026-10-29 |

`98,027,000 + 383,376,000 - 85,477,000 = 395,926,000`. That is the stored EBIT.

Debt on `2026-09-30`, same 10-Q accession `0001104659-26-120213`: `LongTermDebtNoncurrent` `495,131,000` plus `LongTermDebtCurrent` `5,100,000` = `500,231,000`. Cash `CashAndCashEquivalentsAtCarryingValue` `222,223,000`. Enterprise value `3,957,373,679 + 500,231,000 - 222,223,000 = 4,235,381,679`. Earnings yield `395,926,000 / 4,235,381,679 = 0.0934806`.

Working capital on `2026-09-30`, same accession: `AssetsCurrent` `552,570,000` minus `LiabilitiesCurrent` `684,497,000` = `-131,927,000`. Net equipment `PropertyPlantAndEquipmentNet` `324,706,000`. Capital `-131,927,000 + 324,706,000 = 192,779,000`. Return on capital `395,926,000 / 192,779,000 = 2.05378179`.

F-score `8` from the annual facts. One point is off: long-term debt / assets rose, so `f_dlever` is 0. Equity issuance was not tagged; common shares fell, so `f_eqoffer` is 1.

| Input | Year ended 2026-06-30 | Year ended 2025-06-30 | Concept | Accession |
| --- | --- | --- | --- | --- |
| Net income | 251,566,000 | 237,065,000 | NetIncomeLoss | 0001104659-26-092044 |
| Assets | 3,012,708,000 | 2,752,352,000 | Assets | 2026 from 0001104659-26-120213; 2025 from 0001104659-26-092044 |
| Assets, prior beginning |  | 2,741,417,000 (2024-06-30) | Assets | 0001558370-25-010780 |
| Operating cash flow | 470,422,000 |  | NetCashProvidedByUsedInOperatingActivities | 0001104659-26-092044 |
| Long-term debt | 657,750,000 | 552,669,000 | LongTermDebtNoncurrent | 2026 from 0001104659-26-120213; 2025 from 0001104659-26-092044 |
| Current assets | 652,441,000 | 416,190,000 | AssetsCurrent | same pair of accessions as assets |
| Current liabilities | 596,820,000 | 507,927,000 | LiabilitiesCurrent | same pair |
| Revenue | 1,954,085,000 | 1,788,290,000 | Revenues | 0001104659-26-092044 |
| Cost of goods and services | 833,660,000 |  | CostOfGoodsAndServicesSold | 0001104659-26-092044 |
| Gross profit | 1,120,425,000 | 1,016,860,000 | revenue minus that cost | same accession |
| Common shares | 34,033,000 | 35,952,000 | CommonStockSharesOutstanding | 2026 from 0001104659-26-120213; 2025 from 0001104659-26-092044 |

`1,954,085,000 - 833,660,000 = 1,120,425,000`. ROA `251,566,000 / 2,752,352,000` is positive and above the prior year. Cash flow over beginning assets is positive and above ROA. Current ratio and gross margin and asset turnover all rose. Leverage `657,750,000 / 3,012,708,000` is above `552,669,000 / 2,752,352,000`, so that point is 0. Total 8. Match.

## OTEX, Open Text Corp, CIK 0001002638

Close `24.81` on `2026-11-12`. Shares `241,539,906` (`dei:EntityCommonStockSharesOutstanding`, end `2026-10-30`, accession `0001002638-26-000098`, filed `2026-11-05`). Market cap `5,992,605,067.86`.

EBIT bridge of `OperatingIncomeLoss`:

| Piece | Period | Value | Form | Accession | Filed |
| --- | --- | --- | --- | --- | --- |
| Current quarter | 2026-07-01 to 2026-09-30 | 250,811,000 | 10-Q Q1 | 0001002638-26-000098 | 2026-11-05 |
| Prior fiscal year | 2025-07-01 to 2026-06-30 | 1,082,597,000 | 10-K FY | 0001002638-26-000068 | 2026-08-06 |
| Prior-year quarter | 2025-07-01 to 2025-09-30 | 269,949,000 | 10-Q Q1 | 0001002638-26-000098 | 2026-11-05 |

`250,811,000 + 1,082,597,000 - 269,949,000 = 1,063,459,000`.

Debt on `2026-09-30`, accession `0001002638-26-000098`: `LongTermDebtNoncurrent` `5,730,527,000` plus `LongTermDebtCurrent` `35,850,000` = `5,766,377,000`. Cash `983,292,000`. Enterprise value `5,992,605,067.86 + 5,766,377,000 - 983,292,000 = 10,775,690,067.86`. Earnings yield `0.09869057`.

`AssetsCurrent` `2,015,269,000` minus `LiabilitiesCurrent` `2,374,851,000` = `-359,582,000`. `PropertyPlantAndEquipmentNet` `524,125,000`. Capital `164,543,000`. Return on capital `1,063,459,000 / 164,543,000 = 6.46310691`. The ratio is large because working capital is negative and net equipment is small. That is the formula, not a second data source.

F-score `8`. `f_eqoffer` is 0 because `ProceedsFromIssuanceOfCommonStock` for the year ended `2026-06-30` is `56,419,000` in accession `0001002638-26-000068`. The other eight points are 1.

| Input | 2026-06-30 | 2025-06-30 | Concept | Accession |
| --- | --- | --- | --- | --- |
| Net income | 643,022,000 | 435,868,000 | NetIncomeLoss | 0001002638-26-000068 |
| Assets | 13,106,504,000 | 13,774,064,000 | Assets | 2026 from 0001002638-26-000098; 2025 from 0001002638-26-000068 |
| Assets, prior beginning |  | 14,205,707,000 (2024-06-30) | Assets | 0001002638-25-000053 |
| Operating cash flow | 1,006,817,000 |  | NetCashProvidedByUsedInOperatingActivities | 0001002638-26-000068 |
| Long-term debt | 5,734,519,000 | 6,342,071,000 | LongTermDebtNoncurrent | 2026 from 0001002638-26-000098; 2025 from 0001002638-26-000068 |
| Current assets | 2,117,873,000 | 2,201,458,000 | AssetsCurrent | same pair as assets |
| Current liabilities | 2,623,325,000 | 2,747,054,000 | LiabilitiesCurrent | same pair |
| Revenue | 5,246,401,000 | 5,168,405,000 | RevenueFromContractWithCustomerExcludingAssessedTax | 0001002638-26-000068 |
| Gross profit | 3,868,461,000 | 3,734,287,000 | GrossProfit | 0001002638-26-000068 |
| Common shares | 242,126,460 | 254,784,391 | CommonStockSharesOutstanding | 2026 from 0001002638-26-000098; 2025 from 0001002638-26-000068 |

ROA, cash flow, the ROA increase, accruals, the leverage decline, the current-ratio increase, the margin increase, and the turnover increase all score 1. Issuance proceeds are positive, so that point is 0. Total 8. Match.

## MU, Micron Technology Inc., CIK 0000723125

Close `798.44` on `2026-11-12` (`yahoo_chart_close`). Shares `1,131,423,212` (`dei:EntityCommonStockSharesOutstanding`, end `2026-10-02`, accession `0000723125-26-000023`, filed `2026-10-09`). Market cap `903,373,549,389.28`.

EBIT is the fiscal-year `OperatingIncomeLoss` `99,340,000,000` for `2025-08-29` to `2026-09-03`, form 10-K, accession `0000723125-26-000023`, filed `2026-10-09`. No bridge was needed.

`LongTermDebtNoncurrent` is not tagged on `2026-09-03` (the non-dimensional series ends in 2013). Debt uses `LongTermDebtAndCapitalLeaseObligations` `4,688,000,000` on that date, same accession. A separate `DebtCurrent` of `491,000,000` is not added, because the lease-inclusive total is the complete balance under the documented order. Cash `CashAndCashEquivalentsAtCarryingValue` `38,364,000,000`. Enterprise value `903,373,549,389.28 + 4,688,000,000 - 38,364,000,000 = 869,697,549,389.28`. Earnings yield `99,340,000,000 / 869,697,549,389.28 = 0.11422362`.

`AssetsCurrent` `91,070,000,000` minus `LiabilitiesCurrent` `27,482,000,000` = `63,588,000,000`. `PropertyPlantAndEquipmentNet` stops in 2020 in companyfacts. Net equipment uses `PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization` `63,310,000,000` on `2026-09-03`, same 10-K accession. Capital `126,898,000,000`. Return on capital `99,340,000,000 / 126,898,000,000 = 0.78283346`.

F-score `8`. `f_eqoffer` is 0 because common shares rose from `1,122,000,000` to `1,131,000,000` and issuance proceeds were not tagged. The other eight points are 1.

| Input | 2026-09-03 | 2025-08-28 | Concept | Accession |
| --- | --- | --- | --- | --- |
| Net income | 84,969,000,000 | 8,539,000,000 | NetIncomeLoss | 0000723125-26-000023 |
| Assets | 195,888,000,000 | 82,798,000,000 | Assets | 0000723125-26-000023 |
| Assets, prior beginning |  | 69,416,000,000 (2024-08-29) | Assets | 0000723125-25-000028 |
| Operating cash flow | 89,675,000,000 |  | NetCashProvidedByUsedInOperatingActivities | 0000723125-26-000023 |
| Long-term debt | 2,792,000,000 | 11,533,000,000 | LongTermDebt | 2026 from 0000723125-26-000023; 2025 from 0000723125-25-000028 |
| Current assets | 91,070,000,000 | 28,841,000,000 | AssetsCurrent | 0000723125-26-000023 |
| Current liabilities | 27,482,000,000 | 11,454,000,000 | LiabilitiesCurrent | 0000723125-26-000023 |
| Revenue | 133,188,000,000 | 37,378,000,000 | RevenueFromContractWithCustomerExcludingAssessedTax | 0000723125-26-000023 |
| Gross profit | 107,504,000,000 | 14,873,000,000 | GrossProfit | 0000723125-26-000023 |
| Common shares | 1,131,000,000 | 1,122,000,000 | CommonStockSharesOutstanding | 0000723125-26-000023 |

The leverage point uses `LongTermDebt`, not the lease-inclusive total used for enterprise value. Both numbers are in the same 10-K. `2,792,000,000 / 195,888,000,000` is below `11,533,000,000 / 82,798,000,000`, so leverage fell and that point is 1. Share count rose, so the equity point is 0. Total 8. Match.

## Result

SEC concept, period, accession, and value matched the stored EBIT, debt pieces, cash, share count, working-capital pieces, net equipment, and F-score inputs for all three names. No mismatched fact. Enterprise value and both ratios equal the arithmetic of those facts and the close.

The 2026-11-12 folder was regenerated after the Form 4 feed change (`f2f66eedb28e96324de6ee4d8fdc6363c0983f36`). CVSA, OTEX, and MU still show the same EBIT, enterprise value, and net equipment figures.

## Form 4 coverage for 2026-11-12

Acceptance cutoff recorded in the output: `2026-11-12T23:59:59-05:00`. Latest Form 4 filing date included: `2026-11-12`. Latest acceptance time included: `2026-11-12T21:55:03-05:00`. The Nov 11 daily index is absent (US holiday). The current-filings feed supplied 1,673 Form 4 rows for that gap, and the Nov 12 daily index is now present as well. Filings accepted on Nov 13 are after the cutoff and are not included.

### ABCL open-market purchase

Issuer ABCELLERA BIOLOGICS INC., CIK `0001703057`, ticker ABCL. One reporting owner in the screen row.

| Field | Screen row | EDGAR Form 4 |
| --- | --- | --- |
| Accession | 0001834449-26-000009 | 0001834449-26-000009 |
| Insider | Thermopylae Holdings Ltd. | `rptOwnerName` Thermopylae Holdings Ltd., `rptOwnerCik` 0001834449 |
| Role | ten_percent_owner | `isTenPercentOwner` 1, `isDirector` 0, `isOfficer` 0 |
| Date | 2026-11-12 | `transactionDate` 2026-11-12 |
| Code | P, acquired A | `transactionCode` P, `transactionAcquiredDisposedCode` A |
| Shares | 60,565 | `transactionShares` 60565 |
| Price | 11.54 | `transactionPricePerShare` 11.54 |
| Value | 698,920.10 | 60,565 times 11.54 = 698,920.10 |

The form's footnote says 11.54 is the weighted-average price of several fills from 11.29 to 11.86. The screen uses the price the form reports. Match.

Filing: https://www.sec.gov/Archives/edgar/data/1834449/000183444926000009/0001834449-26-000009-index.htm

Submission text: https://www.sec.gov/Archives/edgar/data/1834449/000183444926000009/0001834449-26-000009.txt
