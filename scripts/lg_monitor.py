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
CFG_PATH = ROOT / "configs" / "lg.json"
STATE_PATH = ROOT / "state" / "lg.json"
EVENT_ZONES = {"STOP", "SHOCK", "BUY3", "TARGET1", "TARGET2"}
ZONE_KO = {
    "STOP": "손절 (10만 하회 지속)",
    "SHOCK": "자회사 실적 쇼크",
    "WATCH": "11.6만 이상. 추격 금지",
    "BUY1": "1차 매수 (11.0~11.3만)",
    "BUY2": "2차 매수 (10.5~11.0만)",
    "BUY3": "3차 매수 (10.0~10.5만)",
    "TARGET1": "1차 익절 (13만)",
    "TARGET2": "2차 익절 (14.5만)",
    "BETWEEN": "관망. 현금 유지",
    "DATA_FAIL": "시세 조회 실패",
}
DO_NOW = {
    "STOP": "신규분을 정리한 뒤 재평가하세요. 추가 매수 금지.",
    "SHOCK": "신규분을 정리한 뒤 재평가하세요. 오늘 따라잡지 말세요.",
    "WATCH": "새로 사지 말세요.",
    "BUY1": "목표 금액의 30%만 사세요.",
    "BUY2": "40%를 더 사세요.",
    "BUY3": "남은 30%만. 손절선이 가깝습니다.",
    "TARGET1": "일부 팔아 13만 익절하세요.",
    "TARGET2": "14.5만에서 더 줄이세요.",
    "BETWEEN": "현금을 유지하세요.",
    "DATA_FAIL": "시세를 다시 확인하세요.",
}
TITLE_BY_ZONE = {
    "STOP": "손절 10만 하회 지속",
    "SHOCK": "자회사 실적 쇼크",
    "WATCH": "11.6만 위 추격 금지",
    "BUY1": "1차 30%만",
    "BUY2": "2차 40% 추가",
    "BUY3": "3차 손절선 근접",
    "TARGET1": "13만 익절",
    "TARGET2": "14.5만 추가 정리",
    "BETWEEN": "관망 유지",
    "DATA_FAIL": "시세 확인",
}


def yahoo_price(ticker: str):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(ticker)
        + "?interval=1d&range=5d"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 cwally-alerts"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode())
        meta = payload["chart"]["result"][0].get("meta", {})
        price = meta.get("regularMarketPrice") or meta.get("previousClose")
        return float(price) if price is not None else None
    except Exception as exc:
        print(f"price fetch failed {ticker}: {exc}", file=sys.stderr)
        return None


def zone(price, bands):
    if price is None:
        return "DATA_FAIL"
    if price <= bands["stop_close"]:
        return "STOP"
    if price >= bands["target2"]:
        return "TARGET2"
    if price >= bands["target1"]:
        return "TARGET1"
    if bands["buy3_lo"] <= price < bands["buy3_hi"]:
        return "BUY3"
    if bands["buy2_lo"] <= price < bands["buy2_hi"]:
        return "BUY2"
    if bands["buy1_lo"] <= price <= bands["buy1_hi"]:
        return "BUY1"
    if price >= bands["watch"]:
        return "WATCH"
    return "BETWEEN"


def active_events(cfg, today):
    return [ev for ev in cfg.get("events", []) if ev["start"] <= today <= ev["end"]]


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
    prev = {"zone": None, "price": None, "below_hits": 0, "stop_sent": False, "shock_sent": False, "last_event_ids": []}
    if STATE_PATH.exists():
        try:
            prev.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass

    price = yahoo_price(cfg["ticker"])
    z = zone(price, cfg["bands"])
    if z == "DATA_FAIL" and prev.get("zone"):
        z = prev["zone"]
        price = prev.get("price")

    stop_line = float(cfg.get("stop_close") or cfg["bands"]["stop_close"])
    need = int(cfg.get("persist_hits") or 2)
    below_hits = int(prev.get("below_hits") or 0)
    if price is not None:
        below_hits = below_hits + 1 if price <= stop_line else 0
    persist_stop = price is not None and below_hits >= need
    if persist_stop:
        z = "STOP"

    live = active_events(cfg, today)
    live_ids = [e["id"] for e in live]
    new_live = [e for e in live if e["id"] not in (prev.get("last_event_ids") or [])]

    shock = False
    drop_pct = None
    prev_px = prev.get("price")
    shock_need = float(cfg.get("shock_drop_pct") or 7)
    if live and price is not None and prev_px:
        drop_pct = (float(prev_px) - float(price)) / float(prev_px) * 100.0
        if drop_pct >= shock_need:
            shock = True
            z = "SHOCK"

    zone_changed = bool(prev.get("zone")) and prev.get("zone") != z and (
        z in EVENT_ZONES or prev.get("zone") in EVENT_ZONES
    )
    first_persist = persist_stop and not prev.get("stop_sent")
    first_shock = shock and not prev.get("shock_sent")

    send = False
    title = f"[LG] {TITLE_BY_ZONE.get(z, z)}"
    if first_persist:
        send = True
        title = "[LG] 손절 10만 하회 지속"
    elif first_shock:
        send = True
        title = "[LG] 자회사 실적 쇼크"
    elif mode == "event":
        if zone_changed or new_live:
            send = True
            title = f"[LG] 구간/일정 | {TITLE_BY_ZONE.get(z, z)}"
        else:
            print("no lg event; silent")
    else:
        send = True
        title = f"[LG] 체크 | {TITLE_BY_ZONE.get(z, z)}"

    px = f"{price:,.0f}원" if price is not None else "시세 없음"
    cap = cfg["max_equity_weight_pct"]
    lines = [
        f"{stamp} | LG | {cfg['display']} {cfg['code']}",
        f"이게 뭐 알림: LG지주 매매 가이드입니다. 주식 한도는 총자산 {cap}%입니다.",
        f"지금 상태: {px} | {ZONE_KO.get(z, z)} | 10만 하회 {below_hits}/{need}회",
        f"지금 할 일: {DO_NOW.get(z, '')}",
        "나중에: 매수는 11.0~11.3 / 10.5~11.0 / 10.0~10.5만에서만. 자회사 실적창을 보고 다음 트랜차를 정하세요.",
        "익절: 13만에서 일부, 14.5만에서 추가 정리.",
        "줄일 때: 10만 하회가 2번 이어지거나, LG전자/화학 실적 쇼크면 신규분 정리 후 재평가.",
        f"하지 말 것: 11.6만 위 추격매수, 신용/미수, 총자산 {cap}% 초과.",
    ]
    if drop_pct is not None:
        lines.append(f"직전 가격 대비 {drop_pct:+.1f}%")
    if live:
        lines.append("")
        lines.append("지금 실적창:")
        for e in live:
            lines.append(f"- {e['label']} {e['start']}~{e['end']}: {e['check']}")

    STATE_PATH.write_text(
        json.dumps(
            {
                "updated_at": stamp,
                "zone": z,
                "price": price,
                "below_hits": below_hits,
                "stop_sent": bool(persist_stop),
                "shock_sent": bool(shock),
                "last_event_ids": live_ids,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    msg = "\n".join(lines)
    print(msg)
    if send:
        if persist_stop:
            pri = 5
        elif shock or zone_changed or new_live:
            pri = 4
        else:
            pri = 3
        ntfy_publish(server, topic, title, msg, pri, "warning,chart_with_downwards_trend")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
