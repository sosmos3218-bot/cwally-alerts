#!/usr/bin/env python3
"""SPCX price-support and Q3 checklist. One-shot dates only. Year-guarded."""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "spcxq3.json"
STATE_PATH = ROOT / "state" / "spcxq3.json"
UA = {"User-Agent": "cwally-alerts research@example.com"}
SLOTS = {
    "2026-10-10": "unlock1",
    "2026-10-27": "unlock2",
    "2026-11-04": "q3a",
    "2026-11-18": "q3b",
    "2026-12-09": "unlock180",
}


def load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def yahoo(ticker: str) -> dict:
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + ticker
        + "?interval=1d&range=1mo"
    )
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode())
    result = payload["chart"]["result"][0]
    meta = result.get("meta") or {}
    quote = (result.get("indicators") or {}).get("quote") or [{}]
    volumes = [v for v in (quote[0].get("volume") or []) if isinstance(v, (int, float))]
    price = meta.get("regularMarketPrice") or meta.get("previousClose")
    return {
        "price": float(price) if price is not None else None,
        "volume": volumes[-1] if volumes else None,
        "avg5": (sum(volumes[-5:]) / len(volumes[-5:])) if volumes else None,
        "currency": meta.get("currency") or "USD",
    }


def mark(ok: bool | None) -> str:
    if ok is True:
        return "통과"
    if ok is False:
        return "실패"
    return "미확인"


def action(price: float | None, checklist_pass: bool) -> str:
    if price is None:
        return "가격 조회 실패. 매매 판단 보류."
    if price < 135:
        return "135달러 이탈. 비중 줄일 것. 신규 매수 금지."
    if price < 145:
        return "145달러 아래. 신규만 분할. 추격 금지."
    if checklist_pass:
        return "체크리스트 통과. 160달러도 분할 가능. 한 번에 사지 말 것."
    if price >= 160:
        return "160달러 위. 보유. 추격 매수 금지."
    return "145~160달러. 보유. 신규 보류."


def main() -> int:
    cfg = load_json(CFG_PATH, {})
    now = datetime.now(KST)
    day = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    state = load_json(STATE_PATH, {"sent": {}})
    sent = dict(state.get("sent") or {})

    if now.year != int(cfg.get("year_guard") or 2026):
        print("spcxq3 year-guard silent")
        return 0
    slot = SLOTS.get(day)
    if slot is None:
        print("spcxq3 silent; not an oneshot date", day)
        return 0
    if sent.get(day):
        print("spcxq3 already sent", day)
        return 0

    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    try:
        quote = yahoo(cfg.get("ticker") or "SPCX")
    except Exception as exc:
        print(f"price fetch fail: {exc}", file=sys.stderr)
        title = "[PORT] SPCX 가격 조회 실패 | 판단 보류"
        body = "\n".join([
            f"{stamp} | SPCX | {slot}",
            "이게 뭐 알림: SPCX 가격 조회가 실패했습니다. 조건 미충족이 아닙니다.",
            "지금 상태: 시세 없음. 145달러, 135달러 판정 불가.",
            "지금 할 일: 매매하지 말 것. 다음 슬롯에서 다시 조회.",
            "나중에: 3분기 실적과 락업 판정은 가격이 있어야 한다.",
            "익절/줄일 때: 이번 실패만으로 줄이지 말 것.",
            "하지 말 것: 조회 실패를 145달러 지지로 보지 말 것. 추격, 신용, 미수.",
        ])
        publish("cwally-ops", title, body, 4, ["warning"], server)
        sent[day] = {"at": stamp, "kind": "fetch_fail"}
        state.update({"updated_at": stamp, "sent": sent, "last_price": None, "last_verdict": "fetch_fail"})
        save_state(state)
        return 0

    price = quote["price"]
    avg5 = quote["avg5"]
    support_145 = None if price is None else price >= 145
    support_135 = None if price is None else price >= 135
    if avg5 is None:
        volume_ok = None
    elif avg5 >= 80_000_000 and support_145:
        volume_ok = True
    elif avg5 < 50_000_000 or support_135 is False:
        volume_ok = False
    else:
        volume_ok = None

    # 실적 원문은 이 슬롯에서 자동 파싱하지 않는다. 미확인은 실패가 아니다.
    ai_ok = None
    capex_ok = None
    star_ok = None
    checklist_pass = False
    lines = [
        f"AI 매출: {mark(ai_ok)}. 통과는 30억 달러 이상, 실패는 25.61억 미만 또는 계약 축소.",
        f"캡엑스: {mark(capex_ok)}. 통과는 캡엑스/매출 2.0배 이하, 실패는 2.35배 이상.",
        f"Starlink: {mark(star_ok)}. 통과는 순증 120만 이상이고 ARPU 64달러 이상.",
        f"11월 락업 대비 거래량: {mark(volume_ok)}. 5일 평균 {avg5:,.0f}주." if avg5 else "11월 락업 대비 거래량: 미확인. 거래량 조회 없음.",
        f"145달러 지지: {mark(support_145)}. 135달러 지지: {mark(support_135)}.",
    ]
    do = action(price, checklist_pass)
    price_txt = "조회값 없음" if price is None else f"{price:.2f}달러"
    vol_txt = "없음" if avg5 is None else f"{avg5/1_000_000:.1f}백만 주"
    if slot in ("q3a", "q3b"):
        what = "SPCX 3분기 전 체크리스트와 가격 지지 판정입니다."
        later = "실적 원문이 없으면 AI, 캡엑스, Starlink는 미확인이다. 미확인은 통과가 아니다."
    elif slot == "unlock180":
        what = "SPCX 180일 락업(12월 8일) 다음 거래 판정입니다."
        later = "머스크 지분은 2027년 6월까지 별도 락업이다."
    else:
        what = "SPCX 락업 물량 다음 거래일 가격 지지 판정입니다."
        later = "11월 대량 락업은 3분기 실적 이틀 뒤로 추정. 확정일이 아니다."
    title = f"[PORT] SPCX {price_txt} | {do.split('.')[0]}"
    body = "\n".join([
        f"{stamp} | SPCX | {slot} | 기준가 158.96달러(2026-10-02)",
        f"이게 뭐 알림: {what}",
        f"지금 상태: 가격 {price_txt}. 5일 평균 거래량 {vol_txt}. " + " ".join(lines),
        f"지금 할 일: {do}",
        f"나중에: {later} 12개월 기본 200달러, 강세 235달러, 약세 135달러.",
        "익절/줄일 때: 135달러 종가 이탈이면 줄인다. 체크리스트가 통과하기 전에는 200달러를 익절 신호로 보지 말 것.",
        "하지 말 것: 160달러 위 추격, 신용, 미수. 토큰 SPCX와 나스닥 SPCX를 섞지 말 것.",
    ])
    print(body)
    publish(topic, title, body, 4 if support_135 is False else 3, ["chart_with_upwards_trend"], server)
    sent[day] = {"at": stamp, "kind": slot, "price": price, "avg5": avg5}
    state.update({"updated_at": stamp, "sent": sent, "last_price": price, "last_verdict": do})
    save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
