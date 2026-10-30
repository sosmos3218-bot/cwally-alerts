#!/usr/bin/env python3
"""[IREN-PX] IREN (NASDAQ) close-based zones + SEC filing whitelist (approved in chat 2026-10-30).

Watchlist only (user does not hold IREN). Warnings never carry buy wording.
  - close <= 23.72 (52-week low) -> '52주 저가 이탈, 관망 유지 경고'      (priority 3)
  - close <= 20.00              -> '재평가 구간 진입 경고'               (priority 4, re-check list)
  - close >= 35.00              -> '반등 확인, 추격 매수 보류 체크리스트' (priority 3)
Each zone fires once when a completed close ENTERS it; it re-arms quietly after a close leaves the zone.
If 23.72 and 20.00 fire together only the stronger 20.00 warning is sent. First run only records the close.
Dedupe by the bar's own US/Eastern date (state last_date). Same pattern as figpx_monitor.py.

SEC whitelist (CIK 0001878848, from sec.start_date_kst; every accession at install seeded silently):
  p4: 8-K items 1.01 1.02 2.01 2.05 2.06 3.01 4.01 4.02 5.01, 8-K 5.02 naming the co-CEOs/CFO
      (other 5.02 -> p3), S-1 / S-3 / 424B* (dilution)
  p3: other 8-K items (8.01, 2.03 debt, 3.02 unregistered sales, 3.03 ...), convertible-note 8-Ks
      (text check), Schedule 13D(/A), Form 4 open-market sales (S) by a co-CEO over sec.ceo_sale_usd per filing
  ignored: 144, other Form 4, S-8, 13G, proxy, FWP, D, 10-K, 10-Q ([IREN-EARN] watches results),
           8-K with only 2.02 / 7.01 / 5.07 / 9.01
Same-run hits are combined into one message (max priority).

Runs on the existing morning 09:00 KST slot (after the US close). No new cron.
Modes: auto | force (ignore date dedupe) | dry (print only, no ntfy, no state)
       | confirm (install-confirmation send, no state) | selftest | backtest [days] (SEC classifier on past filings, no send).
Not investment advice.
"""
from __future__ import annotations

import html
import json
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
NY = ZoneInfo("America/New_York")
CFG = ROOT / "configs" / "irenpx.json"
STATE = ROOT / "state" / "irenpx.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}
P = "[IREN-PX]"
BANNED = ("\u00b7", "\u2014")
# buy-ish wording that must never appear in a warning (stop/warn) message
BUY_WORDS = ("분할매수", "매수 검토", "매수 기회", "매수 구간", "저가 매수", "사세요", "진입 검토", "담을", "나눠서", "매수 매력")


def load_cfg() -> dict:
    return json.loads(CFG.read_text(encoding="utf-8"))


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(st: dict, dry: bool) -> None:
    if dry:
        print("dry: state not saved")
        return
    st["updated_at"] = datetime.now(KST).isoformat(timespec="seconds")
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch_bars(cfg: dict, now_ny: datetime) -> list[tuple[date, float]]:
    """Completed daily bars (US/Eastern date, close), ascending. Today's live bar is excluded."""
    req = urllib.request.Request(cfg["sources"]["bars"], headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        res = json.loads(resp.read().decode())["chart"]["result"][0]
    out: dict[date, float] = {}
    for t, c in zip(res.get("timestamp") or [], res["indicators"]["quote"][0].get("close") or []):
        if c is not None:
            out[datetime.fromtimestamp(t, NY).date()] = float(c)
    meta = res.get("meta") or {}
    rmt, rmp = meta.get("regularMarketTime"), meta.get("regularMarketPrice")
    if rmt and rmp:
        mt = datetime.fromtimestamp(rmt, NY)
        if mt.date() not in out and mt.hour * 60 + mt.minute >= 16 * 60:
            out[mt.date()] = float(rmp)
    today = now_ny.date()
    after_close = now_ny.hour * 60 + now_ny.minute >= 16 * 60 + 10
    return sorted((d, c) for d, c in out.items() if d < today or (d == today and after_close))


# ---------- price zones (pure) ----------

def hit(level: dict, close: float) -> bool:
    p = float(level["price"])
    return close <= p if level["op"] == "le" else close >= p


def step(levels: list[dict], armed: dict | None, close: float) -> tuple[dict, list[str]]:
    """armed[key] True = watching (outside the zone). armed None = first run: init, nothing fires."""
    close = round(float(close), 4)
    if armed is None:
        return {lv["key"]: not hit(lv, close) for lv in levels}, []
    new, fired = dict(armed), []
    for lv in levels:
        k = lv["key"]
        inside = hit(lv, close)
        if new.get(k, True):
            if inside:
                new[k] = False
                fired.append(k)
        elif not inside:
            new[k] = True
    return new, fired


def effective(fired: list[str]) -> list[str]:
    """20.00 warning supersedes the 23.72 warning when both fire in the same run."""
    return [k for k in fired if not (k == "low2372" and "low20" in fired)]


LINES = "23.72달러 이하(52주 저가 이탈, 경고), 20달러 이하(재평가 구간, 강한 경고), 35달러 이상(반등 확인)"
RECHECK = ("1) Horizon 2~4 진행: 인도 일정과 고객(Microsoft) 승인, 지연 문구. "
           "2) 펀딩과 희석: 현금(약 59억 달러) 대비 전환사채(약 67억 달러), ATM 신주 발행, 새 전환사채. "
           "3) 가이던스: 2026년 말 계약 ARR 40억 달러 목표 유지 여부, CapEx 계획")
REBOUND_CHECK = ("1) 반등 이유가 공시나 보도자료로 확인되는지(신규 계약, 프리페이먼트, 가이던스 상향, Horizon 인도). "
                 "2) 같은 시기 희석 발표(ATM, 전환사채, 424B) 동반 여부. "
                 "3) 거래량과 공매도 비율(21~23%) 숏커버 가능성, 베타 약 4.3이라 하루 10% 이상 되돌림 흔함. "
                 "4) 실적과 Horizon 일정이 2026-10-30 분석 때보다 실제로 좋아졌는지")


def message(cfg: dict, fired: list[str], close: float, day: str, prev_close: float | None) -> tuple[str, str, int]:
    lv = {x["key"]: x for x in cfg["levels"]}
    fired = effective(fired)
    top = max((lv[k] for k in fired), key=lambda x: x["priority"])
    warn = any(lv[k]["kind"] == "warning" for k in fired)
    chg = f", 전 거래일 대비 {(close / prev_close - 1) * 100:+.1f}%" if prev_close else ""
    labels = " + ".join(lv[k]["label"] for k in fired)
    status = f"{labels}. 미국 종가 {close:.2f}달러({day} 미 동부{chg}). 52주 고가 76.87달러 대비 {(close / 76.87 - 1) * 100:+.0f}%."
    if "low20" in fired:
        title = f"{P} 종가 {close:.2f}달러 20달러 이하 재평가 구간 | 경고, 추가매수 보류"
        todo = ("관망 유지, 추가매수 보류(보유 없음이면 신규 진입도 보류). 재평가 구간이라 아래 3가지를 공시와 실적 자료로 다시 확인: "
                + RECHECK + ".")
        later = "3가지 확인 결과는 메모해 두고, 다음 실적과 SEC 공시 알림([IREN-PX] 공시, [IREN-EARN])과 함께 다시 판단."
        trim = "보유 없음이라 해당 없음. 소규모 투기 포지션을 잡았다면 20달러 아래에서는 손절 기준과 비중부터 재확인."
        dont = "급락 당일 물타기, 반등 기대 추격, 신용/미수, 이 알림만 보고 주문. 투자 권유 아님."
    elif warn:
        title = f"{P} 종가 {close:.2f}달러 52주 저가 이탈 | 관망 유지, 추가매수 보류"
        todo = ("관망 유지, 추가매수 보류(보유 없음이면 신규 진입도 보류). 하락 이유가 Horizon 지연, 희석(ATM, 전환사채), "
                "가이던스 하향 중 무엇인지 뉴스와 SEC 공시로 먼저 확인.")
        later = "20달러 이하로 더 내려가면 재평가 구간 경고(우선순위 4)가 따로 옵니다."
        trim = "보유 없음이라 해당 없음. 소규모 포지션이 있다면 손절 기준 재확인."
        dont = "52주 저가라는 이유만으로 바닥 판단, 장중 급락 추격, 신용/미수, 이 알림만 보고 주문. 투자 권유 아님."
    else:
        title = f"{P} 종가 {close:.2f}달러 35달러 이상 반등 | 추격 매수 보류, 근거 재확인"
        todo = "추격 매수 보류. 반등 근거부터 확인: " + REBOUND_CHECK + "."
        later = "근거가 확인돼도 관망 또는 소규모 투기 포지션 원칙(2026-10-30 분석) 유지. 다시 35달러 아래로 내려갔다 올라오면 또 울립니다."
        trim = "보유 없음이라 해당 없음. 소규모 포지션이 있다면 35달러 반등에서 일부 정리 기준 점검."
        dont = "반등 첫날 추격, 계획 금액 초과, 신용/미수, 이 알림만 보고 주문. 투자 권유 아님."
    body = [
        f"{datetime.now(KST):%Y-%m-%d %H:%M} KST | IREN | 관심 종목(보유 없음)",
        "이게 뭐 알림: " + cfg["explain"],
        "지금 결과/상태: " + status,
        "지금 할 일: " + todo,
        f"나중에: {later} 같은 구간은 종가가 구간을 벗어난 뒤 다시 들어올 때만 또 울립니다. 선: {LINES}.",
        "익절/줄일 때: " + trim,
        "하지 말 것: " + dont,
    ]
    return title, "\n".join(body), int(top["priority"])


def confirm_message(cfg: dict, close: float | None, day: str | None) -> tuple[str, str]:
    px = f"{close:.2f}달러({day} 미 동부 종가)" if close is not None else "조회 실패(구간 미확인)"
    s = cfg["sec"]
    lines = [
        "[테스트] IREN 알림 설정 완료",
        "이게 뭐 알림: " + cfg["explain"],
        f"지금 결과/상태: IREN 현재 종가 {px}. 선 3개: 23.72달러 이하 52주 저가 이탈 경고(우선순위 3), 20달러 이하 재평가 구간 경고(4), 35달러 이상 반등 확인(3). "
        f"SEC(CIK {s['cik']}) 공시 감시도 같은 알림에 포함.",
        "지금 할 일: 없음. 평일 09:00 KST morning 에서 전일 미국 종가로 판정하고 구간에 새로 들어갈 때만 보냅니다. 실적(11/5 미 동부 장 마감 뒤) 전 체크리스트는 11/5 19:00, 실적 공시 확인은 [IREN-EARN] 으로 옵니다.",
        "나중에: SEC 공시는 중요 8-K(계약 체결과 종료, 인수, 손상, 상장 통지, 감사인 변경, 재작성, 지배권 변경, 공동 CEO 또는 CFO 변동)와 S-1/S-3/424B(희석)는 우선순위 4, "
        f"8-K 8.01 등 기타 항목, 전환사채 발행, 13D, 공동 CEO의 신고 1건 {s['ceo_sale_usd'] / 1e6:,.0f}백만 달러 초과 매도는 3. Form 144, S-8, 일반 Form 4, 13G 는 보내지 않습니다. 2027-10-31 까지.",
        "익절/줄일 때: 보유 없음이라 해당 없음.",
        "하지 말 것: 이 설치 확인을 매수/매도 신호로 읽기.",
    ]
    return f"{P} 설치 확인 | 매매 신호 아님", "\n".join(lines)


# ---------- SEC filing whitelist ----------

ITEM_KO = {
    "1.01": "중요 계약 체결", "1.02": "중요 계약 종료", "2.01": "인수 또는 자산 처분 완료",
    "2.03": "차입 등 직접 금융 의무 발생", "2.05": "구조조정 비용", "2.06": "중대한 자산 손상",
    "3.01": "상장 유지 기준 미달 또는 상장폐지 통지", "3.02": "미등록 증권 발행", "3.03": "주주 권리 변경",
    "4.01": "회계감사인 변경", "4.02": "과거 재무제표 신뢰 불가(재작성)", "5.01": "회사 지배권 변경",
    "5.02": "임원 또는 이사 변동", "5.03": "정관 변경", "8.01": "기타 중요 사항",
}
WARN_ITEMS = {"1.02", "2.03", "2.05", "2.06", "3.01", "3.02", "4.01", "4.02", "5.01", "5.02"}
CONV_RE = re.compile(r"convertible\s+(senior\s+)?notes?", re.I)
TEXT_ITEMS = {"1.01", "2.03", "3.02", "8.01"}  # 8-K items where we read the text for convertible-note wording


def sec_get(cfg: dict, url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": cfg["sec"]["user_agent"], "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def sec_rows(subs: dict) -> list[dict]:
    r = (subs.get("filings") or {}).get("recent") or {}
    keys = ("form", "filingDate", "accessionNumber", "items", "primaryDocument")
    cols = [r.get(k) or [] for k in keys]
    n = min(len(c) for c in cols) if cols else 0
    return [{"form": cols[0][i], "date": cols[1][i], "acc": cols[2][i], "items": cols[3][i] or "",
             "doc": cols[4][i] or ""} for i in range(n)]


def filing_dir(cfg: dict, f: dict) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cfg['sec']['cik'])}/{f['acc'].replace('-', '')}"


def index_url(cfg: dict, f: dict) -> str:
    return f"{filing_dir(cfg, f)}/{f['acc']}-index.htm"


def parse_items(raw: str) -> list[str]:
    return [x.strip() for x in str(raw or "").split(",") if x.strip()]


def plain(doc_html: str) -> str:
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", doc_html or "", flags=re.S | re.I)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t)))


def item_502_text(doc_html: str) -> str:
    t = plain(doc_html)
    i = t.find("Item 5.02")
    if i < 0:
        return ""
    seg = t[i + len("Item 5.02"):]
    ends = [j for j in (seg.find("Item "), seg.find("SIGNATURE")) if j > 0]
    return seg[:min(ends)] if ends else seg


def officer_re(cfg: dict) -> re.Pattern:
    names = "|".join(re.escape(n) for n in cfg["sec"]["officer_names"])
    return re.compile(rf"Chief\s+(Executive|Financial)\s+Officer|\b(CEO|CFO)\b|{names}", re.I)


def parse_form4(xml: str) -> dict:
    def tag(name: str, src: str) -> str:
        m = re.search(rf"<{name}>\s*(?:<value>)?\s*([^<]*)", src)
        return html.unescape(m.group(1).strip()) if m else ""
    owner, title = tag("rptOwnerName", xml), tag("officerTitle", xml)
    sells = []
    for t in re.findall(r"<nonDerivativeTransaction>(.*?)</nonDerivativeTransaction>", xml, re.S):
        if tag("transactionCode", t) != "S":
            continue
        try:
            sh, px = float(tag("transactionShares", t) or 0), float(tag("transactionPricePerShare", t) or 0)
        except ValueError:
            sh, px = 0.0, 0.0
        sells.append(sh * px)
    return {"owner": owner, "title": title, "sell_usd": sum(sells), "n_sells": len(sells)}


def usd(v: float) -> str:
    return f"{v / 1e6:,.1f}백만 달러"


def classify(cfg: dict, f: dict, fetch) -> dict | None:
    """One filing -> hit {prio, kind, warn, label, detail, ...} or None. fetch(url) -> text.
    Form 4 fetch errors propagate (accession retried next run); 8-K text errors degrade to p3/no label."""
    sec = cfg["sec"]
    form = f["form"].upper().strip()
    base = {"form": f["form"], "date": f["date"], "acc": f["acc"], "items": f["items"], "url": index_url(cfg, f)}
    if form in {"8-K", "8-K/A"}:
        rest = [i for i in parse_items(f["items"]) if i not in set(sec["ignore_8k_items"])]
        if not rest:
            return None
        prio = 4 if any(i in set(sec["p4_8k_items"]) for i in rest) else 3
        extra, conv, doc = "", False, None
        if "5.02" in rest or set(rest) & TEXT_ITEMS:
            try:
                doc = fetch(f"{filing_dir(cfg, f)}/{f['doc']}")
            except Exception as exc:  # noqa: BLE001
                extra += f" 원문 조회 실패({exc.__class__.__name__})."
        if doc is not None and set(rest) & TEXT_ITEMS:
            conv = bool(CONV_RE.search(plain(doc)))
            if conv:
                extra += " 본문에 전환사채(convertible notes) 언급 있음, 희석 가능성 확인."
        if "5.02" in rest:
            officer = bool(doc is not None and officer_re(cfg).search(item_502_text(doc)))
            if officer:
                prio = 4
                extra += " 공동 CEO 또는 CFO 관련 내용 있음."
            elif doc is not None:
                extra += " 공동 CEO, CFO 언급 없음(이사 또는 다른 임원 변동)."
            else:
                extra += " 임원 범위 미확인이라 우선순위 3."
        names = ", ".join(f"{i} {ITEM_KO.get(i, '기타 항목')}" for i in rest)
        label = ("8-K 전환사채 관련" if conv else f"8-K {names.split(', ')[0]}") + (f" 외 {len(rest) - 1}건" if len(rest) > 1 and not conv else "")
        return {**base, "prio": prio, "kind": "8k", "warn": conv or any(i in WARN_ITEMS for i in rest), "label": label,
                "detail": f"8-K 항목 {names}.{extra}"}
    if any(form.startswith(p) for p in sec["p4_forms_prefix"]):
        return {**base, "prio": 4, "kind": "offer", "warn": True, "label": f"{f['form']} 증권 발행 서류(희석)",
                "detail": f"{f['form']} 증권 신고 또는 발행 설명서. 신주 발행(ATM 포함), 전환사채 관련 주식, 기존 주주 매출일 수 있음(희석)."}
    if form in {x.upper() for x in sec["p3_forms"]}:
        return {**base, "prio": 3, "kind": "13d", "warn": True, "label": f"{f['form']} 대량 보유 신고",
                "detail": f"{f['form']} 5% 이상 보유자의 적극적 보유 목적 신고(지분 변동, 경영 참여 의사)."}
    if form in {"4", "4/A"}:
        info = parse_form4(fetch(f"{filing_dir(cfg, f)}/{f['doc'].split('/')[-1]}"))
        if re.search(cfg["sec"]["ceo_title_re"], info["title"]) and info["sell_usd"] > float(sec["ceo_sale_usd"]):
            return {**base, "prio": 3, "kind": "f4sell", "warn": True, "label": "Form 4 공동 CEO 대규모 매도",
                    "detail": f"Form 4 {info['owner']}({info['title']}) 장내 매도(S) {info['n_sells']}건, 합계 {usd(info['sell_usd'])}, 기준 {usd(float(sec['ceo_sale_usd']))} 초과."}
        return None
    return None


def sec_eval(cfg: dict, st: dict, subs: dict, today: date, fetch) -> tuple[list[dict], list[str], list[str]]:
    """-> (hits, accessions to mark seen, log). Seeds silently on first run and before start date."""
    rows = sec_rows(subs)
    seen = set(st.get("sec_seen") or [])
    log: list[str] = []
    if "sec_seen" not in st:
        log.append(f"seed {len(rows)} accession numbers silently")
        return [], [r["acc"] for r in rows], log
    new = [r for r in rows if r["acc"] not in seen]
    start = date.fromisoformat(cfg["sec"]["start_date_kst"])
    if today < start:
        if new:
            log.append(f"before {start}: record {len(new)} new accession(s) silently")
        return [], [r["acc"] for r in new], log
    hits, mark = [], []
    for r in sorted(new, key=lambda x: (x["date"], x["acc"])):
        try:
            h = classify(cfg, r, fetch)
        except Exception as exc:  # noqa: BLE001
            log.append(f"detail fetch failed, retry next run: {r['form']} {r['acc']} {exc}")
            continue
        mark.append(r["acc"])
        log.append(f"{r['date']} {r['form']} [{r['items']}] {r['acc']} -> " + (f"p{h['prio']} {h['label']}" if h else "ignore"))
        if h:
            hits.append(h)
    return hits, mark, log


def sec_message(cfg: dict, hits: list[dict]) -> tuple[str, str, int]:
    hits = sorted(hits, key=lambda h: (-h["prio"], h["date"]))
    top, prio = hits[0], hits[0]["prio"]
    more = f" 외 {len(hits) - 1}건" if len(hits) > 1 else ""
    if any(h["warn"] for h in hits):
        act = "원문 확인, 추가매수 보류"
        todo = ("EDGAR 원문을 먼저 읽으세요. 관망 유지, 추가매수 보류. 희석(신주, 전환사채 전환가), 펀딩 조건, "
                "Horizon 2~4 일정과 가이던스에 주는 영향부터 확인.")
    else:
        act = "원문 확인"
        todo = "EDGAR 원문에서 계약 상대, 금액, 기간, 선수금(프리페이먼트)이 있는지 확인하세요. 계약 공시 한 건으로 계획을 바꾸지 마세요."
    state = " ".join(f"{i}) p{h['prio']} {h['detail']} 서류 {h['form']}, 항목 {h['items'] or '없음'}, 제출일 {h['date']}, EDGAR {h['url']}"
                     for i, h in enumerate(hits, 1))
    lines = [
        f"{datetime.now(KST):%Y-%m-%d %H:%M} KST | IREN | SEC 공시(보유 없음, 관심 종목)",
        "이게 뭐 알림: IREN SEC 공시 중 판단에 중요한 종류(중요 8-K, 증권 발행과 전환사채, 13D, 공동 CEO 대규모 매도)만 골라 보내는 [IREN-PX] 공시 감시입니다. 실적 공시는 [IREN-EARN] 이 따로 봅니다.",
        f"지금 결과/상태: 새 공시 {len(hits)}건. {state}",
        "지금 할 일: " + todo,
        f"나중에: 같은 접수번호는 다시 오지 않습니다. 종가 구간 알림({LINES})은 따로 계속 봅니다.",
        "익절/줄일 때: 보유 없음이라 해당 없음. 소규모 포지션이 있다면 대규모 희석이나 Horizon 지연 공시가 나올 때 비중부터 점검.",
        "하지 말 것: 공시 제목만 보고 바로 주문, 장 시작 직후 추격, 신용/미수. 투자 권유 아님.",
    ]
    return f"{P} SEC {top['label']}{more} | {act}", "\n".join(lines), prio


def sec_part(cfg: dict, st: dict, now: datetime, dry: bool) -> None:
    try:
        subs = json.loads(sec_get(cfg, cfg["sec"]["submissions"]))
    except Exception as exc:  # noqa: BLE001
        print(f"sec submissions fetch failed: {exc}")
        today = now.date().isoformat()
        if st.get("sec_fail_date") != today and now.date() >= date.fromisoformat(cfg["sec"]["start_date_kst"]):
            title, msg = f"{P} SEC 조회 실패 | 매매 신호 아님", f"IREN EDGAR submissions 조회 실패: {exc}. 다음 09:00 실행에서 다시 봅니다."
            if dry:
                print(f"DRY RUN would send (ops): {title}\n{msg}")
            else:
                publish(resolve_topic("ops"), title, msg, 3, config_name="irenpx")
                st["sec_fail_date"] = today
        return
    hits, mark, log = sec_eval(cfg, st, subs, now.date(), lambda u: sec_get(cfg, u))
    for ln in log:
        print("  sec", ln)
    if hits:
        title, msg, prio = sec_message(cfg, hits)
        if dry:
            print(f"DRY RUN would send (p{prio}): {title}\n{msg}")
        else:
            publish(resolve_topic("port"), title, msg, prio, tags=["page_facing_up"], config_name="irenpx")
    elif not log:
        print("  sec: no new filings")
    st["sec_seen"] = sorted(set(st.get("sec_seen") or []) | set(mark))
    st["sec_checked"] = now.isoformat(timespec="seconds")


def price_part(cfg: dict, st: dict, now: datetime, mode: str, dry: bool) -> None:
    try:
        bars = fetch_bars(cfg, now.astimezone(NY))
    except Exception as exc:  # noqa: BLE001
        print(f"bars fetch failed: {exc}")
        if not st.get("fail_notified"):
            title, msg = f"{P} 시세 조회 실패 | 매매 신호 아님", f"IREN 일봉 조회 실패: {exc}. 다음 09:00 실행에서 다시 봅니다."
            if dry:
                print(f"DRY RUN would send (ops): {title}\n{msg}")
            else:
                publish(resolve_topic("ops"), title, msg, 3, config_name="irenpx")
                st["fail_notified"] = True
        return
    if not bars:
        print("no completed bars")
        return
    st["fail_notified"] = False
    L = cfg["levels"]
    last_d, last_c = bars[-1]
    if (now.date() - last_d).days > int(cfg.get("stale_days", 6)):
        print(f"stale bar {last_d}; skip")
        return
    armed = st.get("armed") if st.get("initialized") else None
    if armed is None:
        armed, _ = step(L, None, last_c)
        st.update({"initialized": True, "armed": armed, "last_date": str(last_d), "last_close": round(last_c, 4),
                   "init_note": f"initialized from {last_d} close {last_c:.2f}, no send"})
        print(f"init from {last_d} close {last_c:.2f}: {armed} (no send)")
        return
    prev_date = st.get("last_date")
    new_bars = [(d, c) for d, c in bars if prev_date is None or str(d) > prev_date]
    if mode == "force" and not new_bars:
        new_bars = bars[-1:]
    if not new_bars:
        print(f"no new bar after {prev_date}; armed={armed}; last close {last_c:.2f} ({last_d})")
        return
    fired_all: list[str] = []
    prev_close = st.get("last_close")
    for d, c in new_bars:
        armed, fired = step(L, armed, c)
        print(f"bar {d} close {c:.2f} -> fired={fired} armed={armed}")
        fired_all += [k for k in fired if k not in fired_all]
    d, c = new_bars[-1]
    if len(new_bars) > 1:
        prev_close = new_bars[-2][1]
    for lv in L:
        print(f"  line {lv['price']:.2f} ({lv['label']}): {'armed' if armed[lv['key']] else 'inside zone'}, distance {(c / lv['price'] - 1) * 100:+.1f}%")
    if fired_all:
        title, msg, prio = message(cfg, fired_all, c, str(d), prev_close)
        if dry:
            print(f"DRY RUN would send (p{prio}): {title}\n{msg}")
        else:
            tag = "chart_with_upwards_trend" if fired_all == ["high35"] else "warning"
            publish(resolve_topic("port"), title, msg, prio, tags=[tag], config_name="irenpx")
    else:
        print("no zone entry; silent")
    st.update({"armed": armed, "last_date": str(d), "last_close": round(c, 4)})


# ---------- backtest (no send, no state) ----------

def backtest(cfg: dict, days: int) -> int:
    cache = Path("/tmp/irenpx_bt"); cache.mkdir(exist_ok=True)

    def fetch(u: str) -> str:
        p = cache / re.sub(r"[^A-Za-z0-9.]", "_", u)[-150:]
        if p.exists():
            return p.read_text(encoding="utf-8")
        t = sec_get(cfg, u)
        p.write_text(t, encoding="utf-8")
        return t
    subs = json.loads(fetch(cfg["sec"]["submissions"]))
    since = (datetime.now(KST).date() - timedelta(days=days)).isoformat()
    rows = [r for r in sec_rows(subs) if r["date"] >= since]
    hits, ign = [], {}
    for r in sorted(rows, key=lambda x: (x["date"], x["acc"])):
        h = classify(cfg, r, fetch)
        if h:
            hits.append(h)
            print(f"  {r['date']} p{h['prio']} {r['form']:<8} [{r['items']}] {h['label']} :: {h['detail'][:110]}")
        else:
            ign[r["form"]] = ign.get(r["form"], 0) + 1
    days_ = sorted({h["date"] for h in hits})
    print(f"since {since}: {len(rows)} filings, {len(hits)} whitelist hits "
          f"(p4 {sum(h['prio'] == 4 for h in hits)}, p3 {sum(h['prio'] == 3 for h in hits)}), {len(days_)} distinct filing days")
    print("ignored:", dict(sorted(ign.items(), key=lambda x: -x[1])))
    return 0


# ---------- selftest ----------

F4_TMPL = """<ownershipDocument><reportingOwner><reportingOwnerId><rptOwnerName>{name}</rptOwnerName></reportingOwnerId>
<reportingOwnerRelationship><isOfficer>1</isOfficer><officerTitle>{title}</officerTitle></reportingOwnerRelationship></reportingOwner>
<nonDerivativeTable>{tx}</nonDerivativeTable></ownershipDocument>"""
TX = """<nonDerivativeTransaction><transactionCoding><transactionCode>{c}</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>{sh}</value></transactionShares><transactionPricePerShare><value>{px}</value></transactionPricePerShare></transactionAmounts></nonDerivativeTransaction>"""


def f4(name: str, title: str, txs: list[tuple]) -> str:
    return F4_TMPL.format(name=name, title=title, tx="".join(TX.format(c=c, sh=sh, px=px) for c, sh, px in txs))


def selftest() -> int:
    cfg = load_cfg()
    L = cfg["levels"]
    fails = 0

    def check(label, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(("ok  " if ok else "FAIL"), label, "" if ok else f"got {got!r} want {want!r}")

    A = {"low2372": True, "low20": True, "high35": True}
    check("init 26.25 all armed, no send", step(L, None, 26.25), (A, []))
    check("init 22 no send", step(L, None, 22.0), ({"low2372": False, "low20": True, "high35": True}, []))
    check("23.73 nothing", step(L, A, 23.73)[1], [])
    check("23.72 low (<=)", step(L, A, 23.72)[1], ["low2372"])
    check("20.00 gap both", step(L, A, 20.0)[1], ["low2372", "low20"])
    check("gap -> only 20 warning sent", effective(["low2372", "low20"]), ["low20"])
    check("20.01 only 23.72", step(L, A, 20.01)[1], ["low2372"])
    check("34.99 nothing", step(L, A, 34.99)[1], [])
    check("35.00 rebound (>=)", step(L, A, 35.0)[1], ["high35"])
    walk = [(23.5, ["low2372"]), (23.0, []), (23.72, []), (23.73, []), (23.6, ["low2372"]),
            (19.9, ["low20"]), (19.0, []), (20.01, []), (20.0, ["low20"]), (26.0, []),
            (35.5, ["high35"]), (36.0, []), (34.99, []), (35.0, ["high35"]), (21.0, ["low2372"])]
    st = dict(A)
    for i, (c, want) in enumerate(walk):
        st, f = step(L, st, c)
        check(f"walk{i} {c}", f, want)
    msgs = {}
    for fired, c in ((["low2372"], 23.5), (["low2372", "low20"], 19.5), (["low20"], 19.9), (["high35"], 35.4)):
        t, b, p = message(cfg, fired, c, "2026-11-06", 25.0)
        msgs[tuple(fired)] = (t, b, p)
        print(f"  p{p} {t}")
        check(f"{fired} no middle dot / em-dash", any(ch in t + b for ch in BANNED), False)
        check(f"{fired} six parts", [x.split(":")[0] for x in b.split("\n")[1:7]],
              ["이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"])
        check(f"{fired} prefix", t.startswith(P + " "), True)
    for k in (("low2372",), ("low2372", "low20"), ("low20",)):
        t, b, p = msgs[k]
        check(f"warning {k} no buy wording", [w for w in BUY_WORDS if w in t + b], [])
        check(f"warning {k} 추가매수 보류 + 관망", "추가매수 보류" in t and "관망 유지" in b, True)
    check("gap msg = 20 warning p4", (msgs[("low2372", "low20")][2], "재평가 구간" in msgs[("low2372", "low20")][0], "52주 저가 이탈 |" in msgs[("low2372", "low20")][0]), (4, True, False))
    check("20 warning re-check items", all(x in msgs[("low20",)][1] for x in ("Horizon 2~4", "펀딩과 희석", "가이던스")), True)
    check("23.72 p3", msgs[("low2372",)][2], 3)
    check("35 p3 추격 매수 보류 checklist", (msgs[("high35",)][2], "추격 매수 보류" in msgs[("high35",)][0], "반등 이유" in msgs[("high35",)][1]), (3, True, True))
    t, b = confirm_message(cfg, 26.25, "2026-10-29")
    check("confirm title", t, "[IREN-PX] 설치 확인 | 매매 신호 아님")
    check("confirm no dot/dash", any(ch in t + b for ch in BANNED), False)

    # SEC classifier
    docs = {
        "ceo502.htm": "<p>Item 5.02 Departure of Directors or Certain Officers. Daniel Roberts will step down as Co-Chief Executive Officer.</p><p>Item 9.01</p>",
        "dir502.htm": "<p>Item 5.02 Departure of Directors. Ms. X resigned from the Board.</p><p>Item 9.01 Exhibits</p>",
        "conv.htm": "<p>Item 1.01 Entry into a Material Definitive Agreement. Indenture for 0.25% Convertible Senior Notes due 2032.</p>",
        "conv801.htm": "<p>Item 8.01 Other Events. IREN announced pricing of $1.0 billion convertible notes offering.</p>",
        "plain801.htm": "<p>Item 8.01 Other Events. IREN published a monthly update.</p>",
        "debt.htm": "<p>Item 1.01 ... Item 2.03 ... GPU financing facility with a lender.</p>",
        "ceobig.xml": f4("Roberts Daniel John", "Co-Chief Executive Officer", [("S", 400000, 26.0)]),
        "ceosmall.xml": f4("Roberts William Gregory", "Co-Chief Executive Officer", [("S", 300000, 26.0)]),
        "dirbig.xml": f4("Guzowski Christopher", "", [("S", 1000000, 26.0)]),
        "ceoaward.xml": f4("Roberts Daniel John", "Co-Chief Executive Officer", [("A", 1000000, 0)]),
        "cfo.xml": f4("Lewis Anthony J", "Chief Financial Officer", [("S", 600000, 26.0)]),
    }

    def fetch(u):
        n = u.rsplit("/", 1)[1]
        if n not in docs:
            raise OSError("404 " + n)
        return docs[n]

    def Fl(form, acc, items="", doc="x.htm", d="2026-11-09"):
        return {"form": form, "date": d, "acc": acc, "items": items, "doc": doc}
    cases = [
        (Fl("8-K", "a1", "1.01,9.01", "plain801.htm"), 4), (Fl("8-K", "a2", "1.02"), 4), (Fl("8-K", "a3", "2.01,9.01"), 4),
        (Fl("8-K", "a4", "2.05"), 4), (Fl("8-K", "a5", "2.06"), 4), (Fl("8-K", "a6", "3.01"), 4), (Fl("8-K", "a7", "4.01,9.01"), 4),
        (Fl("8-K", "a8", "4.02"), 4), (Fl("8-K", "a9", "5.01"), 4),
        (Fl("8-K", "b1", "5.02,9.01", "ceo502.htm"), 4), (Fl("8-K", "b2", "5.02", "dir502.htm"), 3), (Fl("8-K", "b3", "5.02", "missing.htm"), 3),
        (Fl("8-K", "b4", "8.01,9.01", "plain801.htm"), 3), (Fl("8-K", "b5", "8.01,9.01", "conv801.htm"), 3),
        (Fl("8-K", "b6", "1.01,2.03,3.02,8.01,9.01", "conv.htm"), 4), (Fl("8-K", "b7", "2.03", "debt.htm"), 3),
        (Fl("8-K", "b8", "3.03,5.02,5.07,9.01", "dir502.htm"), 3),
        (Fl("8-K", "c1", "2.02,9.01"), None), (Fl("8-K", "c2", "7.01,9.01"), None), (Fl("8-K", "c3", "5.07"), None),
        (Fl("S-3ASR", "d1"), 4), (Fl("424B5", "d2"), 4), (Fl("424B7", "d3"), 4), (Fl("S-1", "d4"), 4),
        (Fl("SCHEDULE 13D", "d5"), 3), (Fl("SC 13D/A", "d6"), 3),
        (Fl("SCHEDULE 13G", "e1"), None), (Fl("144", "e2"), None), (Fl("S-8", "e3"), None), (Fl("10-Q", "e4"), None),
        (Fl("10-K", "e5"), None), (Fl("FWP", "e6"), None), (Fl("DEF 14A", "e7"), None), (Fl("D", "e8"), None),
        (Fl("4", "f1", doc="xslF345X05/ceobig.xml"), 3), (Fl("4", "f2", doc="ceosmall.xml"), None),
        (Fl("4", "f3", doc="dirbig.xml"), None), (Fl("4", "f4", doc="ceoaward.xml"), None), (Fl("4", "f5", doc="cfo.xml"), None),
    ]
    found = {}
    for f, want in cases:
        h = classify(cfg, f, fetch)
        found[f["acc"]] = h
        check(f"{f['form']} [{f['items']}] {f['doc']}", h["prio"] if h else None, want)
    check("conv 8.01 labelled", found["b5"]["label"], "8-K 전환사채 관련")
    check("conv 1.01 labelled + warn", (found["b6"]["label"], found["b6"]["warn"]), ("8-K 전환사채 관련", True))
    check("plain 1.01 not conv", found["a1"]["label"].startswith("8-K 1.01"), True)
    thr = float(cfg["sec"]["ceo_sale_usd"])
    docs["edge0.xml"] = f4("Roberts Daniel John", "Co-Chief Executive Officer", [("S", thr / 20, 20)])
    docs["edge1.xml"] = f4("Roberts Daniel John", "Co-Chief Executive Officer", [("S", thr / 20 + 1, 20)])
    check("ceo sale == $10M ignored", classify(cfg, Fl("4", "g0", doc="edge0.xml"), fetch), None)
    check("ceo sale > $10M p3", (classify(cfg, Fl("4", "g1", doc="edge1.xml"), fetch) or {}).get("prio"), 3)
    try:
        classify(cfg, Fl("4", "g2", doc="boom.xml"), fetch)
        check("form4 fetch error raises", False, True)
    except OSError:
        check("form4 fetch error raises", True, True)

    subs = {"filings": {"recent": {"form": ["8-K", "4", "144", "424B5"], "filingDate": ["2026-11-09"] * 4,
                                   "accessionNumber": ["n1", "n2", "n3", "n4"], "items": ["4.02,9.01", "", "", ""],
                                   "primaryDocument": ["x.htm", "boom.xml", "x.txt", "p.htm"]}}}
    h, mark, _ = sec_eval(cfg, {}, subs, date(2026, 11, 9), fetch)
    check("seed: no hits, all marked", (h, sorted(mark)), ([], ["n1", "n2", "n3", "n4"]))
    h, mark, _ = sec_eval(cfg, {"sec_seen": []}, subs, date(2026, 10, 30), fetch)
    check("before start: silent, marked", (h, sorted(mark)), ([], ["n1", "n2", "n3", "n4"]))
    h, mark, _ = sec_eval(cfg, {"sec_seen": []}, subs, date(2026, 11, 2), fetch)
    check("start: p4 x2, failed Form 4 left for retry", (sorted(x["prio"] for x in h), sorted(mark)), ([4, 4], ["n1", "n3", "n4"]))
    check("dedupe", sec_eval(cfg, {"sec_seen": ["n1", "n3", "n4"]}, subs, date(2026, 11, 2), fetch)[:2], ([], []))
    t, b, p = sec_message(cfg, h)
    check("combined p4 + hold-off title", (p, t.endswith("외 1건 | 원문 확인, 추가매수 보류"), t.startswith("[IREN-PX] SEC ")), (4, True, True))
    allm = [sec_message(cfg, [x]) for x in found.values() if x] + [(t, b, p)]
    for t, b, p in allm:
        check(f"sec no dot/dash {t[:34]}", any(ch in t + b for ch in BANNED), False)
        check(f"sec six parts {t[:34]}", [x.split(":")[0] for x in b.split("\n")[1:7]],
              ["이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"])
        if "보류" in t:
            check(f"sec warn no buy wording {t[:34]}", [w for w in BUY_WORDS if w in t + b], [])
    for acc in ("a7", "b6", "d2", "f1", "d5"):
        check(f"warning hold-off {acc}", "추가매수 보류" in sec_message(cfg, [found[acc]])[0], True)
    check("ends_kst set", cfg["ends_kst"], "2027-10-31 23:59")
    check("valid_until", cfg["valid_until"], "2027-10-31")
    print("selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


# ---------- main ----------

def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode == "selftest":
        return selftest()
    cfg = load_cfg()
    if mode == "backtest":
        return backtest(cfg, int(sys.argv[2]) if len(sys.argv) > 2 else 365)
    dry = mode == "dry"
    now = datetime.now(KST)
    print(f"{now.isoformat(timespec='seconds')} irenpx mode={mode}")
    if dry:
        print("DRY RUN (no ntfy, no state write)")
    if mode == "confirm":
        try:
            d, c = fetch_bars(cfg, now.astimezone(NY))[-1]
            title, msg = confirm_message(cfg, c, str(d))
        except Exception as exc:  # noqa: BLE001
            print(f"bars fetch failed: {exc}")
            title, msg = confirm_message(cfg, None, None)
        print(title)
        print(msg)
        publish(resolve_topic("port"), title, msg, 3, tags=["white_check_mark"], config_name="irenpx")
        return 0
    vu = cfg.get("valid_until")
    if vu and now.date() > date.fromisoformat(vu):
        print(f"expired (valid_until {vu}); silent")
        return 0
    st = load_state()
    price_part(cfg, st, now, mode, dry)
    sec_part(cfg, st, now, dry)
    save_state(st, dry)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
