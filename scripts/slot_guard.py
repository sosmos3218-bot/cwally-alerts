#!/usr/bin/env python3
"""Run each repeating slot at most once per KST day.

GitHub schedule events arrive hours late (09:00 slot ~11:40-13:30, 15:20 slot ~20:40-23:00).
An on-time external trigger (workflow_dispatch, see docs/ontime-dispatch.md) runs the slot and
marks it; the late GitHub cron, or a duplicate dispatch, then sees the mark and skips.
Workflows check the mark on origin/main (git show) because the run's own checkout can predate it,
and each workflow uses a concurrency group, so a second run starts only after the first has pushed.

  python scripts/slot_guard.py check <slot>   # exit 0 = run, exit 1 = already ran today
  python scripts/slot_guard.py mark <slot>
State: state/slots/<slot>.json  {"date": "2026-10-06"}   (one file per slot -> no rebase conflicts
between port-alerts and flow-alerts pushes)
SLOT_FILE=<path> overrides the file read by `check`.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
DIR = Path(__file__).resolve().parents[1] / "state" / "slots"


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] not in {"check", "mark"} or not re.fullmatch(r"[A-Za-z0-9_\-]+", sys.argv[2]):
        print(__doc__)
        return 2
    cmd, slot = sys.argv[1], sys.argv[2]
    today = datetime.now(KST).strftime("%Y-%m-%d")
    path = DIR / f"{slot}.json"
    if cmd == "check":
        src = Path(os.environ["SLOT_FILE"]) if os.environ.get("SLOT_FILE") else path
        try:
            last = json.loads(src.read_text(encoding="utf-8")).get("date")
        except (FileNotFoundError, json.JSONDecodeError, AttributeError):
            last = None
        if last == today:
            print(f"slot {slot} already ran {today}; skip")
            return 1
        print(f"slot {slot} not yet run {today} (last {last})")
        return 0
    DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"date": today}) + "\n", encoding="utf-8")
    print(f"slot {slot} marked {today}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
