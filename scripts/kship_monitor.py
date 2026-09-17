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

    if shi_chase or etf_chase:
        title = "[KSHIP] 월말 점검 | 3차 추격 금지"
        do_now = (
            "주식 평가액만으로 베타 65 이벤트 35를 계산하세요. "
            "이벤트가 30~40% 밖이면 교체하세요. "
            "삼성중 2.4만원 위·ETF 9,500원 위에서 빈 이벤트를 채우지 말세요."
        )
    elif shi_t2 or etf_t2 or hold_t2:
        title = "[KSHIP] 월말 점검 | 2차 구간 검토"
        do_now = (
            "2차 25%를 65:35로 넣을지 보세요. "
            "삼성중 1.84~1.95만, ETF 6,700~7,200, 지주 31.4~33만이 2차입니다. "
            "이벤트 30~40% 밖이면 교체하되, 30만원 미만 매매는 건너도 됩니다."
        )
    else:
        title = "[KSHIP] 월말 점검 | 65:35 유지"
        do_now = (
            "주식만 분모로 이벤트 30~40%(목표 35) 안인지 보세요. "
            "밖이면 ETF·지주↔삼성중으로 교체하세요. "
            "밴드 안이면 비중을 바꾸지 말고, FLNG 본계약만 기다리세요."
        )

    lines = [
        f"{stamp} | KSHIP | 조선 65:35 월말 점검표",
        "이게 무 알림: 조선 예산을 업황 베타 65(조선ETF 50+한국조선해양 15), 삼성중 이벤트 35로 나눐 할지 보는 알림입니다.",
        (
            f"지금 상태: 삼성중 {fmt(shi)} / ETF {fmt(etf)} / 지주 {fmt(hold)}. "
            f"목표 ETF50 지주15 삼성중35. 유지밴드 이벤트 30~40%. "
            f"최소 교체 30만원."
        ),
        f"지금 할 일: {do_now}",
        (
            "나중에: 3차 15%는 FLNG 본계약(약 2조 이상) 또는 영업이익률 두 자릿수 확인 뒤. "
            "MOU만 있으면 비중 그대로. 해양 연간 목표 70% 미달이 먼저 오면 이벤트 20으로."
        ),
        (
            "익절/줄일 때: 삼성중 3만·ETF 1.1만에서 각 1/3. "
            "FLNG 본계약 후 이벤트 50까지. 2건째부터는 2.4~2.8만에서 익절. "
            "오버라이드 한 자릿수·신조선가 3개월 연속 하락이면 이벤트 25."
        ),
        (
            "하지 말 것: 월중 즐격 바꾸기. 삼성중 2.4만·ETF 9,500 위 추격. "
            "사이클 깨지고 삼성중만 남기기. 신용/미수. 새 cron."
        ),
        (
            "빈칸: 이벤트% __ / ETF% __ / 지주% __ / 삼성중% __ | "
            "해양누적 __% | FLNG 본계약 Y/N | OPM 두자릿수 Y/N | 교체 Y/N"
        ),
    ]
    msg = "\n".join(lines)
    print(msg)
    publish("cwally-port", title, msg, priority=4, tags=["ship", "clipboard"])
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
