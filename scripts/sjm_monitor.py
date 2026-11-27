#!/usr/bin/env python3
"""[123700] 에스제이엠 배당 가치주 관찰 알림 (2026-11-27 t25u).

KRX 거래일 19:00 k203780 슬롯. 새 반복 cron 없음. 매매 지시 아님.
  1) 현금ㆍ현물배당결정 공시: 주당배당금, 그날 종가 기준 배당수익률, 205원 유지(>=205),
     6% 안팎(183~204), 6% 미만 감액(151~182), 보수(<=150)로 분류. p4.
  2) 주식소각, 자기주식 취득과 신탁 체결 결정 p4, 자기주식 처분 결정 p3.
  3) 매출액 또는 손익구조 30% 변경 공시(연간 잠정 포함)와 분기, 반기, 사업보고서:
     영업이익, 영업이익률(OPM), 전년 대비, 영업현금흐름 빼기 설비투자(FCF)와 배당 필요액 32억원 대비. p3.
  4) 기업가치 제고 계획, 유상증자, 타법인 취득, 합병과 분할, 유형자산 취득 같은 현금 사용처 공시. p3 또는 p4.
  5) 정규장 종가 <=2,800원(1차 검토선, 재무장 >2,950), <=2,560원(2차 검토선, 재무장 >2,700).
     같은 날 두 선이면 가장 깊은 선 한 통.
첫 실행 공시는 시드만(state 에 미리 시드해 둠). 조회 실패는 cwally-ops 하루 1번.
modes: auto | dry | selftest
"""
from __future__ import annotations

import json
import re
import sys
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import secdiv_monitor as sd  # noqa: E402
from kr_calendar import KST, is_trading_day, prev_trading_day, session_close_ready, session_day  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

NAME = "sjm"
CODE = "123700"
CORP = "에스제이엠"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
DOT = r"[\u318d\u00b7\u2027\u30fb]"
DART_VIEW = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcp}"
DART_SEARCH = "https://dart.fss.or.kr/dsab007/detailSearch.ax"
DART_VIEWER = "https://dart.fss.or.kr/report/viewer.do"
NAVER_NOTICE = "https://finance.naver.com/item/news_notice.naver?code={code}"
CLOSE_HM = (15, 30)
KIND_TAGS = ["moneybag"]

WHAT = (
    "에스제이엠(123700)의 배당결정, 자사주, 손익구조 변경과 정기보고서, 현금 사용처 공시, "
    "종가 2,800/2,560원 검토선을 알리는 배당 가치주 관찰. 매매 지시 아님."
)


# ---------- state ----------
def default_state() -> dict:
    return {"updated_at": None, "seeded": False, "seen": [], "price": {}, "fail": {}, "ops_day": None}


def load_state() -> dict:
    st = default_state()
    if STATE_PATH.exists():
        try:
            st.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    st["seen"] = list(st.get("seen") or [])
    return st


def save_state(st: dict) -> None:
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------- text helpers ----------
def show_title(t: str) -> str:
    return re.sub(r"\s*" + DOT + r"\s*", ", ", t or "").strip()


def norm_title(t: str) -> str:
    s = re.sub(DOT, "", t or "")
    s = re.sub(r"주요사항보고서|\(|\)|\[|\]", "", s)
    return re.sub(r"\s+", "", s)


def clean(s: str) -> str:
    return re.sub(r"\s*" + DOT + r"\s*", ", ", s or "").replace("\u2014", ", ").replace("\u2013", ", ")


def check_text(*parts: str) -> None:
    s = "".join(parts)
    assert not re.search("[\u00b7\u318d\u2014\u2013]", s), s


def plain(text: str) -> str:
    s = text or ""
    if "<" in s:
        s = sd.text_of(s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", s).strip()


def ended(cfg: dict, now: datetime) -> bool:
    try:
        end = datetime.strptime(cfg.get("ends_kst") or "", "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    except ValueError:
        return False
    return now > end


def body(stamp: str, hdr: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([
        f"{stamp} | {CODE} | {CORP} {hdr}",
        f"이게 뭐 알림: {WHAT}",
        f"지금 결과/상태: {status}",
        f"지금 할 일: {action}",
        f"나중에: {later}",
        f"익절/줄일 때: {trim}",
        f"하지 말 것: {avoid}",
    ])


def eok(v: float | None) -> str:
    if v is None:
        return "미기재"
    return f"{v / 1e8:,.1f}억원"


def won(v: float | None) -> str:
    return f"{v:,.0f}원" if v is not None else "?"


def pct(v: float | None, plus: bool = True) -> str:
    if v is None:
        return "?"
    return f"{v:+.1f}%" if plus else f"{v:.1f}%"


def _int(tok: str | None) -> int | None:
    if not tok:
        return None
    s = re.sub(r"[^\d]", "", tok)
    return int(s) if s else None


def _date(tok: str | None) -> str | None:
    m = re.search(r"(\d{4})\s*[-.]\s*(\d{1,2})\s*[-.]\s*(\d{1,2})", tok or "")
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일", tok or "")
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


# ---------- 공시 목록 ----------
def parse_kind_html(page: str) -> list[dict]:
    out, rows = [], 0
    for row in re.findall(r"(?s)<tr[^>]*>(.*?)</tr>", page):
        acpt = re.search(r"openDisclsViewer\('(\d{14})'", row)
        if not acpt:
            continue
        rows += 1
        t = sd.text_of(row)
        dm = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})", t)
        title = t[dm.end():].strip() if dm else t
        title = re.sub(r"\s*(공시차트|주가차트).*$", "", title).strip()
        title = re.sub(r"^\(주\)" + CORP + r"\s+", "", title)
        title = re.sub(r"^주식회사\s*" + CORP + r"\s+", "", title)
        title = re.sub(r"^" + CORP + r"\s+", "", title)
        title = re.sub(r"\s+(" + CORP + r"|유가증권시장본부)$", "", title).strip()
        out.append({
            "id": f"kind:{CODE}:{acpt.group(1)}",
            "acptno": acpt.group(1),
            "title": title,
            "filed": f"{dm.group(1)} {dm.group(2)}" if dm else "",
            "src": "KIND",
            "url": sd.KIND_VIEW.format(acpt=acpt.group(1)),
        })
    if rows == 0 and "조회된 결과값이 없습니다" not in page and "<table" not in page:
        raise RuntimeError("KIND 응답 형식 이상")
    return sorted(out, key=lambda r: r["id"])


def kind_list(since: str, until: str) -> list[dict]:
    form = {
        "method": "searchDetailsSub", "currentPageSize": "100", "pageIndex": "1",
        "orderMode": "1", "orderStat": "D", "forward": "details_sub",
        "searchCodeType": "", "repIsuSrtCd": "A" + CODE, "allRepIsuSrtCd": "",
        "oldSearchCorpName": "", "disclosureType": "", "disTypevalue": "",
        "reportNm": "", "reportCd": "", "searchCorpName": CODE, "business": "",
        "marketType": "", "settlementMonth": "", "securities": "",
        "submitOblgNm": "", "enterprise": "", "fromDate": since, "toDate": until,
        "reportNmTemp": "", "bfrDsclsType": "on",
    }
    page = sd.http(
        sd.KIND + "/disclosure/details.do", data=form,
        headers={"X-Requested-With": "XMLHttpRequest",
                 "Referer": sd.KIND + "/disclosure/details.do?method=searchDetailsMain"},
    )
    return parse_kind_html(page)


def parse_naver_rows(raw, since: str, until: str) -> list[dict]:
    out = []
    for r in raw if isinstance(raw, list) else []:
        title = str(r.get("title") or "")
        title = re.sub(r"^\(주\)" + CORP + r"\s+", "", title).strip()
        title = re.sub(r"^주식회사\s*" + CORP + r"\s+", "", title).strip()
        title = re.sub(r"^" + CORP + r"\s+", "", title).strip()
        day = str(r.get("datetime") or "")[:10]
        nid = str(r.get("no") or r.get("bizdateSeq") or "")
        if not nid or len(day) != 10 or not (since <= day <= until):
            continue
        out.append({
            "id": f"naver:{CODE}:{nid}", "acptno": None, "title": title, "filed": day,
            "src": "네이버 KRX 공시 피드", "url": NAVER_NOTICE.format(code=CODE),
            "body": sd.text_of(str(r.get("contents") or "")),
        })
    return sorted(out, key=lambda r: r["id"])


def naver_list(since: str, until: str) -> list[dict]:
    raw = json.loads(sd.http(
        f"https://stock.naver.com/api/domestic/detail/notice?itemCode={CODE}&page=1&pageSize=100"))
    return parse_naver_rows(raw, since, until)


def parse_dart_html(page: str) -> list[dict]:
    out, seen = [], set()
    for m in re.finditer(r'rcpNo=(\d{14})"[^>]*title="([^"]+)"', page or ""):
        rcp = m.group(1)
        if rcp in seen:
            continue
        seen.add(rcp)
        title = re.sub(r"\s*공시뷰어\s*새창\s*$", "", m.group(2)).strip()
        out.append({"rcept": rcp, "title": title, "filed": f"{rcp[:4]}-{rcp[4:6]}-{rcp[6:8]}",
                    "url": DART_VIEW.format(rcp=rcp)})
    return out


def dart_list(cik: str, since: str, until: str) -> list[dict]:
    q = urlencode({"textCrpCik": cik, "startDate": since.replace("-", ""),
                   "endDate": until.replace("-", ""), "maxResults": "100", "currentPage": "1"})
    page = sd.http(DART_SEARCH + "?" + q)
    rows = parse_dart_html(page)
    if not rows and CORP not in (page or "") and "rcpNo=" not in (page or ""):
        raise RuntimeError("DART 목록 형식 이상")
    return rows


def attach_dart(rows: list[dict], dart_rows: list[dict]) -> None:
    """DART 번호는 같은 날, 같은 제목에 하나만 맞을 때만 붙인다(정정 중복 공시가 서로 가려지지 않게)."""
    for f in rows:
        day = (f.get("filed") or "")[:10]
        nt = norm_title(f.get("title") or "")
        if not day or not nt:
            continue
        hits = [d for d in dart_rows if d["filed"] == day and d["title"] and
                (norm_title(d["title"]) in nt or nt in norm_title(d["title"]))]
        if len(hits) == 1:
            f["dart_rcept"] = hits[0]["rcept"]
            f["url"] = hits[0]["url"]


def ids_of(r: dict) -> list[str]:
    out = [r["id"]]
    if r.get("dart_rcept"):
        out.append(f"dart:{CODE}:{r['dart_rcept']}")
    if r.get("acptno") and not r["id"].startswith("kind:"):
        out.append(f"kind:{CODE}:{r['acptno']}")
    return out


def already_seen(st: dict, r: dict) -> bool:
    seen = set(st.get("seen") or [])
    return any(i in seen for i in ids_of(r))


def mark_seen(st: dict, r: dict) -> None:
    seen = st.setdefault("seen", [])
    for i in ids_of(r):
        if i not in seen:
            seen.append(i)


def fetch_filings(cfg: dict, since: str, until: str) -> tuple[list[dict], list[str], str | None]:
    notes: list[str] = []
    rows: list[dict] | None = None
    hard: str | None = None
    try:
        rows = kind_list(since, until)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"KIND 목록: {exc}")
    if rows is None:
        try:
            rows = naver_list(since, until)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"네이버 목록: {exc}")
            hard = f"KIND와 네이버 목록 모두 실패: {exc}"[:200]
            rows = []
    try:
        attach_dart(rows, dart_list(cfg.get("corp_cik") or "00815369", since, until))
    except Exception as exc:  # noqa: BLE001
        notes.append(f"DART 목록: {exc}")
    rows.sort(key=lambda r: (r.get("filed") or "", r.get("id") or ""))
    return rows, notes, hard


def ensure_body(r: dict, notes: list[str]) -> str:
    if r.get("body"):
        return r["body"]
    if r.get("acptno"):
        try:
            r["body"] = sd.kind_body(r["acptno"])
            return r["body"]
        except Exception as exc:  # noqa: BLE001
            notes.append(f"KIND 본문 {r['acptno']}: {exc}")
    r["body"] = ""
    return ""


# ---------- 분류 ----------
def classify(title: str) -> str | None:
    t = re.sub(DOT, "", title or "")
    t = re.sub(r"\s+", "", t)
    if "자회사" in t or "지배회사" in t:
        return None
    if re.search(r"현금현물배당결정|배당결정", t):
        return "dividend"
    if "주식소각결정" in t or re.search(r"자기주식소각", t):
        return "cancel"
    if re.search(r"자기주식취득(신탁계약체결)?결정|자기주식취득신탁계약체결", t):
        return "acquire"
    if "자기주식처분결정" in t:
        return "dispose"
    if re.search(r"매출액또는손익구조\d+%", t) or "손익구조" in t:
        return "earn30"
    if re.search(r"(?<!감사)(사업보고서|반기보고서|분기보고서)", t) and "감사보고서" not in t and "기업지배구조" not in t:
        return "periodic"
    if "기업가치제고계획" in t:
        return "valueup"
    if re.search(r"유상증자결정|타법인주식및출자증권취득결정|유형자산취득결정|영업양수결정|회사합병결정|회사분할결정|전환사채권발행결정|감자결정", t):
        return "cashuse"
    return None


# ---------- 배당 ----------
def parse_dividend(text: str) -> dict | None:
    t = plain(text)
    out: dict = {}
    m = re.search(r"배당구분\s*(\S+배당)", t)
    out["kind"] = m.group(1) if m else None
    m = re.search(r"1주당\s*배당금\s*\(\s*원\s*\)\s*보통주식\s*([\d,]+)", t)
    out["dps"] = _int(m.group(1)) if m else None
    if out["dps"] is None and re.search(r"배당금\s*(?:총액)?\s*없음|무배당", t):
        out["dps"] = 0
    m = re.search(r"시가배당율\s*\(\s*%\s*\)\s*보통주식\s*([\d.]+)", t)
    out["yield_filed"] = float(m.group(1)) if m else None
    m = re.search(r"배당금총액\s*\(\s*원\s*\)\s*([\d,]+)", t)
    out["total"] = _int(m.group(1)) if m else None
    m = re.search(r"배당기준일\s*(\d{4}-\d{2}-\d{2})", t)
    out["record"] = m.group(1) if m else None
    m = re.search(r"배당금지급\s*예정일자\s*(\d{4}-\d{2}-\d{2})", t)
    out["pay"] = m.group(1) if m else None
    m = re.search(r"주주총회\s*예정일자\s*(\d{4}-\d{2}-\d{2})", t)
    out["agm"] = m.group(1) if m else None
    m = re.search(r"이사회결의일\(결정일\)\s*(\d{4}-\d{2}-\d{2})", t)
    out["board"] = m.group(1) if m else None
    if out["dps"] is None and out["total"] is None:
        return None
    return out


def dps_tier(dps: int, r: dict) -> tuple[str, str]:
    if dps >= r["dps_keep"]:
        return "유지 이상", f"{r['dps_keep']}원 이상. 분석의 배당 유지 조건 충족"
    if dps >= r["dps_six"]:
        return "6% 안팎", f"{r['dps_six']}~{r['dps_keep'] - 1}원. 3,050원 기준 6% 안팎으로 소폭 감액"
    if dps > r["dps_cons"]:
        return "6% 미만 감액", f"{r['dps_cons'] + 1}~{r['dps_six'] - 1}원. 3,050원 기준 6% 미만. 매력 약화"
    return "보수 판단", f"{r['dps_cons']}원 이하. 분석의 보수 조건에 해당"


def dividend_message(r: dict, p: dict | None, stamp: str, cfg: dict, last: tuple[str, float] | None) -> tuple[str, str, int]:
    rules = cfg["rules"]
    link = r.get("url") or "확인 불가"
    head = f"{r.get('filed') or '?'} 접수 '{show_title(r['title'])}' ({r.get('src') or '?'}) 링크: {link}"
    if not p or p.get("dps") is None:
        title = f"[{CODE}] 에스제이엠 배당결정 공시 | 원문 확인"
        status = f"{head}. 본문에서 주당배당금을 자동으로 못 읽었습니다. 추정하지 않음."
        act = "원문에서 배당구분, 주당배당금, 배당기준일, 지급 예정일을 확인하세요. 205원 유지, 183원(6%), 150원 이하(보수) 중 어디인지 보세요."
        msg = body(stamp, "배당결정", status, act, "같은 공시 번호는 다시 안 보냅니다.", "해당 없음(미보유).", "숫자를 짐작해서 판단하기.")
        return clean(title), clean(msg), 4
    dps = p["dps"]
    label, why = dps_tier(dps, rules)
    kind = p.get("kind") or "배당"
    y_txt = ""
    if last and dps:
        y_txt = f"{last[0]} 종가 {last[1]:,.0f}원 기준 세전 {dps / last[1] * 100:.1f}%"
    title = f"[{CODE}] 에스제이엠 {kind} 주당 {dps:,}원 | {label}" + (f" | {y_txt.split('기준 ')[-1]}" if y_txt else "")
    prev = cfg["facts"]["dividend_history"]
    total = p.get("total")
    need = rules["div_total_need_won"]
    parts = [head,
             f"주당 {dps:,}원 ({why}). 직전 결산 {prev['2025']}원(2024년 {prev['2024']}원) 대비 {dps - prev['2025']:+,}원.",
             y_txt + "." if y_txt else "",
             f"배당금총액 {eok(total)} (직전 결산 32.0억원), 배당기준일 {p.get('record') or '?'}, 지급 예정 {p.get('pay') or '?'}, 주총 {p.get('agm') or '?'}.",
             "공시 시가배당률 " + (f"{p['yield_filed']}%" if p.get("yield_filed") else "미기재") + "는 기준일 직전 1주일 평균가 기준이라 위 수익률과 다릅니다."]
    status = " ".join(x for x in parts if x)
    if dps >= rules["dps_keep"]:
        act = ("배당 조건은 충족입니다. 남은 확인은 연간 영업이익률(전년 5.6%)과 영업현금흐름 빼기 설비투자(FCF)가 배당 필요액 32억원을 덮는지입니다. "
               "둘 다 맞으면 분할 매수 검토 가능. 호가가 얇아 소량씩 지정가로.")
        pri = 4
    elif dps >= rules["dps_six"]:
        act = "6%를 간신히 지키는 배당입니다. 가격이 3,050원보다 낮을 때만 의미가 있으니 2,800원 검토선과 같이 보세요."
        pri = 4
    elif dps > rules["dps_cons"]:
        act = "감액 배당입니다. 매수 판단을 보류하고 감액 사유와 사업보고서 FCF를 먼저 확인하세요."
        pri = 4
    else:
        act = "보수 시나리오입니다. 현금이 많다는 이유만으로 사지 말고, 배당이 150원 아래로 내려간 사유와 향후 정책 문구를 확인하세요."
        pri = 4
    later = "같은 공시 번호는 다시 안 보냅니다. 정정이나 중간배당이 나오면 1번씩 더 옵니다. 사업보고서(3월)에서 FCF를 다시 알립니다."
    msg = body(stamp, "배당결정", status, act, later, "미보유라 해당 없음. 보유가 생기면 배당락 전후 변동을 감안.", "배당금 공시만 보고 시장가로 사기, 현금이 많다는 이유만으로 사기.")
    return clean(title), clean(msg), pri


# ---------- 자사주 ----------
def parse_cancel(text: str) -> dict:
    t = plain(text)
    out: dict = {}
    m = re.search(r"소각할\s*주식의\s*종류와\s*수\s*보통주식(?:\s*\(\s*주\s*\))?\s*([0-9,]+)", t)
    out["shares"] = _int(m.group(1)) if m else None
    m = re.search(r"발행주식총수\s*보통주식(?:\s*\(\s*주\s*\))?\s*([0-9,]+)", t)
    out["outstanding"] = _int(m.group(1)) if m else None
    m = re.search(r"소각예정금액(?:\s*\(\s*원\s*\))?\s*([0-9,]+)", t)
    out["amount"] = _int(m.group(1)) if m else None
    m = re.search(r"소각\s*예정일\s*(\d{4}\s*[-.]\s*\d{1,2}\s*[-.]\s*\d{1,2})", t)
    out["date"] = _date(m.group(1)) if m else None
    return out


def parse_acquire(text: str) -> dict:
    t = plain(text)
    out: dict = {}
    m = re.search(r"취득예정주식\s*\(\s*주\s*\)\s*보통주식\s*([0-9,]+)", t)
    out["shares"] = _int(m.group(1)) if m else None
    m = re.search(r"취득예정금액\s*\(\s*원\s*\)\s*보통주식\s*([0-9,]+)", t) or re.search(r"계약금액\s*\(\s*원\s*\)\s*([0-9,]+)", t)
    out["amount"] = _int(m.group(1)) if m else None
    m = re.search(r"취득예상기간\s*시작일\s*(\d{4}-\d{2}-\d{2})\s*종료일\s*(\d{4}-\d{2}-\d{2})", t) or \
        re.search(r"계약기간\s*시작일\s*(\d{4}-\d{2}-\d{2})\s*종료일\s*(\d{4}-\d{2}-\d{2})", t)
    out["period"] = f"{m.group(1)} ~ {m.group(2)}" if m else None
    m = re.search(r"취득목적\s+(.{2,60}?)\s+(?:\d+\.|취득)", t)
    out["purpose"] = m.group(1).strip(" -") if m else None
    return out


def buyback_message(kind: str, r: dict, text: str, stamp: str, cfg: dict) -> tuple[str, str, int]:
    link = r.get("url") or "확인 불가"
    head = f"{r.get('filed') or '?'} 접수 '{show_title(r['title'])}' ({r.get('src') or '?'}) 링크: {link}"
    so = cfg["facts"]["shares_out"]
    if kind == "cancel":
        p = parse_cancel(text)
        sh = p.get("shares")
        ratio = f"{sh / so * 100:.2f}%" if sh else "미기재"
        title = f"[{CODE}] 에스제이엠 자사주 소각 결정" + (f" | {sh:,}주 ({ratio})" if sh else " | 원문 확인")
        status = (f"{head}. 소각 주식 {sh:,}주(발행주식 {so:,}주의 {ratio}), 소각 금액 {eok(p.get('amount'))}, 소각 예정일 {p.get('date') or '?'}."
                  if sh else f"{head}. 본문에서 수량을 자동으로 못 읽었습니다. 추정하지 않음.")
        act = "주주환원이 배당에서 소각으로 넓어졌는지 보세요. 3분기 말 자기주식은 0주였으니 먼저 취득 사실이 있었는지 취득 공시 이력도 확인."
        pri = 4
        hdr = "자사주 소각"
    elif kind == "acquire":
        p = parse_acquire(text)
        sh = p.get("shares")
        title = f"[{CODE}] 에스제이엠 자사주 취득 결정" + (f" | {sh:,}주 ({sh / so * 100:.2f}%)" if sh else " | 원문 확인")
        status = (f"{head}. 취득 예정 {sh:,}주(발행주식의 {sh / so * 100:.2f}%), 금액 {eok(p.get('amount'))}, 기간 {p.get('period') or '?'}, 목적 {p.get('purpose') or '?'}."
                  if sh else f"{head}. 취득 금액 {eok(p.get('amount'))}, 기간 {p.get('period') or '?'}. 수량은 원문에서 확인.")
        act = "소각 목적인지 보유 목적인지 확인하세요. 소각이면 배당과 별개의 주주환원이라 업그레이드 요인입니다. 직접 취득 규모를 일 거래량(약 1.6만주)과 비교."
        pri = 4
        hdr = "자사주 취득"
    else:
        title = f"[{CODE}] 에스제이엠 자기주식 처분 결정 | 원문 확인"
        status = f"{head}. 처분 목적과 수량, 상대방을 원문에서 확인하세요."
        act = "임직원 보상용 소규모인지, 대량 매각인지 구분하세요. 대량 처분이면 주주환원 논리와 반대입니다."
        pri = 3
        hdr = "자사주 처분"
    later = "같은 공시 번호는 다시 안 보냅니다. 취득 결과 보고와 소각 결정은 각각 1번씩 따로 옵니다."
    msg = body(stamp, hdr, status, act, later, "미보유라 해당 없음.", "자사주 공시만 보고 추격 매수하기.")
    return clean(title), clean(msg), pri


# ---------- 손익구조 30% 변경 ----------
def parse_earn30(text: str) -> dict | None:
    t = plain(text)
    i = t.find("매출액 또는 손익구조 변동내용")
    s = t[i:] if i >= 0 else t
    unit = 1000.0
    um = re.search(r"변동내용\s*\(\s*단위\s*:\s*(천원|원|백만원)\s*\)", s)
    if um:
        unit = {"원": 1.0, "천원": 1000.0, "백만원": 1e6}[um.group(1)]
    out: dict = {"unit": unit}
    tok = r"(-?[\d,]*\d)"
    for key, label in (("rev", "매출액"), ("op", "영업이익"), ("pretax", "법인세비용차감전계속사업이익"), ("net", "당기순이익")):
        m = re.search(label + r"\s+" + tok + r"\s+" + tok + r"\s+" + tok + r"\s+(-?[\d.,]+|-)", s)
        if m:
            out[key] = float(m.group(1).replace(",", "")) * unit
            out[key + "_prev"] = float(m.group(2).replace(",", "")) * unit
            try:
                out[key + "_pct"] = float(m.group(4).replace(",", ""))
            except ValueError:
                out[key + "_pct"] = None
    m = re.search(r"종료일\s*(\d{4}-\d{2}-\d{2})", t)
    out["end"] = m.group(1) if m else None
    m = re.search(r"재무제표의\s*종류\s*(\S+)", t)
    out["fs"] = m.group(1) if m else None
    m = re.search(r"주요원인\s*(.{5,300}?)\s*6\.\s*이사회", t)
    out["cause"] = m.group(1).strip() if m else None
    if "op" not in out and "net" not in out:
        return None
    return out


def period_label(end: str | None) -> str:
    if not end:
        return "기간 미확인"
    y, mth = int(end[:4]), int(end[5:7])
    return {3: f"{y} 1분기", 6: f"{y} 상반기", 9: f"{y} 3분기 누적", 12: f"{y} 연간"}.get(mth, f"{y}-{mth:02d} 결산")


def earn30_message(r: dict, p: dict | None, stamp: str, cfg: dict) -> tuple[str, str, int]:
    link = r.get("url") or "확인 불가"
    corr = "정정" in (r.get("title") or "")
    head = f"{r.get('filed') or '?'} 접수 '{show_title(r['title'])}' ({r.get('src') or '?'}) 링크: {link}"
    kind = "손익구조 변경 정정" if corr else "손익구조 변경"
    if not p:
        title = f"[{CODE}] 에스제이엠 {kind} 공시 | 원문 확인"
        status = f"{head}. 본문 숫자를 자동으로 못 읽었습니다. 추정하지 않음."
        act = "영업이익과 당기순이익의 증감률, 영업이익률을 원문에서 확인하세요."
        msg = body(stamp, kind, status, act, "같은 공시 번호는 다시 안 보냅니다.", "해당 없음(미보유).", "숫자를 짐작해서 판단하기.")
        return clean(title), clean(msg), 3
    lab = period_label(p.get("end"))
    opm = p["op"] / p["rev"] * 100 if p.get("op") is not None and p.get("rev") else None
    opm_prev = p["op_prev"] / p["rev_prev"] * 100 if p.get("op_prev") is not None and p.get("rev_prev") else None
    f25 = cfg["facts"]["fy2025"]
    need = cfg["rules"]["div_total_need_won"]
    net = p.get("net")
    cover = f"당기순이익은 배당 필요액 32.0억원의 {net / need:.1f}배" if net and net > 0 else "당기순이익이 적자라 배당 재원은 이익잉여금과 현금에서 나와야 함"
    verdict = "개선 신호" if (opm is not None and opm_prev is not None and opm >= opm_prev and (p.get("op_pct") or 0) >= 0) else \
              ("둔화 지속" if (opm is not None and opm_prev is not None and opm < opm_prev and (p.get("op_pct") or 0) < 0) else "혼조")
    title = (f"[{CODE}] 에스제이엠 {lab} 잠정 영업이익 {eok(p.get('op'))} ({pct(p.get('op_pct'))}) | "
             f"순이익 {eok(net)} ({pct(p.get('net_pct'))}) | {verdict}")
    status = (f"{head}. {p.get('fs') or '?'}기준 {lab} 잠정(감사 전). 매출 {eok(p.get('rev'))}, 영업이익 {eok(p.get('op'))} ({pct(p.get('op_pct'))}), "
              f"영업이익률 {pct(opm, False)} (전년 {pct(opm_prev, False)}), 세전이익 {eok(p.get('pretax'))} ({pct(p.get('pretax_pct'))}), "
              f"당기순이익 {eok(net)} ({pct(p.get('net_pct'))}). {cover}. "
              f"참고: 직전 연간 2025년 영업이익률 {f25['opm_pct']}%, 영업이익 {eok(f25['op_won'])}. "
              + (f"공시 사유: {p['cause'][:140]}." if p.get("cause") else ""))
    if verdict == "개선 신호":
        act = "영업이익률이 전년보다 좋아졌습니다. 분석의 업그레이드 조건 중 이익 항목 충족. 다음은 배당결정(2월 말)과 사업보고서의 FCF 확인."
    elif verdict == "둔화 지속":
        act = "이익 둔화가 이어졌습니다. 매수 판단은 관찰 유지. 배당결정에서 205원이 유지되는지가 다음 분기점."
    else:
        act = "영업이익과 영업이익률 방향이 엇갈립니다. 영업이익률 기준(전년 대비)과 순이익 변동 원인(환율, 손상)을 따로 보세요."
    later = "같은 공시 번호는 다시 안 보냅니다. 감사보고서(3월)와 사업보고서에서 확정 숫자와 FCF를 다시 알립니다. 배당결정은 보통 2월 말."
    msg = body(stamp, kind, status, act, later, "미보유라 해당 없음.", "잠정 숫자만으로 비중 늘리기, 환율과 손상 같은 일회성을 영업 실적으로 읽기.")
    return clean(title), clean(msg), 3


# ---------- 정기보고서 ----------
NUM = r"(\(?△?-?[\d,]*\d\)?)"


def _num(tok: str) -> float:
    neg = tok.startswith("(") or tok.startswith("△") or tok.startswith("-")
    v = float(re.sub(r"[^\d]", "", tok) or 0)
    return -v if neg else v


def _row(s: str, label: str, width: int = 4) -> list[float] | None:
    m = re.search(r"(?<![가-힣])" + label + r"\s+((?:" + NUM + r"\s+){2," + str(width) + r"})", s + " ")
    if not m:
        return None
    return [_num(x) for x in re.findall(NUM, m.group(1))]


def parse_periodic(fin_text: str) -> dict | None:
    t = re.sub(r"\s+", " ", fin_text or "")
    out: dict = {}
    am = re.search(r"연결\s*포괄손익계산서\s*제\s*\d+\s*기", t) or re.search(r"연결\s*손익계산서\s*제\s*\d+\s*기", t)
    if am:
        s = t[am.start():am.start() + 6000]
        rev, op = _row(s, "매출액"), _row(s, "영업이익(?:\\(손실\\))?")
        if rev and op and len(rev) == len(op) and len(op) >= 2:
            n = len(op)
            if n >= 4:
                out.update(q_rev=rev[0], q_op=op[0], q_rev_prev=rev[2], q_op_prev=op[2], rev=rev[1], op=op[1], rev_prev=rev[3], op_prev=op[3])
            else:
                out.update(rev=rev[0], op=op[0], rev_prev=rev[1], op_prev=op[1])
    cm = re.search(r"연결\s*현금흐름표\s*제\s*\d+\s*기", t)
    if cm:
        s = t[cm.start():cm.start() + 9000]
        ocf = _row(s, "영업활동현금흐름", 2)
        ppe = _row(s, "유형자산의\\s*취득", 2)
        intg = _row(s, "무형자산의\\s*취득", 2)
        if ocf:
            out["ocf"] = ocf[0]
            out["ocf_prev"] = ocf[1] if len(ocf) > 1 else None
        capex = 0.0
        found = False
        for rr in (ppe, intg):
            if rr:
                capex += abs(rr[0])
                found = True
        if found:
            out["capex"] = capex
    return out if "op" in out else None


def period_kind(title: str) -> tuple[str, str | None]:
    m = re.search(r"\((\d{4})\.(\d{2})\)", title or "")
    mm = m.group(2) if m else None
    yy = m.group(1) if m else None
    if "사업보고서" in (title or ""):
        return f"{yy or ''} 연간 사업보고서".strip(), "12"
    if "반기보고서" in (title or ""):
        return f"{yy or ''} 상반기".strip(), "06"
    if mm == "03":
        return f"{yy} 1분기", "03"
    if mm == "09":
        return f"{yy} 3분기 누적", "09"
    return f"{yy or ''} 분기".strip(), mm


def doc_fin_text(rcp: str) -> str:
    """DART 뷰어에서 'III. 재무에 관한 사항' 본문 텍스트."""
    page = sd.http(DART_VIEW.format(rcp=rcp))
    blocks = re.findall(
        r"node1\['text'\] = \"(.*?)\";\s*node1\['id'\] = \"\d+\";\s*node1\['rcpNo'\] = \"\d+\";\s*node1\['dcmNo'\] = \"(\d+)\";"
        r"\s*node1\['eleId'\] = \"(\d+)\";\s*node1\['offset'\] = \"(\d+)\";\s*node1\['length'\] = \"(\d+)\"", page, re.S)
    for text, dcm, ele, off, ln in blocks:
        if "재무에 관한 사항" in text:
            q = urlencode({"rcpNo": rcp, "dcmNo": dcm, "eleId": ele, "offset": off, "length": ln, "dtd": "dart4.xsd"})
            return re.sub(r"\s+", " ", sd.text_of(sd.http(DART_VIEWER + "?" + q)))
    raise RuntimeError("재무에 관한 사항 목차 없음")


def periodic_message(r: dict, p: dict | None, stamp: str, cfg: dict) -> tuple[str, str, int]:
    link = r.get("url") or "확인 불가"
    lab, mm = period_kind(r.get("title") or "")
    head = f"{r.get('filed') or '?'} 접수 '{show_title(r['title'])}' ({r.get('src') or '?'}) 링크: {link}"
    f25 = cfg["facts"]["fy2025"]
    need = cfg["rules"]["div_total_need_won"]
    if not p:
        title = f"[{CODE}] 에스제이엠 {lab} 보고서 제출 | 숫자는 원문에서"
        status = f"{head}. 본문 숫자를 자동으로 못 읽었습니다. 추정하지 않음."
        act = "연결 영업이익, 영업이익률, 영업활동현금흐름, 유형자산 취득을 원문에서 확인하세요. 기준: 전년 같은 기간 대비, 연간은 2025년 영업이익률 5.6%, FCF는 배당 필요액 32억원."
        msg = body(stamp, "정기보고서", status, act, "같은 공시 번호는 다시 안 보냅니다.", "해당 없음(미보유).", "숫자를 짐작해서 판단하기.")
        return clean(title), clean(msg), 3
    opm = p["op"] / p["rev"] * 100 if p.get("rev") else None
    opm_prev = p["op_prev"] / p["rev_prev"] * 100 if p.get("rev_prev") else None
    yoy = (p["op"] / p["op_prev"] - 1) * 100 if p.get("op_prev") else None
    q_txt = ""
    q_down = None
    if p.get("q_op") is not None and p.get("q_op_prev"):
        qy = (p["q_op"] / p["q_op_prev"] - 1) * 100
        q_down = qy < 0
        q_txt = f" 이번 분기 단독 영업이익 {eok(p['q_op'])} (전년 {eok(p['q_op_prev'])}, {pct(qy)})."
    if mm == "12":
        base_opm, base_note = f25["opm_pct"], "2025년 연간"
    else:
        base_opm, base_note = opm_prev, "전년 같은 기간"
    up = opm is not None and base_opm is not None and opm >= base_opm and (yoy or 0) >= 0
    down = opm is not None and base_opm is not None and opm < base_opm and (yoy or 0) < 0
    verdict = "개선 신호" if up else ("둔화 지속" if down else "혼조")
    fcf_txt = ""
    if p.get("ocf") is not None and p.get("capex") is not None:
        fcf = p["ocf"] - p["capex"]
        fcf_txt = (f" 영업활동현금흐름 {eok(p['ocf'])} 빼기 설비투자 {eok(p['capex'])} = FCF {eok(fcf)}, "
                   f"배당 필요액 32.0억원의 {fcf / need:.1f}배" + (" (누적 기준)" if mm != "12" else "") + ".")
    title = (f"[{CODE}] 에스제이엠 {lab} 영업이익 {eok(p['op'])} ({pct(yoy)}) | 영업이익률 {pct(opm, False)} | {verdict}")
    status = (f"{head}. 연결 {lab} 매출 {eok(p.get('rev'))}, 영업이익 {eok(p['op'])} ({pct(yoy)}, 전년 {eok(p.get('op_prev'))}), "
              f"영업이익률 {pct(opm, False)} ({base_note} {pct(base_opm, False)}).{q_txt}{fcf_txt}")
    if verdict == "개선 신호":
        act = "영업이익률이 비교 기준보다 좋습니다. 이익 조건 충족. 배당결정 205원 유지와 FCF가 함께 맞으면 분할 매수 검토 가능."
    elif verdict == "둔화 지속":
        act = "이익 둔화가 이어집니다. 관찰 유지. 배당결정에서 205원 유지 여부가 다음 분기점."
    else:
        act = "영업이익과 영업이익률이 엇갈립니다. 분기 단독 흐름과 환율, 손상 같은 일회성을 따로 보세요."
    if mm == "12":
        later = "연간이면 배당결정은 이미 2월 말에 나왔을 가능성이 큽니다. 1분기 보고서(5월)에서 둔화 여부를 다시 알립니다."
    else:
        later = "같은 공시 번호는 다시 안 보냅니다. 연간 잠정(2월)과 배당결정(2월 말)이 다음 분기점입니다."
    msg = body(stamp, "정기보고서", status, act, later, "미보유라 해당 없음.", "분기 숫자 하나로 비중 정하기, 환율과 손상 같은 일회성을 영업 실적으로 읽기.")
    return clean(title), clean(msg), 3


# ---------- 기타 ----------
def other_message(kind: str, r: dict, stamp: str) -> tuple[str, str, int]:
    link = r.get("url") or "확인 불가"
    head = f"{r.get('filed') or '?'} 접수 '{show_title(r['title'])}' ({r.get('src') or '?'}) 링크: {link}"
    if kind == "valueup":
        title = f"[{CODE}] 에스제이엠 기업가치 제고 계획 공시 | 주주환원 문구 확인"
        act = "배당 정책, 배당성향 목표, 자사주 소각 계획이 숫자로 적혔는지 확인하세요. 숫자가 있으면 업그레이드 요인."
        pri = 3
        hdr = "기업가치 제고"
    else:
        title = f"[{CODE}] 에스제이엠 현금 사용처 공시 | {show_title(r['title'])[:30]}"
        act = "금액이 시총 476억원 대비 큰지 보세요. 유상증자는 논리 훼손, 설비나 타법인 투자는 배당 재원(FCF) 영향을 확인."
        pri = 4
        hdr = "현금 사용처"
    msg = body(stamp, hdr, head + ". 본문 숫자는 자동으로 읽지 않습니다.", act, "같은 공시 번호는 다시 안 보냅니다.", "미보유라 해당 없음.", "제목만 보고 매수나 매도 결정하기.")
    return clean(title), clean(msg), pri


def filing_message(r: dict, kind: str, text: str, stamp: str, cfg: dict, last: tuple[str, float] | None,
                   fin_fn=None) -> tuple[str, str, int]:
    if kind == "dividend":
        return dividend_message(r, parse_dividend(text), stamp, cfg, last)
    if kind in {"cancel", "acquire", "dispose"}:
        return buyback_message(kind, r, text, stamp, cfg)
    if kind == "earn30":
        return earn30_message(r, parse_earn30(text), stamp, cfg)
    if kind == "periodic":
        p = None
        if r.get("dart_rcept"):
            try:
                p = parse_periodic((fin_fn or doc_fin_text)(r["dart_rcept"]))
            except Exception as exc:  # noqa: BLE001
                print(f"정기보고서 본문 읽기 실패 {r['dart_rcept']}: {exc}")
        return periodic_message(r, p, stamp, cfg)
    return other_message(kind, r, stamp)


def evaluate_filings(cfg: dict, st: dict, filings: list[dict], stamp: str, seed_only: bool, last: tuple[str, float] | None,
                     notes: list[str], fin_fn=None, body_fn=None) -> list[tuple[str, str, int]]:
    out: list[tuple[str, str, int]] = []
    for r in filings:
        if already_seen(st, r):
            mark_seen(st, r)
            continue
        mark_seen(st, r)
        if seed_only:
            continue
        kind = classify(r.get("title") or "")
        if not kind:
            continue
        text = (body_fn or ensure_body)(r, notes) if kind != "periodic" else ""
        out.append(filing_message(r, kind, text, stamp, cfg, last, fin_fn))
    return out


# ---------- 가격 ----------
OPS = {"le": lambda a, b: a <= b, "gt": lambda a, b: a > b}


def fetch_rows() -> list[dict]:
    import krx_close
    rows = [{"day": b.day, "close": float(b.close), "volume": b.volume} for b in krx_close.regular_bars(CODE, 45)]
    if not rows:
        raise RuntimeError("시세 0건")
    return rows


def expected_day(sday: date, now: datetime) -> str:
    late = (now.hour, now.minute) >= CLOSE_HM or now.date() > sday
    if is_trading_day(sday) and late:
        return sday.isoformat()
    return prev_trading_day(sday).isoformat()


def lag_days(last: str, expect: str) -> int:
    if last >= expect:
        return 0
    n, d, lo = 0, date.fromisoformat(expect), date.fromisoformat(last)
    while d > lo:
        if is_trading_day(d):
            n += 1
        d = date.fromordinal(d.toordinal() - 1)
    return n


def liquidity(rows: list[dict], upto: str) -> str:
    use = [r for r in rows if r["day"] <= upto and r.get("volume")][-20:]
    if len(use) < 5:
        return "거래대금 자료 부족"
    vol = sum(r["volume"] for r in use) / len(use)
    val = sum(r["volume"] * r["close"] for r in use) / len(use)
    return f"최근 {len(use)}거래일 평균 거래량 {vol:,.0f}주, 거래대금 약 {val / 1e8:.1f}억원"


def evaluate_price(cfg: dict, ps: dict, rows: list[dict], today: str) -> list[dict]:
    out = []
    armed = ps.setdefault("armed", {"buy1": True, "buy2": True})
    d0, d1 = cfg["daily_window"]["start"], cfg["daily_window"]["end"]
    for row in rows:
        d, c = row["day"], row["close"]
        if d <= ps["last_date"] or d < d0 or d > today or d > d1:
            continue
        hit = []
        for key in ("buy1", "buy2"):
            ln = cfg["rules"][key]
            if OPS["le"](c, ln["level"]) and armed.get(key, True):
                armed[key] = False
                hit.append(key)
            elif OPS["gt"](c, ln["rearm"]) and not armed.get(key, True):
                armed[key] = True
        if hit:
            key = "buy2" if "buy2" in hit else "buy1"  # 같은 날 두 선이면 가장 깊은 선 한 통
            out.append({"key": key, "day": d, "close": c, "volume": row.get("volume"), "both": len(hit) == 2})
        ps["last_date"], ps["last_close"], ps["last_volume"] = d, c, row.get("volume")
    return out


def price_messages(cfg: dict, ev: list[dict], rows: list[dict], stamp: str) -> list[tuple[str, str, int]]:
    out = []
    so = cfg["facts"]["shares_out"]
    cash = cfg["facts"]["cash_won_20260930"]
    for e in ev:
        key, c, day = e["key"], e["close"], e["day"]
        ln = cfg["rules"][key]
        mcap = so * c
        ys = {d: d_ / c * 100 for d, d_ in (("205", 205), ("183", 183), ("150", 150))}
        title = f"[{CODE}] 에스제이엠 종가 {c:,.0f}원 | {ln['level']:,}원 이하 " + ("1차 분할매수 검토선" if key == "buy1" else "2차 깊은 가치 검토선")
        status = (f"{day} 정규장 종가 {c:,.0f}원, 검토선 {ln['level']:,}원 이하. 52주 저점 2,935원(11/23) 대비 {(c / 2935 - 1) * 100:+.1f}%. "
                  f"시총 약 {mcap / 1e8:,.0f}억원, 9월 말 연결 현금성자산 {cash / 1e8:,.0f}억원의 {cash / mcap:.2f}배(해외법인 운영자금 포함). "
                  f"배당 환산 세전: 205원 {ys['205']:.1f}%, 183원 {ys['183']:.1f}%, 150원 {ys['150']:.1f}%. "
                  f"{liquidity(rows, day)}. 재무장선 {ln['rearm']:,}원 위로 올라가야 다시 무장."
                  + (" 같은 날 1차 선도 함께 터치." if e.get("both") else ""))
        if key == "buy1":
            act = ("새 1년 저점 구간입니다. 사기 전에 확인: (1) 3분기 이후 이익 추가 악화 공시가 없는지, (2) 연간 영업이익률이 전년 5.6%를 넘길 흐름인지, "
                   "(3) 결산배당이 183원 이상으로 유지될 근거. 호가가 얇아 일 거래대금의 10% 안쪽 소량 지정가로 나눠서.")
        else:
            act = ("깊은 가치 구간입니다. 150원 보수 배당이어도 6% 근처. 그래도 이익 훼손 공시가 있었는지 먼저 확인하고, "
                   "유동성이 얇아 한 번에 사지 말고 며칠에 나눠서.")
        later = "같은 선은 재무장선을 넘었다가 다시 와야 알림이 옵니다. 공시 알림(배당결정, 손익구조, 사업보고서)은 별도로 옵니다."
        msg = body(stamp, "종가 검토선", status, act, later, "미보유라 해당 없음. 보유가 생기면 배당락 직전 급등 때 일부 줄이는 것을 검토.",
                   "현금이 시총보다 많다는 이유만으로 사기, 시장가 주문, 호가 얇은 날 한꺼번에 사기.")
        out.append((clean(title), clean(msg), ln["priority"]))
    return out


def ops_msg(stamp: str, status: str) -> tuple[str, str]:
    title = f"[{CODE}] 에스제이엠 조회 실패 | 원문 확인 필요"
    msg = body(stamp, "조회 실패", f"{status} 해당 규칙은 건너뜁니다.",
               "KIND, DART, 네이버 증권에서 공시와 종가를 직접 확인하세요.",
               "다음 슬롯에서 다시 읽고, 못 본 공시는 그때 보냅니다.", "해당 없음.", "조회 실패를 매매 신호로 쓰거나 실패한 숫자를 추정하기.")
    return clean(title), clean(msg)


# ---------- 실행 ----------
def run(mode: str, now: datetime, cfg: dict, pub=publish, rows_fn=None, filings_fn=None, st: dict | None = None,
        save: bool = True, fin_fn=None, body_fn=None) -> int:
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    sday = session_day(now)
    today = sday.isoformat()
    topic, server = resolve_topic("port", cfg.get("ntfy_topic")), resolve_server(cfg.get("ntfy_server"))
    print(f"{stamp} {NAME} mode={mode} session={today}")
    if ended(cfg, now):
        print(f"ended after {cfg.get('ends_kst')}; silent")
        return 0
    d0, d1 = cfg["daily_window"]["start"], cfg["daily_window"]["end"]
    if not (d0 <= today <= d1):
        print(f"{today} 감시 기간 밖({d0} ~ {d1}): 조용히 종료")
        return 0
    if not is_trading_day(sday) and not dry:
        print(f"{today} KRX 휴장: 조용히 종료")
        return 0
    st = load_state() if st is None else st
    out: list[tuple[str, str, int]] = []
    ops: tuple[str, str] | None = None
    errs: list[str] = []
    rows: list[dict] = []

    # 가격
    try:
        allrows = (rows_fn or fetch_rows)()
        rows = [r for r in allrows if session_close_ready(r["day"], today, now, CLOSE_HM)]
        if not rows:
            raise RuntimeError("확정 종가 0건")
        ps = st.setdefault("price", {})
        if not ps.get("last_date"):
            r = rows[-1]
            ps.update({"last_date": r["day"], "last_close": r["close"], "last_volume": r.get("volume"),
                       "armed": {k: not OPS["le"](r["close"], cfg["rules"][k]["level"]) for k in ("buy1", "buy2")},
                       "seed_note": f"첫 실행 {stamp}: 마지막 확정 종가 {r['day']} {r['close']:,.0f}원"})
            print(f"price first run: state only, no alert: {ps}")
        else:
            before = ps["last_date"]
            ev = evaluate_price(cfg, ps, rows, today)
            out.extend(price_messages(cfg, ev, rows, stamp))
            expect = expected_day(sday, now)
            lag = lag_days(ps["last_date"], expect)
            print(f"price judged since {before}: last {ps['last_date']} expected {expect} lag {lag}")
            if lag >= cfg["rules"].get("stale_trading_days", 2):
                errs.append(f"정규장 시세가 {expect} 까지 와야 하는데 마지막 행이 {rows[-1]['day']} ({lag}거래일 지연)")
    except Exception as exc:  # noqa: BLE001
        errs.append(f"시세 읽기 실패: {type(exc).__name__}: {str(exc)[:140]}")

    # 공시
    notes: list[str] = []
    try:
        since = (now - timedelta(days=370)).strftime("%Y-%m-%d")
        if filings_fn:
            filings, fnotes, hard = filings_fn(), [], None
        else:
            filings, fnotes, hard = fetch_filings(cfg, since, today)
        notes.extend(fnotes)
        if hard:
            errs.append(hard)
        seed_only = not st.get("seeded")
        last = None
        if rows:
            last = (rows[-1]["day"], rows[-1]["close"])
        if hard and not filings:
            pass
        else:
            out.extend(evaluate_filings(cfg, st, filings, stamp, seed_only, last, notes, fin_fn, body_fn))
            if seed_only:
                st["seeded"] = True
                st["seed_note"] = f"{stamp} 공시 시드. seen {len(st.get('seen') or [])}건"
                print(st["seed_note"])
    except Exception as exc:  # noqa: BLE001
        errs.append(f"공시 처리 실패: {type(exc).__name__}: {str(exc)[:140]}")
    if notes:
        print("notes: " + " | ".join(notes[:8]))

    fails = st.setdefault("fail", {})
    if errs:
        f = fails.get("src") or {"days": 0, "last": None, "notified": False}
        if f["last"] != today:
            f["days"] += 1
            f["last"] = today
        if f["days"] >= cfg["rules"].get("fail_ops_after_days", 1) and not f["notified"]:
            f["notified"] = True
            ops = ops_msg(stamp, "; ".join(errs)[:400] + ".")
        fails["src"] = f
    else:
        fails["src"] = {"days": 0, "last": None, "notified": False}

    for title, msg, pri in out:
        check_text(title, msg)
        assert route_topic(topic, title) == topic, title
        print(f"TITLE: {title}\nPRIORITY: {pri} TOPIC: {route_topic(topic, title)}\n{msg}\n")
    if ops:
        check_text(*ops)
        print(f"OPS TITLE: {ops[0]} -> {route_topic(resolve_topic('ops'), ops[0])}\n{ops[1]}\n")
    if not out and not ops:
        print("보낼 것 없음")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    for title, msg, pri in out:
        pub(topic, title, msg, pri, KIND_TAGS, server, config_name=NAME)
    if ops:
        try:
            pub(resolve_topic("ops"), ops[0], ops[1], 3, ["warning"], server, config_name=NAME)
        except Exception as exc:  # noqa: BLE001
            print(f"ops notice failed: {exc}")
    st["updated_at"] = now.isoformat()
    if save:
        save_state(st)
    return 0


# ---------- selftest ----------
SAMPLE_DIV = (
    ":: 61500_현금ㆍ현물배당 결정 현금ㆍ현물배당 결정 1. 배당구분 결산배당 2. 배당종류 현금배당 - 현물자산의 상세내역 - "
    "3. 1주당 배당금(원) 보통주식 205 종류주식 - - 차등배당 여부 미해당 4. 시가배당율(%) 보통주식 5.2 종류주식 - "
    "5. 배당금총액(원) 3,199,004,090 6. 배당기준일 2025-12-31 7. 배당금지급 예정일자 2026-04-22 8. 주주총회 개최여부 개최 "
    "9. 주주총회 예정일자 2026-03-27 10. 이사회결의일(결정일) 2026-02-26 - 사외이사 참석여부 참석(명) 1 불참(명) -"
)
SAMPLE_E30 = (
    ":: 91443_매출액 또는 손익구조 30%(대규모법인 15%)이상 변경 1. 재무제표의 종류 연결 2. 결산기간 당해사업연도 직전사업연도 "
    "- 시작일 2025-01-01 2024-01-01 - 종료일 2025-12-31 2024-12-31 3. 매출액 또는 손익구조 변동내용(단위:천원) "
    "당해사업연도 직전사업연도 증감금액 증감비율(%) 흑자적자전환여부 - 매출액 201,415,058 201,912,430 -497,372 -0.2 - "
    "- 영업이익 11,315,146 14,237,607 -2,922,461 -20.5 - - 법인세비용차감전계속사업이익 14,429,254 26,568,520 -12,139,266 -45.7 - "
    "- 당기순이익 10,712,572 20,557,489 -9,844,917 -47.9 - - 대규모법인여부 미해당 4. 재무현황(단위:천원) 당해사업연도 직전사업연도 "
    "- 자산총계 285,846,523 285,110,877 5. 매출액 또는 손익구조 변동 주요원인 - 업황 및 사업 전반 수익성 악화에 따른 영업이익 감소 "
    "- 외화평가손익 악화 및 현금창출단위 손상에 의한 당기순이익 감소 6. 이사회결의일(결정일) 2026-02-05"
)
SAMPLE_Q3 = (
    "2-2. 연결 포괄손익계산서 연결 포괄손익계산서 제 17 기 3분기 2026.01.01 부터 2026.09.30 까지 제 16 기 3분기 2025.01.01 부터 2025.09.30 까지 "
    "(단위 : 원) 제 17 기 3분기 제 16 기 3분기 3개월 누적 3개월 누적 매출액 49,711,658,613 155,349,140,802 49,971,871,545 151,889,036,119 "
    "매출원가 38,395,779,193 117,383,237,775 38,057,283,296 113,753,367,890 매출총이익 11,315,879,420 37,965,903,027 11,914,588,249 38,135,668,229 "
    "판매비와관리비 8,955,519,345 27,771,864,898 8,819,598,583 26,656,208,562 영업이익 2,360,360,075 10,194,038,129 3,094,989,666 11,479,459,667 "
    "금융수익 527,680,733 2,465,676,111 300,613,947 2,101,795,628 "
    "2-4. 연결 현금흐름표 연결 현금흐름표 제 17 기 3분기 2026.01.01 부터 2026.09.30 까지 제 16 기 3분기 2025.01.01 부터 2025.09.30 까지 (단위 : 원) "
    "제 17 기 3분기 제 16 기 3분기 영업활동현금흐름 19,760,823,579 22,264,329,844 영업에서 창출된 현금흐름 20,876,848,695 26,317,115,355 "
    "투자활동현금흐름 (13,243,239,493) 461,498,769 유형자산의 처분 33,148,801 492,600,117 유형자산의 취득 (4,289,463,884) (5,044,438,522) "
    "무형자산의 취득 (60,000,000) 0 정부보조금의 수취 0 80,409,000"
)
SAMPLE_CANCEL = (
    "주식소각결정 1. 소각할 주식의 종류와 수 보통주식(주) 400,000 2. 발행주식총수 보통주식(주) 15,604,898 "
    "3. 소각예정금액(원) 1,200,000,000 4. 소각 예정일 2027-03-15 5. 이사회결의일 2027-03-02"
)
SAMPLE_ACQ = (
    "자기주식 취득 결정 1. 취득예정주식(주) 보통주식 300,000 2. 취득예정금액(원) 보통주식 900,000,000 "
    "3. 취득예상기간 시작일 2027-03-03 종료일 2027-06-02 4. 취득목적 주주가치 제고 및 주가안정 5. 취득방법"
)


def selftest() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    fails: list[str] = []

    def check(name, got, want):
        ok = got == want
        print(("ok   " if ok else "FAIL ") + f"{name}: got={got!r} want={want!r}")
        if not ok:
            fails.append(name)

    # classify
    for t, want in (
        ("현금ㆍ현물 배당 결정", "dividend"), ("현금ㆍ현물배당결정", "dividend"), ("현금·현물배당결정(자회사의 주요경영사항)", None),
        ("매출액또는손익구조30%(대규모법인은15%)이상변경", "earn30"), ("[정정] 매출액또는손익구조30%(대규모법인은15%)이상변경", "earn30"),
        ("[연결포함] 사업보고서(일반법인) (2025.12)", "periodic"), ("분기보고서(일반법인) (2026.09)", "periodic"),
        ("반기보고서(일반법인) (2026.06)", "periodic"), ("감사보고서 제출", None), ("기업지배구조 보고서 공시 (2025.12)", None),
        ("기업가치 제고 계획(자율공시)", "valueup"), ("주식소각결정", "cancel"), ("자기주식취득결정", "acquire"),
        ("자기주식취득 신탁계약 체결결정", "acquire"), ("자기주식처분결정", "dispose"), ("주주총회소집공고", None),
        ("주식등의대량보유상황보고서(일반) 김용호", None), ("유상증자결정", "cashuse"), ("공매도 과열종목 지정(공매도 거래 금지 적용)", None),
    ):
        check(f"classify {t[:24]}", classify(t), want)

    # dividend
    p = parse_dividend(SAMPLE_DIV)
    check("div dps", p and p["dps"], 205)
    check("div total", p and p["total"], 3199004090)
    check("div record", p and p["record"], "2025-12-31")
    check("div pay", p and p["pay"], "2026-04-22")
    check("div kind", p and p["kind"], "결산배당")
    check("div yield filed", p and p["yield_filed"], 5.2)
    check("tier 205", dps_tier(205, cfg["rules"])[0], "유지 이상")
    check("tier 190", dps_tier(190, cfg["rules"])[0], "6% 안팎")
    check("tier 183", dps_tier(183, cfg["rules"])[0], "6% 안팎")
    check("tier 160", dps_tier(160, cfg["rules"])[0], "6% 미만 감액")
    check("tier 151", dps_tier(151, cfg["rules"])[0], "6% 미만 감액")
    check("tier 150", dps_tier(150, cfg["rules"])[0], "보수 판단")
    check("tier 0", dps_tier(0, cfg["rules"])[0], "보수 판단")
    stamp = "2027-02-26 19:00 KST"
    rec = {"id": f"kind:{CODE}:20270226000001", "acptno": "20270226000001", "title": "현금ㆍ현물 배당 결정",
           "filed": "2027-02-26 14:10", "src": "KIND", "url": DART_VIEW.format(rcp="20270226800001"), "dart_rcept": "20270226800001"}
    t1, m1, p1 = dividend_message(rec, p, stamp, cfg, ("2027-02-26", 3050.0))
    check("div title", f"[{CODE}]" in t1 and "205원" in t1 and "유지 이상" in t1 and "6.7%" in t1, True)
    check("div pri", p1, 4)
    check("div body has link", "20270226800001" in m1, True)
    check_text(t1, m1)
    p150 = {**p, "dps": 150, "total": 2340734700}
    t2, m2, _ = dividend_message(rec, p150, stamp, cfg, ("2027-02-26", 3050.0))
    check("div 150 tier", "보수 판단" in t2 and "4.9%" in t2, True)
    check_text(t2, m2)
    t3, m3, _ = dividend_message(rec, None, stamp, cfg, None)
    check("div unreadable", "원문 확인" in t3, True)
    check_text(t3, m3)
    p183 = {**p, "dps": 183}
    t4, _, _ = dividend_message(rec, p183, stamp, cfg, ("2027-02-26", 3050.0))
    check("div 183 tier", "6% 안팎" in t4 and "6.0%" in t4, True)

    # earn30
    e = parse_earn30(SAMPLE_E30)
    check("e30 op", e and e["op"], 11315146000.0)
    check("e30 net pct", e and e["net_pct"], -47.9)
    check("e30 end", e and e["end"], "2025-12-31")
    check("e30 fs", e and e["fs"], "연결")
    check("e30 cause", bool(e and e["cause"] and "수익성 악화" in e["cause"]), True)
    rec_e = {"id": f"kind:{CODE}:20270205000001", "acptno": "20270205000001", "title": "매출액또는손익구조30%(대규모법인은15%)이상변경",
             "filed": "2027-02-05 14:51", "src": "KIND", "url": "https://kind.krx.co.kr/x"}
    te, me, pe = earn30_message(rec_e, e, stamp, cfg)
    check("e30 title", f"[{CODE}]" in te and "2025 연간" in te and "113.2억원" in te and "-47.9%" in te, True)
    check("e30 verdict 둔화", "둔화 지속" in te, True)
    check("e30 pri", pe, 3)
    check_text(te, me)
    e_up = {**e, "op": 13e9, "op_pct": 14.9, "op_prev": 11.315e9, "rev": 205e9, "rev_prev": 201.4e9}
    te2, me2, _ = earn30_message(rec_e, e_up, stamp, cfg)
    check("e30 verdict 개선", "개선 신호" in te2, True)
    check_text(te2, me2)

    # periodic
    pp = parse_periodic(SAMPLE_Q3)
    check("q3 op cum", pp and pp["op"], 10194038129.0)
    check("q3 op prev", pp and pp["op_prev"], 11479459667.0)
    check("q3 q op", pp and pp["q_op"], 2360360075.0)
    check("q3 rev", pp and pp["rev"], 155349140802.0)
    check("q3 ocf", pp and pp["ocf"], 19760823579.0)
    check("q3 capex", pp and round(pp["capex"]), 4349463884)
    rec_q = {"id": f"kind:{CODE}:20261116000737", "acptno": "20261116000737", "title": "분기보고서(일반법인) (2026.09)",
             "filed": "2026-11-16 14:37", "src": "KIND", "url": DART_VIEW.format(rcp="20261116000608"), "dart_rcept": "20261116000608"}
    tq, mq, pq = periodic_message(rec_q, pp, stamp, cfg)
    check("q3 title", f"[{CODE}]" in tq and "3분기 누적" in tq and "101.9억원" in tq and "-11.2%" in tq and "6.6%" in tq and "둔화 지속" in tq, True)
    check("q3 fcf", "FCF 154.1억원" in mq and "4.8배" in mq, True)
    check("q3 quarter", "23.6억원" in mq and "-23.7%" in mq, True)
    check("q3 pri", pq, 3)
    check_text(tq, mq)
    tq2, mq2, _ = periodic_message(rec_q, None, stamp, cfg)
    check("periodic unreadable", "원문에서" in tq2, True)
    check_text(tq2, mq2)
    check("period kind 12", period_kind("[연결포함] 사업보고서(일반법인) (2026.12)")[1], "12")
    check("period kind 03", period_kind("분기보고서(일반법인) (2027.03)")[0], "2027 1분기")
    # annual-report layout (2 columns) + FY2025 baseline
    ann = ("연결 포괄손익계산서 제 16 기 2025.01.01 부터 2025.12.31 까지 (단위 : 원) 제 16 기 제 15 기 매출액 201,415,058,405 201,912,429,709 "
           "영업이익 11,315,146,331 14,237,606,905 금융수익 1 2 연결 현금흐름표 제 16 기 (단위 : 원) 제 16 기 제 15 기 "
           "영업활동현금흐름 25,000,000,000 30,000,000,000 유형자산의 취득 (6,000,000,000) (7,000,000,000)")
    pa = parse_periodic(ann)
    check("annual parse", pa and (pa["op"], pa["op_prev"], round(pa["ocf"]), round(pa["capex"])), (11315146331.0, 14237606905.0, 25000000000, 6000000000))
    rec_a = {**rec_q, "id": f"kind:{CODE}:20270319000001", "title": "[연결포함] 사업보고서(일반법인) (2026.12)", "filed": "2027-03-19 17:01"}
    pa2 = {**pa, "op": 12.5e9, "rev": 205e9, "op_prev": 11.315146331e9, "rev_prev": 201.415058405e9}
    ta, ma, _ = periodic_message(rec_a, pa2, stamp, cfg)
    check("annual verdict vs FY2025", "개선 신호" in ta and "2025년 연간" in ma, True)
    check_text(ta, ma)

    # buyback
    c = parse_cancel(SAMPLE_CANCEL)
    check("cancel shares", c["shares"], 400000)
    check("cancel date", c["date"], "2027-03-15")
    tc, mc, pc = buyback_message("cancel", {**rec, "title": "주식소각결정"}, SAMPLE_CANCEL, stamp, cfg)
    check("cancel title", f"[{CODE}]" in tc and "400,000주" in tc and "2.56%" in tc, True)
    check("cancel pri", pc, 4)
    check_text(tc, mc)
    a = parse_acquire(SAMPLE_ACQ)
    check("acq shares", a["shares"], 300000)
    check("acq amount", a["amount"], 900000000)
    check("acq period", a["period"], "2027-03-03 ~ 2027-06-02")
    ta2, ma2, pa3 = buyback_message("acquire", {**rec, "title": "자기주식취득결정"}, SAMPLE_ACQ, stamp, cfg)
    check("acq title", "300,000주" in ta2 and pa3 == 4, True)
    check_text(ta2, ma2)
    td, md, pd = buyback_message("dispose", {**rec, "title": "자기주식처분결정"}, "", stamp, cfg)
    check("dispose pri", pd, 3)
    check_text(td, md)
    for k in ("valueup", "cashuse"):
        to, mo, po = other_message(k, {**rec, "title": "유상증자결정"}, stamp)
        check(f"other {k}", f"[{CODE}]" in to, True)
        check_text(to, mo)

    # seeding and dedupe
    st = default_state()
    flist = [rec, rec_e, rec_q]
    notes: list[str] = []
    a0 = evaluate_filings(cfg, st, flist, stamp, True, None, notes)
    check("seed silent", a0, [])
    check("seeded marked", all(already_seen(st, r) for r in flist), True)
    a1 = evaluate_filings(cfg, st, flist, stamp, False, None, notes)
    check("dedupe after seed", a1, [])
    st2 = default_state()
    a2 = evaluate_filings(cfg, st2, [rec, rec_e], stamp, False, ("2027-02-26", 3050.0), notes, body_fn=lambda r, n: SAMPLE_DIV if "배당" in r["title"] else SAMPLE_E30)
    check("new filings send", len(a2), 2)
    a3 = evaluate_filings(cfg, st2, [rec, rec_e], stamp, False, None, notes, body_fn=lambda r, n: "")
    check("second skip", a3, [])
    # 정정이 같은 날 원본과 겹쳐도 가려지지 않음(DART 번호 없음)
    r_orig = {**rec_e, "id": f"kind:{CODE}:20270205000010", "acptno": "20270205000010"}
    r_corr = {**rec_e, "id": f"kind:{CODE}:20270205000011", "acptno": "20270205000011", "title": "[정정] 매출액또는손익구조30%(대규모법인은15%)이상변경"}
    st5 = default_state()
    a5 = evaluate_filings(cfg, st5, [r_orig, r_corr], stamp, False, None, notes, body_fn=lambda r, n: SAMPLE_E30)
    check("orig + correction both send", len(a5), 2)
    # attach_dart only on unique hit
    rows_k = [{"id": "kind:123700:1", "title": "현금ㆍ현물 배당 결정", "filed": "2027-02-26 14:17", "acptno": "1"}]
    attach_dart(rows_k, [{"rcept": "20270226800493", "title": "현금ㆍ현물배당결정", "filed": "2027-02-26", "url": "u"}])
    check("dart attach unique", rows_k[0].get("dart_rcept"), "20270226800493")
    rows_k2 = [{"id": "kind:123700:2", "title": "현금ㆍ현물 배당 결정", "filed": "2027-02-26 14:17", "acptno": "2"}]
    attach_dart(rows_k2, [{"rcept": "20270226800493", "title": "현금ㆍ현물배당결정", "filed": "2027-02-26", "url": "u"},
                          {"rcept": "20270226800510", "title": "현금ㆍ현물배당결정", "filed": "2027-02-26", "url": "u"}])
    check("dart attach ambiguous skipped", rows_k2[0].get("dart_rcept"), None)

    # price lines
    def mk(days_closes):
        return [{"day": d, "close": float(c), "volume": 15000.0} for d, c in days_closes]

    def price_state(last_close=2990.0):
        return {"last_date": "2026-11-26", "last_close": last_close, "armed": {"buy1": True, "buy2": True}}

    ps = price_state()
    ev = evaluate_price(cfg, ps, mk([("2026-11-26", 2990), ("2026-11-27", 3050)]), "2026-11-27")
    check("current silent", ev, [])
    check("last_date advanced", ps["last_date"], "2026-11-27")
    ps = price_state()
    ev = evaluate_price(cfg, ps, mk([("2026-11-27", 2800)]), "2026-11-27")
    check("buy1 fires at 2800", [x["key"] for x in ev], ["buy1"])
    check("buy1 disarmed", ps["armed"], {"buy1": False, "buy2": True})
    msgs = price_messages(cfg, ev, mk([("2026-11-27", 2800)]), stamp)
    check("buy1 msg", f"[{CODE}]" in msgs[0][0] and "2,800원 이하" in msgs[0][0] and msgs[0][2] == 3, True)
    check("buy1 yield text", "205원 7.3%" in msgs[0][1] and "150원 5.4%" in msgs[0][1], True)
    check_text(msgs[0][0], msgs[0][1])
    ev = evaluate_price(cfg, ps, mk([("2026-11-30", 2790)]), "2026-11-30")
    check("buy1 stays quiet", ev, [])
    ev = evaluate_price(cfg, ps, mk([("2026-12-01", 2950)]), "2026-12-01")
    check("buy1 not rearmed at 2950", ps["armed"]["buy1"], False)
    ev = evaluate_price(cfg, ps, mk([("2026-12-02", 2951)]), "2026-12-02")
    check("buy1 rearmed above 2950", ps["armed"]["buy1"], True)
    ev = evaluate_price(cfg, ps, mk([("2026-12-03", 2799)]), "2026-12-03")
    check("buy1 fires again", [x["key"] for x in ev], ["buy1"])
    ps = price_state()
    ev = evaluate_price(cfg, ps, mk([("2026-11-27", 2560)]), "2026-11-27")
    check("both lines same day = one deepest", [(x["key"], x["both"]) for x in ev], [("buy2", True)])
    check("both disarmed", ps["armed"], {"buy1": False, "buy2": False})
    msgs = price_messages(cfg, ev, mk([("2026-11-27", 2560)]), stamp)
    check("buy2 msg", "2,560원 이하" in msgs[0][0] and msgs[0][2] == 4 and "1차 선도 함께" in msgs[0][1], True)
    check("buy2 yields", "205원 8.0%" in msgs[0][1] and "150원 5.9%" in msgs[0][1], True)
    check_text(msgs[0][0], msgs[0][1])
    ev = evaluate_price(cfg, ps, mk([("2026-11-30", 2701)]), "2026-11-30")
    check("buy2 rearm above 2700 only buy2", ps["armed"], {"buy1": False, "buy2": True})
    ps = price_state()
    ev = evaluate_price(cfg, ps, mk([("2026-11-27", 2500), ("2026-11-30", 2500)]), "2026-11-30")
    check("multi-day catch-up fires once", len(ev), 1)
    ps = price_state()
    ev = evaluate_price(cfg, ps, mk([("2026-11-25", 2000)]), "2026-11-27")
    check("older than last_date ignored", ev, [])
    ps = price_state()
    ev = evaluate_price(cfg, ps, mk([("2027-07-01", 2000)]), "2027-07-01")
    check("after window ignored", ev, [])

    # dry run end to end: seeded state, no publish
    sent: list = []
    st6 = default_state()
    st6["price"] = price_state()
    st6["seeded"] = True
    now = datetime(2026, 11, 27, 19, 0, tzinfo=KST)
    run("auto", now, cfg, pub=lambda *a, **k: sent.append((a, k)), rows_fn=lambda: mk([("2026-11-26", 2990), ("2026-11-27", 2790)]),
        filings_fn=lambda: [], st=st6, save=False)
    check("run sends buy1", [s[0][1].split(" | ")[1] for s in sent], ["2,800원 이하 1차 분할매수 검토선"])
    check("run topic port", sent[0][0][0], "cwally-port")
    check("run price state", st6["price"]["last_date"], "2026-11-27")
    # first run (no state) seeds silently
    sent.clear()
    st7 = default_state()
    run("auto", now, cfg, pub=lambda *a, **k: sent.append((a, k)), rows_fn=lambda: mk([("2026-11-26", 2990), ("2026-11-27", 3050)]),
        filings_fn=lambda: [rec, rec_e], st=st7, save=False)
    check("first run silent", sent, [])
    check("first run seeded", (st7["seeded"], len(st7["seen"]) >= 2, st7["price"]["last_date"]), (True, True, "2026-11-27"))
    # fetch failure -> ops once, never port
    sent.clear()
    st8 = default_state()
    st8["price"] = price_state()
    st8["seeded"] = True

    def boom():
        raise RuntimeError("daum down")
    run("auto", now, cfg, pub=lambda *a, **k: sent.append((a, k)), rows_fn=boom, filings_fn=lambda: [], st=st8, save=False)
    check("ops once to ops topic", [(s[0][0], "조회 실패" in s[0][1]) for s in sent], [("cwally-ops", True)])
    run("auto", now, cfg, pub=lambda *a, **k: sent.append((a, k)), rows_fn=boom, filings_fn=lambda: [], st=st8, save=False)
    check("ops not repeated same day", len(sent), 1)
    # holiday and window
    sent.clear()
    run("auto", datetime(2026, 12, 25, 19, 0, tzinfo=KST), cfg, pub=lambda *a, **k: sent.append(1), rows_fn=lambda: [], st=default_state(), save=False)
    check("holiday silent", sent, [])

    check("ended", ended(cfg, datetime(2027, 7, 1, 0, 0, tzinfo=KST)), True)
    check("not ended now", ended(cfg, datetime(2026, 11, 27, 19, 0, tzinfo=KST)), False)
    check("config title prefix", cfg["title_prefix"], "[123700]")
    check("config topic", cfg["ntfy_topic"], "cwally-port")

    if fails:
        print("selftest FAIL: " + ", ".join(fails))
        return 1
    print("selftest ok")
    return 0


def main() -> int:
    args = sys.argv[1:]
    mode = (args[0] if args and not args[0].startswith("--") else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry"}:
        print(f"usage: {NAME}_monitor.py <auto|dry|selftest>")
        return 2
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    try:
        return run(mode, now, cfg)
    except Exception as exc:  # noqa: BLE001
        print(traceback.format_exc(), file=sys.stderr)
        if mode != "auto":
            return 1
        try:
            stamp = now.strftime("%Y-%m-%d %H:%M KST")
            title, msg = ops_msg(stamp, f"스크립트 오류 {type(exc).__name__}: {str(exc)[:200]}. state 는 그대로.")
            publish(resolve_topic("ops"), title, msg, 3, ["warning"], resolve_server(cfg.get("ntfy_server")), config_name=NAME)
        except Exception:  # noqa: BLE001
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
