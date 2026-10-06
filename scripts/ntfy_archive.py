#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "archive.json"
STATE_PATH = ROOT / "state" / "archive.json"


def fetch_topic(server: str, topic: str) -> list[dict]:
    url = f"{server.rstrip('/')}/{topic}/json?poll=1&since=48h"
    req = urllib.request.Request(url, headers={"User-Agent": "cwally-alerts-archive"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="replace").strip()
    except Exception as exc:
        print(f"archive fetch fail {topic}: {exc}", file=sys.stderr)
        return []
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("event") not in (None, "message") and msg.get("message") is None:
            continue
        if not msg.get("id") and not msg.get("message"):
            continue
        out.append(
            {
                "id": str(msg.get("id") or f"{topic}-{msg.get('time')}"),
                "topic": topic,
                "title": msg.get("title") or "",
                "message": msg.get("message") or "",
                "priority": msg.get("priority") or 3,
                "tags": msg.get("tags") or [],
                "time": msg.get("time"),
            }
        )
    return out


def main() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    prev = {"items": []}
    if STATE_PATH.exists():
        try:
            prev = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    seen = {str(x.get("id")) for x in prev.get("items") or []}
    items = list(prev.get("items") or [])
    added = 0
    for topic in cfg.get("topics") or []:
        for msg in fetch_topic(cfg.get("ntfy_server") or "https://ntfy.sh", topic):
            if msg["id"] in seen:
                continue
            seen.add(msg["id"])
            items.append(msg)
            added += 1
    items.sort(key=lambda x: x.get("time") or 0, reverse=True)
    cap = int(cfg.get("max_items") or 400)
    items = items[:cap]
    stamp = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    STATE_PATH.write_text(
        json.dumps({"updated_at": stamp, "items": items}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"archive updated {stamp} added={added} total={len(items)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
