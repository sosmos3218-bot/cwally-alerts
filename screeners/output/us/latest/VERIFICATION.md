# US quant screen verification

Trade date `2026-11-23`. Run date `2026-11-23`. Generated at `2026-11-23T18:32:42.133023+09:00`.
Code `54ce8c8d3a4d3abde914dbd638944e972105cd4a`. Previous trade date for diff `2026-11-20`.

This file is rewritten by every screen run. The rows are rule-based screening records.

The weekday schedule is GitHub cron `30 22 * * 0-4` (07:30 Asia/Seoul). GitHub may start that job 4 to 6 hours later. This screen does not add an external scheduler.

## Magic Formula top rows

| combined_rank | ticker | name | shares | shares_end | shares_source | shares_kind | market_cap | enterprise_value | earnings_yield |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 64 | SGU | STAR GROUP, L.P. | 32811216 | 2026-07-31 | dei:EntityCommonStockSharesOutstanding | instant | 436717284.96 | 582283284.96 | 0.23425539 |
| 68 | ABUS | ARBUTUS BIOPHARMA CORP | 153292826 | 2026-11-06 | dei:EntityCommonStockSharesOutstanding | instant | 616237160.52 | 582046160.52 | 0.25283218 |
| 79 | IRWD | IRONWOOD PHARMACEUTICALS INC | 165550760 | 2026-10-31 | dei:EntityCommonStockSharesOutstanding | instant | 778088572 | 1016712572 | 0.24767964 |
| 97 | TBLA | TABOOLA.COM LTD. | 266273301 | 2026-10-30 | class_sum | instant | 495268339.86 | 445091339.86 | 0.33099498 |
| 107 | KBR | KBR, INC. | 126108987 | 2026-10-21 | dei:EntityCommonStockSharesOutstanding | instant | 3964866551.28 | 6066866551.28 | 0.12016088 |
| 131 | HON | HONEYWELL INTERNATIONAL INC | 317358518 | 2026-09-30 | dei:EntityCommonStockSharesOutstanding | instant | 67483115267.52 | 78382115267.52 | 0.1091448 |
| 147 | BBWI | BATH & BODY WORKS, INC. | 197824759 | 2026-11-13 | dei:EntityCommonStockSharesOutstanding | instant | 3691410002.94 | 6740410002.94 | 0.1712062 |
| 148 | HLF | HERBALIFE LTD. | 104895787 | 2026-10-26 | dei:EntityCommonStockSharesOutstanding | instant | 1629031572.11 | 3149031572.11 | 0.16036676 |
| 149 | OTEX | OPEN TEXT CORP | 241539906 | 2026-10-30 | dei:EntityCommonStockSharesOutstanding | instant | 5466048072.78 | 10249133072.78 | 0.10376087 |
| 155 | CROX | CROCS, INC. | 45725039 | 2026-10-22 | dei:EntityCommonStockSharesOutstanding | instant | 7072291782.13 | 8197878782.13 | 0.11281528 |
| 164 | MU | MICRON TECHNOLOGY INC | 1131423212 | 2026-10-02 | dei:EntityCommonStockSharesOutstanding | instant | 672642413766.12 | 639457413766.12 | 0.15535045 |
| 169 | STRL | STERLING INFRASTRUCTURE, INC. | 30502537 | 2026-10-27 | dei:EntityCommonStockSharesOutstanding | instant | 7562188973.04 | 7440521973.04 | 0.10535336 |
| 171 | BXBL | BOXABL INC. | 241989146 | 2026-11-13 | class_sum | instant | 254088603.3 | 247787603.3 | 0.10299482 |
| 171 | MGTX | MEIRAGTX HOLDINGS PLC | 97730540 | 2026-11-04 | dei:EntityCommonStockSharesOutstanding | instant | 779889709.2 | 717254709.2 | 0.2512385 |
| 172 | YELP | YELP INC | 54492897 | 2026-10-30 | dei:EntityCommonStockSharesOutstanding | instant | 991225796.43 | 946385796.43 | 0.17837017 |
| 181 | MNR | MACH NATURAL RESOURCES LP | 170395787 | 2026-10-30 | dei:EntityCommonStockSharesOutstanding | instant | 1354646506.65 | 2479987506.65 | 0.10100938 |
| 187 | CVSA | COVISTA INC. | 33255241 | 2026-10-23 | dei:EntityCommonStockSharesOutstanding | instant | 3629144450.33 | 3907152450.33 | 0.10133365 |
| 190 | DFIN | DONNELLEY FINANCIAL SOLUTIONS, INC. | 24353215 | 2026-10-23 | dei:EntityCommonStockSharesOutstanding | instant | 1571512963.95 | 1671312963.95 | 0.09058746 |
| 196 | LAUR | LAUREATE EDUCATION, INC. | 135745894 | 2026-09-30 | dei:EntityCommonStockSharesOutstanding | instant | 4352013361.64 | 4346780361.64 | 0.11376305 |
| 196 | LOCO | EL POLLO LOCO HOLDINGS, INC. | 30218346 | 2026-11-03 | dei:EntityCommonStockSharesOutstanding | instant | 450857722.32 | 470442722.32 | 0.10921415 |
| 197 | VSNT | VERSANT MEDIA GROUP, INC. | 136692681 | 2026-10-20 | class_sum | instant | 4606543349.7 | 6481543349.7 | 0.17248978 |
| 205 | PARR | PAR PACIFIC HOLDINGS, INC. | 50076962 | 2026-10-30 | dei:EntityCommonStockSharesOutstanding | instant | 3181389395.86 | 2946487395.86 | 0.43145374 |
| 207 | NUTX | NUTEX HEALTH INC. | 6860794 | 2026-11-02 | dei:EntityCommonStockSharesOutstanding | instant | 1115839536.16 | 908232536.16 | 0.34242222 |
| 209 | CCSI | CONSENSUS CLOUD SOLUTIONS, INC. | 17807355 | 2026-11-02 | dei:EntityCommonStockSharesOutstanding | instant | 686651608.8 | 1129561608.8 | 0.1291687 |
| 209 | NATL | NCR ATLEOS CORP | 73939883 | 2026-10-28 | dei:EntityCommonStockSharesOutstanding | instant | 3515102037.82 | 5748102037.82 | 0.1007289 |
| 215 | THC | TENET HEALTHCARE CORP | 79588000 | 2026-10-23 | dei:EntityCommonStockSharesOutstanding | instant | 20946765720 | 31351765720 | 0.1722391 |
| 220 | REZI | RESIDEO TECHNOLOGIES, INC. | 152167728 | 2026-11-03 | dei:EntityCommonStockSharesOutstanding | instant | 3098134942.08 | 4955134942.08 | 0.11361951 |
| 223 | AVAH | AVEANNA HEALTHCARE HOLDINGS, INC. | 218409599 | 2026-11-06 | dei:EntityCommonStockSharesOutstanding | instant | 2404689684.99 | 3502105684.99 | 0.08677836 |
| 227 | PXED | PHOENIX EDUCATION PARTNERS, INC. | 36676644 | 2026-10-27 | dei:EntityCommonStockSharesOutstanding | instant | 721062821.04 | 561699821.04 | 0.21837465 |
| 229 | AMN | AMN HEALTHCARE SERVICES INC | 38562904 | 2026-11-03 | dei:EntityCommonStockSharesOutstanding | instant | 1375924414.72 | 1863316414.72 | 0.10879204 |

## Exclusion counts

| reason | count |
| --- | --- |
| debt_unconfirmed | 267 |
| ebit_missing | 113 |
| cash_missing | 102 |
| multi_class_shares_unsplit | 97 |
| ebit_provisional | 96 |
| capital_non_positive | 80 |
| net_fixed_assets_missing | 76 |
| stale_statement | 49 |
| working_capital_missing | 19 |
| enterprise_value_non_positive | 7 |
| enterprise_value_below_min | 6 |
| earnings_yield_above_max | 1 |
| stale_price | 1 |

## Checked names

MA, TBLA, and CRMD are listed when they are in this run, with the share fields or the exclusion reason.

- `MA`: not in the magic list or the excluded list.
- `TBLA`: on the magic list. shares `266273301` source `class_sum` end `2026-10-30` kind `instant` market_cap `495268339.86` enterprise_value `445091339.86` earnings_yield `0.33099498`.
- `CRMD`: excluded. reason `debt_unconfirmed` detail `no debt tag on 2026-09-30; liabilities 456039000 explained 183959000 residual 272080000`.

## Form 4

Acceptance cutoff `2026-11-23T23:59:59-05:00`.
Latest Form 4 filing date `2026-11-20`.
Latest acceptance `unknown (master.idx)`.
A value of `unknown (master.idx)` means the index rows in this run had no acceptance time.

## Counts

Magic input rows `1338`.
Magic top `30`.
Excluded `914`.
Universe `2251`.
