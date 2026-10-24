#!/usr/bin/env python3
"""[000660] SK하이닉스 close-based plan alert on the k203780 19:00 KST slot.

Same engine as scripts/005930_monitor.py (loaded below), own configs/000660.json and state/000660.json.
The US 10Y (^TNX >= 5.30%) shared warning is sent only by 005930, never here.
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
    raise SystemExit(engine.run("000660", sys.argv))
