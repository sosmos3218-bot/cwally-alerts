# US quant screen verification

Trade date `2026-11-18`. Run date `2026-11-19`. Generated at `2026-11-19T23:00:23.747434+09:00`.
Code `a327738f3a10707e98f0fc45d1a3d97553d130d3`. Previous trade date for diff `2026-11-12`.

This file is rewritten by every screen run. The rows are rule-based screening records.

The weekday schedule is GitHub cron `30 22 * * 0-4` (07:30 Asia/Seoul). GitHub may start that job 4 to 6 hours later. This screen does not add an external scheduler.

## Magic Formula top rows

| combined_rank | ticker | name | shares | shares_end | shares_source | shares_kind | market_cap | enterprise_value | earnings_yield |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 46 | COLL | COLLEGIUM PHARMACEUTICAL, INC | 31075424 | 2026-10-31 | dei:EntityCommonStockSharesOutstanding | instant | 8.0578574e+08 | 6.6098874e+08 | 0.18770819 |
| 58 | ABUS | ARBUTUS BIOPHARMA CORP | 1.5329283e+08 | 2026-11-06 | dei:EntityCommonStockSharesOutstanding | instant | 6.3003351e+08 | 3.6584251e+08 | 0.40224959 |
| 63 | SNDK | SANDISK CORP | 1.4228936e+08 | 2026-10-26 | dei:EntityCommonStockSharesOutstanding | instant | 1.1044785e+11 | 1.0531185e+11 | 0.19934129 |
| 68 | IRWD | IRONWOOD PHARMACEUTICALS INC | 1.6555076e+08 | 2026-10-31 | dei:EntityCommonStockSharesOutstanding | instant | 7.9133263e+08 | 7.0495663e+08 | 0.35721204 |
| 70 | SGU | STAR GROUP, L.P. | 32811216 | 2026-07-31 | dei:EntityCommonStockSharesOutstanding | instant | 4.3277994e+08 | 5.7834594e+08 | 0.23585019 |
| 104 | CCLD | CARECLOUD, INC. | 42548859 | 2026-10-31 | dei:EntityCommonStockSharesOutstanding | instant | 1.0126628e+08 | 96630284 | 0.12992821 |
| 116 | KBR | KBR, INC. | 1.2610899e+08 | 2026-10-21 | dei:EntityCommonStockSharesOutstanding | instant | 4.1716853e+09 | 6.2736853e+09 | 0.11619964 |
| 131 | HON | HONEYWELL INTERNATIONAL INC | 3.1735852e+08 | 2026-09-30 | dei:EntityCommonStockSharesOutstanding | instant | 7.0675742e+10 | 7.5367742e+10 | 0.1135101 |
| 139 | BBWI | BATH & BODY WORKS, INC. | 2.0164531e+08 | 2026-08-21 | dei:EntityCommonStockSharesOutstanding | instant | 3.7627015e+09 | 6.5827015e+09 | 0.18320746 |
| 154 | AMN | AMN HEALTHCARE SERVICES INC | 38562904 | 2026-11-03 | dei:EntityCommonStockSharesOutstanding | instant | 1.4499652e+09 | 1.1939872e+09 | 0.16977904 |
| 155 | HLF | HERBALIFE LTD. | 1.0489579e+08 | 2026-10-26 | dei:EntityCommonStockSharesOutstanding | instant | 1.6825284e+09 | 3.2025284e+09 | 0.15768791 |
| 157 | OTEX | OPEN TEXT CORP | 2.4153991e+08 | 2026-10-30 | dei:EntityCommonStockSharesOutstanding | instant | 5.5481716e+09 | 1.0331257e+10 | 0.10293607 |
| 171 | SD | SANDRIDGE ENERGY INC | 37107240 | 2026-10-29 | dei:EntityCommonStockSharesOutstanding | instant | 4.5233726e+08 | 3.8367126e+08 | 0.19477612 |
| 174 | CROX | CROCS, INC. | 45725039 | 2026-10-22 | dei:EntityCommonStockSharesOutstanding | instant | 7.4422073e+09 | 8.5677943e+09 | 0.10794447 |
| 176 | MGTX | MEIRAGTX HOLDINGS PLC | 97730540 | 2026-11-04 | dei:EntityCommonStockSharesOutstanding | instant | 8.1311809e+08 | 6.2846809e+08 | 0.28673214 |
| 176 | MNR | MACH NATURAL RESOURCES LP | 1.7039579e+08 | 2026-10-30 | dei:EntityCommonStockSharesOutstanding | instant | 1.3631663e+09 | 2.4885073e+09 | 0.10066356 |
| 178 | YELP | YELP INC | 54492897 | 2026-10-30 | dei:EntityCommonStockSharesOutstanding | instant | 1.089313e+09 | 9.6947301e+08 | 0.17412243 |
| 189 | AOS | SMITH A O CORP | 25860239 | 2026-10-27 | dei:EntityCommonStockSharesOutstanding | instant | 1.6692784e+09 | 2.1078784e+09 | 0.31562541 |
| 197 | DFIN | DONNELLEY FINANCIAL SOLUTIONS, INC. | 24353215 | 2026-10-23 | dei:EntityCommonStockSharesOutstanding | instant | 1.6221677e+09 | 1.7219677e+09 | 0.087922674 |
| 199 | MAN | MANPOWERGROUP INC. | 46505570 | 2026-10-28 | dei:EntityCommonStockSharesOutstanding | instant | 2.1011217e+09 | 2.4308217e+09 | 0.12473149 |
| 201 | MU | MICRON TECHNOLOGY INC | 1.1314232e+09 | 2026-10-02 | dei:EntityCommonStockSharesOutstanding | instant | 7.9795885e+11 | 7.6428285e+11 | 0.12997806 |
| 202 | NATL | NCR ATLEOS CORP | 73939883 | 2026-10-28 | dei:EntityCommonStockSharesOutstanding | instant | 3.5550296e+09 | 5.7880296e+09 | 0.10003404 |
| 205 | STRL | STERLING INFRASTRUCTURE, INC. | 30502537 | 2026-10-27 | dei:EntityCommonStockSharesOutstanding | instant | 8.4296811e+09 | 8.3080141e+09 | 0.094352752 |
| 207 | CVSA | COVISTA INC. | 33255241 | 2026-10-23 | dei:EntityCommonStockSharesOutstanding | instant | 3.9843104e+09 | 4.2623184e+09 | 0.092889822 |
| 207 | NGS | NATURAL GAS SERVICES GROUP INC | 12905548 | 2026-11-06 | dei:EntityCommonStockSharesOutstanding | instant | 3.7026017e+08 | 3.6955217e+08 | 0.11866525 |
| 211 | LAUR | LAUREATE EDUCATION, INC. | 1.3574589e+08 | 2026-09-30 | dei:EntityCommonStockSharesOutstanding | instant | 4.5678493e+09 | 4.5626163e+09 | 0.10838146 |
| 214 | PARR | PAR PACIFIC HOLDINGS, INC. | 50076962 | 2026-10-30 | dei:EntityCommonStockSharesOutstanding | instant | 3.2695248e+09 | 3.0346228e+09 | 0.4189229 |
| 216 | AVAH | AVEANNA HEALTHCARE HOLDINGS, INC. | 2.184096e+08 | 2026-11-06 | dei:EntityCommonStockSharesOutstanding | instant | 2.3686521e+09 | 3.4660681e+09 | 0.08768062 |
| 219 | YUM | YUM BRANDS INC | 2.6825866e+08 | 2026-10-30 | dei:EntityCommonStockSharesOutstanding | instant | 4.3135992e+10 | 5.0486992e+10 | 0.086240036 |
| 221 | LOCO | EL POLLO LOCO HOLDINGS, INC. | 30218346 | 2026-11-03 | dei:EntityCommonStockSharesOutstanding | instant | 4.750324e+08 | 4.946174e+08 | 0.10387625 |

## Exclusion counts

| reason | count |
| --- | --- |
| multi_class_shares_unsplit | 297 |
| ebit_missing | 113 |
| cash_missing | 102 |
| ebit_provisional | 98 |
| net_fixed_assets_missing | 79 |
| capital_non_positive | 75 |
| stale_statement | 50 |
| working_capital_missing | 19 |
| enterprise_value_below_min | 9 |
| enterprise_value_non_positive | 9 |
| stale_price | 2 |
| stale_balance_sheet | 1 |

## Checked names

MA, TBLA, and CRMD are listed when they are in this run, with the share fields or the exclusion reason.

- `MA`: excluded. reason `multi_class_shares_unsplit` detail `us-gaap:CommonClassAMember 864657109 end 2026-10-26 source dei:EntityCommonStockSharesOutstanding; us-gaap:CommonClassBMember 6519005 end 2026-10-26 source dei:EntityCommonStockSharesOutstanding`.
- `TBLA`: excluded. reason `multi_class_shares_unsplit` detail `us-gaap:NonvotingCommonStockMember 18039644 end 2026-10-30 source dei:EntityCommonStockSharesOutstanding; tbla:VotingCommonStockMember 248233657 end 2026-10-30 source dei:EntityCommonStockSharesOutstanding`.
- `CRMD`: excluded. reason `enterprise_value_below_min` detail `enterprise_value 2769317.0 market_cap 357919317.0 earnings_yield 57.44196132114886 ev_to_market_cap 0.00773727 min_enterprise_value 50000000.0`.

## Form 4

Acceptance cutoff `2026-11-18T23:59:59-05:00`.
Latest Form 4 filing date `2026-11-18`.
Latest acceptance `unknown (master.idx)`.
A value of `unknown (master.idx)` means the index rows in this run had no acceptance time.

## Counts

Magic input rows `1418`.
Magic top `30`.
Excluded `854`.
Universe `2270`.
