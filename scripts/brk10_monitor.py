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
CFG_PATH = ROOT / "configs" / "brk10.json"
STATE_PATH = ROOT / "state" / "brk10.json"

PRICE_ACTION = {"BUY1", "BUY2", "BUY3", "REVIEW", "HARD", "TRIM1", "TRIM2"}
FX_ACTION = {"FX_LOW", "FX_HIGH"}

ZONE_KO = {
    "BUY1": "1차 밴드 14,350~14,650. 목표의 30%만",
    "BUY2": "2차 밴드 13,600~14,050. 목표의 40%",
    "BUY3": "3차 저점권 12,800~13,350. 절반만",
    "REVIEW": "종가 12,400 이탈. 보유 20% 축소 검토",
    "HARD": "종가 10,950 이탈. 절반 이상 재검토",
    "TRIM1": "+22% 근처. 보유 25% 익절",
    "TRIM2": "+38% 근처. 또 25% 익절, 남은 50% 유지",
    "CHASE": "15,800 위. 잔여 물량 추격 금지",
    "BETWEEN": "밴드 사이. 계획대로만",
    "DATA_FAIL": "시세 조회 실패",
    "FX_LOW": "원달러 1,320 이하. 2·3차 앞당김",
    "FX_HIGH": "원달러 1,520 이상. 신규 매수 중단",
    "FX_BASE": "환율 정상대",
}

DO_NOW = {
    "BUY1": "1차만 사세요. 목표 중 30%. 따라잡지 말세요.",
    "BUY2": "2차를 채우세요. 목표 중 40%. 이 구간 아래에서만.",
    "BUY3": "3차는 절반만 쓰세요. 나머지는 12,400 재검토 선에서.",
    "REVIEW": "보유분의 20%를 줄일지 오늘 결정하세요. 원인이 환율인지 종목인지 확인.",
    "HARD": "보유분 절반 이상 축소를 검토하세요. 지수 구성이 변했는지도 보세요.",
    "TRIM1": "보유분 25%를 팔아 비중을 12%에서 9%로 낳추세요.",
    "TRIM2": "또 25%를 팔고 50%는 5년 만기까지 남기세요.",
    "CHASE": "오늘 새로 사지 말세요. 1차 밴드로 돌아올 때까지 대기.",
    "BETWEEN": "오늘 손대지 말세요.",
    "DATA_FAIL": "네이버·야후에서 475350과 원달러를 다시 확인하세요.",
    "FX_LOW": "원화 강세. 2차·3차 매수를 앞당김세요. 이미 산 1차는 줄이지 말세요.",
    "FX_HIGH": "달러 급등. 신규 매수 중단. 비중 16%를 넘으면 1차 익절을 앞당김세요.",
    "FX_BASE": "환율은 계획대로. 가격 밴드만 보세요.",
}

TITLE_BY_ZONE = {
    "BUY1": "1차 밴드. 목표의 30%만",
    "BUY2": "2차 밴드 진입. 추격 금지",
    "BUY3": "3차 저점권. 절반만",
    "REVIEW": "축소 검토. 20%만 줄일지",
    "HARD": "절반 이상 재검토",
    "TRIM1": "1차 익절. 25% 매도",
    "TRIM2": "2차 익절. 또 25%",
    "CHASE": "15,800 위. 추격 금지",
    "BETWEEN": "유지",
    "DATA_FAIL": "시세 확인",
    "FX_LOW": "환율 극단. 2·3차 앞당김",
    "FX_HIGH": "환율 극단. 신규 매수 중단",
    "FX_BASE": "환율 정상",
}


def yahoo(ticker: str):
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
        print(f"yahoo fail {ticker}: {exc}", file=sys.stderr)
        return None


def price_zone(price, bands):
    if price is None:
        return "DATA_FAIL"
    if price <= bands["hard"]:
        return "HARD"
    if price <= bands["review"]:
        return "REVIEW"
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


def fx_zone(fx, fx_bands):
    if fx is None:
        return "DATA_FAIL"
    if fx <= fx_bands["low"]:
        return "FX_LOW"
    if fx >= fx_bands["high"]:
        return "FX_HIGH"
    return "FX_BASE"


def ntfy_publish(server, topic, title, message, priority, tags):
    payload = json.dumps(
        {
            "topic": route_topic(topic, title),
            "title": title,
            "message": message,
            "priority": priority,
            "tags": tags.split(","),
        }
    ).encode()
    req = urllib.request.Request(
        server.rstrip("/"),
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(resp.read().decode())


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "event").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    prev = {
        "price_zone": None,
        "fx_zone": None,
        "price": None,
        "fx": None,
        "last_alert_key": None,
    }
    if STATE_PATH.exists():
        try:
            prev.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass

    price = yahoo(cfg["ticker"])
    fx = yahoo(cfg["fx_ticker"])
    pz = price_zone(price, cfg["bands"])
    fz = fx_zone(fx, cfg["fx_bands"])
    if pz == "DATA_FAIL" and prev.get("price_zone"):
        pz = prev["price_zone"]
        price = prev.get("price")
    if fz == "DATA_FAIL" and prev.get("fx_zone"):
        fz = prev["fx_zone"]
        fx = prev.get("fx")

    px = f"{price:,.0f}원" if price is not None else "없음"
    fxs = f"{fx:,.1f}" if fx is not None else "없음"
    print(f"{stamp} | BRK10 | {px} | USD/KRW {fxs} | {prev.get('price_zone')}->{pz} | {prev.get('fx_zone')}->{fz}")

    alert_key = None
    if pz in PRICE_ACTION and pz != prev.get("price_zone"):
        alert_key = pz
    elif fz in FX_ACTION and fz != prev.get("fx_zone"):
        alert_key = fz

    STATE_PATH.write_text(
        json.dumps(
            {
                "updated_at": stamp,
                "price_zone": pz if pz != "DATA_FAIL" else prev.get("price_zone"),
                "fx_zone": fz if fz != "DATA_FAIL" else prev.get("fx_zone"),
                "price": price,
                "fx": fx,
                "last_alert_key": alert_key or prev.get("last_alert_key"),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    if mode != "event":
        print(f"brk10 {mode} ignored; event only")
        return 0
    if not prev.get("price_zone") and not prev.get("fx_zone"):
        print("brk10 baseline set; silent")
        return 0
    if not alert_key:
        print("brk10 no zone change; silent")
        return 0

    title_z = alert_key
    title = f"[BRK10] {TITLE_BY_ZONE.get(title_z, title_z)}"
    lines = [
        f"{stamp} | BRK10 | {cfg['display']} {cfg['code']}",
        "이게 무슨 알림: 버크셔 상위10 ETF(475350) 분할매수·축소·환율 가이드입니다. 총자산 12%가 한도입니다.",
        f"지금 상태: 가격 {px}, 원달러 {fxs}. 가격 {ZONE_KO.get(pz, pz)}. 환율 {ZONE_KO.get(fz, fz)}.",
        f"지금 할 일: {DO_NOW.get(alert_key, '')}",
        "나중에: 2·5·8·11월 13F 리밸런싱 다음 영업일에 구성·괴리율을 보세요. 비중이 16%를 넘으면 초과분을 팔고, 8%미만이면 2·3차 밴드에서만 보충.",
        "익절/줄일 때: +22%에서 25%, +38%에서 또 25%. 12,400에서 20% 축소 검토. 전량 손절은 없습니다.",
        "하지 말 것: 15,800 위 추격, 신용·미수, 환헤지로 갈아타기, 총자산 12% 초과.",
    ]
    pri = 5 if alert_key in {"HARD", "REVIEW", "FX_HIGH"} else 4
    ntfy_publish("https://ntfy.sh", "cwally-port", title, "\n".join(lines), pri, "us,chart_with_upwards_trend")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
