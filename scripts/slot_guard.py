#!/usr/bin/env python3
"""Run each repeating slot once per KST day.

GitHub schedule events arrive hours late (09:00 slot ~11:40-13:30, 15:20 slot ~20:40-23:00).
The external trigger (Kikii cron -> `gh workflow run ... -f job=morning`) runs on time and
marks the slot; the late GitHub cron then sees the mark and skips.

  python scripts/slot_guard.py check <slot>   # exit 0 = run, exit 1 = already ran today
  python scripts/slot_guard.py mark <slot>
State: state/slot_runs.json  {"morning": "2026-10-06", ...}
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
PATH = Path(__file__).resolve().parents[1] / "state" / "slot_runs.json"


def load() -> dict:
    src = Path(os.environ["SLOT_RUNS"]) if os.environ.get("SLOT_RUNS") else PATH
    try:
        return json.loads(src.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] not in {"check", "mark"}:
        print(__doc__)
        return 2
    cmd, slot = sys.argv[1], sys.argv[2]
    today = datetime.now(KST).strftime("%Y-%m-%d")
    data = load()
    if cmd == "check":
        if data.get(slot) == today:
            print(f"slot {slot} already ran {today}; skip")
            return 1
        print(f"slot {slot} not yet run {today}")
        return 0
    data[slot] = today
    PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"slot {slot} marked {today}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
