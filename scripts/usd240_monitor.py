#!/usr/bin/env python3
"""261240 close rule. October USD futures close vs 1330 / 1350. 2026 only."""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_calendar import is_trading_day, session_day  # noqa: E402
from ntfy_util import publish  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG = ROOT / "configs" / "usd240.json"
STATE = ROOT / "state" / "usd240.json"
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


def load_cfg():
    return json.loads(CFG.read_text(encoding="utf-8"))


def load_state():
    st = {"sent": {}, "fail_notified": {}}
    if STATE.exists():
        try:
            st.update(json.loads(STATE.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    st.setdefault("sent", {})
    st.setdefault("fail_notified", {})
    return st


def save_state(st, now):
    st["updated_at"] = now.isoformat(timespec="seconds")
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch_oct_close():
    """October USD futures close. Spot is not accepted."""
    url = "https://scanner.tradingview.com/symbol?symbol=KRX:USDV2026&fields=close,description,expiration&no_404=true"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        data = json.loads(resp.read().decode())
    price = data.get("close")
    desc = str(data.get("description") or "")
    exp = str(data.get("expiration") or "")
    if price is None:
        raise ValueError("no close")
    if exp != "20261019" or "Oct 2026" not in desc:
        raise ValueError(f"not October 2026 contract: {desc} {exp}")
    px = float(price)
    if not 1000 < px < 2000:
        raise ValueError(f"close out of range: {px}")
    return {
        "price": px,
        "prev": None,
        "source": "TradingView KRX:USDV2026 close",
        "futures": True,
        "description": desc,
        "expiration": exp,
    }


def judge(price, cfg, day):
    stop = float(cfg["stop_close"])
    chase = float(cfg["chase_close"])
    last = day.isoformat() == cfg["expire_date"]
    if price <= stop:
        return "stop", "손절", "10월물 종가가 1,330원 이하다. 261240을 종가로 정리한다."
    if price > chase:
        return "chase", "추격 금지", "10월물 종가가 1,350원을 넘었다. 새로 사지 않는다."
    if last:
        return "flat", "미도달 청산", "기한 종가인데 1,330원과 1,370원 사이다. 10월 13일 규칙대로 정리한다."
    return "hold", "진입 유지", "10월물 종가가 1,330원 초과 1,350원 이하다. 기존 진입 구간을 유지한다."


def body(kind, price, cfg, day, note):
    last = "10월 13일 종가가 마지막 점검이다." if day.isoformat() == cfg["expire_date"] else "다음 개장일 16:05 KST에 다시 본다."
    return "\n".join([
        "이게 뭐 알림: 261240 종가 규칙 점검. 10월물 1,330원 이하면 손절, 1,350원 초과면 추격 금지, 그 사이면 진입 유지.",
        f"지금 결과/상태: 기준일 {day.isoformat()}, 조회값 {price:,.1f}원. 판정 {note}.",
        f"지금 할 일: {note}",
        f"나중에: {last} 기한은 2026-10-13이다.",
        "익절/줄일 때: 종가 1,370원이면 목표 정리. 종가 1,330원 이하면 손절.",
        "하지 말 것: 장중 1,330원 터치로 팔지 않는다. 261250으로 바꾸지 않는다. 10월 13일 넘기지 않는다.",
        f"출처: {kind}. 투자 권유 아님.",
    ])


def main(argv):
    dry = "plan" in argv or "--dry" in argv
    cfg = load_cfg()
    now = datetime.now(KST)
    if now.year != int(cfg["year_guard"]):
        print(f"year guard: silent {now.year}")
        return 0
    day = session_day(now, rollover_hour=6)
    allowed = {item[:10] for item in cfg["oneshot_kst"]}
    if day.isoformat() not in allowed or not is_trading_day(day):
        print(f"skip: {day.isoformat()} not an open check day")
        return 0
    st = load_state()
    if st["sent"].get(day.isoformat()) and "force" not in argv:
        print(f"skip: already sent {day.isoformat()}")
        return 0
    try:
        quote = fetch_oct_close()
    except Exception as exc:
        title = "[USD240] 10월물 조회 실패"
        msg = "\n".join([
            "이게 뭐 알림: 261240 종가 규칙을 판정하지 못했다.",
            f"지금 결과/상태: {day.isoformat()} 10월물 종가 조회 실패 ({type(exc).__name__}).",
            "지금 할 일: 매매 신호로 보지 않는다.",
            "나중에: 다음 개장일 16:05 KST에 다시 본다.",
            "익절/줄일 때: 숫자 확인 전에는 손절/익절을 실행하지 않는다.",
            "하지 말 것: 조회 실패를 손절이나 추격 금지로 해석하지 않는다.",
        ])
        print(title)
        print(msg)
        if dry:
            return 0
        if st["fail_notified"].get(day.isoformat()):
            return 0
        publish("cwally-ops", title, msg, priority=4, tags=["warning"])
        st["fail_notified"][day.isoformat()] = now.isoformat(timespec="seconds")
        save_state(st, now)
        return 0
    if not quote["futures"]:
        title = "[USD240] 10월물 조회 실패"
        msg = "\n".join([
            "이게 뭐 알림: 261240 종가 규칙을 판정하지 못했다.",
            f"지금 결과/상태: 받은 값은 {quote['source']} {quote['price']:,.1f}원이다. 10월물 종가가 아니다.",
            "지금 할 일: 매매 신호로 보지 않는다.",
            "나중에: 다음 개장일 16:05 KST에 다시 본다.",
            "익절/줄일 때: 선물 종가 확인 전에는 손절/익절을 실행하지 않는다.",
            "하지 말 것: 현물 가격으로 1,330원 손절이나 1,350원 추격 금지를 확정하지 않는다.",
        ])
        print(title)
        print(msg)
        if dry:
            return 0
        if st["fail_notified"].get(day.isoformat()):
            return 0
        publish("cwally-ops", title, msg, priority=4, tags=["warning"])
        st["fail_notified"][day.isoformat()] = now.isoformat(timespec="seconds")
        save_state(st, now)
        return 0
    code, action, note = judge(quote["price"], cfg, day)
    title = f"[USD240] {action}"
    msg = body(quote["source"], quote["price"], cfg, day, note)
    print(title)
    print(msg)
    if dry:
        return 0
    publish("cwally-port", title, msg, priority=4 if code == "stop" else 3, tags=["chart_with_upwards_trend"])
    st["sent"][day.isoformat()] = {"action": action, "price": quote["price"], "at": now.isoformat(timespec="seconds")}
    save_state(st, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
