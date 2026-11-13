"""Latest Form 4 filings from the EDGAR current-filings Atom feed.

The daily index for a US business day is often unpublished at 07:30 KST
(17:30 America/New_York in winter). This feed lists filings as they are
accepted. Pages are newest first.
"""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import quote
from xml.etree import ElementTree

NS = "{http://www.w3.org/2005/Atom}"
_HREF = re.compile(r"/Archives/edgar/data/(\d+)/(\d+)/(\d{10}-\d{2}-\d{6})-index\.htm", re.IGNORECASE)
_TITLE = re.compile(r"^(4/A|4)\s+-\s+(.*?)\s+\((\d+)\)\s+\(([^)]+)\)\s*$")
_FILED = re.compile(r"Filed:\s*(\d{4}-\d{2}-\d{2})")
_ACC = re.compile(r"AccNo:\s*(\d{10}-\d{2}-\d{6})")


def atom_page_url(form: str, start: int, count: int = 100) -> str:
    return (
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
        f"&type={quote(form, safe='')}&owner=include&count={count}&start={start}&output=atom"
    )


def parse_atom(text: str | bytes) -> list[dict]:
    if isinstance(text, bytes):
        text = text.decode("iso-8859-1", errors="replace")
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return []
    rows = []
    for entry in root.findall(f"{NS}entry"):
        title = (entry.findtext(f"{NS}title") or "").strip()
        link = ""
        for item in entry.findall(f"{NS}link"):
            if item.attrib.get("rel", "alternate") == "alternate":
                link = item.attrib.get("href") or ""
                break
        summary = re.sub(r"<[^>]+>", " ", entry.findtext(f"{NS}summary") or "")
        updated = (entry.findtext(f"{NS}updated") or "").strip()
        category = entry.find(f"{NS}category")
        form = (category.attrib.get("term") if category is not None else "") or ""
        title_match = _TITLE.match(title)
        href_match = _HREF.search(link)
        filed_match = _FILED.search(summary)
        acc_match = _ACC.search(summary)
        if href_match is None or filed_match is None:
            continue
        cik = str(int(href_match.group(1))).zfill(10)
        accn = acc_match.group(1) if acc_match else href_match.group(3)
        role = ""
        name = ""
        if title_match:
            form = form or title_match.group(1)
            name = title_match.group(2).strip()
            role = title_match.group(4).strip().lower()
        rows.append(
            {
                "cik": cik,
                "name": name,
                "form": (form or "4").upper(),
                "filed": filed_match.group(1),
                "filename": f"edgar/data/{int(cik)}/{accn}.txt",
                "accn": accn,
                "accepted": updated,
                "role": role,
                "source": "atom",
            }
        )
    return rows


def prefer_filing(current: dict, incoming: dict) -> dict:
    """Issuer rows and index rows outrank a reporting-owner duplicate."""
    def rank(row: dict) -> tuple:
        role = (row.get("role") or "").lower()
        if row.get("source") == "index" or role in {"", "issuer"}:
            role_rank = 2
        else:
            role_rank = 1
        return (role_rank, 1 if row.get("accepted") else 0)

    if rank(incoming) > rank(current):
        merged = dict(incoming)
        if current.get("accepted") and not merged.get("accepted"):
            merged["accepted"] = current["accepted"]
        return merged
    merged = dict(current)
    if incoming.get("accepted") and not merged.get("accepted"):
        merged["accepted"] = incoming["accepted"]
    return merged


def dedupe_accessions(rows: list[dict]) -> list[dict]:
    best: dict[str, dict] = {}
    for row in rows:
        key = row.get("accn") or row.get("filename")
        current = best.get(key)
        best[key] = row if current is None else prefer_filing(current, row)
    return [best[key] for key in sorted(best)]


def within_cutoff(row: dict, cutoff: datetime) -> bool:
    accepted = row.get("accepted")
    if not accepted:
        return row.get("filed", "") <= cutoff.date().isoformat()
    try:
        stamp = datetime.fromisoformat(accepted)
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=cutoff.tzinfo)
    return stamp <= cutoff
