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
CFG_PATH = ROOT / "configs" / "avgochain.json"
STATE_PATH = ROOT / "state" / "avgochain.json"
EVENT_ZONES = {"BUY1", "BUY2", "BUY3", "TRIM1", "TRIM2", "STOP"}
ZONE_KO = {
    "BUY1": "1차 매수 (21,500~23,200원)",
    "BUY2": "2차 추가 (19,000~20,800원)",
    "BUY3": "3차 가산 (16,500~18,200원)",
    "TRIM1": "1차 익절 (28,000원, 절반)",
    "TRIM2": "2차 익절 (31,000원, 잔량)",
    "STOP": "종가 15,800원 이탈. 테제 중단",
    "CHASE": "25,000원 위. 추격 금지",
    "BETWEEN": "매수·익절 사이. 현금 유지",
    "DATA_FAIL": "시세 조회 실패",
}
DO_NOW = {
    "BUY1": "1차만 사세요. 한도의 30%. 25,000원 위에서 따라잡지 말세요.",
    "BUY2": "2차를 더 사세요. 한도의 40%. 이 구간에서만 가산.",
    "BUY3": "3차 마지막 가산. 한도의 30%. AVGO 가이던스가 살아 있을 때만.",
    "TRIM1": "보유의 절반을 팔아 익절하세요. 원달러 1,480 위면 절반보다 조금 더 줄이세요.",
    "TRIM2": "남은 양의 80% 익절. 20%는 36개월 러너로 남길 수 있습니다. 31,000 위 추격 재진입 금지.",
    "STOP": "종가 15,800 이탈이면 정리하고 이 테제는 끝냅니다. 바로 다시 사지 말세요.",
    "CHASE": "새로 사지 말세요. 조정 대기입니다.",
    "BETWEEN": "현금을 유지하세요. 다음 매수구간을 기다리세요.",
    "DATA_FAIL": "시세를 다시 확인하세요.",
}
TITLE_BY_ZONE = {
    "STOP": "종가 15,800 이탈 | 정리",
    "BUY3": "3차 가산 16,500~18,200",
    "BUY2": "2차 추가 19,000~20,800",
    "BUY1": "1차만 21,500~23,200",
    "TRIM1": "28,000 절반 익절",
    "TRIM2": "31,000 잔량 익절",
    "CHASE": "25,000 위 추격 금지",
    "BETWEEN": "현금 유지",
    "DATA_FAIL": "시세 확인",
}


def yahoo_px(ticker: str):
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
        price = meta.get("regularMarketPrice") or meta.get("chartPreviousClose") or meta.get("previousClose")
        return float(price) if price is not None else None
    except Exception as exc:
        print(f"fetch fail {ticker}: {exc}", file=sys.stderr)
        return None


def zone(price, bands):
    if price is None:
        return "DATA_FAIL"
    if price <= bands["stop_close"]:
        return "STOP"
    if price >= bands["trim2"]:
        return "TRIM2"
    if price >= bands["trim1"]:
        return "TRIM1"
    if price >= bands["chase_ban"]:
        return "CHASE"
    lo1, hi1 = bands["buy1"]
    lo2, hi2 = bands["buy2"]
    lo3, hi3 = bands["buy3"]
    if lo1 <= price <= hi1:
        return "BUY1"
    if lo2 <= price <= hi2:
        return "BUY2"
    if lo3 <= price <= hi3:
        return "BUY3"
    return "BETWEEN"


def fx_regime(fx, cfg_fx, prev_fx):
    if fx is None:
        return "FX_FAIL"
    if fx <= cfg_fx["strong_won_max"]:
        return "STRONG_WON"
    if fx >= cfg_fx["weak_won_min"]:
        return "WEAK_WON"
    shock = cfg_fx.get("shock_move", 40)
    if prev_fx and abs(fx - prev_fx) >= shock:
        return "FX_SHOCK"
    return "FX_BASE"


def fx_line(regime, fx):
    px = f"{fx:,.0f}" if fx is not None else "없음"
    if regime == "STRONG_WON":
        return f"원달러 {px}. 원강세. 원화 가격 약세를 AVGO 달러 실적 하락으로 보지 말 것. 환율만으로 추가매수 금지."
    if regime == "WEAK_WON":
        return f"원달러 {px}. 원약세. 익절 구간에서는 졸더 조금 더 줄이세요. 환율 수익을 테제 성공으로 보지 말 것."
    if regime == "FX_SHOCK":
        return f"원달러 {px}. 한번에 40원 이상 움직임. 오늘은 새 주문 없이 좀 보세요."
    return f"원달러 {px}. 기본 구간. 환율로 비중을 바꾸지 말세요."


def active_events(cfg, today):
    return [ev for ev in cfg.get("events", []) if ev["start"] <= today <= ev["end"]]


def ntfy_publish(server, topic, title, message, priority, tags):
    payload = json.dumps(
        {"topic": topic, "title": title, "message": message, "priority": priority, "tags": tags.split(",")}
    ).encode()
    req = urllib.request.Request(server.rstrip("/"), data=payload, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(resp.read().decode())


def pick_priority(z, live, cfg, mode):
    if mode == "print":
        return 5
    hot_ids = set(cfg.get("priority5_event_ids") or ["avgo_fy26q4"])
    if any(e.get("id") in hot_ids for e in live):
        return 5
    if z == "STOP":
        return 5
    if z in {"BUY3", "TRIM2"} or live:
        return 4
    return 3


def oneshot_date(cfg):
    raw = (cfg.get("oneshot_kst") or "2026-12-11 07:15").strip()
    return raw.split()[0]


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    if cfg.get("enabled") is False:
        print("avgochain disabled; no ntfy")
        return 0
    topic = "cwally-port"
    server = "https://ntfy.sh"
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    prev = {"zone": None, "price": None, "fx": None, "fx_regime": None, "last_event_ids": [], "updated_at": None}
    if STATE_PATH.exists():
        try:
            prev.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass

    if mode == "auto":
        mode = "weekly" if now.weekday() == 0 else "event"

    if mode == "print" and today != oneshot_date(cfg):
        print(f"avgochain print skipped; today {today} != {oneshot_date(cfg)}")
        return 0

    price = yahoo_px(cfg["ticker"])
    fx = yahoo_px(cfg.get("fx_ticker") or "USDKRW=X")
    z = zone(price, cfg["bands"])
    if z == "DATA_FAIL" and prev.get("zone"):
        z = prev["zone"]
        price = prev.get("price")
    regime = fx_regime(fx, cfg["fx"], prev.get("fx"))
    if regime == "FX_FAIL" and prev.get("fx_regime"):
        regime = prev["fx_regime"]
        fx = prev.get("fx")

    live = active_events(cfg, today)
    live_ids = [e["id"] for e in live]
    new_live = [e for e in live if e["id"] not in (prev.get("last_event_ids") or [])]
    zone_changed = bool(prev.get("zone")) and prev.get("zone") != z and (
        z in EVENT_ZONES or prev.get("zone") in EVENT_ZONES
    )
    fx_changed = bool(prev.get("fx_regime")) and prev.get("fx_regime") != regime and regime in {"STRONG_WON", "WEAK_WON", "FX_SHOCK"}

    send = False
    title = f"[0051A0] {TITLE_BY_ZONE.get(z, z)}"
    if mode == "print":
        send = True
        title = f"[0051A0] 12/10 장후 발표 | 가이던스 확인"
    elif mode == "weekly":
        send = True
        title = f"[0051A0] 주간 체크 | {TITLE_BY_ZONE.get(z, z)}"
    elif zone_changed or new_live or fx_changed:
        send = True
        if any(e.get("id") == "avgo_fy26q4" for e in live):
            title = f"[0051A0] 12/10 실적창 | {TITLE_BY_ZONE.get(z, z)}"
        elif new_live:
            title = f"[0051A0] AVGO 실적창 | {TITLE_BY_ZONE.get(z, z)}"
        elif fx_changed:
            title = f"[0051A0] 환율 | {TITLE_BY_ZONE.get(z, z)}"
        else:
            title = f"[0051A0] 구간 | {TITLE_BY_ZONE.get(z, z)}"
    else:
        print("no avgochain event; silent")

    px = f"{price:,.0f}원" if price is not None else "시세 없음"
    cap_t = cfg["max_total_weight_pct"]
    cap_e = cfg["max_equity_weight_pct"]
    if mode == "print":
        do_now = (
            "번호를 확인하세요. FY26 AI $58B·FY27 $115B가 유지되면 3차 계획 유지. "
            "하향이면 3차 취소하고 초과분을 줄이세요. 오늘 장 전 추격 매수 금지."
        )
        later = "한국 장 09:00 전에는 새 주문 없이 숫자만. 15:20 구간 알림을 다시 보세요."
    else:
        do_now = DO_NOW.get(z, "")
        later = "2차 19,000~20,800, 3차 16,500~18,200. 3차는 AVGO 가이던스가 살아 있을 때만. 12월 실적창을 보고 취소 여부를 정하세요."
    lines = [
        f"{stamp} | 0051A0 | {cfg['display']}",
        "이게 무 알림: 브로드컴 밸류체인 액티브 36개월 테제 매매 가이드입니다. 총자산 한도 3%, 주식 슬리브 6%.",
        f"지금 상태: {px} | {ZONE_KO.get(z, z)} | {fx_line(regime, fx)}",
        f"지금 할 일: {do_now}",
        f"나중에: {later}",
        "익절: 28,000에서 절반, 31,000에서 남은 양 80%. 20%는 36개월 러너.",
        "줄일 때: 종가 15,800 이탈, 또는 AVGO AI 가이던스 하향, 또는 하이퍼스켈러 캡엑스 동반 하향.",
        f"하지 말 것: 25,000 위 추격, 신용/미수, 총자산 {cap_t}%·주식 {cap_e}% 초과, 환율만 보고 추가매수.",
    ]
    if live:
        lines.append("")
        lines.append("AVGO 실적창:")
        for e in live:
            lines.append(f"- {e['label']} {e['start']}~{e['end']}: {e['check']}")

    STATE_PATH.write_text(
        json.dumps(
            {
                "updated_at": stamp,
                "zone": z,
                "price": price,
                "fx": fx,
                "fx_regime": regime,
                "last_event_ids": live_ids,
                "last_mode": mode,
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
        pri = pick_priority(z, live, cfg, mode)
        ntfy_publish(server, topic, title, msg, pri, "chart_with_upwards_trend,us")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
