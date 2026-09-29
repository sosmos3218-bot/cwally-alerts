#!/usr/bin/env python3
"""091160 KODEX 반도체 변경 조건. kodexsemi 합병본.

122,695 이탈=축소, 122,695~125,000 재시험, 140,500 안착.
관망은 침묵. 조회 실패와 조건 미충을 구분한다.
"""
from __future__ import annotations

import json
import statistics
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "k091160.json"
STATE_PATH = ROOT / "state" / "k091160.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}
NAVER = "https://m.stock.naver.com/api/stock/"
P = "[091160]"
EVENT = {"BREAKDOWN", "RETEST", "RETEST_THIN", "BREAKOUT", "DATA_FAIL"}


def get_json(url: str):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def num(text):
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    raw = str(text).replace(",", "").replace("%", "").replace("+", "").strip()
    try:
        return float(raw)
    except ValueError:
        return None


def usable(date_str: str, now: datetime) -> bool:
    today = now.strftime("%Y-%m-%d")
    if date_str < today:
        return True
    return date_str == today and (now.hour, now.minute) >= (15, 20)


def bars(code: str, now: datetime):
    rows = get_json(f"{NAVER}{code}/price?pageSize=30&page=1")
    out = []
    for r in rows:
        d = r.get("localTradedAt")
        c = num(r.get("closePrice"))
        v = num(r.get("accumulatedTradingVolume") or r.get("volume") or r.get("accQuant"))
        if d and c and usable(d, now):
            out.append((d, c, v))
    return out


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


def copy_for(z, stamp, session, status, vol_line):
    bodies = {
        "BREAKDOWN": (
            f"{P} 12.3만 이탈 | 축소",
            "지금 할 일: 보유 축소를 검토하세요. 이 아래에서 추매 금지.",
            5,
        ),
        "RETEST": (
            f"{P} 12.3만 재시험 | 분할매수 검토",
            "지금 할 일: 분할 매수만 검토하세요. 일괄 추격 금지.",
            4,
        ),
        "RETEST_THIN": (
            f"{P} 12.3만 접근 | 거래량 미확인",
            "지금 할 일: 가격만 12.3만대입니다. 거래량 확인 전에 사지 마세요.",
            3,
        ),
        "BREAKOUT": (
            f"{P} 14.05만 안착 | 분할매수 검토",
            "지금 할 일: 종가 14.05만 안착입니다. 삼성·하이닉스 가이던스를 보고 분할만.",
            4,
        ),
        "HOLD": (
            f"{P} 관망 | 일괄 매수 아님",
            "지금 할 일: 새로 사지 마세요. 보유는 유지.",
            3,
        ),
        "DATA_FAIL": (
            f"{P} 조회실패 | 판정 보류",
            "지금 할 일: 조건 미충이 아닙니다. 시세를 다시 확인하세요.",
            4,
        ),
    }
    title, do_now, pri = bodies[z]
    msg = "\n".join(
        [
            f"{stamp} | 091160 | 세션 {session}",
            "이게 뭐 알림: KODEX 반도체 091160 변경 조건 감시입니다. 매수 신호가 아닙니다.",
            f"지금 결과/상태: {status}",
            do_now,
            f"거래량: {vol_line}",
            "나중에: HBM·가이던스는 자동확인하지 못 합니다. 공시를 보고 결정하세요.",
            "익절/줄일 때: 종가 122,695원 이탈이면 축소. 188,760원 근처는 추격 금지.",
            "하지 말 것: 반등 날 일괄 매수, 레버리지, 조건 미충 추격.",
        ]
    )
    return title, msg, pri


def publish(title: str, message: str, priority: int) -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    import ntfy_util

    ntfy_util.publish("cwally-port", title, message, priority=priority, tags=["chart_with_upwards_trend"])


def main() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    if cfg.get("year_guard") and now.year != int(cfg["year_guard"]):
        print("k091160 year-guard silent")
        return 0
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    prev = {
        "zone": None,
        "last_sent_zone": None,
        "fail_sent_date": None,
        "session_date": None,
    }
    if STATE_PATH.exists():
        try:
            prev.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass

    err = []
    rows = []
    try:
        rows = bars(cfg["code"], now)
    except Exception as exc:
        err.append(str(exc)[:120])

    session = rows[0][0] if rows else None
    close = rows[0][1] if rows else None
    vol = rows[0][2] if rows else None
    past = [r[2] for r in rows[1:21] if r[2]]
    avg20 = statistics.mean(past) if len(past) >= 5 else None
    if close is None:
        err.append("종가 없음")

    z, vol_ok = zone(close, vol, avg20, cfg)
    if err:
        z, vol_ok = "DATA_FAIL", None

    px = f"{close:,.0f}원" if close is not None else "시세 없음"
    vx = f"{vol:,.0f}주" if vol is not None else "N/A"
    ax = f"{avg20:,.0f}주" if avg20 else "N/A"
    vok = "확인" if vol_ok is True else ("미달" if vol_ok is False else "미확인")
    status = f"{px} | {z} | 거래량 {vx} / 20일평 {ax} / 배증 {vok}"
    if err:
        status += f" | 조회실패({'; '.join(err)})"
    vol_line = f"{vx} / 20일평 {ax} / 1.3배 {vok}"
    title, msg, pri = copy_for(z, stamp, session or "없음", status, vol_line)
    print(title)
    print(msg)

    send = False
    if z == "DATA_FAIL":
        send = prev.get("fail_sent_date") != (session or now.strftime("%Y-%m-%d"))
    elif z in EVENT:
        send = prev.get("zone") != z

    STATE_PATH.write_text(
        json.dumps(
            {
                "updated_at": stamp,
                "session_date": session,
                "zone": z,
                "last_sent_zone": z if send else prev.get("last_sent_zone"),
                "fail_sent_date": (session or now.strftime("%Y-%m-%d")) if z == "DATA_FAIL" else None,
                "close": close,
                "volume": vol,
                "vol_avg20": avg20,
                "vol_ok": vol_ok,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if send:
        publish(title, msg, pri)
    else:
        print("no k091160 alert; silent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
