#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from ntfy_util import route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "ccrev.json"
STATE_PATH = ROOT / "state" / "ccrev.json"


def http_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 cwally-alerts"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def http_bytes(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 cwally-alerts"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read()


def yahoo_closes(ticker: str, range_: str = "6mo"):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(ticker)
        + f"?interval=1d&range={range_}"
    )
    payload = http_json(url)
    result = payload["chart"]["result"][0]
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    rows = []
    for h, l, c in zip(highs, lows, closes):
        if h is None or l is None or c is None:
            continue
        rows.append((float(h), float(l), float(c)))
    meta = result.get("meta", {})
    last = meta.get("regularMarketPrice") or (rows[-1][2] if rows else None)
    return rows, (float(last) if last is not None else None)


def box_stats(rows, lookback: int, range_max: float):
    if len(rows) < lookback:
        return False, None, None, None
    window = rows[-lookback:]
    highs = [r[0] for r in window]
    lows = [r[1] for r in window]
    closes = [r[2] for r in window]
    hi, lo = max(highs), min(lows)
    mid = sorted(closes)[len(closes) // 2]
    range_pct = (hi - lo) / mid if mid else None
    last20 = window[-20:] if len(window) >= 20 else window
    last_hi, last_lo = max(r[0] for r in last20), min(r[1] for r in last20)
    not_breakout = last_hi <= hi * 0.985
    not_breakdown = last_lo >= lo * 1.015
    ok = range_pct is not None and range_pct <= range_max and not_breakout and not_breakdown
    return ok, range_pct, hi, lo


CONSENSUS_HIST: dict = {}


def earnings_drop(ticker: str, drop_pct: float):
    """Consensus operating-profit revision via Naver (Yahoo quoteSummary returns 401).

    Naver gives only the current consensus, so the daily value is kept in state
    (consensus_hist) and compared with the oldest sample 7~35 days back.
    """
    code = ticker.split(".")[0]
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import naver_api

        info = naver_api.get_json(f"https://m.stock.naver.com/api/stock/{code}/finance/quarter")["financeInfo"]
        key = next(t["key"] for t in info["trTitleList"] if t.get("isConsensus") == "Y")
        row = next(r for r in info["rowList"] if r.get("title") == "영업이익")
        avg = naver_api.num(row["columns"][key]["value"])
    except Exception as exc:
        print(f"earn fetch fail {ticker}: {exc}", file=sys.stderr)
        return None, None, None
    if avg is None:
        return None, None, None
    today = datetime.now(KST).date()
    hist = CONSENSUS_HIST.setdefault(ticker, {})
    hist = {d: v for d, v in hist.items() if d.startswith(key + ":")} or {}
    hist[f"{key}:{today.isoformat()}"] = avg
    cutoff = (today - timedelta(days=35)).isoformat()
    hist = {d: v for d, v in hist.items() if d.split(":")[1] >= cutoff}
    CONSENSUS_HIST[ticker] = hist
    old = [v for d, v in sorted(hist.items()) if (today - datetime.strptime(d.split(":")[1], "%Y-%m-%d").date()).days >= 7]
    if not old or old[0] == 0:
        return None, avg, None
    chg = avg / old[0] - 1
    return chg <= drop_pct, avg, chg


def news_downgrade(days: int = 14) -> int:
    q = urllib.parse.quote("삼성전자 OR SK하이닉스 (컨센서스 OR 목표주가) (하향 OR 하향조정 OR 실적 우려)")
    url = f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
    try:
        raw = http_bytes(url)
        root = ET.fromstring(raw)
    except Exception as exc:
        print(f"news fetch fail: {exc}", file=sys.stderr)
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    keys = ("하향", "절하", "우려", "내려", "쉽을 내")
    hits = 0
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "")
        pub = item.findtext("pubDate") or ""
        if not any(k in title for k in keys):
            continue
        try:
            dt = datetime.strptime(pub[:25], "%a, %d %b %Y %H:%M:%S").replace(tzinfo=timezone.utc)
            if dt < cutoff:
                continue
        except Exception:
            pass
        hits += 1
    return hits


def ntfy_publish(server, topic, title, message, priority, tags):
    payload = json.dumps(
        {"topic": route_topic(topic, title), "title": title, "message": message, "priority": priority, "tags": tags.split(",")}
    ).encode()
    req = urllib.request.Request(server.rstrip("/"), data=payload, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(resp.read().decode())


def main() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    today = now.date()
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    prev = {
        "updated_at": None,
        "box_ok": False,
        "box_since": None,
        "earn_down": False,
        "ready": False,
        "last_alert_date": None,
    }
    if STATE_PATH.exists():
        try:
            prev.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    CONSENSUS_HIST.update(prev.get("consensus_hist") or {})

    rows, last = [], None
    range_pct = hi = lo = None
    box_ok = False
    try:
        rows, last = yahoo_closes(cfg["index_ticker"])
        box_ok, range_pct, hi, lo = box_stats(rows, cfg["box_lookback_days"], cfg["box_range_max"])
    except Exception as exc:
        print(f"index fetch fail: {exc}", file=sys.stderr)
        box_ok = bool(prev.get("box_ok"))

    box_since = prev.get("box_since")
    if box_ok:
        if not box_since:
            box_since = today.isoformat()
    else:
        box_since = None
    persist = 0
    if box_since:
        persist = (today - datetime.strptime(box_since, "%Y-%m-%d").date()).days
    box_ready = box_ok and persist >= cfg["box_persist_days"]

    drops = []
    for tkr in cfg["earn_tickers"]:
        flag, avg, chg = earnings_drop(tkr, cfg["earn_drop_pct"])
        drops.append((tkr, flag, avg, chg))
    news_hits = news_downgrade()
    yahoo_down = any(flag is True for _, flag, _, _ in drops)
    earn_down = yahoo_down or news_hits >= 2
    ready = bool(box_ready and earn_down)

    last_alert = prev.get("last_alert_date")
    cooldown_ok = True
    if last_alert:
        try:
            delta = (today - datetime.strptime(last_alert, "%Y-%m-%d").date()).days
            cooldown_ok = delta >= cfg["cooldown_days"]
        except ValueError:
            cooldown_ok = True
    send = ready and cooldown_ok

    lo_b, hi_b = cfg["cc_band_pct"]
    core = cfg.get("core_ticker", "091160")
    core_from = cfg.get("core_from_pct", 55)
    core_to = cfg.get("core_to_pct", 40)
    px = f"{last:,.0f}" if last is not None else "없음"
    rng = f"{range_pct * 100:.1f}%" if range_pct is not None else "없음"
    band = f"{hi:,.0f}~{lo:,.0f}" if hi and lo else "없음"
    earn_bits = []
    for tkr, flag, avg, chg in drops:
        if chg is None:
            earn_bits.append(f"{tkr} 없음")
        else:
            earn_bits.append(f"{tkr} 30일 {chg * 100:+.1f}%")
    earn_txt = ", ".join(earn_bits) if earn_bits else "없음"

    if ready:
        title = f"[CCREV] {core} {core_from}→{core_to}% | CC는 인출 있을 때만"
        do_now = (
            f"{core}을 {core_from}%에서 {core_to}%로 줄이고 추가매수는 멈추세요. "
            f"인출 필요가 생겼으면 커버드콜 {lo_b}~{hi_b}%만 검토하세요. "
            "인출이 없으면 커버드콜은 0%를 유지하세요."
        )
    elif box_ready and not earn_down:
        title = "[CCREV] 박스만 확인 | 비중·커버드콜 보류"
        do_now = (
            f"지수는 박스입니다. 이익 추정치가 내리기 전에는 {core} 55%와 커버드콜 0%를 유지하세요."
        )
        send = False
    elif earn_down and not box_ready:
        title = "[CCREV] 추정치 하향 | 박스 미확인 보류"
        do_now = (
            f"실적 추정치는 내리는 중입니다. 박스가 수개월 확인되기 전에는 {core} 55%와 커버드콜 0%를 유지하세요."
        )
        send = False
    else:
        title = "[CCREV] 조건 미도래 | 보류"
        do_now = f"아무 것도 하지 말고 {core} 55%와 커버드콜 0%를 유지하세요."
        send = False

    lines = [
        f"{stamp} | CCREV | {core} 축소 + 커버드콜 재검토",
        (
            f"이게 뭐 알림: {core}을 {core_from}%에서 {core_to}%로 줄일지, "
            "커버드콜 10~20%를 다시 볼지 보는 알림입니다."
        ),
        (
            f"지금 상태: 코스피 {px} | 60일 범위 {rng} ({band}) | "
            f"박스 {persist}일/{cfg['box_persist_days']}일 | "
            f"추정치 {earn_txt} | 하향뉴스 {news_hits}건 | "
            f"조건 {'충족' if ready else '미충족'}."
        ),
        f"지금 할 일: {do_now}",
        (
            "나중에: 박스가 깨지거나 실적이 다시 올라가면 "
            f"{core}을 55%로 되돌리고 커버드콜은 0%로 두세요."
        ),
        (
            f"익절/줄일 때: {core}은 {core_to}% 아래로 내리지 말 것. "
            "커버드콜을 코어로 올리지 말 것. 20%를 넘기지 말 것."
        ),
        "하지 말 것: 분배율만 보고 추격. 인출 없는데 커버드콜을 높이는 것.",
    ]
    msg = "\n".join(lines)
    print(msg)

    STATE_PATH.write_text(
        json.dumps(
            {
                "updated_at": stamp,
                "box_ok": box_ok,
                "box_since": box_since,
                "box_persist_days": persist,
                "earn_down": earn_down,
                "ready": ready,
                "last_px": last,
                "range_pct": range_pct,
                "news_hits": news_hits,
                "consensus_hist": CONSENSUS_HIST,
                "last_alert_date": today.isoformat() if send else last_alert,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if send:
        ntfy_publish("https://ntfy.sh", "cwally-port", title, msg, 4, "bell,chart_with_downwards_trend")
    else:
        print("no ccrev event; silent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
