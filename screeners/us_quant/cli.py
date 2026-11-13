"""CLI for the US screen.

  python -m screeners.us_quant selftest
  python -m screeners.us_quant run --as-of 2026-11-12 --run-date 2026-11-13
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from screeners.us_quant.pipeline import default_as_of, default_run_date, load_config, run

ROOT = Path(__file__).resolve().parents[2]


def _date(value: str):
    return datetime.strptime(value, "%Y-%m-%d").date()


def selftest() -> int:
    import pytest

    return int(pytest.main(["-q", str(Path(__file__).resolve().parent / "tests")]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="screeners.us_quant")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest")
    command = sub.add_parser("run")
    command.add_argument("--as-of", help="Trade date YYYY-MM-DD")
    command.add_argument("--run-date", help="Label date YYYY-MM-DD. Defaults to today in Asia/Seoul")
    command.add_argument("--config")
    command.add_argument("--out")
    command.add_argument("--cache")
    args = parser.parse_args(argv)
    if args.cmd == "selftest":
        return selftest()
    config = load_config(Path(args.config) if args.config else None)
    as_of = _date(args.as_of) if args.as_of else default_as_of()
    run_date = _date(args.run_date) if args.run_date else default_run_date()
    out = Path(args.out) if args.out else ROOT / "screeners" / "output" / "us"
    cache = Path(args.cache) if args.cache else ROOT / "screeners" / "cache" / "us_quant"
    run(as_of, run_date, config, out, cache)
    return 0
