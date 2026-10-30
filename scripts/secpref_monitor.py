#!/usr/bin/env python3
"""[SEC-PREF] 삼성전자 자사주 결의와 주주환원 공시 감시 (2026-10-29 ~ 2027-02-27).

morning 09:00 / afternoon 15:20 / k203780 19:00 슬롯. KIND 상세검색(실패하면 네이버 KRX 공시 피드)에서
'자기주식 취득 결정', '주식 소각 결정', '자기주식취득 신탁계약 체결 결정' 과 그 정정을 공시마다 1번(p4) 알림.
본문이 읽히면 보통주/우선주(기타주식, 종류주식) 별 수량과 금액, 우선주 비중(수량, 금액)을 붙임.
처분 결정, 결과보고서, 변경상장은 보내지 않음. 읽기 실패는 cwally-ops 하루 1번.
2026-10-30 추가(채팅 승인 t21u): 현금ㆍ현물배당결정(현금배당결정 포함), 기업가치 제고 계획, 제목이나 본문에 '주주환원' 이 들어간
자율공시와 공정공시(정정 포함). 2027-01-15 ~ 2027-02-27 첫 해당 공시(모든 종류)는 '1월 이사회 주주환원 공시' 꼬리표와 p5, 그 뒤는 p4.
새 종류는 배포 뒤 첫 실행에서 이미 나와 있던 공시를 본 것으로만 기록(소급 발송 없음, state seed_v2).
괴리율은 기존 [005935] 알림이 맡음(여기서는 안 봄).
modes: auto | dry(발송 없음, state 안 씀) | install(설치 확인 1회, state 안 씀) | selftest ; --as-of "YYYY-MM-DD HH:MM" (dry 전용)
"""
from __future__ import annotations

import json
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import secdiv_monitor as sd  # noqa: E402  (읽기 전용 재사용: http, text_of, clean, kind_body, KIND, KIND_VIEW)
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = sd.KST
TAG = "[SEC-PREF]"
NAME = "secpref"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
NAVER_NOTICE = "https://finance.naver.com/item/news_notice.naver?code=005930"
BUY_RE = re.compile(r"자기주식\s*취득\s*결정")
BURN_RE = re.compile(r"(?<!변경상장\()주식\s*소각\s*결정")
TRUST_RE = re.compile(r"자기주식\s*취득\s*신탁\s*계약\s*체결\s*결정")
DIV_RE = re.compile(r"현금\s*[\u318d\u00b7.,]?\s*현물\s*배당\s*결정|현금\s*배당\s*결정")
VUP_RE = re.compile(r"기업\s*가치\s*제고\s*계획")
DISC_RE = re.compile(r"자율\s*공시|공정\s*공시")
NEW_KINDS = {"div", "vup", "ret"}


def kind_of(title: str) -> str | None:
    t = title or ""
    if re.search(r"처분|결과보고서|변경상장|해지", t):
        return None
    if TRUST_RE.search(t):
        return "trust"
    if BUY_RE.search(t):
        return "buy"
    if BURN_RE.search(t):
        return "burn"
    if DIV_RE.search(t):
        return "div"
    if VUP_RE.search(t):
        return "vup"
    if "주주환원" in t and DISC_RE.search(t):
        return "ret"
    return None


def body_candidate(title: str) -> bool:
    """제목에 종류가 안 보이는 자율공시, 공정공시: 본문에 '주주환원' 이 있으면 ret."""
    t = title or ""
    return kind_of(t) is None and bool(DISC_RE.search(t)) and not re.search(r"처분|결과보고서|변경상장|해지", t)


def show_title(t: str) -> str:
    return re.sub(r"\s*[\u318d\u00b7]\s*", ", ", t or "")


def is_correction(title: str) -> bool:
    return "정정" in (title or "")


def norm_title(t: str) -> str:
    return re.sub(r"\s+", "", t or "")


# ---------- 목록 ----------
def kind_list(cfg: dict, since: str, until: str) -> list[dict]:
    form = {
        "method": "searchDetailsSub", "currentPageSize": "100", "pageIndex": "1", "orderMode": "1", "orderStat": "D",
        "forward": "details_sub", "searchCodeType": "", "repIsuSrtCd": "A" + cfg["code"], "allRepIsuSrtCd": "",
        "oldSearchCorpName": "", "disclosureType": "", "disTypevalue": "", "reportNm": "", "reportCd": "",
        "searchCorpName": cfg["code"], "business": "", "marketType": "", "settlementMonth": "", "securities": "",
        "submitOblgNm": "", "enterprise": "", "fromDate": since, "toDate": until, "reportNmTemp": "", "bfrDsclsType": "on",
    }
    page = sd.http(sd.KIND + "/disclosure/details.do", data=form,
                   headers={"X-Requested-With": "XMLHttpRequest", "Referer": sd.KIND + "/disclosure/details.do?method=searchDetailsMain"})
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
        title = re.sub(r"^" + cfg["corp_name"] + r"\s+", "", title)
        title = re.sub(r"\s+(" + cfg["corp_name"] + r"|유가증권시장본부)$", "", title).strip()
        out.append({"id": "kind:" + acpt.group(1), "acptno": acpt.group(1), "title": title,
                    "filed": f"{dm.group(1)} {dm.group(2)}" if dm else "", "src": "KIND"})
    if rows == 0 and "조회된 결과값이 없습니다" not in page and "<table" not in page:
        raise RuntimeError("KIND 응답 형식 이상")
    return sorted(out, key=lambda r: r["acptno"])


def naver_list(cfg: dict, since: str, until: str) -> list[dict]:
    raw = json.loads(sd.http(f"https://stock.naver.com/api/domestic/detail/notice?itemCode={cfg['code']}&page=1&pageSize=100"))
    out = []
    for r in raw if isinstance(raw, list) else []:
        title = re.sub(r"^삼성전자\(주\)\s*", "", str(r.get("title") or "").strip())
        day = str(r.get("datetime") or "")[:10]
        if since <= day <= until:
            out.append({"id": "naver:" + str(r.get("no") or r.get("bizdateSeq")), "acptno": None, "title": title,
                        "filed": day, "src": "네이버 KRX 공시 피드", "body": sd.text_of(str(r.get("contents") or ""))})
    return sorted(out, key=lambda r: r["id"])


def fetch_filings(cfg: dict, since: str, until: str, known: set | None = None) -> tuple[list[dict], list[str]]:
    """해당 공시 목록. known(이미 본 id, 본문 확인이 끝난 id)은 본문을 다시 읽지 않음."""
    notes: list[str] = []
    known = known or set()

    def pick(rows: list[dict]) -> list[dict]:
        out = []
        for r in rows:
            k = kind_of(r["title"])
            if k:
                r["kind"] = k
                out.append(r)
            elif body_candidate(r["title"]):
                r["kind"] = None
                out.append(r)
        return out

    try:
        reps = pick(kind_list(cfg, since, until))
        nav = None
        for r in reps:
            if r["id"] in known:
                r["body"] = ""
                continue
            try:
                r["body"] = sd.kind_body(r["acptno"])
            except Exception as exc:  # noqa: BLE001
                notes.append(f"KIND 본문 {r['acptno']}: {exc}")
                try:
                    nav = nav if nav is not None else naver_list(cfg, since, until)
                    m = [n for n in nav if n["filed"] == r["filed"][:10] and norm_title(n["title"]) == norm_title(r["title"])]
                    r["body"] = m[-1]["body"] if m else ""
                except Exception as exc2:  # noqa: BLE001
                    notes.append(f"네이버 본문: {exc2}")
                    r["body"] = ""
        return reps, notes
    except Exception as exc:  # noqa: BLE001
        notes.append(f"KIND 목록: {exc}")
    return pick(naver_list(cfg, since, until)), notes


def resolve_kind(r: dict) -> str | None:
    k = r.get("kind") if "kind" in r else kind_of(r["title"])
    if k:
        return k
    if body_candidate(r["title"]) and "주주환원" in (r.get("body") or ""):
        return "ret"
    return None


# ---------- 본문 해석 ----------
def _n(tok: str | None) -> float | None:
    if tok is None or tok.strip() in {"-", ""}:
        return None
    try:
        return float(tok.replace(",", ""))
    except ValueError:
        return None


NUM = r"(-|[\d,]+)"


def parse(kind: str, text: str) -> dict | None:
    t = re.sub(r"\s+", " ", text or "")
    if kind == "buy":
        anchors = [m.start() for m in re.finditer(r"자기주식 취득 결정 1\. 취득예정주식", t)]
        if not anchors:
            return None
        s = t[anchors[-1]:]
        q = re.search(r"취득예정주식\s*\(주\)\s*보통주식\s*" + NUM + r"\s*기타주식\s*" + NUM, s)
        a = re.search(r"취득예정금액\s*\(원\)\s*보통주식\s*" + NUM + r"\s*기타주식\s*" + NUM, s)
        if not q and not a:
            return None
        per = re.search(r"취득예상기간\s*시작일\s*(\d{4}년 \d{2}월 \d{2}일)\s*종료일\s*(\d{4}년 \d{2}월 \d{2}일)", s)
        pur = re.search(r"취득목적\s*(.+?)\s*6\.\s*취득방법", s)
        return {"cq": _n(q.group(1)) if q else None, "pq": _n(q.group(2)) if q else None,
                "ca": _n(a.group(1)) if a else None, "pa": _n(a.group(2)) if a else None,
                "period": f"{per.group(1)} ~ {per.group(2)}" if per else None, "purpose": pur.group(1).strip()[:60] if pur else None}
    if kind == "burn":
        q = re.search(r"소각할 주식의 종류와 수\s*보통주식\s*\(주\)\s*" + NUM + r"\s*종류주식\s*\(주\)\s*" + NUM, t)
        if not q:
            return None
        amt = re.search(r"소각예정금액\s*\(원\)\s*" + NUM, t)
        mkt = re.search(r"종가인?\s*보통주 주당\s*([\d,]+)원,?\s*우선주 주당\s*([\d,]+)원을 기준으로 산출한 금액은\s*([\d,]+)원", t)
        day = re.search(r"소각 예정일\s*(\d{4}-\d{2}-\d{2})", t)
        out = {"cq": _n(q.group(1)), "pq": _n(q.group(2)), "total_book": _n(amt.group(1)) if amt else None,
               "burn_day": day.group(1) if day else None, "ca": None, "pa": None}
        if mkt:
            cpx, ppx = _n(mkt.group(1)), _n(mkt.group(2))
            out.update(ca=(out["cq"] or 0) * cpx, pa=(out["pq"] or 0) * ppx, amt_basis=f"이사회 당일 종가(보통주 {cpx:,.0f}원, 우선주 {ppx:,.0f}원) 기준")
        return out
    if kind == "div":
        q = re.search(r"1주당\s*배당금\s*\(원\)\s*보통주식\s*" + NUM + r"\s*종류주식\s*" + NUM, t)
        tot = re.search(r"배당금\s*총액\s*\(원\)\s*" + NUM, t)
        if not q and not tot:
            return None
        kd = re.search(r"배당구분\s*(\S+)", t)
        rec = re.search(r"배당기준일\s*(\d{4}-\d{2}-\d{2})", t)
        ptot = re.search(r"삼성전자우\s*우선주\s*[\d,]+\s*[\d.]+\s*([\d,]+)", t)
        return {"dps_c": _n(q.group(1)) if q else None, "dps_p": _n(q.group(2)) if q else None,
                "total": _n(tot.group(1)) if tot else None, "total_p": _n(ptot.group(1)) if ptot else None,
                "div_kind": kd.group(1) if kd else None, "record": rec.group(1) if rec else None}
    if kind in ("vup", "ret"):
        if not t.strip():
            return None
        def snip(pat: str) -> str | None:
            m = re.search(pat, t)
            return re.sub(r"\s+", " ", m.group(0)).strip()[:90] if m else None
        return {"buy_txt": snip(r"(자기주식|자사주)[^.]{0,60}?\d[\d,.]*\s*(조|억)\s*원"),
                "div_txt": snip(r"배당[^.]{0,60}?\d[\d,.]*\s*(조|억)\s*원"),
                "dps_txt": snip(r"주당\s*배당금?[^.]{0,40}?\d[\d,]*\s*원"),
                "pref_txt": snip(r"우선주[^.]{0,60}?(\d[\d,.]*\s*(조|억)\s*원|\d[\d,]*\s*주|\d+(\.\d+)?\s*%)")}
    if kind == "trust":
        amt = re.search(r"계약금액\s*\(원\)\s*" + NUM, t)
        per = re.search(r"계약기간\s*시작일\s*(\S+ ?\S* ?\S*)\s*종료일\s*(\S+ ?\S* ?\S*?)\s*\d\.", t)
        pur = re.search(r"계약목적\s*(.+?)\s*\d\.\s", t)
        if not amt:
            return None
        return {"total": _n(amt.group(1)), "period": f"{per.group(1)} ~ {per.group(2)}" if per else None,
                "purpose": pur.group(1).strip()[:60] if pur else None, "cq": None, "pq": None, "ca": None, "pa": None}
    return None


def jo(v: float | None) -> str:
    if v is None:
        return "?"
    return f"{v / 1e12:,.2f}조원" if v >= 1e12 else f"{v / 1e8:,.0f}억원"


def sh(v: float | None) -> str:
    return "없음" if v is None else f"{v:,.0f}주"


def share(p: float | None, c: float | None) -> str | None:
    if p is None and c is None:
        return None
    tot = (p or 0) + (c or 0)
    return f"{(p or 0) / tot * 100:.1f}%" if tot else None


LABEL = {"buy": "자기주식 취득 결정", "burn": "주식 소각 결정", "trust": "자기주식취득 신탁계약 체결 결정",
         "div": "현금, 현물 배당 결정", "vup": "기업가치 제고 계획", "ret": "주주환원 관련 공시(자율, 공정공시)"}
JAN_TAG = "1월 이사회 주주환원 공시"


def won(v: float | None) -> str:
    return "?" if v is None else f"{v:,.0f}원"


def return_items(kind: str, p: dict | None) -> str:
    """주주환원 4개 항목. 못 읽거나 공시에 없는 항목은 그렇다고 적고 추정하지 않음."""
    na, unread = "이 공시에 없음", "자동으로 못 읽음, 원문 확인"
    p = p or {}
    if kind == "div" and p:
        dps = f"보통주 {won(p.get('dps_c'))}, 우선주 {won(p.get('dps_p'))}" if p.get("dps_c") is not None else unread
        tot = jo(p["total"]) if p.get("total") else unread
        if p.get("total_p"):
            tot += f" (우선주 {jo(p['total_p'])})"
        buy, mix = na, na
    elif kind in ("vup", "ret") and p:
        dps = p.get("dps_txt") and f"원문 '{p['dps_txt']}'" or unread
        tot = p.get("div_txt") and f"원문 '{p['div_txt']}'" or unread
        buy = p.get("buy_txt") and f"원문 '{p['buy_txt']}'" or unread
        mix = p.get("pref_txt") and f"원문 '{p['pref_txt']}'" or unread
    elif kind in ("buy", "burn") and p:
        dps, tot = na, na
        amt = (p.get("ca") or 0) + (p.get("pa") or 0)
        buy = jo(amt) if amt else (f"보통주 {sh(p.get('cq'))}, 우선주 {sh(p.get('pq'))}" if p.get("cq") is not None else unread)
        sq = share(p.get("pq"), p.get("cq"))
        sa = share(p.get("pa"), p.get("ca")) if (p.get("pa") or p.get("ca")) else None
        mix = ", ".join(x for x in [sq and f"우선주 수량 {sq}", sa and f"우선주 금액 {sa}"] if x) or unread
    elif kind == "trust" and p:
        dps, tot, mix = na, na, "신탁 공시는 보통주와 우선주 구분이 없을 수 있음, 원문 확인"
        buy = f"신탁 계약금액 {jo(p.get('total'))}" if p.get("total") else unread
    else:
        dps = tot = buy = mix = unread
    return f"주주환원 항목: 주당 배당금 {dps}. 배당 총액 {tot}. 자사주 매입 규모 {buy}. 보통주와 우선주 매입 비중 {mix}."


def detail_line(kind: str, p: dict | None) -> str:
    if kind in NEW_KINDS:
        if not p:
            return "본문을 자동으로 못 읽음. 링크에서 원문을 직접 확인하세요."
        if kind == "div":
            extra = [x for x in [p.get("div_kind") and f"배당구분 {p['div_kind']}", p.get("record") and f"배당기준일 {p['record']}"] if x]
            return (", ".join(extra) + ".") if extra else "배당 공시."
        return "본문에서 주주환원 관련 숫자를 찾아 아래에 원문 그대로 붙임."
    if not p:
        return "숫자는 자동으로 못 읽음. 링크에서 보통주와 우선주 수량, 금액을 직접 확인하세요."
    parts = []
    if kind == "trust":
        parts.append(f"계약금액 {jo(p.get('total'))}")
        if p.get("period"):
            parts.append(f"계약기간 {p['period']}")
        if p.get("purpose"):
            parts.append(f"목적 {p['purpose']}")
        parts.append("신탁 공시에는 보통주와 우선주 구분이 없을 수 있어 원문 확인")
        return ", ".join(parts) + "."
    verb = "취득" if kind == "buy" else "소각"
    parts.append(f"보통주 {sh(p.get('cq'))}" + (f" {jo(p['ca'])}" if p.get("ca") else ""))
    parts.append(f"우선주 {sh(p.get('pq'))}" + (f" {jo(p['pa'])}" if p.get("pa") else ""))
    line = f"{verb} 예정: " + ", ".join(parts)
    sq, sa = share(p.get("pq"), p.get("cq")), share(p.get("pa"), p.get("ca")) if (p.get("pa") or p.get("ca")) else None
    w = []
    if sq:
        w.append(f"수량 기준 {sq}")
    if sa:
        w.append(f"금액 기준 {sa}")
    if w:
        line += f". 우선주 비중 {', '.join(w)}"
    if kind == "burn":
        if p.get("amt_basis"):
            line += f" (금액은 {p['amt_basis']})"
        if p.get("total_book"):
            line += f". 소각예정금액(장부가) {jo(p['total_book'])}"
        if p.get("burn_day"):
            line += f", 소각 예정일 {p['burn_day']}"
    else:
        if p.get("period"):
            line += f". 취득기간 {p['period']}"
        if p.get("purpose"):
            line += f", 목적 {p['purpose']}"
    return line + "."


def link_of(r: dict) -> str:
    return sd.KIND_VIEW.format(acpt=r["acptno"]) if r.get("acptno") else f"{NAVER_NOTICE} (네이버 공시 목록, KIND 목록을 못 읽음)"


def body(stamp: str, hdr: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([f"{stamp} | 005930 | 삼성전자 {hdr}", f"이게 뭐 알림: {what}", f"지금 결과/상태: {status}",
                      f"지금 할 일: {action}", f"나중에: {later}", f"익절/줄일 때: {trim}", f"하지 말 것: {avoid}"])


WHAT = ("삼성전자 자사주 결의(취득, 소각, 신탁계약), 현금 배당 결정, 기업가치 제고 계획, 주주환원 자율공시와 공정공시(정정 포함)가 나올 때마다 "
        "1번 보내는 알림. 우선주(005935) 몫을 함께 봄. 매매 지시 아님.")


def message(cfg: dict, r: dict, kind: str, p: dict | None, stamp: str, jan: bool = False, in_jan: bool = False) -> tuple[str, str, int]:
    corr = is_correction(r["title"])
    lab = LABEL[kind]
    pq = (p or {}).get("pq") if kind in ("buy", "burn", "trust") else None
    pref_tag = ""
    if kind in ("buy", "burn", "trust"):
        pref_tag = ", 우선주 포함" if pq else (", 보통주만" if p and kind != "trust" and p.get("cq") else "")
    head = f"{JAN_TAG} | " if jan else ""
    title = f"{TAG} {head}{'정정 ' if corr else ''}{lab}{pref_tag}"
    st = f"{r['filed']} 접수 '{show_title(r['title'])}' ({r['src']}). {detail_line(kind, p)}"
    if kind in NEW_KINDS or jan or in_jan:
        st += " " + return_items(kind, p)
    st += f" 링크: {link_of(r)}"
    if corr:
        act = "정정 사유와 바뀐 숫자를 원문 정정 표에서 확인하세요. 우선주 몫이 바뀌었는지 보기."
    elif jan:
        act = "1월 이사회 주주환원 결정입니다. 우선주 매입 비중과 배당을 [005935] 할인율 알림과 같이 보고 우선주와 보통주 중 어느 쪽을 살지 점검."
    elif kind == "div":
        act = "배당 금액만 확인. 우선주와 보통주 주당 배당이 같으면 할인율이 큰 쪽이 배당수익률이 높음."
    elif kind in ("vup", "ret"):
        act = "주주환원 규모와 우선주 몫을 원문에서 확인. [005935] 할인율 알림과 같이 보기."
    elif pq:
        act = "우선주 매입이나 소각이 들어감. 할인율 축소 요인이니 [005935] 할인율 알림과 같이 보고 우선주 비중 계획 점검."
    else:
        act = "우선주 몫이 없거나 확인 안 됨. 우선주 비중 계획은 그대로 두고 1월 이사회 결의를 기다리기."
    later = "같은 종류의 다음 공시나 정정이 나오면 1번씩 더 보냅니다. 2027년 1월 이사회의 최종 주주환원 결정이 핵심."
    trim = "우선주 매입 확대로 할인율이 20% 이하로 좁혀지면 보통주와 다시 비교([005935] 알림 기준)."
    avoid = "공시 제목만 보고 바로 매매하기. 원문 숫자부터 확인. 못 읽은 항목을 추정하기."
    pri = 5 if jan else cfg["priority"]
    return title, sd.clean(body(stamp, "주주환원 공시", WHAT, st, act, later, trim, avoid)), pri


# ---------- 실행 ----------
def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(st: dict) -> None:
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def check_text(t: str, m: str) -> None:
    assert not re.search("[\u00b7\u318d\u2014]", t + m), t


def ops_msg(stamp: str, status: str) -> tuple[str, str]:
    return (f"{TAG} 공시 조회 실패 | 원문 확인 필요",
            body(stamp, "공시 읽기 실패", "삼성전자 자사주 공시 감시의 데이터 읽기 실패 안내. 하루 1번. 매매 신호 아님.", status,
                 "KIND 나 DART 에서 삼성전자 공시를 직접 보세요.", "다음 슬롯에서 다시 읽고, 못 본 공시는 그때 보냅니다.",
                 "이 안내로 비중을 바꾸지 않기.", "읽기 실패를 공시 없음으로 읽기."))


def install_message(stamp: str) -> tuple[str, str]:
    return (f"{TAG} 설치 확인 | 매매 신호 아님",
            body(stamp, "설치 확인", "삼성전자 자사주 결의 공시 감시 설치 확인용 테스트 발송입니다. 매매 신호가 아닙니다.",
                 "평일 09:00, 15:20, 19:00 KST 에 KIND 공시를 보고 자기주식 취득 결정, 주식 소각 결정, 자기주식취득 신탁계약 체결 결정"
                 ", 현금 배당 결정, 기업가치 제고 계획, 주주환원 자율공시와 공정공시(정정 포함)가 나오면 공시마다 1번 보냅니다. 읽히면 보통주와 우선주 수량, 금액, 우선주 비중을 붙입니다. 괴리율은 기존 [005935] 알림이 맡습니다.",
                 "알림이 도착했는지 확인만 하세요.", "2027년 1월 이사회 결정까지 보고 2/27 에 끝납니다.",
                 "해당 없음.", "이 메시지를 매매 신호로 해석하기."))


def step(cfg: dict, st: dict, now: datetime, stamp: str, errs: list[str], fetch=None) -> list[tuple[str, str, int]]:
    today = now.strftime("%Y-%m-%d")
    if not (cfg["window_start"] <= today <= cfg["window_end"]):
        print(f"{today} 감시 기간 밖({cfg['window_start']} ~ {cfg['window_end']}): 조용히 종료")
        return []
    seen = set(st.get("seen") or [])
    checked = set(st.get("checked") or [])
    try:
        if fetch:
            reps, notes = fetch(cfg, cfg["window_start"], today)
        else:
            reps, notes = fetch_filings(cfg, cfg["window_start"], today, known=seen | checked)
    except Exception as exc:  # noqa: BLE001
        errs.append(f"KIND, 네이버 공시 피드 모두 조회 실패: {str(exc)[:120]}")
        return []
    for n in notes:
        print("note:", n)
    nav_keys = set(st.get("naver_keys") or [])
    out, sent = [], st.setdefault("sent", [])
    seeding = not st.get("seed_v2")
    jan_lo, jan_hi = cfg.get("jan_window", ["2027-01-15", "2027-02-27"])
    for r in reps:
        key = f"{r['filed'][:10]}|{norm_title(r['title'])}"
        if r["id"] in seen or r["id"] in checked or key in nav_keys:
            continue
        kind = resolve_kind(r)
        if not kind:
            if body_candidate(r["title"]) and (r.get("body") or "").strip():
                checked.add(r["id"])  # 본문을 읽었고 주주환원 아님: 다시 안 읽음
            continue
        if seeding and kind in NEW_KINDS:
            print(f"seed (no send) {r['id']} {r['filed']} {r['title']!r} kind={kind}")
            seen.add(r["id"])
            if not r.get("acptno"):
                nav_keys.add(key)
            continue
        p = parse(kind, r.get("body") or "")
        in_jan = jan_lo <= r["filed"][:10] <= jan_hi
        jan = in_jan and not st.get("jan_first")
        print(f"filing {r['id']} {r['filed']} {r['title']!r} kind={kind} jan={jan} parsed={p}")
        t, m, pri = message(cfg, r, kind, p, stamp, jan, in_jan)
        out.append((t, m, pri))
        if jan:
            st["jan_first"] = r["id"]
        seen.add(r["id"])
        if not r.get("acptno"):
            nav_keys.add(key)
        sent.append({"id": r["id"], "filed": r["filed"], "title": r["title"], "kind": kind, "at": stamp, "priority": pri})
    if seeding:
        st["seed_v2"] = stamp
    st["seen"], st["naver_keys"], st["checked"] = sorted(seen), sorted(nav_keys), sorted(checked)
    if not out:
        print("새 해당 공시 없음")
    return out


def run(mode: str, now: datetime, cfg: dict, pub=publish) -> int:
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    topic, server = resolve_topic("port", cfg.get("ntfy_topic")), resolve_server(cfg.get("ntfy_server"))
    print(f"{stamp} {NAME} mode={mode}")
    if mode == "install":
        t, m = install_message(stamp)
        check_text(t, m)
        print(f"{t}\n{m}")
        pub(topic, t, m, 3, ["white_check_mark"], server, config_name=NAME)
        print("install confirmation sent (state untouched)")
        return 0
    st = load_state()
    errs: list[str] = []
    out = step(cfg, st, now, stamp, errs)
    ops, today = None, now.strftime("%Y-%m-%d")
    if errs and st.get("ops_day") != today:
        ops = ops_msg(stamp, " / ".join(errs) + ". state 는 그대로.")
        st["ops_day"] = today
    for t, m, pri in out:
        check_text(t, m)
        print(f"TITLE: {t}\nPRIORITY: {pri} TOPIC: {route_topic(topic, t)}\n{m}\n")
    if ops:
        print(f"OPS TITLE: {ops[0]} -> {route_topic(topic, ops[0])}\n{ops[1]}\n")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    for t, m, pri in out:
        pub(topic, t, m, pri, ["moneybag"], server, config_name=NAME)
    if ops:
        try:
            pub(resolve_topic("ops"), ops[0], ops[1], 3, ["warning"], server, config_name=NAME)
        except Exception as exc:  # noqa: BLE001
            print(f"ops notice failed: {exc}")
    st["updated_at"] = stamp
    save_state(st)
    return 0


SAMPLE_BUY = ("자기주식 취득 결정 1. 취득예정주식(주) 보통주식 50,144,628 기타주식 6,912,036 2. 취득예정금액(원) 보통주식 2,682,737,598,000 "
              "기타주식 317,262,452,400 3. 취득예상기간 시작일 2024년 11월 18일 종료일 2025년 02월 17일 4. 보유예상기간 시작일 - 종료일 - "
              "5. 취득목적 주주가치 제고 등 6. 취득방법 유가증권시장을 통한 장내 매수")
SAMPLE_BURN = ("주식 소각 결정 1. 소각할 주식의 종류와 수 보통주식 (주) 73,359,314 종류주식 (주) 13,603,461 2. 발행주식 총수 보통주식 (주) 5,919,637,922 "
               "4. 소각예정금액(원) 5,345,538,035,503 7. 소각 예정일 2026-04-02 ※ 이사회 당일(3/30) 종가인 보통주 주당 176,300원, 우선주 주당 121,100원을 "
               "기준으로 산출한 금액은 14,580,626,185,300원임")


SAMPLE_DIV = ("현금ㆍ현물배당 결정 1. 배당구분 분기배당 2. 배당종류 현금배당 - 현물자산의 상세내역 - 3. 1주당 배당금(원) 보통주식 4,601 종류주식 4,601 "
              "- 차등배당 여부 미해당 4. 시가배당률(%) 보통주식 1.7 종류주식 2.2 5. 배당금총액(원) 30,003,024,457,217 6. 배당기준일 2026-09-30 "
              "【종류주식에 대한 배당 관련 사항】 종류주식명 종류주식구분 1주당 배당금(원) 시가배당률(%) 배당금총액(원) 삼성전자우 우선주 4,601 2.2 3,691,709,905,003")
SAMPLE_VUP = ("기업가치 제고 계획(자율공시) 1. 주주환원 정책 2027년부터 3년간 잉여현금흐름의 50%를 환원. 자기주식 매입 10조원 규모를 추진. "
              "정규 배당 연 9.8조원 유지. 우선주 매입 비중은 수량 기준 12% 수준.")


def selftest() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    assert kind_of("자기주식 취득 결정") == "buy" and kind_of("[정정] 자기주식 취득 결정") == "buy"
    assert kind_of("주식 소각 결정") == "burn" and kind_of("자기주식취득 신탁계약 체결 결정") == "trust"
    for bad in ("자기주식 처분 결정", "자기주식취득결과보고서", "변경상장(주식소각)", "자기주식처분결과보고서", "자기주식취득 신탁계약 해지 결정"):
        assert kind_of(bad) is None, bad
    p = parse("buy", "정정 표 ... " + SAMPLE_BUY)
    assert p["cq"] == 50144628 and p["pq"] == 6912036 and p["pa"] == 317262452400, p
    line = detail_line("buy", p)
    assert "우선주 비중 수량 기준 12.1%, 금액 기준 10.6%" in line, line
    b = parse("burn", SAMPLE_BURN)
    assert b["pq"] == 13603461 and abs(b["ca"] + b["pa"] - 14580626185300) < 1, b
    p0 = parse("buy", SAMPLE_BUY.replace("기타주식 6,912,036", "기타주식 -").replace("기타주식 317,262,452,400", "기타주식 -"))
    assert p0["pq"] is None and "우선주 비중 수량 기준 0.0%" in detail_line("buy", p0)
    rep = {"id": "kind:20261130000001", "acptno": "20261130000001", "title": "자기주식 취득 결정", "filed": "2026-11-30 07:40", "src": "KIND", "body": SAMPLE_BUY}
    corr = {**rep, "id": "kind:20261130000002", "acptno": "20261130000002", "title": "[정정] 자기주식 취득 결정"}
    noise = {**rep, "id": "kind:20261130000003", "acptno": "20261130000003", "title": "자기주식 처분 결정"}
    now = datetime(2026, 11, 30, 9, 0, tzinfo=KST)
    st: dict = {}
    out = step(cfg, st, now, "T", [], fetch=lambda c, a, b: ([rep, noise], []))
    assert len(out) == 1 and "우선주 포함" in out[0][0] and out[0][2] == 4, out
    assert step(cfg, st, now, "T", [], fetch=lambda c, a, b: ([rep], [])) == []
    out2 = step(cfg, st, now, "T", [], fetch=lambda c, a, b: ([rep, corr], []))
    assert len(out2) == 1 and "정정" in out2[0][0], out2
    nav = {**rep, "id": "naver:1", "acptno": None, "title": "주식 소각 결정", "filed": "2026-12-01", "src": "네이버 KRX 공시 피드", "body": SAMPLE_BURN}
    out3 = step(cfg, st, now.replace(month=12, day=1), "T", [], fetch=lambda c, a, b: ([nav], []))
    assert len(out3) == 1 and "네이버 공시 목록" in out3[0][1]
    kd = {**nav, "id": "kind:20261201000009", "acptno": "20261201000009", "filed": "2026-12-01 07:38", "src": "KIND"}
    assert step(cfg, st, now.replace(month=12, day=1), "T", [], fetch=lambda c, a, b: ([kd], [])) == []
    nob = step(cfg, {}, now, "T", [], fetch=lambda c, a, b: ([{**rep, "body": ""}], []))
    assert "직접 확인" in nob[0][1]
    assert step(cfg, {}, datetime(2027, 2, 28, 9, 0, tzinfo=KST), "T", [], fetch=lambda c, a, b: ([rep], [])) == []
    for t, m, _ in out + out2 + out3 + nob:
        check_text(t, m)
    # ---- 2026-10-30 추가 종류 ----
    for t in ("현금ㆍ현물배당결정", "현금ㆍ현물 배당 결정", "현금·현물배당 결정", "현금배당결정", "[정정] 현금ㆍ현물배당결정"):
        assert kind_of(t) == "div", t
    assert kind_of("기업가치 제고 계획(자율공시)") == "vup" and kind_of("[기재정정]기업가치제고계획(자율공시)") == "vup"
    assert kind_of("주주환원 정책 안내(자율공시)") == "ret" and kind_of("주주환원 관련(공정공시)") == "ret"
    assert kind_of("주주환원 정책 안내") is None and kind_of("중간(분기)배당락 기준가격 안내") is None
    assert body_candidate("기타 경영사항(자율공시)") and not body_candidate("자기주식 처분 결정")
    assert show_title("현금ㆍ현물 배당 결정") == "현금, 현물 배당 결정"
    d = parse("div", SAMPLE_DIV)
    assert d["dps_c"] == 4601 and d["dps_p"] == 4601 and d["total"] == 30003024457217 and d["total_p"] == 3691709905003, d
    items = return_items("div", d)
    assert "보통주 4,601원, 우선주 4,601원" in items and "30.00조원" in items and "자사주 매입 규모 이 공시에 없음" in items, items
    v = parse("vup", SAMPLE_VUP)
    vi = return_items("vup", v)
    assert "10조원" in vi and "우선주 매입 비중" in vi, vi
    assert "자동으로 못 읽음" in return_items("vup", None)
    cfg2 = dict(cfg, window_start="2026-10-29")
    div_old = {"id": "kind:20261029000101", "acptno": "20261029000101", "title": "현금ㆍ현물 배당 결정", "filed": "2026-10-29 09:53", "src": "KIND", "body": SAMPLE_DIV}
    buy_new = {**rep, "id": "kind:20261030000777", "acptno": "20261030000777", "filed": "2026-10-30 17:00"}
    st2: dict = {"seen": [], "sent": []}
    now2 = datetime(2026, 10, 30, 19, 0, tzinfo=KST)
    o = step(cfg2, st2, now2, "T", [], fetch=lambda c, a, b: ([div_old, buy_new], []))
    assert [x[0] for x in o] == [o[0][0]] and "자기주식 취득 결정" in o[0][0], o  # 배당은 seed, 자사주는 정상 발송
    assert "kind:20261029000101" in st2["seen"] and st2.get("seed_v2"), st2
    assert step(cfg2, st2, now2, "T", [], fetch=lambda c, a, b: ([div_old, buy_new], [])) == []
    div_nov = {**div_old, "id": "kind:20261127000001", "acptno": "20261127000001", "filed": "2026-11-27 16:00"}
    o = step(cfg2, st2, datetime(2026, 11, 27, 19, 0, tzinfo=KST), "T", [], fetch=lambda c, a, b: ([div_nov], []))
    assert len(o) == 1 and o[0][2] == 4 and "현금, 현물 배당 결정" in o[0][0] and JAN_TAG not in o[0][0], o
    jan1 = {**div_old, "id": "kind:20270128000101", "acptno": "20270128000101", "title": "현금ㆍ현물배당결정", "filed": "2027-01-28 07:40"}
    jan2 = {**rep, "id": "kind:20270128000102", "acptno": "20270128000102", "filed": "2027-01-28 07:42"}
    jnow = datetime(2027, 1, 28, 9, 0, tzinfo=KST)
    o = step(cfg2, st2, jnow, "T", [], fetch=lambda c, a, b: ([jan1, jan2], []))
    assert len(o) == 2 and o[0][2] == 5 and o[0][0].startswith(f"{TAG} {JAN_TAG} | ") and o[1][2] == 4 and JAN_TAG not in o[1][0], o
    assert "주당 배당금 보통주 4,601원, 우선주 4,601원" in o[0][1] and "주주환원 항목" in o[1][1], o[1][1]
    jan3 = {**jan1, "id": "kind:20270129000001", "acptno": "20270129000001", "filed": "2027-01-29 07:40"}
    o = step(cfg2, st2, jnow, "T", [], fetch=lambda c, a, b: ([jan3], []))
    assert len(o) == 1 and o[0][2] == 4, o
    # 본문 확인형 자율공시: 주주환원 있으면 ret, 없으면 checked 로 다시 안 읽음
    stc: dict = {"seed_v2": "x"}
    ret_b = {"id": "kind:20261105000001", "acptno": "20261105000001", "title": "기타 경영사항(자율공시)", "filed": "2026-11-05 17:00", "src": "KIND", "kind": None, "body": SAMPLE_VUP}
    non_b = {**ret_b, "id": "kind:20261105000002", "acptno": "20261105000002", "body": "1. 제목 기부금 출연 2. 주요내용 출연 금액 12.6억원"}
    o = step(cfg2, stc, datetime(2026, 11, 5, 19, 0, tzinfo=KST), "T", [], fetch=lambda c, a, b: ([ret_b, non_b], []))
    assert len(o) == 1 and "주주환원 관련 공시" in o[0][0] and "kind:20261105000002" in stc["checked"], (o, stc)
    for t, m, _ in o:
        check_text(t, m)
    for kname in ("div", "vup", "ret"):
        t, m, _ = message(cfg, {**rep, "title": "현금ㆍ현물배당결정"}, kname, None, "T", True)
        check_text(t, m)
    assert not re.search("[\u00b7\u318d\u2014]", CFG_PATH.read_text(encoding="utf-8"))
    print("selftest new kinds ok")
    print("selftest ok")
    return 0


def main() -> int:
    args = sys.argv[1:]
    mode = (args[0] if args and not args[0].startswith("--") else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry", "install"}:
        print(f"usage: {NAME}_monitor.py <auto|dry|install|selftest> [--as-of 'YYYY-MM-DD HH:MM']")
        return 2
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    if "--as-of" in args:
        if mode != "dry":
            print("--as-of 는 dry 에서만")
            return 2
        now = datetime.strptime(args[args.index("--as-of") + 1], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    try:
        return run(mode, now, cfg)
    except Exception as exc:  # noqa: BLE001
        print(traceback.format_exc(), file=sys.stderr)
        if mode != "auto":
            return 1
        try:
            t, m = ops_msg(now.strftime("%Y-%m-%d %H:%M KST"), f"스크립트 오류 {type(exc).__name__}: {str(exc)[:200]}. state 는 그대로.")
            publish(resolve_topic("ops"), t, m, 3, ["warning"], resolve_server(cfg.get("ntfy_server")), config_name=NAME)
        except Exception:  # noqa: BLE001
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
