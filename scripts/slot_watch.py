#!/usr/bin/env python3
"""Late daily safety net: if a cron-job.org on-time slot did not mark today, dispatch it.

GitHub schedule backups for recurring slots were removed (they still billed 1 minute
when they no-oped, and often arrived hours late). This script is the sparse fallback.
It does not replay TSLA US-hours checks.

Exit 0 if every due slot is already marked, or missed slots were dispatched.
Exit 1 if a dispatch command failed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kr_calendar
from slot_guard import DIR as SLOT_DIR

KST = timezone(timedelta(hours=9))
REPO = os.environ.get("GITHUB_REPOSITORY", "sosmos3218-bot/cwally-alerts")

# slot -> (workflow file, dispatch fields). 0090b0 also runs flow+trend after the 15:40 merge.
PORT = ".github/workflows/port-alerts.yml"
FLOW = ".github/workflows/flow-alerts.yml"
TREND = ".github/workflows/trend-monitor.yml"
PORT_AUTO = {"job": "", "oneoff_mode": "auto"}


def slot_marked(slot: str, today: str) -> bool:
    path = SLOT_DIR / f"{slot}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("date") == today
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        return False


def sync_from_origin(slot: str) -> None:
    SLOT_DIR.mkdir(parents=True, exist_ok=True)
    dest = SLOT_DIR / f"{slot}.json"
    r = subprocess.run(
        ["git", "show", f"origin/main:state/slots/{slot}.json"],
        capture_output=True,
        text=True,
    )
    if r.returncode == 0 and r.stdout:
        dest.write_text(r.stdout, encoding="utf-8")


def dispatch(workflow: str, fields: dict[str, str]) -> None:
    cmd = ["gh", "workflow", "run", workflow, "-R", REPO]
    for k, v in fields.items():
        if v != "":
            cmd.extend(["-f", f"{k}={v}"])
    print("dispatch:", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def main() -> int:
    now = datetime.now(KST)
    today = kr_calendar.session_day(now).strftime("%Y-%m-%d")
    cal = now.date()
    print(f"slot-watch {now.isoformat(timespec='seconds')} session_day={today} calendar={cal}")
    subprocess.run(["git", "fetch", "-q", "origin", "main"], check=False)

    due: list[tuple[str, str, dict[str, str]]] = []
    # daily
    due.append(("tslaspcx", PORT, {"job": "tslaspcx", "oneoff_mode": "auto"}))
    if kr_calendar.is_trading_day(cal):
        due.append(("morning", PORT, {"job": "morning", "oneoff_mode": "auto"}))
        due.append(("afternoon", PORT, {"job": "afternoon", "oneoff_mode": "auto"}))
        due.append(("k203780", PORT, {"job": "k203780", "oneoff_mode": "auto"}))
        due.append(("0090b0", PORT, {"job": "0090b0", "oneoff_mode": "auto"}))
        due.append(("flow", FLOW, {}))
        due.append(("trend", TREND, {}))
    if cal.weekday() == 6:
        due.append(("portlight", PORT, {"job": "portlight", "oneoff_mode": "auto"}))

    missed: list[tuple[str, str, dict[str, str]]] = []
    for slot, wf, fields in due:
        sync_from_origin(slot)
        if slot_marked(slot, today):
            print(f"ok: {slot} marked {today}")
        else:
            print(f"MISS: {slot} not marked {today}")
            missed.append((slot, wf, fields))

    # After the 15:40 merge, a 0090b0 dispatch also runs flow+trend.
    # If 0090b0 itself is missing, don't also fire the standalone workflows.
    names = {m[0] for m in missed}
    if "0090b0" in names:
        missed = [m for m in missed if m[0] not in {"flow", "trend"}]

    if not missed:
        print("all due slots marked; no dispatch")
        return 0

    dry = os.environ.get("SLOT_WATCH_DRY", "").strip() in {"1", "true", "True"}
    if dry:
        print("dry_run: would dispatch " + ", ".join(m[0] for m in missed) + " (no dispatch)")
        return 0

    failed = 0
    for slot, wf, fields in missed:
        try:
            dispatch(wf, fields)
            print(f"dispatched {slot}")
        except subprocess.CalledProcessError as e:
            print(f"ERROR dispatch {slot}: {e}", file=sys.stderr)
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
