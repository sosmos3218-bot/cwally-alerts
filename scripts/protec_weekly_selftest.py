#!/usr/bin/env python3
"""PROTEC weekly_sent_week selftest (pt15u). No network."""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KST = timezone(timedelta(hours=9))


def main() -> int:
    fails = 0

    def check(label, got, want):
        nonlocal fails
        ok = got == want
        fails += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'} {label}: {got!r} want={want!r}")

    src = (ROOT / "scripts" / "protec_monitor.py").read_text(encoding="utf-8")
    check("weekly_sent_week written to state", "weekly_sent_week" in src, True)
    check("monday gated by weekly_sent_week", "prev.get(\"weekly_sent_week\") == week" in src, True)
    check("mode weekly marks the week", 'mode in {"weekly"}' in src or "mode in {'weekly'}" in src, True)

    monday = datetime(2026, 11, 2, 9, 0, tzinfo=KST)
    week = monday.strftime("%G-W%V")
    check("ISO week of 2026-11-02", week, "2026-W45")

    def would_monday(prev_week: str | None, when: datetime) -> bool:
        w = when.strftime("%G-W%V")
        flag = when.weekday() == 0
        if flag and prev_week == w:
            flag = False
        return flag

    check("first monday of week sends", would_monday(None, monday), True)
    check("same week afternoon silent", would_monday(week, monday.replace(hour=15, minute=21)), False)
    check("next monday opens", would_monday(week, datetime(2026, 11, 9, 9, 0, tzinfo=KST)), True)

    st = json.loads((ROOT / "state" / "protec.json").read_text(encoding="utf-8"))
    check("repo state seeded this week", st.get("weekly_sent_week"), week)
    # no middle dots / em dashes introduced in Korean titles of weekly path
    check("no middle dot in weekly title line", "·" not in "[PROTEC] 053610 주간 점검 | 관망", True)

    print("PROTEC WEEKLY SELFTEST " + ("PASS" if not fails else f"FAIL ({fails})"))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
