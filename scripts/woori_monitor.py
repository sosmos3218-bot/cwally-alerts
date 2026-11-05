#!/usr/bin/env python3
"""[WOORI] 우리금융지주(316140) 4Q26/FY2026 CET1·주주환원 감시 (2026-11-05 승인 pt62u).

Window weekdays 2027-01-26 ~ 2027-02-27 KST (ends_kst 2027-03-02 23:59). Attached to existing
morning 09:00 / afternoon 15:20 / k203780 19:00 bundles. No new cron, no new workflow_dispatch inputs,
no cron-job.org job. Script gates by date.

Sources: OpenDART list/document (OPENDART_API_KEY) with KIND/Naver fallback; woorifg.com IR PDF
(pdftotext) for CET1 (usually only in the IR presentation); DART attachment PDF for 기업가치 제고 계획.

Rules (each once, dedup in state/woori.json sent):
  1. p4 악화: end-2026 CET1 < 13.5%
  2. p3 주의: 13.5% <= CET1 < 13.6%
  3. p4 개선: official 2027+ nominal total shareholder return target >= 50%
  4. p4 강한 개선 (instead of 3 if both): official >= 55% OR new buyback >= 5,000억원
CFO 'personal view' / press speculation do not count for 3/4.
If 4Q/FY earnings disclosure appears but CET1 cannot be parsed: one ops notice per day via
earnings_parse.safe_parse. Never send p4 on a parse failure.
If no 4Q earnings disclosure by the last check on 2027-02-27: one 미게시 notice (cwally-ops).

Modes: auto | dry (no ntfy, no state; date gate ignored) | selftest (no network).
Position line: 25주, 평단 33,018원 + current price/P/L% when krx_close/daum is available.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402
from earnings_parse import safe_parse  # noqa: E402

KST = timezone(timedelta(hours=9))
NAME = "woori"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
TAG = "[WOORI]"
TICKER = "316140"
HEAD = f"{TAG} {TICKER} 우리금융"
OPS_TITLE = f"[OPS] WOORI 파싱 실패 [{TICKER}] | 판정 유보"
BANNED = ("\u00b7", "\u318d", "\u3186", "\u2027", "\u2219", "\u2014", "\u2013", "ㅉ", "ㆍ")
UA = {"User-Agent": "Mozilla/5.0 (cwally-alerts woori; +mailto:sosmos3218@gmail.com)"}
KIND = "https://kind.krx.co.kr"
KIND_VIEW = KIND + "/common/disclsviewer.do?method=search&acptno={acpt}"
DART_VIEW = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcp}"
OPENDART_LIST = "https://opendart.fss.or.kr/api/list.json"
OPENDART_DOC = "https://opendart.fss.or.kr/api/document.xml"
IR_LIST = "https://www.woorifg.com/kor/investor/ir/earnings-announcement/list.do"

EARN_TITLE_RE = re.compile(
    r"(연결)?재무제표기준영업\s*\(?잠정\)?\s*실적|매출액또는손익구조|결산실적공시예고|"
    r"기업설명회\s*\(IR\)|기업가치\s*제고\s*계획",
    re.I,
)
EARN_4Q_RE = re.compile(
    r"(26\.?4Q|'26\.?4Q|2026\s*년?\s*4\s*분기|2026-10-01\s*~\s*2026-12-31|"
    r"2026\s*년?\s*연간|FY\s*'?26|FY2026|2026년\s*기말|기말\s*실적)",
    re.I,
)
VUP_RE = re.compile(r"기업\s*가치\s*제고\s*계획|주주환원|자기주식\s*취득\s*결정|자기주식취득")
CET1_LABEL = re.compile(
    r"(?:CET\s*1(?:\s*Ratio)?|CET1비율|보통주자본비율|보통주\s*자본비율|Common\s*Equity\s*Tier\s*1)",
    re.I,
)
PCT_RE = re.compile(r"(\d{1,2}(?:\.\d{1,2})?)\s*%")
PERSONAL_RE = re.compile(
    r"(개인\s*적|개인\s*의견|제\s*생각|제\s*견해|개인적으로|personal\s+view|my\s+(?:own\s+)?view|"
    r"personally|I\s+personally|애널리스트|증권사\s*추정|시장\s*예상)",
    re.I,
)
OFFICIAL_RE = re.compile(
    r"(목표|계획|추진|지향|상향|제시|결정|공시|당사|그룹|이사회|공식|가이던스|유지|"
    r"target|plan|aim|intend|guidance|outlook)",
    re.I,
)
TSR_RE = re.compile(
    r"(총\s*주주\s*환원\s*율|주주\s*환원\s*율|total\s+shareholder\s+return|"
    r"total\s+return(?:\s+ratio)?|TSR)",
    re.I,
)
BUYBACK_RE = re.compile(
    r"(자기주식|자사주)\s*(?:취득|매입|매수)[^0-9]{0,40}?(\d[\d,]*(?:\.\d+)?)\s*(조|억)\s*원?",
    re.I,
)
YEAR_END_RE = re.compile(
    r"(2026\s*년?\s*(?:말|기말|연말)|기말|연말|end[- ]?2026|year[- ]?end|FY\s*'?26|FY2026|"
    r"2026\s*년\s*4\s*분기|26\.?4Q)",
    re.I,
)
FWD_YEAR_RE = re.compile(r"(2027|2028|FY\s*'?27|내년|향후)", re.I)


# ================================================================ helpers
def now_kst() -> datetime:
    return datetime.now(KST)


def load_cfg() -> dict:
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"sent": {}, "seen_rcps": [], "earnings_rcp": None, "empty_notified": False}
    try:
        v = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {"sent": {}, "seen_rcps": []}


def save_state(st: dict, dry: bool, now: datetime) -> None:
    if dry:
        print("DRY RUN: state not written")
        return
    st["updated_at"] = now.isoformat(timespec="seconds")
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def clean(text: str) -> str:
    t = text or ""
    for b in BANNED:
        t = t.replace(b, ", " if b in ("\u00b7", "\u318d", "ㆍ") else " ")
    return re.sub(r"\s+", " ", t).strip()


def check_title(title: str, body: str = "") -> None:
    assert TICKER in title, title
    assert "매수" not in title, title
    blob = title + body
    for b in BANNED:
        assert b not in blob, f"banned {b!r} in text"


def text_of(html: str) -> str:
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html or "")
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", unescape(t)).strip()


def http(url: str, data: dict | None = None, headers: dict | None = None,
         tries: int = 3, timeout: int = 30, binary: bool = False):
    h = dict(UA)
    h.update(headers or {})
    body = urllib.parse.urlencode(data).encode() if data else None
    if body:
        h.setdefault("Content-Type", "application/x-urlencoded" if False else "application/x-www-form-urlencoded")
    err: Exception | None = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=body, headers=h, method="POST" if body else "GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if binary:
                    return raw
                ctype = (resp.headers.get("Content-Type") or "").lower()
                enc = "euc-kr" if "euc-kr" in ctype else "utf-8"
                return raw.decode(enc, errors="ignore")
        except Exception as exc:  # noqa: BLE001
            err = exc
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"{url.split('?')[0]}: {err}")


def api_key() -> str:
    return (os.environ.get("OPENDART_API_KEY") or "").strip()


# ================================================================ price / position
def current_price(code: str = TICKER) -> tuple[float | None, str | None]:
    try:
        import krx_close
        bars = krx_close.completed(krx_close.regular_bars(code, 5), datetime.now(KST))
        if bars:
            return float(bars[-1].close), getattr(bars[-1], "src", "krx") or "krx"
    except Exception as exc:  # noqa: BLE001
        print(f"krx_close fail: {type(exc).__name__}", file=sys.stderr)
    try:
        url = (f"https://finance.daum.net/api/quote/A{code}/days"
               f"?symbolCode=A{code}&page=1&perPage=5&pagination=true")
        raw = http(url, headers={"Referer": f"https://finance.daum.net/quotes/A{code}"})
        rows = (json.loads(raw).get("data") or [])
        if rows:
            return float(rows[0]["tradePrice"]), "daum"
    except Exception as exc:  # noqa: BLE001
        print(f"daum price fail: {type(exc).__name__}", file=sys.stderr)
    return None, None


def position_line(cfg: dict) -> str:
    pos = cfg.get("position") or {}
    shares = int(pos.get("shares") or 0)
    avg = float(pos.get("avg_krw") or 0)
    base = f"보유 포지션: {shares}주, 평단 {avg:,.0f}원"
    px, src = current_price(cfg.get("ticker") or TICKER)
    if px is None or not avg:
        return base + " (현재가 확인 불가)."
    pnl = (px / avg - 1.0) * 100.0
    return f"{base}. 현재가 {px:,.0f}원({src}), 손익 {pnl:+.2f}%."


# ================================================================ sources
def dart_list(cfg: dict, since: str, until: str) -> list[dict]:
    key = api_key()
    if not key:
        raise RuntimeError("OPENDART_API_KEY 없음")
    out: list[dict] = []
    page = 1
    while page <= 5:
        qs = urllib.parse.urlencode({
            "crtfc_key": key, "corp_code": cfg["dart_corp_code"],
            "bgn_de": since.replace("-", ""), "end_de": until.replace("-", ""),
            "page_count": "100", "page_no": str(page),
        })
        data = json.loads(http(f"{OPENDART_LIST}?{qs}", tries=2, timeout=25))
        if data.get("status") not in ("000", "013"):
            raise RuntimeError(f"OpenDART list {data.get('status')} {data.get('message')}")
        rows = data.get("list") or []
        for r in rows:
            nm = (r.get("report_nm") or "").strip()
            rcp = (r.get("rcept_no") or "").strip()
            if not rcp:
                continue
            out.append({
                "id": f"dart:{rcp}", "rcp": rcp, "title": nm,
                "filed": r.get("rcept_dt") or "", "src": "OpenDART",
            })
        total = int(data.get("total_page") or 1)
        if page >= total or not rows:
            break
        page += 1
    return out


def kind_list(cfg: dict, since: str, until: str) -> list[dict]:
    form = {
        "method": "searchDetailsSub", "currentPageSize": "100", "pageIndex": "1",
        "orderMode": "1", "orderStat": "D", "forward": "details_sub", "searchCodeType": "",
        "repIsuSrtCd": "A" + cfg["ticker"], "allRepIsuSrtCd": "", "oldSearchCorpName": "",
        "disclosureType": "", "disTypevalue": "", "reportNm": "", "reportCd": "",
        "searchCorpName": cfg["ticker"], "business": "", "marketType": "",
        "settlementMonth": "", "securities": "", "submitOblgNm": "", "enterprise": "",
        "fromDate": since, "toDate": until, "reportNmTemp": "", "bfrDsclsType": "on",
    }
    page = http(KIND + "/disclosure/details.do", data=form,
                headers={"X-Requested-With": "XMLHttpRequest",
                         "Referer": KIND + "/disclosure/details.do?method=searchDetailsMain"})
    out, rows = [], 0
    for row in re.findall(r"(?s)<tr[^>]*>(.*?)</tr>", page):
        acpt = re.search(r"openDisclsViewer\('(\d{14})'", row)
        if not acpt:
            continue
        rows += 1
        t = text_of(row)
        dm = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})", t)
        title = t[dm.end():].strip() if dm else t
        title = re.sub(r"\s*(공시차트|주가차트).*$", "", title).strip()
        out.append({
            "id": "kind:" + acpt.group(1), "rcp": acpt.group(1), "title": title,
            "filed": f"{dm.group(1)}" if dm else "", "src": "KIND",
        })
    if rows == 0 and "조회된 결과값이 없습니다" not in page and "<table" not in page:
        raise RuntimeError("KIND 응답 형식 이상")
    return out


def naver_list(cfg: dict, since: str, until: str) -> list[dict]:
    raw = json.loads(http(
        f"https://stock.naver.com/api/domestic/detail/notice?itemCode={cfg['ticker']}&page=1&pageSize=50"))
    out = []
    for r in raw if isinstance(raw, list) else []:
        title = str(r.get("title") or "").strip()
        day = str(r.get("datetime") or "")[:10]
        if not (since <= day <= until):
            continue
        out.append({
            "id": "naver:" + str(r.get("no") or r.get("bizdateSeq")),
            "rcp": None, "title": title, "filed": day, "src": "네이버",
            "body": text_of(str(r.get("contents") or "")),
        })
    return out


def list_filings(cfg: dict, since: str, until: str) -> tuple[list[dict], list[str]]:
    notes: list[str] = []
    rows: list[dict] = []
    try:
        rows = dart_list(cfg, since, until)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"OpenDART: {exc}")
        try:
            rows = kind_list(cfg, since, until)
        except Exception as exc2:  # noqa: BLE001
            notes.append(f"KIND: {exc2}")
            try:
                rows = naver_list(cfg, since, until)
            except Exception as exc3:  # noqa: BLE001
                notes.append(f"네이버: {exc3}")
                raise RuntimeError("; ".join(notes)) from exc3
    # relevant only
    hits = []
    for r in rows:
        title = r.get("title") or ""
        if EARN_TITLE_RE.search(title) or VUP_RE.search(title):
            hits.append(r)
    return hits, notes


def dart_document_text(rcp: str) -> str:
    key = api_key()
    if not key or not rcp:
        return ""
    raw = http(f"{OPENDART_DOC}?crtfc_key={key}&rcept_no={rcp}", binary=True, tries=2, timeout=60)
    if raw[:2] != b"PK":
        return text_of(raw.decode("utf-8", "ignore"))
    zf = zipfile.ZipFile(io.BytesIO(raw))
    parts = []
    for n in zf.namelist():
        data = zf.read(n)
        for enc in ("utf-8", "cp949", "euc-kr"):
            try:
                parts.append(data.decode(enc))
                break
            except UnicodeDecodeError:
                continue
    return text_of("\n".join(parts))


def dart_attach_pdf_text(rcp: str) -> str:
    """Download first attachment PDF from DART viewer download page, return pdftotext."""
    if not rcp:
        return ""
    try:
        html = http(f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcp}", tries=2, timeout=30)
    except Exception as exc:  # noqa: BLE001
        print(f"dart viewer fail: {exc}", file=sys.stderr)
        return ""
    dcm = re.search(r'<option value="rcpNo=\d+&amp;dcmNo=(\d+)"[^>]*>\s*기타공시첨부', html) or \
        re.search(r"openPdfDownload\('\d+',\s*'(\d+)'\)", html)
    if not dcm:
        # any attach option
        dcm = re.search(r'<option value="rcpNo=\d+&amp;dcmNo=(\d+)"', html)
    if not dcm:
        return ""
    dcm_no = dcm.group(1)
    try:
        page = http(f"https://dart.fss.or.kr/pdf/download/main.do?rcp_no={rcp}&dcm_no={dcm_no}",
                    tries=2, timeout=30)
    except Exception as exc:  # noqa: BLE001
        print(f"dart pdf page fail: {exc}", file=sys.stderr)
        return ""
    m = re.search(r'/pdf/download/file\.do\?[^"\']+', page)
    if not m:
        return ""
    url = "https://dart.fss.or.kr" + m.group(0).replace("&amp;", "&")
    try:
        data = http(url, headers={"Referer": f"https://dart.fss.or.kr/pdf/download/main.do?rcp_no={rcp}"},
                    binary=True, tries=2, timeout=90)
    except Exception as exc:  # noqa: BLE001
        print(f"dart pdf download fail: {exc}", file=sys.stderr)
        return ""
    if data[:4] != b"%PDF":
        return ""
    return pdf_to_text(data)


def pdf_to_text(data: bytes) -> str:
    if not data or data[:4] != b"%PDF":
        return ""
    if not shutil.which("pdftotext"):
        return ""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
        fh.write(data)
        path = fh.name
    try:
        out = subprocess.run(
            ["pdftotext", "-enc", "UTF-8", "-layout", path, "-"],
            capture_output=True, timeout=120,
        )
        text = out.stdout.decode("utf-8", "replace")
        # unwrap hangul mid-word wraps like earnings_parse.unwrap_pdf
        text = re.sub(r"(?<=[가-힣])\n(?=[가-힣])", "", text)
        return text
    except Exception as exc:  # noqa: BLE001
        print(f"pdftotext fail: {exc}", file=sys.stderr)
        return ""
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def fetch_ir_pdf_text(cfg: dict) -> tuple[str, str]:
    """Try woorifg.com IR list for the latest 실적발표 PDF. Returns (text, source_url)."""
    try:
        html = http(IR_LIST, tries=2, timeout=25)
    except Exception as exc:  # noqa: BLE001
        print(f"woorifg IR list unreachable: {type(exc).__name__}: {exc}", file=sys.stderr)
        return "", ""
    # FileDown.do links or mergeView seq
    links = re.findall(r'(https?://www\.woorifg\.com/cmm/fms/FileDown\.do\?[^"\'>\s]+)', html)
    links += ["https://www.woorifg.com" + p for p in re.findall(
        r'(/cmm/fms/FileDown\.do\?[^"\'>\s]+)', html)]
    # prefer 2026/2027 filenames
    links = list(dict.fromkeys(links))
    for url in links[:8]:
        url = unescape(url)
        try:
            data = http(url, headers={"Referer": IR_LIST}, binary=True, tries=2, timeout=60)
        except Exception as exc:  # noqa: BLE001
            print(f"IR file fail {url[:80]}: {exc}", file=sys.stderr)
            continue
        if data[:4] == b"%PDF":
            return pdf_to_text(data), url
    return "", ""


def filing_body(row: dict) -> str:
    if row.get("body"):
        return row["body"]
    parts = []
    rcp = row.get("rcp")
    if rcp and row.get("src") in ("OpenDART", "KIND", "dart"):
        try:
            parts.append(dart_document_text(rcp))
        except Exception as exc:  # noqa: BLE001
            print(f"doc {rcp}: {exc}", file=sys.stderr)
        try:
            parts.append(dart_attach_pdf_text(rcp))
        except Exception as exc:  # noqa: BLE001
            print(f"attach {rcp}: {exc}", file=sys.stderr)
    if row.get("src") == "KIND" and row.get("rcp"):
        try:
            viewer = http(KIND_VIEW.format(acpt=row["rcp"]), tries=2, timeout=30)
            m = re.search(r"<option value='(\d+)\|Y'", viewer) or re.search(r"<option value='(\d+)\|", viewer)
            if m:
                sc = http(f"{KIND}/common/disclsviewer.do?method=searchContents&docNo={m.group(1)}", tries=2)
                p = re.search(r"setPath\('[^']*','([^']+\.htm)'", sc)
                if p:
                    parts.append(text_of(http(p.group(1), timeout=60)))
        except Exception as exc:  # noqa: BLE001
            print(f"kind body: {exc}", file=sys.stderr)
    return "\n".join(p for p in parts if p)


# ================================================================ parsers
def parse_cet1(text: str) -> list[dict]:
    """Return list of {value, ctx, year_end_ish} from CET1 mentions."""
    out = []
    if not text:
        return out
    for m in CET1_LABEL.finditer(text):
        window = text[max(0, m.start() - 80): m.end() + 120]
        # avoid matching non-ratio nearby numbers first; prefer % after label
        after = text[m.end(): m.end() + 80]
        pm = PCT_RE.search(after) or PCT_RE.search(window)
        if not pm:
            continue
        val = float(pm.group(1))
        if not (8.0 <= val <= 25.0):
            continue
        ctx = re.sub(r"\s+", " ", window).strip()
        out.append({"value": val, "ctx": ctx[:220], "year_end_ish": bool(YEAR_END_RE.search(window))})
    # table-ish: "보통주자본비율 13.80" without % immediately
    for m in re.finditer(
            r"(?:CET\s*1(?:비율|Ratio)?|보통주자본비율)\s*[:=\s]*?(\d{1,2}\.\d{1,2})\s*%?",
            text, re.I):
        val = float(m.group(1))
        if not (8.0 <= val <= 25.0):
            continue
        window = text[max(0, m.start() - 60): m.end() + 60]
        ctx = re.sub(r"\s+", " ", window).strip()
        if not any(abs(x["value"] - val) < 1e-9 and x["ctx"][:40] == ctx[:40] for x in out):
            out.append({"value": val, "ctx": ctx[:220], "year_end_ish": bool(YEAR_END_RE.search(window))})
    return out


def pick_year_end_cet1(hits: list[dict]) -> dict | None:
    ye = [h for h in hits if h.get("year_end_ish")]
    if ye:
        return ye[0]
    return hits[0] if hits else None


def classify_tsr(text: str) -> list[dict]:
    """Find official forward TSR targets. Returns {pct, official, personal, ctx}."""
    out = []
    if not text:
        return out
    for m in TSR_RE.finditer(text):
        window = text[max(0, m.start() - 100): m.end() + 140]
        if not FWD_YEAR_RE.search(window) and "2027" not in window and "내년" not in window:
            # still allow "총주주환원율 50%" near 목표 without year if official words present
            if not OFFICIAL_RE.search(window):
                continue
        pm = PCT_RE.search(window[window.find(m.group(0)):] if m.group(0) in window else window)
        # search after label preferentially
        after = text[m.end(): m.end() + 100]
        # "40%대 중반" is a band, not a hard target — check before bare N%
        band = re.search(r"(\d{2})\s*%\s*대", after) or re.search(r"(\d{2})\s*%\s*대", window)
        if band:
            out.append({
                "pct": None, "band": int(band.group(1)), "official": bool(OFFICIAL_RE.search(window)),
                "personal": bool(PERSONAL_RE.search(window)),
                "ctx": re.sub(r"\s+", " ", window).strip()[:240],
            })
            continue
        pm = PCT_RE.search(after) or PCT_RE.search(window)
        if not pm:
            continue
        pct = float(pm.group(1))
        if not (10.0 <= pct <= 100.0):
            continue
        personal = bool(PERSONAL_RE.search(window))
        official = bool(OFFICIAL_RE.search(window)) and not personal
        out.append({
            "pct": pct, "band": None, "official": official, "personal": personal,
            "ctx": re.sub(r"\s+", " ", window).strip()[:240],
        })
    return out


def parse_buyback_eok(text: str) -> list[dict]:
    out = []
    if not text:
        return out
    for m in BUYBACK_RE.finditer(text):
        num = float(m.group(2).replace(",", ""))
        unit = m.group(3)
        eok = int(round(num * 10000)) if unit == "조" else int(round(num))
        window = text[max(0, m.start() - 40): m.end() + 40]
        out.append({"eok": eok, "ctx": re.sub(r"\s+", " ", window).strip()[:200]})
    # also "5,000억원 규모 자기주식"
    for m in re.finditer(
            r"(\d[\d,]*(?:\.\d+)?)\s*(조|억)\s*원?\s*(?:규모\s*)?(?:의\s*)?(자기주식|자사주)\s*(?:취득|매입)",
            text):
        num = float(m.group(1).replace(",", ""))
        eok = int(round(num * 10000)) if m.group(2) == "조" else int(round(num))
        window = text[max(0, m.start() - 40): m.end() + 40]
        if not any(x["eok"] == eok for x in out):
            out.append({"eok": eok, "ctx": re.sub(r"\s+", " ", window).strip()[:200]})
    return out


def is_4q_earnings(row: dict, body: str = "") -> bool:
    title = row.get("title") or ""
    blob = title + "\n" + (body or "")
    if re.search(r"자회사의\s*주요경영사항", title):
        return False
    if re.search(r"기업가치\s*제고", title):
        return False
    if re.search(r"기업설명회|IR\s*개최", title) and not re.search(r"잠정\s*실적|손익구조", title):
        # IR 안내 alone is not the earnings disclosure; still useful for timing
        return bool(EARN_4Q_RE.search(blob)) and bool(re.search(r"실적", title + body))
    if not re.search(r"잠정\)?\s*실적|손익구조|결산실적", title):
        return False
    return bool(EARN_4Q_RE.search(blob))


def is_vup_or_return(row: dict) -> bool:
    t = row.get("title") or ""
    return bool(re.search(r"기업\s*가치\s*제고\s*계획|자기주식\s*취득\s*결정", t))


# ================================================================ messages
def send(cfg: dict, kind: str, title: str, lines: list[str], prio: int, dry: bool) -> None:
    body = "\n".join(clean(x) for x in lines)
    title = clean(title)
    check_title(title, body)
    if dry:
        print(title, body, "dry-run; no ntfy", sep="\n")
        return
    topic = resolve_topic(kind, cfg.get("ntfy_topic") if kind == "port" else None)
    # 미게시 / 파싱 실패 titles route to ops via publish/route_topic
    publish(topic, title, body, prio,
            ["warning"] if kind == "ops" or "미게시" in title or "파싱 실패" in title else ["chart_with_upwards_trend"],
            resolve_server(cfg.get("ntfy_server")))


def base_lines(cfg: dict, now: datetime, what: str, detail: str, source: str) -> list[str]:
    bl = cfg.get("baseline") or {}
    return [
        f"이게 뭐 알림: 우리금융지주({TICKER}) 4Q26/FY2026 감시 신호입니다. {what} 투자 권유가 아닙니다.",
        (f"지금 결과/상태: {detail}. 기준 맥락: 3Q26 CET1 {bl.get('q3_cet1_pct')}%"
         f"(일회성 제외 {bl.get('q3_cet1_ex_oneoffs')}), 회사 연말 전망 {bl.get('year_end_outlook_pct')}%, "
         f"현 가이던스 '{bl.get('tsr_guidance')}'. 출처 {source}. 조회 {now:%Y-%m-%d %H:%M} KST."),
        position_line(cfg),
        "지금 할 일: 공시/IR 원문을 확인하고 포지션을 재검토하세요. 이 알림은 모니터링 신호입니다.",
        "하지 말 것: 파싱 실패나 언론 추정을 확정 실적으로 보기. 투자 권유 아님.",
    ]


# ================================================================ run
def in_window(cfg: dict, now: datetime) -> bool:
    day = now.strftime("%Y-%m-%d")
    if now.year != int(cfg.get("year_guard") or 2027):
        return False
    return cfg["window_start"] <= day <= cfg["window_end"]


def already(st: dict, key: str) -> bool:
    return bool((st.get("sent") or {}).get(key))


def mark(st: dict, key: str, meta: dict) -> None:
    sent = dict(st.get("sent") or {})
    sent[key] = meta
    st["sent"] = sent


def evaluate_corpus(text: str, year_end_mode: bool) -> dict:
    """Pure evaluation over combined text. year_end_mode=True after 4Q earnings found."""
    cet1_hits = parse_cet1(text)
    cet1 = pick_year_end_cet1(cet1_hits) if year_end_mode else None
    if year_end_mode and cet1 is None and cet1_hits:
        # if earnings is out, accept first CET1 even without year-end cue
        cet1 = cet1_hits[0]
    tsr = classify_tsr(text)
    official = [t for t in tsr if t.get("official") and t.get("pct") is not None]
    buybacks = parse_buyback_eok(text)
    return {
        "cet1_hits": cet1_hits,
        "cet1": cet1,
        "tsr": tsr,
        "official_tsr": official,
        "buybacks": buybacks,
        "guidance_band": next((t for t in tsr if t.get("band") is not None), None),
    }


def run(cfg: dict, st: dict, now: datetime, mode: str) -> int:
    dry = mode == "dry"
    day = now.strftime("%Y-%m-%d")
    since = "2027-01-01"
    until = day if day >= cfg["window_start"] else cfg["window_end"]

    try:
        rows, notes = list_filings(cfg, since, until)
        for n in notes:
            print(f"source note: {n}")
    except Exception as exc:  # noqa: BLE001
        print(f"::warning::filing list failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        if st.get("ops_fetch_day") != day:
            title = f"{HEAD} 조회 실패 | 판정 유보"
            lines = [f"{now:%Y-%m-%d %H:%M} KST 공시 목록 조회 실패({type(exc).__name__}). 다음 실행에서 다시 봅니다. 매매 신호가 아닙니다."]
            send(cfg, "ops", title, lines, 3, dry)
            if not dry:
                st["ops_fetch_day"] = day
                save_state(st, dry, now)
        return 0

    # classify
    earn_rows = []
    vup_rows = []
    for r in rows:
        body_preview = ""
        if is_vup_or_return(r):
            vup_rows.append(r)
        # need body for some 4Q checks on prelim titles without period in title
        if re.search(r"잠정\)?\s*실적|손익구조|결산실적", r.get("title") or ""):
            if EARN_4Q_RE.search(r.get("title") or ""):
                earn_rows.append(r)
            else:
                # fetch body to confirm period (only for new unseen)
                if r.get("rcp") and r["rcp"] not in (st.get("seen_rcps") or []) and not st.get("earnings_rcp"):
                    try:
                        body_preview = filing_body(r)
                    except Exception:  # noqa: BLE001
                        body_preview = ""
                    if is_4q_earnings(r, body_preview):
                        r = dict(r, body=body_preview)
                        earn_rows.append(r)

    # empty notice on last day
    last_check = day >= cfg["window_end"] and now.hour >= 15
    if not earn_rows and not st.get("earnings_rcp"):
        if last_check and not st.get("empty_notified"):
            title = f"{HEAD} 4Q 실적 미게시 | 판정 유보"
            lines = [
                "이게 뭐 알림: 우리금융지주(316140) 4Q26/FY2026 실적 공시 감시 창이 끝났습니다. 매매 신호가 아닙니다.",
                f"지금 결과/상태: {cfg['window_start']}~{cfg['window_end']} 동안 4Q/연간 잠정실적 공시를 찾지 못했습니다. "
                f"조회 {now:%Y-%m-%d %H:%M} KST. DART/KIND/woorifg.com 을 직접 확인하세요.",
                position_line(cfg),
                "지금 할 일: 공시 일정을 직접 확인하세요.",
            ]
            send(cfg, "port", title, lines, 3, dry)  # 미게시 -> ops via route
            if not dry:
                st["empty_notified"] = True
                save_state(st, dry, now)
        else:
            print("4Q earnings not posted yet; silent")
        # still scan vup/buyback below even without earnings
    else:
        if earn_rows and not st.get("earnings_rcp"):
            st["earnings_rcp"] = earn_rows[0].get("rcp") or earn_rows[0].get("id")

    # gather corpus
    corpus_parts: list[str] = []
    sources: list[str] = []
    for r in earn_rows + vup_rows:
        rid = r.get("rcp") or r.get("id")
        try:
            body = r.get("body") or filing_body(r)
        except Exception as exc:  # noqa: BLE001
            body = ""
            print(f"body fail {rid}: {exc}", file=sys.stderr)
        if body:
            corpus_parts.append(body)
            link = DART_VIEW.format(rcp=r["rcp"]) if r.get("rcp") else (r.get("title") or "")
            sources.append(f"{r.get('title')} ({r.get('src')}) {link}")
        seen = list(st.get("seen_rcps") or [])
        if r.get("rcp") and r["rcp"] not in seen:
            seen.append(r["rcp"])
            st["seen_rcps"] = seen

    ir_text, ir_url = "", ""
    if earn_rows or st.get("earnings_rcp"):
        ir_text, ir_url = fetch_ir_pdf_text(cfg)
        if ir_text:
            corpus_parts.append(ir_text)
            sources.append(f"woorifg IR PDF {ir_url}")
        else:
            print("IR PDF not fetched (woorifg unreachable or no PDF link)")

    corpus = "\n".join(corpus_parts)
    year_end_mode = bool(earn_rows or st.get("earnings_rcp"))

    def parse_all() -> dict:
        if year_end_mode and not corpus.strip():
            raise ValueError("실적 공시는 있으나 본문/IR 텍스트가 비어 CET1을 읽을 수 없음")
        ev = evaluate_corpus(corpus, year_end_mode=year_end_mode)
        if year_end_mode and ev.get("cet1") is None and not (st.get("sent") or {}).get("cet1_worse") \
                and not (st.get("sent") or {}).get("cet1_watch"):
            # earnings out but no CET1 -> treat as parse soft-failure for ops (not p4)
            raise ValueError("CET1 비율을 실적/IR 텍스트에서 찾지 못함")
        return ev

    def ops(detail: str) -> None:
        lines = [
            f"{now:%Y-%m-%d %H:%M} KST | OPS | woori 4Q/CET1",
            "이게 뭐 알림: 실적 공시는 찾았지만 CET1/주주환원 숫자 읽기(parse)에서 오류가 났습니다. 투자 판단 알림이 아닙니다.",
            f"지금 상태: 오류 {detail}. 이번 실행은 판정 없이 끝났고 다음 실행에서 다시 읽습니다. p4를 보내지 않았습니다.",
            f"지금 할 일: 로그의 ::error:: 줄과 DART/IR 원문을 확인하세요.",
            position_line(cfg),
        ]
        send(cfg, "ops", OPS_TITLE, lines, 3, dry)

    before = st.get("ops_parse_day")
    if year_end_mode or corpus.strip():
        ev = safe_parse(parse_all, state=st, day=day, ops_notify=ops, dry=dry, label="woori parse")
        if ev is None:
            if not dry and st.get("ops_parse_day") != before:
                save_state(st, dry, now)
            # still allow buyback/tsr from non-earnings corpus? if parse raised on CET1 miss,
            # safe_parse returns None — do not send p4. Exit after save.
            if year_end_mode:
                return 0
            # no earnings yet: evaluate vup-only without forcing CET1
            ev = evaluate_corpus(corpus, year_end_mode=False)
    else:
        ev = evaluate_corpus("", year_end_mode=False)

    src = "; ".join(sources) if sources else "공시 목록만 확인"
    th = cfg["thresholds"]
    changed = False

    # Rule 1 / 2 CET1
    cet1 = ev.get("cet1")
    if cet1 and year_end_mode:
        val = float(cet1["value"])
        if val < th["cet1_worse_lt"] and not already(st, "cet1_worse"):
            title = f"{HEAD} CET1 악화"
            detail = (f"공시/IR에서 기말 CET1 {val:.2f}% (< {th['cet1_worse_lt']}%). "
                      f"인용: {cet1['ctx']}")
            send(cfg, "port", title, base_lines(cfg, now, "CET1 악화(p4) 모니터링 신호.", detail, src), 4, dry)
            mark(st, "cet1_worse", {"val": val, "at": now.isoformat(timespec="seconds")})
            changed = True
        elif th["cet1_worse_lt"] <= val < th["cet1_watch_lt"] and not already(st, "cet1_watch"):
            title = f"{HEAD} CET1 주의"
            detail = (f"공시/IR에서 기말 CET1 {val:.2f}% (주의 구간 {th['cet1_worse_lt']} 이상 "
                      f"{th['cet1_watch_lt']} 미만). 인용: {cet1['ctx']}")
            send(cfg, "port", title, base_lines(cfg, now, "CET1 주의(p3) 모니터링 신호.", detail, src), 3, dry)
            mark(st, "cet1_watch", {"val": val, "at": now.isoformat(timespec="seconds")})
            changed = True
        else:
            print(f"CET1 {val:.2f}% no new threshold alert")

    # Rules 3 / 4 TSR + buyback
    official = ev.get("official_tsr") or []
    best = max((t["pct"] for t in official), default=None)
    strong_buy = [b for b in (ev.get("buybacks") or []) if b["eok"] >= th["buyback_strong_eok"]]
    if (best is not None and best >= th["tsr_strong_pct"]) or strong_buy:
        if not already(st, "tsr_strong"):
            if strong_buy and not (best is not None and best >= th["tsr_strong_pct"]):
                b = strong_buy[0]
                detail = f"신규 자사주 매입 {b['eok']:,}억원 (>= {th['buyback_strong_eok']:,}억). 인용: {b['ctx']}"
                why = "자사주 매입 강한 개선(p4)"
            else:
                hit = next(t for t in official if t["pct"] == best)
                detail = (f"공식 2027+ 총주주환원율 목표 {best:.0f}% (>= {th['tsr_strong_pct']}%). "
                          f"인용: {hit['ctx']}")
                why = "총주주환원 강한 개선(p4)"
            title = f"{HEAD} 주주환원 강한 개선"
            send(cfg, "port", title, base_lines(cfg, now, why + " 모니터링 신호.", detail, src), 4, dry)
            mark(st, "tsr_strong", {"best": best, "buy": [b["eok"] for b in strong_buy],
                                    "at": now.isoformat(timespec="seconds")})
            # also mark improve so we don't double-send rule 3
            if not already(st, "tsr_improve"):
                mark(st, "tsr_improve", {"superseded_by": "tsr_strong"})
            changed = True
    elif best is not None and best >= th["tsr_improve_pct"]:
        if not already(st, "tsr_improve") and not already(st, "tsr_strong"):
            hit = next(t for t in official if t["pct"] == best)
            title = f"{HEAD} 주주환원 개선"
            detail = (f"공식 2027+ 총주주환원율 목표 {best:.0f}% (>= {th['tsr_improve_pct']}%). "
                      f"인용: {hit['ctx']}")
            send(cfg, "port", title, base_lines(cfg, now, "총주주환원 개선(p4) 모니터링 신호.", detail, src), 4, dry)
            mark(st, "tsr_improve", {"best": best, "at": now.isoformat(timespec="seconds")})
            changed = True
    else:
        # log personal-only so we know we skipped
        personal = [t for t in (ev.get("tsr") or []) if t.get("personal") and t.get("pct")]
        if personal:
            print(f"skip personal-view TSR {personal[0]['pct']}: {personal[0]['ctx'][:80]}")

    if changed or st.get("earnings_rcp") or st.get("seen_rcps"):
        save_state(st, dry, now)
    if not changed:
        print("no new WOORI alert; silent")
    return 0


# ================================================================ selftest
def selftest() -> int:
    cfg = load_cfg()
    fails = 0

    def check(name, got, want=True):
        nonlocal fails
        ok = got == want
        print(("ok  " if ok else "FAIL"), name, "" if ok else f"got={got!r} want={want!r}")
        fails += not ok

    # CET1 boundaries
    for val, expect_key in [
        (13.49, "worse"), (13.50, "watch"), (13.59, "watch"), (13.60, "none"),
    ]:
        text = f"그룹 기말 CET1비율은 {val}%입니다. 2026년 말 기준."
        ev = evaluate_corpus(text, year_end_mode=True)
        c = ev["cet1"]["value"]
        check(f"CET1 parse {val}", abs(c - val) < 1e-9)
        if expect_key == "worse":
            check(f"CET1 {val} worse", c < 13.5)
        elif expect_key == "watch":
            check(f"CET1 {val} watch", 13.5 <= c < 13.6)
        else:
            check(f"CET1 {val} none", c >= 13.6)

    # label variants
    for label in ("보통주자본비율", "CET1비율", "CET1 Ratio", "CET1"):
        hits = parse_cet1(f"2026년 말 {label} 13.80%")
        check(f"label {label}", bool(hits) and abs(hits[0]["value"] - 13.80) < 1e-9)

    # table variant without immediate words
    hits = parse_cet1("구분 보통주자본비율\n13.80\n총자본비율 16.70")
    check("table CET1", bool(hits) and abs(hits[0]["value"] - 13.80) < 1e-9)

    # official vs personal TSR
    off = classify_tsr("당사는 2027년 총주주환원율 목표를 50%로 상향 추진합니다.")
    check("official 50", bool(off) and off[0]["official"] and off[0]["pct"] == 50)
    pers = classify_tsr("CFO personal view로는 2027 total shareholder return이 50%도 가능해 보입니다.")
    check("personal skipped as official", not any(p.get("official") for p in pers))
    strong = classify_tsr("이사회는 2027년 총주주환원율 목표 55%를 제시했습니다.")
    check("official 55", bool(strong) and strong[0]["pct"] == 55 and strong[0]["official"])

    # buyback 5000억
    bb = parse_buyback_eok("자기주식 취득 결정 5,000억원 규모 자사주 매입을 결의")
    check("buyback 5000", bool(bb) and bb[0]["eok"] == 5000)
    bb2 = parse_buyback_eok("자사주 매입 1,500억원")
    check("buyback 1500 below", bool(bb2) and bb2[0]["eok"] == 1500 and bb2[0]["eok"] < 5000)

    # 40%대 중반 guidance band
    band = classify_tsr("현재 가이던스는 total return 40%대 중반 이상을 유지합니다.")
    check("40%대 band", bool(band) and band[0].get("band") == 40)

    # title rules
    title = f"{HEAD} CET1 악화"
    body = "\n".join(base_lines(cfg, datetime(2027, 2, 5, 15, 20, tzinfo=KST),
                                "테스트", "CET1 13.40%", "https://dart.fss.or.kr"))
    try:
        check_title(title, body)
        check("title rules", True)
    except AssertionError as exc:
        check("title rules", False, True)
        print(" title err", exc)
    check("no 매수 in title", "매수" not in title)
    check("has ticker", TICKER in title)
    check("no banned in body", not any(b in body for b in BANNED))

    # 미게시 routes to ops
    check("미게시->ops", route_topic("cwally-port", f"{HEAD} 4Q 실적 미게시 | 판정 유보"), "cwally-ops")
    check("result->port", route_topic("cwally-port", f"{HEAD} CET1 주의"), "cwally-port")

    # dedup: simulate state
    st = {"sent": {}}
    mark(st, "cet1_watch", {"val": 13.55})
    check("dedup watch", already(st, "cet1_watch"), True)
    check("dedup worse absent", already(st, "cet1_worse"), False)

    # strong supersedes improve logic: both thresholds
    ev = evaluate_corpus(
        "이사회 결정: 2027년 총주주환원율 목표 55%. 아울러 자기주식 취득 6,000억원을 결의.",
        year_end_mode=False,
    )
    check("strong tsr", max(t["pct"] for t in ev["official_tsr"]) >= 55)
    check("strong buyback", max(b["eok"] for b in ev["buybacks"]) >= 5000)

    # real-ish 3Q26 IR excerpt (company-published figures; used when woorifg PDF unreachable)
    q3 = (
        "우리금융그룹 2026년 3분기 실적발표자료. 그룹 보통주자본비율(CET1)은 13.80%입니다. "
        "일회성 요인을 제외하면 13.93~13.95% 수준입니다. "
        "연말 CET1은 13.6~13.7% 수준을 유지할 전망입니다. "
        "주주환원: 현재 가이던스는 total return 40%대 중반 이상입니다."
    )
    q3_hits = parse_cet1(q3)
    check("3Q26 CET1 13.80", bool(q3_hits) and abs(q3_hits[0]["value"] - 13.80) < 1e-9)
    q3_tsr = classify_tsr(q3)
    check("3Q26 40%대 guidance", any(t.get("band") == 40 for t in q3_tsr))

    # is_4q_earnings
    check("3Q prelim not 4Q", is_4q_earnings(
        {"title": "연결재무제표기준영업(잠정)실적(공정공시)"},
        "실적기간 당기실적 2026-07-01 ~ 2026-09-30 (26년 3분기)"), False)
    check("4Q prelim is 4Q", is_4q_earnings(
        {"title": "연결재무제표기준영업(잠정)실적(공정공시)"},
        "실적기간 당기실적 2026-10-01 ~ 2026-12-31 ('26.4Q)"), True)
    check("FY prelim is 4Q", is_4q_earnings(
        {"title": "연결재무제표기준영업(잠정)실적(공정공시)"},
        "2026년 연간 당기실적"), True)

    print("woori selftest", "PASS" if not fails else f"FAIL {fails}", f"checks_failed={fails}")
    return 1 if fails else 0


def main() -> int:
    args = sys.argv[1:]
    mode = (args[0] if args else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry"}:
        raise SystemExit(f"unknown mode: {mode} (auto|dry|selftest)")
    now = now_kst()
    if "--now" in args:
        if mode != "dry":
            raise SystemExit("--now is for dry only")
        now = datetime.strptime(args[args.index("--now") + 1], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    cfg = load_cfg()
    st = load_state()
    if mode == "dry":
        print(f"DRY RUN (no ntfy, no state write) now={now:%Y-%m-%d %H:%M} KST")
    # dry ignores date gate so manual dispatch can exercise fetch/parse before the window
    if mode != "dry" and not in_window(cfg, now):
        print(f"outside WOORI window {cfg['window_start']}~{cfg['window_end']}; silent")
        return 0
    return run(cfg, st, now, mode)


if __name__ == "__main__":
    raise SystemExit(main())
