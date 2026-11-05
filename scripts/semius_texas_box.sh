#!/usr/bin/env bash
# [SEMIUS] Texas part (TLO 90R data center moratorium bills + governor executive orders) on the Grok box.
# GitHub-hosted runners get 'connection refused' from capitol.texas.gov / gov.texas.gov (2026-11-05), so this
# runs from a box routine instead (approved 2026-11-05, pt59u). The script gates by date itself
# (weekly = first run of each ISO week to 2027-01-11, weekdays 2027-01-12 ~ 06-02, ends 2027-06-05), so the
# routine can simply call this every weekday morning. ntfy.sh is public: no secret needed.
# State: state/semius_tx.json only (the GitHub job writes state/semius.json), committed as sosmos3218-bot.
# Usage: bash scripts/semius_texas_box.sh            (live: may send p3/p4 or one ops notice a day)
#        bash scripts/semius_texas_box.sh --dry      (texas-dry: print only, no ntfy, no state, no git)
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
MODE=texas
[ "${1:-}" = "--dry" ] && MODE=texas-dry
G=(git -c user.name=sosmos3218-bot -c user.email=bot@sosmos3218.github)
if ! "${G[@]}" pull --rebase --autostash -q origin main; then
  "${G[@]}" rebase --abort >/dev/null 2>&1
  echo "warn: git pull --rebase failed; running on the local checkout"
fi
LOG=$(mktemp)
python3 scripts/semius_monitor.py "$MODE" 2>&1 | tee "$LOG"
rc=${PIPESTATUS[0]}
# safety net: the script commits/pushes its own state; push anything it left behind (only that file)
if [ "$MODE" = "texas" ] && [ -n "$(git status --porcelain -- state/semius_tx.json)" ]; then
  "${G[@]}" add -- state/semius_tx.json && "${G[@]}" commit -q -m "semius: texas state" -- state/semius_tx.json
fi
if [ "$MODE" = "texas" ] && [ -n "$(git log origin/main..HEAD --oneline -- state/semius_tx.json 2>/dev/null)" ]; then
  for i in 1 2 3; do
    "${G[@]}" pull --rebase --autostash -q origin main && "${G[@]}" push -q origin HEAD:main && break
    "${G[@]}" rebase --abort >/dev/null 2>&1; sleep $((5 * i))
  done
fi
RES=$(grep '^SEMIUS_RESULT:' "$LOG" | tail -1 | sed 's/^SEMIUS_RESULT: *//')
rm -f "$LOG"
case "$RES" in
  *"sent "*) KIND=sent ;;
  *" ops"*) KIND=ops ;;
  *" none"*) KIND=none ;;
  *skip*) KIND=none ;;
  *) KIND=error ;;
esac
[ "$rc" != "0" ] && KIND=error
echo "semius texas: $KIND | ${RES:-no result line} | rc=$rc"
exit "$rc"
