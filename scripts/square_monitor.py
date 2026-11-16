#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ntfy_util  # noqa: E402,F401  ntfy_util 안전장치(df362a5)가 import 시점에 걸리도록 맨 위에서 import

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "square.json"
STATE_PATH = ROOT / "state" / "square.json"

def http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 cwally-alerts"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())

def yahoo_price(ticker: str):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(ticker)
        + "?interval=1d&range=5d"
    )
    try:
        payload = http_json(url)
        meta = payload["chart"]["result"][0].get("meta", {})
        price = meta.get("regularMarketPrice") or meta.get("previousClose")
        return float(price) if price is not None else None
    except Exception as exc:
        print(f"price fetch failed {ticker}: {exc}", file=sys.stderr)
        return None

def ntfy_publish(server, topic, title, message, priority):
    payload = json.dumps(
        {"topic": topic, "title": title, "message": message, "priority": priority, "tags": ["chart_with_upwards_trend"]}
    ).encode()
    if __import__("ntfy_util").dup_guard_skip_payload(server, payload):  # 하루 한 번 발송 보호 (같은 제목 오늘 이미 발송)
        return
    req = urllib.request.Request(
        server.rstrip("/"), data=payload, method="POST", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(resp.read().decode())

def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    dry = mode == "dry"
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    if now.year != int(cfg.get("year_guard") or 2026):
        print("year guard; silent")
        return 0
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    sq = yahoo_price(cfg["ticker_sq"])
    hx = yahoo_price(cfg["ticker_hx"])
    if sq is None or hx is None:
        print("DATA_FAIL")
        return 0
    nav = (hx * cfg["shares_hx"] + cfg["other_nav"]) / cfg["shares_sq"]
    discount = 1 - sq / nav if nav else 0
    prev = {}
    if STATE_PATH.exists():
        try:
            prev = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    attractive = discount >= cfg["attractive_discount"]
    high = discount >= cfg["high_attractive"]
    status = "HIGH" if high else ("ATTRACTIVE" if attractive else "NORMAL")
    status_ko = {"HIGH": "매력 높음 | 할인 확대", "ATTRACTIVE": "매력 있음 | 할인 유지", "NORMAL": "평소 | 추가 확인"}[status]
    do_now = {
        "HIGH": "NAV 할인 확대. 하이닉스 장기 낙관 전제 하 할인 입장 기회 검토.",
        "ATTRACTIVE": "할인 유지. 하이닉스 실적과 할인율 추이 모니터.",
        "NORMAL": "할인 축소. 추가 매수 보류, 하이닉스 추이 우선."
    }[status]
    lines = [
        f"이게 뭐 알림: SK스퀘어(402340) 투자매력 점검. 하이닉스 프록시 + NAV 할인 기반.",
        f"지금 상태: 스퀘어 {sq:,.0f}원, 하이닉스 {hx:,.0f}원, NAV 약 {nav:,.0f}원, 할인 {discount*100:.1f}% | {status_ko}",
        f"지금 할 일: {do_now}",
        "나중에: 하이닉스 AI 메모리 사이클과 주주환원 집행 여부 확인. 할인 30% 목표 추이.",
        "익절/줄일 때: 하이닉스 하락 또는 할인 30% 이하 축소 시 비중 재검토. 고점 대비 큰 조정 시 축소.",
        "하지 말 것: 할인율만 보고 추격매수. 독립 사업 가치 과대평가 금지. 이 알림은 매수 신호가 아님."
    ]
    sell = {}
    try:
        sell = json.loads((ROOT / "state" / "square_sell.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    sell_ko = {"SELL": "매도 검토", "WATCH": "주의 관찰", "HOLD": "그대로 보유"}.get(sell.get("last_status") or "", "")
    if sell_ko:
        lines.insert(2, f"매도 점검(하이닉스 대비, square_sell_alert): {sell_ko} ({sell.get('updated_at') or '-'} 기준)")
    msg = "\n".join(lines)
    print(msg)
    week = now.strftime("%G-W%V")
    changed = prev.get("last_status") != status
    weekly = prev.get("last_weekly_week") != week
    title = f"[SQUARE] 402340 {status_ko}" if changed or prev.get("last_status") is None else f"[SQUARE] 402340 주간 체크 | {status_ko}"
    print(f"TITLE: {title}")
    print(f"STATUS: {status} DISCOUNT: {discount*100:.1f}%")
    if dry and bool(cfg.get("nav_status_alerts", True)):
        print("DRY RUN: no publish, no state write")
        return 0
    # send only if the status changed, or once a week (first run of the ISO week; the afternoon slot runs Mon-Fri,
    # so normally Monday). The weekly message also carries the square_sell_alert status, so SELL/WATCH is not
    # repeated as a separate message.
    # 2026-11-16 (사용자 요청 2026-11-05 t60u, t61u): NAV 40%/45% 상태 변화 알림은 끄고 할인율 판정은
    # [402340] k402340_monitor.py(KRX 확정 종가, 47/50/30 선) 하나로 모은다. nav_status_alerts=false 이면
    # square_sell_alert 의 SELL/WATCH 상태 주간 반복(월요일 첫 실행)만 남긴다. HOLD 면 조용히.
    nav_alerts = bool(cfg.get("nav_status_alerts", True))
    if not nav_alerts:
        sell_status = sell.get("last_status") or ""
        if weekly and sell_status in ("SELL", "WATCH"):
            title = f"[SQUARE] 402340 주간 매도 점검 | {sell_ko}"
            print(f"TITLE: {title} (nav_status_alerts off)")
            if dry:
                print("DRY RUN: no publish, no state write")
                return 0
            ntfy_publish("https://ntfy.sh", "cwally-port", title, msg, 4)
            sent = True
        else:
            print("nav_status_alerts off; silent (sell status weekly repeat only)")
            if dry:
                print("DRY RUN: no publish, no state write")
                return 0
            sent = False
    elif changed or weekly or prev.get("last_status") is None:
        ntfy_publish("https://ntfy.sh", "cwally-port", title, msg, 4 if high else 3)
        sent = True
    else:
        print("no change; silent")
        sent = False
    STATE_PATH.write_text(
        json.dumps(
            {
                "updated_at": stamp,
                "last_status": status,
                "last_price_sq": sq,
                "last_price_hx": hx,
                "discount": round(discount, 4),
                "last_sent": sent,
                "last_weekly_week": week if sent else prev.get("last_weekly_week"),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
