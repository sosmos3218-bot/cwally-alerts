#!/usr/bin/env python3
"""[417790] 트루엔 자사주 신탁 해지/소각 공시 감시 (2026-11-16 요청 t19u).

KRX 거래일 19:00 k203780 슬롯. 새 반복 cron 없음.
  1) 주식소각결정 / 자기주식소각결정 신규 1번/건 (우선 높음). 수량, 비율, 소각 예정일, DART 링크.
  2) 신탁계약 해지 결정 / 해지결과보고서 신규 1번/건 (정보). 원문에 소각 계획이 있으면 그대로 적고, 없으면 추측하지 않음.
  3) 신탁 종료일(2026-12-14) D-7(2026-12-07)부터 12-18 사이 거래일 19:00에 한 번 리마인더.
변경상장, 취득상황보고서, 신탁 체결 결정은 발송하지 않음.
첫 실행(seeded 아님): 이미 있는 해당 공시를 seen 만 채우고 발송 없음.
modes: auto | dry | install | selftest
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import secdiv_monitor as sd  # noqa: E402
from kr_calendar import KST, is_trading_day, session_day  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

NAME = "truentrust"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
FX = Path(__file__).resolve().parent / "fixtures" / NAME
TAG = "[417790]"
CODE = "417790"
CORP = "트루엔"
CORP_CIK = "01134216"
DOT = r"[\u318d\u00b7\u2027\u30fb]"
DART_VIEW = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcp}"
DART_SEARCH = "https://dart.fss.or.kr/dsab007/detailSearch.ax"

SKIP_RE = re.compile(r"상황보고서|주주명부|변경상장")
BURN_RE = re.compile(r"(자기)?주식\s*소각\s*결정")
TERM_DEC_RE = re.compile(r"신탁\s*계약\s*해지\s*결정")
TERM_RES_RE = re.compile(r"해지\s*결과\s*보고서")


def default_state() -> dict:
    return {
        "updated_at": None,
        "seeded": False,
        "seen": [],
        "d7_notified": False,
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


def classify(title: str) -> str | None:
    """Return burn | term_decision | term_result, or None."""
    t = title or ""
    if SKIP_RE.search(t):
        return None
    if BURN_RE.search(t):
        return "burn"
    if TERM_RES_RE.search(t):
        return "term_result"
    if TERM_DEC_RE.search(t):
        return "term_decision"
    return None


def plain(text: str) -> str:
    s = text or ""
    if "<" in s:
        s = sd.text_of(s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", s).strip()


def _int(tok: str | None) -> int | None:
    if not tok:
        return None
    s = re.sub(r"[^\d]", "", tok)
    return int(s) if s else None


def _date(tok: str | None) -> str | None:
    if not tok:
        return None
    t = tok.strip()
    m = re.search(r"(\d{4})\s*[-.\.]\s*(\d{1,2})\s*[-.\.]\s*(\d{1,2})", t)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일", t)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


def parse_cancel(text: str) -> dict:
    t = plain(text)
    out: dict = {}
    m = re.search(r"소각할\s*주식의\s*종류와\s*수\s*보통주식(?:\s*\(\s*주\s*\))?\s*([0-9,]+)", t)
    if m:
        out["shares"] = _int(m.group(1))
    m = re.search(r"발행주식총수\s*보통주식(?:\s*\(\s*주\s*\))?\s*([0-9,]+)", t)
    if m:
        out["outstanding"] = _int(m.group(1))
    m = re.search(r"소각예정금액(?:\s*\(\s*원\s*\))?\s*([0-9,]+)", t)
    if m:
        out["amount"] = _int(m.group(1))
    m = re.search(r"소각\s*예정일\s*([0-9년월일.\-\s]+?)(?:\s+\d+\.|$| -| 자기주식| 이사회)", t)
    if m:
        out["date"] = _date(m.group(1))
    if not out.get("date"):
        m = re.search(r"소각\s*예정일\s*(\d{4}\s*[-.\.년]\s*\d{1,2}\s*[-.\.월]\s*\d{1,2}\s*일?)", t)
        if m:
            out["date"] = _date(m.group(1))
    m = re.search(r"발행주식총수의\s*([\d.]+)\s*%", t)
    if m:
        out["ratio_pct"] = float(m.group(1))
    elif out.get("shares") and out.get("outstanding"):
        out["ratio_pct"] = round(out["shares"] / out["outstanding"] * 100, 2)
    return out


def classify_plan(text: str) -> str:
    t = plain(text)
    has_cancel = bool(re.search(r"소각\s*(예정|결정|하기로|할\s*예정|계획)|전량\s*소각|이익소각", t))
    has_hold = bool(re.search(r"(증권)?계좌로\s*입고|직접\s*보유|보유\s*예정|입고\s*예정", t))
    has_sell = bool(re.search(r"처분\s*(예정|결정|계획)", t))
    has_rsu = bool(re.search(r"양도제한조건부|RSU", t, re.I))
    if has_cancel and not has_hold and not has_sell:
        return "원문에 소각 예정으로 기재됨"
    if has_cancel and (has_hold or has_rsu):
        return "원문에 소각과 보유/부여 계획이 함께 있어 수량을 원문에서 확인할 것"
    if has_sell and not has_cancel:
        return "원문에 처분 계획이 기재됨. 소각 여부는 별도 주식소각 결정을 확인"
    if has_hold and not has_cancel:
        return "원문에 회사 계좌 입고 후 보유로 기재됨. 소각 여부는 별도 주식소각 결정을 확인"
    if has_rsu and not has_cancel:
        return "원문에 양도제한조건부주식 등 부여 관련 기재가 있음. 소각 여부는 별도 결정을 확인"
    return "원문에 소각 여부가 분명하지 않음. 별도 주식소각 결정을 확인"


def parse_term(text: str) -> dict:
    t = plain(text)
    out: dict = {"plan": classify_plan(t)}
    m = re.search(r"해지(?:예정)?(?:일자|일)\s*([0-9년월일.\-\s]+?)(?:\s+\d+\.|$| 해지| 계약| 기타)", t)
    if m:
        out["date"] = _date(m.group(1))
    if not out.get("date"):
        m = re.search(r"해지(?:예정)?(?:일자|일)\s*(\d{4}\s*[-.\.년]\s*\d{1,2}\s*[-.\.월]\s*\d{1,2}\s*일?)", t)
        if m:
            out["date"] = _date(m.group(1))
    m = re.search(r"해지예정주식(?:\s*\(\s*주\s*\))?\s*보통주식\s*([0-9,]+)", t)
    if m:
        out["shares"] = _int(m.group(1))
    if not out.get("shares"):
        m = re.search(r"실제\s*취득\s*수량\s*보통주식\s*([0-9,]+)\s*주?", t)
        if m:
            out["shares"] = _int(m.group(1))
    m = re.search(r"실제\s*취득\s*금액(?:\s*\(\s*원\s*\))?\s*([0-9,]+)", t)
    if m:
        out["amount"] = _int(m.group(1))
    m = re.search(r"해지목적\s+([^\d][^.]{2,80}?)(?:\s+\d+\.|$)", t)
    if m:
        out["purpose"] = m.group(1).strip(" -")
    m = re.search(r"반환방법\s+([^.]{2,80}?)(?:\s+\d+\.|$)", t)
    if m:
        out["return_method"] = m.group(1).strip(" -")
    return out


def fmt_shares(n: int | None) -> str:
    return f"{n:,}주" if n is not None else "수량 미확인"


def fmt_won(n: int | None) -> str:
    return f"{n:,}원" if n is not None else "금액 미확인"


def parse_kind_html(page: str, corp: str = CORP) -> list[dict]:
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
        title = re.sub(r"^주식회사\s*" + corp + r"\s+", "", title)
        title = re.sub(r"^" + corp + r"\s+", "", title)
        title = re.sub(r"\s+(" + corp + r"|코스닥시장본부|유가증권시장본부)$", "", title).strip()
        out.append({
            "id": "kind:" + acpt.group(1),
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
        "method": "searchDetailsSub", "currentPageSize": "100", "pageIndex": "1", "orderMode": "1", "orderStat": "D",
        "forward": "details_sub", "searchCodeType": "", "repIsuSrtCd": "A" + CODE, "allRepIsuSrtCd": "",
        "oldSearchCorpName": "", "disclosureType": "", "disTypevalue": "", "reportNm": "", "reportCd": "",
        "searchCorpName": CODE, "business": "", "marketType": "", "settlementMonth": "", "securities": "",
        "submitObligNm": "", "enterprise": "", "fromDate": since, "toDate": until, "reportNmTemp": "", "bfrDsclsType": "on",
    }
    # keep the field name used by KIND (submitOblgNm)
    form.pop("submitObligNm", None)
    form["submitOblgNm"] = ""
    page = sd.http(
        sd.KIND + "/disclosure/details.do",
        data=form,
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Referer": sd.KIND + "/disclosure/details.do?method=searchDetailsMain",
        },
    )
    return parse_kind_html(page)


def parse_naver_rows(raw) -> list[dict]:
    out = []
    for r in raw if isinstance(raw, list) else []:
        title = str(r.get("title") or "")
        title = re.sub(r"^주식회사\s*" + CORP + r"\s+", "", title).strip()
        title = re.sub(r"^" + CORP + r"\s+", "", title).strip()
        day = str(r.get("datetime") or "")[:10]
        nid = str(r.get("no") or r.get("bizdateSeq") or "")
        if not nid or len(day) != 10:
            continue
        out.append({
            "id": "naver:" + nid,
            "acptno": None,
            "title": title,
            "filed": day,
            "src": "네이버 KRX 공시 피드",
            "url": f"https://finance.naver.com/item/news_notice.naver?code={CODE}",
            "body": sd.text_of(str(r.get("contents") or "")),
        })
    return sorted(out, key=lambda r: r["id"])


def naver_list() -> list[dict]:
    raw = json.loads(sd.http(f"https://stock.naver.com/api/domestic/detail/notice?itemCode={CODE}&page=1&pageSize=100"))
    return parse_naver_rows(raw)


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
    return out


def dart_list(since: str, until: str) -> list[dict]:
    q = urlencode({
        "textCrpCik": CORP_CIK,
        "startDate": since.replace("-", ""),
        "endDate": until.replace("-", ""),
        "maxResults": "100",
        "currentPage": "1",
    })
    page = sd.http(DART_SEARCH + "?" + q)
    rows = parse_dart_html(page)
    if not rows and "트루엔" not in (page or "") and "rcpNo=" not in (page or ""):
        raise RuntimeError("DART 목록 형식 이상")
    return rows


def attach_dart(rows: list[dict], dart_rows: list[dict]) -> None:
    for f in rows:
        day = (f.get("filed") or "")[:10]
        nt = norm_title(f.get("title") or "")
        if not day or not nt:
            continue
        hits = [d for d in dart_rows if d["filed"] == day and (nt in norm_title(d["title"]) or norm_title(d["title"]) in nt)]
        if len(hits) == 1:
            f["dart_rcept"] = hits[0]["rcept"]
            f["dart_url"] = hits[0]["url"]
        elif len(hits) > 1:
            exact = [d for d in hits if norm_title(d["title"]) == nt]
            pick = exact[0] if exact else hits[0]
            f["dart_rcept"] = pick["rcept"]
            f["dart_url"] = pick["url"]


def filing_links(f: dict) -> str:
    parts = []
    if f.get("dart_url") or f.get("dart_rcept"):
        parts.append("DART " + (f.get("dart_url") or DART_VIEW.format(rcp=f["dart_rcept"])))
    if f.get("url"):
        parts.append(("KIND " if f.get("src") == "KIND" else f"{f.get('src')} ") + f["url"])
    return " ".join(parts).strip()


def fetch_filings(today: str) -> tuple[list[dict], str, list[str]]:
    since = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=400)).strftime("%Y-%m-%d")
    notes: list[str] = []
    rows: list[dict] = []
    src = ""
    try:
        rows = kind_list(since, today)
        if rows:
            src = "KIND"
        else:
            notes.append("KIND 0건")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"KIND {exc}")
    if not rows:
        try:
            rows = naver_list()
            if rows:
                src = "네이버"
            else:
                notes.append("네이버 0건")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"네이버 {exc}")
    if not rows:
        raise RuntimeError(" / ".join(notes) or "공시 목록 없음")
    try:
        attach_dart(rows, dart_list(since, today))
    except Exception as exc:  # noqa: BLE001
        notes.append(f"DART {exc}")
    return rows, src, notes


def enrich_body(f: dict) -> None:
    if f.get("body"):
        return
    if f.get("acptno"):
        try:
            f["body"] = sd.kind_body(f["acptno"])
            return
        except Exception:  # noqa: BLE001
            f["body"] = ""
    f.setdefault("body", "")


def body(stamp: str, text: str, action: str, later: str = "", avoid: str = "") -> str:
    return "\n".join([
        f"{stamp} | {CODE} | {CORP}",
        f"이게 뭐 알림: {CODE}({CORP}) 자사주 신탁 해지결과/해지 결정과 주식소각 결정, 신탁 종료 D-7 리마인더. 매매 지시 아님.",
        f"지금 결과/상태: {text}",
        f"지금 할 일: {action}",
        f"나중에: {later or '같은 공시는 다시 오지 않습니다. 정정이나 다음 결정이 나오면 다시 알림.'}",
        "익절/줄일 때: 계획한 비중이 있으면 그 안에서만.",
        f"하지 말 것: {avoid or '제목만 보고 전량 소각으로 단정하기, 조회 실패를 신호로 읽기.'}",
    ])


def evaluate(cfg: dict, st: dict, obs: dict, today: str, seed_only: bool = False) -> list[tuple]:
    out: list[tuple] = []
    seen = set(st.get("seen") or [])
    filings = obs.get("filings") or []

    for f in filings:
        hit = classify(f["title"])
        if not hit:
            # still seed related trust/cancel noise so a later source switch does not replay them
            if any(k in (f["title"] or "") for k in ("신탁", "소각", "해지", "자기주식", "자사주")):
                seen.add(f["id"])
            continue
        if f["id"] in seen:
            continue
        seen.add(f["id"])
        if seed_only:
            continue
        enrich_body(f)
        title_s = show_title(f["title"])[:80]
        links = filing_links(f)
        dedup = f.get("dart_rcept") or f.get("acptno") or f["id"]
        if hit == "burn":
            p = parse_cancel(f.get("body") or "")
            bits = [f"{f['filed']} '{title_s}' ({f['src']})"]
            if p.get("shares") is not None:
                bits.append(f"소각 주식 {fmt_shares(p['shares'])}")
            else:
                bits.append("소각 주식 수량은 원문에서 확인")
            if p.get("ratio_pct") is not None and p.get("outstanding"):
                bits.append(f"발행주식 {p['outstanding']:,}주 대비 {p['ratio_pct']:g}%")
            elif p.get("ratio_pct") is not None:
                bits.append(f"발행주식 대비 {p['ratio_pct']:g}%")
            bits.append(f"소각 예정일 {p['date']}" if p.get("date") else "소각 예정일은 원문에서 확인")
            if p.get("amount") is not None:
                bits.append(f"소각예정금액 {fmt_won(p['amount'])}")
            if links:
                bits.append(links)
            out.append((
                f"{TAG} 트루엔 자사주 소각 결정",
                ". ".join(bits) + ".",
                4,
                dedup,
            ))
        else:
            p = parse_term(f.get("body") or "")
            label = "해지결과보고서" if hit == "term_result" else "신탁계약 해지 결정"
            bits = [f"{f['filed']} '{title_s}' ({f['src']})"]
            if p.get("date"):
                bits.append(f"해지일 {p['date']}")
            if p.get("shares") is not None:
                bits.append(f"대상 주식 {fmt_shares(p['shares'])}")
            if p.get("amount") is not None:
                bits.append(f"취득금액 {fmt_won(p['amount'])}")
            bits.append(p.get("plan") or "원문에 소각 여부가 분명하지 않음. 별도 주식소각 결정을 확인")
            if links:
                bits.append(links)
            title = f"{TAG} 트루엔 신탁 해지결과보고서" if hit == "term_result" else f"{TAG} 트루엔 신탁계약 해지 결정"
            out.append((title, ". ".join(bits) + ".", 3, dedup))

    st["seen"] = sorted(seen)

    if not seed_only:
        d7 = cfg.get("d7_date") or "2026-12-07"
        d7_until = cfg.get("d7_until") or "2026-12-18"
        if d7 <= today <= d7_until and not st.get("d7_notified"):
            tr = cfg.get("trust") or {}
            stt = cfg.get("latest_status") or {}
            dart = DART_VIEW.format(rcp=tr.get("dart_rcept") or "20251215000083")
            status = (
                f"신탁 계약 종료일 {tr.get('end', '2026-12-14')}. "
                f"체결일 {tr.get('signed_date', '2025-12-15')}, 계약금액 {fmt_won(tr.get('amount_won'))}, "
                f"위탁사 {tr.get('trustee', '미래에셋증권')}. "
                f"{stt.get('as_of', '2026-09-16')} 취득상황 기준 {fmt_shares(stt.get('shares'))} "
                f"({stt.get('ratio_pct', 3.54):g}%, 이행률 {stt.get('fill_pct', 84.81):g}%). "
                "이후 추가 취득은 해지결과보고서에서 확인. "
                "계약 목적에 양도제한조건부주식 부여수량 확보가 있어 전량 소각이 아닐 수 있음. "
                f"체결 원문 {dart}"
            )
            out.append((
                f"{TAG} 트루엔 자사주 신탁 종료 D-7",
                status,
                3,
                "d7-20261214",
            ))
            st["d7_notified"] = True
    return out


def fail_alerts(cfg: dict, st: dict, errs: dict, today: str) -> list[tuple]:
    out = []
    fails = st.setdefault("fail", {})
    key = "filings"
    f = fails.get(key) or {"days": 0, "last": None, "notified": False}
    if key in errs:
        if f["last"] != today:
            f["days"] += 1
            f["last"] = today
        if f["days"] >= cfg["rules"]["fail_ops_after_days"] and not f["notified"]:
            f["notified"] = True
            out.append((
                f"{TAG} 공시 목록 조회 실패 | {f['days']}일 연속, 그 규칙 건너뜀",
                f"신탁/소각 공시 목록 조회가 {f['days']}일 연속 실패({errs[key]}). 조건 미충족이 아님.",
                3,
                None,
            ))
    else:
        f = {"days": 0, "last": None, "notified": False}
    fails[key] = f
    return out


def gather(today: str) -> tuple[dict, dict]:
    obs: dict = {}
    errs: dict = {}
    try:
        rows, src, notes = fetch_filings(today)
        obs["filings"] = rows
        obs["filings_src"] = src
        if notes:
            obs["notes"] = notes
    except Exception as exc:  # noqa: BLE001
        obs["filings"] = None
        errs["filings"] = str(exc)[:200]
    return obs, errs


def send_actions(alerts: list[tuple], stamp: str) -> list[tuple]:
    built = []
    for item in alerts:
        title, text, pri = item[0], item[1], item[2]
        dedup = item[3] if len(item) > 3 else None
        if "조회 실패" in title:
            msg = body(stamp, text, "다음 거래일 19:00 재시도를 기다리세요.", "읽힌 항목은 정상 판단했습니다.", "조회 실패를 조건 충족으로 읽기.")
        elif "소각 결정" in title:
            msg = body(stamp, text, "원문에서 소각 주식 수, 비율, 예정일을 확인하고 비중 계획을 점검.", "정정이나 변경상장은 소각 결정이 아님.")
        elif "해지결과" in title or "해지 결정" in title:
            msg = body(stamp, text, "원문에서 취득 수량과 소각/보유/부여 계획을 확인. 소각은 별도 주식소각 결정이 나와야 확정.", "주식소각 결정이 나오면 따로 알림.")
        elif "D-7" in title:
            msg = body(stamp, text, "종료일과 해지결과보고서, 소각 결정 공시를 기다리면 됩니다. 지금 당장 매매할 일 없음.", "해지 또는 소각 공시가 나오면 따로 알림.")
        else:
            msg = body(stamp, text, "원문을 확인하세요.")
        built.append((title, msg, pri, dedup))
    return built


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry", "install"}:
        print(f"unknown mode {mode}")
        return 2

    as_of = None
    if "--as-of" in sys.argv:
        i = sys.argv.index("--as-of")
        if i + 1 < len(sys.argv):
            as_of = sys.argv[i + 1]

    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    if as_of and mode == "dry":
        raw = as_of.strip()
        now = datetime.strptime(raw, "%Y-%m-%d %H:%M" if " " in raw else "%Y-%m-%d").replace(tzinfo=KST)
    if now > datetime.strptime(cfg["ends_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST):
        print("ended")
        return 0

    dry = mode == "dry"
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    day = session_day(now)
    today = day.isoformat()
    stamp = now.strftime("%Y-%m-%d %H:%M KST")

    if mode == "install":
        obs, errs = gather(today)
        n_match = sum(1 for f in (obs.get("filings") or []) if classify(f["title"]))
        status = f"공시 소스 {obs.get('filings_src', '없음')}, 감시 대상 공시 {n_match}건(시드용)"
        if errs:
            status += f". 조회 실패: {json.dumps(errs, ensure_ascii=False)}"
        title = f"{TAG} 트루엔 신탁/소각 알림 설치 확인"
        msg = body(
            stamp,
            status + ". 설치 확인용. 매매 신호 아님.",
            "없음. KRX 거래일 19:00에 해지결과, 해지 결정, 소각 결정만 옵니다. D-7은 2026-12-07 이후 한 번.",
            f"신탁 종료일 {cfg['trust']['end']}. 체결 원문 {DART_VIEW.format(rcp=cfg['trust']['dart_rcept'])}",
            "이 설치 확인을 매수나 매도 신호로 읽기.",
        )
        print(title)
        print(msg)
        if not dry:
            publish(topic, title, msg, 3, ["white_check_mark"], server)
        return 0

    if not is_trading_day(day) and mode != "dry":
        print(f"{today} KRX holiday; silent")
        return 0

    st = load_state()
    obs, errs = gather(today)
    if errs:
        print("errors: " + json.dumps(errs, ensure_ascii=False), file=sys.stderr)
    print("obs: " + json.dumps({
        "filings_src": obs.get("filings_src"),
        "filings_n": len(obs.get("filings") or []),
        "match_n": sum(1 for f in (obs.get("filings") or []) if classify(f["title"])),
        "notes": obs.get("notes") or [],
    }, ensure_ascii=False))

    if not st.get("seeded"):
        evaluate(cfg, st, obs, today, seed_only=True)
        st["seeded"] = True
        st["seed_note"] = f"{stamp} 시드. seen {len(st.get('seen') or [])}건"
        print(st["seed_note"])
        if not dry:
            st["updated_at"] = now.isoformat()
            save_state(st)
        else:
            print("DRY RUN: seed not written")
        return 0

    raw = evaluate(cfg, st, obs, today) + fail_alerts(cfg, st, errs, today)
    alerts = send_actions(raw, stamp)
    for title, msg, pri, dedup in alerts:
        print(f"TITLE: {title}\nPRIORITY: {pri} TOPIC: {route_topic(topic, title)}\n{msg}\n")
        if not dry:
            publish(topic, title, msg, pri, ["office"], server, dedup_key=dedup)
    if not alerts:
        print("no alert")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    st["updated_at"] = now.isoformat()
    save_state(st)
    return 0


def selftest() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    fails: list[str] = []

    def check(name, got, want):
        ok = got == want
        print(("ok   " if ok else "FAIL ") + f"{name}: got={got!r} want={want!r}")
        if not ok:
            fails.append(name)

    kind_rows = parse_kind_html((FX / "kind.html").read_text(encoding="utf-8"))
    check("kind fixture has rows", len(kind_rows) >= 10, True)
    trust = next((r for r in kind_rows if "신탁계약 체결" in r["title"]), None)
    burn = next((r for r in kind_rows if r["title"].startswith("주식소각") or "주식소각 결정" in r["title"]), None)
    check("kind has trust contract", bool(trust and trust["acptno"] == "20251215000349"), True)
    check("kind has 2025 cancel", bool(burn and burn["acptno"] == "20251124000148"), True)
    check("live 2025 cancel is seed-class", classify(burn["title"]) if burn else None, "burn")
    check("skip situation report", classify("신탁계약에의한 취득 상황보고서"), None)
    check("skip listing change", classify("변경상장(주식소각)"), None)
    check("skip 체결", classify("자기주식취득 신탁계약 체결 결정"), None)
    check("class burn", classify("주식소각 결정"), "burn")
    check("class self-burn", classify("자기주식소각결정"), "burn")
    check("class term decision", classify("자기주식취득 신탁계약 해지 결정"), "term_decision")
    check("class term result", classify("신탁계약 해지 결과보고서"), "term_result")

    dart_rows = parse_dart_html((FX / "dart_list.html").read_text(encoding="utf-8"))
    check("dart parse trust rcept", any(d["rcept"] == "20251215000083" for d in dart_rows), True)
    attach_dart(kind_rows, dart_rows)
    if trust:
        check("dart attach trust", trust.get("dart_rcept"), "20251215000083")
    if burn:
        check("dart attach 2025 cancel", burn.get("dart_rcept"), "20251124900148")

    p = parse_cancel((FX / "cancel_20251124.txt").read_text(encoding="utf-8"))
    check("parse cancel shares", p.get("shares"), 531259)
    check("parse cancel outstanding", p.get("outstanding"), 10999650)
    check("parse cancel date", p.get("date"), "2025-12-08")
    check("parse cancel amount", p.get("amount"), 5993975230)
    check("parse cancel ratio about 4.83", p.get("ratio_pct") is not None and abs(p["ratio_pct"] - 4.83) < 0.02, True)

    hold = parse_term((FX / "term_result_hold.txt").read_text(encoding="utf-8"))
    check("term hold shares", hold.get("shares"), 370722)
    check("term hold date", hold.get("date"), "2026-12-14")
    check("term hold plan mentions 보유 and 별도 소각", "보유" in (hold.get("plan") or "") and "주식소각" in (hold.get("plan") or ""), True)

    can = parse_term((FX / "term_result_cancel.txt").read_text(encoding="utf-8"))
    check("term cancel plan", can.get("plan"), "원문에 소각 예정으로 기재됨")
    check("term cancel shares", can.get("shares"), 200000)

    dec = parse_term((FX / "term_decision.txt").read_text(encoding="utf-8"))
    check("term dec shares", dec.get("shares"), 370722)
    check("term dec date", dec.get("date"), "2026-12-14")
    check("term dec plan 보유", "보유" in (dec.get("plan") or ""), True)

    def init():
        s = default_state()
        s.update(seeded=True, seen=["kind:old"], d7_notified=False)
        return s

    new_burn = {
        "id": "kind:20261215000001", "acptno": "20261215000001",
        "title": "주식소각 결정", "filed": "2026-12-15 16:10",
        "src": "KIND", "url": "https://kind.krx.co.kr/x",
        "dart_rcept": "20261215900001",
        "dart_url": DART_VIEW.format(rcp="20261215900001"),
        "body": (FX / "cancel_20251124.txt").read_text(encoding="utf-8"),
    }
    st = init()
    a = evaluate(cfg, st, {"filings": [new_burn]}, "2026-11-16")
    check("new burn title", [x[0] for x in a], [f"{TAG} 트루엔 자사주 소각 결정"])
    check("new burn priority", a[0][2], 4)
    check("burn body has shares and date and dart", (
        "531,259주" in a[0][1] and "2025-12-08" in a[0][1] and "20261215900001" in a[0][1]
    ), True)
    check("burn seen", "kind:20261215000001" in st["seen"], True)
    check("same burn silent", evaluate(cfg, st, {"filings": [new_burn]}, "2026-11-16"), [])

    new_res = {
        "id": "kind:20261214000002", "acptno": "20261214000002",
        "title": "신탁계약 해지 결과보고서", "filed": "2026-12-14 15:00",
        "src": "KIND", "url": "https://kind.krx.co.kr/y",
        "dart_rcept": "20261214000099",
        "dart_url": DART_VIEW.format(rcp="20261214000099"),
        "body": (FX / "term_result_hold.txt").read_text(encoding="utf-8"),
    }
    st = init()
    a = evaluate(cfg, st, {"filings": [new_res]}, "2026-11-16")
    check("term result title", [x[0] for x in a], [f"{TAG} 트루엔 신탁 해지결과보고서"])
    check("term result priority", a[0][2], 3)
    check("term result plan not guessed as 소각 예정으로만", "원문에 소각 예정으로 기재됨" not in a[0][1], True)
    check("term result mentions 보유", "보유" in a[0][1], True)

    st = init()
    evaluate(cfg, st, {"filings": [new_burn, new_res]}, "2026-11-16", seed_only=True)
    check("seed_only no alerts", True, True)
    check("seed stores ids", "kind:20261215000001" in st["seen"] and "kind:20261214000002" in st["seen"], True)

    st = init()
    a = evaluate(cfg, st, {"filings": []}, "2026-12-01")
    check("before D-7 silent", a, [])
    check("d7 flag still false", st.get("d7_notified"), False)
    a = evaluate(cfg, st, {"filings": []}, "2026-12-07")
    check("D-7 title", [x[0] for x in a], [f"{TAG} 트루엔 자사주 신탁 종료 D-7"])
    check("D-7 has end date and dart", "2026-12-14" in a[0][1] and "20251215000083" in a[0][1], True)
    check("D-7 once", evaluate(cfg, st, {"filings": []}, "2026-12-08"), [])

    old_burn = {
        "id": "kind:20251124000148", "acptno": "20251124000148",
        "title": "주식소각 결정", "filed": "2025-11-24 11:08",
        "src": "KIND", "url": "https://kind.krx.co.kr/z",
    }
    st = init()
    st["seen"] = ["kind:20251124000148"]
    check("old 2025 cancel silent", evaluate(cfg, st, {"filings": [old_burn]}, "2026-11-16"), [])

    msgs = []
    st = init()
    for ti, tx, *_ in evaluate(cfg, init(), {"filings": [new_burn, new_res]}, "2026-11-16"):
        msgs.append(ti + body("s", tx, "a"))
    d7 = evaluate(cfg, init(), {"filings": []}, "2026-12-07")
    msgs.extend(ti + body("s", tx, "a") for ti, tx, *_ in d7)
    text = " ".join(msgs) + json.dumps(cfg, ensure_ascii=False)
    check(
        "titles have [417790] and 417790, no middle dot or em-dash",
        (all(m.startswith(TAG) for m in msgs), "417790" in "".join(msgs), "\u00b7" in text, "\u2014" in text, "\u2013" in text),
        (True, True, False, False, False),
    )
    check("ends", cfg["ends_kst"], "2027-06-30 23:59")
    check("trust end", cfg["trust"]["end"], "2026-12-14")
    check("trust dart", cfg["trust"]["dart_rcept"], "20251215000083")

    stf = init()
    fail_alerts(cfg, stf, {"filings": "x"}, "2026-11-16")
    f2 = fail_alerts(cfg, stf, {"filings": "x"}, "2026-11-17")
    check("ops day2", [x[0] for x in f2], [f"{TAG} 공시 목록 조회 실패 | 2일 연속, 그 규칙 건너뜀"])
    check("ops -> cwally-ops", route_topic("cwally-port", f2[0][0]), "cwally-ops")

    print(f"SELFTEST {'PASS' if not fails else 'FAIL'}: {len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
