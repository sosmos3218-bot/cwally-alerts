#!/usr/bin/env python3
from __future__ import annotations
import json, sys, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from ntfy_util import route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "state" / "steel_zones.json"
KEYS = ("POSCO", "HYUNDAI", "POONGSAN")
EVENT_ZONES = {"BUY2", "BUY3", "REVIEW", "TARGET1", "TARGET2", "STRETCH"}
ZONE_KO = {
    "BUY1": "1차 매수 구간",
    "BUY2": "2차 추가 매수 구간",
    "BUY3": "3차 마지막 매수 구간",
    "REVIEW": "줄일 재검토 구간",
    "TARGET1": "1차 익절 구간",
    "TARGET2": "2차 익절 구간",
    "STRETCH": "더 줄일 검토 구간",
    "ABOVE_BUY1": "1차 위. 추격 금지",
    "BETWEEN": "매수가 아니라 현금 유지",
    "DATA_FAIL": "시세 조회 실패",
}
DO_NOW = {
    "BUY1": "이 종목 매수금의 40%만 사세요.",
    "BUY2": "논거가 같으면 35%를 더 사세요.",
    "BUY3": "남은 25%만 소량 사세요. 물타기 금지.",
    "REVIEW": "비중을 줄이고 논거를 다시 보세요.",
    "TARGET1": "현대제철·풍산은 일부 팔아 익절하세요.",
    "TARGET2": "철강 3종목 전체의 40~50%를 줄이세요.",
    "STRETCH": "더 정리할지 검토하세요. 추가 매수는 마세요.",
    "ABOVE_BUY1": "지금은 따라잡지 말고, 가격이 내려오기를 기다리세요.",
    "BETWEEN": "새로 사지 말고 현금을 유지하세요.",
    "DATA_FAIL": "시세를 다시 확인하세요.",
}
TITLE_BY_ZONE = {
    "REVIEW": "줄일 재검토",
    "BUY3": "3차만 소량",
    "BUY2": "2차 추가 검토",
    "BUY1": "1차 40%만",
    "TARGET1": "1차 익절 검토",
    "TARGET2": "2차 익절 검토",
    "STRETCH": "추가 정리 검토",
    "ABOVE_BUY1": "추격 금지",
    "BETWEEN": "추가매수 보류",
    "DATA_FAIL": "시세 확인",
}


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
        print(f"price fetch failed {ticker}: {exc}", file=sys.stderr)
        return None


def zone(price, bands):
    if price is None:
        return "DATA_FAIL"
    if price <= bands["review"]:
        return "REVIEW"
    lo3, hi3 = bands["buy3"]
    lo2, hi2 = bands["buy2"]
    lo1, hi1 = bands["buy1"]
    if lo3 <= price <= hi3:
        return "BUY3"
    if lo2 <= price <= hi2:
        return "BUY2"
    if lo1 <= price <= hi1:
        return "BUY1"
    if price >= bands["stretch"]:
        return "STRETCH"
    if price >= bands["target2"]:
        return "TARGET2"
    if price >= bands["target1"]:
        return "TARGET1"
    if price > hi1:
        return "ABOVE_BUY1"
    return "BETWEEN"


def ntfy_publish(server, topic, title, message, priority, tags):
    payload = json.dumps({"topic": route_topic(topic, title), "title": title, "message": message, "priority": priority, "tags": tags.split(",")}).encode()
    req = urllib.request.Request(server.rstrip("/"), data=payload, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(resp.read().decode())


def pick_focus(zones):
    for key in ("REVIEW", "BUY3", "STRETCH", "TARGET2", "TARGET1", "BUY2", "BUY1", "ABOVE_BUY1", "BETWEEN", "DATA_FAIL"):
        if key in zones:
            return key
    return "BETWEEN"


# 2026-10-30 채팅 승인 t24u: POSCO홀딩스가 매수 구간(1~3차)이나 재검토 구간이면 11/19까지 한 줄 덧붙임. 그 밖은 그대로.
STEELRE_ZONES = {"BUY1", "BUY2", "BUY3", "REVIEW"}
STEELRE_UNTIL = "2026-11-19"
STEELRE_LINE = "재진입 판정은 11/19 [STEELRE] 결과 우선 (9/15 기준 가격대)"


def steelre_note(rows, today):
    posco = next((z for key, *_, z in rows if key == "POSCO"), None)
    return STEELRE_LINE if posco in STEELRE_ZONES and today <= STEELRE_UNTIL else None


def selftest():
    r = lambda z: [("POSCO", {}, {}, 1.0, z), ("HYUNDAI", {}, {}, 1.0, "BUY1")]  # noqa: E731
    for z, day, want in (("BUY1", "2026-11-19", True), ("BUY3", "2026-10-30", True), ("REVIEW", "2026-11-01", True),
                         ("BUY2", "2026-11-20", False), ("BETWEEN", "2026-11-01", False), ("ABOVE_BUY1", "2026-11-01", False),
                         ("TARGET1", "2026-11-01", False)):
        assert (steelre_note(r(z), day) is not None) == want, (z, day)
    assert "\u00b7" not in STEELRE_LINE and "\u2014" not in STEELRE_LINE
    print("steel selftest ok")
    return 0


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "weekly").lower()
    if mode == "selftest":
        return selftest()
    cfg = json.loads((ROOT / "configs" / "steel.json").read_text(encoding="utf-8"))
    topic = "cwally-port"
    server = "https://ntfy.sh"
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    prev = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {"zones": {}, "prices": {}}
    prev_zones = prev.get("zones", {})
    rows, new_zones, new_prices, changes = [], {}, {}, []
    for key in KEYS:
        meta, bands = cfg["weights"][key], cfg["bands"][key]
        price = yahoo_price(meta["ticker"])
        z = zone(price, bands)
        rows.append((key, meta, bands, price, z))
        if z == "DATA_FAIL" and key in prev_zones:
            new_zones[key] = prev_zones[key]
            new_prices[key] = prev.get("prices", {}).get(key)
            continue
        new_zones[key] = z
        new_prices[key] = price
        old = prev_zones.get(key)
        if old and old != z and (z in EVENT_ZONES or old in EVENT_ZONES):
            kind = "진입" if z in EVENT_ZONES and old not in EVENT_ZONES else ("이탈" if old in EVENT_ZONES and z not in EVENT_ZONES else "이동")
            changes.append((meta["name"], old, z, kind))
    STATE_PATH.write_text(json.dumps({"updated_at": now, "zones": new_zones, "prices": new_prices}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if mode == "event" and (not prev_zones or not changes):
        print("no steel event; silent")
        return 0

    cap = cfg.get("portfolio", {}).get("max_equity_weight_pct", 30)
    focus = pick_focus([z for *_, z in rows])
    lines = [
        f"{now} | STEEL | 철강 3종목",
        "이게 뭐 알림: 포스코홀딩스 60% / 현대제철 25% / 풍산 15% 매매 구간 점검입니다. 주식 한도는 총자산 {cap}%입니다.".format(cap=cap),
        "지금 상태:",
    ]
    for key, meta, bands, price, z in rows:
        px = f"{price:,.0f}원" if price is not None else "시세 없음"
        lines.append(f"- {meta['name']} {meta['weight_pct']}% | {px} | {ZONE_KO.get(z, z)}")
    if changes:
        lines.append("")
        lines.append("밴드 구간:")
        for name, old, z, kind in changes:
            lines.append(f"- {name}: {ZONE_KO.get(old, old)} → {ZONE_KO.get(z, z)} ({kind})")
    todo = []
    for key, meta, bands, price, z in rows:
        if z in {"REVIEW", "BUY3", "BUY2", "BUY1", "TARGET1", "TARGET2", "STRETCH"}:
            todo.append(f"{meta['name']}: {DO_NOW[z]}")
    if not todo:
        todo.append(DO_NOW[focus])
    lines += [
        "",
        "지금 할 일: " + " ".join(todo),
        "나중에: 2차·3차 가격에 내려왔을 때만 더 삽니다. 포스코 30.0~31.5만 / 현대제철 2.95~3.15만 / 풍산 6.8~7.3만이 2차입니다.",
        "익절: 포스코 40만·46만, 현대제철 4.0만·4.3만, 풍산 9.5만·10.5만. 2차 목표에서 전체 40~50% 줄입니다.",
        "줄일 때: 포스코 26만, 현대제철 2.5만, 풍산 5.6만 아래면 비중을 줄이고 다시 봅니다.",
        "하지 말 것: 위에서 추격매수, 신용/미수, 철강 비중 {cap}% 초과.".format(cap=cap),
    ]
    note = steelre_note(rows, datetime.now(KST).strftime("%Y-%m-%d"))
    if note:
        lines.insert(lines.index("지금 할 일: " + " ".join(todo)) + 1, note)
    if mode == "quarterly":
        lines.append("분기 확인: 스프레드/리튀/판가/탄약수주. +40% 난 종목은 원래 비중으로 돌리세요.")

    focus_title = TITLE_BY_ZONE.get(focus, "구간 점검")
    if mode == "event":
        title = f"[STEEL] 005490/004020/103140 구간 변화 | {focus_title}"
    elif mode == "quarterly":
        title = f"[STEEL] 005490/004020/103140 분기 리밸런스 | {focus_title}"
    else:
        title = f"[STEEL] 005490/004020/103140 {focus_title}"
    msg = "\n".join(lines)
    print(msg)
    pri = 5 if any(z in {"REVIEW", "BUY3"} for *_, z in rows) else (4 if mode != "weekly" else 3)
    ntfy_publish(server, topic, title, msg, pri, "factory,chart_with_upwards_trend")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
