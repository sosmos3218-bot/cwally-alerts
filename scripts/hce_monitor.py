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
CFG_PATH = ROOT / "configs" / "hce.json"
STATE_PATH = ROOT / "state" / "hce.json"
ONESHOT_DATES_2026 = {"2026-10-27", "2026-10-28", "2026-10-29"}
EVENT_ZONES = {"KILL", "REDUCE"}
ZONE_KO = {
    "KILL": "폐기선 11.4만 이하",
    "REDUCE": "축소선 11.4만~12.2만",
    "WAIT": "12.2만 위. 선취 금지",
    "HOLD": "유지 확인 가격",
    "PENDING": "가격 미확정",
    "DATA_FAIL": "시세 조회 실패",
}
DO_NOW = {
    "KILL": "3~6개월 스윙을 버리세요. 선취하지 마세요.",
    "REDUCE": "축소로 봅니다. 11.4만을 다시 깨면 폐기입니다.",
    "WAIT": "실적 익일 종가까지 선취하지 마세요.",
    "HOLD": "유지입니다. 13.0만~13.7만 회복을 봅니다.",
    "PENDING": "익일 종가로 유지/축소를 갈라세요.",
    "DATA_FAIL": "시세를 다시 확인하세요.",
}
TITLE_BY_ZONE = {
    "KILL": "11.4만 이탈 폐기",
    "REDUCE": "12.2만 이하 축소",
    "WAIT": "선취 금지 | 익일 확인",
    "HOLD": "유지",
    "PENDING": "미확정",
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
    if price <= bands["kill"]:
        return "KILL"
    if price <= bands["reduce_hi"]:
        return "REDUCE"
    return "WAIT"


def active_events(cfg, today):
    return [ev for ev in cfg.get("events", []) if ev["start"] <= today <= ev["end"]]


def upcoming_events(cfg, today, days=21):
    lim = (datetime.strptime(today, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")
    return [ev for ev in cfg.get("events", []) if today < ev["start"] <= lim]


def ntfy_publish(server, topic, title, message, priority, tags):
    payload = json.dumps(
        {"topic": topic, "title": title, "message": message, "priority": priority, "tags": tags.split(",")}
    ).encode()
    req = urllib.request.Request(
        server.rstrip("/"), data=payload, method="POST", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(resp.read().decode())


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "daily").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    topic = "cwally-port"
    server = "https://ntfy.sh"
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")

    if today[:4] != "2026" and today[5:] in {"10-27", "10-28", "10-29"}:
        print("oneshot year-guard; silent")
        return 0

    prev = {"zone": None, "price": None, "last_event_ids": [], "updated_at": None}
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

    live = active_events(cfg, today)
    soon = upcoming_events(cfg, today)
    live_ids = [e["id"] for e in live]
    new_live = [e for e in live if e["id"] not in (prev.get("last_event_ids") or [])]
    zone_changed = prev.get("zone") and prev.get("zone") != z and (
        z in EVENT_ZONES or prev.get("zone") in EVENT_ZONES
    )
    oneshot = today in ONESHOT_DATES_2026
    prenight = today == "2026-10-27"

    send = True
    if mode == "event" and not (zone_changed or new_live or oneshot):
        print("no hce event; silent")
        send = False

    px = f"{price:,.0f}원" if price is not None else "시세 없음"
    cap = cfg["max_equity_weight_pct"]
    lines = [
        f"{stamp} | HCE | {cfg['display']}",
        f"이게 뭐 알림: HD현대건설기계({cfg['code']}) 3~6개월 스윙 카드입니다.",
        f"지금 상태: {px} | {ZONE_KO.get(z, z)}",
        f"지금 할 일: {DO_NOW.get(z, '')}",
        "나중에: 실적은 익일 종가로 확인. OP 1,500억 미만 폐기, 1,700억 축소, 1,701~1,899억 미확정, 1,900억 이상 유지 대기.",
        "익절/줄일 때: 종가 114,000원 이탈 또는 OP 1,500억 미만이면 폐기. 114,000~122,000원은 축소.",
        f"하지 말 것: 선취, 추격, 신용/미수, 총자산 {cap}% 초과. 2Q 10.2% 재현을 기대하지 말 것.",
    ]
    if prenight:
        lines.append("")
        lines.append("발표 전날 밤 확인:")
        for item in cfg.get("pre_night", []):
            lines.append(f"- {item}")
        lines.append("하나라도 깨지면 선취하지 말고 1단계부터 보세요. 10/28은 예상일이고 미확정입니다.")
    if live:
        lines.append("")
        lines.append("지금 진행 중인 일정:")
        for e in live:
            lines.append(f"- {e['label']} {e['start']}~{e['end']}: {e['check']}")
    if soon:
        lines.append("")
        lines.append("21일 안 일정:")
        for e in soon:
            lines.append(f"- {e['label']} {e['start']}")

    STATE_PATH.write_text(
        json.dumps(
            {"updated_at": stamp, "zone": z, "price": price, "last_event_ids": live_ids},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    msg = "\n".join(lines)
    print(msg)
    if send:
        title = f"[HCE] {TITLE_BY_ZONE.get(z, z)}"
        if prenight:
            title = "[HCE] 발표 전날 | 선취 금지"
        elif oneshot and today == "2026-10-28":
            title = f"[HCE] 당일 종가 | {TITLE_BY_ZONE.get(z, z)}"
        elif oneshot and today == "2026-10-29":
            title = f"[HCE] 익일 종가 | {TITLE_BY_ZONE.get(z, z)}"
        elif zone_changed:
            title = f"[HCE] 구간 변화 | {TITLE_BY_ZONE.get(z, z)}"
        elif new_live:
            title = f"[HCE] 일정 창 | {TITLE_BY_ZONE.get(z, z)}"
        elif mode != "event" and now.weekday() == 0:
            title = f"[HCE] 주간 체크 | {TITLE_BY_ZONE.get(z, z)}"
        pri = 5 if z == "KILL" or prenight else (4 if z == "REDUCE" or zone_changed or oneshot else 3)
        ntfy_publish(server, topic, title, msg, pri, "tractor,chart_with_upwards_trend")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
