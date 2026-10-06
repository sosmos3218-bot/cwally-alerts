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
CFG_PATH = ROOT / "configs" / "kship.json"
STATE_PATH = ROOT / "state" / "kship.json"

sys.path.insert(0, str(ROOT / "scripts"))
from ntfy_util import publish  # noqa: E402


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
        print(f"price fail {ticker}: {exc}", file=sys.stderr)
        return None


def last_weekday_of_month(now: datetime) -> bool:
    d = now.date()
    if d.weekday() >= 5:
        return False
    nxt = d + timedelta(days=1)
    while nxt.month == d.month:
        if nxt.weekday() < 5:
            return False
        nxt += timedelta(days=1)
    return True


def fmt(px) -> str:
    return f"{px:,.0f}원" if px is not None else "시세 없음"


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    month_end = last_weekday_of_month(now)
    force = mode in {"force", "manual", "dispatch"}

    prev = {"updated_at": None, "last_alert_date": None, "last_mode": None}
    if STATE_PATH.exists():
        try:
            prev.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass

    send = force or month_end
    if send and not force and prev.get("last_alert_date") == now.date().isoformat():
        print("kship already sent today; silent")
        return 0
    if not send:
        print(f"kship skip; not month-end weekday ({now.date()})")
        STATE_PATH.write_text(
            json.dumps(
                {
                    "updated_at": stamp,
                    "last_alert_date": prev.get("last_alert_date"),
                    "last_mode": mode,
                    "month_end": month_end,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return 0

    w = cfg["weights"]
    z = cfg["buy_zones"]
    prices = {k: yahoo_price(v["ticker"]) for k, v in w.items()}
    shi, etf, hold = prices["shi"], prices["etf"], prices["holdco"]

    shi_chase = shi is not None and shi > z["shi"]["chase"]
    etf_chase = etf is not None and etf > z["etf"]["chase"]
    shi_t2 = shi is not None and z["shi"]["t2"][0] <= shi <= z["shi"]["t2"][1]
    etf_t2 = etf is not None and z["etf"]["t2"][0] <= etf <= z["etf"]["t2"][1]
    hold_t2 = hold is not None and z["holdco"]["t2"][0] <= hold <= z["holdco"]["t2"][1]
    shi_trim = shi is not None and shi >= z["shi"]["trim"]
    etf_trim = etf is not None and etf >= z["etf"]["trim"]

    if shi_trim or etf_trim:
        title = "[KSHIP] 익절 구간 | 각 1/3 축소"
        do_now = "삼성중 3만원 또는 ETF 1.1만원이면 각 1/3만 줄이세요. 65:35 비율은 유지합니다."
    elif shi_chase or etf_chase:
        title = "[KSHIP] 추격 금지 | 3차 보류"
        do_now = "삼성중 2.4만원 위 또는 ETF 9,500원 위입니다. 빈 이벤트를 채우지 말고 3차는 남김니다."
    elif shi_t2 or etf_t2 or hold_t2:
        title = "[KSHIP] 2차 구간 | 25% 분할"
        do_now = "2차 25%를 ETF 50 / 지주 15 / 삼성중 35로 분할합니다. 30만원 미만 교체는 쉽니다."
    else:
        title = "[KSHIP] 65:35 유지 | 추가매수 보류"
        do_now = "현 구간은 1차 또는 대기입니다. 목표 비중 ETF 50 / 지주 15 / 삼성중 35을 유지합니다."

    lines = [
        f"{stamp} | KSHIP | 조선 65:35",
        "이게 뭐 알림: 조선ETF·한국조선해양·삼성중공업 월말 가격·구간 알림입니다.",
        (
            f"지금 상태: 삼성중 {fmt(shi)} / ETF {fmt(etf)} / 지주 {fmt(hold)}. "
            "목표 베타 65(조선ETF 50+지주 15), 이벤트 35(삼성중). "
            "유지밴드 이벤트 30~40%. 최소 교체 30만원."
        ),
        f"지금 할 일: {do_now}",
        (
            "나중에: 3차 15%는 FLNG 본계약(약 2조 이상) 또는 영업이익률 두 자릿수 확인 뒤. "
            "MOU만 있으면 비중 그대로. 해양 연간 목표 70% 미달이면 이벤트 20."
        ),
        (
            "익절/줄일 때: 삼성중 3만원·ETF 1.1만원에서 각 1/3. "
            "FLNG 본계약 후 이벤트 50. 2건째부터 2.4~2.8만원에서 익절. "
            "영업이익률 한 자릿수·신조선가 3개월 연속 하락이면 이벤트 25."
        ),
        (
            "하지 말 것: 월중 비중 급격 변경. 삼성중 2.4만원·ETF 9,500원 위 추격. "
            "사이클 깨지고 삼성중만 남기기. 신용/미수."
        ),
    ]
    msg = "\n".join(lines)
    print(msg)
    publish("cwally-port", title, msg, priority=4, tags=["ship"])
    STATE_PATH.write_text(
        json.dumps(
            {
                "updated_at": stamp,
                "last_alert_date": now.date().isoformat(),
                "last_mode": mode,
                "month_end": month_end,
                "prices": prices,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
