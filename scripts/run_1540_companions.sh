#!/usr/bin/env bash
# Run flow + trend inside the port-alerts 0090b0 job so 15:40 is one billed runner.
# Slot guards skip a companion that already ran on time (cron-job.org or a prior dispatch).
set -u
failed=0
IS_DRY=0
if [ "${GITHUB_EVENT_NAME:-}" = "workflow_dispatch" ] && [ "${ONEOFF_MODE:-dry}" != "auto" ]; then
  IS_DRY=1
fi

run_one() {
  local label="$1"
  shift
  echo "::group::$label"
  if "$@"; then
    echo "$label ok"
  else
    echo "::error::$label failed (exit $?)"
    failed=1
  fi
  echo "::endgroup::"
}

git fetch -q origin main || true

# --- flow ---
if [ "$IS_DRY" = 1 ]; then
  echo "dry dispatch: skip flow slot"
else
  git show origin/main:state/slots/flow.json > /tmp/slot-flow.json 2>/dev/null || echo '{}' > /tmp/slot-flow.json
  if SLOT_FILE=/tmp/slot-flow.json python scripts/slot_guard.py check flow; then
    python scripts/dup_guard_since.py --job flow >> "$GITHUB_ENV" || true
    python scripts/slot_guard.py mark flow
    pip install -q -r .github/workflows/flow-requirements.txt
    run_one flow_monitor python scripts/flow_monitor.py
    run_one secu_flow python scripts/secu_flow_monitor.py
    run_one kospi_begin python scripts/kospi_monitor.py begin
    export KOSPI_LADDER=1
    run_one kosbrk python scripts/kosbrk_monitor.py
    run_one kosdown python scripts/kosdown_monitor.py
    run_one kosup python scripts/kosup_monitor.py
    run_one kos5500 python scripts/kos5500_monitor.py afterclose
    unset KOSPI_LADDER
    run_one kospi python scripts/kospi_monitor.py send
    run_one frde python scripts/frde_monitor.py auto
    run_one k200 python scripts/k200_monitor.py
    run_one k278240 python scripts/k278240_monitor.py
    run_one 377990 python scripts/377990_monitor.py
    run_one ethhl python scripts/ethhl_monitor.py
    run_one kica python scripts/kica_monitor.py
    run_one dssotp python scripts/dssotp_monitor.py
    run_one gsai python scripts/gsai_monitor.py
  else
    echo "flow already marked today; skip"
  fi
fi

# --- trend ---
if [ "$IS_DRY" = 1 ]; then
  echo "dry dispatch: skip trend slot"
else
  git show origin/main:state/slots/trend.json > /tmp/slot-trend.json 2>/dev/null || echo '{}' > /tmp/slot-trend.json
  TREND_RUN=1
  if ! SLOT_FILE=/tmp/slot-trend.json python scripts/slot_guard.py check trend; then
    TREND_RUN=0
  fi
  day=$(TZ=Asia/Seoul date +%F)
  if git cat-file -e "origin/main:trend_monitor/output/${day}.json" 2>/dev/null; then
    echo "trend output ${day}.json already on main; skip"
    TREND_RUN=0
  fi
  if [ "$TREND_RUN" = 1 ]; then
    python scripts/dup_guard_since.py --job trend >> "$GITHUB_ENV" || true
    python scripts/slot_guard.py mark trend
    python -m pip install -q -r trend_monitor/requirements.txt
    python -m trend_monitor || { echo "::error::trend_monitor failed"; failed=1; }
  fi
fi

if [ "$failed" -ne 0 ]; then
  echo "one or more 15:40 companion scripts failed"
  exit 1
fi
