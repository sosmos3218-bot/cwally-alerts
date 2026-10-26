"""publish() for the [KOSPI] ladder components (kosdown, kosup, kosbrk, kos5500), 2026-10-26.

With KOSPI_LADDER=1 (set only in the flow-alerts 15:40 step) a signal send is appended to the spool file and reported
as sent, so the component saves its fired state exactly as before; `kospi_monitor.py send` then publishes one combined
[KOSPI] message (and restores the component states if that publish fails). Failure notices (ntfy_util.OPS_TITLE_RE,
e.g. 조회 실패) always go out directly. Without KOSPI_LADDER (manual runs, job=kos5500 dispatch) this is ntfy_util.publish.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import ntfy_util


def spool_path() -> Path:
    return Path(os.environ.get("KOSPI_SPOOL") or Path(os.environ.get("RUNNER_TEMP") or "/tmp") / "kospi_spool.jsonl")


def publish(topic, title, message, priority=3, tags=None, server=None, **kwargs):
    if os.environ.get("KOSPI_LADDER") != "1" or ntfy_util.OPS_TITLE_RE.search(title or ""):
        return ntfy_util.publish(topic, title, message, priority, tags, server, **kwargs)
    skip = ntfy_util.ends_skip_reason(topic, title, config_name=kwargs.get("config_name"), config=kwargs.get("config"))
    if skip:  # same per-component ends_kst gate as a direct send
        print(skip)
        ntfy_util.LAST_PUBLISH.clear()
        ntfy_util.LAST_PUBLISH.update({"sent": False, "reason": skip, "title": title})
        return None
    name = Path(sys.argv[0]).stem.replace("_monitor", "")
    p = spool_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"name": name, "topic": topic, "title": title, "message": message,
                            "priority": int(priority or 3), "tags": tags, "server": server}, ensure_ascii=False) + "\n")
    ntfy_util.LAST_PUBLISH.clear()
    ntfy_util.LAST_PUBLISH.update({"sent": True, "reason": "spooled for [KOSPI]", "title": title})
    print(f"spooled for [KOSPI]: {title}")
    return None
