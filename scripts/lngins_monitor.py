#!/usr/bin/env python3
"""[033500]/[017960] 동성화인텍·한국카본 LNG 보냉재 페어 알림 (2026-11-16 t20u).

KRX 거래일 19:00 k203780 슬롯. 새 반복 cron 없음.
  1) 두 종목의 단일판매 공급계약 체결(자율공시, 정정 포함) 공시마다 1번.
     해지는 대상 아님. 중복은 rcept/KIND/네이버 id. 첫 실행은 시드만.
  2) 033500 종가 <=10,000 추가 매수 검토(재무장 >10,400),
     >=15,000 재평가 점검(재무장 <14,500).
  3) 017960 종가 <=12,000 페어 비중 재점검(재무장 >12,500).
  4) 033500/017960 가격 비율이 20거래일 대비 ±15%면 페어 재점검
     (재무장 ±8% 이내).
조회 실패는 cwally-ops 하루 1번. 매매 지시 아님.
modes: auto | dry | selftest
"""
from __future__ import annotations

import json
import re
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import secdiv_monitor as sd  # noqa: E402
from kr_calendar import KST, is_trading_day, session_day  # noqa: E402
from krx_close import session_closes  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

NAME = "lngins"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
DOT = r"[\u318d\u00b7\u2027\u30fb]"
TITLE_RE = re.compile(r"단일판매\s*" + DOT + r"?\s*공급계약\s*체결")
DART_VIEW = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcp}"
DART_SEARCH = "https://dart.fss.or.kr/dsab007/detailSearch.ax"
NAVER_NOTICE = "https://finance.naver.com/item/news_notice.naver?code={code}"
COMPANIES = {
    "033500": {"code": "033500", "name": "동성화인텍"},
    "017960": {"code": "017960", "name": "한국카본"},
}


def default_state() -> dict:
    return {
        "updated_at": None,
        "seeded": False,
        "seen": [],
        "ds_low_armed": True,
        "ds_high_armed": True,
        "kc_low_armed": True,
        "rel_up_armed": True,
        "rel_down_armed": True,
        "fail": {},
    }


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


def show_title(t: str) -> str:
    return re.sub(r"\s*" + DOT + r"\s*", ", ", t or "").strip()


def norm_title(t: str) -> str:
    s = re.sub(DOT, "", t or "")
    s = re.sub(r"주요사항보고서|\(|\)|\[|\]", "", s)
    s = re.sub(r"\s+", "", s)
    return s


def is_target(title: str) -> bool:
    t = title or ""
    return bool(TITLE_RE.search(t)) and "해지" not in t


def is_correction(title: str) -> bool:
    return "정정" in (title or "")


def clean(s: str) -> str:
    return re.sub(r"\s*" + DOT + r"\s*", ", ", s or "").replace("\u2014", ", ").replace("\u2013", ", ")


def ended(cfg: dict, now: datetime) -> bool:
    raw = cfg.get("ends_kst") or ""
    try:
        end = datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    except ValueError:
        return False
    return now > end


def body(stamp: str, codes: str, names: str, hdr: str, what: str, status: str,
         action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([
        f"{stamp} | {codes} | {names} {hdr}",
        f"이게 뭐 알림: {what}",
        f"지금 결과/상태: {status}",
        f"지금 할 일: {action}",
        f"나중에: {later}",
        f"익절/줄일 때: {trim}",
        f"하지 말 것: {avoid}",
    ])


WHAT = (
    "동성화인텍(033500)과 한국카본(017960)의 단일판매 공급계약 체결 공시, "
    "동성 10,000/15,000원 밴드, 한국카본 12,000원 밴드, "
    "20거래일 상대 수익률 ±15%를 알리는 페어 감시. 매매 지시 아님."
)


# ---------- 목록 ----------
def parse_kind_html(page: str, code: str, name: str) -> list[dict]:
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
        title = re.sub(r"^\(주\)" + name + r"\s+", "", title)
        title = re.sub(r"^주식회사\s*" + name + r"\s+", "", title)
        title = re.sub(r"^" + name + r"\s+", "", title)
        title = re.sub(r"\s+(" + name + r"|코스닥시장본부|유가증권시장본부)$", "", title).strip()
        out.append({
            "id": f"kind:{code}:{acpt.group(1)}",
            "code": code,
            "acptno": acpt.group(1),
            "title": title,
            "filed": f"{dm.group(1)} {dm.group(2)}" if dm else "",
            "src": "KIND",
            "url": sd.KIND_VIEW.format(acpt=acpt.group(1)),
        })
    if rows == 0 and "조회된 결과값이 없습니다" not in page and "<table" not in page:
        raise RuntimeError(f"KIND 응답 형식 이상 ({code})")
    return sorted(out, key=lambda r: r["id"])


def kind_list(code: str, name: str, since: str, until: str) -> list[dict]:
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
    page = sd.http(
        sd.KIND + "/disclosure/details.do",
        data=form,
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Referer": sd.KIND + "/disclosure/details.do?method=searchDetailsMain",
        },
    )
    return parse_kind_html(page, code, name)


def parse_naver_rows(raw, code: str, name: str, since: str, until: str) -> list[dict]:
    out = []
    for r in raw if isinstance(raw, list) else []:
        title = str(r.get("title") or "")
        title = re.sub(r"^\(주\)" + name + r"\s+", "", title).strip()
        title = re.sub(r"^주식회사\s*" + name + r"\s+", "", title).strip()
        title = re.sub(r"^" + name + r"\s+", "", title).strip()
        day = str(r.get("datetime") or "")[:10]
        nid = str(r.get("no") or r.get("bizdateSeq") or "")
        if not nid or len(day) != 10 or not (since <= day <= until):
            continue
        out.append({
            "id": f"naver:{code}:{nid}",
            "code": code,
            "acptno": None,
            "title": title,
            "filed": day,
            "src": "네이버 KRX 공시 피드",
            "url": NAVER_NOTICE.format(code=code),
            "body": sd.text_of(str(r.get("contents") or "")),
        })
    return sorted(out, key=lambda r: r["id"])


def naver_list(code: str, name: str, since: str, until: str) -> list[dict]:
    raw = json.loads(sd.http(
        f"https://stock.naver.com/api/domestic/detail/notice?itemCode={code}&page=1&pageSize=100"
    ))
    return parse_naver_rows(raw, code, name, since, until)


def parse_dart_html(page: str) -> list[dict]:
    out = []
    for m in re.finditer(r'rcpNo=(\d{14})"[^>]*title="([^"]+)"', page or ""):
        rcp, title = m.group(1), m.group(2)
        title = re.sub(r"\s*공시뷰어\s*새창\s*$", "", title).strip()
        out.append({
            "rcept": rcp,
            "title": title,
            "filed": f"{rcp[:4]}-{rcp[4:6]}-{rcp[6:8]}",
            "url": DART_VIEW.format(rcp=rcp),
        })
    # alternate pattern
    if not out:
        for m in re.finditer(r"rcpNo=(\d{14})", page or ""):
            rcp = m.group(1)
            out.append({
                "rcept": rcp,
                "title": "",
                "filed": f"{rcp[:4]}-{rcp[4:6]}-{rcp[6:8]}",
                "url": DART_VIEW.format(rcp=rcp),
            })
    # unique
    seen, uniq = set(), []
    for r in out:
        if r["rcept"] in seen:
            continue
        seen.add(r["rcept"])
        uniq.append(r)
    return uniq


def dart_list(name: str, since: str, until: str) -> list[dict]:
    q = urlencode({
        "textCrpNm": name,
        "startDate": since.replace("-", ""),
        "endDate": until.replace("-", ""),
        "maxResults": "100",
        "currentPage": "1",
    })
    page = sd.http(DART_SEARCH + "?" + q)
    rows = parse_dart_html(page)
    if not rows and name not in (page or "") and "rcpNo=" not in (page or ""):
        raise RuntimeError(f"DART 목록 형식 이상 ({name})")
    return rows


def attach_dart(rows: list[dict], dart_rows: list[dict]) -> None:
    for f in rows:
        day = (f.get("filed") or "")[:10]
        nt = norm_title(f.get("title") or "")
        if not day or not nt:
            continue
        hits = [
            d for d in dart_rows
            if d["filed"] == day and (
                not d["title"]
                or nt in norm_title(d["title"])
                or norm_title(d["title"]) in nt
            )
        ]
        # if untitled dart rows only, skip ambiguous attach
        titled = [d for d in hits if d["title"]]
        use = titled if titled else hits
        if len(use) == 1:
            f["dart_rcept"] = use[0]["rcept"]
            f["url"] = use[0]["url"]


def ids_of(r: dict) -> list[str]:
    out = [r["id"]]
    if r.get("dart_rcept"):
        out.append(f"dart:{r['code']}:{r['dart_rcept']}")
    if r.get("acptno") and not r["id"].startswith("kind:"):
        out.append(f"kind:{r['code']}:{r['acptno']}")
    return out


def already_seen(st: dict, r: dict) -> bool:
    seen = set(st.get("seen") or [])
    return any(i in seen for i in ids_of(r))


def mark_seen(st: dict, r: dict) -> None:
    seen = st.setdefault("seen", [])
    for i in ids_of(r):
        if i not in seen:
            seen.append(i)


def fetch_filings(since: str, until: str) -> tuple[list[dict], list[str], dict[str, str]]:
    notes: list[str] = []
    hard: dict[str, str] = {}
    all_rows: list[dict] = []
    for code, meta in COMPANIES.items():
        name = meta["name"]
        reps: list[dict] | None = None
        try:
            reps = [r for r in kind_list(code, name, since, until) if is_target(r["title"])]
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{code} KIND 목록: {exc}")
        if reps is None:
            try:
                reps = [r for r in naver_list(code, name, since, until) if is_target(r["title"])]
            except Exception as exc:  # noqa: BLE001
                notes.append(f"{code} 네이버 목록: {exc}")
                hard[code] = f"KIND와 네이버 목록 모두 실패: {exc}"[:200]
                reps = []
        nav = None
        for r in reps:
            if r.get("body"):
                continue
            if r.get("acptno"):
                try:
                    r["body"] = sd.kind_body(r["acptno"])
                    continue
                except Exception as exc:  # noqa: BLE001
                    notes.append(f"{code} KIND 본문 {r['acptno']}: {exc}")
            try:
                nav = nav if nav is not None else naver_list(code, name, since, until)
                m = [
                    n for n in nav
                    if n["filed"][:10] == (r.get("filed") or "")[:10]
                    and norm_title(n["title"]) == norm_title(r["title"])
                ]
                r["body"] = m[-1]["body"] if m else ""
            except Exception as exc:  # noqa: BLE001
                notes.append(f"{code} 네이버 본문: {exc}")
                r["body"] = r.get("body") or ""
        try:
            attach_dart(reps, dart_list(name, since, until))
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{code} DART 목록: {exc}")
        all_rows.extend(reps)
    all_rows.sort(key=lambda r: (r.get("filed") or "", r.get("id") or ""))
    return all_rows, notes, hard


# ---------- 본문 파싱 ----------
def _n(tok: str | None) -> float | None:
    if tok is None or tok.strip() in {"-", ""}:
        return None
    try:
        return float(tok.replace(",", ""))
    except ValueError:
        return None


def _g(pat: str, s: str) -> str | None:
    m = re.search(pat, s)
    return m.group(1).strip() if m else None


def parse(text: str) -> dict | None:
    t = re.sub(r"\s+", " ", text or "")
    anchors = [
        m.start()
        for m in re.finditer(r"단일판매\s*" + DOT + r"?\s*공급계약\s*체결\s*1\.\s*판매", t)
    ]
    s = t[anchors[-1]:] if anchors else t
    amt_raw = (
        _g(r"계약금액\s*총액\s*\(\s*원\s*\)\s*(-|[\d,]+)", s)
        or _g(r"확정\s*계약금액\s*(-|[\d,]+)", s)
        or _g(r"계약금액\s*\(\s*원\s*\)\s*(-|[\d,]+)", s)
    )
    party = (
        _g(r"3\.\s*계약상대(?:방)?\s+(.+?)\s+-\s*(?:최근\s*매출액|주요사업|회사와)", s)
        or _g(r"3\.\s*계약상대(?:방)?\s+(.+?)\s+4\.\s*판매", s)
    )
    sales_raw = (
        _g(r"최근\s*매출액\s*\(\s*원\s*\)\s*(-|[\d,]+)", s)
        or _g(r"최근매출액\s*\(\s*원\s*\)\s*(-|[\d,]+)", s)
    )
    ratio_raw = (
        _g(r"매출액\s*대비\s*\(\s*%\s*\)\s*(-|[\d.]+)", s)
        or _g(r"매출액대비\s*\(\s*%\s*\)\s*(-|[\d.]+)", s)
    )
    name = (
        _g(r"판매\s*" + DOT + r"?\s*공급계약\s*내용\s+(.+?)\s+2\.\s*계약내역", s)
        or _g(r"체결계약명\s+(.+?)\s+2\.\s*계약내역", s)
    )
    start = _g(r"시작일\s*(\d{4}-\d{2}-\d{2})", s)
    end = _g(r"종료일\s*(\d{4}-\d{2}-\d{2})", s)
    out = {
        "name": name,
        "amount": _n(amt_raw),
        "amount_raw": amt_raw,
        "sales": _n(sales_raw),
        "ratio": _n(ratio_raw),
        "party": party,
        "start": start,
        "end": end,
        "corr_reason": _g(r"정정사유\s+(.+?)\s+4\.\s*정정사항", t),
    }
    if out["amount"] is None and out["party"] is None and out["name"] is None:
        return None
    return out


def won(v: float | None) -> str:
    if v is None:
        return "금액 미확인"
    if v >= 1e12:
        return f"{v / 1e12:,.2f}조원"
    return f"{v / 1e8:,.0f}억원"


def detail(p: dict | None) -> str:
    if not p:
        return "본문 숫자를 자동으로 못 읽음. 링크에서 계약상대방, 금액, 기간을 직접 확인하세요. 추정하지 않음."
    parts = []
    if p.get("party"):
        parts.append(f"계약상대방: {p['party']}")
    else:
        parts.append("계약상대방: 유보 또는 미확인, 추정하지 않음")
    if p.get("amount") is not None:
        parts.append(f"계약금액: {won(p['amount'])}({p['amount']:,.0f}원)")
    else:
        parts.append("계약금액: 유보 또는 미확인, 추정하지 않음")
    if p.get("ratio") is not None:
        parts.append(f"최근 매출액 대비: {p['ratio']}%")
    else:
        parts.append("최근 매출액 대비: 유보 또는 미기재")
    if p.get("sales"):
        parts.append(f"최근 매출액 {won(p['sales'])}")
    parts.append(f"계약기간: {p.get('start') or '?'} ~ {p.get('end') or '?'}")
    if p.get("name"):
        parts.append(f"계약내용: {p['name'][:80]}")
    if p.get("corr_reason"):
        parts.append(f"정정사유: {p['corr_reason'][:80]}")
    return ". ".join(parts) + "."


def filing_message(r: dict, p: dict | None, stamp: str) -> tuple[str, str, int]:
    code = r["code"]
    name = COMPANIES[code]["name"]
    corr = is_correction(r["title"])
    amt = p.get("amount") if p else None
    ratio = p.get("ratio") if p else None
    bits = []
    if amt is not None:
        bits.append(won(amt))
    if ratio is not None:
        bits.append(f"매출 대비 {ratio}%")
    kind = "공급계약 정정" if corr else "공급계약 체결"
    extra = f" | {', '.join(bits)}" if bits else ""
    title = f"[{code}] {kind}{extra}"
    st = (
        f"{r.get('filed') or '?'} 접수 '{show_title(r['title'])}' ({r.get('src') or '?'}). "
        f"{detail(p)} 링크: {r.get('url') or '확인 불가'}"
    )
    if corr:
        act = "정정 사유와 바뀐 금액, 상대방, 기간을 원문에서 확인하세요. 잔고 추정을 바로 고치지 마세요."
    else:
        act = (
            "계약 규모와 기간, 상대 조선소를 원문에서 확인하세요. "
            "수주 공시만으로 비중을 늘리지 말고 기록한 뒤 밴드와 같이 보세요."
        )
    later = "같은 공시 번호는 다시 안 보냅니다. 정정이나 새 체결이 나오면 1번씩 더 옵니다."
    trim = "수주 공시만으로 추격 매수하지 마세요. 지정가로만."
    avoid = "유보된 금액을 짐작하기, 언론 추정치를 쓰기, 이 알림을 매수 지시로 읽기."
    msg = clean(body(stamp, code, name, kind, WHAT, st, act, later, trim, avoid))
    return title, msg, 3 if corr else 4


# ---------- 가격/상대 ----------
def gather_prices() -> tuple[dict[str, list[tuple[str, float]]], dict[str, str]]:
    bars: dict[str, list[tuple[str, float]]] = {}
    errs: dict[str, str] = {}
    for code in COMPANIES:
        try:
            rows = session_closes(code, n=40, compare=False)
            if not rows:
                raise RuntimeError("종가 없음")
            bars[code] = rows
        except Exception as exc:  # noqa: BLE001
            errs[code] = f"{type(exc).__name__}: {exc}"[:200]
    return bars, errs


def last_close(bars: list[tuple[str, float]] | None) -> tuple[str, float] | None:
    return bars[-1] if bars else None


def ratio_change(ds: list[tuple[str, float]], kc: list[tuple[str, float]], lookback: int):
    da = {d: c for d, c in ds}
    db = {d: c for d, c in kc}
    days = sorted(set(da) & set(db))
    if len(days) <= lookback:
        return None
    d0, d1 = days[-(lookback + 1)], days[-1]
    if db[d0] == 0 or da[d0] == 0:
        return None
    r0, r1 = da[d0] / db[d0], da[d1] / db[d1]
    chg = (r1 / r0 - 1.0) * 100.0
    r_ds = (da[d1] / da[d0] - 1.0) * 100.0
    r_kc = (db[d1] / db[d0] - 1.0) * 100.0
    return {
        "base_day": d0, "day": d1,
        "base_ratio": r0, "ratio": r1, "chg": chg,
        "ds0": da[d0], "ds1": da[d1], "ds_ret": r_ds,
        "kc0": db[d0], "kc1": db[d1], "kc_ret": r_kc,
    }


def price_alerts(cfg: dict, st: dict, bars: dict, today: str, stamp: str) -> list[tuple[str, str, int]]:
    out: list[tuple[str, str, int]] = []
    r = cfg["rules"]
    later = "같은 조건은 재무장 선을 넘었다가 다시 와야 알림이 옵니다."
    trim = "계획한 비중이 있으면 그 안에서만. 없으면 새로 비중을 정한 뒤."
    avoid = "조회 실패를 신호로 읽기, 밴드 터치만으로 전량 매수하거나 전량 매도하기."

    ds = last_close(bars.get("033500"))
    if ds:
        day, close = ds
        if close <= r["ds_low"] and st.get("ds_low_armed", True):
            st["ds_low_armed"] = False
            title = f"[033500] 종가 {close:,.0f}원 | {r['ds_low']:,}원 이하 추가 매수 검토선"
            status = (
                f"{day} 동성화인텍 종가 {close:,.0f}원, 추가 매수 검토선 {r['ds_low']:,}원 이하. "
                f"재무장선 {r['ds_low_rearm']:,}원 위로 올라가야 다시 무장. "
                f"11/16 종가 11,300원, 52주 저점 11,010원 대비 이탈 여부 확인."
            )
            act = (
                "수주나 실적 훼손이 없는지 확인한 뒤 분할 추가를 검토하세요. "
                "한국카본 12,000원 선과 페어 비중도 같이 보세요."
            )
            out.append((title, clean(body(stamp, "033500", "동성화인텍", "추가 매수 검토선", WHAT, status, act, later, trim, avoid)), 4))
        elif close > r["ds_low_rearm"]:
            st["ds_low_armed"] = True

        if close >= r["ds_high"] and st.get("ds_high_armed", True):
            st["ds_high_armed"] = False
            title = f"[033500] 종가 {close:,.0f}원 | {r['ds_high']:,}원 이상 재평가 점검선"
            status = (
                f"{day} 동성화인텍 종가 {close:,.0f}원, 재평가 점검선 {r['ds_high']:,}원 이상. "
                f"재무장선 {r['ds_high_rearm']:,}원 아래로 내려와야 다시 무장. "
                f"분석의 2026E 약 4배 기준으로는 약 5.3배 구간."
            )
            act = "저평가 전제가 줄어들었는지 보고, 익절이나 비중 조절을 검토하세요. 추격 매수는 하지 마세요."
            out.append((title, clean(body(stamp, "033500", "동성화인텍", "재평가 점검선", WHAT, status, act, later, trim, avoid)), 3))
        elif close < r["ds_high_rearm"]:
            st["ds_high_armed"] = True

    kc = last_close(bars.get("017960"))
    if kc:
        day, close = kc
        if close <= r["kc_low"] and st.get("kc_low_armed", True):
            st["kc_low_armed"] = False
            title = f"[017960] 종가 {close:,.0f}원 | {r['kc_low']:,}원 이하 페어 비중 재점검선"
            status = (
                f"{day} 한국카본 종가 {close:,.0f}원, 재점검선 {r['kc_low']:,}원 이하. "
                f"재무장선 {r['kc_low_rearm']:,}원 위로 올라가야 다시 무장. "
                f"11/16 종가 13,910원, 52주 저점 13,520원 대비 이탈 여부 확인."
            )
            act = (
                "중국 조선 점유 확대 같은 약세 시나리오가 더 반영됐는지 보고, "
                "동성 대비 한국카본 비중을 다시 정하세요."
            )
            out.append((title, clean(body(stamp, "017960", "한국카본", "페어 비중 재점검선", WHAT, status, act, later, trim, avoid)), 4))
        elif close > r["kc_low_rearm"]:
            st["kc_low_armed"] = True

    rel = None
    if bars.get("033500") and bars.get("017960"):
        rel = ratio_change(bars["033500"], bars["017960"], int(r["rel_lookback_bars"]))
    if rel:
        chg = rel["chg"]
        st["rel_last"] = {"date": rel["day"], "chg": chg, "ratio": rel["ratio"]}
        base = (
            f"{rel['day']} 비율 {rel['ratio']:.4f} (동성 {rel['ds1']:,.0f} / 카본 {rel['kc1']:,.0f}), "
            f"{r['rel_lookback_bars']}거래일 전 {rel['base_day']} 비율 {rel['base_ratio']:.4f} 대비 {chg:+.2f}%. "
            f"동성 {rel['ds_ret']:+.2f}%, 카본 {rel['kc_ret']:+.2f}%."
        )
        if chg >= r["rel_pct"] and st.get("rel_up_armed", True):
            st["rel_up_armed"] = False
            title = f"[033500/017960] 20거래일 상대 {chg:+.1f}% | 페어 재점검"
            status = (
                f"{base} 동성이 한국카본보다 20거래일 상대 +{r['rel_pct']:.0f}% 이상. "
                f"{r['rel_rearm_pct']:+.0f}% 안으로 돌아와야 다시 무장."
            )
            act = "동성 우위가 주가에 반영되기 시작했는지 보고, 추격 매수 여부와 익절 계획을 점검하세요."
            out.append((title, clean(body(stamp, "033500/017960", "동성화인텍/한국카본", "상대 강세 재점검", WHAT, status, act, later, trim, avoid)), 4))
        elif abs(chg) < r["rel_rearm_pct"]:
            st["rel_up_armed"] = True

        if chg <= -r["rel_pct"] and st.get("rel_down_armed", True):
            st["rel_down_armed"] = False
            title = f"[033500/017960] 20거래일 상대 {chg:+.1f}% | 페어 재점검"
            status = (
                f"{base} 동성이 한국카본보다 20거래일 상대 -{r['rel_pct']:.0f}% 이하. "
                f"{-r['rel_rearm_pct']:+.0f}% 안으로 돌아와야 다시 무장."
            )
            act = (
                "동성 우위 전제가 흔들리는지 확인하세요. "
                "중국 점유 확대 같은 약세 시나리오면 한국카본 상대 우위를 다시 보세요."
            )
            out.append((title, clean(body(stamp, "033500/017960", "동성화인텍/한국카본", "상대 약세 재점검", WHAT, status, act, later, trim, avoid)), 4))
        elif abs(chg) < r["rel_rearm_pct"]:
            st["rel_down_armed"] = True
    return out


def fail_alerts(cfg: dict, st: dict, errs: dict, today: str, stamp: str) -> list[tuple[str, str, int]]:
    out: list[tuple[str, str, int]] = []
    fails = st.setdefault("fail", {})
    key = "src"
    f = fails.get(key) or {"days": 0, "last": None, "notified": False}
    if errs:
        if f["last"] != today:
            f["days"] += 1
            f["last"] = today
        if f["days"] >= cfg["rules"]["fail_ops_after_days"] and not f["notified"]:
            f["notified"] = True
            detail_s = "; ".join(f"{k}: {v}" for k, v in errs.items())[:400]
            title = "[033500/017960] 조회 실패 | 원문 확인 필요"
            status = f"데이터 조회가 {f['days']}일 연속 실패. {detail_s}. 해당 규칙은 건너뜁니다."
            msg = body(
                stamp, "033500/017960", "동성화인텍/한국카본", "조회 실패",
                "동성화인텍과 한국카본 페어 감시의 데이터 읽기 실패 안내. 하루 1번. 매매 신호 아님.",
                status,
                "KIND, DART, 네이버 증권에서 공시와 종가를 직접 확인하세요.",
                "다음 슬롯에서 다시 읽고, 못 본 공시는 그때 보냅니다.",
                "조회 실패를 매매 신호로 쓰지 마세요.",
                "실패한 숫자를 추정해서 적기.",
            )
            out.append((clean(title), clean(msg), 3))
        fails[key] = f
    else:
        fails[key] = {"days": 0, "last": None, "notified": False}
    return out


def check_text(*parts: str) -> None:
    s = "".join(parts)
    assert not re.search("[\u00b7\u318d\u2014\u2013]", s), s


# ---------- 실행 ----------
def evaluate_filings(st: dict, filings: list[dict], stamp: str, seed_only: bool) -> list[tuple[str, str, int]]:
    out: list[tuple[str, str, int]] = []
    for r in filings:
        if already_seen(st, r):
            mark_seen(st, r)
            continue
        mark_seen(st, r)
        if seed_only:
            continue
        p = parse(r.get("body") or "")
        out.append(filing_message(r, p, stamp))
    return out


def run(mode: str, now: datetime, cfg: dict) -> int:
    dry = mode == "dry"
    topic = resolve_topic("port")
    server = resolve_server(cfg.get("ntfy_server"))
    day = session_day(now)
    today = day.isoformat()
    stamp = now.strftime("%Y-%m-%d %H:%M KST")

    if ended(cfg, now):
        print(f"ended after {cfg.get('ends_kst')}; silent")
        return 0
    if not is_trading_day(day) and mode != "dry":
        print(f"{today} KRX holiday; silent")
        return 0

    since = (now - timedelta(days=400)).strftime("%Y-%m-%d")
    filings, fnotes, ferrs = fetch_filings(since, today)
    bars, perrs = gather_prices()
    errs: dict[str, str] = {f"filings_{k}": v for k, v in ferrs.items()}
    errs.update({f"px_{k}": v for k, v in perrs.items()})
    if fnotes:
        print("filing notes: " + " | ".join(fnotes[:8]))
    print(json.dumps({
        "filings_n": len(filings),
        "bars": {c: (rows[-1] if rows else None) for c, rows in bars.items()},
        "errs": errs,
    }, ensure_ascii=False, default=str))

    st = load_state()
    seed_only = not st.get("seeded")
    alerts: list[tuple[str, str, int]] = []
    alerts.extend(evaluate_filings(st, filings, stamp, seed_only=seed_only))
    if seed_only:
        st["seeded"] = True
        st["seed_note"] = f"{stamp} 시드. seen {len(st.get('seen') or [])}건"
        print(st["seed_note"])
    else:
        alerts.extend(price_alerts(cfg, st, bars, today, stamp))
    alerts.extend(fail_alerts(cfg, st, errs, today, stamp))

    for title, msg, pri in alerts:
        dest = route_topic(topic, title)
        print(f"TITLE: {title}\nPRIORITY: {pri} TOPIC: {dest}\n{msg}\n")
        check_text(title, msg)
        if not dry:
            publish(dest, title, msg, pri, ["office"], server)
    if not alerts:
        print("새 알림 없음" if not seed_only else "시드만, 발송 없음")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    st["updated_at"] = now.isoformat()
    save_state(st)
    return 0


# ---------- selftest ----------
SAMPLE_DS = (
    "단일판매ㆍ공급계약체결 1. 판매ㆍ공급계약 내용 LNG운반선의 초저온 보냉자재 공급계약 "
    "2. 계약내역 조건부 계약여부 미해당 확정 계약금액 192,318,052,200 조건부 계약금액 - "
    "계약금액 총액(원) 192,318,052,200 최근 매출액(원) 742,186,008,411 매출액 대비(%) 25.9 "
    "3. 계약상대방 HD현대중공업 주식회사 - 최근 매출액(원) 17,580,633,257,000 "
    "- 주요사업 선박과 해양구조물 4. 판매ㆍ공급지역 계약상대방이 지정하는 장소 "
    "5. 계약기간 시작일 2026-11-05 종료일 2029-03-31"
)
SAMPLE_KC = (
    "단일판매ㆍ공급계약 체결 1. 판매ㆍ공급계약 구분 기타 판매ㆍ공급계약 "
    "- 체결계약명 LNG 수송선 화물창용 초저온 보냉자재 공급계약 "
    "2. 계약내역 계약금액(원) 72,000,000,000 최근매출액(원) 908,810,220,281 매출액대비(%) 7.9 "
    "대규모법인여부 미해당 3. 계약상대 한화오션(주) - 회사와의 관계 - "
    "4. 판매ㆍ공급지역 국내 5. 계약기간 시작일 2026-05-21 종료일 2027-12-26"
)
SAMPLE_CORR = (
    "정정신고(보고) 정정일자 2026-07-23 1. 정정관련 공시서류 단일판매ㆍ공급계약체결 "
    "3. 정정사유 계약금액조정을 수반한 변경합의서 체결 4. 정정사항 "
    "단일판매ㆍ공급계약체결 1. 판매ㆍ공급계약 내용 LNG운반선의 초저온 보냉자재 공급계약 "
    "2. 계약내역 확정 계약금액 311,292,912,000 계약금액 총액(원) 311,292,912,000 "
    "최근 매출액(원) 531,408,738,039 매출액 대비(%) 58.6 "
    "3. 계약상대방 HD현대중공업 주식회사 - 최근 매출액(원) 1 "
    "5. 계약기간 시작일 2024-10-31 종료일 2028-09-30"
)


def selftest() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    fails: list[str] = []

    def check(name, got, want):
        ok = got == want
        print(("ok   " if ok else "FAIL ") + f"{name}: got={got!r} want={want!r}")
        if not ok:
            fails.append(name)

    check("target 체결", is_target("단일판매ㆍ공급계약체결"), True)
    check("target 자율", is_target("단일판매ㆍ공급계약체결(자율공시)"), True)
    check("target 정정", is_target("[정정] 단일판매ㆍ공급계약체결"), True)
    check("target mid-dot", is_target("단일판매·공급계약 체결"), True)
    check("skip 해지", is_target("단일판매ㆍ공급계약해지"), False)
    check("skip 실적", is_target("연결재무제표기준영업(잠정)실적(공정공시)"), False)

    p = parse(SAMPLE_DS)
    check("ds amount", p and p["amount"], 192318052200.0)
    check("ds ratio", p and p["ratio"], 25.9)
    check("ds party", p and p["party"], "HD현대중공업 주식회사")
    check("ds end", p and p["end"], "2029-03-31")

    p2 = parse(SAMPLE_KC)
    check("kc amount", p2 and p2["amount"], 72000000000.0)
    check("kc ratio", p2 and p2["ratio"], 7.9)
    check("kc party", p2 and p2["party"], "한화오션(주)")

    p3 = parse(SAMPLE_CORR)
    check("corr amount", p3 and p3["amount"], 311292912000.0)
    check("corr reason", bool(p3 and p3.get("corr_reason")), True)

    stamp = "2026-11-16 19:00 KST"
    rec = {
        "id": "kind:033500:20261105000001",
        "code": "033500",
        "acptno": "20261105000001",
        "title": "단일판매ㆍ공급계약체결",
        "filed": "2026-11-05 15:50",
        "src": "KIND",
        "url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20261105900115",
        "body": SAMPLE_DS,
        "dart_rcept": "20261105900115",
    }
    t, m, pri = filing_message(rec, parse(SAMPLE_DS), stamp)
    check("title ticker", "[033500]" in t and "1,923억원" in t and "25.9%" in t, True)
    check("pri new", pri, 4)
    check("body dart", "20261105900115" in m, True)
    check_text(t, m)

    st = default_state()
    a0 = evaluate_filings(st, [rec], stamp, seed_only=True)
    check("seed silent", a0, [])
    check("seed marked", already_seen(st, rec), True)
    a1 = evaluate_filings(st, [rec], stamp, seed_only=False)
    check("dedupe after seed", a1, [])

    st2 = default_state()
    a2 = evaluate_filings(st2, [rec], stamp, seed_only=False)
    check("new filing sends", len(a2), 1)
    a3 = evaluate_filings(st2, [rec], stamp, seed_only=False)
    check("second skip", a3, [])

    rec_c = {**rec, "id": "kind:033500:20260723000002", "acptno": "20260723000002",
             "title": "(정정)단일판매ㆍ공급계약체결", "body": SAMPLE_CORR, "dart_rcept": "20260723900000"}
    tc, mc, pric = filing_message(rec_c, parse(SAMPLE_CORR), stamp)
    check("corr title", "정정" in tc and "[033500]" in tc, True)
    check("corr pri", pric, 3)
    check_text(tc, mc)

    # prices
    def bars_ds(close, n=25, start="2026-10-01"):
        d0 = datetime.strptime(start, "%Y-%m-%d")
        return [((d0 + timedelta(days=i)).strftime("%Y-%m-%d"), float(close)) for i in range(n)]

    def pair(ds_c, kc_c, n=25):
        d0 = datetime(2026, 10, 1)
        ds, kc = [], []
        for i in range(n):
            day = (d0 + timedelta(days=i)).strftime("%Y-%m-%d")
            ds.append((day, float(ds_c[i] if isinstance(ds_c, list) else ds_c)))
            kc.append((day, float(kc_c[i] if isinstance(kc_c, list) else kc_c)))
        return {"033500": ds, "017960": kc}

    st = default_state()
    st["seeded"] = True
    a = price_alerts(cfg, st, pair(11300, 13910), "2026-11-16", stamp)
    check("current silent", a, [])

    st = default_state(); st["seeded"] = True
    a = price_alerts(cfg, st, pair(10000, 13910), "2026-11-16", stamp)
    check("ds low fire", len(a) == 1 and "[033500]" in a[0][0] and "10,000" in a[0][0] and a[0][2] == 4, True)
    check("ds low disarmed", st["ds_low_armed"], False)
    a = price_alerts(cfg, st, pair(10200, 13910), "2026-11-17", stamp)
    check("ds low stays", a, [])
    a = price_alerts(cfg, st, pair(10401, 13910), "2026-11-18", stamp)
    check("ds low rearm", st["ds_low_armed"], True)
    a = price_alerts(cfg, st, pair(9900, 13910), "2026-11-19", stamp)
    check("ds low again", len(a) == 1, True)
    check_text(*(x[0] + x[1] for x in a))

    st = default_state(); st["seeded"] = True
    a = price_alerts(cfg, st, pair(15000, 13910), "2026-11-16", stamp)
    check("ds high fire", len(a) == 1 and "15,000" in a[0][0] and a[0][2] == 3, True)
    a = price_alerts(cfg, st, pair(14600, 13910), "2026-11-17", stamp)
    check("ds high stays", st["ds_high_armed"], False)
    a = price_alerts(cfg, st, pair(14499, 13910), "2026-11-18", stamp)
    check("ds high rearm", st["ds_high_armed"], True)

    st = default_state(); st["seeded"] = True
    a = price_alerts(cfg, st, pair(11300, 12000), "2026-11-16", stamp)
    check("kc low fire", len(a) == 1 and "[017960]" in a[0][0] and "12,000" in a[0][0], True)
    a = price_alerts(cfg, st, pair(11300, 12400), "2026-11-17", stamp)
    check("kc low stays", st["kc_low_armed"], False)
    a = price_alerts(cfg, st, pair(11300, 12501), "2026-11-18", stamp)
    check("kc low rearm", st["kc_low_armed"], True)

    # relative ±15% with prices inside the bands so only the relative rule fires
    st = default_state(); st["seeded"] = True
    ds = [11300.0] * 21 + [13108.0]  # +16%
    kc = [14000.0] * 22
    a = price_alerts(cfg, st, pair(ds, kc, n=22), "2026-11-16", stamp)
    check("rel up fire", len(a) == 1 and "[033500/017960]" in a[0][0] and "상대" in a[0][0], True)
    check("rel up disarmed", st["rel_up_armed"], False)
    ds2 = [11300.0] * 21 + [12091.0]  # +7%
    a = price_alerts(cfg, st, pair(ds2, kc, n=22), "2026-11-17", stamp)
    check("rel up rearm", st["rel_up_armed"], True)

    st = default_state(); st["seeded"] = True
    ds = [13000.0] * 21 + [10920.0]  # -16%, still above 10,000
    kc = [14000.0] * 22
    a = price_alerts(cfg, st, pair(ds, kc, n=22), "2026-11-16", stamp)
    check("rel down fire", len(a) == 1 and "[033500/017960]" in a[0][0], True)
    check("rel down disarmed", st["rel_down_armed"], False)

    # ops + end
    st = default_state()
    fa = fail_alerts(cfg, st, {"px_033500": "boom"}, "2026-11-16", stamp)
    check("ops once", len(fa) == 1 and "조회 실패" in fa[0][0] and "033500" in fa[0][0], True)
    fa2 = fail_alerts(cfg, st, {"px_033500": "boom"}, "2026-11-16", stamp)
    check("ops dedupe same day", fa2, [])
    check_text(fa[0][0], fa[0][1])

    past = datetime(2027, 11, 17, 0, 0, tzinfo=KST)
    check("ended", ended(cfg, past), True)
    check("not ended now", ended(cfg, datetime(2026, 11, 16, 19, 0, tzinfo=KST)), False)

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
            title = "[033500/017960] 조회 실패 | 스크립트 오류"
            msg = body(
                stamp, "033500/017960", "동성화인텍/한국카본", "스크립트 오류",
                "동성화인텍과 한국카본 페어 감시의 스크립트 오류 안내. 매매 신호 아님.",
                f"스크립트 오류 {type(exc).__name__}: {str(exc)[:200]}. state 는 그대로.",
                "KIND 와 DART 에서 공시를 직접 확인하세요.",
                "다음 슬롯에서 다시 실행됩니다.",
                "오류를 매매 신호로 쓰지 마세요.",
                "실패한 숫자를 추정하기.",
            )
            publish(resolve_topic("ops"), clean(title), clean(msg), 3, ["warning"], resolve_server(cfg.get("ntfy_server")), config_name=NAME)
        except Exception:  # noqa: BLE001
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
