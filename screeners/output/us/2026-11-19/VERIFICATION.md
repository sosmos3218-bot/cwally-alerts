# US quant screen verification

Trade date `2026-11-19`. Run date `2026-11-20`. Generated at `2026-11-20T12:37:57.331870+09:00`.
Code `cb4c3499f2a35d5cf901da7c973eb1f50345f303`. Previous trade date for diff `2026-11-18`.

This file is rewritten by every screen run. The rows are rule-based screening records.

The weekday schedule is GitHub cron `30 22 * * 0-4` (07:30 Asia/Seoul). GitHub may start that job 4 to 6 hours later. This screen does not add an external scheduler.

## Magic Formula top rows

| combined_rank | ticker | name | shares | shares_end | shares_source | shares_kind | market_cap | enterprise_value | earnings_yield |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 45 | COLL | COLLEGIUM PHARMACEUTICAL, INC | 31075424 | 2026-10-31 | dei:EntityCommonStockSharesOutstanding | instant | 783411439.04 | 638614439.04 | 0.19428468 |
| 56 | ABUS | ARBUTUS BIOPHARMA CORP | 153292826 | 2026-11-06 | dei:EntityCommonStockSharesOutstanding | instant | 623901801.82 | 359710801.82 | 0.40910642 |
| 67 | IRWD | IRONWOOD PHARMACEUTICALS INC | 165550760 | 2026-10-31 | dei:EntityCommonStockSharesOutstanding | instant | 779744079.6 | 693368079.6 | 0.36318228 |
| 68 | SGU | STAR GROUP, L.P. | 32811216 | 2026-07-31 | dei:EntityCommonStockSharesOutstanding | instant | 436717284.96 | 582283284.96 | 0.23425539 |
| 117 | KBR | KBR, INC. | 126108987 | 2026-10-21 | dei:EntityCommonStockSharesOutstanding | instant | 4034226494.13 | 6136226494.13 | 0.11880266 |
| 128 | HON | HONEYWELL INTERNATIONAL INC | 317358518 | 2026-09-30 | dei:EntityCommonStockSharesOutstanding | instant | 69133379561.12 | 73825379561.12 | 0.11588156 |
| 141 | AMN | AMN HEALTHCARE SERVICES INC | 38562904 | 2026-11-03 | dei:EntityCommonStockSharesOutstanding | instant | 1324635752.4 | 1068657752.4 | 0.18969029 |
| 150 | BBWI | BATH & BODY WORKS, INC. | 197824759 | 2026-11-13 | dei:EntityCommonStockSharesOutstanding | instant | 3740866192.69 | 6789866192.69 | 0.16995917 |
| 152 | HLF | HERBALIFE LTD. | 104895787 | 2026-10-26 | dei:EntityCommonStockSharesOutstanding | instant | 1633227403.59 | 3153227403.59 | 0.16015337 |
| 152 | OTEX | OPEN TEXT CORP | 241539906 | 2026-10-30 | dei:EntityCommonStockSharesOutstanding | instant | 5391170701.92 | 10174255701.92 | 0.1045245 |
| 165 | CROX | CROCS, INC. | 45725039 | 2026-10-22 | dei:EntityCommonStockSharesOutstanding | instant | 7193005885.09 | 8318592885.09 | 0.11117818 |
| 167 | SD | SANDRIDGE ENERGY INC | 37107240 | 2026-10-29 | dei:EntityCommonStockSharesOutstanding | instant | 444173662.8 | 375507662.8 | 0.19901059 |
| 171 | YELP | YELP INC | 54492897 | 2026-10-30 | dei:EntityCommonStockSharesOutstanding | instant | 1011388168.32 | 891548168.32 | 0.18934142 |
| 175 | MGTX | MEIRAGTX HOLDINGS PLC | 97730540 | 2026-11-04 | dei:EntityCommonStockSharesOutstanding | instant | 795526595.6 | 610876595.6 | 0.2949892 |
| 177 | MNR | MACH NATURAL RESOURCES LP | 170395787 | 2026-10-30 | dei:EntityCommonStockSharesOutstanding | instant | 1378501916.83 | 2503842916.83 | 0.10004701 |
| 187 | AOS | SMITH A O CORP | 25860239 | 2026-10-27 | dei:EntityCommonStockSharesOutstanding | instant | 1647555826.69 | 2086155826.69 | 0.31891194 |
| 193 | MAN | MANPOWERGROUP INC. | 46505570 | 2026-10-28 | dei:EntityCommonStockSharesOutstanding | instant | 2030898241.9 | 2360598241.9 | 0.12844202 |
| 194 | DFIN | DONNELLEY FINANCIAL SOLUTIONS, INC. | 24353215 | 2026-10-23 | dei:EntityCommonStockSharesOutstanding | instant | 1592943793.15 | 1692743793.15 | 0.08944059 |
| 195 | NATL | NCR ATLEOS CORP | 73939883 | 2026-10-28 | dei:EntityCommonStockSharesOutstanding | instant | 3525453621.44 | 5758453621.44 | 0.10054783 |
| 198 | MU | MICRON TECHNOLOGY INC | 1131423212 | 2026-10-02 | dei:EntityCommonStockSharesOutstanding | instant | 781179842493.28 | 747503842493.28 | 0.13289564 |
| 199 | CVSA | COVISTA INC. | 33255241 | 2026-10-23 | dei:EntityCommonStockSharesOutstanding | instant | 3841312887.91 | 4119320887.91 | 0.09611439 |
| 203 | STRL | STERLING INFRASTRUCTURE, INC. | 30502537 | 2026-10-27 | dei:EntityCommonStockSharesOutstanding | instant | 8334818235.25 | 8213151235.25 | 0.09544254 |
| 205 | LAUR | LAUREATE EDUCATION, INC. | 135745894 | 2026-09-30 | dei:EntityCommonStockSharesOutstanding | instant | 4410384096.06 | 4405151096.06 | 0.11225563 |
| 205 | LOCO | EL POLLO LOCO HOLDINGS, INC. | 30218346 | 2026-11-03 | dei:EntityCommonStockSharesOutstanding | instant | 452368639.62 | 471953639.62 | 0.10886451 |
| 211 | NGS | NATURAL GAS SERVICES GROUP INC | 12905548 | 2026-11-06 | dei:EntityCommonStockSharesOutstanding | instant | 371937893.36 | 371229893.36 | 0.11812896 |
| 212 | PARR | PAR PACIFIC HOLDINGS, INC. | 50076962 | 2026-10-30 | dei:EntityCommonStockSharesOutstanding | instant | 3220950195.84 | 2986048195.84 | 0.4257376 |
| 214 | AVAH | AVEANNA HEALTHCARE HOLDINGS, INC. | 218409599 | 2026-11-06 | dei:EntityCommonStockSharesOutstanding | instant | 2336982709.3 | 3434398709.3 | 0.08848914 |
| 215 | LOPE | GRAND CANYON EDUCATION, INC. | 25510517 | 2026-11-02 | dei:EntityCommonStockSharesOutstanding | instant | 2837279700.74 | 2778519700.74 | 0.11270678 |
| 218 | CCSI | CONSENSUS CLOUD SOLUTIONS, INC. | 17807355 | 2026-11-02 | dei:EntityCommonStockSharesOutstanding | instant | 693240330.15 | 1136150330.15 | 0.12841963 |
| 219 | NUTX | NUTEX HEALTH INC. | 6860794 | 2026-11-02 | dei:EntityCommonStockSharesOutstanding | instant | 1112134707.4 | 904527707.4 | 0.34382474 |

## Exclusion counts

| reason | count |
| --- | --- |
| multi_class_shares_unsplit | 296 |
| ebit_missing | 113 |
| cash_missing | 102 |
| ebit_provisional | 97 |
| net_fixed_assets_missing | 78 |
| capital_non_positive | 76 |
| stale_statement | 50 |
| working_capital_missing | 19 |
| enterprise_value_below_min | 11 |
| enterprise_value_non_positive | 9 |
| stale_price | 2 |
| earnings_yield_above_max | 1 |

## Checked names

MA, TBLA, and CRMD are listed when they are in this run, with the share fields or the exclusion reason.

- `MA`: excluded. reason `multi_class_shares_unsplit` detail `us-gaap:CommonClassAMember 864657109 end 2026-10-26 source dei:EntityCommonStockSharesOutstanding; us-gaap:CommonClassBMember 6519005 end 2026-10-26 source dei:EntityCommonStockSharesOutstanding`.
- `TBLA`: excluded. reason `multi_class_shares_unsplit` detail `us-gaap:NonvotingCommonStockMember 18039644 end 2026-10-30 source dei:EntityCommonStockSharesOutstanding; tbla:VotingCommonStockMember 248233657 end 2026-10-30 source dei:EntityCommonStockSharesOutstanding`.
- `CRMD`: excluded. reason `enterprise_value_below_min` detail `enterprise_value 48901140.07999998 market_cap 404051140.08 earnings_yield 3.252991642725726 ev_to_market_cap 0.12102711 min_enterprise_value 50000000.0`.

## Form 4

Acceptance cutoff `2026-11-19T23:59:59-05:00`.
Latest Form 4 filing date `2026-11-19`.
Latest acceptance `unknown (master.idx)`.
A value of `unknown (master.idx)` means the index rows in this run had no acceptance time.

## Counts

Magic input rows `1415`.
Magic top `30`.
Excluded `854`.
Universe `2267`.
