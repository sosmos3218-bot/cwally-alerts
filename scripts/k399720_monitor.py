#!/usr/bin/env python3
"""[K399720] 399720 가온칩스 공시 + 종가 구간. 매수/매도 지시 아님.

KRX 거래일 19:00 k203780 슬롯. 새 cron 없음.
  1) DART 신규 공시 1건당 1통. 첫 성공 실행은 시드만.
     분기/반기/사업보고서, 영업(잠정)실적, 매출액 또는 손익구조 변동,
     단일판매 공급계약 체결, 해지, 금액이 줄어든 정정,
     제목에 2nm/2나노, DeepX/딥엑스, 지연, 해지, 취소.
  2) 2026년 연간 실적(잠정 또는 사업보고서)이 나오면 매출 1,000억 이상과
     영업이익 흑자(조건 a)를 본문에 적음. 숫자를 못 읽으면 충족이 아님.
  3) 정규장 종가 <= 25,750 (52주 장중 저가 이탈 경고), >= 50,000 (관찰 구간, 목표가 아님).
     들어올 때 1번, 구간을 벗어나면 재무장.
조회 실패는 cwally-ops 하루 1번.
modes: auto | dry | selftest
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import traceback
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_calendar import KST, is_trading_day, session_day  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

NAME = "k399720"
TAG = "[K399720]"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts k399720"}
DART = "https://dart.fss.or.kr"
DART_VIEW = DART + "/dsaf001/main.do?rcpNo={rcp}"
DART_SEARCH = DART + "/dsab007/detailSearch.ax"
OPENDART_LIST = "https://opendart.fss.or.kr/api/list.json"
OPENDART_DOC = "https://opendart.fss.or.kr/api/document.xml"
OPENDART_ACCT = "https://opendart.fss.or.kr/api/fnlttSinglAcnt.json"
KIND = "https://kind.krx.co.kr"
DOT = r"[\u318d\u00b7\u2027\u30fb]"
SUPPLY_RE = re.compile(r"단일판매\s*" + DOT + r"?\s*공급계약")
BANNED_RE = re.compile("[\u00b7\u2014\u2013]")
OPS = {
    "le": lambda v, x: v <= x,
    "lt": lambda v, x: v < x,
    "ge": lambda v, x: v >= x,
    "gt": lambda v, x: v > x,
}


def load_cfg() -> dict:
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


def default_state() -> dict:
    return {
        "updated_at": None,
        "seeded": False,
        "price_seeded": False,
        "seen": [],
        "arms": {},
        "last_close": None,
        "ops_day": None,
    }


def load_state() -> dict:
    st = default_state()
    if STATE_PATH.exists():
        try:
            raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                st.update(raw)
        except json.JSONDecodeError:
            pass
    st["seen"] = list(st.get("seen") or [])
    if not isinstance(st.get("arms"), dict):
        st["arms"] = {}
    return st


def save_state(st: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def clean(s: str) -> str:
    s = (s or "").replace("\u00b7", ", ").replace("\u2014", ", ").replace("\u2013", ", ")
    return re.sub(r"[ \t]{2,}", " ", s)


def check_text(*parts: str) -> None:
    blob = "".join(parts)
    bad = BANNED_RE.search(blob)
    if bad:
        raise AssertionError(f"forbidden char {bad.group(0)!r}")


def api_key() -> str:
    return (os.environ.get("OPENDART_API_KEY") or "").strip()


def redact(text: str) -> str:
    return re.sub(r"crtfc_key=[^&\s]+", "crtfc_key=REDACTED", text or "")[:240]


def http(url: str, data: dict | None = None, timeout: int = 25, binary: bool = False):
    body = urlencode(data).encode() if data else None
    headers = dict(UA)
    if body:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    err: Exception | None = None
    for i in range(3):
        try:
            req = Request(url, data=body, headers=headers, method="POST" if body else "GET")
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if binary:
                    return raw
                ctype = (resp.headers.get("Content-Type") or "").lower()
                enc = "euc-kr" if "euc-kr" in ctype else "utf-8"
                return raw.decode(enc, "replace")
        except Exception as exc:  # noqa: BLE001
            err = exc
            if i == 2:
                break
    raise RuntimeError(f"{url.split('?')[0]}: {redact(str(err))}")


def text_of(html: str) -> str:
    s = re.sub(r"(?is)<script[\s\S]*?</script>", " ", html or "")
    s = re.sub(r"(?is)<style[\s\S]*?</style>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</tr>", "\n", s)
    s = re.sub(r"(?i)</t[dh]>", " | ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#39;", "'")
    s = s.replace("\u00a0", " ")
    return re.sub(r"[ \t]+", " ", s)


def ended(cfg: dict, now: datetime) -> bool:
    raw = cfg.get("ends_kst") or ""
    try:
        end = datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    except ValueError:
        return False
    return now > end


def num(tok: str | None) -> float | None:
    if tok is None:
        return None
    s = str(tok).strip().replace(",", "").replace(" ", "")
    if not s or s in {"-", "—", "–"}:
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1]
    if s.startswith("△") or s.startswith("▲"):
        neg = True
        s = s[1:]
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def won_text(v: float | None) -> str:
    if v is None:
        return "금액 못 읽음"
    sign = "-" if v < 0 else ""
    a = abs(v)
    if a >= 1e12:
        return f"{sign}{a / 1e12:,.2f}조원"
    return f"{sign}{a / 1e8:,.1f}억원"


def make_title(head: str) -> str:
    return clean(f"{TAG} 399720 가온칩스 {head}")


def body(stamp: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([
        f"{stamp} | 399720 | 가온칩스",
        f"이게 뭐 알림: {what}",
        f"지금 결과/상태: {status}",
        f"지금 할 일: {action}",
        f"나중에: {later}",
        f"익절/줄일 때: {trim}",
        f"하지 말 것: {avoid}",
    ])


WHAT = (
    "가온칩스(399720) 공시와 종가 구간 알림. "
    "새 보고서, 잠정실적, 손익구조 변동, 공급계약, 해지, 금액 축소, "
    "2나노/DeepX/지연/취소 제목, 2026년 연간 매출 1,000억과 영업이익 흑자, "
    "종가 25,750원과 50,000원. 비중을 바꾸라는 지시가 아님."
)
LATER = "같은 접수번호는 다시 안 보냅니다. 정정은 새 접수번호일 때 1번 더 옵니다."
TRIM = "익절 신호가 아님. 50,000원은 관찰 구간이고 목표가 아님."
AVOID = "이 알림만 보고 비중을 바꾸기. 빈 금액을 추정해서 채우기."


# ---------- 분류 ----------
def classify(title: str) -> dict:
    t = title or ""
    compact = re.sub(r"\s+", "", t)
    supply = bool(SUPPLY_RE.search(t))
    cancel = supply and ("해지" in t)
    correction = supply and ("정정" in t) and not cancel
    new_order = supply and not cancel
    period = bool(re.search(r"(분기|반기|사업)보고서", t))
    provisional = ("잠정" in t) and ("실적" in t)
    profit_struct = ("손익구조" in compact) or ("매출액또는손익" in compact)
    keys = []
    if re.search(r"2\s*nm|2나노", t, re.I):
        keys.append("2nm")
    if re.search(r"deepx|딥엑스", t, re.I):
        keys.append("DeepX")
    for word, label in (("지연", "지연"), ("해지", "해지"), ("취소", "취소")):
        if word in t:
            keys.append(label)
    relevant = bool(supply or period or provisional or profit_struct or keys)
    return {
        "relevant": relevant,
        "supply": supply,
        "cancel": cancel,
        "correction": correction,
        "new_order": new_order,
        "period": period,
        "business": "사업보고서" in t and "반기" not in t and "분기" not in t,
        "provisional": provisional,
        "profit_struct": profit_struct,
        "keywords": keys,
    }


def is_fy_candidate(cfg: dict, filing: dict, text: str = "") -> bool:
    """2026년 연간 실적 후보. 분기 3개월 열은 여기서 걸러지지 않고 숫자 단계에서 거절."""
    annual = cfg["annual"]
    year = str(annual["year"])
    filed = (filing.get("filed") or "")[:10]
    kind = filing.get("kind") or classify(filing.get("title") or "")
    if kind.get("business") and filed >= annual["business_report_from"]:
        return True
    if not kind.get("provisional"):
        return False
    if not (annual["provisional_from"] <= filed <= annual["provisional_until"]):
        return False
    if re.search(r"1분기|2분기|3분기|반기", filing.get("title") or ""):
        return False
    if text and not full_year_text(text, year) and quarter_text(text, year):
        return False
    return True


def full_year_text(text: str, year: str) -> bool:
    t = re.sub(r"\s+", "", text or "")
    if re.search(rf"{year}[.\-/년]*0?1[.\-/월]*0?1일?부터?{year}[.\-/년]*12[.\-/월]*31", t):
        return True
    if re.search(rf"{year}년?(연간|결산|1월?부터?12월|1~12)", t):
        return True
    return False


def quarter_text(text: str, year: str) -> bool:
    t = re.sub(r"\s+", "", text or "")
    if "3개월" in (text or "") and "사업보고서" not in (text or ""):
        return True
    return bool(re.search(rf"{year}[.\-/년]*0?[7-9][.\-/월]*0?1", t)) and "12월31" not in t and "12.31" not in t


# ---------- 목록 ----------
def _row(rcp: str, title: str, filed: str, src: str) -> dict:
    rcp = (rcp or "").strip()
    filed = (filed or "")[:10]
    if len(filed) != 10 and len(rcp) == 14:
        filed = f"{rcp[:4]}-{rcp[4:6]}-{rcp[6:8]}"
    return {
        "id": f"dart:{rcp}" if rcp else "",
        "rcp": rcp,
        "title": (title or "").strip(),
        "filed": filed,
        "src": src,
        "url": DART_VIEW.format(rcp=rcp) if rcp else "",
    }


def opendart_list(cfg: dict, since: str, until: str) -> list[dict]:
    key = api_key()
    if not key:
        raise RuntimeError("OPENDART_API_KEY 없음")
    out: list[dict] = []
    page = 1
    while page <= 5:
        qs = urlencode({
            "crtfc_key": key,
            "corp_code": cfg["dart_corp_code"],
            "bgn_de": since.replace("-", ""),
            "end_de": until.replace("-", ""),
            "page_count": "100",
            "page_no": str(page),
        })
        data = json.loads(http(f"{OPENDART_LIST}?{qs}", timeout=25))
        status = str(data.get("status") or "")
        if status not in {"000", "013"}:
            raise RuntimeError(f"OpenDART list {status}")
        rows = data.get("list") or []
        for r in rows:
            rcp = (r.get("rcept_no") or "").strip()
            if rcp:
                out.append(_row(rcp, r.get("report_nm") or "", r.get("rcept_dt") or "", "OpenDART"))
        total = int(data.get("total_page") or 1)
        if page >= total or not rows:
            break
        page += 1
    return out


def parse_dart_html(page: str) -> list[dict]:
    out = []
    seen = set()
    for m in re.finditer(r'rcpNo=(\d{14})"[^>]*title="([^"]+)"', page or ""):
        rcp, title = m.group(1), m.group(2)
        if rcp in seen:
            continue
        seen.add(rcp)
        title = re.sub(r"\s*공시뷰어\s*새창\s*$", "", title).strip()
        out.append(_row(rcp, title, "", "DART"))
    return out


def public_dart_list(cfg: dict, since: str, until: str) -> list[dict]:
    out = []
    seen = set()
    for page in range(1, 6):
        q = urlencode({
            "textCrpNm": cfg["corp_name"],
            "startDate": since.replace("-", ""),
            "endDate": until.replace("-", ""),
            "maxResults": "100",
            "currentPage": str(page),
        })
        html = http(DART_SEARCH + "?" + q, timeout=25)
        rows = parse_dart_html(html)
        fresh = [r for r in rows if r["rcp"] not in seen]
        if not fresh and page == 1 and cfg["corp_name"] not in (html or "") and "rcpNo=" not in (html or ""):
            raise RuntimeError("DART 목록 형식 이상")
        if not fresh:
            break
        for r in fresh:
            seen.add(r["rcp"])
            out.append(r)
        if len(rows) < 100:
            break
    return out


def kind_list(cfg: dict, since: str, until: str) -> list[dict]:
    code = cfg["code"]
    form = {
        "method": "searchDetailsSub", "currentPageSize": "100", "pageIndex": "1",
        "orderMode": "1", "orderStat": "D", "forward": "details_sub",
        "searchCodeType": "", "repIsuSrtCd": "A" + code, "allRepIsuSrtCd": "",
        "oldSearchCorpName": "", "disclosureType": "", "disTypevalue": "",
        "reportNm": "", "reportCd": "", "searchCorpName": code, "business": "",
        "marketType": "", "settlementMonth": "", "securities": "",
        "submitOblgNm": "", "enterprise": "", "fromDate": since, "toDate": until,
        "reportNmTemp": "", "bfrDsclsType": "on",
    }
    page = http(KIND + "/disclosure/details.do", data=form, timeout=25)
    out = []
    rows = 0
    for row in re.findall(r"(?s)<tr[^>]*>(.*?)</tr>", page):
        acpt = re.search(r"openDisclsViewer\('(\d{14})'", row)
        if not acpt:
            continue
        rows += 1
        t = text_of(row)
        dm = re.search(r"(\d{4}-\d{2}-\d{2})", t)
        title = t[dm.end():].strip() if dm else t
        title = re.sub(r"\s*(공시차트|주가차트).*$", "", title).strip()
        out.append({
            "id": "kind:" + acpt.group(1),
            "rcp": acpt.group(1),
            "title": title,
            "filed": dm.group(1) if dm else "",
            "src": "KIND",
            "url": f"https://kind.krx.co.kr/common/disclsviewer.do?method=search&acptno={acpt.group(1)}",
        })
    if rows == 0 and "조회된 결과값이 없습니다" not in page and "<table" not in page:
        raise RuntimeError("KIND 응답 형식 이상")
    return out


def naver_list(cfg: dict, since: str, until: str) -> list[dict]:
    raw = json.loads(http(
        f"https://stock.naver.com/api/domestic/detail/notice?itemCode={cfg['code']}&page=1&pageSize=100"
    ))
    out = []
    for r in raw if isinstance(raw, list) else []:
        day = str(r.get("datetime") or "")[:10]
        if len(day) != 10 or not (since <= day <= until):
            continue
        nid = str(r.get("no") or r.get("bizdateSeq") or "")
        if not nid:
            continue
        out.append({
            "id": "naver:" + nid,
            "rcp": "",
            "title": str(r.get("title") or "").strip(),
            "filed": day,
            "src": "네이버",
            "url": f"https://finance.naver.com/item/news_notice.naver?code={cfg['code']}",
            "body": text_of(str(r.get("contents") or "")),
        })
    return out


def list_filings(cfg: dict, since: str, until: str) -> tuple[list[dict], str]:
    notes = []
    try:
        rows = opendart_list(cfg, since, until)
        src = "OpenDART"
    except Exception as exc:  # noqa: BLE001
        notes.append(f"OpenDART: {redact(str(exc))}")
        try:
            rows = public_dart_list(cfg, since, until)
            src = "DART"
        except Exception as exc2:  # noqa: BLE001
            notes.append(f"DART: {redact(str(exc2))}")
            try:
                rows = kind_list(cfg, since, until)
                src = "KIND"
            except Exception as exc3:  # noqa: BLE001
                notes.append(f"KIND: {redact(str(exc3))}")
                try:
                    rows = naver_list(cfg, since, until)
                    src = "네이버"
                except Exception as exc4:  # noqa: BLE001
                    notes.append(f"네이버: {redact(str(exc4))}")
                    raise RuntimeError("; ".join(notes)) from exc4
    kept = []
    for r in rows:
        kind = classify(r.get("title") or "")
        if not kind["relevant"]:
            continue
        r["kind"] = kind
        if not r.get("id"):
            continue
        kept.append(r)
    kept.sort(key=lambda r: (r.get("filed") or "", r.get("id") or ""))
    note = f"{src} {len(rows)}건 중 대상 {len(kept)}건"
    if notes:
        note += " | " + " | ".join(notes)
    return kept, note


# ---------- 본문 ----------
def opendart_document(rcp: str) -> str:
    key = api_key()
    if not key or not rcp:
        return ""
    raw = http(f"{OPENDART_DOC}?crtfc_key={key}&rcept_no={rcp}", binary=True, timeout=60)
    if not isinstance(raw, (bytes, bytearray)):
        return ""
    if raw[:2] != b"PK":
        return text_of(raw.decode("utf-8", "replace"))
    zf = zipfile.ZipFile(io.BytesIO(raw))
    parts = []
    for name in zf.namelist():
        data = zf.read(name)
        for enc in ("utf-8", "cp949", "euc-kr"):
            try:
                parts.append(data.decode(enc))
                break
            except UnicodeDecodeError:
                continue
    return text_of("\n".join(parts))


def viewer_nodes(page: str) -> list[dict]:
    pat = re.compile(
        r"node\d+\['text'\]\s*=\s*\"([^\"]+)\";\s*"
        r"node\d+\['id'\]\s*=\s*\"(\d+)\";\s*"
        r"node\d+\['rcpNo'\]\s*=\s*\"(\d+)\";\s*"
        r"node\d+\['dcmNo'\]\s*=\s*\"(\d+)\";\s*"
        r"node\d+\['eleId'\]\s*=\s*\"(\d+)\";\s*"
        r"node\d+\['offset'\]\s*=\s*\"(\d+)\";\s*"
        r"node\d+\['length'\]\s*=\s*\"(\d+)\";\s*"
        r"node\d+\['dtd'\]\s*=\s*\"([^\"]+)\"",
        re.S,
    )
    out = []
    for m in pat.finditer(page or ""):
        out.append({
            "text": m.group(1), "rcp": m.group(3), "dcm": m.group(4),
            "ele": m.group(5), "off": m.group(6), "length": m.group(7), "dtd": m.group(8),
        })
    return out


def fetch_viewer(node: dict) -> str:
    q = urlencode({
        "rcpNo": node["rcp"], "dcmNo": node["dcm"], "eleId": node["ele"],
        "offset": node["off"], "length": node["length"], "dtd": node["dtd"],
    })
    return http(f"{DART}/report/viewer.do?{q}", timeout=40)


def public_document(rcp: str, want: str) -> str:
    if not rcp:
        return ""
    page = http(DART_VIEW.format(rcp=rcp), timeout=30)
    nodes = viewer_nodes(page)
    if not nodes:
        m = re.search(
            r'viewDoc\("(\d+)",\s*"(\d+)",\s*"(\d+)",\s*"(\d+)",\s*"(\d+)",\s*"([^"]+)"',
            page,
        )
        if not m:
            return text_of(page)[:8000]
        nodes = [{
            "text": "", "rcp": m.group(1), "dcm": m.group(2), "ele": m.group(3),
            "off": m.group(4), "length": m.group(5), "dtd": m.group(6),
        }]
    picked = nodes
    if want == "contract":
        picked = nodes[:3]
    elif want == "annual":
        income = [n for n in nodes if "포괄손익" in n["text"] and "주석" not in n["text"]]
        cons = [n for n in income if "연결" in n["text"]]
        picked = (cons or income or nodes)[:2]
    chunks = []
    for n in picked:
        if int(n["length"]) > 400000:
            continue
        try:
            chunks.append(fetch_viewer(n))
        except Exception:  # noqa: BLE001
            continue
    return "\n".join(text_of(c) for c in chunks)


def document_text(rcp: str, want: str, fallback: str = "") -> str:
    if fallback:
        return fallback
    try:
        text = opendart_document(rcp)
        if text.strip():
            return text
    except Exception:  # noqa: BLE001
        pass
    try:
        return public_document(rcp, want)
    except Exception:  # noqa: BLE001
        return ""


def parse_contract(text: str) -> dict | None:
    t = re.sub(r"\s+", " ", text or "")
    if not t.strip():
        return None
    anchors = [m.start() for m in re.finditer(r"단일판매\s*" + DOT + r"?\s*공급계약", t)]
    s = t[anchors[-1]:] if anchors else t

    def grab(*pats: str) -> str | None:
        for p in pats:
            m = re.search(p, s)
            if m:
                return m.group(1).strip()
        return None

    amount = num(grab(
        r"계약금액\s*총액\s*\(\s*원\s*\)\s*(\(?-?[\d,]+\)?|-)",
        r"확정\s*계약금액\s*(\(?-?[\d,]+\)?|-)",
        r"계약금액\s*\(\s*원\s*\)\s*(\(?-?[\d,]+\)?|-)",
    ))
    ratio = num(grab(
        r"매출액\s*대비\s*\(\s*%\s*\)\s*(\(?-?[\d.]+\)?|-)",
        r"매출액대비\s*\(\s*%\s*\)\s*(\(?-?[\d.]+\)?|-)",
    ))
    party = grab(
        r"3\.\s*계약상대(?:방)?\s+(.+?)\s+-\s*(?:최근\s*매출액|주요사업|회사와)",
        r"3\.\s*계약상대(?:방)?\s+(.+?)\s+4\.\s*판매",
    )

    def grab_all(*pats: str) -> str | None:
        for p in pats:
            m = re.search(p, t)
            if m:
                return m.group(1).strip()
        return None

    before = num(grab_all(
        r"정정\s*전\s*계약금액(?:\s*\(\s*원\s*\))?\s*(\(?-?[\d,]+\)?)",
        r"변경\s*전\s*계약금액(?:\s*\(\s*원\s*\))?\s*(\(?-?[\d,]+\)?)",
    ))
    after = num(grab_all(
        r"정정\s*후\s*계약금액(?:\s*\(\s*원\s*\))?\s*(\(?-?[\d,]+\)?)",
        r"변경\s*후\s*계약금액(?:\s*\(\s*원\s*\))?\s*(\(?-?[\d,]+\)?)",
    ))
    reduced = before is not None and after is not None and after < before
    if amount is None and party is None and before is None and after is None:
        return None
    return {
        "amount": after if after is not None else amount,
        "ratio": ratio,
        "party": party,
        "before": before,
        "after": after,
        "reduced": reduced,
    }


def parse_income_text(text: str) -> dict:
    """손익 본문에서 매출액, 영업이익. 3개월 표는 full_year False."""
    raw = text or ""
    quarterly = "3개월" in raw and "사업보고서" not in raw
    t = re.sub(r"\s+", " ", raw)
    unit = 1.0
    if re.search(r"단위\s*:\s*백만원|단위\s*백만원", t):
        unit = 1_000_000.0
    elif re.search(r"단위\s*:\s*억원|단위\s*억원", t):
        unit = 100_000_000.0
    elif re.search(r"단위\s*:\s*천원", t):
        unit = 1_000.0

    def row_amount(label: str) -> float | None:
        m = re.search(label + r"\s*(\(?-?[\d,]+\)?)", t)
        if not m:
            return None
        v = num(m.group(1))
        if v is None:
            return None
        return v * unit

    rev = row_amount(r"(?<![\w])매출액")
    op = row_amount(r"영업이익(?:\(손실\))?")
    return {"revenue": rev, "op": op, "full_year": not quarterly, "scope": "본문"}


def pick_accounts(rows: list[dict]) -> dict:
    """OpenDART fnlttSinglAcnt rows. 연결(CFS) 우선, 없으면 별도."""
    def take(scope: str) -> tuple[float | None, float | None]:
        rev = op = None
        for r in rows or []:
            if str(r.get("fs_div") or "").upper() != scope:
                continue
            name = str(r.get("account_nm") or "")
            amt = num(r.get("thstrm_amount"))
            if amt is None:
                continue
            if name in {"매출액", "수익(매출액)"}:
                rev = amt
            elif name in {"영업이익", "영업이익(손실)"}:
                op = amt
        return rev, op

    rev, op = take("CFS")
    scope = "연결"
    if rev is None and op is None:
        rev, op = take("OFS")
        scope = "별도"
    return {"revenue": rev, "op": op, "full_year": True, "scope": scope}


def judge_annual(cfg: dict, parsed: dict | None) -> dict:
    annual = cfg["annual"]
    parsed = parsed or {}
    rev, op = parsed.get("revenue"), parsed.get("op")
    if not parsed.get("full_year"):
        return {"verdict": "보류", "revenue": rev, "op": op, "why": "연간 열이 아님"}
    if rev is None or op is None:
        return {"verdict": "보류", "revenue": rev, "op": op, "why": "매출 또는 영업이익을 원문에서 못 읽음"}
    rev_ok = rev >= float(annual["revenue_min_won"])
    op_ok = op > float(annual["op_gt"])
    if rev_ok and op_ok:
        return {"verdict": "충족", "revenue": rev, "op": op, "why": "매출 1,000억 이상, 영업이익 흑자"}
    bits = []
    if not rev_ok:
        bits.append(f"매출 {won_text(rev)} (기준 1,000억 미만)")
    else:
        bits.append(f"매출 {won_text(rev)} (1,000억 이상)")
    if not op_ok:
        bits.append(f"영업이익 {won_text(op)} (흑자 아님)")
    else:
        bits.append(f"영업이익 {won_text(op)} (흑자)")
    return {"verdict": "미충족", "revenue": rev, "op": op, "why": ", ".join(bits)}


def opendart_accounts(cfg: dict) -> list[dict] | None:
    key = api_key()
    if not key:
        return None
    year = str(cfg["annual"]["year"])
    qs = urlencode({
        "crtfc_key": key,
        "corp_code": cfg["dart_corp_code"],
        "bsns_year": year,
        "reprt_code": cfg["annual"]["reprt_code"],
    })
    data = json.loads(http(f"{OPENDART_ACCT}?{qs}", timeout=25))
    status = str(data.get("status") or "")
    if status == "013":
        return []
    if status != "000":
        raise RuntimeError(f"OpenDART accounts {status}")
    return list(data.get("list") or [])


def annual_numbers(cfg: dict, filing: dict, text: str) -> dict:
    kind = filing.get("kind") or {}
    if kind.get("business"):
        try:
            rows = opendart_accounts(cfg)
        except Exception:  # noqa: BLE001
            rows = None
        if rows:
            return pick_accounts(rows)
    parsed = parse_income_text(text)
    if kind.get("business"):
        parsed["full_year"] = "3개월" not in (text or "")
    elif not full_year_text(text, str(cfg["annual"]["year"])):
        parsed["full_year"] = False
    return parsed


# ---------- 메시지 ----------
def contract_bits(parsed: dict | None) -> str:
    if not parsed:
        return "본문 금액을 못 읽음. 추정하지 않음."
    parts = []
    if parsed.get("party"):
        parts.append(f"계약상대: {parsed['party']}")
    if parsed.get("before") is not None or parsed.get("after") is not None:
        parts.append(f"정정 전 {won_text(parsed.get('before'))}, 정정 후 {won_text(parsed.get('after'))}")
    elif parsed.get("amount") is not None:
        parts.append(f"계약금액 {won_text(parsed['amount'])} ({parsed['amount']:,.0f}원)")
    else:
        parts.append("계약금액 못 읽음. 추정하지 않음.")
    if parsed.get("ratio") is not None:
        parts.append(f"매출 대비 {parsed['ratio']}%")
    else:
        parts.append("매출 대비 비율 못 읽음")
    return ". ".join(parts) + "."


def filing_message(cfg: dict, filing: dict, parsed: dict | None, annual: dict | None, stamp: str) -> tuple[str, str, int]:
    kind = filing.get("kind") or classify(filing.get("title") or "")
    link = filing.get("url") or "링크 없음"
    title_s = clean(filing.get("title") or "")
    pri = 3
    head = title_s[:40] or "공시"
    if kind.get("cancel") or (kind.get("correction") and parsed and parsed.get("reduced")):
        pri = 5
        head = "공급계약 해지 | 수주 축소" if kind.get("cancel") else "공급계약 정정 | 금액 축소"
    elif kind.get("new_order"):
        pri = 4
        extra = []
        if parsed and parsed.get("amount") is not None:
            extra.append(won_text(parsed["amount"]))
        if parsed and parsed.get("ratio") is not None:
            extra.append(f"매출 대비 {parsed['ratio']}%")
        suffix = (" | " + ", ".join(extra)) if extra else ""
        head = ("공급계약 정정" if kind.get("correction") else "공급계약 체결") + suffix
    elif annual is not None:
        pri = 4
        label = {"충족": "조건 a 충족", "미충족": "조건 a 미충족", "보류": "조건 a 판단 보류"}[annual["verdict"]]
        head = f"2026년 연간 실적 | {label}"
    elif kind.get("keywords"):
        pri = 4
        head = "공시 | " + ", ".join(kind["keywords"])
    elif kind.get("provisional"):
        head = "잠정실적"
    elif kind.get("profit_struct"):
        head = "손익구조 변동"
    elif kind.get("period"):
        head = "정기보고서"

    status = f"{filing.get('filed') or '?'} '{title_s}' ({filing.get('src') or '?'}). 링크: {link}"
    if kind.get("supply"):
        status += " " + contract_bits(parsed)
        if kind.get("cancel") or (parsed and parsed.get("reduced")):
            status += " 수주 취소 또는 축소로 표시."
    if annual is not None:
        status += (
            f" 조건 a ({cfg['annual']['fiscal_label']} 연간, 매출 "
            f"{cfg['annual']['revenue_min_won']:,.0f}원 이상, 영업이익 > {cfg['annual']['op_gt']}): "
            f"{annual['why']}."
        )
        if annual.get("revenue") is not None or annual.get("op") is not None:
            status += f" 읽은 값 매출 {won_text(annual.get('revenue'))}, 영업이익 {won_text(annual.get('op'))}."
    if kind.get("keywords"):
        status += " 제목 단어: " + ", ".join(kind["keywords"]) + "."
    action = "원문 링크에서 금액, 상대, 기간, 실적을 확인하세요. 비중 변경 지시가 아닙니다."
    if annual is not None and annual["verdict"] == "충족":
        action = "조건 a(연간 매출 1,000억 이상, 영업이익 흑자) 숫자가 맞습니다. 원문을 다시 보세요. 비중 변경 지시가 아닙니다."
    elif annual is not None and annual["verdict"] == "미충족":
        action = "조건 a는 미충족입니다. 원문 숫자와 다르면 그 숫자를 따르세요."
    elif annual is not None:
        action = "연간 매출과 영업이익을 원문에서 직접 읽으세요. 못 읽은 값은 충족으로 보지 않습니다."
    msg = clean(body(stamp, WHAT, status, action, LATER, TRIM, AVOID))
    return make_title(head), msg, pri


def price_zone(line: dict, close: float) -> bool:
    return OPS[line["op"]](close, float(line["level"]))


def price_rearm(line: dict, close: float) -> bool:
    return OPS[line["rearm_op"]](close, float(line["rearm"]))


def seed_arms(cfg: dict, close: float) -> dict:
    return {ln["key"]: (not price_zone(ln, close)) for ln in cfg["lines"]}


def price_alerts(cfg: dict, st: dict, day: str, close: float, stamp: str) -> list[tuple[str, str, int]]:
    out = []
    arms = st.setdefault("arms", {})
    for ln in cfg["lines"]:
        key = ln["key"]
        armed = arms.get(key, True)
        if price_zone(ln, close) and armed:
            arms[key] = False
            title = make_title(f"종가 {close:,.0f}원 | {ln['label']}")
            if key == "low52":
                status = (
                    f"{day} 정규장 종가 {close:,.0f}원. 52주 장중 저가 {ln['level']:,.0f}원 이하. "
                    f"종가가 {ln['rearm']:,.0f}원보다 위로 나오면 다시 무장."
                )
                action = "저점 이탈인지 시세만 대조하세요. 비중을 바꾸라는 뜻이 아닙니다."
            else:
                status = (
                    f"{day} 정규장 종가 {close:,.0f}원. 관찰 구간 {ln['level']:,.0f}원 이상. "
                    f"목표가 아님. 종가가 {ln['rearm']:,.0f}원 미만으로 나오면 다시 무장."
                )
                action = "가격이 관찰 구간에 들어왔는지만 보세요. 목표가나 청산 신호가 아닙니다."
            out.append((title, clean(body(stamp, WHAT, status, action, LATER, TRIM, AVOID)), int(ln["priority"])))
        elif price_rearm(ln, close):
            arms[key] = True
    st["last_close"] = {"day": day, "close": close}
    return out


def already_seen(st: dict, filing: dict) -> bool:
    return filing.get("id") in set(st.get("seen") or [])


def mark_seen(st: dict, filing: dict) -> None:
    seen = st.setdefault("seen", [])
    if filing.get("id") and filing["id"] not in seen:
        seen.append(filing["id"])


def build_filing_alert(cfg: dict, filing: dict, stamp: str, text: str) -> tuple[str, str, int]:
    kind = filing.get("kind") or classify(filing.get("title") or "")
    filing["kind"] = kind
    parsed = parse_contract(text) if kind.get("supply") else None
    annual = None
    if is_fy_candidate(cfg, filing, text):
        annual = judge_annual(cfg, annual_numbers(cfg, filing, text))
    return filing_message(cfg, filing, parsed, annual, stamp)


def evaluate(cfg: dict, st: dict, filings: list[dict], close: tuple[str, float] | None,
             stamp: str, texts: dict[str, str] | None = None) -> list[tuple[str, str, int]]:
    """texts maps filing id -> body. Missing body is fetched only for new, non-seed filings by the caller."""
    texts = texts or {}
    alerts: list[tuple[str, str, int]] = []
    seed = not st.get("seeded")
    for filing in filings:
        if already_seen(st, filing):
            continue
        mark_seen(st, filing)
        if seed:
            continue
        alerts.append(build_filing_alert(cfg, filing, stamp, texts.get(filing["id"], "")))
    if seed:
        st["seeded"] = True
        st["seeded_at"] = stamp
    if close is None:
        return alerts
    day, px = close
    if not st.get("price_seeded"):
        st["arms"] = seed_arms(cfg, px)
        st["price_seeded"] = True
        st["last_close"] = {"day": day, "close": px}
        return alerts
    alerts.extend(price_alerts(cfg, st, day, px, stamp))
    return alerts


def ops_alert(st: dict, today: str, detail: str, stamp: str) -> list[tuple[str, str, int]]:
    if not detail:
        st["ops_day"] = None
        return []
    if st.get("ops_day") == today:
        return []
    st["ops_day"] = today
    title = make_title("조회 실패")
    status = f"오늘 데이터를 일부 또는 전부 읽지 못함. {detail[:400]}"
    msg = body(
        stamp, "가온칩스 알림의 조회 실패. 하루 1번. 비중 변경 지시가 아님.",
        status, "DART와 증권사 시세에서 공시와 종가를 직접 보세요.",
        "다음 슬롯에서 다시 읽습니다.",
        "실패를 가격 신호로 쓰지 마세요.",
        "못 읽은 숫자를 채워서 조건 충족으로 보지 마세요.",
    )
    return [(clean(title), clean(msg), 3)]


def latest_close(code: str, now: datetime) -> tuple[str, float]:
    import krx_close
    rows = krx_close.session_closes(code, n=5, now=now, compare=False)
    if not rows:
        raise RuntimeError("종가 없음")
    day, close = rows[-1]
    return day, float(close)


def run_alerts(mode, now, cfg) -> int:
    dry = mode == "dry"
    topic = resolve_topic("port")
    server = resolve_server(cfg.get("ntfy_server"))
    day = session_day(now)
    today = day.isoformat()
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    if ended(cfg, now):
        print(f"ended after {cfg.get('ends_kst')}; silent")
        return 0
    if mode != "dry" and not is_trading_day(day):
        print(f"{today} KRX 휴장; silent")
        return 0

    since = (now - timedelta(days=int(cfg.get("lookback_days") or 400))).strftime("%Y-%m-%d")
    note, ferr = "", ""
    filings: list[dict] = []
    try:
        filings, note = list_filings(cfg, since, today)
    except Exception as exc:  # noqa: BLE001
        ferr = redact(str(exc))
    close = None
    perr = ""
    try:
        close = latest_close(cfg["code"], now)
    except Exception as exc:  # noqa: BLE001
        perr = redact(str(exc))
    titles = [f"{r.get('filed')} {r.get('rcp')} {r.get('title')}" for r in filings[:12]]
    print(json.dumps({
        "note": note, "filings": len(filings), "sample": titles, "close": close,
        "filing_err": ferr, "price_err": perr,
    }, ensure_ascii=False))

    st = load_state()
    seed = not st.get("seeded")
    texts: dict[str, str] = {}
    if not seed and not ferr:
        for filing in filings:
            if already_seen(st, filing):
                continue
            kind = filing.get("kind") or classify(filing.get("title") or "")
            filing["kind"] = kind
            if kind.get("supply"):
                want = "contract"
            elif is_fy_candidate(cfg, filing, ""):
                want = "annual"
            else:
                texts[filing["id"]] = filing.get("body") or ""
                continue
            texts[filing["id"]] = document_text(filing.get("rcp") or "", want, filing.get("body") or "")

    indexed: list[tuple[dict | None, tuple[str, str, int]]] = []
    if not ferr:
        before = set(st.get("seen") or [])
        alerts = evaluate(cfg, st, filings, close, stamp, texts)
        new_ids = [f for f in filings if f["id"] not in before]
        # evaluate appends filing alerts in new_ids order, then price alerts.
        n_file = 0 if seed else len(new_ids)
        for filing, alert in zip(new_ids, alerts[:n_file]):
            indexed.append((filing, alert))
        for alert in alerts[n_file:]:
            indexed.append((None, alert))
    else:
        if close is not None and st.get("seeded"):
            if not st.get("price_seeded"):
                st["arms"] = seed_arms(cfg, close[1])
                st["price_seeded"] = True
                st["last_close"] = {"day": close[0], "close": close[1]}
            else:
                for alert in price_alerts(cfg, st, close[0], close[1], stamp):
                    indexed.append((None, alert))
    detail = "; ".join(x for x in (f"공시 {ferr}" if ferr else "", f"종가 {perr}" if perr else "") if x)
    for alert in ops_alert(st, today, detail, stamp):
        indexed.append((None, alert))

    if seed and not ferr:
        print(f"시드만, 발송 없음. seen {len(st.get('seen') or [])}건. price_seeded={st.get('price_seeded')} arms={st.get('arms')}")
    for filing, (title, msg, pri) in indexed:
        dest = route_topic(topic, title)
        print(f"TITLE: {title}\nPRIORITY: {pri} TOPIC: {dest}\n{msg}\n")
        check_text(title, msg)
        if not dry:
            key = (filing or {}).get("rcp") or (filing or {}).get("id")
            publish(dest, title, msg, pri, ["chart_with_upwards_trend"], server, dedup_key=key or None)
    if not indexed and not (seed and not ferr):
        print("새 알림 없음")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    st["updated_at"] = now.isoformat()
    save_state(st)
    return 0


# ---------- selftest ----------
SAMPLE_ORDER = (
    "단일판매ㆍ공급계약체결 1. 판매ㆍ공급계약 내용 AI 가속기 설계 "
    "2. 계약내역 확정 계약금액 12,000,000,000 계약금액 총액(원) 12,000,000,000 "
    "최근 매출액(원) 68,522,000,000 매출액 대비(%) 17.5 "
    "3. 계약상대방 딥엑스 주식회사 - 최근 매출액(원) 1 "
    "5. 계약기간 시작일 2026-11-20 종료일 2028-12-31"
)
SAMPLE_CUT = (
    "정정 전 계약금액 12,000,000,000 정정 후 계약금액 7,000,000,000 "
    "단일판매ㆍ공급계약체결 계약금액 총액(원) 7,000,000,000 매출액 대비(%) 10.2"
)
SAMPLE_YEAR = (
    "사업보고서 제15기 2026.01.01 부터 2026.12.31 까지 (단위 : 원) "
    "매출액 120,000,000,000 매출원가 80,000,000,000 영업이익 8,000,000,000 당기순이익 3,000,000,000"
)
SAMPLE_YEAR_MISS = (
    "사업보고서 제15기 2026.01.01 부터 2026.12.31 까지 (단위 : 원) "
    "매출액 90,000,000,000 영업이익 (2,000,000,000)"
)


def selftest() -> int:
    cfg = load_cfg()
    fails: list[str] = []

    def check(name, got, want):
        ok = got == want
        print(("ok   " if ok else "FAIL ") + f"{name}: got={got!r} want={want!r}")
        if not ok:
            fails.append(name)

    check("supply", classify("단일판매ㆍ공급계약체결")["new_order"], True)
    check("supply mid-dot", classify("단일판매·공급계약 체결")["new_order"], True)
    check("cancel", classify("단일판매ㆍ공급계약해지")["cancel"], True)
    check("period", classify("분기보고서 (2026.09)")["period"], True)
    check("provisional", classify("연결재무제표기준영업(잠정)실적")["provisional"], True)
    check("profit", classify("매출액또는손익구조30%(대규모법인은15%)이상변동")["profit_struct"], True)
    check("keyword 2nm", "2nm" in classify("주요사항 2nm 일정 지연")["keywords"], True)
    check("keyword deepx", "DeepX" in classify("딥엑스 공급 논의")["keywords"], True)
    check("skip holder", classify("주식등의대량보유상황보고서")["relevant"], False)
    trust = classify("주요사항보고서(자기주식취득신탁계약해지결정)")
    check("trust haeji not supply cancel", trust["cancel"], False)
    check("trust haeji keyword", "해지" in trust["keywords"], True)

    p = parse_contract(SAMPLE_ORDER)
    check("order amount", p and p["amount"], 12000000000.0)
    check("order ratio", p and p["ratio"], 17.5)
    check("order party", p and p["party"], "딥엑스 주식회사")
    cut = parse_contract(SAMPLE_CUT)
    check("cut reduced", cut and cut["reduced"], True)
    check("cut after", cut and cut["after"], 7000000000.0)

    acct = pick_accounts([
        {"fs_div": "OFS", "account_nm": "매출액", "thstrm_amount": "1"},
        {"fs_div": "CFS", "account_nm": "매출액", "thstrm_amount": "100,000,000,000"},
        {"fs_div": "CFS", "account_nm": "영업이익(손실)", "thstrm_amount": "5000000000"},
        {"fs_div": "CFS", "account_nm": "매출원가", "thstrm_amount": "9"},
    ])
    check("acct cfs rev", acct["revenue"], 100000000000.0)
    check("acct cfs op", acct["op"], 5000000000.0)

    met = judge_annual(cfg, {"revenue": 120e9, "op": 1.0, "full_year": True})
    miss = judge_annual(cfg, {"revenue": 90e9, "op": -1.0, "full_year": True})
    hold = judge_annual(cfg, {"revenue": None, "op": 1.0, "full_year": True})
    qhold = judge_annual(cfg, {"revenue": 200e9, "op": 10e9, "full_year": False})
    check("annual met", met["verdict"], "충족")
    check("annual miss", miss["verdict"], "미충족")
    check("annual hold", hold["verdict"], "보류")
    check("quarter not annual", qhold["verdict"], "보류")
    parsed_year = parse_income_text(SAMPLE_YEAR)
    check("year text rev", parsed_year["revenue"], 120000000000.0)
    check("year text full", parsed_year["full_year"], True)
    check("year text judge", judge_annual(cfg, parsed_year)["verdict"], "충족")
    check("year miss judge", judge_annual(cfg, parse_income_text(SAMPLE_YEAR_MISS))["verdict"], "미충족")

    stamp = "2026-11-19 19:00 KST"
    order = {
        "id": "dart:20261120000001", "rcp": "20261120000001",
        "title": "단일판매ㆍ공급계약체결", "filed": "2026-11-20", "src": "DART",
        "url": DART_VIEW.format(rcp="20261120000001"),
        "kind": classify("단일판매ㆍ공급계약체결"),
    }
    st = default_state()
    a0 = evaluate(cfg, st, [order], ("2026-11-19", 36900.0), stamp, {order["id"]: SAMPLE_ORDER})
    check("seed silent", a0, [])
    check("seed seen", "dart:20261120000001" in st["seen"], True)
    check("seed price outside", st["arms"].get("low52") and st["arms"].get("watch"), True)
    a1 = evaluate(cfg, st, [order], ("2026-11-19", 36900.0), stamp, {order["id"]: SAMPLE_ORDER})
    check("dedup", a1, [])

    st2 = default_state()
    st2["seeded"] = True
    st2["price_seeded"] = True
    st2["arms"] = {"low52": True, "watch": True}
    a2 = evaluate(cfg, st2, [order], ("2026-11-19", 36900.0), stamp, {order["id"]: SAMPLE_ORDER})
    check("new order count", len(a2), 1)
    check("new order title", "399720" in a2[0][0] and "공급계약 체결" in a2[0][0] and "120.0억원" in a2[0][0], True)
    check("new order ratio", "17.5%" in a2[0][0], True)
    check("new order pri", a2[0][2], 4)
    check("new order link", "20261120000001" in a2[0][1], True)
    check("araea kept", "\u318d" in a2[0][1], True)
    check_text(a2[0][0], a2[0][1])
    a2b = evaluate(cfg, st2, [order], ("2026-11-19", 36900.0), stamp, {order["id"]: SAMPLE_ORDER})
    check("dedup second", a2b, [])

    cancel = {
        "id": "dart:20261121000002", "rcp": "20261121000002",
        "title": "단일판매ㆍ공급계약해지", "filed": "2026-11-21", "src": "DART",
        "url": DART_VIEW.format(rcp="20261121000002"),
        "kind": classify("단일판매ㆍ공급계약해지"),
    }
    ac = evaluate(cfg, st2, [cancel], None, stamp, {cancel["id"]: "계약 해지"})
    check("cancel pri", ac[0][2], 5)
    check("cancel words", "해지" in ac[0][0] and "399720" in ac[0][0], True)
    check_text(ac[0][0], ac[0][1])

    corr = {
        "id": "dart:20261122000003", "rcp": "20261122000003",
        "title": "[정정]단일판매ㆍ공급계약체결", "filed": "2026-11-22", "src": "DART",
        "url": DART_VIEW.format(rcp="20261122000003"),
        "kind": classify("[정정]단일판매ㆍ공급계약체결"),
    }
    ar = evaluate(cfg, st2, [corr], None, stamp, {corr["id"]: SAMPLE_CUT})
    check("reduction pri", ar[0][2], 5)
    check("reduction title", "금액 축소" in ar[0][0] and "399720" in ar[0][0], True)
    check_text(ar[0][0], ar[0][1])

    fy = {
        "id": "dart:20270319000009", "rcp": "20270319000009",
        "title": "사업보고서", "filed": "2027-03-19", "src": "DART",
        "url": DART_VIEW.format(rcp="20270319000009"),
        "kind": classify("사업보고서"),
    }
    am = evaluate(cfg, st2, [fy], None, stamp, {fy["id"]: SAMPLE_YEAR})
    check("fy met", "조건 a 충족" in am[0][0] and "399720" in am[0][0], True)
    check_text(am[0][0], am[0][1])
    fy2 = {**fy, "id": "dart:20270320000010", "rcp": "20270320000010"}
    an = evaluate(cfg, st2, [fy2], None, stamp, {fy2["id"]: SAMPLE_YEAR_MISS})
    check("fy miss", "조건 a 미충족" in an[0][0], True)
    blank = {**fy, "id": "dart:20270321000011", "rcp": "20270321000011"}
    ah = evaluate(cfg, st2, [blank], None, stamp, {blank["id"]: "사업보고서 본문 없음"})
    check("fy hold", "판단 보류" in ah[0][0], True)
    check("fy hold not met label", "조건 a 충족" not in ah[0][0], True)

    # price enter / stay / re-arm
    st3 = default_state()
    st3["seeded"] = True
    st3["price_seeded"] = True
    st3["arms"] = {"low52": True, "watch": True}
    z = evaluate(cfg, st3, [], ("2026-11-18", 36900.0), stamp, {})
    check("mid silent", z, [])
    low = evaluate(cfg, st3, [], ("2026-11-20", 25750.0), stamp, {})
    check("low enter", len(low) == 1 and "52주 저점" in low[0][0] and low[0][2] == 4, True)
    check("low disarmed", st3["arms"]["low52"], False)
    stay = evaluate(cfg, st3, [], ("2026-11-21", 25000.0), stamp, {})
    check("low stay", stay, [])
    evaluate(cfg, st3, [], ("2026-11-24", 25751.0), stamp, {})
    check("low rearm", st3["arms"]["low52"], True)
    again = evaluate(cfg, st3, [], ("2026-11-25", 25750.0), stamp, {})
    check("low again", len(again) == 1, True)
    check_text(again[0][0], again[0][1])

    hi = evaluate(cfg, st3, [], ("2026-12-01", 50000.0), stamp, {})
    check("watch enter", len(hi) == 1 and "관찰 구간" in hi[0][0] and "399720" in hi[0][0], True)
    check("watch disarmed", st3["arms"]["watch"], False)
    evaluate(cfg, st3, [], ("2026-12-02", 49999.0), stamp, {})
    check("watch rearm", st3["arms"]["watch"], True)
    check_text(hi[0][0], hi[0][1])

    # already inside on first seed: no send
    st4 = default_state()
    seeded = evaluate(cfg, st4, [], ("2026-11-19", 24000.0), stamp, {})
    check("inside seed silent", seeded, [])
    check("inside seed disarmed", st4["arms"]["low52"], False)
    still = evaluate(cfg, st4, [], ("2026-11-20", 24000.0), stamp, {})
    check("inside stays silent", still, [])

    st5 = default_state()
    st5["seeded"] = True
    op = ops_alert(st5, "2026-11-19", "DART down", stamp)
    check("ops title", "399720" in op[0][0] and "조회 실패" in op[0][0], True)
    check("ops topic", route_topic("cwally-port", op[0][0]), "cwally-ops")
    op2 = ops_alert(st5, "2026-11-19", "DART down", stamp)
    check("ops once", op2, [])
    check_text(op[0][0], op[0][1])

    dirty = make_title("테스트 \u00b7 항목 \u2014 끝")
    check("banned stripped", ("\u00b7" not in dirty and "\u2014" not in dirty), True)
    check("ticker literal", "399720" in dirty, True)

    q3 = {
        "id": "dart:20261116000452", "title": "분기보고서", "filed": "2026-11-16",
        "kind": classify("분기보고서"),
    }
    check("q3 not fy yet", is_fy_candidate(cfg, q3, SAMPLE_YEAR), False)
    future = {"title": "영업(잠정)실적", "filed": "2027-02-10", "kind": classify("영업(잠정)실적")}
    check("prov window", is_fy_candidate(cfg, future, ""), True)
    late_q = {"title": "영업(잠정)실적(1분기)", "filed": "2027-05-15", "kind": classify("영업(잠정)실적(1분기)")}
    check("q1 prov skipped", is_fy_candidate(cfg, late_q, ""), False)

    past = datetime(2027, 11, 20, 0, 0, tzinfo=KST)
    check("ended", ended(cfg, past), True)
    check("not ended", ended(cfg, datetime(2026, 11, 19, 19, 0, tzinfo=KST)), False)
    check("prefix", cfg["title_prefix"], "[K399720]")
    check("revenue floor", cfg["annual"]["revenue_min_won"], 100000000000)
    check("watch level", cfg["lines"][1]["level"], 50000)
    check("verified rcept", cfg["verified"]["quarterly_report"]["rcept_no"], "20261116000452")
    check("unverified net cash", cfg["unverified"]["net_cash_won"], None)
    check("no emdash in quote", "\u2014" not in cfg["verified"]["two_nano"]["quote"], True)

    if fails:
        print("selftest FAIL: " + ", ".join(fails))
        return 1
    print("selftest ok")
    return 0


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    mode = (args[0] if args else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry"}:
        print(f"usage: {NAME}_monitor.py <auto|dry|selftest>")
        return 2
    cfg = load_cfg()
    now = datetime.now(KST)
    try:
        return run_alerts(mode, now, cfg)
    except Exception:  # noqa: BLE001
        print(traceback.format_exc(), file=sys.stderr)
        if mode != "auto":
            return 1
        try:
            stamp = now.strftime("%Y-%m-%d %H:%M KST")
            st = load_state()
            alerts = ops_alert(st, now.strftime("%Y-%m-%d"), "스크립트 오류", stamp)
            if alerts:
                title, msg, pri = alerts[0]
                publish(route_topic(resolve_topic("port"), title), title, msg, pri,
                        ["warning"], resolve_server(cfg.get("ntfy_server")))
                st["updated_at"] = now.isoformat()
                save_state(st)
        except Exception:  # noqa: BLE001
            print(traceback.format_exc(), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
