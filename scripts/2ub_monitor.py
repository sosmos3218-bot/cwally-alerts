#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from ntfy_util import route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "2ub.json"
STATE_PATH = ROOT / "state" / "2ub.json"

EVENT_ZONES = {"ADD", "BUY1", "HALT", "TRIM1", "TRIM2", "EXIT", "KILL", "DATA_FAIL"}
ZONE_KO = {
    "ADD": "추매",
    "BUY1": "1차 매수 구간",
    "HOLD": "보유",
    "HALT": "추매 중단",
    "TRIM1": "1차 익절",
    "TRIM2": "2차 익절",
    "EXIT": "청산",
    "KILL": "전량 출구",
    "DATA_FAIL": "시세 조회 실패",
}
DO_NOW = {
    "ADD": "16.5만~17.5만 또는 16.45만 이하에서만 추가.",
    "BUY1": "17.5만~19.0만, 괴리 48~55%에서만 1차.",
    "HOLD": "보유. 단타 금지.",
    "HALT": "21만 이상 또는 괴리 40% 미만. 추매 중단.",
    "TRIM1": "괴리 35%(권 30%) 촉근. 1차 익절.",
    "TRIM2": "괴리 25%. 2차 익절.",
    "EXIT": "괴리 20% 이하 또는 26만~28만. 청산.",
    "KILL": "배당 중단·최소 1만 철회·소각 후퇴·적자 히트면 전량 출구. 스크립트는 가격만 봐 출구 신호를 내지 않음.",
    "DATA_FAIL": "시세를 다시 확인.",
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


def gap_pct(common, pref):
    if common is None or pref is None or common <= 0:
        return None
    return (common - pref) / common * 100.0


def zone(pref, gap, bands, g):
    if pref is None or gap is None:
        return "DATA_FAIL"
    if gap <= g["exit"] or bands["exit_lo"] <= pref <= bands["exit_hi"]:
        return "EXIT"
    if gap <= g["trim2"]:
        return "TRIM2"
    if gap <= g["trim1"]:
        return "TRIM1"
    if pref >= bands["halt_price"] or gap < g["halt"]:
        return "HALT"
    if pref <= bands["add_hard"] or bands["add_lo"] <= pref <= bands["add_hi"]:
        return "ADD"
    if bands["buy1_lo"] <= pref <= bands["buy1_hi"] and g["buy_lo"] <= gap <= g["buy_hi"]:
        return "BUY1"
    return "HOLD"


def ntfy_publish(server, topic, title, message, priority, tags):
    payload = json.dumps(
        {"topic": route_topic(topic, title), "title": title, "message": message, "priority": priority, "tags": tags.split(",")}
    ).encode()
    if __import__("ntfy_util").dup_guard_skip_payload(server, payload):  # 하루 한 번 발송 보호 (같은 제목 오늘 이미 발송)
        return
    req = urllib.request.Request(server.rstrip("/"), data=payload, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(resp.read().decode())


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    prev = {"zone": None, "pref": None, "common": None, "gap_pct": None, "last_sent_zone": None}
    if STATE_PATH.exists():
        try:
            prev.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass

    pref = yahoo_price(cfg["pref_ticker"])
    common = yahoo_price(cfg["common_ticker"])
    gp = gap_pct(common, pref)
    z = zone(pref, gp, cfg["bands"], cfg["gap"])
    if z == "DATA_FAIL" and prev.get("zone"):
        z = prev["zone"]
        pref = prev.get("pref")
        common = prev.get("common")
        gp = prev.get("gap_pct")

    zone_changed = bool(prev.get("zone")) and prev.get("zone") != z
    monday = now.weekday() == 0
    send = False
    if mode == "force" or mode == "weekly":
        send = True
    elif mode == "event":
        send = zone_changed and z in EVENT_ZONES
    else:
        send = monday or (zone_changed and z in EVENT_ZONES)

    title_act = ZONE_KO.get(z, z)
    title = f"[2UB] 005387/005380 {title_act}"
    if monday:
        title = f"[2UB] 005387/005380 주간 | {title_act}"

    px = f"{pref:,.0f}원" if pref is not None else "시세 없음"
    cx = f"{common:,.0f}원" if common is not None else "시세 없음"
    gx = f"{gp:.1f}%" if gp is not None else "N/A"
    yld = None
    if pref:
        yld = cfg["annual_div_pref"] / pref * 100.0
    yx = f"{yld:.1f}%" if yld is not None else "N/A"

    lines = [
        f"{stamp} | 2UB | {cfg['display']} {cfg['pref_code']}",
        "이게 뭐 알림: 현대차2우B 보유 전략 주간 체크입니다. 2우B만 보유.",
        f"지금 상태: 2우B {px} / 보통 {cx} / 괴리 {gx} / 배당수익 {yx}",
        f"지금 할 일: {DO_NOW.get(z, '')}",
        "나중에: 다음 배당락은 11월 말. 락 당일 급락은 손절이 아님.",
        "익절/줄일 때: 괴리 35%(1차)·25%(2차)·20% 이하 또는 26~28만.",
        "하지 말 것: 단타, 21만·괴리 40% 미만 추매, 보통주 호환.",
    ]
    msg = "\n".join(lines)
    print(msg)

    STATE_PATH.write_text(
        json.dumps(
            {
                "updated_at": stamp,
                "zone": z,
                "pref": pref,
                "common": common,
                "gap_pct": gp,
                "last_sent_zone": z if send else prev.get("last_sent_zone"),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if send:
        pri = 5 if z in {"EXIT", "KILL", "DATA_FAIL"} else (4 if z in {"ADD", "TRIM1", "TRIM2", "HALT"} else 3)
        ntfy_publish("https://ntfy.sh", "cwally-port", title, msg, pri, "chart_with_upwards_trend")
    else:
        print("no 2ub alert; silent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
