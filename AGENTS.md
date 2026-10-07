# cwally-alerts rules (do not violate)

This repo is the only place for GitHub Actions + ntfy.

- Never create a new repository for an ntfy alert.
- Topics: `cwally-port` for holdings, trades, AND market flow. `cwally-ops` for ops/system alerts only: workflow failures (`scripts/ops_notify.py`) and data-fetch failure notices (titles with 조회 실패/수집 실패/파싱 실패/해석 불가/미게시 are routed by `ntfy_util.route_topic`; custom publishers must call it too). A fetch failure is never a trade signal, so it never goes to `cwally-port`.
- `[PORT]` is only for the port universe check (`port_monitor.py`) and `[FLOW]` only for market/stock investor flow (`flow_monitor`, `secu_flow`). Every other alert uses its own prefix.
- Do not use `cwally-flow` or ad-hoc topics such as `clarity-52505089`.
- Do not let `NTFY_TOPIC` env/secret override onto a personal topic. Workflows must hardcode `cwally-port` (or `cwally-ops`).
- Title prefix required: `[STEEL]` `[SQUARE]` `[ALUX]` `[FLOW]` `[HCE]` `[SILICON2]` `[LG]` `[IREN]` `[COVER0198]` `[0220WL]` `[USTSY]` `[SGYN]` `[YENCARRY]` `[CCREV]` `[KSHIP]` `[PORT]` `[KRWFX]` `[SEC]` `[GVAL]` `[455850]` `[0005D0]` `[0192T0]` `[091160]` `[278240]` `[2UB]` `[377990]` `[489000]` `[ADDSTOP]` `[AEO]` `[BRK10]` `[BTC90]` `[BTCOCT]` `[CL]` `[CPI]` `[DHR]` `[DRAM27]` `[GENPACT]` `[GOOGL]` `[ISRG]` `[KOSBRK]` `[NKE]` `[NOV3]` `[OKLO]` `[PATH]` `[PCOR]` `[PFFD]` `[PLTR]` `[PROTEC]` `[SEMREV]` `[SHINWON]` `[SKC]` `[SMH]` `[ZS]` `[0090B0]` `[BTCBOX]` `[XRPBOX]` `[DKQ3]` `[KRBANK]` `[SNDK]` `[SPCX]` `[TSMC]` `[232140]` `[PORTLIGHT]` `[BURRY]` `[ETH]` `[RATES]` `[FOMC]` `[LGIT]` `[NVDA]` `[OPS]` `[EWS]` `[PYPL]` `[USD240]` `[SDI]` `[CEG]` `[0144M0]` `[487230]` `[HBMCHK]` `[HBMASP]` `[ASML]` `[SAMG]` `[VTRSPG]` `[280930]` `[SPG]` `[SIVR]` `[051905]` `[BANKRATE]` `[ISAREB]` `[SKINC]` `[FXBOX]` `[RBLX]`.
- Adding that alert's own title prefix to the Title prefix required line, in the same commit as the alert, needs no second approval (approved 2026-10-06). This does not waive a new topic, a new repeating cron, a new workflow file, a live test send other than the install-confirmation send below, or any other AGENTS.md edit.
- Hourly cron is forbidden. No new workflow file.
- Exception (approved in chat 2026-10-06): `.github/workflows/cronjob-register.yml` (`workflow_dispatch` only, no schedule) registers one `oneshot_kst` time on cron-job.org via `scripts/cronjob_register.py`. It does not allow any other new workflow file.
- Recurring alerts attach to an existing repeating slot first (see "Repeating slots" below): add the script to that slot's job in the existing workflow and set `schedule_kst` to the slot time. That is a normal alert add and needs no extra approval.
- A new repeating cron (a time that is not an existing slot) needs chat approval. After approval, record it in `configs/<name>.json` as `"cron_approved": "YYYY-MM-DD"` plus `"cron_utc": "<cron string>"`, and add the slot to the table below in the same commit. A repeating cron without `cron_approved` is a violation.
- One-shots stay on the existing dispatcher only. Source of truth is `oneshot_kst` in `configs/<name>.json`. Map that entry to one job id in the existing `Decide jobs` step (that GitHub cron is the late fallback). Do not create `.github/workflows/<alert>.yml`. A mode argument other than the default (`auto`; `oklo` uses `event`, `hce` uses `daily`) goes in `oneshot_args` as `{"YYYY-MM-DD HH:MM": "<mode>"}`.
- Never let an unmatched schedule fall through to `morning`. If `oneshot_kst` is not mapped, stop and report. Do not run the morning bundle.
- GitHub cron has no year. A one-shot script MUST year-guard and stay silent in later years. Convert KST to UTC (`KST = UTC+9`) only when registering the mapped slot.
- Put the chosen KST time in `configs/<name>.json` as `schedule_kst`. One-shot times go in `oneshot_kst`.
- New alert files are only `configs/<name>.json` + `scripts/<name>_monitor.py` + `state/<name>.json`.
- Alert end line (approved in chat 2026-10-07): every alert config has `ends_kst` (`"YYYY-MM-DD HH:MM"` KST = the last time the alert can still send, or `null` = no end date) and `ends_note` (short Korean reason or what ends when). `ntfy_util.publish()` appends `꺼지는 때: <ends_kst> KST (<ends_note>)` as the last body line, finding the config from the running script name `<name>_monitor.py`. A custom publisher must pass its message through `ntfy_util.append_ends()`. When a window is extended or one-shots are added, update `ends_kst` in the same commit. New alerts without `ends_kst` are incomplete.
- Install-confirmation test send (approved in chat 2026-10-07): when adding a new alert, one test send per alert is allowed without extra approval, only to `cwally-port`, titled `[NAME] 설치 확인 | 매매 신호 아님`, body in the six-item order, sent through `ntfy_util.publish()` so the end line is appended. It must not touch `state/slots/*.json`, one-shot records or `state/<name>.json`. Report the ntfy id and KST time. Manual workflow runs (job=all, a single job, force), other topics, repeated sends and titles that read like a trade signal still need chat approval.
- Creating an alert includes activating it in the same turn. After `oneshot_kst` exists, register it on the existing dispatcher in `.github/workflows/port-alerts.yml`: add only those cron slots, one `Decide jobs` branch for that job id, and one run step for that script. Do not ask for a second approval. Do not leave the alert unmapped. Then register every new `oneshot_kst` time on the external cron (cron-job.org) as a one-off `port-alerts.yml` dispatch with `{"job":"oneshot"}`; list the times with `python scripts/oneshot_pick.py plan --from <YYYY-MM-DD> --to <YYYY-MM-DD>`. That registration is part of the alert add and needs no extra approval.
- That mapping is the only workflow edit included in a normal alert add. Do not add a YAML job, a new workflow file, or a repeating cron.
- Extra chat approval is required for a new repeating cron time, a new workflow file, a topic change, a new repo, a live ntfy test send other than the install-confirmation send above, an AGENTS.md edit other than the same-commit title-prefix add above, or any workflow edit outside that one-shot mapping.
- Do not duplicate Telegram Alpha Catch signals here.
- Old single-purpose repos must have workflows disabled, not deleted immediately.

## Repeating slots

Attach recurring alerts here before asking for a new time. Times are KST; the cron column is UTC.

| Slot | KST | cron (UTC) | Workflow / job |
|---|---|---|---|
| morning | weekdays 09:00 | `0 0 * * 1-5` | `port-alerts.yml` / `morning` |
| afternoon | weekdays 15:20 | `20 6 * * 1-5` | `port-alerts.yml` / `afternoon` |
| flow | weekdays 15:40 | `40 6 * * 1-5` | `flow-alerts.yml` / `flow` |
| 0090b0 | weekdays 15:40 | `40 6 * * 1-5` | `port-alerts.yml` / `0090b0` (approved 2026-10-06) |
| portlight | Sunday 21:00 | `0 12 * * 0` | `port-alerts.yml` / `portlight` |

## On-time external trigger

GitHub schedule events arrive hours late. On-time runs come from an external cron calling `workflow_dispatch` for the morning / afternoon / flow / 0090b0 slots; see `docs/ontime-dispatch.md`. `scripts/slot_guard.py` + `state/slots/<slot>.json` let each slot run once per KST day, so a late GitHub cron or a duplicate dispatch skips. Keep the GitHub crons as the fallback.
One-shots are on time the same way: the external cron dispatches `job=oneshot` at each `oneshot_kst` time, and `scripts/oneshot_pick.py` runs every one-shot whose time is within the last 60 minutes and not yet recorded in `state/slots/oneshot.json` (key `<name>@<YYYY-MM-DD HH:MM>`). A late GitHub one-shot cron checks that file and skips an occurrence that already ran. An occurrence is recorded *before* its script runs, so a failed script is not re-sent by the fallback; it raises the ops notice instead, and a manual retry is a dispatch of that alert's job id (if listed) or a `force` run of its script. One-shot times at 09:00 / 15:20 for alerts already in that slot bundle are left to the slot. KRX holidays live in `scripts/kr_calendar.py` (update every December).

## ntfy copy style

Write every alert so a tired reader knows what to do in 5 seconds. Korean, short sentences, no unexplained jargon.

Required body order:
1. 이게 뭐 알림
2. 지금 결과/상태
3. 지금 할 일
4. 나중에
5. 익절/줄일 때
6. 하지 말 것
7. 꺼지는 때 (자동: `ntfy_util`이 `ends_kst`/`ends_note`로 맨 끝에 붙임. 본문에 직접 쓰지 않는다)

Title format: `[NAME] 한 줄 행동`
Example: `[HCE] 아직 결과 없음 | 추가매수 보류`

Template: `configs/alert_copy.json`. Helper: `scripts/ntfy_util.py`.
