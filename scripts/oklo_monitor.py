#!/usr/bin/env python3
"""OKLO: 2x zone on morning. Nov 11 classifies, Nov 12-13 cross-check SEC."""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "oklo.json"
STATE_PATH = ROOT / "state" / "oklo.json"
UA = {"User-Agent": "cwally-alerts oklo research monitor research@example.com"}
CLASSIFY_DAY = "2026-11-11"
VERIFY_DAYS = {"2026-11-12", "2026-11-13"}


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return dict(fallback)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return dict(fallback)
    out = dict(fallback)
    out.update(data)
    return out


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def quote(url: str) -> tuple[float | None, str | None]:
    data = json.loads(fetch(url))
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        return None, "quote empty"
    price = (result[0].get("meta") or {}).get("regularMarketPrice")
    try:
        return (float(price) if price is not None else None), None
    except (TypeError, ValueError):
        return None, "price parse fail"


def latest_filing(url: str) -> dict | None:
    data = json.loads(fetch(url))
    recent = ((data.get("filings") or {}).get("recent") or {})
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    accessions = recent.get("accessionNumber") or []
    docs = recent.get("primaryDocument") or []
    for form, day, acc, doc in zip(forms, dates, accessions, docs):
        if form in {"10-Q", "8-K", "10-K"}:
            return {"form": form, "date": day, "accession": acc, "doc": doc}
    return None


def filing_text(item: dict) -> tuple[str, str]:
    acc = item["accession"].replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/1849056/{acc}/{item['doc']}"
    raw = fetch(url)
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", raw)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)[:200000], url


def money_bil(text: str, label: str) -> float | None:
    m = re.search(label + r".{0,80}?\$\s*([0-9]{1,3}(?:,[0-9]{3})+)", text, re.I)
    if not m:
        return None
    return float(m.group(1).replace(",", "")) / 1_000_000


def parse_checklist(text: str) -> dict:
    cash = money_bil(text, r"cash, cash equivalents, and marketable debt securities were")
    if cash is None:
        cash = money_bil(text, r"cash and cash equivalents")
    capex = None
    m = re.search(r"purchases of property and equipment.{0,40}?\$\s*([0-9]{1,3}(?:,[0-9]{3})+)", text, re.I)
    if m:
        capex = float(m.group(1).replace(",", "")) / 1000
    atm_shares = None
    m = re.search(r"sold\s+([0-9,]+)\s+shares", text, re.I)
    if m:
        atm_shares = int(m.group(1).replace(",", ""))
    atm_gross = None
    m = re.search(r"gross proceeds of approximately \$\s*([0-9,.]+)\s*(billion|million)", text, re.I)
    if m:
        atm_gross = float(m.group(1).replace(",", ""))
        if m.group(2).lower() == "million":
            atm_gross /= 1000
    aurora = "확인 불가"
    if re.search(r"Aurora-INL", text, re.I):
        if re.search(r"Aurora-INL.{0,160}(criticality|critical)", text, re.I):
            aurora = "임계 문구 있음"
        elif re.search(r"Aurora-INL.{0,160}(2028|delay|deferred)", text, re.I):
            aurora = "2028 또는 지연 문구 있음"
        else:
            aurora = "언급 있음, 임계 문구 없음"
    return {"cash_bil": cash, "capex_mil": capex, "atm_shares": atm_shares, "atm_gross_bil": atm_gross, "aurora": aurora}


def snippet(text: str, pattern: str) -> str:
    m = re.search(pattern, text, re.I)
    if not m:
        return "원문에 해당 문구 없음"
    start = max(0, m.start() - 40)
    return text[start : m.end() + 80].strip()


def cross_check(text: str, parsed: dict) -> list[str]:
    lines = []
    if parsed.get("cash_bil") is None:
        hit = re.search(r"cash, cash equivalents|marketable debt securities", text, re.I)
        lines.append("현금: 파싱 실패. " + ("원문에 문구 있음, 숫자 추출 실패" if hit else "원문에 합계 문구 없음"))
        if hit:
            lines.append("현금 원문: " + snippet(text, r"cash, cash equivalents.{0,120}"))
    else:
        lines.append(f"현금: 파싱 {parsed['cash_bil']:.2f}억달러")
    if parsed.get("capex_mil") is None:
        hit = re.search(r"purchases of property and equipment|capital expenditure", text, re.I)
        lines.append("설비투자: 파싱 실패. " + ("원문에 문구 있음" if hit else "원문에 문구 없음"))
    else:
        lines.append(f"설비투자: 파싱 {parsed['capex_mil']:.1f}백만달러")
    if not parsed.get("atm_shares") and parsed.get("atm_gross_bil") is None:
        hit = re.search(r"at-the-market|ATM|gross proceeds", text, re.I)
        lines.append("ATM: 파싱 실패. " + ("원문에 문구 있음" if hit else "원문에 사용 문구 없음"))
    else:
        lines.append("ATM: 파싱 값 있음")
    if parsed.get("aurora") == "확인 불가":
        hit = re.search(r"Aurora-INL", text, re.I)
        lines.append("Aurora-INL: 파싱 실패. " + ("원문에 이름 있음" if hit else "원문에 이름 없음"))
        if hit:
            lines.append("Aurora 원문: " + snippet(text, r"Aurora-INL.{0,140}"))
    else:
        lines.append(f"Aurora-INL: {parsed['aurora']}")
    return lines


def body(stamp: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join(
        [
            f"{stamp} | OKLO | 현금배수와 3분기 체크리스트",
            "이게 뭐 알림: 오클로 순현금 2배와 3분기 현금, ATM, 설비투자, Aurora-INL만 보는 알림입니다. 매수 지시가 아닙니다.",
            f"지금 결과/상태: {status}",
            f"지금 할 일: {action}",
            f"나중에: {later}",
            f"익절/줄일 때: {trim}",
            f"하지 말 것: {avoid}",
        ]
    )


def emit(cfg: dict, title: str, msg: str, pri: int, dry: bool) -> None:
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    print(title)
    print(msg)
    print(f"topic={topic} send={not dry}")
    if not dry:
        publish(topic, title, msg, pri, ["chart_with_upwards_trend"], server)


def remember_class(state: dict, today: str, kind: str) -> None:
    if today == CLASSIFY_DAY or not state.get("nov11_class"):
        state["nov11_class"] = kind
        state["nov11_class_date"] = today


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    dry = mode == "dry" or os.environ.get("OKLO_DRY") == "1"
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = load_json(STATE_PATH, {"zone": "above", "last_filing": cfg["last_known_filing"], "empty_dates": [], "fail_dates": [], "verify_dates": []})
    now = datetime.now(KST)
    today = now.date().isoformat()
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    base = cfg["baseline"]
    event = mode == "event"
    verify = event and today in VERIFY_DAYS
    if event and now.year != int(cfg.get("event_year", 2026)) and mode not in {"force", "dry"}:
        print("later year; silent")
        return 0
    if today > cfg["valid_until"] and mode not in {"force", "dry", "event"}:
        print(f"after {cfg['valid_until']}; silent")
        return 0

    price, qerr = None, None
    try:
        price, qerr = quote(cfg["sources"]["quote"])
    except Exception as exc:
        qerr = str(exc)
    multiple = (price / base["net_cash_per_share"]) if price else None
    zone = "buy" if price is not None and price <= base["two_x"] else "above"
    print(f"price={price} multiple={multiple} zone={zone} qerr={qerr} verify={verify}")

    alerts: list[tuple[str, str, int]] = []
    if price is None and not event and state.get("fail_dates", [])[-1:] != [today]:
        alerts.append(
            (
                "[OKLO] 주가 조회 실패 | 2배 조건 미확인",
                body(stamp, f"주가 조회 실패. {qerr or '빈 응답'}. 조건 미충족이 아닙니다.", "증권 앱에서 OKLO를 직접 보세요. 32.32달러 이탈로 판정하지 마세요.", "다음 평일 09:00 KST에 재시도합니다.", "이번 알림으로 사지 마세요.", "조회 실패를 2배 진입이나 정상으로 취급."),
                4,
            )
        )
        state.setdefault("fail_dates", []).append(today)
    elif price is not None and not event:
        prev = state.get("zone") or "above"
        if zone != prev or mode == "force":
            title = "[OKLO] 순현금 2배 이하 | 분할 관심" if zone == "buy" else "[OKLO] 순현금 2배 위 | 관심 해제"
            action = "32.32달러 이하입니다. 새 돈은 한 번에 넣지 마세요." if zone == "buy" else "가격 조건은 꺼졌습니다. 관망 유지."
            alerts.append(
                (
                    title,
                    body(stamp, f"OKLO {price:.2f}달러, 순현금 {base['net_cash_per_share']}달러의 {multiple:.2f}배. 구간 {prev} -> {zone}.", action, "11일 08:20은 판정, 12~13일 08:20은 SEC 대조.", "32.32달러 복귀는 가격 관심 해제. 손절 신호는 아닙니다.", "2배 터치를 확정 매수로 쓰기. 신용, 미수, 추격."),
                    4 if zone == "buy" else 3,
                )
            )
            state["zone"] = zone
            state["last_price"] = price
        else:
            state["zone"] = zone
            state["last_price"] = price
            print("price zone unchanged; silent")

    filing = None
    ferr = None
    try:
        filing = latest_filing(cfg["sources"]["sec"])
    except Exception as exc:
        ferr = str(exc)
    last = state.get("last_filing") or cfg["last_known_filing"]
    new_filing = bool(filing and filing["date"] > last)
    text, doc_url = "", ""
    parsed: dict = {}
    if filing and (new_filing or verify):
        try:
            text, doc_url = filing_text(filing)
            parsed = parse_checklist(text)
        except Exception as exc:
            ferr = str(exc)

    if verify and today not in state.get("verify_dates", []):
        prior = state.get("nov11_class") or "11일 판정 없음"
        if ferr and not text:
            kind = "조회 실패"
            status = f"11일 판정은 {prior}. 이번 SEC 조회 실패. {ferr}. 원문 없음으로 바꾸지 않습니다."
            action = "SEC 원문을 직접 여세요. 이 실패로 채점하지 마세요."
        elif not filing or filing["date"] <= cfg["last_known_filing"]:
            kind = "원문 없음"
            match = "11일 판정과 같음" if prior == "원문 없음" else f"11일 판정은 {prior}, 지금은 원문 없음"
            status = f"{match}. 기준 공시 {cfg['last_known_filing']} 이후 10-Q/8-K 없음."
            action = "체크리스트 채점으로 읽지 마세요. 추가매수 보류."
        else:
            kind = "체크리스트 채점"
            gaps = cross_check(text, parsed)
            status = f"11일 판정은 {prior}. 지금 공시 {filing['form']} {filing['date']}. " + " ".join(gaps)
            action = "파싱 실패인데 원문에 문구가 있으면 그 항목은 미확인입니다. 충족으로 세지 마세요."
        alerts.append(
            (
                f"[OKLO] 11일 알림 대조 | {kind}",
                body(stamp, status, action, "13일 08:20 KST가 마지막 대조. 2027년에는 침묵.", "대조 결과로 비중을 늘리지 마세요.", "조회 실패를 원문 없음으로, 파싱 실패를 정상으로 읽기."),
                4,
            )
        )
        state.setdefault("verify_dates", []).append(today)
        state["last_verify_class"] = kind
        if doc_url:
            state["last_doc_url"] = doc_url
    elif event or new_filing:
        if event and not new_filing and not ferr:
            if today not in state.get("empty_dates", []) or mode == "force":
                px = f"{price:.2f}달러, {multiple:.2f}배" if price else "주가 확인 불가"
                alerts.append(
                    (
                        "[OKLO] 3분기 원문 없음 | 체크리스트 대기",
                        body(stamp, f"10-Q/8-K가 {last} 이후 없음. 가격 {px}. 2배 선은 {base['two_x']}달러.", "보유나 관심 유지. 원문 전 추가매수 금지.", "12~13일 08:20 KST에 이 판정을 SEC와 대조합니다.", "미발표로 비중 조정 금지.", "2분기 현금을 3분기 실적으로 확정."),
                        3,
                    )
                )
                state.setdefault("empty_dates", []).append(today)
                remember_class(state, today, "원문 없음")
        elif new_filing and parsed:
            cash = parsed.get("cash_bil")
            capex = parsed.get("capex_mil")
            aurora = parsed.get("aurora")
            cash_txt = f"{cash:.2f}억달러" if cash is not None else "확인 불가"
            capex_txt = f"{capex:.1f}백만달러" if capex is not None else "확인 불가"
            atm_txt = "확인 불가"
            if parsed.get("atm_shares"):
                atm_txt = f"{parsed['atm_shares']:,}주"
            if parsed.get("atm_gross_bil") is not None:
                atm_txt += f", 총액 약 {parsed['atm_gross_bil']:.2f}억달러"
            two_x = "예" if price is not None and price <= base["two_x"] else ("아니오" if price is not None else "확인 불가")
            alerts.append(
                (
                    "[OKLO] 3분기 체크리스트 | 원문 대조",
                    body(stamp, f"새 공시 {filing['form']} {filing['date']}. 현금 {cash_txt}. ATM {atm_txt}. 설비투자 {capex_txt}. Aurora-INL {aurora}. 2배 이하 {two_x}.", "확인 불가 항목은 12~13일 SEC 대조 전 충족으로 세지 마세요.", "현금 25억달러 미만 또는 ATM 5억달러 이상이면 추가매수 보류.", "Aurora-INL 지연이 원문과 맞으면 관심 축소.", "파싱 실패를 정상이나 호재로 읽기. 신용, 미수."),
                    4,
                )
            )
            state["last_filing"] = filing["date"]
            state["last_parsed"] = parsed
            state["final_notified"] = True
            remember_class(state, today, "체크리스트 채점")
        elif ferr and today not in state.get("fail_dates", []):
            alerts.append(
                (
                    "[OKLO] 공시 조회 실패 | 체크리스트 미확인",
                    body(stamp, f"SEC 조회 실패. {ferr}. 조건 미충족이 아닙니다.", "SEC 원문을 직접 보세요.", "12~13일 대조에서 재시도.", "이번 알림으로 비중을 바꾸지 마세요.", "조회 실패를 원문 없음이나 정상으로 취급."),
                    4,
                )
            )
            state.setdefault("fail_dates", []).append(today)
            remember_class(state, today, "조회 실패")

    for title, msg, pri in alerts:
        emit(cfg, title, msg, pri, dry)
    if not alerts:
        print("no alert")
    state["updated_at"] = now.isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
