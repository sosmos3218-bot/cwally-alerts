#!/usr/bin/env python3
"""SPCX price-support and Q3 checklist. One-shot dates only. Year-guarded."""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape
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
CIK = "0001181412"


def load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", "replace")


def yahoo(ticker: str) -> dict:
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + ticker
        + "?interval=1d&range=1mo"
    )
    payload = json.loads(fetch(url))
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


def plain(raw: str) -> str:
    text = re.sub(r"(?is)<script.*?>.*?</script>", " ", raw)
    text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text)


def money_to_usd(value: str, unit: str) -> float:
    n = float(value.replace(",", ""))
    unit = unit.lower()
    if unit.startswith("billion"):
        return n * 1_000_000_000
    if unit.startswith("million"):
        return n * 1_000_000
    return n


def first_money(text: str, pattern: str) -> float | None:
    hit = re.search(pattern, text, flags=re.I)
    if not hit:
        return None
    return money_to_usd(hit.group(1), hit.group(2))


def is_q3(text: str) -> bool:
    return "September 30, 2026" in text and (
        "Third Quarter 2026" in text or "quarter ended September 30, 2026" in text
    )


def find_q3_text() -> dict:
    """Return status, text, url, filed. status is ok, absent, or fail."""
    try:
        raw = fetch(f"https://data.sec.gov/submissions/CIK{CIK}.json")
        recent = json.loads(raw)["filings"]["recent"]
    except Exception as exc:
        return {"status": "fail", "error": str(exc), "text": "", "url": "", "filed": ""}
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    accessions = recent.get("accessionNumber") or []
    docs = recent.get("primaryDocument") or []
    for i, form in enumerate(forms):
        filed = dates[i] if i < len(dates) else ""
        if filed < "2026-10-01" or form not in ("8-K", "8-K/A"):
            continue
        acc = accessions[i]
        acc_path = acc.replace("-", "")
        primary = docs[i] if i < len(docs) else ""
        urls = []
        if primary:
            urls.append(f"https://www.sec.gov/Archives/edgar/data/1181412/{acc_path}/{primary}")
        try:
            index = json.loads(fetch(
                f"https://www.sec.gov/Archives/edgar/data/1181412/{acc_path}/{acc}-index.json"
            ))
            for item in index.get("directory", {}).get("item") or []:
                name = item.get("name") or ""
                if "earnings" in name.lower() or name.lower().endswith(".htm"):
                    urls.append(f"https://www.sec.gov/Archives/edgar/data/1181412/{acc_path}/{name}")
        except Exception:
            pass
        seen = []
        for url in urls:
            if url in seen:
                continue
            seen.append(url)
            try:
                text = plain(fetch(url))
            except Exception:
                continue
            if is_q3(text) and ("AI revenues" in text or "Starlink" in text):
                return {"status": "ok", "text": text, "url": url, "filed": filed}
    return {"status": "absent", "text": "", "url": "", "filed": ""}


def parse_q3(text: str) -> dict:
    ai = first_money(text, r"AI revenues were [^.]{0,120}?to \$([0-9]+(?:\.[0-9]+)?) (billion|million)")
    if ai is None:
        ai = first_money(text, r"AI revenues \$([0-9,]+(?:\.[0-9]+)?)(?: (million|billion))?")
        if ai is not None and ai < 100_000:
            ai *= 1_000_000
    revenue = first_money(text, r"Revenues of \$([0-9]+(?:\.[0-9]+)?) (billion|million)")
    capex = first_money(text, r"capital expenditures? (?:of |were |reached )?\$([0-9]+(?:\.[0-9]+)?) (billion|million)")
    if capex is None:
        capex = first_money(text, r"Capital expenditures \$([0-9,]+(?:\.[0-9]+)?)(?: (million|billion))?")
        if capex is not None and capex < 100_000:
            capex *= 1_000_000
    net_add = None
    net_hit = re.search(r"up ([0-9]+(?:\.[0-9]+)?) million sequentially", text, flags=re.I)
    if net_hit:
        net_add = float(net_hit.group(1)) * 1_000_000
    arpu = None
    arpu_hit = re.search(r"ARPU(?: at)? \$([0-9]+(?:\.[0-9]+)?)", text, flags=re.I)
    if arpu_hit:
        arpu = float(arpu_hit.group(1))
    cut = re.search(r"terminat|cancel|not renew|reduced the (?:AI|compute) contract", text, flags=re.I)
    return {
        "ai": ai,
        "revenue": revenue,
        "capex": capex,
        "net_add": net_add,
        "arpu": arpu,
        "cut": bool(cut),
    }


def band(value: float | None, pass_at: float, fail_at: float, higher_is_pass: bool) -> bool | None:
    if value is None:
        return None
    if higher_is_pass:
        if value >= pass_at:
            return True
        if value < fail_at:
            return False
        return None
    if value <= pass_at:
        return True
    if value >= fail_at:
        return False
    return None


def score_fundamentals(found: dict, cfg: dict) -> dict:
    score = cfg.get("score") or {}
    if found.get("status") == "fail":
        return {"status": "fail", "ai": None, "capex": None, "star": None, "detail": found.get("error") or "조회 실패"}
    if found.get("status") != "ok":
        return {"status": "absent", "ai": None, "capex": None, "star": None, "detail": "2026-10-01 이후 3분기 실적 원문 없음"}
    parsed = parse_q3(found.get("text") or "")
    ai_ok = band(parsed["ai"], float(score.get("ai_pass_usd") or 3_000_000_000), float(score.get("ai_fail_usd") or 2_561_000_000), True)
    if parsed["cut"]:
        ai_ok = False
    ratio = None
    if parsed["capex"] and parsed["revenue"]:
        ratio = parsed["capex"] / parsed["revenue"]
    capex_ok = band(ratio, float(score.get("capex_ratio_pass") or 2.0), float(score.get("capex_ratio_fail") or 2.35), False)
    net_ok = band(parsed["net_add"], float(score.get("net_add_pass") or 1_200_000), float(score.get("net_add_fail") or 1_000_000), True)
    arpu_ok = band(parsed["arpu"], float(score.get("arpu_pass") or 64), float(score.get("arpu_fail") or 60), True)
    if net_ok is False or arpu_ok is False:
        star_ok = False
    elif net_ok is True and arpu_ok is True:
        star_ok = True
    else:
        star_ok = None
    return {
        "status": "ok",
        "ai": ai_ok,
        "capex": capex_ok,
        "star": star_ok,
        "parsed": parsed,
        "ratio": ratio,
        "url": found.get("url") or "",
        "filed": found.get("filed") or "",
    }


def verify_reread(first: dict, url: str, cfg: dict) -> dict:
    """Re-fetch the filing URL and check the four scores against the first pass."""
    if not url:
        return {"status": "wait", "match": None, "detail": "원문 없음. 점검 대기", "second": {}}
    try:
        text = plain(fetch(url))
    except Exception as exc:
        return {"status": "fail", "match": None, "detail": f"재독 조회 실패: {exc}", "second": {}}
    if not is_q3(text):
        return {"status": "fail", "match": None, "detail": "재독 원문이 3분기 실적이 아님", "second": {}}
    second = score_fundamentals({"status": "ok", "text": text, "url": url, "filed": first.get("filed") or ""}, cfg)
    labels = all(first.get(k) == second.get(k) for k in ("ai", "capex", "star"))
    nums = all((first.get("parsed") or {}).get(k) == (second.get("parsed") or {}).get(k) for k in ("ai", "revenue", "capex", "net_add", "arpu"))
    match = labels and nums
    return {
        "status": "ok",
        "match": match,
        "detail": "재독 수치와 통과/실패가 1차 채점과 같음" if match else "재독 결과와 1차 채점이 다름",
        "second": second,
    }


def mark(ok: bool | None, status: str) -> str:
    if status == "fail":
        return "조회 실패"
    if status == "absent":
        return "미발표"
    if ok is True:
        return "통과"
    if ok is False:
        return "실패"
    return "보류"



def verify_line(checked: dict, scored: dict) -> str:
    second = checked.get("second") or {}
    parsed = second.get("parsed") or scored.get("parsed") or {}
    ratio = second.get("ratio")
    if ratio is None:
        ratio = scored.get("ratio")
    ratio_txt = "없음" if ratio is None else f"{ratio:.2f}배"
    url = scored.get("url") or "없음"
    if checked.get("status") == "wait":
        return f"채점 점검: 대기. 원문 URL {url}."
    if checked.get("status") == "fail":
        return f"채점 점검: 조회 실패. {checked.get('detail')}. 원문 URL {url}."
    word = "일치" if checked.get("match") else "불일치"
    return (
        f"채점 점검: {word}. 원문 URL {url}. "
        f"AI {fmt_bn(parsed.get('ai'))} {mark(second.get('ai'), 'ok')}, "
        f"캡엑스/매출 {ratio_txt} {mark(second.get('capex'), 'ok')}, "
        f"순증 {parsed.get('net_add') or '없음'} {mark(second.get('star'), 'ok')}, "
        f"ARPU {parsed.get('arpu') or '없음'}."
    )


def fmt_bn(n: float | None) -> str:
    if n is None:
        return "숫자 없음"
    return f"{n / 1_000_000_000:.2f}억 달러"


def action(price: float | None, checklist_pass: bool, any_fail: bool, mismatch: bool = False) -> str:
    if price is None:
        return "가격 조회 실패. 매매 판단 보류."
    if price < 135:
        return "135달러 이탈. 비중 줄일 것. 신규 매수 금지."
    if mismatch:
        return "채점 점검 불일치. 매매 보류."
    if any_fail:
        return "실적 3항목 중 실패. 160달러 분할 금지. 보유 또는 축소 검토."
    if price < 145:
        return "145달러 아래. 신규만 분할. 추격 금지."
    if checklist_pass:
        return "실적 3항목 통과. 160달러도 분할 가능. 한 번에 사지 말 것."
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

    found = find_q3_text()
    scored = score_fundamentals(found, cfg)
    checked = verify_reread(scored, scored.get("url") or "", cfg)
    status = scored["status"]
    parsed = scored.get("parsed") or {}
    ai_ok = scored["ai"]
    capex_ok = scored["capex"]
    star_ok = scored["star"]
    checklist_pass = status == "ok" and ai_ok is True and capex_ok is True and star_ok is True
    any_fail = status == "ok" and (ai_ok is False or capex_ok is False or star_ok is False)
    mismatch = checked.get("match") is False
    ratio = scored.get("ratio")
    ratio_txt = "숫자 없음" if ratio is None else f"{ratio:.2f}배"
    lines = [
        f"AI 매출: {mark(ai_ok, status)}. {fmt_bn(parsed.get('ai'))}. 통과 30억 이상, 실패 25.61억 미만 또는 계약 축소.",
        f"캡엑스/매출: {mark(capex_ok, status)}. {ratio_txt}. 통과 2.0배 이하, 실패 2.35배 이상.",
        f"Starlink: {mark(star_ok, status)}. 순증 {parsed.get('net_add') or '없음'}, ARPU {parsed.get('arpu') or '없음'}. 통과는 순증 120만 이상이고 ARPU 64달러 이상.",
        f"11월 락업 대비 거래량: {mark(volume_ok, 'ok')}. 5일 평균 {avg5:,.0f}주." if avg5 else "11월 락업 대비 거래량: 미확인. 거래량 조회 없음.",
        f"145달러 지지: {mark(support_145, 'ok')}. 135달러 지지: {mark(support_135, 'ok')}.",
        verify_line(checked, scored),
    ]
    do = action(price, checklist_pass, any_fail, mismatch)
    price_txt = "조회값 없음" if price is None else f"{price:.2f}달러"
    vol_txt = "없음" if avg5 is None else f"{avg5 / 1_000_000:.1f}백만 주"
    src = scored.get("url") or "원문 없음"
    if slot in ("q3a", "q3b"):
        what = "SPCX 3분기 실적 3항목과 가격 지지 판정입니다."
        later = "미발표와 조회 실패는 통과가 아니다. 실적 3항목이 모두 통과해야 160달러 분할이다."
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
        f"지금 상태: 가격 {price_txt}. 5일 평균 거래량 {vol_txt}. 실적 상태 {status}. " + " ".join(lines),
        f"지금 할 일: {do}",
        f"나중에: {later} 원문 {src}. 12개월 기본 200달러, 강세 235달러, 약세 135달러.",
        "익절/줄일 때: 135달러 종가 이탈이면 줄인다. 실적 3항목 통과 전에는 200달러를 익절 신호로 보지 말 것.",
        "하지 말 것: 160달러 위 추격, 신용, 미수. 미발표를 실패로 보지 말 것. 토큰 SPCX와 나스닥 SPCX를 섞지 말 것.",
    ])
    print(body)
    publish(topic, title, body, 4 if support_135 is False or any_fail or mismatch else 3, ["chart_with_upwards_trend"], server)
    sent[day] = {"at": stamp, "kind": slot, "price": price, "avg5": avg5, "earnings": status, "verify": checked.get("status"), "verify_match": checked.get("match"), "url": scored.get("url") or ""}
    state.update({"updated_at": stamp, "sent": sent, "last_price": price, "last_verdict": do})
    save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
