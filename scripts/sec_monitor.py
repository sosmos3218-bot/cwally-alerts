#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "sec.json"
STATE_PATH = ROOT / "state" / "sec.json"

ZONE_KO = {
    "REDUCE": "20만미만",
    "BUY1": "20~24만",
    "BUY2": "24~28만",
    "WATCH": "28~29만/32~33만/38~41만/45~46만",
    "HOLD": "29~32만",
    "SELL1": "33~38만",
    "HOLD38": "38~41만",
    "NONEW": "41~45만",
    "SELL2": "46~50만",
    "EXIT": "50만위",
    "DATA_FAIL": "시세 조회 실패",
}
ACTION = {
    "REDUCE": "감액",
    "BUY1": "진입",
    "BUY2": "진입",
    "WATCH": "관찰",
    "HOLD": "보유",
    "SELL1": "익절",
    "HOLD38": "관찰",
    "NONEW": "관찰",
    "SELL2": "익절",
    "EXIT": "익절",
    "DATA_FAIL": "관찰",
}
DO_NOW = {
    "REDUCE": "신규 금지. 비중을 줄이세요.",
    "BUY1": "1차 30~40%만. LTA파기나 가이던스 하향이면 쉬세요.",
    "BUY2": "2차 20~30%만. 올인 금지.",
    "WATCH": "추격 금지. 구간 선만 보세요.",
    "HOLD": "보유. 29만 위 비중 증가 금지.",
    "SELL1": "1차 20~30% 익절.",
    "HOLD38": "38만 재이탈하면 감액. 신규 금지.",
    "NONEW": "신규 없음. 38만 재이탈시 감액.",
    "SELL2": "2차 익절.",
    "EXIT": "50만 위 정리.",
    "DATA_FAIL": "시세를 다시 확인하세요.",
}
WATCH_LINE = {
    "REDUCE": "20만",
    "BUY1": "20만/24만",
    "BUY2": "24만/28만",
    "WATCH": "29만",
    "HOLD": "29만/32만",
    "SELL1": "33만/38만",
    "HOLD38": "38만",
    "NONEW": "38만/41만",
    "SELL2": "46만",
    "EXIT": "50만",
    "DATA_FAIL": "시세",
}


def yahoo_close(ticker: str):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(ticker)
        + "?interval=1d&range=10d"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 cwally-alerts"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode())
        res = payload["chart"]["result"][0]
        meta = res.get("meta", {})
        quote = (res.get("indicators") or {}).get("quote") or [{}]
        closes = quote[0].get("close") or []
        last = None
        for val in reversed(closes):
            if val is not None:
                last = float(val)
                break
        price = last or meta.get("previousClose") or meta.get("regularMarketPrice")
        return float(price) if price is not None else None
    except Exception as exc:
        print(f"price fetch failed {ticker}: {exc}", file=sys.stderr)
        return None


def zone(price):
    if price is None:
        return "DATA_FAIL"
    if price < 200000:
        return "REDUCE"
    if price < 240000:
        return "BUY1"
    if price < 280000:
        return "BUY2"
    if price < 290000:
        return "WATCH"
    if price < 320000:
        return "HOLD"
    if price < 330000:
        return "WATCH"
    if price < 380000:
        return "SELL1"
    if price < 410000:
        return "HOLD38"
    if price < 450000:
        return "NONEW"
    if price < 460000:
        return "WATCH"
    if price < 500000:
        return "SELL2"
    return "EXIT"


def zone_label(z, price):
    if price is None:
        return ZONE_KO[z]
    if z == "REDUCE":
        return "20만미만"
    if z == "BUY1":
        return "20~24만"
    if z == "BUY2":
        return "24~28만"
    if z == "HOLD":
        return "29~32만"
    if z == "SELL1":
        return "33~38만"
    if z == "HOLD38":
        return "38~41만"
    if z == "NONEW":
        return "41~45만"
    if z == "SELL2":
        return "46~50만"
    if z == "EXIT":
        return "50만위"
    if 280000 <= price < 290000:
        return "28~29만"
    if 320000 <= price < 330000:
        return "32~33만"
    if 450000 <= price < 460000:
        return "45~46만"
    return ZONE_KO[z]


def warnings(price, prev_price):
    out = []
    if price is None:
        return out
    if price < 200000:
        out.append("20만하회")
    if price >= 290000:
        out.append("29만돌파(종가)")
    if price >= 330000:
        out.append("33만감액")
    if prev_price is not None and prev_price >= 380000 and price < 380000:
        out.append("38만재이탈")
    if price >= 410000:
        out.append("41만재평가")
    if price >= 460000:
        out.append("46만익절")
    if price >= 500000:
        out.append("50만과열")
    return out


def ntfy_publish(server, topic, title, message, priority, tags):
    payload = json.dumps(
        {"topic": topic, "title": title, "message": message, "priority": priority, "tags": tags.split(",")}
    ).encode()
    req = urllib.request.Request(server.rstrip("/"), data=payload, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(resp.read().decode())


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    if mode == "auto":
        mode = "weekly" if now.weekday() == 0 else "event"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    prev = {"zone": None, "price": None, "action": None, "last_alert_date": None, "warn": []}
    if STATE_PATH.exists():
        try:
            prev.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass

    price = yahoo_close(cfg["ticker"])
    z = zone(price)
    if z == "DATA_FAIL" and prev.get("zone"):
        z = prev["zone"]
        price = prev.get("price")
    act = ACTION[z]
    zname = zone_label(z, price)
    warn = warnings(price, prev.get("price"))
    warn_txt = ", ".join(warn) if warn else "경고 없음."
    man = None if price is None else round(price / 10000, 2)
    man_txt = f"{man}만" if man is not None else "시세없음"
    line1 = f"{act} - {man_txt}, {zname}."
    line2 = f"오늘 장중 {WATCH_LINE[z]} 선을 먼저 보고 {DO_NOW[z]}"

    zone_changed = bool(prev.get("zone")) and prev.get("zone") != z
    today = now.strftime("%Y-%m-%d")
    already = prev.get("last_alert_date") == today
    send = False
    if z == "DATA_FAIL":
        send = not already
    elif mode == "weekly":
        send = not already
    elif zone_changed or (warn and warn != (prev.get("warn") or [])):
        send = not already
    else:
        print("no sec event; silent")

    STATE_PATH.write_text(
        json.dumps(
            {
                "updated_at": stamp,
                "zone": z,
                "price": price,
                "action": act,
                "last_alert_date": today if send else prev.get("last_alert_date"),
                "warn": warn,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    display = cfg.get("display", "삼성전자")
    code = cfg.get("code", "005930")
    lines = [
        f"{stamp} | SEC | {display} {code}",
        "이게 뭐 알림: 삼성전자 주간 구간 감시입니다. 투자 권유 아닙니다.",
        f"지금 상태: {line1}",
        f"지금 할 일: {line2}",
        "나중에: 29만 위에서 비중을 늘리지 마세요. LTA, 2027 OP, 환원 숫자는 확인될 때만 하방으로 봅니다.",
        "익절/줄일 때: 33~38만 1차 20~30%, 46~50만 2차, 50만 위 정리. 38만 재이탈시 감액.",
        "하지 말 것: 20만 미만 신규, 29만 위 추격/비중증가, 올인, 신용/미수.",
        f"경고: {warn_txt}",
    ]
    msg = "\n".join(lines)
    print(msg)
    if send:
        title = f"[SEC] {act} {man_txt} {zname}"
        pri = 5 if z in {"REDUCE", "EXIT"} or "20만하회" in warn or "50만과열" in warn else (4 if warn or z in {"SELL1", "SELL2"} else 3)
        ntfy_publish("https://ntfy.sh", "cwally-port", title, msg, pri, "chart_with_upwards_trend")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
