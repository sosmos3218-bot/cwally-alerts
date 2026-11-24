# US quant screen verification

Trade date `2026-11-23`. Run date `2026-11-24`. Generated at `2026-11-24T18:32:39.857743+09:00`.
Code `e165bc82590e9b061161982f6e2a0593be687b75`. Previous trade date for diff `2026-11-23`.

This file is rewritten by every screen run. The rows are rule-based screening records.

The weekday schedule is GitHub cron `30 22 * * 0-4` (07:30 Asia/Seoul). GitHub may start that job 4 to 6 hours later. This screen does not add an external scheduler.

## Magic Formula top rows

| combined_rank | ticker | name | shares | shares_end | shares_source | shares_kind | market_cap | enterprise_value | earnings_yield |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 63 | SGU | STAR GROUP, L.P. | 32811216 | 2026-07-31 | dei:EntityCommonStockSharesOutstanding | instant | 431795602.56 | 577361602.56 | 0.23625229 |
| 69 | ABUS | ARBUTUS BIOPHARMA CORP | 153292826 | 2026-11-06 | dei:EntityCommonStockSharesOutstanding | instant | 640764012.68 | 606573012.68 | 0.24260888 |
| 79 | IRWD | IRONWOOD PHARMACEUTICALS INC | 165550760 | 2026-10-31 | dei:EntityCommonStockSharesOutstanding | instant | 819476262 | 1058100262 | 0.23799162 |
| 99 | TBLA | TABOOLA.COM LTD. | 266273301 | 2026-10-30 | class_sum | instant | 505919271.9 | 455742271.9 | 0.32325946 |
| 108 | KBR | KBR, INC. | 126108987 | 2026-10-21 | dei:EntityCommonStockSharesOutstanding | instant | 4114936245.81 | 6216936245.81 | 0.11726033 |
| 141 | HON | HONEYWELL INTERNATIONAL INC | 317358518 | 2026-09-30 | dei:EntityCommonStockSharesOutstanding | instant | 70444070240.46 | 81343070240.46 | 0.10517184 |
| 143 | OTEX | OPEN TEXT CORP | 241539906 | 2026-10-30 | dei:EntityCommonStockSharesOutstanding | instant | 5557833237.06 | 10340918237.06 | 0.1028399 |
| 145 | BXBL | BOXABL INC. | 241989146 | 2026-11-13 | class_sum | instant | 241989146 | 235688146 | 0.10828223 |
| 147 | BBWI | BATH & BODY WORKS, INC. | 197824759 | 2026-11-13 | dei:EntityCommonStockSharesOutstanding | instant | 3835822077.01 | 6884822077.01 | 0.16761508 |
| 150 | HLF | HERBALIFE LTD. | 104895787 | 2026-10-26 | dei:EntityCommonStockSharesOutstanding | instant | 1685675297.09 | 3205675297.09 | 0.15753311 |
| 155 | MNR | MACH NATURAL RESOURCES LP | 170395787 | 2026-10-30 | dei:EntityCommonStockSharesOutstanding | instant | 1277968402.5 | 2403309402.5 | 0.10423211 |
| 156 | CROX | CROCS, INC. | 45725039 | 2026-10-22 | dei:EntityCommonStockSharesOutstanding | instant | 7214496653.42 | 8340083653.42 | 0.11089169 |
| 171 | YELP | YELP INC | 54492897 | 2026-10-30 | dei:EntityCommonStockSharesOutstanding | instant | 992315654.37 | 947475654.37 | 0.178165 |
| 176 | MGTX | MEIRAGTX HOLDINGS PLC | 97730540 | 2026-11-04 | dei:EntityCommonStockSharesOutstanding | instant | 835596117 | 772961117 | 0.23313204 |
| 181 | CVSA | COVISTA INC. | 33255241 | 2026-10-23 | dei:EntityCommonStockSharesOutstanding | instant | 3682020283.52 | 3960028283.52 | 0.0999806 |
| 188 | DFIN | DONNELLEY FINANCIAL SOLUTIONS, INC. | 24353215 | 2026-10-23 | dei:EntityCommonStockSharesOutstanding | instant | 1595866178.95 | 1695666178.95 | 0.08928644 |
| 192 | MU | MICRON TECHNOLOGY INC | 1131423212 | 2026-10-02 | dei:EntityCommonStockSharesOutstanding | instant | 800708207132.4 | 767523207132.4 | 0.12942931 |
| 194 | VSNT | VERSANT MEDIA GROUP, INC. | 136692681 | 2026-10-20 | class_sum | instant | 4657119641.67 | 6532119641.67 | 0.17115424 |
| 196 | LAUR | LAUREATE EDUCATION, INC. | 135745894 | 2026-09-30 | dei:EntityCommonStockSharesOutstanding | instant | 4415813931.82 | 4410580931.82 | 0.11211743 |
| 199 | NATL | NCR ATLEOS CORP | 73939883 | 2026-10-28 | dei:EntityCommonStockSharesOutstanding | instant | 3586823724.33 | 5819823724.33 | 0.09948755 |
| 203 | STRL | STERLING INFRASTRUCTURE, INC. | 30502537 | 2026-10-27 | dei:EntityCommonStockSharesOutstanding | instant | 8503192239.49 | 8381525239.49 | 0.09352522 |
| 204 | LOCO | EL POLLO LOCO HOLDINGS, INC. | 30218346 | 2026-11-03 | dei:EntityCommonStockSharesOutstanding | instant | 465060344.94 | 484645344.94 | 0.1060136 |
| 207 | PARR | PAR PACIFIC HOLDINGS, INC. | 50076962 | 2026-10-30 | dei:EntityCommonStockSharesOutstanding | instant | 3175380160.42 | 2940478160.42 | 0.43233547 |
| 213 | CCSI | CONSENSUS CLOUD SOLUTIONS, INC. | 17807355 | 2026-11-02 | dei:EntityCommonStockSharesOutstanding | instant | 719239068.45 | 1162149068.45 | 0.12554672 |
| 213 | NUTX | NUTEX HEALTH INC. | 6860794 | 2026-11-02 | dei:EntityCommonStockSharesOutstanding | instant | 1169765377 | 962158377 | 0.32323057 |
| 215 | THC | TENET HEALTHCARE CORP | 79588000 | 2026-10-23 | dei:EntityCommonStockSharesOutstanding | instant | 21727524000 | 32132524000 | 0.16805403 |
| 221 | AMN | AMN HEALTHCARE SERVICES INC | 38562904 | 2026-11-03 | dei:EntityCommonStockSharesOutstanding | instant | 1372839382.4 | 1860231382.4 | 0.10897247 |
| 223 | PXED | PHOENIX EDUCATION PARTNERS, INC. | 36676644 | 2026-10-27 | dei:EntityCommonStockSharesOutstanding | instant | 679251446.88 | 519888446.88 | 0.23593715 |
| 225 | REZI | RESIDEO TECHNOLOGIES, INC. | 152167728 | 2026-11-03 | dei:EntityCommonStockSharesOutstanding | instant | 3312691438.56 | 5169691438.56 | 0.10890399 |
| 228 | AVAH | AVEANNA HEALTHCARE HOLDINGS, INC. | 218409599 | 2026-11-06 | dei:EntityCommonStockSharesOutstanding | instant | 2532459300.405 | 3629875300.405 | 0.08372381 |

## Exclusion counts

| reason | count |
| --- | --- |
| debt_unconfirmed | 271 |
| ebit_missing | 113 |
| cash_missing | 102 |
| multi_class_shares_unsplit | 97 |
| ebit_provisional | 96 |
| capital_non_positive | 82 |
| net_fixed_assets_missing | 77 |
| stale_statement | 51 |
| working_capital_missing | 19 |
| enterprise_value_below_min | 10 |
| enterprise_value_non_positive | 5 |
| stale_price | 1 |

## Checked names

MA, TBLA, and CRMD are listed when they are in this run, with the share fields or the exclusion reason.

- `MA`: not in the magic list or the excluded list.
- `TBLA`: on the magic list. shares `266273301` source `class_sum` end `2026-10-30` kind `instant` market_cap `505919271.9` enterprise_value `455742271.9` earnings_yield `0.32325946`.
- `CRMD`: excluded. reason `debt_unconfirmed` detail `no debt tag on 2026-09-30; liabilities 456039000 explained 183959000 residual 272080000`.

## Form 4

Acceptance cutoff `2026-11-24T23:59:59-05:00`.
Latest Form 4 filing date `2026-11-23`.
Latest acceptance `unknown (master.idx)`.
A value of `unknown (master.idx)` means the index rows in this run had no acceptance time.

## Counts

Magic input rows `1339`.
Magic top `30`.
Excluded `924`.
Universe `2262`.
