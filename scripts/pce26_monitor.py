#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "pce26.json"
STATE_PATH = ROOT / "state" / "pce26.json"
ARCHIVE_PATH = ROOT / "state" / "archive.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}

ITEM_RE = {
    "portfolio": re.compile(r"portfolio management|investment advice|asset management|자산운용|포트폴리오", re.I),
    "software": re.compile(r"computer software|software and accessories|소프트웨어", re.I),
    "legal": re.compile(r"legal services|법률서비스|법률 서비스", re.I),
}


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def strip_html(html: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", unescape(text))


def parse_pct(text: str, patterns: list[str]):
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return None


def parse_bea(text: str) -> dict:
    yoy_head = parse_pct(
        text,
        [
            r"PCE price index increased ([0-9.]+) percent from one year ago",
            r"PCE price index (?:increased|rose) ([0-9.]+) percent .*(?:year|year ago)",
            r"increased ([0-9.]+) percent from the same month one year ago",
        ],
    )
    yoy_core = parse_pct(
        text,
        [
            r"Excluding food and energy, the PCE price index increased ([0-9.]+) percent from one year ago",
            r"excluding food and energy[\s\S]{0,80}increased ([0-9.]+) percent from one year ago",
        ],
    )
    mom_head = parse_pct(
        text,
        [
            r"PCE price index for August (?:increased|decreased|was unchanged at) ([0-9.]+) percent",
            r"From the preceding month, the PCE price index for August (?:increased|decreased) ([0-9.]+) percent",
        ],
    )
    mom_core = parse_pct(
        text,
        [
            r"Excluding food and energy, the PCE price index (?:increased|decreased) ([0-9.]+) percent",
        ],
    )
    if re.search(r"PCE price index for August decreased", text, re.I) and mom_head is not None:
        mom_head = -abs(mom_head)
    published = bool(
        re.search(r"Personal Income and Outlays, August 2026", text, re.I)
        and (yoy_core is not None or yoy_head is not None)
        and not re.search(r"EMBARGOED UNTIL RELEASE AT 8:30 a\.m\. EDT, Wednesday, September 30", text)
    )
    items = {k: bool(rx.search(text)) for k, rx in ITEM_RE.items()}
    return {
        "published": published,
        "yoy_head": yoy_head,
        "yoy_core": yoy_core,
        "mom_head": mom_head,
        "mom_core": mom_core,
        "items": items,
    }


def last_fred(url: str, min_date: str):
    raw = fetch(url)
    rows = []
    for line in raw.strip().splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 2:
            continue
        date, val = parts[0].strip(), parts[1].strip()
        if val in {".", ""}:
            continue
        try:
            rows.append((date, float(val)))
        except ValueError:
            continue
    rows = [r for r in rows if r[0] >= min_date]
    return rows[-1] if rows else None


def load_state() -> dict:
    state = {
        "final_notified": False,
        "fail_notified": False,
        "empty_notified": False,
        "audit_notified": False,
        "last_key": None,
        "updated_at": None,
        "bea_ok": None,
        "fred_ok": None,
        "trim_ok": None,
        "parsed": {},
    }
    if STATE_PATH.exists():
        try:
            state.update(json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return state
