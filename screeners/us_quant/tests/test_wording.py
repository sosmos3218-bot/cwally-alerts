"""Screen copy stays descriptive."""

from __future__ import annotations

import re
from pathlib import Path

BANNED = re.compile(r"\b(buy|sell|target|recommend|recommendation)\b", re.IGNORECASE)


def test_screen_package_has_no_trade_language():
    root = Path(__file__).resolve().parents[1]
    repo = root.parents[1]
    paths = list(root.rglob("*"))
    paths.append(repo / ".github" / "workflows" / "us-quant-screen.yml")
    hits = []
    for path in paths:
        if path.suffix not in {".py", ".md", ".json", ".yml"} or not path.exists():
            continue
        if path.name == "test_wording.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if BANNED.search(line):
                hits.append(f"{path}:{lineno}:{line.strip()}")
    assert hits == []
