# cwally-alerts rules (do not violate)

This repo is the only place for GitHub Actions + ntfy.

- Never create a new repository for an ntfy alert.
- Topics only: `cwally-port` (holdings/trades), `cwally-flow` (market flow), `cwally-ops` (Kikii/server).
- Title prefix required: `[STEEL]` `[SQUARE]` `[ALUX]` `[FLOW]`.
- Slots only: 09:00 KST port morning, 15:20 KST port afternoon, 15:40 KST flow. Do not add new crons.
- New alert = `configs/<name>.json` + `scripts/<name>_monitor.py` + `state/<name>.json` only.
- Do not duplicate Telegram Alpha Catch signals here.
- Old single-purpose repos must have workflows disabled, not deleted immediately.
