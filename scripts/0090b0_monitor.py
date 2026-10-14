#!/usr/bin/env python3
"""0090B0 PLUS K방산소부장 장마감 가격 구간 점검.

구간: 7,400 이하 1차 / 7,000 이하 가속 / 6,500 이하 2차, 괴리 절대값 1.5% 이상은 추격 금지.
가속은 두 조건을 모두 통과할 때만 낸다.
  1) KOSPI 게이트: 종가가 kospi_gate.off_below 미만이면 보류, on_above 이상이면 허용,
     그 사이는 직전 상태 유지(최근 종가를 순서대로 따라가며 판정, 상태 파일에 의존하지 않음).
  2) 거래량: 당일 거래량이 직전 volume_avg_days 거래일 평균의 volume_ratio_min 배 이상.
알림은 구간(가속 보류/거래량 미달/괴리 플래그 포함)이 바뀔 때만 보낸다.
휴장일이거나 시세 날짜가 오늘 세션이 아니면 보내지 않는다.
모드: auto(기본) / dry(발송과 state 쓰기 없이 출력만).
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "0090b0.json"
STATE_PATH = ROOT / "state" / "0090b0.json"

sys.path.insert(0, str(ROOT / "scripts"))
from kr_calendar import is_trading_day, session_day  # noqa: E402
from ntfy_util import publish  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


def fetch_json(url: str):
    req = urllib.request.Request(url, headers=UA)
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


def signed_premium(indicator: dict, price, nav):
    """네이버 deviationRate는 부호 없는 값이고 부호는 deviationSign에 따로 온다."""
    rate = parse_num(indicator.get("deviationRate"))
    if rate is not None:
        sign = str(indicator.get("deviationSign") or "").strip()
        return -abs(rate) if sign == "-" else abs(rate)
    if price and nav:
        return (price - nav) / nav * 100.0
    return None


def daily_rows(url: str):
    rows = []
    for r in fetch_json(url) or []:
        d = str(r.get("localTradedAt") or "")[:10]
        close = parse_num(r.get("closePrice"))
        if not d or close is None:
            continue
        rows.append({"date": d, "close": close, "volume": parse_num(r.get("accumulatedTradingVolume"))})
    rows.sort(key=lambda x: x["date"])
    return rows


def fetch_market(code: str):
    basic = fetch_json(f"https://m.stock.naver.com/api/stock/{code}/basic")
    integ = fetch_json(f"https://m.stock.naver.com/api/stock/{code}/integration")
    indicator = integ.get("etfKeyIndicator") or {}
    price = parse_num(basic.get("closePrice"))
    nav = parse_num(indicator.get("nav"))
    volume = None
    for info in integ.get("totalInfos") or []:
        if info.get("code") == "accumulatedTradingVolume":
            volume = parse_num(info.get("value"))
    etf = {
        "date": str(basic.get("localTradedAt") or "")[:10],
        "price": price,
        "change_pct": parse_num(basic.get("fluctuationsRatio")),
        "nav": nav,
        "premium": signed_premium(indicator, price, nav),
        "aum": indicator.get("totalNav"),
        "volume": volume,
        "status": basic.get("marketStatus"),
        "rows": daily_rows(f"https://m.stock.naver.com/api/stock/{code}/price?pageSize=20&page=1"),
    }
    kospi_rows = daily_rows("https://m.stock.naver.com/api/index/KOSPI/price?pageSize=30&page=1")
    return etf, kospi_rows


def kospi_gate(rows, session: str, cfg: dict):
    """최근 종가를 오래된 순으로 따라가며 히스테리시스 판정. 오늘 종가가 없으면 UNKNOWN."""
    g = cfg["kospi_gate"]
    state = g.get("initial", "ON")
    hist = [r for r in rows if r["date"] <= session]
    for r in hist:
        if r["close"] < g["off_below"]:
            state = "OFF"
        elif r["close"] >= g["on_above"]:
            state = "ON"
    today = hist[-1] if hist and hist[-1]["date"] == session else None
    if today is None:
        return "UNKNOWN", None
    return state, today["close"]


def volume_check(etf: dict, session: str, cfg: dict):
    n = int(cfg.get("volume_avg_days", 5))
    rows = etf.get("rows") or []
    prev = [r["volume"] for r in rows if r["date"] < session and r["volume"] is not None][-n:]
    today_vol = etf.get("volume")
    if today_vol is None:
        today_vol = next((r["volume"] for r in rows if r["date"] == session), None)
    if today_vol is None or len(prev) < n:
        return today_vol, None, None, False
    avg = sum(prev) / len(prev)
    ratio = today_vol / avg if avg else None
    ok = ratio is not None and ratio >= float(cfg.get("volume_ratio_min", 1.0))
    return today_vol, avg, ratio, ok


def price_zone(price, cfg):
    if price <= cfg["buy2"]:
        return "BUY2"
    if price <= cfg["accel"]:
        return "ACCEL"
    if price <= cfg["buy1"]:
        return "BUY1"
    return "NONE"


def evaluate(cfg: dict, prev: dict, etf: dict, kospi_rows, now: datetime):
    """순수 판정. (publish 인자 또는 None, 새 state, 로그 문자열) 을 돌려준다."""
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    sess = session_day(now)
    session = sess.isoformat()
    base_state = {k: prev.get(k) for k in ("last_zone", "last_sent_date", "last_fail_date")}

    if not is_trading_day(sess):
        return None, {**base_state, "updated_at": stamp, "last_status": "holiday"}, f"{session} KRX 휴장; silent"
    if etf.get("price") is None:
        return "DATA_FAIL", {**base_state, "updated_at": stamp}, "quote missing"
    if etf.get("date") != session:
        return None, {**base_state, "updated_at": stamp, "last_status": "stale_quote"}, (
            f"시세 날짜 {etf.get('date')} != 세션 {session}; silent")

    price = etf["price"]
    zone = price_zone(price, cfg)
    prem = etf.get("premium")
    prem_flag = prem is not None and abs(prem) >= cfg["premium_abs"]
    gate, kclose = kospi_gate(kospi_rows, session, cfg)
    vol, vavg, vratio, vol_ok = volume_check(etf, session, cfg)
    g = cfg["kospi_gate"]

    if zone == "ACCEL":
        if gate != "ON":
            key = "ACCEL_HOLD"
        elif not vol_ok:
            key = "ACCEL_WAIT"
        else:
            key = "ACCEL"
    elif zone == "NONE":
        key = "PREM" if prem_flag else "NONE"
    else:
        key = zone
    if prem_flag and key not in {"PREM", "NONE"}:
        key += "+PREM"

    snap = {
        "updated_at": stamp, "session": session, "price": price, "premium": prem,
        "kospi_close": kclose, "kospi_gate": gate, "volume": vol,
        "volume_avg": round(vavg) if vavg else None, "volume_ratio": round(vratio, 2) if vratio else None,
    }
    if key == "NONE":
        return None, {**base_state, **snap, "last_zone": "NONE", "last_status": "no_hit"}, f"{session} 구간 밖; silent"
    if prev.get("last_zone") == key:
        return None, {**base_state, **snap, "last_zone": key, "last_status": "same_zone"}, f"{session} 같은 구간 {key}; silent"

    off, on = f"{g['off_below']:,.0f}", f"{g['on_above']:,.0f}"
    ktxt = f"{kclose:,.2f}" if kclose is not None else "미확인"
    gate_txt = {"ON": "가속 허용", "OFF": f"{off} 아래 이력, 가속 보류", "UNKNOWN": "오늘 종가 미확인, 가속 보류"}[gate]
    ratio_txt = f"{vratio:.2f}배" if vratio is not None else "미확인"
    cfg_n = int(cfg.get("volume_avg_days", 5))

    texts = {
        "BUY2": ("[0090B0] 2차 구간 | 6500 분할",
                 "6,500원 이하입니다. 2차 분할만 하세요. 위성 비중 상한 2~3%를 넘기지 마세요.", 4),
        "ACCEL": ("[0090B0] 7천 이하 | 분할 가속",
                  f"7,000원 이하이고 KOSPI 조건 통과, 거래량 {cfg_n}일 평균의 {ratio_txt}입니다. 분할 속도만 올리세요. 한 번에 채우지 마세요.", 4),
        "ACCEL_HOLD": ("[0090B0] 7천 이하 | 가속 보류, 분할 유지",
                       f"7,000원 이하지만 KOSPI가 {off} 아래로 내려간 뒤 {on}을 회복하지 못했습니다. 기존 속도로 1회분만 나누어 사고 속도는 올리지 마세요.", 4),
        "ACCEL_WAIT": ("[0090B0] 7천 이하 | 거래량 미달, 속도 유지",
                       f"7,000원 이하이고 KOSPI 조건은 통과했지만 거래량이 {cfg_n}일 평균의 {ratio_txt}로 기준 미달입니다. 속도는 그대로 두세요.", 3),
        "BUY1": ("[0090B0] 1차 구간 | 분할만",
                 "7,400원 이하 1차 구간입니다. 1차 분할만 하고 나머지는 6,500원 또는 수주 확인 뒤로 미루세요.", 3),
        "PREM": ("[0090B0] 괴리 확대 | 추격 금지",
                 "괴리율 절대값이 1.5% 이상입니다. 가격이 구간이어도 추격하지 말고 다음 종가를 보세요.", 4),
    }
    base_key = key.replace("+PREM", "")
    title, do_now, priority = texts[base_key]
    if base_key == "BUY2" and gate != "ON":
        do_now += f" KOSPI가 {off} 아래 국면이라 2차도 한 번에 채우지 말고 나누세요."
    if key.endswith("+PREM"):
        do_now += " 괴리율도 1.5%를 넘었습니다. 체결 전 괴리를 다시 보세요."

    px = f"{price:,.0f}원"
    chg = f"{etf['change_pct']:+.2f}%" if etf.get("change_pct") is not None else "없음"
    nav = f"{etf['nav']:,.0f}원" if etf.get("nav") is not None else "없음"
    ptxt = f"{prem:+.2f}%" if prem is not None else "없음"
    vtxt = f"{vol:,.0f}주" if vol is not None else "없음"
    atxt = f"{vavg:,.0f}주" if vavg else "미확인"
    lines = [
        f"{stamp} | 0090B0 | {cfg['display']}",
        "이게 뭐 알림: 방산 소부장 ETF 장마감 가격 구간 점검입니다. 구간이나 조건이 바뀔 때만 옵니다.",
        f"지금 상태: {px} ({chg}) / NAV {nav} / 괴리 {ptxt} / 거래량 {vtxt} ({cfg_n}일 평균 {atxt}의 {ratio_txt}) / 순자산 {etf.get('aum') or '없음'} / KOSPI {ktxt} ({gate_txt}).",
        f"지금 할 일: {do_now}",
        f"나중에: 평일 15:40 KST에 점검하고 구간, KOSPI 조건, 거래량 조건이 바뀔 때만 다시 알립니다. 가속은 KOSPI 종가 {on} 이상 회복 후에만 다시 허용합니다.",
        "익절/줄일 때: 이 알림은 매수 구간만 봅니다. 고점 대비 반등만으로 익절하지 마세요.",
        "하지 말 것: 일시 추격, 신용, 미수, 코어 비중화 금지. 체계업체 ETF와 역할을 섞지 마세요.",
    ]
    msg = "\n".join(lines)
    new_state = {**base_state, **snap, "last_zone": key, "last_sent_date": session, "last_status": "sent"}
    return {"title": title, "message": msg, "priority": priority}, new_state, f"{session} 구간 {key} 발송"


def data_fail_notice(cfg, stamp):
    title = "[0090B0] 시세 조회 실패 | 매수 보류"
    lines = [
        f"{stamp} | 0090B0 | {cfg['display']}",
        "이게 뭐 알림: 방산 소부장 ETF 장마감 가격 구간 점검입니다.",
        "지금 상태: 시세 조회 실패. 조건 미충족이 아닙니다.",
        "지금 할 일: 네이버에서 0090B0 현재가, 괴리율, KOSPI 종가를 직접 확인하세요.",
        "나중에: 다음 평일 15:40 KST에 다시 점검합니다.",
        "익절/줄일 때: 가격을 못 읽으면 익절 판단도 하지 않습니다.",
        "하지 말 것: 실패 알림을 매수 신호로 보지 마세요. 신용, 미수, 일시 추격 금지.",
    ]
    return title, "\n".join(lines)


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "auto"
    dry = mode == "dry"
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    prev = {}
    if STATE_PATH.exists():
        try:
            prev = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            prev = {}

    def save(state):
        if dry:
            print("DRY RUN: state not written ->", json.dumps(state, ensure_ascii=False))
            return
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if not is_trading_day(session_day(now)):
        print(f"{session_day(now)} KRX 휴장; silent")
        return 0

    try:
        etf, kospi_rows = fetch_market(cfg["code"])
    except Exception as exc:
        print(f"quote failed: {exc}", file=sys.stderr)
        etf, kospi_rows = {"price": None}, []

    out, state, log = evaluate(cfg, prev, etf, kospi_rows, now)
    print(log)
    if out == "DATA_FAIL":
        today = session_day(now).isoformat()
        title, msg = data_fail_notice(cfg, stamp)
        print(msg)
        if prev.get("last_fail_date") == today:
            print("data fail already sent today; silent")
            return 0
        if dry:
            print("DRY RUN: would publish", title)
        else:
            publish("cwally-port", title, msg, priority=5, tags=["warning"])
        save({**{k: prev.get(k) for k in ("last_zone", "last_sent_date")}, "updated_at": stamp,
              "last_fail_date": today, "last_status": "data_fail"})
        return 0
    if out:
        print(out["title"])
        print(out["message"])
        if dry:
            print("DRY RUN: would publish", out["title"])
        else:
            publish("cwally-port", out["title"], out["message"], priority=out["priority"],
                    tags=["chart_with_downwards_trend"])
    save(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
