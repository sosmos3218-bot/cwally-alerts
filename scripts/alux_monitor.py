#!/usr/bin/env python3
from __future__ import annotations
import json, os, re, sys, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "state" / "alux_seen.json"
PRE = "[실적 전] DART 계약/유증/CB · 84억 군드론 납기 · 발표 직전 추격 금지 · 손절 5000 / 익절 8800"
POST = "[발표 후] 유지=매출+적자축소 / 축소=적자재확대·증자 / 폐기=종가 5000이탈·계약취소"

def zone(price, b):
    if price is None: return "DATA_FAIL"
    if price <= b["stop_close"]: return "STOP"
    lo3,hi3=b["buy3"]; lo2,hi2=b["buy2"]; lo1,hi1=b["buy1"]; loa,hia=b["tp1a"]; lob,hib=b["tp1b"]
    if lo3<=price<=hi3: return "BUY3"
    if lo2<=price<=hi2: return "BUY2"
    if lo1<=price<=hi1: return "BUY1"
    if lob<=price<=hib or price>hib: return "TP1B"
    if loa<=price<=hia: return "TP1A"
    if price>=b["no_chase"]: return "NO_CHASE"
    if price<lo3: return "BELOW_BUY3"
    return "WAIT"

def action(z):
    return {"BUY1":"1차 40%","BUY2":"2차 35%","BUY3":"3차 25%. 5000 이탈 시 물타기 금지","STOP":"종가 5000 이탈 전량","TP1A":"8800~9200 30% 매도","TP1B":"9800~10500 40% 매도","NO_CHASE":"8000+ 추격 금지","BELOW_BUY3":"3차 아래","WAIT":"대기","DATA_FAIL":"시세 실패"}.get(z,"관찰")

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
        print(exc, file=sys.stderr); return None

def fetch_notices(code):
    url = f"https://finance.naver.com/item/news_notice.naver?code={code}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 cwally-alerts"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("euc-kr", errors="ignore")
    except Exception as exc:
        print(exc, file=sys.stderr); return []
    items=[]
    for m in re.finditer(r'<a[^>]*href="(/item/news_read[^"]+)"[^>]*>([^<]+)</a>', html, re.I):
        title = unescape(re.sub(r"\s+", " ", m.group(2))).strip()
        href = "https://finance.naver.com" + unescape(m.group(1))
        if title: items.append({"id": href, "title": title, "url": href})
    out=[]; seen=set()
    for it in items:
        if it["id"] in seen: continue
        seen.add(it["id"]); out.append(it)
    return out[:15]

def ntfy_publish(server, topic, title, message, priority, tags):
    payload = json.dumps({"topic": topic, "title": title, "message": message, "priority": priority, "tags": tags.split(",")}).encode()
    req = urllib.request.Request(server.rstrip("/"), data=payload, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(resp.read().decode())

def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "event").lower()
    cfg = json.loads((ROOT / "configs" / "alux.json").read_text(encoding="utf-8"))
    topic = os.environ.get("NTFY_TOPIC") or "cwally-port"
    server = os.environ.get("NTFY_SERVER") or "https://ntfy.sh"
    now_dt = datetime.now(KST); now = now_dt.strftime("%Y-%m-%d %H:%M KST")
    price = yahoo_price(cfg["ticker"]); z = zone(price, cfg["bands"])
    notices = fetch_notices(cfg["code"])
    prev = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {"seen_notices": [], "zone": None}
    seen = list(prev.get("seen_notices") or [])
    new = [n for n in notices if n["id"] not in set(seen)]
    hot = [n for n in new if any(k.lower() in n["title"].lower() for k in cfg["keywords"])]
    for n in reversed(new): seen.append(n["id"])
    STATE_PATH.write_text(json.dumps({"updated_at": now, "zone": z if z!="DATA_FAIL" else prev.get("zone"), "price": price, "seen_notices": seen[-80:]}, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    px = f"{price:,.0f}" if price is not None else "N/A"
    lines = [f"{now} | {mode} | 에이럭스 475580", f"가격 {px}원 | {z} | {action(z)}", "타깃 9500 / 손절 5000 / 추격금지 8000+"]
    should=False; title="[에이럭스] 체크"; pri=3; tags="chart_with_upwards_trend"
    if mode=="weekly":
        should=True; title="[에이럭스] 주간 체크"; lines += ["", PRE, "", POST]
        if now_dt.month in (10,) or (now_dt.month==11 and now_dt.day<=20):
            lines.append("3분기 국면(10/1~11/20)")
    elif mode=="event":
        if hot:
            should=True; title="[에이럭스] 공시"; pri=5; tags="warning,memo"
            lines += ["", "신규 공시:"] + [f"- {n['title']}\n  {n['url']}" for n in hot[:5]] + ["", POST]
        prev_z = prev.get("zone")
        if prev_z and z not in {prev_z, "DATA_FAIL"}:
            should=True; title=f"[에이럭스] {prev_z}→{z}"; pri=5 if z in {"STOP","BUY3","TP1A","TP1B"} else 4
            lines += [f"\n구간 변화: {prev_z} → {z}", action(z)]
        if not should:
            print("no alux event; silent"); return 0
    else:
        should=True; title=f"[에이럭스] {mode}"
    msg="\n".join(lines); print(msg)
    if should: ntfy_publish(server, topic, title, msg, pri, tags)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
