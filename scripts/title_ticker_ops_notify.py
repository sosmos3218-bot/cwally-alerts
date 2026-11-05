#!/usr/bin/env python3
"""CI helper for .github/workflows/title-ticker-check.yml (pt112u). Not an alert.

Reads the captured title_ticker_check.py output and, unless DRY=1 or ALREADY=1,
publishes one p3 note to cwally-ops. Only STATIC and legacy FAIL lines count;
report-only lines are ignored. Writes sent=1 to $GITHUB_OUTPUT only after a real send.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def build(out: str, sha: str, run_url: str) -> tuple[str, str, int]:
    lines = out.splitlines()
    static = [ln[len("STATIC "):] for ln in lines if ln.startswith("STATIC ")]
    legacy = []
    if lines and lines[0].startswith("title ticker check FAIL"):
        for ln in lines[1:]:
            if ln.startswith("title ticker audit"):
                break
            legacy.append(ln)
    rows = legacy + static
    n = len(rows)
    title = f"[OPS] SPY 제목 티커 검사 위반 {n}건"
    body = "\n".join(rows[:5] + ([f"외 {n - 5}건"] if n > 5 else []))
    body += f"\n커밋 {sha[:7]}\n{run_url}"
    return title, body, n


def main() -> int:
    out = Path(sys.argv[1]).read_text(encoding="utf-8")
    title, body, n = build(out, os.environ.get("SHA", ""), os.environ.get("RUN_URL", ""))
    print(f"would notify cwally-ops p3: {title}")
    print(body)
    sent = "0"
    if n == 0:
        print("no STATIC or legacy FAIL lines: not sending")
    elif os.environ.get("DRY") == "1":
        print("DRY: not sent, cache not written")
    elif os.environ.get("ALREADY") == "1":
        print(f"already notified for {os.environ.get('DAY')}: not sent")
    else:
        import ntfy_util
        try:
            ntfy_util.publish("cwally-ops", title, body, 3, ["warning"],
                              os.environ.get("NTFY_SERVER") or "https://ntfy.sh",
                              append_end=False, dedup_key=f"title-ticker-ops-{os.environ.get('DAY')}")
            sent = "1" if ntfy_util.LAST_PUBLISH.get("sent") else "0"
        except Exception as e:  # send failure must not hide the red job or mark the day as sent
            print(f"::warning::ops notify failed: {e}")
        print(f"publish result: {ntfy_util.LAST_PUBLISH}")
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a", encoding="utf-8") as f:
            f.write(f"sent={sent}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
