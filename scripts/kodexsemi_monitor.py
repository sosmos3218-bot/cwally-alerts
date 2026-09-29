#!/usr/bin/env python3
from __future__ import annotations

import json
import statistics
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ntfy_util import publish

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "kodexsemi.json"
STATE_PATH = ROOT / "state" / "kodexsemi.json"
EVENT_ZONES = {"BREAKDOWN", "RETEST", "RETEST_THIN", "BREAKOUT", "DATA_FAIL"}
ZONE_KO = {
    "BREAKDOWN": "12.3만 이탈. 축소",
    "RETEST": "12.3만 재시험. 분할매수 검토",
    "RETEST_THIN": "12.3만 접근. 거래량 미확인",
    "BREAKOUT": "14.05만 안착. 분할매수 검토",
    "HOLD": "관망. 일괄 매수 아님",
    "DATA_FAIL": "시세 조회 실패",
}
DO_NOW = {
    "BREAKDOWN": "보유 축소를 검토하세요. 이 아래에서 추매 금지.",
    "RETEST": "분할 매수만 검토하세요. 일괄 추격 금지.",
    "RETEST_THIN": "가격만 12.3만대입니다. 거래량 확인 전에 사지 마세요.",
    "BREAKOUT": "14.05만 종가 안착입니다. 삼성·하이닉스 가이던스를 확인한 뒤 분할만.",
    "HOLD": "새로 사지 마세요. 보유는 유지.",
    "DATA_FAIL": "시세를 다시 확인하세요.",
}


def fetch_bar(ticker: str):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(ticker)
        + "?interval=1d&range=2mo"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 cwally-alerts"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode())
        result = payload["chart"]["result"][0]
        meta = result.get("meta", {})
        price = meta.get("regularMarketPrice") or meta.get("chartPreviousClose") or meta.get("previousClose")
        quote = (result.get("indicators") or {}).get("quote") or [{}]
        vols = [v for v in (quote[0].get("volume") or []) if v]
        vol = vols[-1] if vols else meta.get("regularMarketVolume")
        avg20 = statistics.mean(vols[-21:-1]) if len(vols) >= 21 else (statistics.mean(vols[:-1]) if len(vols) >= 6 else None)
        return {
            "price": float(price) if price is not None else None,
            "volume": float(vol) if vol is not None else None,
            "vol_avg20": float(avg20) if avg20 else None,
        }
    except Exception as exc:
        print(f"price fetch failed {ticker}: {exc}", file=sys.stderr)
        return {"price": None, "volume": None, "vol_avg20": None}


def zone(price, vol, avg20, cfg):
    if price is None:
        return "DATA_FAIL", None
    vol_ok = None
    if vol is not None and avg20:
        vol_ok = vol >= cfg["vol_mult"] * avg20
    if price < cfg["support"]:
        return "BREAKDOWN", vol_ok
    if price <= cfg["retest_hi"]:
        if vol_ok is False:
            return "RETEST_THIN", vol_ok
        return "RETEST", vol_ok
    if price >= cfg["breakout"]:
        return "BREAKOUT", vol_ok
    return "HOLD", vol_ok


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "event").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    if cfg.get("year_guard") and now.year != int(cfg["year_guard"]):
        print("kodexsemi year-guard silent")
        return 0
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    prev = {
        "zone": None,
        "price": None,
        "volume": None,
        "vol_avg20": None,
        "vol_ok": None,
        "last_sent_zone": None,
    }
    if STATE_PATH.exists():
        try:
            prev.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass

    bar = fetch_bar(cfg["ticker"])
    z, vol_ok = zone(bar["price"], bar["volume"], bar["vol_avg20"], cfg)
    if z == "DATA_FAIL" and prev.get("zone") and prev.get("zone") != "DATA_FAIL":
        z = prev["zone"]
        bar["price"] = prev.get("price")
        bar["volume"] = prev.get("volume")
        bar["vol_avg20"] = prev.get("vol_avg20")
        vol_ok = prev.get("vol_ok")
        fetch_failed = True
    else:
        fetch_failed = z == "DATA_FAIL"

    zone_changed = bool(prev.get("zone")) and prev.get("zone") != z
    send = False
    if mode in {"force", "weekly"}:
        send = True
    elif mode == "event":
        send = zone_changed and z in EVENT_ZONES
    else:
        send = zone_changed and z in EVENT_ZONES

    title = f"[PORT] 091160 {ZONE_KO.get(z, z)}"
    px = f"{bar['price']:,.0f}원" if bar["price"] is not None else "시세 없음"
    vx = f"{bar['volume']:,.0f}주" if bar["volume"] is not None else "N/A"
    ax = f"{bar['vol_avg20']:,.0f}주" if bar["vol_avg20"] is not None else "N/A"
    vok = "확인" if vol_ok is True else ("미달" if vol_ok is False else "미확인")
    lines = [
        f"{stamp} | PORT | {cfg['display']} {cfg['code']}",
        "이게 뭐 알림: KODEX 반도체 091160 변경 조건 감시입니다. 매수 신호가 아닙니다.",
        f"지금 상태: {px} | {ZONE_KO.get(z, z)} | 거래량 {vx} / 20일평 {ax} / 배증 {vok}",
        f"지금 할 일: {DO_NOW.get(z, '')}",
        "나중에: HBM·가이던스는 이 스크립트가 자동확인하지 못 합니다. 공시를 보고 결정하세요.",
        "익절/줄일 때: 종가 122,695원 이탈이면 축소. 188,760원 근처는 추격 금지.",
        "하지 말 것: 반등 날 일괄 매수, 레버리지 교체, 조건 미충 추격.",
    ]
    if fetch_failed:
        lines.append("조회: 오늘 시세 조회 실패. 이전 구간을 유지했습니다.")
    msg = "\n".join(lines)
    print(msg)

    STATE_PATH.write_text(
        json.dumps(
            {
                "updated_at": stamp,
                "zone": z,
                "price": bar["price"],
                "volume": bar["volume"],
                "vol_avg20": bar["vol_avg20"],
                "vol_ok": vol_ok,
                "last_sent_zone": z if send else prev.get("last_sent_zone"),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if send:
        pri = 5 if z in {"BREAKDOWN", "DATA_FAIL"} else (4 if z in {"RETEST", "BREAKOUT"} else 3)
        publish("cwally-port", title, msg, pri, ["chart_with_upwards_trend"])
    else:
        print("no kodexsemi alert; silent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
