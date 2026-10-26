# Retired alerts

Alerts removed from this repo after every oneshot_kst / end date had passed. The files (configs, scripts,
state) stay in git history: `git log --all -- configs/<name>.json` and `git show <commit>^:configs/<name>.json`.
Sent messages stay in state/archive.json (until rotated) and in cwally-ntfy-archive.

## 2026-10-26 cleanup (branch cleanup/ntfy-2026-10-26)

Removed: config, scripts/<name>_monitor.py, state file, port-alerts.yml schedule crons, Decide branch, Run
step, workflow_dispatch job option / mode input, AGENTS.md title prefix (only when no other alert uses it).
cron-job.org one-shot jobs for these times are listed in the PR body (deleted separately, not by this PR).

| name | prefix | added | oneshot window (KST) | what it did |
|---|---|---|---|---|
| 0144m0 | `[0144M0]` | 2026-10-07 | 2026-10-07 16:20 ~ 2026-10-07 16:20 | KODEX 미국드론UAM TOP10 |
| bankrate1014 | `[BANKRATE]` | 2026-10-07 | 2026-10-14 08:00 ~ 2026-10-14 08:00 | 10/13 미국 JPM, WFC, 씨티, 골드만삭스 실적과 10/13 10년물 종가(5.31% 재돌파 여부)로 시나리오 A~D를 판정하고, 10... |
| cpi1014 | `[CPI]` | 2026-10-03 | 2026-10-14 22:45 ~ 2026-10-15 09:10 | 10월 14일 밤 미국 9월 CPI를 헤드라인, 근원, 에너지, 주거로 나누고 10월 동결이 깨지는 수치와 12월 인상 확률 변화 조건을 정리하... |
| dearattr | `[DEAR]` | 2026-10-12 | 2026-10-13 09:30 ~ 2026-10-14 09:30 | 디어유 |
| fedodds | `[FOMC]` | 2026-10-06 | 2026-10-08 03:35 ~ 2026-10-15 09:25 | 9월 의사록과 9월 CPI 이후 CME 연방기금 선물로 10월 동결 77~85%와 12월 인상 가격이 유지되는지 판정하는 2026년 일회 알림입... |
| fomc1007 | `[FOMC]` | 2026-10-05 | 2026-10-08 03:20 ~ 2026-10-08 09:10 | 9월 FOMC 의사록에서 추가 인상, 유가, 고용 둔화 문장을 세어 매파와 비둘기 비중을 나누고 다우, 반도체, 2년물 영향을 판단하는 2026... |
| isrgq3 | `[ISRG]` | 2026-10-06 | 2026-10-21 05:40 ~ 2026-10-21 08:00 | 인튜이티브 서지컬 |
| k227540 | `[227540]` | 2026-10-20 | 2026-10-21 16:40 ~ 2026-10-23 15:52 | 삼성바이오 3Q 실적 + 227540 1차 매수 판정 |
| kosreb | `[KOSREB]` | 2026-10-14 | 2026-10-14 23:10 ~ 2026-10-15 09:20 | 키움 10/14 리포트 반등 시나리오를 9월 CPI 결과, 14일 코스피 마감과 외국인 수급, 반도체 선행 PER, 27~28년 컨센 방향으로 ... |
| sapq3 | `[SAPQ3]` | 2026-10-19 | 2026-10-22 07:10 ~ 2026-10-22 09:10 | SAP Q3 실적 후 조건부 매수 재평가 |
| semicrit | `[SEMCRIT]` | 2026-10-12 | 2026-10-13 09:05 ~ 2026-10-15 09:05 | 2026-10-09 영상(오픈AI 매출 혼선, 순환금융, 솔리다임, 외국인 SK하이닉스 매도) 기반 보수적 관찰. 자동 매수/매도 아님. |
| sena | `[SENA]` | 2026-10-12 | 2026-10-21 09:05 ~ 2026-10-21 09:05 | 삼성E&A |
| tsmc1015 | `[TSMC]` | 2026-10-05 | 2026-10-15 16:30 ~ 2026-10-15 17:10 | TSMC 3분기 보도자료와 콜만 보고 달러 매출, 매출총이익률, 4분기 가이던스, 2026-2027 CAPEX, 웨이퍼 가격을 추출해 472달러... |
| usd240 | `[USD240]` | 2026-10-07 | 2026-10-07 16:05 ~ 2026-10-13 16:05 | 261240 KODEX 미국달러선물 규칙을 10월물 종가로 판정하는 2026년 일회 알림입니다. |
| ustsy10 | `[USTSY]` | 2026-09-27 | 2026-10-02 00:30 ~ 2026-10-09 02:15 | 10월 1일 재무부 10년, 30년 입찰 공고의 확정 규모, CUSIP, WI 개시와, 10/6 3년, 10/7 10년, 10/8 30년 결과가... |
| ktb148070 | `[KTB148070]` | 2026-10-20 | 2026-10-22 15:30 ~ 2026-10-22 15:30 | 금통위 후 148070 분할매수 판정. 한은 결정 판정 함수는 scripts/bok_mpc.py 로 이동(krrate 사용) |

## 2026-10-26 merge: pcor into pcorq3 (approved in chat 2026-10-26, "적절히 통합하기")

Disabled, not deleted: `configs/pcor.json` has `disabled: true`, `oneshot_kst: []`, `ends_kst` 2026-10-26 11:00, and
`scripts/pcor_monitor.py` exits at once when disabled. `state/pcor.json` is kept as history (it never sent).
Update 2026-10-26 (PR #15): the disabled shell is now deleted too (config, script, state, `[PCOR]` prefix).
Nothing dispatches job=pcor: it is not a dispatch option, no Decide line or schedule cron maps to it, and
cron-job.org 8589357 / 8589366 send `job=oneshot` (picked as pcorq3).
Its Run step was removed; its GitHub fallback crons (`0 23 4 11 *`, `0 9 5 11 *`) now map to `JOB=pcorq3`, and its
two one-shot times (2026-11-05 08:00 and 18:00 KST) moved to `configs/pcorq3.json` `oneshot_kst`, so the existing
cron-job.org jobs 8589357 and 8589366 (payload `job=oneshot`, titles still say pcor) now run pcorq3.

| name | prefix | added | oneshot window (KST) | what it did |
|---|---|---|---|---|
| pcor | `[PCOR]` | 2026-10-05 | 2026-11-05 08:00 ~ 2026-11-05 18:00 | Procore 3분기 6개 항목(매출, 영업이익률, FCF, 10만 달러 고객, 유지율, 2027 가이던스) 통과/보류/축소 채점. pcorq3 로 통합 |

## 2026-10-26 retire fxbox, krbank (approved in chat 2026-10-26, "fxbox, krbank 폐기")

Both had `ends_kst` in the past, so `ntfy_util.publish()` already blocked every send, but they still ran in
slots. No other script imported or read their files and no cron-job.org job pointed at them. Removed: config,
script, state, fxbox `run_one` line and `state/fxbox.json` in flow-alerts.yml (flow slot itself kept),
krbank Run block, step name/if term and job option in port-alerts.yml, `[FXBOX]` `[KRBANK]` from AGENTS.md
and README.md. No crons were removed (both rode on shared slots).

| name | prefix | added | end (KST) | what it did |
|---|---|---|---|---|
| fxbox | `[FXBOX]` | 2026-10-07 | 2026-10-15 15:40 | 원달러 1,338~1,348원 박스 전망 마감 사후검증 (flow 15:40 슬롯) |
| krbank | `[KRBANK]` | 2026-09-28 | 2026-09-28 11:25 | 우리금융, 하나금융 목표가와 분할매도 구간 1회 안내 (morning 09:00 슬롯) |

## 2026-10-26 delete pcor shell (merged into pcorq3 by c5635aa)

| name | prefix | added | oneshot window (KST) | what it did |
|---|---|---|---|---|
| pcor (shell) | `[PCOR]` | 2026-10-05 | none (moved to pcorq3) | c5635aa 에서 pcorq3 로 통합된 뒤 남은 disabled config, script, state 삭제. 기능은 pcorq3 에 있음 |

## 2026-10-26 audit: lges and lgeshold shells (approved in chat 2026-10-26 18:15 KST, "기타 현 시점 비효율적이거나 쓸데없는 알림 있는지 체크해서 통합하거나 삭제해 줘")

Both were folded into `[LGES]` lgeswatch earlier the same day (commit 0431f79) and left as config-flag-off shells that
exited at once on every 15:20 afternoon run. Nothing else read their config or state. Deleted: config, script, state,
the two afternoon Run lines, the dry check in the LGES step, and the `[LGESH]` title prefix (`[LG]` stays: lg, lgcnsq3,
lgesq3 still use it). No schedule cron, Decide branch, dispatch option or cron-job.org job pointed at them.
Same pattern as the pcor shell (PR #15). To restore: `git show <this PR merge>^:configs/<name>.json`.

| name | prefix | added | what it did | now in |
|---|---|---|---|---|
| lges | `[LG]` | 2026-10-06 | 373220 new exchange disclosures (실적, 공급계약, ESS/GM/AMPC) on the 15:20 slot; `disclosure_alerts=false` since 2026-10-26 | lgeswatch KIND filing watch (19:00) |
| lgeshold | `[LGESH]` | 2026-10-14 | 373220 zones: stop 300,000 / add 360,000~400,000 / targets 500,000, 550,000; `zone_alerts=false` since 2026-10-26 | lgeswatch levels (19:00) |

Also removed from the title-prefix registry (AGENTS.md, README.md): `[SMH]` and `[ZS]`, used by no config, script or
outbox file (their alerts were removed earlier).

## 2026-10-26 audit: fig and ccrev (approved in chat 2026-10-26 18:22 KST, "네 소신껏 진행", audit item 2)

Both ran in the morning 09:00 bundle with no own cron, no Decide branch, no `oneshot_kst` and no cron-job.org job, so
no cron was removed. Deleted: config, script, state, the morning Run lines, the `job=fig` dry-check step, the `fig` and
`ccrev` dispatch options (and `ccrev` from the step name/if), and the `[FIG]` `[CCREV]` title prefixes. `[FIG-PX]`,
`[FIG-EARN]`, `[FIG-Q3]` stay and keep the FIG price zones and the Q3 earnings coverage. The dashboard `planRemoval`
plan for both keys matched this diff. To restore: `git show <this PR merge>^:configs/<name>.json`.

| name | prefix | added | what it did | why retired |
|---|---|---|---|---|
| fig | `[FIG]` | 2026-10-16 | Figma Monday status and new SEC filings (price zones 20/27 off since 2026-10-26); ends 2026-11-15 | covered by `[FIG-PX]` 22 / 16.6 / 28.4 zones, `[FIG-EARN]` 11/4 reminder and 11/5 checklist, `[FIG-Q3]` 11/5 summary |
| ccrev | `[CCREV]` | 2026-09-17 | 091160 55→40% cut + covered call review when KOSPI held a 60-day box (range under 18%) for 45 days and 005930/000660 estimates fell 3%; no end date | condition far off (60-day range 29% on 2026-10-26), never sent in the 9/21~10/26 archive; 091160 levels stay in `[091160]` |

## 2026-10-26 audit: samup, hynixup, hynixlad, lgcns folded into the plan-line engine (approved in chat 2026-10-26 18:22 KST, audit item 4)

Every level moved into `extra_lines` of the 005930 engine configs (notify-only, own message in the engine wording,
state in `state/<name>.json` `extra`, fired/armed state migrated so nothing re-fires). The plan `lines`
(buy/trim/stop) stay authoritative; zone wording that suggests buying is dropped when the close is under the stop line.
All four now run on the 19:00 k203780 slot from the Yahoo regular close (slot change noted below). Deleted: config,
script, state, Run lines (`flow-alerts.yml` run_one + git add paths, `port-alerts.yml` morning/afternoon lines) and
the `[SAMUP]` `[HYNIXUP]` `[HYNIXLAD]` `[LGCNS]` prefixes. No cron or cron-job.org job pointed at them.
To restore: `git show <this PR merge>^:configs/<name>.json`.

| name | prefix | old slot | levels | now in |
|---|---|---|---|---|
| samup | `[SAMUP]` | flow 15:40, Naver current price | 220,000 / 230,000 up, re-arm on any close below | 005930 `up220k` `up230k` (rearm_pct 0) |
| hynixup | `[HYNIXUP]` | flow 15:40, Naver current price | 1,300,000 / 1,400,000 up, re-arm on any close below | 000660 `up1300k` `up1400k` (rearm_pct 0) |
| hynixlad | `[HYNIXLAD]` | morning 09:00, previous Naver close | 1,450,000 ladder exhausted, 1,322,000 July low, +3% recovery messages; ends 2026-12-30 | 000660 `lad_exhaust` `lad_july` (rearm_pct 3, rearm_title, until 2026-12-30) |
| lgcns | `[LGCNS]` | afternoon 15:20, Naver price | zones 60,000 / 80,000 / 90,000 (DEEP, SPLIT, NONEW, STOP), sends on zone change + Monday weekly; ends 2026-12-31 | 064400 `z60_*` `z80_*` `z90_*` (one pair per boundary, rearm_pct 0). The Monday weekly is dropped. |

lgcnsq3 (`[LG]`, 3Q judgment until 11/16) is unchanged. d0chk was not folded in this PR (done in the next one, below): its price
trigger needs Naver volume, which the `basic` endpoint no longer returns, so it never fired although the price is
under 15,300, and it conflicts with ssb0005d0 BELOW_NOV. Left for a decision.

## 2026-10-26 audit: d0chk folded into ssb0005d0 (approved in chat 2026-10-26 18:22 KST, audit item 5)

Both were `[0005D0]` (same ETF). The d0chk 1% checklist (price <= 15,300 with volume, 247540 Q3 earnings filing,
006400 US LFP start; each once) is now the `checklist()` stage of `scripts/ssb0005d0_monitor.py`, run right after the
condition watch on the 15:20 afternoon slot. Config moved to `configs/ssb0005d0.json` `"checklist"`, state to
`state/ssb0005d0.json` `"checklist"` (values copied: price_sent, earn_sent, lfp_sent all false). Same triggers, texts
and once-only flags. Changes: the 09:00 morning check is dropped (15:20 only); when the watch zone is under 15,000
(TREND/BELOW/BELOW_NOV) the price item says "1% 매수 보류" instead of "주식 1% 매수" (no buy advice next to the
watch's 추가매수 보류). Known, unchanged: Naver `basic` returns no volume field, so the price item (needs volume > 0)
has not fired although the price is under 15,300; fixing that would send once at once, so it is left for a decision.

| name | prefix | added | old slots | now in |
|---|---|---|---|---|
| d0chk | `[0005D0]` | 2026-10-06 | 09:00 morning + 15:20 afternoon | ssb0005d0 `checklist()` (15:20) |

Follow-up 2026-10-26 22:40 KST (approved in chat): the price item reads volume from Naver's polling endpoint
(`accumulatedTradingVolumeRaw`, `basic` stays the fallback; with no volume field at all, a quote stamped today while
trading counts), so it can fire; expected once at the 10/27 15:20 slot (price 14,180, zone BELOW_NOV, so the title is
"1.5만 미만이라 1% 매수 보류"), then `price_sent` stops it. The earnings item also matches the standard KRX title
"연결재무제표 기준 영업(잠정)실적(공정공시)" (parentheses ignored); because that title names no quarter, it counts only when
filed on or after `earn_from` (2026-10-01), so the 2026-07-31 Q2 filing never matches. `--dry-run` no longer writes state.

## 2026-10-26 audit: KOSPI ladder (approved in chat 2026-10-26 18:22 KST, audit item 7)

kosdown `[KOSDOWN]`, kosup `[KOSUP]`, kosbrk `[KOSBRK]` and kos5500 `[KOS5500]` could each send a separate message
on the same 15:40 flow run, and kos5500 repeated its report every trading day plus again at the next 09:00. They now
send one `[KOSPI]` message. Nothing was deleted: each component keeps its script, `configs/<name>.json`,
`state/<name>.json`, thresholds and re-arm logic, and still runs on its own `run_one` line in `flow-alerts.yml`. With
`KOSPI_LADDER=1` their signal sends are spooled by `scripts/kospi_spool.py` and `scripts/kospi_monitor.py send`
publishes the combined message; `begin` snapshots the component states and a failed publish restores them. State
migration: none needed for the components (fired flags unchanged); `state/kospi.json` was seeded with
`kos5500_last_label = "5500 이탈 첫날"` (sent 2026-10-26 15:40), so that label is not re-sent. Old vs new were run on
throwaway copies (ntfy blocked, faketime 10/26 15:41, 10/27 15:40, 10/28 15:40): same silence, identical component
states. Retired: the kos5500 09:00 morning repeat (`kos5500_monitor.py previous` in the morning bundle). k200,
kosown and macroir are unchanged.

| name | prefix | old slots | now |
|---|---|---|---|
| kosdown, kosup, kosbrk | `[KOSDOWN]` `[KOSUP]` `[KOSBRK]` | flow 15:40, one message each | flow 15:40, a section of `[KOSPI]` |
| kos5500 | `[KOS5500]` | flow 15:40 daily + 09:00 morning repeat | flow 15:40, a `[KOSPI]` section only when the scenario label changes |
