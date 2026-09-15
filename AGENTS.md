# cwally-alerts rules (do not violate)

This repo is the only place for GitHub Actions + ntfy.

- Never create a new repository for an ntfy alert.
- Topics only: `cwally-port` (holdings/trades), `cwally-flow` (market flow), `cwally-ops` (Kikii/server).
- Title prefix required: `[STEEL]` `[SQUARE]` `[ALUX]` `[FLOW]` `[CLARITY]`.
- Cron slots are NOT fixed. Pick a KST time that matches the request, then convert to UTC (`KST = UTC+9`) in the workflow cron.
- Put the chosen KST time in `configs/<name>.json` as `schedule_kst`.
- New alert = `configs/<name>.json` + `scripts/<name>_monitor.py` + `state/<name>.json` only.
- Do not duplicate Telegram Alpha Catch signals here.
- Old single-purpose repos must have workflows disabled, not deleted immediately.
