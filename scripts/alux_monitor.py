#!/usr/bin/env python3
from __future__ import annotations
import json, re, sys, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta
from html import unescape
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "state" / "alux_seen.json"
ZONE_KO = {
    "BUY1": "1차 매수 (6,900~7,200원)",
    "BUY2": "2차 매수 (6,200~6,600원)",
    "BUY3": "3차 매수 (5,400~5,800원)",
    "STOP": "손절 (종가 5,000원 이탈)",
    "TP1A": "1차 익절 (8,800~9,200원)",
    "TP1B": "2차 익절 (9,800~10,500원)",
    "NO_CHASE": "8,000원 위. 추격 금지",
    "BELOW_BUY3": "3차 아래. 물타기 금지",
    "WAIT": "매수·익절 사이. 대기",
    "DATA_FAIL": "시세 조회 실패",
}
DO_NOW = {
    "BUY1": "매수금의 40%만 사세요.",
    "BUY2": "35%를 더 사세요.",
    "BUY3": "남은 25%만 사세요. 5,000원 아래에서 물타기 금지.",
    "STOP": "종가 5,000원을 껈으면 전량 팔세요.",
    "TP1A": "보유의 30%를 팔아 익절하세요.",
    "TP1B": "보유의 40%를 더 팔세요.",
    "NO_CHASE": "지금 따라잡지 말세요. 가격이 내려오기를 기다리세요.",
    "BELOW_BUY3": "새로 사지 말고, 5,000원 손절선만 지키세요.",
    "WAIT": "새 주문 없이 들고 계세요.",
    "DATA_FAIL": "시세를 다시 확인하세요.",
}
TITLE_BY_ZONE = {
    "STOP": "손절 5,000 이탈",
    "BUY3": "3차 25%만",
    "BUY2": "2차 35% 추가",
    "BUY1": "1차 40%만",
    "TP1A": "30% 익절",
    "TP1B": "40% 익절",
    "NO_CHASE": "8,000 위 추격 금지",
    "BELOW_BUY3": "물타기 금지",
    "WAIT": "대기 유지",
    "DATA_FAIL": "시세 확인",
}


def zone(price, b):
    if price is None:
        return "DATA_FAIL"
    if price <= b["stop_close"]:
        return "STOP"
    lo3, hi3 = b["buy3"]
    lo2, hi2 = b["buy2"]
    lo1, hi1 = b["buy1"]
    loa, hia = b["tp1a"]
    lob, hib = b["tp1b"]
    if lo3 <= price <= hi3:
        return "BUY3"
    if lo2 <= price <= hi2:
        return "BUY2"
    if lo1 <= price <= hi1:
        return "BUY1"
    if lob <= price <= hib or price > hib:
        return "TP1B"
    if loa <= price <= hia:
        return "TP1A"
    if price >= b["no_chase"]:
        return "NO_CHASE"
    if price < lo3:
        return "BELOW_BUY3"
    return "WAIT"


def yahoo_price(ticker):
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(ticker) + "?interval=1d&range=5d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 cwally-alerts"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode())
        meta = payload["chart"]["result"][0].get("meta", {})
        price = meta.get("regularMarketPrice") or meta.get("previousClose")
        return float(price) if price is not None else None
    except Exception as exc:
        print(exc, file=sys.stderr)
        return None


def fetch_notices(code):
    url = f"https://finance.naver.com/item/news_notice.naver?code={code}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 cwally-alerts"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("euc-kr", errors="ignore")
    except Exception as exc:
        print(exc, file=sys.stderr)
        return []
    items = []
    for m in re.finditer(r'<a[^>]*href="(/item/news_read[^"]+)"[^>]*>([^<]+)</a>', html, re.I):
        title = unescape(re.sub(r"\s+", " ", m.group(2))).strip()
        href = "https://finance.naver.com" + unescape(m.group(1))
        if title:
            items.append({"id": href, "title": title, "url": href})
    out, seen = [], set()
    for it in items:
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        out.append(it)
    return out[:15]


def ntfy_publish(server, topic, title, message, priority, tags):
    payload = json.dumps({"topic": topic, "title": title, "message": message, "priority": priority, "tags": tags.split(",")}).encode()
    req = urllib.request.Request(server.rstrip("/"), data=payload, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(resp.read().decode())


def build_body(now, px, z, hot, zone_changed, prev_z, q_window):
    lines = [
        f"{now} | ALUX | 에이럭스 475580",
        "이게 뭐 알림: 에이럭스 매매 가이드입니다. 군드론 납기·실적·증자를 보고 살지 말지 결정합니다.",
        f"지금 상태: {px}원 | {ZONE_KO.get(z, z)}" + (f" (이전 {ZONE_KO.get(prev_z, prev_z)})" if zone_changed and prev_z else ""),
        f"지금 할 일: {DO_NOW.get(z, '대기하세요.')}",
        "나중에: 2차 6,200~6,600 / 3차 5,400~5,800에서만 더 상니다. 실적 발표 직전에는 새로 사지 말세요.",
        "익절: 8,800~9,200원에서 30%, 9,800~10,500원에서 40%. 타깃 9,500원.",
        "줄일 때: 종가 5,000원 이탈 전량. 적자 다시 늘어나거나 유증/전환사채면 비중 축소. 계약 취소면 폐기.",
        "하지 말 것: 8,000원 위 추격매수, 신용/미수, 5,000원 아래 물타기.",
    ]
    if q_window:
        lines.append("이번 국면: 3분기 실적창(10/1~11/20). 발표 직전 추격 금지.")
    if hot:
        lines.append("")
        lines.append("새 공시:")
        for n in hot[:5]:
            lines.append(f"- {n['title']}")
            lines.append(f"  {n['url']}")
    return lines


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "event").lower()
    cfg = json.loads((ROOT / "configs" / "alux.json").read_text(encoding="utf-8"))
    topic = "cwally-port"
    server = "https://ntfy.sh"
    now_dt = datetime.now(KST)
    now = now_dt.strftime("%Y-%m-%d %H:%M KST")
    price = yahoo_price(cfg["ticker"])
    z = zone(price, cfg["bands"])
    notices = fetch_notices(cfg["code"])
    prev = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {"seen_notices": [], "zone": None}
    seen = list(prev.get("seen_notices") or [])
    new = [n for n in notices if n["id"] not in set(seen)]
    hot = [n for n in new if any(k.lower() in n["title"].lower() for k in cfg["keywords"])]
    for n in reversed(new):
        seen.append(n["id"])
    STATE_PATH.write_text(
        json.dumps({"updated_at": now, "zone": z if z != "DATA_FAIL" else prev.get("zone"), "price": price, "seen_notices": seen[-80:]}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    px = f"{price:,.0f}" if price is not None else "없음"
    prev_z = prev.get("zone")
    zone_changed = bool(prev_z) and z not in {prev_z, "DATA_FAIL"}
    q_window = now_dt.month == 10 or (now_dt.month == 11 and now_dt.day <= 20)
    should = False
    pri = 3
    tags = "chart_with_upwards_trend"
    title = f"[ALUX] {TITLE_BY_ZONE.get(z, z)}"
    if mode == "weekly":
        should = True
        title = f"[ALUX] 주간 체크 | {TITLE_BY_ZONE.get(z, z)}"
    elif mode == "event":
        if hot:
            should = True
            title = f"[ALUX] 새 공시 | {TITLE_BY_ZONE.get(z, z)}"
            pri = 5
            tags = "warning,memo"
        if zone_changed:
            should = True
            title = f"[ALUX] 구간 변화 | {TITLE_BY_ZONE.get(z, z)}"
            pri = 5 if z in {"STOP", "BUY3", "TP1A", "TP1B"} else 4
        if not should:
            print("no alux event; silent")
            return 0
    else:
        should = True
        title = f"[ALUX] {mode} | {TITLE_BY_ZONE.get(z, z)}"
    lines = build_body(now, px, z, hot, zone_changed, prev_z, q_window)
    msg = "\n".join(lines)
    print(msg)
    if should:
        ntfy_publish(server, topic, title, msg, pri, tags)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
