#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
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
DEPOSIT_URL = "https://finance.naver.com/sise/sise_deposit.naver"
TOP = ("개인", "외국인", "기관합계", "기타법인")
DETAIL = ("연기금 등", "금융투자", "투신(사모)", "은행", "기타금융")
H4_MIN = 300_000_000_000
H5_WINDOW = 20
H5_PCT = 3.0
H5S3_WINDOW = 5


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


def fetch_deposit_series():
    """[(date_str, deposit_eok, credit_eok), ...] newest last."""
    import requests

    rows = {}
    try:
        for page in (1, 2, 3):
            r = requests.get(DEPOSIT_URL, params={"page": page}, headers={"User-Agent": "Mozilla/5.0 cwally-alerts"}, timeout=20)
            r.encoding = "euc-kr"
            if r.status_code != 200:
                continue
            for block in re.findall(r"<tr[^>]*>(.*?)</tr>", r.text, flags=re.S):
                txt = re.sub(r"<[^>]+>", " ", block)
                txt = re.sub(r"\s+", " ", txt).strip()
                m = re.match(r"^(\d{2}\.\d{2}\.\d{2})\s+(.+)$", txt)
                if not m:
                    continue
                nums = m.group(2).replace(",", "").split()
                if len(nums) < 3:
                    continue
                try:
                    rows[m.group(1)] = (float(nums[0]), float(nums[2]))
                except ValueError:
                    continue
    except Exception as exc:
        print(f"deposit fetch skip: {exc}")
        return []
    return [(d, v[0], v[1]) for d, v in sorted(rows.items())]


def h5_line(series) -> tuple[str, bool]:
    if len(series) < H5_WINDOW + 1:
        return "H5-S2 예탁금: 데이터 없음", False
    deps = [v[1] for v in series]
    last_date, last, _ = series[-1]
    window = deps[-(H5_WINDOW + 1):-1]
    if not window:
        return "H5-S2 예탁금: 데이터 없음", False
    ma = sum(window) / len(window)
    if ma <= 0:
        return "H5-S2 예탁금: 데이터 없음", False
    dev = (last / ma - 1.0) * 100.0
    fired = dev > H5_PCT
    status = f"ON +{dev:.1f}% vs 20일평균" if fired else f"off {dev:+.1f}%"
    return f"H5-S2 예탁금: {status} ({last_date} {last/10000:.1f}조)", fired


def h5s3_line(series) -> tuple[str, bool]:
    if len(series) < H5S3_WINDOW + 1:
        return "H5-S3 천장: 데이터 없음", False
    last_date, last_dep, last_cred = series[-1]
    prev_dep = series[-(H5S3_WINDOW + 1)][1]
    prev_cred = series[-(H5S3_WINDOW + 1)][2]
    if prev_dep <= 0 or prev_cred <= 0:
        return "H5-S3 천장: 데이터 없음", False
    dep_chg = (last_dep / prev_dep - 1.0) * 100.0
    cred_chg = (last_cred / prev_cred - 1.0) * 100.0
    fired = dep_chg > H5_PCT and cred_chg > H5_PCT
    if fired:
        status = f"ON 예탁5일 {dep_chg:+.1f}% 신용5일 {cred_chg:+.1f}%"
    else:
        status = f"off 예탁5일 {dep_chg:+.1f}% 신용5일 {cred_chg:+.1f}%"
    return f"H5-S3 천장: {status} ({last_date})", fired


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
    fgn, inst = flow["외국인"], flow["기관합계"]
    h4 = fgn >= H4_MIN and inst >= H4_MIN
    series = fetch_deposit_series()
    h5_text, h5 = h5_line(series)
    s3_text, s3 = h5s3_line(series)
    lines = [f"{now.strftime('%Y-%m-%d %H:%M KST')} | FLOW | KOSPI"]
    if close is not None:
        lines.append(f"지수 {close:,.2f} ({chg:+.2f}%)")
    lines.append("H4 동반강매수: " + ("ON 외국인+기관 각 3천억+" if h4 else "off"))
    lines.append(h5_text)
    lines.append(s3_text)
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
    tag = ("H4 " if h4 else "") + ("H5 " if h5 else "") + ("S3 " if s3 else "")
    title = f"[FLOW] {tag}KOSPI 수급 {day[4:6]}/{day[6:8]}"
    ntfy(server, topic, title, msg, 4 if (h4 or h5 or s3) else 3)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps({"day": day, "h4": h4, "h5": h5, "s3": s3, "updated_at": now.isoformat()}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
