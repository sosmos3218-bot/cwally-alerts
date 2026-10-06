# cwally-alerts rules (do not violate)

This repo is the only place for GitHub Actions + ntfy.

- Never create a new repository for an ntfy alert.
- Topics: `cwally-port` for holdings, trades, AND market flow. `cwally-ops` only for Kikii/server.
- Do not use `cwally-flow` or ad-hoc topics such as `clarity-52505089`.
- Do not let `NTFY_TOPIC` env/secret override onto a personal topic. Workflows must hardcode `cwally-port` (or `cwally-ops`).
- Title prefix required: `[STEEL]` `[SQUARE]` `[ALUX]` `[FLOW]` `[CLARITY]` `[HCE]` `[SILICON2]` `[LG]` `[IREN]` `[COVER0198]` `[0220WL]` `[USTSY]` `[SGYN]` `[YENCARRY]` `[CCREV]` `[KSHIP]` `[PORT]` `[KRWFX]` `[SEC]` `[GVAL]` `[455850]` `[0005D0]` `[0192T0]` `[091160]` `[278240]` `[2UB]` `[377990]` `[489000]` `[ADDSTOP]` `[AEO]` `[BRK10]` `[BTC90]` `[BTCOCT]` `[CL]` `[CPI]` `[DHR]` `[DRAM27]` `[GENPACT]` `[GOOGL]` `[ISRG]` `[KOSBRK]` `[NKE]` `[NOV3]` `[OKLO]` `[PATH]` `[PCOR]` `[PFFD]` `[PLTR]` `[PROTEC]` `[SEMREV]` `[SHINWON]` `[SKC]` `[SMH]` `[ZS]`.
- Hourly cron is forbidden. No new workflow file.
- Recurring alerts attach to an existing repeating slot first (see "Repeating slots" below): add the script to that slot's job in the existing workflow and set `schedule_kst` to the slot time. That is a normal alert add and needs no extra approval.
- A new repeating cron (a time that is not an existing slot) needs chat approval. After approval, record it in `configs/<name>.json` as `"cron_approved": "YYYY-MM-DD"` plus `"cron_utc": "<cron string>"`, and add the slot to the table below in the same commit. A repeating cron without `cron_approved` is a violation.
- One-shots stay on the existing dispatcher only. Source of truth is `oneshot_kst` in `configs/<name>.json`. Map that entry to one job id in the existing `Decide jobs` step. Do not create `.github/workflows/<alert>.yml`.
- Never let an unmatched schedule fall through to `morning`. If `oneshot_kst` is not mapped, stop and report. Do not run the morning bundle.
- GitHub cron has no year. A one-shot script MUST year-guard and stay silent in later years. Convert KST to UTC (`KST = UTC+9`) only when registering the mapped slot.
- Put the chosen KST time in `configs/<name>.json` as `schedule_kst`. One-shot times go in `oneshot_kst`.
- New alert files are only `configs/<name>.json` + `scripts/<name>_monitor.py` + `state/<name>.json`.
- Creating an alert includes activating it in the same turn. After `oneshot_kst` exists, register it on the existing dispatcher in `.github/workflows/port-alerts.yml`: add only those cron slots, one `Decide jobs` branch for that job id, and one run step for that script. Do not ask for a second approval. Do not leave the alert unmapped.
- That mapping is the only workflow edit included in a normal alert add. Do not add a YAML job, a new workflow file, or a repeating cron.
- Extra chat approval is required for a new repeating cron time, a new workflow file, a topic change, a new repo, a live ntfy test send, an AGENTS.md edit, or any workflow edit outside that one-shot mapping.
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

## ntfy copy style

Write every alert so a tired reader knows what to do in 5 seconds. Korean, short sentences, no unexplained jargon.

Required body order:
1. 이게 뭐 알림
2. 지금 결과/상태
3. 지금 할 일
4. 나중에
5. 익절/줄일 때
6. 하지 말 것

Title format: `[NAME] 한 줄 행동`
Example: `[CLARITY] 아직 결과 없음 | 추가매수 보류`

Template: `configs/alert_copy.json`. Helper: `scripts/ntfy_util.py`.
