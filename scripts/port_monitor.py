#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "port.json"
STATE_PATH = ROOT / "state" / "port.json"

BASE = {
    "005930": 28,
    "000660": 22,
    "329180": 12,
    "012450": 9,
    "105560": 15,
    "000810": 8,
    "005380": 6,
}
NAMES = {
    "005930": "삼성전자",
    "000660": "하이닉스",
    "329180": "HD현대중",
    "012450": "한화에어로",
    "105560": "KB금융",
    "000810": "삼성화재",
    "005380": "현대차",
}


def yahoo(ticker: str):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(ticker)
        + "?interval=1d&range=10d"
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


def naver_yield(code: str):
    url = (
        "https://finance.naver.com/marketindex/interestDailyQuote.naver?marketindexCd="
        + urllib.parse.quote(code)
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 cwally-alerts"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("euc-kr", errors="ignore")
        nums = re.findall(r"class=\"number\">([0-9]+\.[0-9]+)", html)
        return float(nums[0]) if nums else None
    except Exception as exc:
        print(f"naver fail {code}: {exc}", file=sys.stderr)
        return None


def streak(hist, key, n, pred) -> bool:
    vals = [h.get(key) for h in hist[-n:] if h.get(key) is not None]
    return len(vals) >= n and all(pred(v) for v in vals[-n:])


def fmt(v, nd=1):
    return f"{v:,.{nd}f}" if v is not None else "조회 실패"


def wtxt(w):
    return " / ".join(f"{NAMES[k]} {w[k]}" for k in BASE)


def ntfy_publish(server, topic, title, message, priority, tags):
    payload = json.dumps(
        {
            "topic": topic,
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


def apply_rules(hist, fx, wti, ktb3, ktb10):
    w = dict(BASE)
    eq, cash, front, ten = 55, 10, 25, 10
    reasons = []
    halt = False

    fx_strong = streak(hist, "fx", 2, lambda x: x <= 1340)
    fx_410 = streak(hist, "fx", 2, lambda x: x >= 1410)
    fx_430 = streak(hist, "fx", 2, lambda x: x >= 1430)
    wti90 = streak(hist, "wti", 3, lambda x: x <= 90)
    wti110 = streak(hist, "wti", 3, lambda x: x >= 110)
    wti115 = streak(hist, "wti", 3, lambda x: x >= 115)
    wti120 = streak(hist, "wti", 3, lambda x: x >= 120)
    k3_low = ktb3 is not None and ktb3 <= 3.85
    k3_420 = ktb3 is not None and ktb3 >= 4.20
    k3_430 = ktb3 is not None and ktb3 >= 4.30
    k10_high = ktb10 is not None and ktb10 >= 4.75
    k10_low = ktb10 is not None and ktb10 <= 4.40

    hits = sum([fx_strong, fx_410, wti110, k3_low, k3_430])

    if wti120:
        halt = True
        eq, cash = 40, 25
        w.update({"000660": 18, "005380": 4, "012450": 9})
        reasons.append("WTI 120. 시나리오 중지. 주식 40, 현금 25, 1~2년물만.")
    elif hits >= 2:
        eq, cash = 45, 20
        reasons.append("조건 2개 동시. 주식 45, 현금 20. 종목보다 현금 먼저.")
    elif wti115:
        eq, cash = 45, 15
        w.update({"005380": 4, "012450": 7, "000660": 19})
        reasons.append("WTI 115. 주식 45. 현대차 4, 에어로 7, 닉스 19. 10년 금지.")
    elif wti110:
        cash = 15
        w.update({"005380": 4, "000660": 19})
        reasons.append("WTI 110. 현금 15. 현대차 4, 닉스 19. 10년 신규 금지.")
    elif wti90:
        ten = 15
        reasons.append("WTI 90. 채권 안에서 10년만 +5. 화학주는 안 넣음.")

    if not halt:
        if fx_430:
            w.update({"105560": 10, "000810": 5, "005930": 28, "000660": 27, "005380": 8})
            reasons.append("원달러 1430. KB 10, 화재 5, 반도체 55, 현대차 8. 10년 금지.")
        elif fx_410:
            cash = max(cash, 15)
            eq = min(eq, 50)
            w.update({"005930": 24, "105560": 12})
            reasons.append("원달러 1410. 현금 15, 주식 50. 전자 24, KB 12.")
        elif fx_strong:
            w.update({"005380": 3, "329180": 9, "000660": 18})
            reasons.append("원달러 1340. 현대차 3, HD중 9, 닉스 18.")

        if k3_430:
            w.update({"005930": min(w["005930"], 25), "000660": min(w["000660"], 19), "005380": min(w["005380"], 4)})
            reasons.append("국고 3년 4.30. 전자 25, 닉스 19, 현대차 4. 3년 매수 유지, 10년 금지.")
        elif k3_420:
            reasons.append("국고 3년 4.20. 3년물만 분할 매수. 주식 유지.")
        elif k3_low:
            w.update({"105560": min(w["105560"], 12), "000810": min(w["000810"], 5)})
            reasons.append("국고 3년 3.85. 한은 인상 기대가 접혔다. KB 12, 화재 5.")

        if k10_high:
            ten = max(ten, 15)
            reasons.append("국고 10년 4.75. 채권 안에서 10년 15. 주식은 그대로.")
        elif k10_low:
            w["000810"] = min(w["000810"], 6)
            reasons.append("국고 10년 4.40. 10년 신규 금지. 화재 6.")

    if not reasons:
        reasons.append("밴드 안이니 오늘은 종목을 교체하지 말고 그대로 두세요.")
    if ktb3 is None and ktb10 is None:
        reasons.append("국고금리는 조회 실패라 오늘 금리 규칙은 적용하지 않습니다.")

    key = "HALT" if halt else ("DUAL" if hits >= 2 else "BASE")
    title = (
        "중지 | 현금부터"
        if halt
        else ("조건 2개 | 현금부터" if hits >= 2 else "유지 | 오늘 손대지 말 것")
    )
    return w, eq, cash, front, ten, reasons, key, title, halt


def event_blocks(today: str, cfg: dict) -> tuple[list[str], list[str], bool]:
    live = [e for e in cfg.get("events", []) if e["start"] <= today <= e["end"]]
    soon = [
        e
        for e in cfg.get("events", [])
        if today < e["start"] <= (datetime.strptime(today, "%Y-%m-%d") + timedelta(days=21)).strftime("%Y-%m-%d")
    ]
    live_lines: list[str] = []
    if live:
        live_lines.append("오늘은 일정 날입니다. 당일 주문은 금지하고, 결과 확인 다음날부터 조정하세요.")
        ids = {e["id"] for e in live}
        if "samsung_prelim" in ids:
            cons = cfg.get("samsung_op_consensus_trn", 114)
            live_lines.append(
                f"잠정 영업이익 기준 {cons}조. 플러마이너스는 ±5% 유지, +10%는 전자 32·닉스 24, -10%는 전자 22·닉스 18·주식 50."
            )
        if "samsung_confirm" in ids:
            live_lines.append("확정에서 HBM이 밀리면 전자 22·닉스 16. 가이던스가 내리고 재고가 늘면 반도체 35·KB 18·화재 10.")
        if "bok_oct" in ids:
            live_lines.append("금통위 동결+기존 문장이면 유지. 매파면 KB 18·화재 10·현대차 4. +25bp면 반도체 45·HD중 10, 당일 채권 금지.")
        if "bok_nov" in ids:
            live_lines.append("11월에 3.25%가 확인되면 화재 6, 채권 안에서 10년 15. 종목은 그대로.")
        if "fomc_oct" in ids:
            live_lines.append("10월 연준 회의는 점 그림이 없습니다. +25bp여도 12월 규칙을 앞당게 적용하지 말고, 현금만 15로 둘리세요.")
        if "fomc_dec" in ids:
            live_lines.append("12월 연준: 동결+점 그림 내림이면 10년 18. +25bp면 닉스 18·현대차 4·현금 15. +50bp면 주식 35·현금 30으로 중지.")
    soon_lines = []
    if soon:
        soon_lines.append("21일 안 일정: " + ", ".join(f"{e['label']} {e['start']}" for e in soon))
    return live_lines, soon_lines, bool(live)


def main() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    prev = {"zone": None, "last_alert_key": None, "history": []}
    if STATE_PATH.exists():
        try:
            prev.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass

    fx = yahoo(cfg["tickers"]["fx"])
    wti = yahoo(cfg["tickers"]["wti"])
    ktb3 = yahoo(cfg["tickers"]["ktb3_yahoo"]) or naver_yield(cfg["naver_yield"]["ktb3"])
    ktb10 = yahoo(cfg["tickers"]["ktb10_yahoo"]) or naver_yield(cfg["naver_yield"]["ktb10"])

    hist = [h for h in (prev.get("history") or []) if h.get("date") != today]
    hist.append({"date": today, "fx": fx, "wti": wti, "ktb3": ktb3, "ktb10": ktb10})
    hist = hist[-8:]

    w, eq, cash, front, ten, reasons, key, title, halt = apply_rules(hist, fx, wti, ktb3, ktb10)
    live_lines, soon_lines, is_live = event_blocks(today, cfg)
    if is_live:
        title = "일정 날 | 당일 금지"
        key = "EVENT:" + key

    STATE_PATH.write_text(
        json.dumps(
            {
                "updated_at": stamp,
                "zone": key,
                "last_alert_key": key,
                "fx": fx,
                "wti": wti,
                "ktb3": ktb3,
                "ktb10": ktb10,
                "history": hist,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    later = [
        "나중에: 빈자리는 현금으로 둘립니다. 새 종목은 넣지 말세요. 10월 22일 금통위, 10월 삼성 잠정·확정, 12월 9일 연준 회의만 시나리오를 바꿉니다.",
    ]
    later.extend(soon_lines)
    later.extend(live_lines)

    lines = [
        f"{stamp} | PORT 장전 체크",
        "이게 무슨 알림: 7종목 유니버스 장전 체크입니다. 기본은 주식 55, 채권 35, 현금 10입니다.",
        f"지금 결과/상태: 원달러 {fmt(fx, 1)}, WTI {fmt(wti, 2)}, 국고 3년 {fmt(ktb3, 3)}, 국고 10년 {fmt(ktb10, 3)}.",
        f"지금 할 일: 주식 {eq}, 채권 35(짧은 만기 {front} / 10년 {ten}), 현금 {cash}. 종목 {wtxt(w)}. "
        + " ".join(reasons),
        *later,
        "익절/줄일 때: 조건이 2개면 주식 45. WTI 120 또는 연준이 0.50%포인트를 올리면 중지합니다.",
        "하지 말 것: 추격 매수, 신용·미수, 7개 밖 종목, 당일 매매, 테마 반등으로 교체.",
    ]

    msg = "\n".join(lines)
    print(msg)
    ntfy_publish(
        "https://ntfy.sh",
        "cwally-port",
        f"[PORT] {title}",
        msg,
        5 if halt else (4 if is_live else 3),
        "chart_with_upwards_trend,kr",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
