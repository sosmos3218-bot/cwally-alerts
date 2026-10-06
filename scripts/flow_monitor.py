#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "state" / "flow_seen.json"
CFG_PATH = ROOT / "configs" / "flow.json"
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


def fetch_flow(day: str) -> dict | None:
    """KOSPI investor net buy (won) from Naver mobile API. Legacy investorDealTrendDay is 410."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import naver_api

    now_kst = datetime.now(timezone(timedelta(hours=9)))
    if day == now_kst.strftime("%Y%m%d") and now_kst.strftime("%H:%M") < "15:35":
        print("장중 수급(잠정치)이라 판정 안 함; quiet skip")
        return None
    try:
        flow = naver_api.kospi_trend(day)
    except Exception as exc:
        print(f"Naver mobile flow fail; quiet skip: {exc}")
        return None
    if not flow:
        print(f"Naver에 {day} 수급 없음(장중이거나 휴장); quiet skip")
        return None
    return flow


def fetch_deposit_series():
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


def h5_stats(series):
    if len(series) < H5_WINDOW + 1:
        return None, False
    deps = [v[1] for v in series]
    last_date, last, _ = series[-1]
    window = deps[-(H5_WINDOW + 1):-1]
    if not window:
        return None, False
    ma = sum(window) / len(window)
    if ma <= 0:
        return None, False
    dev = (last / ma - 1.0) * 100.0
    return (last_date, last, dev), dev > H5_PCT


def s3_stats(series):
    if len(series) < H5S3_WINDOW + 1:
        return None, False
    last_date, last_dep, last_cred = series[-1]
    prev_dep = series[-(H5S3_WINDOW + 1)][1]
    prev_cred = series[-(H5S3_WINDOW + 1)][2]
    if prev_dep <= 0 or prev_cred <= 0:
        return None, False
    dep_chg = (last_dep / prev_dep - 1.0) * 100.0
    cred_chg = (last_cred / prev_cred - 1.0) * 100.0
    return (last_date, dep_chg, cred_chg), dep_chg > H5_PCT and cred_chg > H5_PCT


def fetch_index(day: str):
    import FinanceDataReader as fdr

    day_dt = datetime.strptime(day, "%Y%m%d").date()
    start = (day_dt - timedelta(days=10)).strftime("%Y-%m-%d")
    end = day_dt.strftime("%Y-%m-%d")
    try:
        idx = fdr.DataReader("KS11", start, end)
        dates = idx.index.strftime("%Y-%m-%d").tolist()
    except Exception as exc:
        print(f"fdr index fail: {exc}")
        idx, dates = None, []
    if idx is None or idx.empty or end not in dates:
        try:
            sys.path.insert(0, str(ROOT / "scripts"))
            import naver_api

            rows = naver_api.get_json("https://m.stock.naver.com/api/index/KOSPI/price?pageSize=3&page=1")
            row = next(r for r in rows if r.get("localTradedAt") == end)
            return naver_api.num(row["closePrice"]), naver_api.num(row["fluctuationsRatio"])
        except Exception as exc:
            print(f"naver index fail: {exc}")
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
    sys.path.insert(0, str(ROOT / "scripts"))
    import kr_calendar

    session = kr_calendar.session_day(now)
    if not kr_calendar.is_trading_day(session):
        print(f"{session} KRX 휴장/주말 skip")
        return 0
    day = session.strftime("%Y%m%d")
    cfg = {}
    if CFG_PATH.exists():
        try:
            cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cfg = {}
    topic = "cwally-port"
    server = cfg.get("ntfy_server") or "https://ntfy.sh"
    try:
        flow = fetch_flow(day)
    except Exception as exc:
        print(f"Naver flow unexpected error; quiet skip: {exc}")
        return 0
    if not flow:
        return 0
    if flow["외국인"] == 0 and flow["기관합계"] == 0:
        print("zero flow; market maybe open")
        return 0
    close, chg = fetch_index(day)
    fgn, inst = flow["외국인"], flow["기관합계"]
    h4 = fgn >= H4_MIN and inst >= H4_MIN
    series = fetch_deposit_series()
    h5_info, h5 = h5_stats(series)
    s3_info, s3 = s3_stats(series)

    explain = cfg.get("explain") or "오늘 코스피에서 누가 사고 팔았는지 요약입니다. 특정 종목 주문 신호가 아닙니다."
    idx_txt = f"코스피 {close:,.2f} ({chg:+.2f}%)" if close is not None else "지수 없음"
    flags = []
    if h4:
        flags.append("외국인+기관이 각 3천억 이상 샀다(강한 매수)")
    else:
        flags.append("외국인+기관 동시 강매수는 아님")
    if h5_info:
        last_date, last, dev = h5_info
        flags.append(
            f"예탁금 {last/10000:.1f}조, 20일 평균 대비 {dev:+.1f}%" + (" → 자금 유입 강함" if h5 else "")
        )
    else:
        flags.append("예탁금 데이터 없음")
    if s3_info:
        last_date, dep_chg, cred_chg = s3_info
        flags.append(
            f"5일 예탁금 {dep_chg:+.1f}% / 신용 {cred_chg:+.1f}%" + (" → 과열 조짐" if s3 else "")
        )
    else:
        flags.append("예탁금·신용 5일 데이터 없음")

    if s3:
        do_now = "장이 과열 조짐입니다. 새로 추격해 사지 말고, 이미 있는 포지션은 익절 라인만 지키세요."
        title = "[FLOW] 과열 조짐 | 추격 금지"
    elif h4:
        do_now = "외국인·기관이 같이 사고 있습니다. 기존 매수 계획은 유지하되, 이 숫자만 보고 새 종목을 추격하지는 마세요."
        title = "[FLOW] 외국인+기관 강매수"
    elif h5:
        do_now = "계좌 예탁금이 평소보다 많이 늘었습니다. 시장 호흡의 한 신호일 뿐, 종목 주문으로 쓰지 마세요."
        title = "[FLOW] 예탁금 호흡 | 종목 주문 아님"
    else:
        do_now = "특별한 강매수·과열 신호는 없습니다. 기존 계획대로 두세요."
        title = "[FLOW] 오늘 수급 평온 | 계획 유지"

    lines = [
        f"{now.strftime('%Y-%m-%d %H:%M KST')} | FLOW | {idx_txt}",
        f"이게 뭐 알림: {explain}",
        "지금 상태: " + " / ".join(flags),
        f"지금 할 일: {do_now}",
        "나중에: 내일도 같은 시간에 한 번 더 보냅니다. 종목별 매수는 [STEEL] [HCE] 같은 보유 알림을 따르세요.",
        "익절/줄일 때: 이 알림으로 팔지 않습니다. 과열 조짐(예탁금+신용 5일 +3%)이면 새 매수만 쉽게 보세요.",
        "하지 말 것: 수급 숫자만 보고 추격매수. 이 알림을 단타 주문으로 쓰지 말 것.",
        "",
        "오늘 숫자:",
    ]
    for name in TOP:
        if name in flow:
            lines.append(f"- {name} {fmt_krw(flow[name])}")
    for name in DETAIL:
        if name in flow:
            lines.append(f"- {name} {fmt_krw(flow[name])}")
    msg = "\n".join(lines)
    print(msg)
    print(f"topic={topic}")
    prev = {}
    if STATE_PATH.exists():
        try:
            prev = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            prev = {}
    if prev.get("day") == day:
        print("already sent today; silent")
        return 0
    ntfy(server, topic, title, msg, 4 if (h4 or h5 or s3) else 3)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps({"day": day, "h4": h4, "h5": h5, "s3": s3, "updated_at": now.isoformat()}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
