# cwally-alerts rules (do not violate)

This repo is the only place for GitHub Actions + ntfy.

- Never create a new repository for an ntfy alert.
- Topics: `cwally-port` for holdings, trades, AND market flow. `cwally-ops` only for Kikii/server.
- Do not use `cwally-flow` or ad-hoc topics such as `clarity-52505089`.
- Do not let `NTFY_TOPIC` env/secret override onto a personal topic. Workflows must hardcode `cwally-port` (or `cwally-ops`).
- Title prefix required: `[STEEL]` `[SQUARE]` `[ALUX]` `[FLOW]` `[CLARITY]` `[HCE]` `[SILICON2]` `[LG]` `[IREN]` `[COVER0198]` `[0220WL]` `[USTSY]` `[SGYN]` `[YENCARRY]` `[CCREV]` `[KSHIP]` `[PORT]` `[0051A0]` `[KRWFX]` `[SEC]`.
- Default attach: reuse weekday `09:00` KST (UTC 00:00) or `15:20` KST (UTC 06:20). Do not add a new weekday/repeating cron unless the user asks for a new recurring schedule. No hourly cron.
- After-close / dated prints (KR earnings, FOMC, BOJ) MAY add a `m h D M *` oneshot cron without extra chat approval. Convert KST→UTC (`KST = UTC+9`). GitHub cron has no year, so the script MUST year-guard and stay silent in later years.
- Every new `m h D M *` cron MUST be mapped in `Decide jobs` to a single job id. Never let an unmatched schedule fall through to `morning`.
- Put the chosen KST time in `configs/<name>.json` as `schedule_kst`. One-shot extras go in `oneshot_kst`.
- New alert = `configs/<name>.json` + `scripts/<name>_monitor.py` + `state/<name>.json` only. Workflow edits are allowed only to register that oneshot cron and its job map.
- Do not duplicate Telegram Alpha Catch signals here.
- Old single-purpose repos must have workflows disabled, not deleted immediately.

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
