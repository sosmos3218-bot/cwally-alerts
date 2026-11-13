"""Current-filings feed parser. No network."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from screeners.us_quant.edgar_atom import dedupe_accessions, parse_atom, within_cutoff
from screeners.us_quant.pipeline import acceptance_cutoff

NY = ZoneInfo("America/New_York")

SAMPLE = """<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry>
<title>4 - KAO MIN H (0001134100) (Reporting)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/1134100/000119312526459015/0001193125-26-459015-index.htm"/>
<summary type="html">
 &lt;b&gt;Filed:&lt;/b&gt; 2026-11-12 &lt;b&gt;AccNo:&lt;/b&gt; 0001193125-26-459015 &lt;b&gt;Size:&lt;/b&gt; 5 KB
</summary>
<updated>2026-11-12T21:55:03-05:00</updated>
<category scheme="https://www.sec.gov/" label="form type" term="4"/>
</entry>
<entry>
<title>4 - GARMIN LTD (0001121788) (Issuer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/1121788/000119312526459015/0001193125-26-459015-index.htm"/>
<summary type="html">Filed: 2026-11-12 AccNo: 0001193125-26-459015 Size: 5 KB</summary>
<updated>2026-11-12T21:55:03-05:00</updated>
<category scheme="https://www.sec.gov/" label="form type" term="4"/>
</entry>
<entry>
<title>4 - LATE FILER (0000000001) (Issuer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/1/000000000126000001/0000000001-26-000001-index.htm"/>
<summary type="html">Filed: 2026-11-13 AccNo: 0000000001-26-000001 Size: 4 KB</summary>
<updated>2026-11-13T03:25:00-05:00</updated>
<category scheme="https://www.sec.gov/" label="form type" term="4"/>
</entry>
</feed>
"""


def test_atom_parser_keeps_the_issuer_copy_and_the_cutoff():
    rows = parse_atom(SAMPLE)
    assert len(rows) == 3
    garmin = next(row for row in rows if row["name"] == "GARMIN LTD")
    assert garmin["cik"] == "0001121788"
    assert garmin["accn"] == "0001193125-26-459015"
    assert garmin["filename"] == "edgar/data/1121788/0001193125-26-459015.txt"
    assert garmin["filed"] == "2026-11-12"
    assert garmin["role"] == "issuer"
    merged = dedupe_accessions(rows)
    by_accn = {row["accn"]: row for row in merged}
    assert by_accn["0001193125-26-459015"]["role"] == "issuer"
    assert by_accn["0001193125-26-459015"]["cik"] == "0001121788"
    cutoff = acceptance_cutoff(date(2026, 11, 12))
    assert cutoff == datetime(2026, 11, 12, 23, 59, 59, tzinfo=NY)
    assert within_cutoff(garmin, cutoff) is True
    assert within_cutoff(by_accn["0000000001-26-000001"], cutoff) is False
