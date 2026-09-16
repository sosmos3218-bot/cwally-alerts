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
CFG_PATH = ROOT / "configs" / "cover0198.json"
STATE_PATH = ROOT / "state" / "cover0198.json"
EVENT_ZONES = {"STOP20", "HOLD10", "TRIM12", "TRIM20", "TRIM35"}
ZONE_KO = {
    "TRIM35": "+35% 익절 (11,540원)",
    "TRIM20": "+20% 익절 (10,260원)",
    "TRIM12": "+12% 익절 (9,580원)",
    "HOLD10": "-10% 구간 (7,695원). 매도 없음",
    "STOP20": "-20% 손절 검토 (6,840원)",
    "WATCH": "규칙 구간 밖. 분배 50/50 유지",
    "DATA_FAIL": "시세 조회 실패",
}
DO_NOW = {
    "TRIM35": "약 150주(풀매수면 310주)를 팔고 인컴 잔량만 남기세요.",
    "TRIM20": "약 164주(풀매수면 340주)를 팔아 비중을 5% 안으로 내리세요.",
    "TRIM12": "200주(풀매수면 415주) 지정가로 팔세요.",
    "HOLD10": "팔지 말고, 예정된 2차만 검토하세요.",
    "STOP20": "자동매수를 멈추고 보유의 50% 수동 매도를 검토하세요. 품질악화 2개면 잔량 폐기.",
    "WATCH": "오늘은 그대로 두세요.",
    "DATA_FAIL": "시세를 다시 확인하세요.",
}
TITLE_BY_ZONE = {
    "TRIM35": "+35% 인컴만 남기",
    "TRIM20": "+20% 비중 5%로",
    "TRIM12": "+12% 1차 매도",
    "HOLD10": "-10% 보유 유지",
    "STOP20": "-20% 50% 매도 검토",
    "WATCH": "분배 유지",
    "DATA_FAIL": "시세 확인",
}


def yahoo_chart(ticker: str):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(ticker)
        + "?interval=1d&range=1mo"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 cwally-alerts"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode())
    result = payload["chart"]["result"][0]
    meta = result.get("meta", {})
    price = meta.get("regularMarketPrice") or meta.get("chartPreviousClose") or meta.get("previousClose")
    closes = [x for x in (result.get("indicators", {}).get("quote", [{}])[0].get("close") or []) if x is not None]
    ret20 = None
    if len(closes) > 20:
        ret20 = closes[-1] / closes[-21] - 1
    return (float(price) if price is not None else None), ret20


def zone(price, bands):
    if price is None:
        return "DATA_FAIL"
    if price <= bands["stop20"]:
        return "STOP20"
    if price <= bands["hold10"]:
        return "HOLD10"
    if price >= bands["trim35"]:
        return "TRIM35"
    if price >= bands["trim20"]:
        return "TRIM20"
    if price >= bands["trim12"]:
        return "TRIM12"
    return "WATCH"


def ntfy_publish(server, topic, title, message, priority, tags):
    payload = json.dumps(
        {"topic": topic, "title": title, "message": message, "priority": priority, "tags": tags.split(",")}
    ).encode()
    req = urllib.request.Request(server.rstrip("/"), data=payload, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(resp.read().decode())


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "event").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    topic = "cwally-port"
    server = "https://ntfy.sh"
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    prev = {"zone": None, "price": None, "last_event_ids": [], "updated_at": None}
    if STATE_PATH.exists():
        try:
            prev.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass

    price = ret20 = bench20 = None
    try:
        price, ret20 = yahoo_chart(cfg["ticker"])
    except Exception as exc:
        print(f"price fetch failed: {exc}", file=sys.stderr)
    try:
        _, bench20 = yahoo_chart(cfg["bench_ticker"])
    except Exception as exc:
        print(f"bench fetch failed: {exc}", file=sys.stderr)

    z = zone(price, cfg["bands"])
    if z == "DATA_FAIL" and prev.get("zone"):
        z = prev["zone"]
        price = prev.get("price")

    live = [ev for ev in cfg.get("events", []) if ev["start"] <= today <= ev["end"]]
    live_ids = [e["id"] for e in live]
    new_live = [e for e in live if e["id"] not in (prev.get("last_event_ids") or [])]
    zone_changed = bool(prev.get("zone")) and prev.get("zone") != z and (
        z in EVENT_ZONES or prev.get("zone") in EVENT_ZONES
    )
    lag = (ret20 - bench20) if ret20 is not None and bench20 is not None else None

    send = False
    title = f"[COVER0198] {TITLE_BY_ZONE.get(z, z)}"
    if mode == "event":
        if zone_changed or new_live:
            send = True
            title = f"[COVER0198] 매도점검 | {TITLE_BY_ZONE.get(z, z)}"
        else:
            print("no cover0198 event; silent")
    else:
        send = True
        title = f"[COVER0198] 점검 | {TITLE_BY_ZONE.get(z, z)}"

    px = f"{price:,.0f}원" if price is not None else "시세 없음"
    cost = cfg["cost"]
    pnl = f"{(price / cost - 1) * 100:+.1f}%" if price else "없음"
    rel = (
        f"코스닥150 20일 {bench20 * 100:+.1f}% / ETF 20일 {ret20 * 100:+.1f}% / 상대 {lag * 100:+.1f}%p"
        if ret20 is not None and bench20 is not None and lag is not None
        else "상대수익 없음"
    )
    lines = [
        f"{stamp} | COVER0198 | {cfg['display']}",
        f"이게 뭐 알림: 키움 코스닥150 커버드콜 ETF 매도 검토입니다. 1차 {cfg['shares_phase1']}주, 원가 {cost:,}원.",
        f"지금 상태: {px} | 원가 대비 {pnl} | {ZONE_KO.get(z, z)}. {rel}",
        f"지금 할 일: {DO_NOW.get(z, '')}",
        "나중에: 2026-12 분배점검, 2027-06 절반 검토, 2028-06 교체 판단. 분배가 2개월 연속 100원 미만이면 50% 매도.",
        "익절: +12% / +20% / +35% 구간에서 순차 축소.",
        "줄일 때: 6,840원에서 50% 검토. 품질악화 2개+가 겁치면 잔량 전량 폐기.",
        "하지 말 것: 손절 후 30일 재매수. 이 ETF를 추격매수하지 말 것.",
    ]
    if live:
        lines.append("")
        lines.append("시간 매도 창:")
        for e in live:
            lines.append(f"- {e['label']} {e['start']}~{e['end']}: {e['check']}")
    if lag is not None and lag <= -0.08 and bench20 is not None and bench20 >= 0.12:
        lines.append("")
        lines.append("상대 부진: 급등장을 따라가지 못했습니다. 30%를 순수 코스닥150로 옮길지 검토하세요.")

    STATE_PATH.write_text(
        json.dumps({"updated_at": stamp, "zone": z, "price": price, "last_event_ids": live_ids}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    msg = "\n".join(lines)
    print(msg)
    if send:
        pri = 5 if z == "STOP20" or new_live else (4 if z in EVENT_ZONES or zone_changed else 3)
        ntfy_publish(server, topic, title, msg, pri, "warning,chart_with_downwards_trend")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
