#!/usr/bin/env python3
"""Emit DUP_GUARD_SINCE=<unix> for GitHub Actions $GITHUB_ENV (pt15u).

usage:
  python scripts/dup_guard_since.py --job morning
  python scripts/dup_guard_since.py --job oneshot --schedule "10 7 28 10 *"
  python scripts/dup_guard_since.py --job tsla

Prints one line: DUP_GUARD_SINCE=<unix>
Unknown job / missing schedule -> still prints midnight KST (same as unset fallback).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import dup_guard_since_unix  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", default="")
    ap.add_argument("--schedule", default="")
    args = ap.parse_args()
    since = dup_guard_since_unix(args.job or None, args.schedule or None)
    print(f"DUP_GUARD_SINCE={since}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
