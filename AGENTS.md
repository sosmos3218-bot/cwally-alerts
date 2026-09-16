# cwally-alerts rules (do not violate)

This repo is the only place for GitHub Actions + ntfy.

- Never create a new repository for an ntfy alert.
- Topics only: `cwally-port` (holdings/trades), `cwally-flow` (market flow), `cwally-ops` (Kikii/server).
- Holdings/trades always go to `cwally-port`. Never use ad-hoc topics such as `clarity-52505089`.
- Do not let `NTFY_TOPIC` env/secret override holdings onto a personal topic. Workflows must hardcode the official topic.
- Title prefix required: `[STEEL]` `[SQUARE]` `[ALUX]` `[FLOW]` `[CLARITY]` `[HCE]` `[SILICON2]` `[LG]` `[IREN]` `[COVER0198]` `[0220WL]` `[USTSY]`.
- Cron slots are NOT fixed. Pick a KST time that matches the request, then convert to UTC (`KST = UTC+9`) in the workflow cron.
- Put the chosen KST time in `configs/<name>.json` as `schedule_kst`.
- New alert = `configs/<name>.json` + `scripts/<name>_monitor.py` + `state/<name>.json` only.
- Do not duplicate Telegram Alpha Catch signals here.
- Old single-purpose repos must have workflows disabled, not deleted immediately.

## ntfy copy style

Write every alert so a tired reader knows what to do in 5 seconds. Korean, short sentences, no unexplained jargon.

Required body order:
1. 이게 뭔 알림
2. 지금 결과/상태
3. 지금 할 일
4. 나중에
5. 익절/줄일 때
6. 하지 말 것

Title format: `[NAME] 한 줄 행동`
Example: `[CLARITY] 아직 결과 없음 | 추가매수 보류`

Template: `configs/alert_copy.json`. Helper: `scripts/ntfy_util.py`.
