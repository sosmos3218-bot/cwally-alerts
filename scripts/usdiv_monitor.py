#!/usr/bin/env python3
"""[US-DIV] 미국 배당 ETF 분기 비교: 429000 TIGER 미국S&P500배당귀족 vs 458730 TIGER 미국배당다우존스.
검토용 사실 요약. 매수/매도 권유 아님, 자동 주문 아님.

기존 k203780 19:00 슬롯(cron-job.org 정시 dispatch + GitHub cron 백업)에 붙어, 분기 첫 KRX 거래일(1/4/7/10월)에 1번 보낸다.
그 실행이 빠지면 그 분기 첫 catchup_trading_days 거래일 안의 다음 실행에서 1번.
1) 1년/3년 총수익률(TR)과 가격 수익률, 같은 기간 일별 MDD. 네이버 무수정 일별 종가 + 분배락일 1좌당 분배금으로 직접 계산.
   r_t = (P_t + D_t) / P_(t-1) - 1. 매일 '종가 - 전일 대비'가 전일 종가(분배락일은 전일 종가 - 분배금 = 분배락 기준가격)와
   맞는지 검사해 빠진 날/빠진 분배금을 잡는다. 하나라도 안 맞으면 그 기간은 확인 불가 (fillna 없음).
2) 비용: 총보수(네이버), 합성총보수/실부담(funetf, 금융투자협회 공시 재가공), 차이 %p.
3) 분배금: 최근 12개월(분배락일 기준) 합계, 끝일 종가 대비 분배율, 직전 점검 대비 변화.
4) 교체 검토 신호 (a)~(d), 기준은 config. 신호 2개 이상이 두 분기 연속일 때만 priority 4 + rotating_light.
분배금 이력은 state 에 쌓고 새 분배락 공시만 추가로 조회한다.
modes: auto | dry(발송 없음, state 안 씀, 지금 기준 점검 내용 출력, --as-of "YYYY-MM-DD HH:MM" 가능)
       | seed(설치용: 분배금 이력 + 2026년 4분기 기준 점검을 조용히 state 에 저장, 발송 없음)
       | selftest(가짜 값, 네트워크 없음)
"""
from __future__ import annotations

import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402
from kr_calendar import KST, is_trading_day, session_day  # noqa: E402

NAME = "usdiv"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
NA = "확인 불가"
UA = "Mozilla/5.0 (cwally-alerts; +mailto:sosmos3218@gmail.com)"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
KIND = "https://kind.krx.co.kr"
KIND_VIEW = KIND + "/common/disclsviewer.do?method=search&acptno={acpt}"
FUNETF = "https://www.funetf.co.kr"
REVIEWS_KEEP = 12


# ---------------- io ----------------
def http(url: str, data: dict | None = None, headers: dict | None = None, tries: int = 3, timeout: int = 30) -> str:
    h = {"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"}
    h.update(headers or {})
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    if body is not None:
        h["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
    err: Exception | None = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=body, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
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


def num(v) -> float | None:
    if v is None:
        return None
    s = str(v).replace(",", "").replace("+", "").replace("%", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def load_state() -> dict:
    base = {"updated_at": None, "seeded": False, "last_review_q": None, "dists": {}, "seen_notice": {},
            "kind_notice_until": {}, "reviews": [], "fail_notice": {}}
    if STATE_PATH.exists():
        try:
            base.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return base


# ---------------- 가격 (네이버, 무수정) ----------------
def naver_closes(code: str, since: str, max_pages: int = 40) -> dict[str, dict]:
    """{day: {'close': P, 'chg': 전일 대비(부호)}}. pageSize 는 60 이 상한."""
    out: dict[str, dict] = {}
    for page in range(1, max_pages + 1):
        raw = json.loads(http(f"https://m.stock.naver.com/api/stock/{code}/price?pageSize=60&page={page}",
                              headers={"User-Agent": BROWSER_UA}))
        if not isinstance(raw, list) or not raw:
            break
        for r in raw:
            d = str(r.get("localTradedAt") or "")[:10]
            close, chg = num(r.get("closePrice")), num(r.get("compareToPreviousClosePrice"))
            if len(d) != 10 or close is None or chg is None:
                raise RuntimeError(f"네이버 {code} {d} 행 형식 이상")
            cc = str((r.get("compareToPreviousPrice") or {}).get("code") or "")
            if cc in ("4", "5") and chg > 0:  # 하한/하락인데 부호 없는 값
                chg = -chg
            out[d] = {"close": close, "chg": chg}
        if min(out) <= since:
            break
    if not out:
        raise RuntimeError(f"네이버 {code} 시세 0건")
    return dict(sorted(out.items()))


def naver_etf(code: str) -> dict:
    a = json.loads(http(f"https://m.stock.naver.com/api/stock/{code}/etfAnalysis", headers={"User-Agent": BROWSER_UA}))
    return {"fee": num(a.get("totalFee")), "ttm_div": num((a.get("dividend") or {}).get("dividendPerShareTtm")),
            "ref": str(a.get("returnPerformanceReferenceDate") or "")}


# ---------------- KIND ----------------
def kind_search(code: str, report: str, since: str, until: str, max_pages: int = 10) -> list[dict]:
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        form = {
            "method": "searchDetailsSub", "currentPageSize": "100", "pageIndex": str(page), "orderMode": "1",
            "orderStat": "D", "forward": "details_sub", "searchCodeType": "", "repIsuSrtCd": ("A" + code) if code else "",
            "allRepIsuSrtCd": "", "oldSearchCorpName": "", "disclosureType": "", "disTypevalue": "", "reportNm": report,
            "reportCd": "", "searchCorpName": code, "business": "", "marketType": "", "settlementMonth": "",
            "securities": "", "submitOblgNm": "", "enterprise": "", "fromDate": since, "toDate": until,
            "reportNmTemp": "", "bfrDsclsType": "on",
        }
        pg = http(KIND + "/disclosure/details.do", data=form,
                  headers={"X-Requested-With": "XMLHttpRequest", "Referer": KIND + "/disclosure/details.do?method=searchDetailsMain"})
        rows = 0
        for row in re.findall(r"(?s)<tr[^>]*>(.*?)</tr>", pg):
            acpt = re.search(r"openDisclsViewer\('(\d{14})'", row)
            if not acpt:
                continue
            rows += 1
            t = text_of(row)
            dm = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})", t)
            title = re.sub(r"\s*공시차트\s*주가차트\s*$", "", t[dm.end():].strip()) if dm else t
            out.append({"acptno": acpt.group(1), "filed": f"{dm.group(1)} {dm.group(2)}" if dm else "", "title": title})
        if rows == 0 and "조회된 결과값이 없습니다" not in pg and "<table" not in pg:
            raise RuntimeError("KIND 응답 형식 이상")
        if rows < 100:
            break
    return out


def kind_doc_text(acpt: str) -> str:
    viewer = http(KIND_VIEW.format(acpt=acpt))
    m = re.search(r"<option value='(\d+)\|Y'", viewer) or re.search(r"<option value='(\d+)\|", viewer)
    if not m:
        raise RuntimeError("KIND 본문 문서번호 없음")
    sc = http(f"{KIND}/common/disclsviewer.do?method=searchContents&docNo={m.group(1)}")
    p = re.search(r"setPath\('[^']*','([^']+\.htm)'", sc)
    if not p:
        raise RuntimeError("KIND 본문 경로 없음")
    return text_of(http(p.group(1), timeout=60))


def parse_notice(text: str) -> dict:
    """'ETF 분배락 기준가격 안내' 본문 -> 기준가격, 적용일(분배락일)."""
    b = re.search(r"기준가격\s*\(원\)\s*([\d,]+)", text)
    a = re.search(r"적용일\s*(\d{4})[-./](\d{2})[-./](\d{2})", text)
    if not b or not a or "분배락" not in text:
        raise RuntimeError("분배락 기준가격 안내 형식 이상")
    return {"base": num(b.group(1)), "ex": f"{a.group(1)}-{a.group(2)}-{a.group(3)}"}


DATE_RE = r"(\d{4})[-./]?(\d{2})[-./]?(\d{2})"


def parse_batch(text: str, isin: str) -> dict | None:
    """'ETF이익금분배신고(분배금안내)(일괄공시)' 본문에서 그 ISIN 행 -> 지급기준일, 1좌당 분배금."""
    m = re.search(isin + r"\s+(.{1,80}?)\s+" + DATE_RE + r"\s+" + DATE_RE + r"\s+([\d,]+(?:\.\d+)?)", text)
    if not m:
        return None
    g = m.groups()
    return {"record": f"{g[1]}-{g[2]}-{g[3]}", "pay": f"{g[4]}-{g[5]}-{g[6]}", "amt": num(g[7])}


def kind_amount(isin: str, filed_day: str, cache: dict) -> tuple[dict | None, str | None]:
    """분배락 안내가 올라온 날 전후 일괄공시에서 금액 찾기. ([정정] 포함 최신 공시 우선)"""
    d = date.fromisoformat(filed_day)
    key = filed_day
    if key not in cache:
        cache[key] = {"list": kind_search("", "이익금분배신고", (d - timedelta(days=6)).isoformat(),
                                          (d + timedelta(days=1)).isoformat()), "docs": {}}
    docs = [r for r in cache[key]["list"] if "이익금분배신고" in r["title"].replace(" ", "")]
    docs.sort(key=lambda r: (r["filed"], r["acptno"]), reverse=True)
    for r in docs:
        if r["acptno"] not in cache[key]["docs"]:
            try:
                cache[key]["docs"][r["acptno"]] = kind_doc_text(r["acptno"])
            except Exception as exc:  # noqa: BLE001
                cache[key]["docs"][r["acptno"]] = ""
                print(f"KIND doc {r['acptno']}: {exc}", file=sys.stderr)
        hit = parse_batch(cache[key]["docs"][r["acptno"]], isin)
        if hit:
            return hit, r["acptno"]
    return None, None


# ---------------- funetf ----------------
def funetf_form(isin: str) -> dict:
    pg = http(f"{FUNETF}/product/etf/view/{isin}", headers={"User-Agent": BROWSER_UA})
    f = re.search(r'(?s)<form name="frm" id="frm">(.*?)</form>', pg)
    if not f:
        raise RuntimeError("funetf 상품 페이지 형식 이상")
    fields = dict(re.findall(r'name="([^"]+)"[^>]*value="([^"]*)"', f.group(1)))
    if not fields.get("fundCd") or fields.get("itemId") != isin:
        raise RuntimeError("funetf fundCd 없음")
    return fields


def funetf_api(ep: str, form: dict, extra: dict | None = None):
    q = dict(form)
    q.update(extra or {})
    return json.loads(http(f"{FUNETF}/api/public/product/view/{ep}?" + urllib.parse.urlencode(q),
                           headers={"User-Agent": BROWSER_UA, "X-Requested-With": "XMLHttpRequest",
                                    "Referer": f"{FUNETF}/product/etf/view/{form.get('itemId')}"}))


def funetf_divs(form: dict) -> dict[str, float]:
    out = {}
    for r in funetf_api("etfdividend", form) or []:
        g, a = str(r.get("gijunYmd") or ""), num(r.get("divAmt"))
        if re.fullmatch(r"\d{8}", g) and a is not None:
            out[f"{g[:4]}-{g[4:6]}-{g[6:]}"] = a
    return out


def funetf_cost(form: dict) -> dict:
    rows = funetf_api("etftradinginfo", form) or []
    if not rows:
        raise RuntimeError("funetf 비용 0건")
    r = rows[0]
    return {"fee": num(r.get("feeTot")), "ter": num(r.get("terRate")), "real": num(r.get("realRate"))}


def funetf_top10(form: dict, day: date, back: int = 7) -> dict:
    """그 날짜(없으면 최대 back 일 전까지) PDF 의 상위 10종목 비중."""
    for i in range(back + 1):
        d = day - timedelta(days=i)
        rows = funetf_api("etfpdf", form, {"etfPdfYmd": d.strftime("%Y%m%d")}) or []
        rows = [r for r in rows if num(r.get("evP")) is not None and not str(r.get("grpItmNo") or "").startswith(("KRD", "CASH"))
                and "현금" not in str(r.get("citmNm") or "")]
        if rows:
            rows.sort(key=lambda r: -num(r["evP"]))
            top = rows[:10]
            return {"day": d.isoformat(), "sum": round(sum(num(r["evP"]) for r in top), 2),
                    "names": [f"{r.get('ticker') or r.get('citmNm')}" for r in top]}
    raise RuntimeError("funetf PDF 0건")


# ---------------- 분배금 이력 ----------------
def sync_dists(cfg: dict, state: dict, today: str, log: list[str]) -> None:
    """KIND 분배락 안내(분배락일, 기준가격) + KIND 일괄공시(금액) + funetf 교차확인. 새 공시만 조회."""
    cache: dict = {}
    for e in cfg["etfs"]:
        code, isin = e["code"], e["isin"]
        dist = state["dists"].setdefault(code, {})
        seen = set(state["seen_notice"].setdefault(code, []))
        since = state["kind_notice_until"].get(code) or e["listed"]
        since = (date.fromisoformat(since) - timedelta(days=45)).isoformat()
        try:
            fun = funetf_divs(funetf_form(isin))
        except Exception as exc:  # noqa: BLE001
            fun = {}
            log.append(f"funetf 분배금 {code}: {exc}")
        notices = []
        y = int(since[:4])
        while f"{y}-01-01" <= today:
            a, b = max(since, f"{y}-01-01"), min(today, f"{y}-12-31")
            notices += [n for n in kind_search(code, "분배락", a, b) if "분배락 기준가격" in n["title"]]
            y += 1
        for n in sorted(notices, key=lambda n: n["filed"]):
            if n["acptno"] in seen:
                continue
            try:
                info = parse_notice(kind_doc_text(n["acptno"]))
            except Exception as exc:  # noqa: BLE001
                log.append(f"KIND 분배락 안내 {code} {n['acptno']}: {exc}")
                continue
            ex = info["ex"]
            rec = {"base": info["base"], "notice": n["acptno"], "filed": n["filed"][:10]}
            try:
                hit, acpt = kind_amount(isin, n["filed"][:10], cache)
            except Exception as exc:  # noqa: BLE001
                hit, acpt = None, None
                log.append(f"KIND 일괄공시 {code} {ex}: {exc}")
            fa = fun.get(ex)
            if hit:
                rec.update(amt=hit["amt"], record=hit["record"], src="KIND", src_doc=acpt)
                if fa is not None and abs(fa - hit["amt"]) > 1e-9:
                    rec["note"] = f"funetf {fa:g} 와 다름, KIND 값 사용"
                    log.append(f"{code} {ex}: KIND {hit['amt']:g} vs funetf {fa:g}")
            elif fa is not None:
                rec.update(amt=fa, src="funetf (KIND 금액 못 찾음)")
            else:
                rec.update(amt=None, src=NA)
            if ex in dist and dist[ex].get("amt") is not None and rec.get("amt") is None:
                continue
            dist[ex] = rec
            if rec.get("amt") is not None:
                seen.add(n["acptno"])
        # KIND 안내가 없는데 funetf 에만 있는 분배락일 (표시만, 가격 검사로 확인)
        for ex, a in fun.items():
            if ex not in dist and ex >= e["listed"]:
                dist[ex] = {"amt": a, "src": "funetf (KIND 분배락 안내 없음)", "base": None}
                log.append(f"{code} {ex}: KIND 분배락 안내 없음, funetf {a:g}")
        # 금액 못 찾은 공시는 다음 실행에서 다시 본다
        state["seen_notice"][code] = sorted(seen)
        state["kind_notice_until"][code] = today
        state["dists"][code] = dict(sorted(dist.items()))


def dist_sanity(dist: dict) -> list[str]:
    issues = []
    by_year: dict[str, int] = {}
    days = sorted(d for d, v in dist.items() if v.get("amt") is not None)
    for d in days:
        by_year[d[:4]] = by_year.get(d[:4], 0) + 1
    for y, n in by_year.items():
        if n > 12:
            issues.append(f"{y}년 분배 {n}회 (월 1회 초과)")
    for a, b in zip(days, days[1:]):
        if (date.fromisoformat(b) - date.fromisoformat(a)).days < 10:
            issues.append(f"분배락일 간격 이상 {a}, {b}")
    for d, v in dist.items():
        if v.get("amt") is None:
            issues.append(f"{d} 분배금 {NA}")
        elif v["amt"] <= 0:
            issues.append(f"{d} 분배금 {v['amt']}")
    return issues


# ---------------- 계산 (순수 함수, selftest 대상) ----------------
def start_day(days: list[str], end: str, years: int) -> str | None:
    e = date.fromisoformat(end)
    try:
        target = e.replace(year=e.year - years)
    except ValueError:  # 2/29
        target = e.replace(year=e.year - years, day=28)
    c = [d for d in days if d <= target.isoformat()]
    return c[-1] if c else None


def window_calc(closes: dict, dist: dict, start: str, end: str, tick: float) -> dict:
    """총수익/가격 수익률, TR 기준 MDD. 검사 실패면 {'ok': False, 'why': ...}."""
    days = [d for d in sorted(closes) if start <= d <= end]
    if not days or days[0] != start or days[-1] != end:
        return {"ok": False, "why": f"{start}~{end} 종가 구간 없음"}
    tr, peak, peak_d, mdd, mdd_pk, mdd_tr = 1.0, 1.0, start, 0.0, start, start
    n_div = 0
    for p, t in zip(days, days[1:]):
        P0, P1, chg = closes[p]["close"], closes[t]["close"], closes[t]["chg"]
        base = P1 - chg  # 네이버 전일 대비 기준값 = 전일 종가, 분배락일은 분배락 기준가격
        D = 0.0
        if t in dist:
            D = dist[t].get("amt")
            if D is None:
                return {"ok": False, "why": f"{t} 분배금 {NA}"}
            if abs(base - (P0 - D)) >= tick:
                return {"ok": False, "why": f"{t} 분배락 검산 불일치 (전일 {P0:,.0f} - 분배금 {D:g} vs 기준 {base:,.0f})"}
            n_div += 1
        elif abs(base - P0) > 1e-9:
            return {"ok": False, "why": f"{t} 전일 종가 불일치 ({p} {P0:,.0f} vs {base:,.0f}, 누락 또는 미확인 분배락)"}
        tr *= (P1 + D) / P0
        if tr > peak:
            peak, peak_d = tr, t
        dd = tr / peak - 1
        if dd < mdd:
            mdd, mdd_pk, mdd_tr = dd, peak_d, t
    return {"ok": True, "start": start, "end": end, "tr": (tr - 1) * 100,
            "price": (closes[end]["close"] / closes[start]["close"] - 1) * 100,
            "mdd": mdd * 100, "mdd_peak": mdd_pk, "mdd_trough": mdd_tr, "n_div": n_div, "n_days": len(days)}


def dist12(dist: dict, end: str) -> dict:
    e = date.fromisoformat(end)
    lo = e.replace(year=e.year - 1).isoformat()
    ev = {d: v for d, v in dist.items() if lo < d <= end}
    if any(v.get("amt") is None for v in ev.values()):
        return {"ok": False, "from": lo, "n": len(ev)}
    return {"ok": True, "from": lo, "sum": sum(v["amt"] for v in ev.values()), "n": len(ev)}


def eval_signals(cfg: dict, cur: dict, prev: dict | None) -> dict:
    """{'a':bool|None, 'b':..., 'c':..., 'd':..., 'count':int|None, 'why':{...}}. prev 없으면 판정 없음."""
    s = cfg["signals"]
    if not prev:
        return {"a": None, "b": None, "c": None, "d": None, "count": None, "why": {}}
    why: dict[str, str] = {}
    # (a) 비용 차이
    a = None
    for basis, k in (("실부담", "real"), ("총보수", "fee")):
        g1, g0 = cur["gap"].get(k), prev.get("gap", {}).get(k)
        if g1 is not None and g0 is not None:
            a = (g1 - g0) >= s["cost_gap_widen_pp"] - 1e-12
            why["a"] = f"{basis} 차이 {g0:.4f}%p → {g1:.4f}%p ({g1 - g0:+.4f}%p)"
            break
    # (b) 12개월 분배금 감소
    b, parts = None, []
    for code, e in cur["etf"].items():
        s1, s0 = e.get("sum12"), prev.get("etf", {}).get(code, {}).get("sum12")
        if s1 is None or s0 is None or s0 <= 0:
            parts.append(f"{code} {NA}")
            continue
        ch = (s1 / s0 - 1) * 100
        hit = ch <= -s["dist12m_drop_pct"] + 1e-12
        b = bool(b) or hit
        parts.append(f"{code} {s0:,.0f}원 → {s1:,.0f}원 ({ch:+.1f}%)")
    why["b"] = ", ".join(parts)
    # (c) 상위 10종목 비중
    c, parts = None, []
    for code, e in cur["etf"].items():
        t1, t0 = e.get("top10"), prev.get("etf", {}).get(code, {}).get("top10")
        if t1 is None or t0 is None:
            parts.append(f"{code} {NA}")
            continue
        ch = t1 - t0
        hit = abs(ch) >= s["top10_weight_change_pp"] - 1e-12
        c = bool(c) or hit
        parts.append(f"{code} {t0:.1f}% → {t1:.1f}% ({ch:+.1f}%p)")
    why["c"] = ", ".join(parts)
    # (d) KIND 공시
    d = None
    if cur.get("kind") is not None:
        d = len(cur["kind"]) > 0
        why["d"] = "; ".join(f"{k['code']} {k['filed'][:10]} {k['title']}" for k in cur["kind"]) or "해당 공시 없음"
    else:
        why["d"] = NA
    count = sum(1 for x in (a, b, c, d) if x)
    return {"a": a, "b": b, "c": c, "d": d, "count": count, "why": why}


def emphasis(cfg: dict, sig: dict, prev: dict | None) -> bool:
    s = cfg["signals"]
    if sig.get("count") is None or not prev or prev.get("signals", {}).get("count") is None:
        return False
    return sig["count"] >= s["emphasis_min_signals"] and prev["signals"]["count"] >= s["emphasis_min_signals"]


# ---------------- 분기 일정 ----------------
def quarter_key(d: date) -> str:
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def quarter_first_days(d: date, n: int) -> list[date]:
    q0 = date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)
    out, x = [], q0
    while len(out) < n:
        if is_trading_day(x):
            out.append(x)
        x += timedelta(days=1)
    return out


def next_review_day(d: date) -> date:
    m = 3 * ((d.month - 1) // 3) + 1 + 3
    y = d.year + (1 if m > 12 else 0)
    return quarter_first_days(date(y, (m - 1) % 12 + 1, 1), 1)[0]


# ---------------- 점검 계산 ----------------
def build_review(cfg: dict, state: dict, ref_day: date, log: list[str], fetch_live: bool = True) -> tuple[dict, list[str]]:
    """ref_day 가 속한 분기의 점검. 끝일 = 직전 분기 마지막 거래일. 반환 (review, 치명 오류 목록)."""
    q0 = date(ref_day.year, 3 * ((ref_day.month - 1) // 3) + 1, 1)
    tick = cfg["returns"]["price_tick_won"]
    years = cfg["returns"]["windows_years"]
    fatal: list[str] = []
    rv: dict = {"q": quarter_key(ref_day), "built": datetime.now(KST).strftime("%Y-%m-%d %H:%M"), "etf": {}, "gap": {}}
    since = (q0 - timedelta(days=366 * max(years) + 20)).isoformat()
    for e in cfg["etfs"]:
        code = e["code"]
        r: dict = {"name": e["name"]}
        try:
            closes = naver_closes(code, since)
        except Exception as exc:  # noqa: BLE001
            fatal.append(f"네이버 시세 {code}: {exc}")
            rv["etf"][code] = r
            continue
        days = [d for d in closes if d < q0.isoformat()]
        if not days:
            fatal.append(f"{code} 직전 분기 종가 없음")
            rv["etf"][code] = r
            continue
        end = days[-1]
        rv["end"] = end
        r["close_end"] = closes[end]["close"]
        dist = state["dists"].get(code, {})
        for y in years:
            sd = start_day(days, end, y)
            if sd is None or sd < e["listed"]:
                r[f"w{y}"] = {"ok": False, "why": f"{y}년 전 종가 없음 (상장 {e['listed']})"}
            else:
                r[f"w{y}"] = window_calc(closes, dist, sd, end, tick)
            if not r[f"w{y}"]["ok"]:
                log.append(f"{code} {y}년: {r[f'w{y}']['why']}")
        d12 = dist12(dist, end)
        r["d12"] = d12
        r["sum12"] = d12.get("sum") if d12["ok"] else None
        r["yield12"] = (d12["sum"] / closes[end]["close"] * 100) if d12["ok"] else None
        r["dist_issues"] = dist_sanity(dist)
        r["dist_src"] = {"KIND": sum(1 for v in dist.values() if v.get("src") == "KIND"),
                         "funetf": sum(1 for v in dist.values() if str(v.get("src", "")).startswith("funetf")),
                         "na": sum(1 for v in dist.values() if v.get("amt") is None)}
        r["n_dist_years"] = {}
        for d0, v in dist.items():
            r["n_dist_years"][d0[:4]] = r["n_dist_years"].get(d0[:4], 0) + 1
        if fetch_live:
            try:
                nv = naver_etf(code)
                r["fee"] = nv["fee"]
                r["naver_ttm"] = nv["ttm_div"]
            except Exception as exc:  # noqa: BLE001
                log.append(f"네이버 총보수 {code}: {exc}")
            try:
                form = funetf_form(e["isin"])
                try:
                    cst = funetf_cost(form)
                    r["ter"], r["real"] = cst["ter"], cst["real"]
                    r["fee_funetf"] = cst["fee"]
                except Exception as exc:  # noqa: BLE001
                    log.append(f"funetf 비용 {code}: {exc}")
                try:
                    t10 = funetf_top10(form, min(datetime.now(KST).date(), ref_day + timedelta(days=10)))
                    r["top10"], r["top10_day"], r["top10_names"] = t10["sum"], t10["day"], t10["names"]
                except Exception as exc:  # noqa: BLE001
                    log.append(f"funetf PDF {code}: {exc}")
            except Exception as exc:  # noqa: BLE001
                log.append(f"funetf {code}: {exc}")
        if r.get("fee") is not None and abs(r["fee"] - e["official_fee_pct"]) > 1e-9:
            log.append(f"{code} 총보수 {r['fee']}% (config {e['official_fee_pct']}%) 변경 여부 확인")
        rv["etf"][code] = r
    c1, c2 = (cfg["etfs"][0]["code"], cfg["etfs"][1]["code"])
    for k in ("fee", "ter", "real"):
        v1, v2 = rv["etf"].get(c1, {}).get(k), rv["etf"].get(c2, {}).get(k)
        rv["gap"][k] = round(abs(v1 - v2), 6) if v1 is not None and v2 is not None else None
    return rv, fatal


def kind_changes(cfg: dict, since: str, until: str) -> list[dict]:
    s = cfg["signals"]
    out = []
    for e in cfg["etfs"]:
        for n in kind_search(e["code"], "", since, until):
            if re.search(s["kind_title_skip_re"], n["title"]):
                continue
            try:
                txt = kind_doc_text(n["acptno"])
            except Exception as exc:  # noqa: BLE001
                out.append({"code": e["code"], "filed": n["filed"], "title": f"{n['title']} (본문 {NA}: {str(exc)[:60]})",
                            "acptno": n["acptno"]})
                continue
            if re.search(s["kind_text_hit_re"], txt) and "휴장" not in txt[:200]:
                out.append({"code": e["code"], "filed": n["filed"], "title": n["title"], "acptno": n["acptno"]})
    return out


# ---------------- 메시지 ----------------
def pct(v: float | None, n: int = 1, sign: bool = True) -> str:
    if v is None:
        return NA
    return f"{v:+.{n}f}%" if sign else f"{v:.{n}f}%"


def body(stamp: str, head: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([f"{stamp} | US-DIV | {head}", f"이게 뭐 알림: {what}", f"지금 결과/상태: {status}",
                      f"지금 할 일: {action}", f"나중에: {later}", f"익절/줄일 때: {trim}", f"하지 말 것: {avoid}"])


def yn(v) -> str:
    return NA if v is None else ("해당" if v else "아님")


def review_msg(cfg: dict, stamp: str, rv: dict, sig: dict, prev: dict | None, emph: bool, nxt: date) -> tuple[str, str, int, list[str]]:
    c1, c2 = cfg["etfs"][0]["code"], cfg["etfs"][1]["code"]
    E = rv["etf"]
    years = cfg["returns"]["windows_years"]
    lines = []
    for y in years:
        w1, w2 = E.get(c1, {}).get(f"w{y}") or {}, E.get(c2, {}).get(f"w{y}") or {}
        span = next((f"{w['start']}~{w['end']}" for w in (w1, w2) if w.get("ok")), "")
        if w1.get("ok") and w2.get("ok") and w1["start"] != w2["start"]:
            span = f"{c1} {w1['start']}, {c2} {w2['start']} 시작 ~{w1['end']}"

        def one(c, w):
            if not w.get("ok"):
                return f"{c} {NA} ({w.get('why', '데이터 없음')})"
            return f"{c} TR {pct(w['tr'])} (가격 {pct(w['price'])}), MDD {pct(w['mdd'], sign=False)} (고점 {w['mdd_peak']}, 저점 {w['mdd_trough']})"
        lines.append(f"[{y}년 {span}] {one(c1, w1)} / {one(c2, w2)}")
    sec1 = "\n".join(["1) 수익률(무수정 일별 종가 + 분배락일 분배금으로 직접 계산, 분배금 재투자, 세전, MDD 는 일별 총수익 기준):"] + lines)

    def cost(c):
        r = E.get(c, {})
        return f"{c} 총보수 {pct(r.get('fee'), 2, False)}, 합성총보수 {pct(r.get('ter'), 2, False)}, 실부담 {pct(r.get('real'), 4, False)}"
    g = rv["gap"]
    gaps = f"차이 총보수 {g['fee']:.2f}%p" if g.get("fee") is not None else f"차이 총보수 {NA}"
    gaps += f", 실부담 {g['real']:.4f}%p" if g.get("real") is not None else f", 실부담 {NA}"
    sec2 = (f"2) 비용: {cost(c1)} / {cost(c2)}. {gaps}. 출처 총보수 네이버(조회 {rv['built'][:10]}), "
            "합성총보수/실부담 funetf(금융투자협회 공시 재가공, 기준일 미표기).")

    def dv(c):
        r = E.get(c, {})
        d12 = r.get("d12") or {}
        if r.get("sum12") is None:
            return f"{c} {NA}"
        p = (prev or {}).get("etf", {}).get(c, {}).get("sum12")
        ch = f", 직전 점검 {p:,.0f}원 대비 {(r['sum12'] / p - 1) * 100:+.1f}%" if p else ", 직전 점검 없음"
        return f"{c} {r['sum12']:,.0f}원 {d12.get('n')}회, 종가 {r['close_end']:,.0f}원 대비 {r['yield12']:.2f}%{ch}"
    d12f = (E.get(c1, {}).get("d12") or {}).get("from", "")
    sec3 = f"3) 분배금 최근 12개월(분배락일 {d12f} 초과~{rv.get('end', '')}): {dv(c1)} / {dv(c2)}."
    issues = [f"{c} {x}" for c in (c1, c2) for x in (E.get(c, {}).get("dist_issues") or [])]
    def cnt(c):
        r = E.get(c, {})
        ys = " ".join(f"{y}:{n}" for y, n in sorted((r.get("n_dist_years") or {}).items())) or NA
        src = r.get("dist_src") or {}
        return f"{c} {ys} (금액 KIND {src.get('KIND', 0)}건, funetf {src.get('funetf', 0)}건, {NA} {src.get('na', 0)}건)"
    sec3 += f" 연도별 분배 횟수(분배락일 기준): {cnt(c1)}; {cnt(c2)}."
    if issues:
        sec3 += " 분배 이력 점검: " + ", ".join(issues[:4]) + "."
    w = sig.get("why", {})
    if sig.get("count") is None:
        sec4 = "4) 교체 검토 신호: 직전 점검 기록이 없어 판정 없음."
    else:
        pc = (prev or {}).get("signals", {}).get("count")
        pcs = f"직전 점검 {pc}개" if pc is not None else "직전 점검은 기준값 저장용이라 신호 판정 없음"
        sec4 = (f"4) 교체 검토 신호 {sig['count']}개 ({pcs}): "
                f"(a) 비용 차이 확대 {yn(sig['a'])} ({w.get('a', NA)}); (b) 12개월 분배금 10% 이상 감소 {yn(sig['b'])} ({w.get('b', NA)}); "
                f"(c) 상위 10종목 비중 변화 {yn(sig['c'])} ({w.get('c', NA)}); (d) 지수/방법론/분배정책 공시 {yn(sig['d'])} ({w.get('d', NA)}).")
    q = rv["q"]
    head = f"배당 ETF 분기 비교 ({q[:4]}년 {q[-1]}분기 점검)"
    title = (f"[US-DIV] {q[:4]} {q[-1]}분기 배당 ETF 비교 | 신호 {sig['count']}개 2분기 연속, 교체 검토"
             if emph else f"[US-DIV] {q[:4]} {q[-1]}분기 배당 ETF 비교 | {c1} vs {c2} 검토용")
    msg = body(stamp, head,
               f"{c1} {cfg['etfs'][0]['name']} vs {c2} {cfg['etfs'][1]['name']} 분기 비교. 사실 정리와 검토 안내이며 매수/매도 권유 아님.",
               "\n".join([sec1, sec2, sec3, sec4]),
               "숫자만 확인. 신호가 있으면 원문(KIND 공시, 운용사 안내)을 직접 확인." + (" 신호 2개 이상이 두 분기 연속이라 교체 검토 시점." if emph else ""),
               f"다음 점검 {nxt.isoformat()} 19:00.",
               "이 비교 하나로 팔거나 갈아타지 않기. 세금과 매매 비용까지 따져 보기.",
               "권유로 읽기, 확인 불가 값을 0 으로 읽기, 과거 수익률을 미래 수익률로 읽기.")
    return title, msg, (4 if emph else 3), (["rotating_light", "bar_chart"] if emph else ["bar_chart"])


# ---------------- main ----------------
def main() -> int:
    args = sys.argv[1:]
    mode = (args[0] if args and not args[0].startswith("--") else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    if mode == "selftest":
        return selftest(cfg)
    now = datetime.now(KST)
    if "--as-of" in args:
        if mode != "dry":
            print("--as-of 는 dry 에서만")
            return 2
        now = datetime.strptime(args[args.index("--as-of") + 1], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    if mode not in {"auto", "dry", "seed"}:
        print(f"unknown mode {mode}")
        return 2
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    today = now.strftime("%Y-%m-%d")
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    state = load_state()
    sess = session_day(now)
    n_catch = int(cfg.get("catchup_trading_days", 5))
    window = quarter_first_days(sess, n_catch)
    qk = quarter_key(sess)
    due = is_trading_day(sess) and sess in window and state.get("last_review_q") != qk and state.get("seeded")
    print(f"now={now:%Y-%m-%d %H:%M} session={sess} trading={is_trading_day(sess)} quarter={qk} "
          f"window={[d.isoformat() for d in window]} last_review_q={state.get('last_review_q')} due={due}")
    if mode == "auto" and not due:
        print("not a review run: skip")
        return 0
    out: list[tuple[str, str, int, list[str]]] = []
    log: list[str] = []

    def ops_once(key: str, title: str, msg: str) -> None:
        fn = state.setdefault("fail_notice", {})
        if fn.get(key) != today:
            out.append((title, msg, 3, ["warning"]))
            fn[key] = today

    fatal: list[str] = []
    try:
        sync_dists(cfg, state, today, log)
    except Exception as exc:  # noqa: BLE001
        fatal.append(f"KIND 분배락 공시: {exc}")
    rv, f2 = build_review(cfg, state, sess, log)
    fatal += f2
    prev = state["reviews"][-1] if state.get("reviews") else None
    if mode == "seed":
        prev = None
    since = (prev or {}).get("kind_until")
    if since:
        try:
            rv["kind"] = kind_changes(cfg, since, today)
        except Exception as exc:  # noqa: BLE001
            rv["kind"] = None
            log.append(f"KIND 공시 검색: {exc}")
    rv["kind_until"] = today
    sig = eval_signals(cfg, rv, prev)
    rv["signals"] = {k: sig[k] for k in ("a", "b", "c", "d", "count")}
    rv["signals_why"] = sig["why"]
    emph = emphasis(cfg, sig, prev)
    nxt = next_review_day(sess)
    for x in log:
        print("note:", x)
    for code, r in rv["etf"].items():
        brief = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()
                 if k in ("close_end", "sum12", "yield12", "fee", "ter", "real", "top10", "top10_day", "naver_ttm", "n_dist_years")}
        print(f"{code}: {brief}")
        for y in cfg["returns"]["windows_years"]:
            print(f"  {y}y: {json.dumps(r.get(f'w{y}'), ensure_ascii=False)}")
    print(f"gap: {rv['gap']} signals: {rv['signals']} emphasis={emph}")

    last_try = sess == window[-1]
    if fatal and mode == "auto" and not last_try:
        ops_once("fetch", "[US-DIV] 배당 ETF 분기 비교 조회 실패 | 다음 실행에서 다시 시도",
                 body(stamp, "배당 ETF 분기 비교", "분기 비교 알림의 데이터 조회 실패 안내.",
                      f"{NA}: {' | '.join(fatal)[:400]}.", "필요하면 네이버 증권, KIND 에서 직접 보세요.",
                      f"이 분기 {window[-1].isoformat()} 까지 19:00 실행마다 다시 시도합니다.",
                      "이번 안내로 비중을 바꾸지 마세요.", "조회 실패를 신호 없음으로 읽기."))
    else:
        title, msg, pri, tags = review_msg(cfg, stamp, rv, sig, prev, emph, nxt)
        if fatal:
            msg = msg.replace("지금 할 일:", f"(조회 실패로 일부 {NA}: {' | '.join(fatal)[:200]})\n지금 할 일:", 1)
        if mode == "seed":
            print("SEED: baseline review stored, nothing sent")
            print(f"[preview]\nTITLE: {title}\n{msg}\n")
        else:
            out.append((title, msg, pri, tags))
        if mode in ("auto", "seed"):
            state["reviews"] = (state.get("reviews") or [])[-(REVIEWS_KEEP - 1):] + [rv]
            state["last_review_q"] = qk
            if mode == "seed":
                state["seeded"] = True
    for title, msg, pri, tags in out:
        print(f"TITLE: {title}\nPRIORITY: {pri} TOPIC: {route_topic(topic, title)} BYTES: {len(msg.encode())}\n{msg}\n")
        if not dry:
            publish(topic, title, msg, pri, tags, server)
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    state["updated_at"] = now.isoformat()
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return 0


def selftest(cfg: dict) -> int:
    """가짜 값으로 계산, 검산, 신호, 강조, 일정, 문구 검사 (네트워크, ntfy, state 없음)."""
    tick = cfg["returns"]["price_tick_won"]
    closes = {"2026-01-02": {"close": 100.0, "chg": 0.0}, "2026-01-05": {"close": 110.0, "chg": 10.0},
              "2026-01-06": {"close": 99.0, "chg": -11.0}, "2026-01-07": {"close": 103.0, "chg": 6.0},
              "2026-01-08": {"close": 105.0, "chg": 2.0}}
    dist = {"2026-01-07": {"amt": 2.0}}  # 분배락 기준가격 97 = 99 - 2
    w = window_calc(closes, dist, "2026-01-02", "2026-01-08", tick)
    exp_tr = (110 / 100) * (99 / 110) * ((103 + 2) / 99) * (105 / 103) - 1
    assert w["ok"] and abs(w["tr"] - exp_tr * 100) < 1e-9, w
    assert abs(w["price"] - 5.0) < 1e-9 and abs(w["mdd"] - (99 / 110 - 1) * 100) < 1e-9, w
    assert w["mdd_peak"] == "2026-01-05" and w["mdd_trough"] == "2026-01-06" and w["n_div"] == 1
    # 분배금 빠지면 확인 불가 (fillna 없음)
    assert not window_calc(closes, {}, "2026-01-02", "2026-01-08", tick)["ok"]
    assert not window_calc(closes, {"2026-01-07": {"amt": None}}, "2026-01-02", "2026-01-08", tick)["ok"]
    # 금액이 틀리면 검산 실패
    assert not window_calc(closes, {"2026-01-07": {"amt": 9.0}}, "2026-01-02", "2026-01-08", tick)["ok"]
    # 하루 빠진 종가
    gap = {k: v for k, v in closes.items() if k != "2026-01-06"}
    assert not window_calc(gap, dist, "2026-01-02", "2026-01-08", tick)["ok"]
    # 호가 단위 반올림(기준가 14,405 = 14,440 - 39 를 5원 올림)
    c2 = {"a": {"close": 14440.0, "chg": 0.0}, "b": {"close": 14225.0, "chg": -180.0}}
    assert window_calc(c2, {"b": {"amt": 39.0}}, "a", "b", tick)["ok"]
    assert start_day(["2023-09-26", "2023-09-27", "2023-10-04"], "2026-09-30", 3) == "2023-09-27"
    d12 = dist12({"2025-09-29": {"amt": 5}, "2025-10-29": {"amt": 30}, "2026-09-29": {"amt": 40}}, "2026-09-30")
    assert d12["ok"] and d12["sum"] == 70 and d12["n"] == 2, d12
    assert dist_sanity({"2026-01-07": {"amt": 2.0}, "2026-01-09": {"amt": 1.0}})
    # 일정
    assert quarter_first_days(date(2027, 1, 4), 5)[0] == date(2027, 1, 4)
    assert next_review_day(date(2026, 10, 23)) == date(2027, 1, 4)
    assert quarter_key(date(2027, 1, 4)) == "2027Q1"
    # 신호
    c1, c2c = cfg["etfs"][0]["code"], cfg["etfs"][1]["code"]

    def rvw(real1, real2, s1, s2, t1, t2, kind):
        return {"etf": {c1: {"sum12": s1, "top10": t1}, c2c: {"sum12": s2, "top10": t2}},
                "gap": {"fee": 0.24, "real": abs(real1 - real2)}, "kind": kind}
    base = rvw(0.30, 0.10, 276, 434, 20.0, 40.0, [])
    s0 = eval_signals(cfg, base, None)
    assert s0["count"] is None
    quiet = eval_signals(cfg, rvw(0.30, 0.10, 270, 430, 21.0, 41.0, []), base)
    assert quiet["count"] == 0, quiet
    two = eval_signals(cfg, rvw(0.36, 0.10, 240, 430, 21.0, 41.0, []), base)  # 갭 +0.06, 배당귀족 -13%
    assert two["a"] and two["b"] and not two["c"] and not two["d"] and two["count"] == 2, two
    edge = eval_signals(cfg, rvw(0.35, 0.10, 248.4, 434, 25.0, 40.0, [{"code": c1, "filed": "2027-02-01 10:00", "title": "x"}]), base)
    assert edge["a"] and edge["b"] and edge["c"] and edge["d"] and edge["count"] == 4, edge  # 경계값 포함
    unk = eval_signals(cfg, {"etf": {c1: {}, c2c: {}}, "gap": {}, "kind": None}, base)
    assert unk["count"] == 0 and unk["a"] is None and unk["c"] is None and unk["d"] is None
    prev2 = dict(base, signals={"count": 2})
    prev1 = dict(base, signals={"count": 1})
    prev_seed = dict(base, signals={"count": None})
    assert emphasis(cfg, two, prev2) and not emphasis(cfg, two, prev1) and not emphasis(cfg, two, prev_seed)
    assert not emphasis(cfg, quiet, prev2)
    # 문구
    win = {"ok": True, "start": "2025-09-30", "end": "2026-09-30", "tr": 7.1, "price": 4.9, "mdd": -12.3,
           "mdd_peak": "2026-02-10", "mdd_trough": "2026-04-08", "n_div": 12}
    rv = {"q": "2027Q1", "built": "2027-01-04 19:00", "end": "2026-12-30", "gap": {"fee": 0.24, "ter": 0.2, "real": 0.2},
          "etf": {c1: {"w1": win, "w3": {"ok": False, "why": "2023-10-27 분배금 확인 불가"}, "fee": 0.25, "ter": 0.33,
                       "real": 0.3712, "sum12": 276.0, "yield12": 2.1, "close_end": 13145.0,
                       "d12": {"ok": True, "from": "2025-12-30", "n": 12}, "dist_issues": []},
                  c2c: {"w1": win, "w3": win, "fee": 0.01, "ter": 0.06, "real": 0.1064, "sum12": 434.0, "yield12": 2.9,
                        "close_end": 14810.0, "d12": {"ok": True, "from": "2025-12-30", "n": 12}, "dist_issues": []}}}
    for sig, pv, em in ((s0, None, False), (two, prev2, True), (quiet, prev1, False)):
        t, m, p, tg = review_msg(cfg, "2027-01-04 19:00 KST", rv, sig, pv, em, date(2027, 4, 1))
        assert "\u00b7" not in t + m and "\u2014" not in t + m, "middle dot / em dash"
        assert route_topic("cwally-port", t) == "cwally-port", t
        assert len((m + "\n꼬지는 때: 없음 (수동 해제 전까지, 기준은 config)").encode()) < 3900, len(m.encode())
        assert (p == 4 and "rotating_light" in tg) if em else (p == 3 and "rotating_light" not in tg)
        assert "권유 아님" in m
        print(f"--- sample (emph={em})\nTITLE: {t}\nPRIORITY: {p} TAGS: {tg} BYTES: {len(m.encode())}\n{m}\n")
    ops_t = "[US-DIV] 배당 ETF 분기 비교 조회 실패 | 다음 실행에서 다시 시도"
    assert route_topic("cwally-port", ops_t) == "cwally-ops"
    print("SELFTEST OK: no network, no publish, no state")
    return 0


if __name__ == "__main__":
    sys.exit(main())
