#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "state" / "flow_seen.json"
NAVER = (
    "https://finance.naver.com/sise/investorDealTrendDay.naver"
    "?bizdate={day}&isuCd=KOSPI&isuCd2=KSP"
)
TOP = ("개인", "외국인", "기관합계", "기타법인")
DETAIL = ("연기금 등", "금융투자", "투신(사모)", "은행", "기타금융")


def fmt_krw(won: float) -> str:
    sign = "+" if won > 0 else ""
    if abs(won) >= 1_000_000_000_000:
        return f"{sign}{won / 1_000_000_000_000:.2f}조"
    if abs(won) >= 100_000_000:
        return f"{sign}{won / 100_000_000:.0f}억"
    return f"{sign}{won:,.0f}원"


def parse_uk(val: str) -> float:
    cleaned = val.replace(",", "").strip()
    if not cleaned or cleaned == "-":
        return 0.0
    return float(cleaned) * 100_000_000


def fetch_flow(day: str) -> dict:
    import requests
    from bs4 import BeautifulSoup

    url = NAVER.format(day=day)
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0 cwally-alerts"}, timeout=30)
    resp.encoding = "euc-kr"
    resp.raise_for_status()
    target = f"{day[2:4]}.{day[4:6]}.{day[6:8]}"
    soup = BeautifulSoup(resp.text, "html.parser")
    for tr in soup.find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if not cells or cells[0] != target or len(cells) < 11:
            continue
        return {
            "개인": parse_uk(cells[1]),
            "외국인": parse_uk(cells[2]),
            "기관합계": parse_uk(cells[3]),
            "금융투자": parse_uk(cells[4]),
            "투신(사모)": parse_uk(cells[6]),
            "은행": parse_uk(cells[7]),
            "기타금융": parse_uk(cells[8]),
            "연기금 등": parse_uk(cells[9]),
            "기타법인": parse_uk(cells[10]) if len(cells) > 10 else 0.0,
        }
    raise RuntimeError(f"Naver에 {target} 수급 없음")


def fetch_index(day: str):
    import FinanceDataReader as fdr

    day_dt = datetime.strptime(day, "%Y%m%d").date()
    start = (day_dt - timedelta(days=10)).strftime("%Y-%m-%d")
    end = day_dt.strftime("%Y-%m-%d")
    idx = fdr.DataReader("KS11", start, end)
    dates = idx.index.strftime("%Y-%m-%d").tolist()
    if idx.empty or end not in dates:
        return None, None
    close = float(idx.loc[end]["Close"])
    loc = idx.index.get_loc(end)
    if loc == 0:
        return close, 0.0
    prev = float(idx.iloc[loc - 1]["Close"])
    return close, (close / prev - 1) * 100 if prev else 0.0


def ntfy(server, topic, title, message, priority=3):
    payload = json.dumps(
        {"topic": topic, "title": title, "message": message, "priority": priority, "tags": ["chart_with_upwards_trend"]}
    ).encode()
    req = urllib.request.Request(server.rstrip("/"), data=payload, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(resp.read().decode())


def main() -> int:
    now = datetime.now(KST)
    if now.weekday() >= 5:
        print("weekend skip")
        return 0
    day = now.strftime("%Y%m%d")
    topic = os.environ.get("NTFY_TOPIC") or "cwally-flow"
    server = os.environ.get("NTFY_SERVER") or "https://ntfy.sh"
    flow = fetch_flow(day)
    if flow["외국인"] == 0 and flow["기관합계"] == 0:
        print("zero flow; market maybe open")
        return 0
    close, chg = fetch_index(day)
    lines = [f"{now.strftime('%Y-%m-%d %H:%M KST')} | FLOW | KOSPI"]
    if close is not None:
        lines.append(f"지수 {close:,.2f} ({chg:+.2f}%)")
    lines.append("")
    for name in TOP:
        if name in flow:
            lines.append(f"{name} {fmt_krw(flow[name])}")
    lines.append("")
    for name in DETAIL:
        if name in flow:
            lines.append(f"{name} {fmt_krw(flow[name])}")
    msg = "\n".join(lines)
    print(msg)
    prev = {}
    if STATE_PATH.exists():
        try:
            prev = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            prev = {}
    if prev.get("day") == day:
        print("already sent today; silent")
        return 0
    ntfy(server, topic, f"[FLOW] KOSPI 수급 {day[4:6]}/{day[6:8]}", msg, 3)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps({"day": day, "updated_at": now.isoformat()}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
