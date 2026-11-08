#!/usr/bin/env python3
"""[LGES] LG에너지솔루션 (KRX 373220) watch (approved in chat 2026-10-26).

1. Price zones (configs levels): close <= 330,000 '매수 매력 구간 진입', and (moved from [LGESH] lgeshold
   2026-10-26) <= 300,000 손절/재검토, 360,000~400,000 추가매수, >= 500,000 / >= 550,000 목표. Priority 4.
   Each line fires once when the close ENTERS its zone, quietly re-arms when a close leaves it.
   First run (no state) only records the current close. Dedupe by trading date (state last_date).
   Same pattern as figpx_monitor.py. Prices: Naver mobile daily prices (same source as lgesfor/k079900).
2. Filing watch: KIND company disclosure list (kind.krx.co.kr, no API key; same request as k079900).
   Sends titles matching configs filings.rules (조회공시/풍문/해명/불성실공시, 영업(잠정)실적, 횡령/배임,
   단일판매/공급계약, 주요사항보고서, 분기/반기/사업보고서). Dedupe by KIND receipt number (seen_acptno).
   2026-11-03: px350 line (<= 350,000, LG화학 EB 교환가 337,700 / PRS 347,500) uses basis "low": the zone test
   takes min(close, day low) so an intraday trade at/below the line also counts; re-arms on a day whose low is above.
   First run seeds the current list without sending.
   2026-11-03: contract filings fetch the KIND body; ESS-related AND large (amount >= 1조원 or >= 5% of 최근 매출액,
   filings.rules contract.ess_large) -> p5 'ESS 대형 수주 (정정/해지) 공시' title. Everything else stays p3. Body fetch or
   amount parse failure = not large, body says '금액 확인 필요' (fail open, the filing is still sent).
3. Q4 2026 checklist: a new 영업(잠정)실적 filing inside q4.window (2027-01-01..02-28) also sends the
   checklist once (q4_checklist_sent). The prelim notice and that checklist append a reference line
   (세전손익, 연결 당기순손익, 지배/비지배, 일회성) from the KIND body. A body failure says 확인 불가
   and still sends. The line is not a buy signal and does not change priority or the checklist.

Runs on the existing k203780 19:00 KST slot (KRX trading days). No new cron.
Modes: auto | force (re-evaluate the last bar even if seen) | dry (print only, no ntfy, no state)
       | confirm (install-confirmation send, no state) | selftest.
Not investment advice.
"""
from __future__ import annotations

import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_topic  # noqa: E402
from earnings_parse import safe_reference  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG = ROOT / "configs" / "lgeswatch.json"
STATE = ROOT / "state" / "lgeswatch.json"
UA = "Mozilla/5.0 (X11; Linux x86_64) cwally-alerts"
KIND = "https://kind.krx.co.kr"
LOWS: dict[str, float] = {}  # KST date -> day low, filled by fetch_bars (for basis "low" lines)


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
        return
    st["updated_at"] = datetime.now(KST).isoformat(timespec="seconds")
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def http(url: str, data: dict | None = None, headers: dict | None = None, tries: int = 3, timeout: int = 30) -> str:
    h = {"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"}
    h.update(headers or {})
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    if body is not None:
        h["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
    err: Exception | None = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=h), timeout=timeout) as resp:
                raw = resp.read()
                ctype = (resp.headers.get("Content-Type") or "").lower()
            if "euc-kr" in ctype:
                return raw.decode("euc-kr", errors="replace")
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return raw.decode("euc-kr", errors="replace")
        except Exception as exc:  # noqa: BLE001
            err = exc
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"{url.split('?')[0]}: {err}")


def text_of(page: str) -> str:
    t = re.sub(r"(?s)<(script|style)[^>]*>.*?</\1>", " ", page)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", html.unescape(t)).strip()


def clean(t: str) -> str:
    """KIND titles use the Hangul dot (U+318D) and sometimes U+00B7; the copy rule bans middle dots."""
    return t.replace("\u318d", "/").replace("\u00b7", "/").replace("\u2014", "-")


# ---------- data ----------

def fetch_bars(cfg: dict, now: datetime) -> list[tuple[str, float]]:
    """Completed regular-session closes, ascending. Today's bar only after market_close_kst."""
    import krx_close
    hh, mm = map(int, str(cfg.get("market_close_kst", "15:40")).split(":"))
    bars = krx_close.completed(krx_close.regular_bars(cfg["code"], 25), now, (hh, mm))
    if not bars:
        raise RuntimeError("시세 0건")
    LOWS.clear()
    for b in bars:
        if b.low and b.low > 0:
            LOWS[b.day] = float(b.low)
    return [(b.day, b.close) for b in bars]


def kind_list(cfg: dict, since: str, until: str) -> list[dict]:
    form = {
        "method": "searchDetailsSub", "currentPageSize": "100", "pageIndex": "1", "orderMode": "1", "orderStat": "D",
        "forward": "details_sub", "searchCodeType": "", "repIsuSrtCd": "A" + cfg["code"], "allRepIsuSrtCd": "",
        "oldSearchCorpName": "", "disclosureType": "", "disTypevalue": "", "reportNm": "", "reportCd": "",
        "searchCorpName": cfg["code"], "business": "", "marketType": "", "settlementMonth": "", "securities": "",
        "submitOblgNm": "", "enterprise": "", "fromDate": since, "toDate": until, "reportNmTemp": "", "bfrDsclsType": "on",
    }
    page = http(KIND + "/disclosure/details.do", data=form,
                headers={"X-Requested-With": "XMLHttpRequest", "Referer": KIND + "/disclosure/details.do?method=searchDetailsMain"})
    out, rows = [], 0
    for row in re.findall(r"(?s)<tr[^>]*>(.*?)</tr>", page):
        acpt = re.search(r"openDisclsViewer\('(\d{14})'", row)
        if not acpt:
            continue
        rows += 1
        cells = [text_of(td) for td in re.findall(r"(?s)<td[^>]*>(.*?)</td>", row)]
        # cells: no, "YYYY-MM-DD HH:MM", company, title, submitter, charts
        if len(cells) < 5 or cfg["name_kr"] not in cells[2] or not re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", cells[1]):
            continue
        out.append({"acptno": acpt.group(1), "filed": cells[1][:16], "title": cells[3], "submitter": cells[4]})
    if rows == 0 and "조회된 결과값이 없습니다" not in page and "<table" not in page:
        raise RuntimeError("KIND 응답 형식 이상")
    return out


# ---------- core (pure) ----------

def hit(level: dict, close: float, low: float | None = None) -> bool:
    if level.get("basis") == "low" and low is not None:
        close = min(float(close), float(low))  # close OR intraday trade at/below the line
    op = level["op"]
    if op == "between":
        return float(level["low"]) <= close <= float(level["high"])
    p = float(level["price"])
    return close <= p if op == "le" else close >= p if op == "ge" else close < p


def step(level: dict, armed: bool | None, close: float, low: float | None = None) -> tuple[bool, bool]:
    """One line: (new armed, fired). armed None = first run: init only."""
    inside = hit(level, round(float(close), 2), low)
    if armed is None:
        return (not inside), False
    if armed and inside:
        return False, True
    if not armed and not inside:
        return True, False
    return armed, False


def step_all(levels: list[dict], armed: dict | None, close: float, low: float | None = None) -> tuple[dict, list[str]]:
    new, fired = {}, []
    for lv in levels:
        a, f = step(lv, None if armed is None else armed.get(lv["key"]), close, low)
        if armed is not None and lv["key"] not in armed:
            a, f = step(lv, None, close, low)  # line added later: init quietly
        new[lv["key"]] = a
        if f:
            fired.append(lv["key"])
    return new, fired


def line_text(lv: dict) -> str:
    if lv["op"] == "between":
        return f"{won(lv['low'])}~{won(lv['high'])}"
    return f"{won(lv['price'])} {'이상' if lv['op'] == 'ge' else '이하' if lv['op'] == 'le' else '미만'}"


def kind_body(acpt: str) -> str:
    """KIND filing body text (same 3-step viewer request as secdiv/steelre kind_body)."""
    viewer = http(cfg_view(acpt))
    m = re.search(r"<option value='(\d+)\|Y'", viewer) or re.search(r"<option value='(\d+)\|", viewer)
    if not m:
        raise RuntimeError("KIND 본문 문서번호 없음")
    sc = http(f"{KIND}/common/disclsviewer.do?method=searchContents&docNo={m.group(1)}")
    p = re.search(r"setPath\('[^']*','([^']+\.htm)'", sc)
    if not p:
        raise RuntimeError("KIND 본문 경로 없음")
    return text_of(http(p.group(1), timeout=60))


def cfg_view(acpt: str) -> str:
    return f"{KIND}/common/disclsviewer.do?method=search&acptno={acpt}"


def _num(tok: str | None) -> float | None:
    if tok is None or tok.strip() in {"-", ""}:
        return None
    try:
        return float(tok.replace(",", ""))
    except ValueError:
        return None


def parse_contract(text: str) -> dict | None:
    """단일판매/공급계약 체결 or 해지 body -> amount/sales/ratio/name/party/period. Uses the LAST form section
    (a 정정 body repeats the corrected full form after the 정정사항 table). None = not parseable."""
    t = re.sub(r"\s+", " ", text or "")
    anchors = [m.start() for m in re.finditer(r"단일판매\s*[\u318d\u00b7]?\s*공급계약\s*(?:체결|해지)\s*1\.\s*판매", t)]
    if not anchors:
        return None
    s = t[anchors[-1]:]

    def g(pat: str) -> str | None:
        m = re.search(pat, s)
        return m.group(1).strip() if m else None
    out = {
        "kind": "해지" if re.match(r"단일판매\s*[\u318d\u00b7]?\s*공급계약\s*해지", s) else "체결",
        "name": g(r"(?:체결|해지)계약명\s*(.+?)\s*2\.\s*(?:계약|해지)내역"),
        "amount_raw": g(r"(?:계약|해지)금액\s*\(원\)\s*(-|[\d,]+)"),
        "sales": _num(g(r"최근\s*매출액\s*\(원\)\s*(-|[\d,]+)")),
        "ratio": _num(g(r"매출액\s*대비\s*\(%\)\s*(-|[\d.]+)")),
        "party": g(r"3\.\s*계약상대(?:방)?\s*(.+?)\s*-\s*회사와(?:의)?\s*관계"),
        "start": g(r"계약기간\s*시작일\s*(\S+)"), "end": g(r"계약기간\s*시작일\s*\S+\s*종료일\s*(\S+)"),
    }
    out["amount"] = _num(out["amount_raw"])
    if out["ratio"] is None and out["amount"] and out["sales"]:
        out["ratio"] = round(out["amount"] / out["sales"] * 100, 1)
    if out["amount"] is None and out["name"] is None and out["party"] is None:
        return None
    return out


def contract_info(cfg: dict, title: str, body: str | None, err: str | None = None) -> dict:
    """ESS/large verdict for one contract filing. Fail open: no body or no amount -> not large + 금액 확인 필요."""
    rule = next(r for r in cfg["filings"]["rules"] if r["key"] == "contract")
    el = rule["ess_large"]
    p = parse_contract(body) if body else None
    ess = bool(re.search(el["ess_re"], (title or "") + " " + (body or ""), re.I))
    amt, ratio = (p or {}).get("amount"), (p or {}).get("ratio")
    large = bool(amt is not None and (amt >= float(el["min_amount_won"]) or (ratio is not None and ratio >= float(el["min_ratio_pct"]))))
    if p is None:
        why = err or ("본문 없음" if not body else "본문 파싱 실패")
    elif amt is None:
        why = "계약금액 유보 또는 미기재"
    else:
        why = None
    corr = "정정" in (title or "")
    kind = "해지" if ("해지" in (title or "") or (p or {}).get("kind") == "해지") else "체결"
    return {"p": p, "ess": ess, "large": large, "ess_large": ess and large, "check_amount": why,
            "corr": corr, "kind": kind, "priority": int(el["priority"]) if ess and large else int(rule["priority"])}


def big_won(v: float) -> str:
    return f"{v / 1e12:.2f}조원" if v >= 1e12 else f"{v / 1e8:,.0f}억원"


def contract_line(cfg: dict, ci: dict) -> str:
    el = next(r for r in cfg["filings"]["rules"] if r["key"] == "contract")["ess_large"]
    p = ci["p"] or {}
    parts = []
    if p.get("name"):
        parts.append(f"계약명 {p['name']}")
    if p.get("amount") is not None:
        parts.append(f"{'해지' if ci['kind'] == '해지' else '계약'}금액 {big_won(p['amount'])}" + (f"(최근 매출액 대비 {p['ratio']}%)" if p.get("ratio") is not None else ""))
    if p.get("party") and p["party"] not in {"-", ""}:
        parts.append(f"계약상대 {p['party']}")
    if p.get("start") and p.get("end"):
        parts.append(f"기간 {p['start']}~{p['end']}")
    parts.append("ESS 언급 있음" if ci["ess"] else "ESS 언급 없음")
    parts.append(f"대형 기준({float(el['min_amount_won']) / 1e12:g}조원 이상 또는 매출액 대비 {el['min_ratio_pct']:g}% 이상) {'충족' if ci['large'] else '미충족'}")
    if ci["check_amount"]:
        parts.append(f"금액 확인 필요({ci['check_amount']}), 원문에서 직접 확인")
    return ", ".join(parts) + "."


def contract_label(rule: dict, title: str) -> str:
    """Title label. The contract rule covers 체결, 해지 and their 정정; say which one (2026-11-03 fix: a p3 해지
    filing used to read '단일판매/공급계약 체결'). Other rules keep their label."""
    if rule.get("key") != "contract":
        return rule["label"]
    t = title or ""
    base = "단일판매/공급계약 해지" if "해지" in t else "단일판매/공급계약 체결"
    return base + (" 정정" if "정정" in t else "")


def classify(cfg: dict, title: str) -> dict | None:
    for r in cfg["filings"]["rules"]:
        if re.search(r["re"], title):
            return r
    return None


def in_q4(cfg: dict, filed: str) -> bool:
    q = cfg["q4"]
    return q["window_start"] <= filed[:10] <= q["window_end"]


def won(v: float) -> str:
    return f"{v:,.0f}원"


BUY_LEAN = "논리가 그대로면 계획 금액을 나눠서만 검토"
STOP_TODO = "추가매수 보류, AMPC 제외 영업이익과 ESS 수주부터 재확인해 투자 논리 점검."
ACTION = {"buy330": "논리 확인 뒤 분할", "stop300": "비중 재평가", "add360": "분할만, 추격 금지", "tgt500": "일부 익절 검토", "tgt550": "추가 익절 검토", "px350": "교환가/PRS 기준가 근접, 분할만"}


def price_msg(cfg: dict, fired: list[str], close: float, day: str, prev: float | None, low: float | None = None) -> tuple[str, str, int]:
    lvs = {x["key"]: x for x in cfg["levels"]}
    top = max((lvs[k] for k in fired), key=lambda x: (x["priority"], x["key"] == "stop300"))  # stop wins a tie
    ref = cfg["reference"]
    chg = f", 전 거래일 대비 {(close / prev - 1) * 100:+.1f}%" if prev else ""
    labels = " + ".join(lvs[k]["label"] for k in fired)
    low_txt = f", 장중 저가 {won(low)}" if low is not None and any(lvs[k].get("basis") == "low" for k in fired) else ""
    title = f"[LGES] 373220 LG에너지솔루션 종가 {won(close)} {top['label']} | {ACTION.get(top['key'], '확인')}"
    all_lines = ", ".join(f"{x['label'].replace(' 구간 진입', '')} {line_text(x)}" for x in cfg["levels"])
    lines = [
        f"{datetime.now(KST):%Y-%m-%d %H:%M} KST | 373220 | LG에너지솔루션",
        "이게 뭐 알림: " + cfg["explain"],
        f"지금 결과/상태: {labels}. KRX 종가 {won(close)}({day}{chg}{low_txt}). 선: {all_lines}. 52주 최고 {won(ref['high_52w'])}. {cfg['sources'].get('stock_page', '')}".rstrip(),
        "지금 할 일: " + (STOP_TODO if "stop300" in fired else " ".join(lvs[k]["note"] for k in fired) + " 논리가 그대로면 계획 금액을 나눠서만 검토."),
        "나중에: 같은 선은 종가가 그 구간을 벗어난 뒤 다시 들어올 때만 또 울립니다(35만원 선은 저가가 350,000원 위인 날이 나온 뒤). 4분기 잠정실적(2027년 1월 초)에서 AMPC 제외 영업이익 3,000억원 이상인지 확인.",
        "익절/줄일 때: AMPC 요건 강화/FEOC 규정, ESS 수주나 마진 미달이면 비중 축소 검토.",
        "하지 말 것: 실적 악화로 빠진 하락을 싼 가격으로 읽기, 일괄 매수, 신용/미수, 이 알림만 보고 주문. 투자 권유 아님.",
    ]
    return title, "\n".join(lines), int(top["priority"])


def prelim_ref(f: dict, body: str | None, err: str | None) -> str:
    """Reference line for one prelim filing. Never raises, never turns a read failure into 0."""
    return safe_reference(
        None if err or not (body or "").strip() else body,
        source=f"KIND {f.get('acptno', '')} {clean(f.get('title', ''))}",
        filed=str(f.get("filed") or ""),
        fetch_error=err or (None if (body or "").strip() else "본문 없음"),
    )


def filing_msg(cfg: dict, f: dict, rule: dict, ci: dict | None = None, ref: str | None = None) -> tuple[str, str, int]:
    t = clean(f["title"])
    link = cfg["sources"]["kind_view"].format(acpt=f["acptno"])
    extra = {
        "inquiry": "조회공시는 답변 내용(확정/미확정/사실무근)과 다음 공시 기한을 확인.",
        "prelim": "AMPC 제외 영업이익, ESS 매출과 수주, AMPC 비중을 확인.",
        "embezzle": "금액, 자기자본 대비 비율, 거래정지/불성실공시 여부를 확인.",
        "contract": "계약 금액, 매출액 대비 비율, 상대방(ESS/EV), 기간을 확인.",
        "major": "유상증자, 사채 발행, 자산 양수도 같은 결정인지 확인.",
        "periodic": "AMPC 제외 이익, ESS 수주 잔고, 우발채무 주석을 확인.",
    }.get(rule["key"], "원문을 확인.")
    title = f"[LGES] 373220 LG에너지솔루션 새 공시 {contract_label(rule, f['title'])} | 원문 확인"
    prio = int(rule["priority"])
    status_extra = ""
    if ci is not None:
        status_extra = " " + contract_line(cfg, ci)
        prio = ci["priority"]
        if ci["ess_large"]:
            tag = ("해지 " if ci["kind"] == "해지" else "") + ("정정 " if ci["corr"] else "")
            title = f"[LGES] 373220 LG에너지솔루션 ESS 대형 수주 {tag}공시 {big_won(ci['p']['amount'])} | 원문 확인"
            extra = ("ESS 대형 계약 해지: 해지 사유, 잔여 금액, 수주 잔고 영향을 확인." if ci["kind"] == "해지" else
                     "ESS 대형 수주: 계약상대, 기간, GWh 규모, 연간 ESS 수주 90GWh 목표 대비 비중을 원문으로 확인.")
    ref_txt = ""
    dont = "하지 말 것: 제목만 보고 주문, 장중 급변동 추격. 투자 권유 아님."
    if rule["key"] == "prelim":
        ref_txt = " " + (ref if ref else prelim_ref(f, None, "본문 없음"))
        dont = ("하지 말 것: 제목만 보고 주문, 장중 급변동 추격, "
                "AMPC 제외 영업이익을 보상금 제외 경상 영업이익으로 보기, "
                "세전손익이나 순손익 참고를 매수 조건으로 쓰기. 투자 권유 아님.")
    lines = [
        f"{datetime.now(KST):%Y-%m-%d %H:%M} KST | 373220 | LG에너지솔루션",
        "이게 뭐 알림: " + cfg["explain"],
        f"지금 결과/상태: {f['filed']} 공시 '{t}' (KIND 접수번호 {f['acptno']}).{status_extra} {link}{ref_txt}",
        "지금 할 일: " + extra,
        "나중에: 같은 공시는 다시 보내지 않습니다. 정정 공시는 새 접수번호라 1번 더 옵니다.",
        "익절/줄일 때: 횡령/배임 금액이 크거나 거래정지, 불성실공시법인 지정이 나오면 비중 축소 검토.",
        dont,
    ]
    return title, "\n".join(lines), prio


def q4_msg(cfg: dict, f: dict, ref: str | None = None) -> tuple[str, str, int]:
    link = cfg["sources"]["kind_view"].format(acpt=f["acptno"])
    title = "[LGES] 373220 4분기 잠정실적 확인 | 체크리스트 4개"
    ref_txt = ref if ref else prelim_ref(f, None, "본문 없음")
    lines = [
        f"{datetime.now(KST):%Y-%m-%d %H:%M} KST | 373220 | LG에너지솔루션",
        "이게 뭐 알림: " + cfg["explain"],
        f"지금 결과/상태: {f['filed']} '{clean(f['title'])}' 공시. {cfg['q4']['checklist']} {ref_txt}",
        f"지금 할 일: 원문에서 위 4개를 직접 확인. {link}",
        "나중에: 사업보고서(3월)에서 연간 ESS 수주와 AMPC 금액을 다시 대조.",
        "익절/줄일 때: AMPC 제외 영업이익 3,000억원 미달이나 FEOC/AMPC 불리한 변화면 비중 축소 검토.",
        "하지 말 것: 헤드라인 영업이익(AMPC 포함)만 보고 판단, AMPC 제외 영업이익을 보상금 제외 경상 영업이익으로 보기, 세전손익이나 순손익 참고를 매수 조건으로 쓰기, 실적 당일 추격. 투자 권유 아님.",
    ]
    return title, "\n".join(lines), 4


def confirm_msg(cfg: dict, close: float | None, day: str | None) -> tuple[str, str]:
    px = f"{won(close)}({day} KRX 종가)" if close is not None else "조회 실패(구간 미확인)"
    lines = [
        "[테스트] LGES 알림 설정 완료",
        "이게 뭐 알림: " + cfg["explain"],
        f"지금 결과/상태: 373220 최근 종가 {px}. 선: " + ", ".join(f"{x['label'].replace(' 구간 진입', '')} {line_text(x)}" for x in cfg["levels"]) + ". 공시 감시: KIND 목록, 키 없이 동작.",
        "지금 할 일: 없음. KRX 거래일 19:00 KST 에 판정하고, 구간에 새로 들어가거나 새 중요 공시가 있을 때만 보냅니다. 4분기 잠정실적 공시가 나오면 체크리스트를 1번 보냅니다.",
        "나중에: 같은 선은 종가가 그 구간을 벗어난 뒤 다시 들어올 때만 또 울립니다.",
        "익절/줄일 때: AMPC 요건 강화/FEOC 규정, ESS 수주나 마진 미달이면 비중 축소 검토.",
        "하지 말 것: 이 설치 확인을 매수/매도 신호로 읽기.",
    ]
    return "[LGES] 373220 설치 확인 | 매매 신호 아님", "\n".join(lines)


# ---------- selftest ----------

def selftest() -> int:
    cfg = load_cfg()
    lv = next(x for x in cfg["levels"] if x["key"] == "buy330")
    L = cfg["levels"]
    fails = 0

    def check(label, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(("ok  " if ok else "FAIL"), label, got, "" if ok else f"want {want}")

    check("init 374000", step(lv, None, 374000), (True, False))
    check("init 320000 no send", step(lv, None, 320000), (False, False))
    check("330001 nothing", step(lv, True, 330001), (True, False))
    check("330000 fires (<=)", step(lv, True, 330000), (False, True))
    walk = [(329000, True), (320000, False), (330000, False), (330500, False), (329999, True), (400000, False), (300000, True)]
    a = True
    for i, (c, want) in enumerate(walk):
        a, f = step(lv, a, c)
        check(f"walk{i} {c}", f, want)
    A0 = {"buy330": True, "stop300": True, "add360": False, "tgt500": True, "tgt550": True, "px350": True}
    check("all init 374000 (inside 36~40 band, no send)", step_all(L, None, 374000), (A0, []))
    check("401000 leaves band, nothing fires", step_all(L, A0, 401000)[1], [])
    a1, _ = step_all(L, A0, 401000)
    check("back to 399000 -> add360", step_all(L, a1, 399000)[1], ["add360"])
    check("gap to 295000 -> buy330 + stop300 + px350", step_all(L, A0, 295000)[1], ["buy330", "stop300", "px350"])
    p350 = next(x for x in L if x["key"] == "px350")
    check("px350 close 350001 low 351000 nothing", step(p350, True, 350001, 351000), (True, False))
    check("px350 close 350000 fires (<=)", step(p350, True, 350000, 349000), (False, True))
    check("px350 close 357500 but low 349500 fires (trades at/below)", step(p350, True, 357500, 349500), (False, True))
    check("px350 low missing -> close only", step(p350, True, 357500, None), (True, False))
    check("px350 inside, low 349000 stays inside (no repeat)", step(p350, False, 352000, 349000), (False, False))
    check("px350 low 350500 re-arms quietly", step(p350, False, 356000, 350500), (True, False))
    check("buy330 ignores low (close basis)", step(lv, True, 335000, 329000), (True, False))
    a_new, f_new = step_all(L, {k: v for k, v in A0.items() if k != "px350"}, 339000, 335500)
    check("px350 added to old state inits quietly", (a_new["px350"], f_new), (False, []))
    check("seeded px350 armed + 339000 -> px350 only", step_all(L, A0, 339000, 335500)[1], ["px350"])
    t350, b350, pr350 = price_msg(cfg, ["px350"], 339000, "2026-11-03", 357500, 335500)
    check("px350 title has ticker + name", "373220 LG에너지솔루션" in t350, True)
    check("px350 body has low + link", ("장중 저가 335,500원" in b350, "m.stock.naver.com/domestic/stock/373220" in b350), (True, True))
    check("px350 priority 4", pr350, 4)
    print(pr350, t350)
    check("560000 -> tgt500 + tgt550", step_all(L, A0, 560000)[1], ["tgt500", "tgt550"])
    check("legacy state key missing inits quietly", step_all(L, {"buy330": True}, 374000)[1], [])
    check("priority buy330 alone 4", price_msg(cfg, ["buy330"], 329000, "d", None)[2], 4)
    check("priority stop300 combined 5", price_msg(cfg, ["buy330", "stop300"], 299000, "d", None)[2], 5)
    check("priority stop300 title", "손절/재검토" in price_msg(cfg, ["buy330", "stop300"], 299000, "d", None)[0], True)
    for fired_set, px in ((["stop300"], 299000), (["buy330", "stop300"], 299000)):
        t, b, p = price_msg(cfg, fired_set, px, "d", 335000)
        check(f"stop {fired_set} no buy-leaning line", BUY_LEAN in b, False)
        check(f"stop {fired_set} stop todo", "지금 할 일: " + STOP_TODO in b, True)
        check(f"stop {fired_set} six-part body", [ln.split(":")[0] for ln in b.split("\n")[1:]], ["이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"])
    check("buy330 only keeps buy line", BUY_LEAN in price_msg(cfg, ["buy330"], 329000, "d", None)[1], True)
    check("band edges", (hit(L[2], 360000), hit(L[2], 400000), hit(L[2], 359999)), (True, True, False))
    cases = {
        "조회공시 요구(풍문 또는 보도)": "inquiry", "조회공시요구(풍문또는보도)에대한답변(미확정)": "inquiry",
        "풍문 또는 보도에 대한 해명(미확정)": "inquiry", "연결재무제표기준영업(잠정)실적(공정공시)": "prelim",
        "횡령\u318d배임혐의발생": "embezzle", "[정정] 단일판매\u318d공급계약체결": "contract",
        "주요사항보고서(유상증자결정)": "major", "투자판단관련주요경영사항": "invest",
        "단일판매\u318d공급계약해지": "contract", "반기보고서(일반법인) (2026.06)": "periodic",
        "임원\u318d주요주주특정증권등소유상황보고서": None, "기업설명회(IR) 개최(안내공시)": None,
        "주식선물\u318d주식옵션 2단계 가격제한폭 확대요건 도달(상승)": None,
    }
    for t, want in cases.items():
        r = classify(cfg, t)
        check(f"classify {t[:20]}", r["key"] if r else None, want)
    # ESS large contract samples (shapes copied from real KIND bodies 20261001000119 / 20251208000031 / 20251226000706)
    D = "\u318d"
    form = ("단일판매" + D + "공급계약 체결 1. 판매" + D + "공급계약 구분 기타 판매" + D + "공급계약 - 체결계약명 {name} 2. 계약내역 "
            "계약금액(원) {amt} 최근매출액(원) 33,745,469,740,463 매출액대비(%) {ratio} 대규모법인여부 해당 3. 계약상대 {party} - 회사와의 관계 - "
            "4. 판매" + D + "공급지역 미국 5. 계약기간 시작일 2026-11-03 종료일 2029-12-31 6. 주요 계약조건 9. 기타 투자판단과 관련한 중요사항 가. {etc} ※ 관련공시 -")
    ess_big = form.format(name="ESS 배터리 공급계약", amt="2,060,100,000,000", ratio="6.1", party="Tesla Energy", etc="총 20GWh 규모 ESS(Energy Storage System) 배터리")
    ess_small = form.format(name="ESS 배터리 공급계약", amt="400,000,000,000", ratio="1.2", party="Hanwha Q CELLS USA Corp", etc="총 1.5GWh 규모 ESS 배터리")
    ev_big = form.format(name="전기차 배터리 공급계약", amt="5,944,227,336,000", ratio="17.6", party="-", etc="글로벌 지역 LFP 배터리 공급")
    ess_held = form.format(name="ESS 배터리 공급계약", amt="-", ratio="-", party="-", etc="에너지저장장치용 배터리, 금액은 유보기한 종료 후 공시")
    ratio_only = form.format(name="전력망 저장장치용 배터리 공급계약", amt="1,700,000,000,000".replace("1,700", "0,900"), ratio="-", party="-", etc="그리드 저장 프로젝트")
    term = ("단일판매" + D + "공급계약 해지 1. 판매" + D + "공급계약 해지 구분 기타 판매" + D + "공급계약 - 해지계약명 ESS 배터리 공급계약 2. 해지내역 "
            "해지금액(원) 3,921,711,000,000 최근매출액(원) 33,745,469,740,463 매출액대비(%) 11.6 대규모법인여부 해당 3. 계약상대 Freudenberg - 회사와의 관계 - 4. 계약기간 시작일 2024-04-01 종료일 2031-12-31")
    T, TC, TT = "단일판매" + D + "공급계약체결", "[정정] 단일판매" + D + "공급계약체결", "단일판매" + D + "공급계약 해지"
    crule = classify(cfg, T)
    ff = {"acptno": "20261103000999", "filed": "2026-11-03 16:00", "title": T}
    c1 = contract_info(cfg, T, ess_big)
    check("ESS large: ess+large p5", (c1["ess"], c1["large"], c1["priority"]), (True, True, 5))
    t1, b1, p1 = filing_msg(cfg, ff, crule, c1)
    check("ESS large title", t1, "[LGES] 373220 LG에너지솔루션 ESS 대형 수주 공시 2.06조원 | 원문 확인")
    check("ESS large body amount/ratio", "계약금액 2.06조원(최근 매출액 대비 6.1%)" in b1 and "금액 확인 필요" not in b1, True)
    c2 = contract_info(cfg, T, ess_small)
    check("ESS small: p3, normal title", (c2["ess"], c2["large"], c2["priority"], filing_msg(cfg, ff, crule, c2)[0]),
          (True, False, 3, "[LGES] 373220 LG에너지솔루션 새 공시 단일판매/공급계약 체결 | 원문 확인"))
    c3 = contract_info(cfg, T, ev_big)
    check("non-ESS large: p3", (c3["ess"], c3["large"], c3["priority"]), (False, True, 3))
    c4 = contract_info(cfg, T, None, "KIND 본문 조회 실패: timeout")
    t4, b4, p4 = filing_msg(cfg, ff, crule, c4)
    check("body fetch failure: fail open p3 + 금액 확인 필요", (p4, "금액 확인 필요(KIND 본문 조회 실패: timeout)" in b4, "새 공시" in t4), (3, True, True))
    c5 = contract_info(cfg, T, "알 수 없는 형식의 본문 ESS")
    check("parse failure: ESS but not large, 금액 확인 필요", (c5["ess"], c5["large"], c5["priority"], c5["check_amount"]), (True, False, 3, "본문 파싱 실패"))
    c6 = contract_info(cfg, T, ess_held)
    check("amount withheld: not large + 금액 확인 필요", (c6["ess"], c6["priority"], c6["check_amount"]), (True, 3, "계약금액 유보 또는 미기재"))
    c7 = contract_info(cfg, T, ratio_only)
    check("ratio computed 0.9조/33.7조 = 2.7% -> not large", (c7["ess"], c7["large"], c7["p"]["ratio"]), (True, False, 2.7))
    c8 = contract_info(cfg, TC, ess_big)
    check("정정 ESS large title", filing_msg(cfg, {**ff, "title": TC}, crule, c8)[:3:2], ("[LGES] 373220 LG에너지솔루션 ESS 대형 수주 정정 공시 2.06조원 | 원문 확인", 5))
    c9 = contract_info(cfg, TT, term)
    check("해지 ESS large title", filing_msg(cfg, {**ff, "title": TT}, crule, c9)[:3:2], ("[LGES] 373220 LG에너지솔루션 ESS 대형 수주 해지 공시 3.92조원 | 원문 확인", 5))
    check("PROCESS/BUSINESS do not count as ESS", contract_info(cfg, T, ev_big.replace("LFP", "PROCESS BUSINESS LFP"))["ess"], False)
    check("no-contract rule unchanged (ci None)", filing_msg(cfg, ff, crule)[2], 3)
    TCT = "[정정] 단일판매" + D + "공급계약 해지"
    for ttl, ci_x, want in (
        (T, c2, "[LGES] 373220 LG에너지솔루션 새 공시 단일판매/공급계약 체결 | 원문 확인"),
        (TT, contract_info(cfg, TT, term.replace("ESS 배터리", "전기차 배터리").replace("3,921,711,000,000", "300,000,000,000").replace("11.6", "0.9")),
         "[LGES] 373220 LG에너지솔루션 새 공시 단일판매/공급계약 해지 | 원문 확인"),
        (TC, c2, "[LGES] 373220 LG에너지솔루션 새 공시 단일판매/공급계약 체결 정정 | 원문 확인"),
        (TCT, None, "[LGES] 373220 LG에너지솔루션 새 공시 단일판매/공급계약 해지 정정 | 원문 확인"),
        (TT, c4, "[LGES] 373220 LG에너지솔루션 새 공시 단일판매/공급계약 해지 | 원문 확인"),
    ):
        tx, bx, px = filing_msg(cfg, {**ff, "title": ttl}, crule, ci_x)
        check(f"p3 label {ttl}", (tx, px), (want, 3))
        check(f"p3 label no dot/dash {ttl}", any(ch in tx + bx for ch in "\u00b7\u2014\u318d"), False)
        print(tx)
    check("p5 해지 정정 title", filing_msg(cfg, {**ff, "title": TCT}, crule, contract_info(cfg, TCT, term))[:3:2],
          ("[LGES] 373220 LG에너지솔루션 ESS 대형 수주 해지 정정 공시 3.92조원 | 원문 확인", 5))
    check("other rule label unchanged", filing_msg(cfg, {**ff, "title": "투자판단관련주요경영사항"}, classify(cfg, "투자판단관련주요경영사항"))[0],
          "[LGES] 373220 LG에너지솔루션 새 공시 투자판단 관련 주요경영사항 | 원문 확인")
    for t, b, _ in (filing_msg(cfg, ff, crule, c) for c in (c1, c2, c3, c4, c5, c6, c8, c9)):
        check(f"contract text no dot/dash: {t[:40]}", any(ch in t + b for ch in "\u00b7\u2014\u318d"), False)
        print(t)
    check("q4 window in", in_q4(cfg, "2027-01-08 08:30"), True)
    check("q4 window out", in_q4(cfg, "2026-10-08 08:34"), False)
    f = {"acptno": "20270108000055", "filed": "2027-01-08 08:30", "title": "연결재무제표기준영업(잠정)실적(공정공시)"}
    msgs = [price_msg(cfg, ["buy330"], 329000, "2026-11-02", 335000), price_msg(cfg, ["buy330", "stop300"], 295000, "2026-11-02", 335000),
            price_msg(cfg, ["tgt500"], 505000, "2026-11-02", 495000), filing_msg(cfg, {**f, "title": "횡령\u318d배임혐의발생"}, classify(cfg, "횡령배임")),
            q4_msg(cfg, f), confirm_msg(cfg, 374000, "2026-10-26") + (3,)]
    for t, b, p in msgs:
        check(f"no middle dot / em-dash: {t[:30]}", any(ch in t + b for ch in "\u00b7\u2014\u318d"), False)
        print(p, t)
    def ref_slice(body: str) -> str:
        line = next(ln for ln in body.split("\n") if "참고(판정에 쓰지 않음)" in ln)
        return line.split("참고(판정에 쓰지 않음)", 1)[-1]

    # Earnings reference is text-only. Checklist, title, priority, and price/contract paths stay put.
    q4_title, q4_body, q4_prio = q4_msg(cfg, f)
    check("q4 title unchanged", q4_title, "[LGES] 373220 4분기 잠정실적 확인 | 체크리스트 4개")
    check("q4 priority unchanged", q4_prio, 4)
    check("q4 checklist unchanged", cfg["q4"]["checklist"] in q4_body and "AMPC 제외 영업이익 3,000억원" in q4_body, True)
    check("q4 todo unchanged", "지금 할 일: 원문에서 위 4개를 직접 확인" in q4_body, True)
    q4_gap = ref_slice(q4_body)
    check("q4 no body says 확인 불가", "세전손익 확인 불가" in q4_gap and "0억원" not in q4_gap, True)
    check("q4 six-part", [ln.split(":")[0] for ln in q4_body.split("\n")[1:]],
          ["이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"])
    prule = classify(cfg, "연결재무제표기준영업(잠정)실적(공정공시)")
    pt, pb, pp = filing_msg(cfg, f, prule)
    check("prelim title unchanged", pt, "[LGES] 373220 LG에너지솔루션 새 공시 영업(잠정)실적 | 원문 확인")
    check("prelim priority unchanged", pp, 4)
    check("prelim todo unchanged", "지금 할 일: AMPC 제외 영업이익, ESS 매출과 수주, AMPC 비중을 확인." in pb, True)
    prelim_gap = ref_slice(pb)
    check("prelim missing body", "세전손익 확인 불가" in prelim_gap and "0억원" not in prelim_gap, True)
    check("price has no earn ref", "참고(판정에 쓰지 않음)" in price_msg(cfg, ["buy330"], 329000, "d", None)[1], False)
    check("contract has no earn ref", "참고(판정에 쓰지 않음)" in filing_msg(cfg, ff, crule, c2)[1], False)
    body_ok = (
        "실적기간 당기실적 2026-10-01 ~ 2026-12-31 단위 : 억원, % "
        "법인세비용차감전계속사업이익 당해실적 -1,800 당기순이익 당해실적 2,200 "
        "지배기업 소유주지분 순이익 당해실적 - 비지배지분 순이익 당해실적 - "
        "2026년 4분기 일회성 위약금은 150억원. 2026년 3분기 일회성 보상금은 3,000억원."
    )
    ref_ok = prelim_ref(f, body_ok, None)
    t_ok, b_ok, p_ok = q4_msg(cfg, f, ref=ref_ok)
    part = ref_slice(b_ok)
    check("q4 numbers do not change decision", (t_ok, p_ok, cfg["q4"]["checklist"] in b_ok), (q4_title, 4, True))
    check("q4 negative pretax", "세전손익(법인세비용차감전계속사업이익) -1,800억원" in part, True)
    check("q4 positive net", "연결 당기순손익(지배주주와 비지배 합계) 2,200억원" in part, True)
    check("q4 parent missing waits", "지배주주순손익 미공개" in part and "상세 공시(분기보고서)를 기다림" in part, True)
    check("q4 parent not swapped", "지배주주순손익 2,200억원" not in part, True)
    check("q4 oneoff period", "위약금 150억원" in part and "3,000" not in part, True)
    q3_prose = (
        "실적기간 당기실적 2026-10-01 ~ 2026-12-31 단위 : 억원. "
        "2026년 3분기 법인세 차감 전 순손실은 2,312억 원. 2026년 3분기 당기순손실은 2,969억 원. "
        "2026년 4분기 실적에서 전년 보상금은 1,500억원."
    )
    q3_slice = ref_slice(q4_msg(cfg, f, ref=prelim_ref(f, q3_prose, None))[1])
    check("q4 q3 prose not current", "미공개" in q3_slice and "-2,312억원" not in q3_slice and "1,500" not in q3_slice, True)
    check("q4 q3 prose keeps checklist title", q4_msg(cfg, f, ref=prelim_ref(f, q3_prose, None))[0], q4_title)
    check("q4 unit period", "기간 2026년 4분기" in part and "단위 억원" in part, True)
    zero_body = "실적기간 당기실적 2026-10-01 ~ 2026-12-31 단위 : 억원 법인세비용차감전계속사업이익 당해실적 0 당기순이익 당해실적 0 2026년 4분기 일회성 보상금은 0억원."
    zpart = ref_slice(q4_msg(cfg, f, ref=prelim_ref(f, zero_body, None))[1])
    check("q4 disclosed zero", zpart.count("0억원(공시된 0)") >= 2, True)
    miss_body = "실적기간 당기실적 2026-10-01 ~ 2026-12-31 단위 : 억원 법인세비용차감전계속사업이익 당해실적 - 당기순이익 당해실적 -"
    mpart = ref_slice(q4_msg(cfg, f, ref=prelim_ref(f, miss_body, None))[1])
    check("q4 dash not zero", "세전손익(법인세비용차감전계속사업이익) 미공개" in mpart and "0억원" not in mpart, True)
    bad_body = "단위 : 억원 당기실적('26.4Q) 법인세비용차감전계속사업이익 당해실적 해당없음 당기순이익 당해실적 적자전환"
    bpart = ref_slice(filing_msg(cfg, f, prule, ref=prelim_ref(f, bad_body, None))[1])
    check("prelim nonnumeric", "세전손익(법인세비용차감전계속사업이익) 확인 불가" in bpart and "연결 당기순손익(지배주주와 비지배 합계) 확인 불가" in bpart, True)
    check("prelim nonnumeric title stays", filing_msg(cfg, f, prule, ref=prelim_ref(f, bad_body, None))[0], pt)
    err_ref = prelim_ref(f, None, "KIND 본문 조회 실패: TimeoutError")
    et, eb, ep = q4_msg(cfg, f, ref=err_ref)
    err_slice = ref_slice(eb)
    check("source failure still checklist", (et, ep, "3,000억원" in eb, "세전손익 확인 불가" in err_slice, "0억원" not in err_slice),
          (q4_title, 4, True, True, True))
    check("earn ref no dot", any(ch in b_ok + eb + zpart for ch in "\u00b7\u2014\u318d"), False)
    check("not recurring op", "경상 영업이익이 아님" in b_ok, True)
    print("selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


# ---------- main ----------

def send(dry: bool, topic: str, title: str, msg: str, prio: int, tags: list[str], dedup_key: str | None = None) -> None:
    print(title)
    if dry:
        print(f"DRY RUN would send (p{prio}):\n{msg}")
    else:
        publish(topic, title, msg, prio, tags=tags, dedup_key=dedup_key)


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode == "selftest":
        return selftest()
    dry = mode == "dry"
    cfg = load_cfg()
    now = datetime.now(KST)
    print(f"{now.isoformat(timespec='seconds')} lgeswatch mode={mode}")
    if dry:
        print("DRY RUN (no ntfy, no state write)")
    if mode == "confirm":
        try:
            d, c = fetch_bars(cfg, now)[-1]
            title, msg = confirm_msg(cfg, c, d)
        except Exception as exc:
            print(f"bars fetch failed: {exc}")
            title, msg = confirm_msg(cfg, None, None)
        print(msg)
        publish(resolve_topic("port"), title, msg, 3, tags=["white_check_mark"])
        return 0
    vu = cfg.get("valid_until")
    if vu and now.date() > date.fromisoformat(vu):
        print(f"expired (valid_until {vu}); silent")
        return 0
    st = load_state()
    port, ops = resolve_topic("port"), resolve_topic("ops")

    # 1. price zone
    L = cfg["levels"]
    try:
        bars = fetch_bars(cfg, now)
        st["price_fail_notified"] = False
    except Exception as exc:
        bars = []
        print(f"bars fetch failed: {exc}")
        if not st.get("price_fail_notified"):
            send(dry, ops, "[LGES] 373220 시세 조회 실패 | 매매 신호 아님", f"373220 일별 시세 조회 실패: {exc}. 다음 19:00 실행에서 다시 봅니다.", 3, ["warning"])
            st["price_fail_notified"] = True
    if bars:
        last_d, last_c = bars[-1]
        if (now.date() - date.fromisoformat(last_d)).days > int(cfg.get("stale_days", 6)):
            print(f"stale bar {last_d}; price skip")
        elif not st.get("initialized"):
            armed, _ = step_all(L, None, last_c, LOWS.get(last_d))
            st.update({"initialized": True, "armed": armed, "last_date": last_d, "last_close": last_c,
                       "init_note": f"initialized from {last_d} close {last_c:,.0f}, no send"})
            print(f"init from {last_d} close {last_c:,.0f}: armed={armed} (no send)")
        else:
            prev_date = st.get("last_date")
            new = [(d, c) for d, c in bars if prev_date is None or d > prev_date]
            if mode == "force" and not new:
                new = bars[-1:]
            armed = st.get("armed")
            if not isinstance(armed, dict):  # legacy single-line state (buy330 only)
                armed = {"buy330": True if armed is None else bool(armed)}
            fired_all, prev_close = [], st.get("last_close")
            for d, c in new:
                armed, fired = step_all(L, armed, c, LOWS.get(d))
                print(f"bar {d} close {c:,.0f} low {LOWS.get(d)} -> fired={fired} armed={armed}")
                fired_all += [k for k in fired if k not in fired_all]
            if new:
                d, c = new[-1]
                if len(new) > 1:
                    prev_close = new[-2][1]
                for lv in L:
                    print(f"  line {line_text(lv)} ({lv['label']}): {'armed' if armed[lv['key']] else 'inside zone'}")
                if fired_all:
                    t, m, p = price_msg(cfg, fired_all, c, d, prev_close, LOWS.get(d))
                    send(dry, port, t, m, p, ["chart_with_downwards_trend"])
                else:
                    print("no zone entry; silent")
                st.update({"armed": armed, "last_date": d, "last_close": c})
            else:
                print(f"no new bar after {prev_date}; armed={armed}")
            st["armed"] = armed

    # 2. filings (+ 3. Q4 checklist)
    since = (now - timedelta(days=int(cfg["filings"]["lookback_days"]))).strftime("%Y-%m-%d")
    try:
        fl = kind_list(cfg, since, now.strftime("%Y-%m-%d"))
        st["kind_fail_notified"] = False
    except Exception as exc:
        fl = None
        print(f"KIND fetch failed: {exc}")
        if not st.get("kind_fail_notified"):
            send(dry, ops, "[LGES] 373220 공시 조회 실패 | 매매 신호 아님", f"KIND 공시 목록 조회 실패: {exc}. 다음 19:00 실행에서 다시 봅니다.", 3, ["warning"])
            st["kind_fail_notified"] = True
    if fl is not None:
        print("filings:", [(f["acptno"], clean(f["title"])) for f in fl])
        seen = set(st.get("seen_acptno") or [])
        if not st.get("filings_seeded"):
            st["seen_acptno"] = sorted(seen | {f["acptno"] for f in fl})
            st["filings_seeded"] = True
            print(f"filings seeded (no alert): {len(fl)}")
        else:
            for f in sorted((x for x in fl if x["acptno"] not in seen), key=lambda x: x["acptno"]):
                rule = classify(cfg, f["title"])
                if rule:
                    ci = None
                    eref = None
                    if rule["key"] == "contract":
                        body, berr = None, None
                        try:
                            body = kind_body(f["acptno"])
                        except Exception as exc:  # noqa: BLE001  fail open: still send at p3
                            berr = f"KIND 본문 조회 실패: {exc}"
                            print(berr)
                        ci = contract_info(cfg, f["title"], body, berr)
                        print(f"contract {f['acptno']}: ess={ci['ess']} large={ci['large']} prio={ci['priority']} check={ci['check_amount']}")
                    elif rule["key"] == "prelim":
                        pbody, perr = None, None
                        try:
                            pbody = kind_body(f["acptno"])
                            if not (pbody or "").strip():
                                perr = "본문 없음"
                                pbody = None
                        except Exception as exc:  # noqa: BLE001  fail open: still send the filing and the checklist
                            perr = f"KIND 본문 조회 실패: {type(exc).__name__}"
                            print(perr)
                        eref = prelim_ref(f, pbody, perr)
                    t, m, p = filing_msg(cfg, f, rule, ci, ref=eref)
                    send(dry, port, t, m, p, ["page_facing_up"], dedup_key=f["acptno"])
                    if rule["key"] == "prelim" and in_q4(cfg, f["filed"]) and not st.get("q4_checklist_sent"):
                        t, m, p = q4_msg(cfg, f, ref=eref)
                        send(dry, port, t, m, p, ["clipboard"], dedup_key=f"{f['acptno']}:q4")
                        st["q4_checklist_sent"] = f["acptno"]
                else:
                    print(f"skip (not watched): {f['acptno']} {clean(f['title'])}")
                seen.add(f["acptno"])
            st["seen_acptno"] = sorted(seen)[-300:]
    save_state(st, dry)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
