"""EDGAR daily index rows, plus the quarterly master that compiles those days."""

from __future__ import annotations

import re
from datetime import date, datetime

_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


def parse_master(text: str) -> tuple[list[dict], date | None]:
    if isinstance(text, bytes):
        text = text.decode("latin-1", errors="replace")
    received = None
    rows = []
    for line in text.splitlines():
        if line.lower().startswith("last data received:"):
            received = _received_date(line.split(":", 1)[1].strip())
            continue
        parts = line.split("|")
        if len(parts) < 5:
            continue
        cik_raw, name, form, filed_raw, filename = parts[0], parts[1], parts[2], parts[3], parts[4]
        if not cik_raw.isdigit():
            continue
        filed = _filed_date(filed_raw)
        if filed is None or "edgar/data/" not in filename:
            continue
        filename = filename.strip()
        base = filename.rsplit("/", 1)[-1]
        accn = base[:-4] if base.lower().endswith(".txt") else base
        rows.append(
            {
                "cik": str(int(cik_raw)).zfill(10),
                "name": name.strip(),
                "form": form.strip().upper(),
                "filed": filed.isoformat(),
                "filename": filename,
                "accn": accn,
            }
        )
    if received is None and rows:
        received = datetime.strptime(max(row["filed"] for row in rows), "%Y-%m-%d").date()
    return rows, received


def _filed_date(value: str) -> date | None:
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _received_date(value: str) -> date | None:
    match = re.search(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", value)
    if not match:
        return _filed_date(value)
    month = _MONTHS.get(match.group(1).lower())
    if not month:
        return None
    return date(int(match.group(3)), month, int(match.group(2)))


def dedupe_rows(rows: list[dict]) -> list[dict]:
    best = {}
    for row in rows:
        best[row["filename"]] = row
    return [best[key] for key in sorted(best)]


def quarter_labels(start: date, end: date) -> list[tuple[int, int]]:
    labels = []
    year, month = start.year, start.month
    cursor = date(year, ((month - 1) // 3) * 3 + 1, 1)
    while cursor <= end:
        quarter = (cursor.month - 1) // 3 + 1
        labels.append((cursor.year, quarter))
        if cursor.month >= 10:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 3, 1)
    return labels
