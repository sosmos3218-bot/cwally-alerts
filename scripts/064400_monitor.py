#!/usr/bin/env python3
"""[064400] LG씨엔에스 close-based plan alert on the k203780 19:00 KST slot.

Same engine as scripts/005930_monitor.py (loaded below), own configs/064400.json and state/064400.json.
No US 10Y warning for this ticker (configs/064400.json has no us10y key). Approved in chat 2026-10-26.
Modes: auto | force | dry | selftest | install | seed (see 005930_monitor.py).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location("krplan_005930", Path(__file__).resolve().parent / "005930_monitor.py")
engine = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(engine)

if __name__ == "__main__":
    raise SystemExit(engine.run("064400", sys.argv))
