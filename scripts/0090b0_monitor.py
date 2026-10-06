#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "0090b0.json"
STATE_PATH = ROOT / "state" / "0090b0.json"

sys.path.insert(0, str(ROOT / "scripts"))
from ntfy_util import publish  # noqa: E402


def fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 cwally-alerts"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def parse_num(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "").replace("%", "").replace("+", "").strip()
    if text in {"", "-", "N/A"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def quote(code: str):
    basic = fetch_json(f"https://m.stock.naver.com/api/stock/{code}/basic")
    integ = fetch_json(f"https://m.stock.naver.com/api/stock/{code}/integration")
    price = parse_num(basic.get("closePrice"))
    change = parse_num(basic.get("compareToPreviousClosePrice"))
    change_pct = parse_num(basic.get("fluctuationsRatio"))
    indicator = integ.get("etfKeyIndicator") or {}
    nav = parse_num(indicator.get("nav"))
    premium = parse_num(indicator.get("deviationRate"))
    if premium is None and price and nav:
        premium = (price - nav) / nav * 100.0
    volume = None
    value = None
    for info in integ.get("totalInfos") or []:
        if info.get("code") == "accumulatedTradingVolume":
            volume = parse_num(info.get("value"))
        if info.get("code") == "accumulatedTradingValue":
            value = info.get("value")
    return {
        "price": price,
        "change": change,
        "change_pct": change_pct,
        "nav": nav,
        "premium": premium,
        "aum": indicator.get("totalNav"),
        "volume": volume,
        "value": value,
        "status": basic.get("marketStatus"),
    }


def zone_of(price, premium, cfg):
    flags = []
    if price is None:
        return "DATA_FAIL", flags
    if price <= cfg["buy2"]:
        flags.append("BUY2")
    elif price <= cfg["accel"]:
        flags.append("ACCEL")
    elif price <= cfg["buy1"]:
        flags.append("BUY1")
    if premium is not None and abs(premium) >= cfg["premium_abs"]:
        flags.append("PREM")
    if "BUY2" in flags:
        return "BUY2", flags
    if "ACCEL" in flags:
        return "ACCEL", flags
    if "BUY1" in flags:
        return "BUY1", flags
    if "PREM" in flags:
        return "PREM", flags
    return "NONE", flags


def write_state(payload):
    STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    today = now.date().isoformat()
    prev = {"last_sent_date": None, "last_zone": None, "last_status": None}
    if STATE_PATH.exists():
        try:
            prev.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass

    try:
        q = quote(cfg["code"])
    except Exception as exc:
        print(f"quote failed: {exc}", file=sys.stderr)
        q = None

    if q is None or q.get("price") is None:
        title = "[0090B0] 시세 조회 실패 | 매수 보류"
        lines = [
            f"{stamp} | 0090B0 | {cfg['display']}",
            "이게 뭐 알림: 방산 소부장 ETF 장마감 가격 구간 점검입니다.",
            "지금 상태: 시세 조회 실패. 조건 미충족이 아닙니다.",
            "지금 할 일: 네이버에서 0090B0 현재가와 괴리율을 직접 확인하세요.",
            "나중에: 다음 평일 15:40 KST에 다시 점검합니다. 종료일 없습니다.",
            "익절/줄일 때: 가격을 못 읽으면 익절 판단도 하지 않습니다.",
            "하지 말 것: 실패 알림을 매수 신호로 보지 마세요. 신용, 미수, 일시 추격 금지.",
        ]
        msg = "\n".join(lines)
        print(msg)
        if prev.get("last_sent_date") == today and prev.get("last_zone") == "DATA_FAIL":
            print("data fail already sent today; silent")
            write_state({**prev, "updated_at": stamp, "last_status": "data_fail_deduped"})
            return 0
        publish("cwally-port", title, msg, priority=5, tags=["warning"])
        write_state({"updated_at": stamp, "last_sent_date": today, "last_zone": "DATA_FAIL", "last_status": "data_fail"})
        return 0

    zone, flags = zone_of(q["price"], q["premium"], cfg)
    px = f"{q['price']:,.0f}원"
    nav = f"{q['nav']:,.0f}원" if q.get("nav") is not None else "없음"
    prem = f"{q['premium']:+.2f}%" if q.get("premium") is not None else "없음"
    chg = f"{q['change_pct']:+.2f}%" if q.get("change_pct") is not None else "없음"
    vol = f"{q['volume']:,.0f}주" if q.get("volume") is not None else "없음"

    if zone == "NONE":
        print(f"{stamp} condition not met; price {px} premium {prem}; silent")
        write_state({"updated_at": stamp, "last_sent_date": prev.get("last_sent_date"), "last_zone": prev.get("last_zone"), "last_status": "no_hit", "price": q["price"], "premium": q.get("premium")})
        return 0

    if prev.get("last_sent_date") == today and prev.get("last_zone") == zone:
        print(f"same zone {zone} already sent today; silent")
        write_state({"updated_at": stamp, "last_sent_date": today, "last_zone": zone, "last_status": "deduped", "price": q["price"], "premium": q.get("premium")})
        return 0

    actions = {
        "BUY2": ("[0090B0] 2차 구간 | 6500 분할", "6,500원 이하입니다. 2차 분할만 하세요. 위성 비중 상한 2~3%를 넘기지 마세요.", 4),
        "ACCEL": ("[0090B0] 7천 이하 | 분할 가속", "7,000원 이하입니다. 거래량이 유지되면 분할 속도만 올리세요. 한 번에 채우지 마세요.", 4),
        "BUY1": ("[0090B0] 1차 구간 | 분할만", "7,400원 이하 1차 구간입니다. 1차 분할만 하고 나머지는 6,500원 또는 수주 확인 뒤로 미루세요.", 3),
        "PREM": ("[0090B0] 괴리 확대 | 추격 금지", "괴리율 절대값이 1.5% 이상입니다. 가격이 구간이어도 추격하지 말고 다음 종가를 보세요.", 4),
    }
    title, do_now, priority = actions[zone]
    if "PREM" in flags and zone != "PREM":
        do_now += " 괴리율도 1.5%를 넘었습니다. 체결 전 괴리를 다시 보세요."
    lines = [
        f"{stamp} | 0090B0 | {cfg['display']}",
        "이게 뭐 알림: 방산 소부장 ETF 장마감 가격 구간 점검입니다.",
        f"지금 상태: {px} ({chg}) / NAV {nav} / 괴리 {prem} / 거래량 {vol} / 순자산 {q.get('aum') or '없음'}.",
        f"지금 할 일: {do_now}",
        "나중에: 다음 평일 15:40 KST에 다시 점검합니다. 종료일 없습니다.",
        "익절/줄일 때: 이 알림은 매수 구간만 봅니다. 고점 대비 반등만으로 익절하지 마세요.",
        "하지 말 것: 일시 추격, 신용, 미수, 코어 비중화 금지. 체계업체 ETF와 역할을 섞지 마세요.",
    ]
    msg = "\n".join(lines)
    print(msg)
    publish("cwally-port", title, msg, priority=priority, tags=["chart_with_downwards_trend"])
    write_state({"updated_at": stamp, "last_sent_date": today, "last_zone": zone, "last_status": "sent", "price": q["price"], "premium": q.get("premium"), "flags": flags})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
