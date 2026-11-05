#!/usr/bin/env python3
"""[KAL] 대한항공 003490 관심 종목 알림 (2026-11-03 설치, 같은 날 11:45 사용자 분석으로 갱신).

평일 15:40 KST (0090b0 슬롯에 부착). 모두 상태가 바뀔 때만 1번, 같은 실행에서 걸린 것은 한 건으로 묶음.
  price : 정규장 확정 종가. 26,500 이하 '1차 지정가 검토 구간'(매수 검토, 조건 1 점검표와 규모),
          24,000 비관 시나리오 p4, 32,500 기본 시나리오 구간 진입 p3, 40,000 낙관 시나리오 p3. 선마다 rearm.
  pref  : 우선주 003495 할인율 = 1 - 우선주/보통주(같은 날 확정 종가). 28% 이상 p3, 25% 아래 재무장.
  wti   : CL=F 미국장 정산가(매 실행 chartPreviousClose 를 state 에 쌓음) 2일 연속 110 이상 p3, 105 아래 재무장.
  fuel  : 제트유 대용 HO=F, 설치 기준 대비 +10% p3(+5% 아래 재무장), -15% p3(-10% 위 재무장).
  events: DART 목록 + 네이버 공시. 유상증자결정 p4(언제든 1번), 합병 완료 p3, 합병 일정 정정 p3(공시마다),
          12/19 까지 완료 없음 p3 안내, 2027-04-01 까지 없음 p4, 3분기 분기보고서/연결 잠정 p3(11/3~11/30),
          4분기 잠정 전날 1/14 안내와 공시 1번.
  2026-11-04: 32,500 선은 종가 또는 장중 고가, 26,500 선은 종가 또는 장중 저가(basis hl, 재무장 3% 종가, 잠깐 있던 26,000 선은 26,500 에 합침).
          12/15 '내일 합병기일 12/16, 합병등기 12/17' 안내 1번. 전용 규칙에 안 걸린 공시도 events.filings 규칙으로
          1번(실적, 사채/증자 p4, 합병, 공급계약 p3; 같은 날 같은 규칙은 DART/네이버 중복 1번).
첫 실행(또는 새 항목)은 조용히 시드. 데이터 조회 실패는 하루 1번 cwally-ops.
usage: kal_monitor.py <auto|dry> | selftest | confirm
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402
from kr_calendar import session_close_ready  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "kal.json"
STATE_PATH = ROOT / "state" / "kal.json"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
TAG = "[KAL] 003490 대한항공"
HL: dict[str, tuple[float, float]] = {}  # day -> (high, low) of the regular-session bar, filled by price_rows
BAD_WORDS = ("분할매수", "분할 매수", "매수 기회", "매수 구간", "저가 매수", "사세요", "진입 검토", "담을", "비중 확대", "지금 매수",
             "매수 검토", "매수하세요", "사 모으")


def won(v: float) -> str:
    return f"{v:,.0f}"


def get(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as resp:
        return resp.read()


def price_rows(code: str) -> list[tuple[str, float]]:
    import krx_close
    bars = krx_close.regular_bars(code, 260)
    if code == "003490":
        HL.update({b.day: (b.high, b.low) for b in bars})
    return [(b.day, b.close) for b in bars]


def confirmed(rows: list[tuple[str, float]], now: datetime) -> list[tuple[str, float]]:
    today = now.strftime("%Y-%m-%d")
    return [r for r in rows if session_close_ready(r[0], today, now, (15, 31))]


def fuel_price(symbol: str) -> float:
    d = json.loads(get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d"))["chart"]["result"][0]
    p = d["meta"].get("regularMarketPrice")
    if not p:
        closes = [c for c in d["indicators"]["quote"][0]["close"] if c]
        p = closes[-1] if closes else None
    if not p:
        raise ValueError("HO=F 시세 없음")
    return float(p)


def load_notices(code: str) -> list[dict]:
    import naver_api
    return [{**n, "src": "naver"} for n in naver_api.notices(code, 30)]


def dart_list(cik: str, now: datetime, days: int = 30) -> list[dict]:
    """DART 공시 목록(제목만). [{src, id(rcpNo), title, datetime, text}] newest first, filer 대한항공 only."""
    import html as _html
    import urllib.parse
    data = urllib.parse.urlencode({"textCrpNm": "대한항공", "textCrpCik": cik, "startDate": (now - timedelta(days=days)).strftime("%Y%m%d"),
                                   "endDate": now.strftime("%Y%m%d"), "maxResults": 100, "currentPage": 1, "sort": "date",
                                   "series": "desc"}).encode()
    req = urllib.request.Request("https://dart.fss.or.kr/dsab007/detailSearch.ax", data=data, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8", "ignore")
    out = []
    for row in re.findall(r"<tr>.*?</tr>", raw, re.S):
        m = re.search(r'rcpNo=(\d+)"[^>]*>(.*?)</a>', row, re.S)
        if not m:
            continue
        title = _html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(2)))).strip()
        filer = re.search(r'class="tL ellipsis" title="([^"]*)"', row)
        day = re.search(r"<td>(\d{4})\.(\d{2})\.(\d{2})</td>", row)
        if filer and "대한항공" not in filer.group(1):
            continue
        out.append({"src": "dart", "id": m.group(1), "title": title, "text": "",
                    "datetime": f"{day.group(1)}-{day.group(2)}-{day.group(3)}" if day else m.group(1)[:8]})
    if "rcpNo=" not in raw and "조회 결과가 없습니다" not in raw and "검색된 자료가 없습니다" not in raw:
        raise ValueError("DART 목록 형식 확인 불가")
    return out


def wti_settle(symbol: str) -> float:
    d = json.loads(get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1d&interval=5m"))["chart"]["result"][0]
    v = d["meta"].get("chartPreviousClose") or d["meta"].get("previousClose")
    if not v:
        raise ValueError("CL=F 직전 정산가 없음")
    return float(v)


# ---------- 판정 (순수 함수) ----------
def p_hit(ln: dict, p: float, hl: tuple[float, float] | None = None) -> bool:
    if ln.get("basis") == "hl" and hl:
        p = min(p, hl[1]) if ln["kind"] == "down" else max(p, hl[0])  # close OR intraday low/high
    return p <= ln["level"] if ln["kind"] == "down" else p >= ln["level"]


def p_rearm(ln: dict, p: float) -> bool:
    return p >= ln["rearm"] if ln["kind"] == "down" else p < ln["rearm"]


def judge_price(lines: list[dict], armed: dict, p: float, hl: tuple[float, float] | None = None) -> list[dict]:
    keys = {ln["key"] for ln in lines}
    for k in [k for k in armed if k not in keys]:  # line removed from config (u38500 -> u40000, 2026-11-03)
        armed.pop(k)
    fired = []
    for ln in lines:
        k = ln["key"]
        if k not in armed:  # new line seeds silently
            armed[k] = not p_hit(ln, p, hl)
            continue
        if armed[k] and p_hit(ln, p, hl):
            armed[k] = False
            fired.append(ln)
        elif not armed[k] and p_rearm(ln, p):
            armed[k] = True
    return fired


def judge_pref(pc: dict, ps: dict, disc: float) -> bool:
    """disc in %. Seeds silently. Fires once at >= min, re-arms below rearm."""
    if "armed" not in ps:
        ps["armed"] = disc < pc["min_discount_pct"]
        return False
    if ps["armed"] and disc >= pc["min_discount_pct"]:
        ps["armed"] = False
        return True
    if not ps["armed"] and disc < pc["rearm_pct"]:
        ps["armed"] = True
    return False


def add_settle(ws: dict, day: str, v: float) -> bool:
    """Append a US settle observed on KST run day `day`. Same value as last (holiday) or same day -> replace/skip.
    Returns True when a new settle was added."""
    hist = ws.setdefault("hist", [])
    if hist and hist[-1][0] == day:
        hist[-1] = [day, v]
        return False
    if hist and abs(hist[-1][1] - v) < 1e-9:
        return False
    hist.append([day, v])
    ws["hist"] = hist[-10:]
    return True


def judge_wti(wc: dict, ws: dict) -> bool:
    hist = ws.get("hist") or []
    if not hist:
        return False
    last = [v for _, v in hist[-int(wc["days"]):]]
    hit = len(last) == int(wc["days"]) and all(v >= wc["level"] for v in last)
    if "armed" not in ws:
        ws["armed"] = not hit
        return False
    if ws["armed"] and hit:
        ws["armed"] = False
        return True
    if not ws["armed"] and hist[-1][1] < wc["rearm"]:
        ws["armed"] = True
    return False


def judge_fuel(fc: dict, fs: dict, price: float) -> tuple[str | None, float]:
    """fs: {baseline, up_armed, down_armed}. Returns (fired 'up'|'down'|None, pct vs baseline)."""
    base = fc.get("baseline") or fs.get("baseline")
    if not base:
        fs.update({"baseline": price, "up_armed": True, "down_armed": True})
        return None, 0.0
    pct = (price / base - 1) * 100
    fired = None
    if fs.get("up_armed", True):
        if pct >= fc["up_pct"]:
            fs["up_armed"], fired = False, "up"
    elif pct < fc["up_rearm_pct"]:
        fs["up_armed"] = True
    if fs.get("down_armed", True):
        if pct <= fc["down_pct"]:
            fs["down_armed"], fired = False, "down"
    elif pct > fc["down_rearm_pct"]:
        fs["down_armed"] = True
    return fired, pct


def _t(n: dict) -> str:
    return (n.get("title") or "").replace(" ", "")


def is_rights(n: dict) -> bool:
    return "유상증자결정" in _t(n)


def is_merger_done(n: dict) -> bool:
    t = _t(n)
    if "합병등종료보고서" in t:
        return True
    return n.get("src") == "naver" and "합병" in t and ("종료" in t or "완료" in t) and "아시아나" in t + (n.get("text") or "")


def is_merger_change(n: dict) -> bool:
    t, body = _t(n), n.get("text") or ""
    if "정정" not in t or is_merger_done(n):
        return False
    if n.get("src") == "dart":
        return "회사합병결정" in t and "종속회사" not in t
    return "합병" in t and "종속회사" not in t and "아시아나" in t + body and ("합병기일" in body or "일정" in body)


def is_q3(n: dict) -> bool:
    t = _t(n)
    return "분기보고서(2026.09)" in t or "연결재무제표기준영업(잠정)실적" in t


def is_q4(n: dict) -> bool:
    return "영업(잠정)실적" in _t(n)


def classify_filing(ec: dict, n: dict) -> dict | None:
    t = _t(n)
    for r in (ec.get("filings") or {}).get("rules") or []:
        if re.search(r["re"], t):
            return r
    return None


def filing_key(n: dict, rule_key: str, today: str) -> str:
    return f"{(n.get('datetime') or today)[:10]}|{rule_key}|{'정정' in _t(n)}"


def judge_events(ec: dict, es: dict, notices: list[dict], today: str, sources: tuple[str, ...] = ("naver", "dart")) -> list[tuple[str, dict | None]]:
    """notices from the sources fetched this run. A source seen for the first time seeds silently.
    es: seen ['src:id'], seeded [src], flags rights_found, merger_done, merger_reminded, merger_delay_sent, q3_found,
    q4_found, q4_reminded, change_days."""
    out: list[tuple[str, dict | None]] = []
    seen = set(x if ":" in x else f"naver:{x}" for x in es.get("seen") or [])  # pre-2026-11-03 ids were naver only
    seeded = set(es.get("seeded") or (["naver"] if es.get("seen") else []))
    m, q3, q4 = ec["merger"], ec["q3"], ec["q4"]
    for src in sources:
        if src not in seeded:
            seen |= {f"{src}:{n['id']}" for n in notices if n["src"] == src}
            seeded.add(src)
    fkeys = set(es.get("filing_keys") or [])
    for n in reversed(notices):  # oldest first
        key = f"{n['src']}:{n['id']}"
        if key in seen:
            continue
        seen.add(key)
        if (is_q3(n) and not (q3["start"] <= today <= q3["end"] and not es.get("q3_found"))) or \
                (not is_q3(n) and is_q4(n) and not (q4["watch_start"] <= today <= q4["watch_end"] and not es.get("q4_found"))):
            pass  # outside the dedicated window: handled by the generic filings rules below
        elif is_q3(n) or is_q4(n):
            fkeys.add(filing_key(n, "earn", today))  # the other source's copy of the same filing stays quiet
        if is_rights(n):
            if not es.get("rights_found"):
                es["rights_found"] = today
                out.append(("rights", n))
        elif is_merger_done(n):
            if m["start"] <= today <= m["end"] and not es.get("merger_done"):
                es["merger_done"] = today
                out.append(("merger_done", n))
        elif is_merger_change(n):
            d = (n.get("datetime") or today)[:10]
            days = es.setdefault("change_days", [])
            if today <= m["end"] and not es.get("merger_done") and d not in days:
                days.append(d)
                out.append(("merger_change", n))
        elif is_q3(n) and q3["start"] <= today <= q3["end"] and not es.get("q3_found"):
            es["q3_found"] = today
            out.append(("q3", n))
        elif is_q4(n) and q4["watch_start"] <= today <= q4["watch_end"] and not es.get("q4_found"):
            es["q4_found"] = today
            out.append(("q4", n))
        else:
            r = classify_filing(ec, n)
            if r:
                fk = filing_key(n, r["key"], today)
                if fk not in fkeys:
                    fkeys.add(fk)
                    out.append(("filing", {**n, "rule": r}))
    es["filing_keys"] = sorted(fkeys)[-200:]
    es["seen"] = sorted(seen)[-400:]
    es["seeded"] = sorted(seeded)
    if m.get("pre_reminder") and m["pre_reminder"] <= today <= m["launch"] and not es.get("merger_done") and not es.get("merger_pre_sent"):
        es["merger_pre_sent"] = today
        out.append(("merger_pre", None))
    if m["reminder"] <= today <= m["end"] and not es.get("merger_done") and not es.get("merger_reminded"):
        es["merger_reminded"] = today
        out.append(("merger_rem", None))
    if m["delay_date"] <= today and not es.get("merger_done") and not es.get("merger_delay_sent"):
        es["merger_delay_sent"] = today
        out.append(("merger_delay", None))
    if q4["reminder"] <= today <= q4["watch_end"] and not es.get("q4_found") and not es.get("q4_reminded"):
        es["q4_reminded"] = today
        out.append(("q4_rem", None))
    return out


def merger_status(es: dict) -> str:
    if es.get("merger_done"):
        return f"통합 완료 공시 확인({es['merger_done']})"
    if es.get("change_days"):
        return f"합병 일정 정정 공시 있음({', '.join(es['change_days'][-2:])}), 12/17 유지 여부 원문 확인 필요"
    return "12/17 일정 변경 공시 없음(DART, 네이버 공시 감시 중)"


# ---------- 문구 ----------
def n_ref(n: dict) -> str:
    return f"'{n['title']}' ({(n.get('datetime') or '')[:10]}, {'DART' if n.get('src') == 'dart' else '네이버 공시'})"


def item_text(kind: str, obj, cfg: dict, ctx: dict) -> tuple[str, str, int]:
    """(title tail, 지금 할 일 문장, priority)"""
    ev = cfg["events"]
    p = ctx.get("close", (None, None))[1]
    if kind == "price":
        k = obj["key"]
        hl = ctx.get("hl")
        ext = ""
        if obj.get("basis") == "hl" and hl:
            v = hl[0] if obj["kind"] == "up" else hl[1]
            if not p_hit(obj, p):  # close alone did not cross: the intraday high/low did
                ext = f"({'고가' if obj['kind'] == 'up' else '저가'} {won(v)})"
        tail = f"종가 {won(p)}{ext} | {obj['label']}"
        if k == "d26500":
            w = ctx.get("wti_last")
            wtxt = f"WTI 최근 정산 {w:.2f}달러, {'110 이상' if w >= 110 else '110 미만'}" if w else "WTI 확인 불가"
            wok = "재안착 아님" if not ctx.get("wti_110") else "2일 연속 110 이상, 조건 미충족"
            plan = cfg["plan"]
            drop = " 유상증자 공시가 있어 폐기 조건에 해당합니다." if ctx.get("rights") else ""
            return (tail + " | 분할 매수 검토",
                    f"조건 1 점검표: 1) 종가 또는 장중 저가 26,500 이하 맞음 2) {ctx.get('merger', '합병 상태 확인 불가')} 3) {wtxt}, {wok} "
                    f"4) 참고 200일선 {won(ctx['ma']) if ctx.get('ma') else '계산 불가'}원. 셋 다 맞으면 1차 지정가를 검토하세요(주문 아님). "
                    f"규모: {plan['sizing']}. {plan['account']}, {plan['vehicle']}.{drop}", obj["priority"])
        if k == "d24000":
            return (tail + " | 신규 매수 계획 재점검",
                    "비관 시나리오 가격입니다. 협상 결렬로 유가 재급등, 자회사 적자 지속, 증자 중 무엇이 반영됐는지 확인하고 신규 계획을 다시 따져 보세요.", obj["priority"])
        if k == "u32500":
            return (tail + " | 추격 매수 보류",
                    "52주 고점(32,500)을 넘었고 기본 시나리오(33,000, 삼성 목표가 32,000 보유)에 가까워졌습니다. 평균 목표가 약 37,727원은 2027년 이익 급증 전제라, "
                    "추격 매수는 보류하고 통합 진행, 유가, 2027년 이익 추정 변화를 다시 확인하세요.", obj["priority"])
        return (tail + " | 밸류 재점검",
                "낙관 시나리오 가격입니다. 급유단가 하락과 2027년 연결 영업이익 2.5조원 경로가 실제로 확인되는지 다시 따져 보세요.", obj["priority"])
    if kind == "pref":
        pc = cfg["preferred"]
        return (f"003495 우선주 할인율 {ctx['pref'][2]:.1f}% | 우선주 부차 베팅 검토 구간, 300만원 이하",
                f"우선주 {won(ctx['pref'][1])}원, 보통주 {won(p)}원, 할인율 {ctx['pref'][2]:.1f}%(기준 {pc['min_discount_pct']:g}% 이상). "
                f"부차 베팅은 {won(pc['max_won'])}원 이하만 검토하세요. 거래량이 적어 지정가로만.", pc["priority"])
    if kind == "wti":
        h = ctx["wti_hist"][-2:]
        return (f"WTI {h[-1][1]:.2f}달러 | WTI 110달러 재안착, 1차 조건 미충족",
                f"WTI 정산가 2일 연속 110 이상({', '.join(f'{v:.2f}' for _, v in h)}). 1차 지정가 조건이 깨졌습니다. 신규 매수 보류, 연료비 전망을 확인하세요.",
                cfg["wti"]["priority"])
    if kind == "fuel":
        price, pct = ctx["fuel"]
        if obj == "up":
            return (f"제트유 대용 HO=F {price:.3f}달러 ({pct:+.1f}%) | 유가 부담 확대 | 신규 매수 보류",
                    "연료비 부담이 커졌습니다. 4분기, 2027년 영업이익 전망 하향 여부를 확인하세요.", cfg["fuel"]["priority"])
        return (f"제트유 대용 HO=F {price:.3f}달러 ({pct:+.1f}%) | 유가 안정 신호 | 근거 재확인",
                "연료비 부담이 줄어드는 신호입니다. 급유단가 104달러/배럴(약 2.48달러/갤런) 아래가 실제 확인되는지 보세요.", cfg["fuel"]["priority"])
    if kind == "rights":
        return ("유상증자 결정 공시 | 신규 계획 폐기 조건",
                f"공시 {n_ref(obj)}. 판단 조건 3의 폐기 조건입니다. 신규 매수 계획을 멈추고 증자 규모와 가격을 원문으로 확인하세요.", ev["rights"]["priority"])
    if kind == "merger_done":
        return ("아시아나 통합 완료 공시 | 조건 2 점검 시작",
                f"공시 {n_ref(obj)}. 통합 완료가 확인되면 조건 2(4분기 별도 영업이익률 8% 근처, 급유단가 104달러 아래)를 이어서 봅니다.", ev["merger"]["priority"])
    if kind == "merger_change":
        return ("아시아나 합병 일정 정정 공시 | 12/17 유지 여부 확인",
                f"공시 {n_ref(obj)}. 합병기일이 바뀌었는지 원문을 확인하세요. 2027년 1분기 이후로 밀리면 신규 계획 폐기 조건입니다.", ev["merger"]["priority"])
    if kind == "merger_rem":
        return ("통합 일정(12/17) 확인 필요 | 완료 공시 아직 없음",
                "12/17 출범 예정이던 통합의 합병등종료보고서가 아직 안 보입니다. 등기나 보고 지연인지, 일정 자체가 밀렸는지 확인하세요.", ev["merger"]["priority"])
    if kind == "merger_delay":
        return ("합병 2027년 1분기 이후 지연 | 신규 계획 폐기 조건",
                "2027-03-31 까지 통합 완료 공시가 없습니다. 판단 조건 3의 폐기 조건입니다. 신규 매수 계획을 멈추세요.", ev["merger"]["delay_priority"])
    if kind == "merger_pre":
        return ("내일 12/16 합병기일, 12/17 합병등기 | 일정 확인",
                "아시아나 합병기일은 내일 12/16, 합병등기는 12/17 입니다. 아시아나는 이미 연결 반영 중이라 매출이 새로 더해지는 이벤트가 아닙니다. "
                "일정 변경 공시가 없는지, 합병 뒤 통합비용과 순차입금 안내만 확인하세요. 합병 당일 추격 매수 금지.", ev["merger"]["priority"])
    if kind == "filing":
        r = obj["rule"]
        link = (f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={obj['id']}" if obj.get("src") == "dart"
                else "https://finance.naver.com/item/news_notice.naver?code=003490")
        what = {"earn": "연결과 별도를 구분해 영업이익, 2027년 이익 추정에 주는 영향, 외화환산손익 같은 일회성을 확인하세요.",
                "debt": "발행 금액, 금리, 용도를 보고 순차입금이 얼마나 늘었는지 확인하세요. 유상증자면 신규 계획 폐기 조건입니다.",
                "merger": "합병 일정(12/16 합병기일, 12/17 등기)과 비율, 통합비용 변화가 있는지 원문을 확인하세요.",
                "contract": "계약 금액, 상대방, 기간, 매출액 대비 비율을 확인하세요."}.get(r["key"], "원문을 확인하세요.")
        corr = " 정정" if "정정" in _t(obj) else ""
        return (f"새 공시 {r['label']}{corr} | 원문 확인", f"공시 {n_ref(obj)} {link}. {what}", r["priority"])
    if kind == "q3":
        return ("3분기 연결 실적 공시 | 연결 영업이익, 자회사 손실 확인",
                f"공시 {n_ref(obj)}. 별도 잠정(10/15)에 없던 연결 영업이익과 자회사(LCC 포함) 손익, 순외화부채를 확인하세요.", ev["q3"]["priority"])
    if kind == "q4":
        return ("4분기 잠정실적 공시 | 별도 영업이익률 8% 확인",
                f"공시 {n_ref(obj)}. 별도 영업이익률이 8% 근처인지(조건 2), 연료비와 일회성 인건비를 확인하세요.", ev["q4"]["priority"])
    return ("4분기 잠정실적 발표 예상 전날 | 확인 준비",
            "최근 패턴상 내일(1월 중순) 4분기 잠정실적이 나올 수 있습니다. 별도 영업이익률 8% 기준과 연료비 가정을 미리 보세요.", ev["q4"]["priority"])


WARN_KINDS = {"d24000", "wti", "fuel_up", "rights", "merger_change", "merger_rem", "merger_delay"}


def item_key(kind: str, obj) -> str:
    return obj["key"] if kind == "price" else (f"fuel_{obj}" if kind == "fuel" else kind)


def message(cfg: dict, items: list[tuple[str, object]], stamp: str, ctx: dict) -> tuple[str, str, int]:
    texts = [item_text(k, o, cfg, ctx) for k, o in items]
    order = sorted(range(len(texts)), key=lambda i: -texts[i][2])
    top = texts[order[0]]
    title = f"{TAG} {top[0]}" + (f" 외 {len(texts) - 1}건" if len(texts) > 1 else "")
    st = []
    if ctx.get("close"):
        st.append(f"{ctx['close'][0]} 확정 종가 {won(ctx['close'][1])}원")
    if ctx.get("ma"):
        st.append(f"200일선 {won(ctx['ma'])}원")
    if ctx.get("pref"):
        st.append(f"우선주 {won(ctx['pref'][1])}원(할인율 {ctx['pref'][2]:.1f}%)")
    if ctx.get("wti_last"):
        st.append(f"WTI {ctx['wti_last']:.2f}달러")
    if ctx.get("fuel") and ctx.get("fuel_base"):
        st.append(f"제트유 대용 HO=F {ctx['fuel'][0]:.3f}달러(기준 {ctx['fuel_base']:.3f}, {ctx['fuel'][1]:+.1f}%, 급유단가 조건 104달러/배럴은 약 2.48달러/갤런 참고)")
    if ctx.get("merger"):
        st.append(ctx["merger"])
    buy_review = any(item_key(k, o) == "d26500" for k, o in items)
    body = "\n".join([
        "이게 뭐 알림: 대한항공(003490) 관심 종목 알림입니다. 종가 시나리오 선, 우선주 할인율, WTI, 제트유 대용, 합병과 증자, 실적 공시가 바뀔 때만 옵니다. 미보유 기준이고 지정가와 일정은 주문이 아닙니다.",
        f"{stamp} | 003490 | 대한항공",
        "지금 결과/상태: " + ", ".join(st) + ". 걸린 것: " + "; ".join(texts[i][0] for i in order) + ".",
        "지금 할 일: " + " ".join(texts[i][1] for i in order),
        "나중에: 같은 선은 다시 무장된 뒤에만 다시 옵니다(26,500 은 27,295 위, 24,000 은 24,700 위 회복, 32,500 은 31,525 아래, 40,000 은 38,500 아래, "
        f"우선주 할인율 25% 아래, WTI 105 아래, 제트유 대용 +5%, -10% 안). 통합 출범 예정 12/17. 종료 {cfg['ends_kst'][:10]}.",
        ("익절/줄일 때: 아직 미보유. 1차 뒤에는 비관 24,000 과 폐기 조건(합병 지연, 2027년 연결 영업이익 1.5조원 아래, 유상증자)으로만 줄일지 정하세요."
         if buy_review else "익절/줄일 때: 미보유라 해당 없음. 보유하게 되면 경고 때는 비중부터 점검하세요."),
        "하지 말 것: 추격 매수, 신용, 미수, 우선주 300만원 넘게, 하루 움직임만 보고 결정하기, 경고를 반등 기회로 읽기.",
    ])
    return title, body, max(t[2] for t in texts)


# ---------- 실행 ----------
def ops(stamp: str, what: str, err: str) -> tuple[str, str, int]:
    body = "\n".join([
        "이게 뭐 알림: 대한항공(003490) 알림의 데이터 조회 실패 안내. 매매 신호 아님.",
        f"{stamp} | 003490 | 대한항공",
        f"지금 결과/상태: {what} 확인 불가 ({err[:150]}).",
        "지금 할 일: 필요하면 네이버 증권, DART, Yahoo 에서 직접 보세요.",
        "나중에: 다음 평일 15:40 에 다시 봅니다.",
        "익절/줄일 때: 이 안내로 판단을 바꾸지 않기.",
        "하지 말 것: 조회 실패를 조건 미충족으로 읽기.",
    ])
    return f"{TAG} {what} 조회 실패 | 판정 보류", body, 3


def run(mode: str, now: datetime, cfg: dict) -> int:
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    today = now.strftime("%Y-%m-%d")
    st = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    if mode == "auto" and now.weekday() >= 5:
        print("weekend; silent")
        return 0
    items: list[tuple[str, object]] = []
    fails: list[tuple[str, str]] = []
    ctx: dict = {}
    new_day = False
    try:
        rows = confirmed(price_rows(cfg["code"]), now)
        if not rows:
            raise ValueError("확정 종가 없음")
        close = rows[-1]
        cl = [c for _, c in rows]
        ctx["close"], ctx["ma"] = close, (sum(cl[-200:]) / 200 if len(cl) >= 200 else None)
        new_day = close[0] != st.get("last_date")
        st["last_close"], st["ma200"] = list(close), round(ctx["ma"], 1) if ctx["ma"] else None
    except Exception as exc:  # noqa: BLE001
        fails.append(("종가", str(exc)))
    try:
        if ctx.get("close"):
            pr = dict(confirmed(price_rows(cfg["preferred"]["code"]), now))
            if ctx["close"][0] in pr:
                pp = pr[ctx["close"][0]]
                ctx["pref"] = (ctx["close"][0], pp, (1 - pp / ctx["close"][1]) * 100)
                st["pref_last"] = [ctx["close"][0], pp, round(ctx["pref"][2], 2)]
    except Exception as exc:  # noqa: BLE001
        fails.append(("우선주", str(exc)))
    try:
        ws = st.setdefault("wti", {})
        add_settle(ws, today, wti_settle(cfg["wti"]["symbol"]))
        ctx["wti_hist"] = ws["hist"]
        ctx["wti_last"] = ws["hist"][-1][1]
        if judge_wti(cfg["wti"], ws):
            items.append(("wti", None))
        ctx["wti_110"] = not ws.get("armed", True)
    except Exception as exc:  # noqa: BLE001
        fails.append(("WTI", str(exc)))
    try:
        fp = fuel_price(cfg["fuel"]["symbol"])
        fs = st.setdefault("fuel", {})
        seeded = not (cfg["fuel"].get("baseline") or fs.get("baseline"))
        f, pct = judge_fuel(cfg["fuel"], fs, fp)
        if seeded:
            fs["baseline_at"] = stamp
        ctx["fuel"], ctx["fuel_base"] = (fp, pct), cfg["fuel"].get("baseline") or fs.get("baseline")
        fs["last"] = [stamp, fp, round(pct, 2)]
        if f:
            items.append(("fuel", f))
    except Exception as exc:  # noqa: BLE001
        fails.append(("HO=F", str(exc)))
    notices, srcs = [], []
    for src, fn in (("naver", lambda: load_notices(cfg["code"])), ("dart", lambda: dart_list("00113526", now))):
        try:
            notices += fn()
            srcs.append(src)
        except Exception as exc:  # noqa: BLE001
            fails.append(("DART 공시" if src == "dart" else "네이버 공시", str(exc)))
    es = st.setdefault("events", {})
    items += judge_events(cfg["events"], es, notices, today, tuple(srcs))
    ctx["merger"], ctx["rights"] = merger_status(es), bool(es.get("rights_found"))
    if ctx.get("close") and new_day:  # price-based lines judged after events so the 26,500 checklist sees today's DART state
        ctx["hl"] = HL.get(ctx["close"][0])
        items = [("price", ln) for ln in judge_price(cfg["price_lines"], st.setdefault("armed", {}), ctx["close"][1], ctx["hl"])] + items
        if ctx.get("pref") and judge_pref(cfg["preferred"], st.setdefault("pref", {}), ctx["pref"][2]):
            items.append(("pref", None))
        st["last_date"] = ctx["close"][0]
    elif ctx.get("close"):  # same bar: only seed lines or the pref line that are new in config (no judging, no send)
        armed = st.setdefault("armed", {})
        keys = {ln["key"] for ln in cfg["price_lines"]}
        for k in [k for k in armed if k not in keys]:
            armed.pop(k)
        for ln in cfg["price_lines"]:
            armed.setdefault(ln["key"], not p_hit(ln, ctx["close"][1], HL.get(ctx["close"][0])))
        if ctx.get("pref") and "armed" not in st.setdefault("pref", {}):
            judge_pref(cfg["preferred"], st["pref"], ctx["pref"][2])
    print(f"close={ctx.get('close')} ma200={round(ctx['ma']) if ctx.get('ma') else None} pref={ctx.get('pref')} wti={ctx.get('wti_last')} "
          f"fuel={ctx.get('fuel')} armed={st.get('armed')} pref_armed={(st.get('pref') or {}).get('armed')} wti_armed={(st.get('wti') or {}).get('armed')} "
          f"merger={ctx['merger']} sources={srcs}")
    out = []
    if items:
        out.append(message(cfg, items, stamp, ctx))
    if fails and st.get("fail_day") != today:
        st["fail_day"] = today
        out.append(ops(stamp, ", ".join(w for w, _ in fails), "; ".join(e for _, e in fails)))
    st["updated_at"] = stamp
    for t, m, pri in out:
        print(t)
        print(m)
        if not dry:
            publish(resolve_topic("port", cfg.get("ntfy_topic")), t, m, pri, ["warning"] if pri >= 4 else ["airplane"],
                    resolve_server(cfg.get("ntfy_server")), config_name="kal")
            st["last_sent"] = {"at": stamp, "title": t}
    if not out:
        print("no alert")
    if dry:
        print("DRY RUN: no publish, state not written")
    else:
        STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


def selftest(cfg: dict) -> int:
    fails = 0

    def check(label, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(("ok  " if ok else "FAIL"), label, "" if ok else f"got {got!r} want {want!r}")
    LA = cfg["price_lines"]
    L = LA  # close-only walk (hl None); the 26,500 low cross is tested in the hl walk
    armed: dict = {"d26500": True, "d24000": True, "u32500": True, "u38500": True}
    check("migration: u38500 dropped, u40000 seeds silent", (judge_price(L, armed, 29900), sorted(armed)), ([], ["d24000", "d26500", "u32500", "u40000"]))
    check("all armed", all(armed.values()), True)
    walk = [26500, 26000, 27294, 26400, 27300, 26450, 23900, 24699, 23000, 24700, 32500, 33000, 31500, 31499, 32600, 40000, 38600, 38499, 40100]
    got = [[x["key"] for x in judge_price(L, armed, p)] for p in walk]
    check("price walk", got, [["d26500"], [], [], [], [], ["d26500"], ["d24000"], [], [], [], ["u32500"], [], [], [], ["u32500"], ["u40000"], [], [], ["u40000"]])
    a2 = {k: True for k in ("d26500", "d24000", "u32500", "u40000")}
    a2x = {**a2, "d26000": False}
    check("removed d26000 key dropped from old state", (judge_price(LA, a2x, 29150, (29500, 28800)), "d26000" in a2x, a2x["d26500"]), ([], False, True))
    hl_walk = [(29150, (32000, 28900), []), (32000, (32500, 31000), ["u32500"]), (32400, (33000, 31800), []), (31600, (32000, 31400), []),
               (31500, (32000, 31000), []), (32100, (32550, 31500), ["u32500"]), (27000, (27500, 26000), ["d26500"]),
               (26500, (26900, 26100), []), (27294, (27500, 26600), []), (27295, (27500, 26900), []), (27000, (27400, 26500), ["d26500"]),
               (27100, (27200, 26400), []), (23900, (24500, 23800), ["d24000"])]
    a3 = {k: True for k in ("d26500", "d24000", "u32500", "u40000")}
    got = [[x["key"] for x in judge_price(LA, a3, c, hl)] for c, hl, _ in hl_walk]
    check("hl walk (high/low cross, 3% close re-arm)", got, [w for _, _, w in hl_walk])
    check("hl missing -> close only", [x["key"] for x in judge_price(LA, {k: True for k in a3}, 32400, None)], [])
    pc = cfg["preferred"]
    ps: dict = {}
    check("pref seed silent at 24.6%", (judge_pref(pc, ps, 24.6), ps), (False, {"armed": True}))
    check("pref walk", [judge_pref(pc, ps, d) for d in (27.9, 28.0, 30, 25.1, 25.0, 24.9, 28.5)], [False, True, False, False, False, False, True])
    wc = cfg["wti"]
    ws: dict = {}
    add_settle(ws, "2026-11-03", 96.46)
    check("wti seed silent", (judge_wti(wc, ws), ws["armed"]), (False, True))
    seq = [("11-04", 110.5), ("11-04", 110.6), ("11-05", 111), ("11-06", 112), ("11-09", 106), ("11-10", 104.9), ("11-11", 110), ("11-12", 110.2)]
    res = []
    for d, v in seq:
        add_settle(ws, "2026-" + d, v)
        res.append(judge_wti(wc, ws))
    check("wti two-day 110, re-arm < 105", res, [False, False, True, False, False, False, False, True])
    ws2 = {"hist": [["2026-11-05", 111.0]], "armed": True}
    check("holiday repeat not a new day", (add_settle(ws2, "2026-11-06", 111.0), judge_wti(wc, ws2)), (False, False))
    fc = cfg["fuel"]
    fs: dict = {}
    check("fuel seed", (judge_fuel(fc, fs, 4.424)[0], fs["baseline"]), (None, 4.424))
    check("fuel walk", [judge_fuel(fc, fs, x)[0] for x in [4.6, 4.87, 5.2, 4.66, 4.64, 4.87, 3.77, 3.76, 3.70, 3.98, 3.99, 3.75]],
          [None, "up", None, None, None, "up", None, "down", None, None, None, "down"])
    ec = cfg["events"]
    nav_old = {"src": "naver", "id": "1", "title": "(주)대한항공 (정정)회사합병 결정", "datetime": "2026-06-26T06:50:13", "text": "아시아나항공 합병기일"}
    dart_old = {"src": "dart", "id": "20261030801318", "title": "[기재정정]타법인주식및출자증권취득결정", "datetime": "2026-10-30", "text": ""}
    es = {"seen": ["1"]}
    check("dart seeds silently, old naver ids migrate", judge_events(ec, es, [nav_old, dart_old], "2026-11-03"), [])
    check("seeded both", (es["seeded"], "naver:1" in es["seen"]), (["dart", "naver"], True))
    import copy
    q3 = {"src": "dart", "id": "20261116000001", "title": "분기보고서 (2026.09)", "datetime": "2026-11-16", "text": ""}
    e = copy.deepcopy(es)
    check("q3 report in window", [k for k, _ in judge_events(ec, e, [q3, nav_old, dart_old], "2026-11-16")], ["q3"])
    e = copy.deepcopy(es)
    check("q3 after window ignored", [k for k, _ in judge_events(ec, e, [q3], "2026-12-01")], [])
    rights = {"src": "dart", "id": "20261120000002", "title": "주요사항보고서(유상증자결정)", "datetime": "2026-11-20", "text": ""}
    rights_n = {"src": "naver", "id": "77", "title": "(주)대한항공 유상증자결정", "datetime": "2026-11-20T07:00:00", "text": ""}
    e = copy.deepcopy(es)
    check("rights once across sources", [k for k, _ in judge_events(ec, e, [rights, rights_n], "2026-11-20")], ["rights"])
    chg = {"src": "dart", "id": "20261201000003", "title": "[기재정정]주요사항보고서(회사합병결정)", "datetime": "2026-12-01", "text": ""}
    sub = {"src": "dart", "id": "20261201000004", "title": "[기재정정]회사합병결정(종속회사의주요경영사항)", "datetime": "2026-12-01", "text": ""}
    e = copy.deepcopy(es)
    check("merger schedule change; subsidiary merger only as generic p3 filing", [(k, (o or {}).get("rule", {}).get("key")) for k, o in judge_events(ec, e, [chg, sub], "2026-12-01")],
          [("filing", "merger"), ("merger_change", None)])
    done = {"src": "dart", "id": "20261218000005", "title": "합병등종료보고서", "datetime": "2026-12-18", "text": ""}
    check("completion", [k for k, _ in judge_events(ec, e, [done], "2026-12-18")], ["merger_done"])
    check("found -> no 12/19 reminder, no 4/1", [k for k, _ in judge_events(ec, e, [], "2026-12-19")] + [k for k, _ in judge_events(ec, e, [], "2027-04-01")], [])
    e = copy.deepcopy(es)
    check("12/19 reminder when nothing", [k for k, _ in judge_events(ec, e, [], "2026-12-19")], ["merger_rem"])
    check("reminder once", [k for k, _ in judge_events(ec, e, [], "2026-12-21")], [])
    check("q4 reminder 1/14", [k for k, _ in judge_events(ec, e, [], "2027-01-14")], ["q4_rem"])
    q4n = {"src": "naver", "id": "9", "title": "(주)대한항공 영업(잠정)실적(공정공시)", "datetime": "2027-01-15T06:51:00", "text": ""}
    check("q4 notice", [k for k, _ in judge_events(ec, e, [q4n], "2027-01-15")], ["q4"])
    check("4/1 delay p4", [k for k, _ in judge_events(ec, e, [], "2027-04-01")], ["merger_delay"])
    check("delay once", [k for k, _ in judge_events(ec, e, [], "2027-04-02")], [])
    e = copy.deepcopy(es)
    check("12/14 no pre-reminder", [k for k, _ in judge_events(ec, e, [], "2026-12-14")], [])
    check("12/15 pre-reminder", [k for k, _ in judge_events(ec, e, [], "2026-12-15")], ["merger_pre"])
    check("pre-reminder once", [k for k, _ in judge_events(ec, e, [], "2026-12-16")], [])
    e2 = copy.deepcopy(es)
    e2["merger_done"] = "2026-12-14"
    check("no pre-reminder after completion", [k for k, _ in judge_events(ec, e2, [], "2026-12-15")], [])
    e3 = copy.deepcopy(es)
    check("no pre-reminder after 12/17", [k for k, _ in judge_events(ec, e3, [], "2026-12-18")], [])
    bond = {"src": "dart", "id": "20261110000010", "title": "주요사항보고서(신종자본증권발행결정)", "datetime": "2026-11-10", "text": ""}
    bond_n = {"src": "naver", "id": "501", "title": "(주)대한항공 사채권발행결정(신종자본증권)", "datetime": "2026-11-10T16:00:00", "text": ""}
    supply = {"src": "dart", "id": "20261110000011", "title": "단일판매ㆍ공급계약체결", "datetime": "2026-11-10", "text": ""}
    sub_m = {"src": "dart", "id": "20261110000012", "title": "회사합병결정(종속회사의주요경영사항)", "datetime": "2026-11-10", "text": ""}
    misc = {"src": "dart", "id": "20261110000013", "title": "임원ㆍ주요주주특정증권등소유상황보고서", "datetime": "2026-11-10", "text": ""}
    q3n = {"src": "naver", "id": "502", "title": "(주)대한항공 분기보고서 (2026.09)", "datetime": "2026-11-16T15:00:00", "text": ""}
    e = copy.deepcopy(es)
    ev1 = judge_events(ec, e, [misc, sub_m, supply, bond_n, bond], "2026-11-10")
    check("generic filings: bond p4 once across sources, supply, subsidiary merger", [(k, o["rule"]["key"]) for k, o in ev1],
          [("filing", "debt"), ("filing", "contract"), ("filing", "merger")])
    check("generic dedupe on rerun", judge_events(ec, e, [misc, sub_m, supply, bond_n, bond], "2026-11-10"), [])
    ev2 = judge_events(ec, e, [q3n, q3], "2026-11-16")
    check("q3 dedicated once, other source copy quiet", [k for k, _ in ev2], ["q3"])
    q3late = {"src": "dart", "id": "20261201000020", "title": "[기재정정]분기보고서 (2026.09)", "datetime": "2026-12-01", "text": ""}
    prelim27 = {"src": "dart", "id": "20270415000021", "title": "연결재무제표기준영업(잠정)실적(공정공시)", "datetime": "2027-04-15", "text": ""}
    check("earnings outside windows -> generic p4", [(k, o["rule"]["key"], o["rule"]["priority"]) for k, o in judge_events(ec, e, [prelim27], "2027-04-15") if k == "filing"],
          [("filing", "earn", 4)])
    check("분기보고서 correction after window: not a 잠정 title -> ignored", judge_events(ec, e, [q3late], "2026-12-01"), [])
    check("rights still once, dedicated", [k for k, _ in judge_events(ec, copy.deepcopy(es), [rights], "2026-11-20")], ["rights"])
    stamp = "2026-12-01 15:40 KST"
    base = {"close": ("2026-12-01", 26400.0), "ma": 26240.0, "pref": ("2026-12-01", 19000.0, 28.0), "wti_last": 97.1, "wti_110": False,
            "wti_hist": [["d", 111.0], ["e", 112.0]], "fuel": (4.9, 10.8), "fuel_base": 4.424, "merger": merger_status({}), "rights": False}
    t1, b1, p1 = message(cfg, [("price", L[0]), ("pref", None)], stamp, base)
    check("buy-review title p3", (t1, p1), ("[KAL] 003490 대한항공 종가 26,400 | 1차 지정가 26,500 이하 | 분할 매수 검토 외 1건", 3))
    d26 = next(x for x in LA if x["key"] == "d26500")
    u32 = next(x for x in LA if x["key"] == "u32500")
    td, bd, pd = message(cfg, [("price", d26)], stamp, {**base, "close": ("2026-12-01", 26700.0), "hl": (27000.0, 26400.0)})
    check("d26500 title (low crossed)", (td, pd), ("[KAL] 003490 대한항공 종가 26,700(저가 26,400) | 1차 지정가 26,500 이하 | 분할 매수 검토", 3))
    check("d26500 checklist says close or low", "종가 또는 장중 저가 26,500 이하 맞음" in bd, True)
    tu, bu, pu = message(cfg, [("price", u32)], stamp, {**base, "close": ("2026-12-01", 32100.0), "hl": (32550.0, 31500.0)})
    check("u32500 title (high crossed)", tu, "[KAL] 003490 대한항공 종가 32,100(고가 32,550) | 52주 고점 32,500 돌파 (기본 시나리오 33,000 근접) | 추격 매수 보류")
    tu2 = message(cfg, [("price", u32)], stamp, {**base, "close": ("2026-12-01", 32600.0), "hl": (32800.0, 32000.0)})[0]
    check("u32500 title (close crossed, no 고가 tag)", tu2.split(" | ")[0], "[KAL] 003490 대한항공 종가 32,600")
    tp_ = message(cfg, [("merger_pre", None)], "2026-12-15 15:40 KST", base)
    check("merger_pre title", (tp_[0], tp_[2]), ("[KAL] 003490 대한항공 내일 12/16 합병기일, 12/17 합병등기 | 일정 확인", 3))
    tf = [message(cfg, [("filing", o)], stamp, base) for _, o in ev1]
    check("filing titles", [(t, p_) for t, _, p_ in tf], [("[KAL] 003490 대한항공 새 공시 사채/증자 공시, 순차입금 점검 | 원문 확인", 4),
                                                        ("[KAL] 003490 대한항공 새 공시 단일판매/공급계약 공시 | 원문 확인", 3),
                                                        ("[KAL] 003490 대한항공 새 공시 합병 관련 공시 | 원문 확인", 3)])
    check("filing body dart link", "dart.fss.or.kr/dsaf001/main.do?rcpNo=" in tf[0][1], True)
    check("checklist values", all(x in b1 for x in ("12/17 일정 변경 공시 없음", "WTI 최근 정산 97.10달러, 110 미만", "재안착 아님", "200일선 26,240원",
                                                   "1차 500만원 내외", "최대 1,000만원", "일반계좌", "보통주 003490 만", "2.48달러/갤런")), True)
    check("pref title", message(cfg, [("pref", None)], stamp, base)[0], "[KAL] 003490 대한항공 003495 우선주 할인율 28.0% | 우선주 부차 베팅 검토 구간, 300만원 이하")
    warn_items = [("price", L[1]), ("wti", None), ("fuel", "up"), ("rights", rights), ("merger_change", chg), ("merger_rem", None), ("merger_delay", None)]
    wt, wb, wp = message(cfg, warn_items, stamp, {**base, "close": ("2026-12-01", 23900.0)})
    check("warn combo p4 first", (wt.split(" | ")[0], wp), ("[KAL] 003490 대한항공 종가 23,900", 4))
    check("no buy wording in warnings", [w for w in BAD_WORDS if w in wt + wb] + (["검토 구간"] if "지정가를 검토" in wb else []), [])
    each = [message(cfg, [it], stamp, {**base, "close": ("2026-12-01", 23900.0)}) for it in warn_items]
    check("no buy wording in each warning", [t for t, b, _ in each if any(w in t + b for w in BAD_WORDS) or "지정가를 검토" in b], [])
    others = [message(cfg, [it], stamp, {**base, "close": ("2026-12-01", 40100.0)}) for it in
              [("price", L[2]), ("price", L[3]), ("fuel", "down"), ("merger_done", done), ("q3", q3), ("q4", q4n), ("q4_rem", None)]]
    titles = [t1, wt, td, tu, tp_[0]] + [t for t, _, _ in each + others + tf]
    bodies = [b1, wb, bd, bu, tp_[1]] + [b for _, b, _ in each + others + tf]
    check("wti title", each[1][0], "[KAL] 003490 대한항공 WTI 112.00달러 | WTI 110달러 재안착, 1차 조건 미충족")
    check("delay p4", each[6][2], 4)
    check("ticker in titles", all(t.startswith("[KAL] 003490 대한항공 ") for t in titles), True)
    check("no buy wording in filing/merger_pre", [w for w in BAD_WORDS if w in tp_[0] + tp_[1] + "".join(t + b for t, b, _ in tf)], [])
    check("first line 이게 뭐 알림", all(b.split("\n")[0].startswith("이게 뭐 알림:") for b in bodies), True)
    check("six parts", [x.split(":")[0] for x in b1.split("\n")[2:]], ["지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"])
    check("no dot/dash", any(ch in "".join(titles + bodies) for ch in "\u00b7\u2014"), False)
    check("confirmed close before 15:31", confirmed([("2026-11-02", 1.0), ("2026-11-03", 2.0)], datetime(2026, 11, 3, 11, 0, tzinfo=KST))[-1][0], "2026-11-02")
    print("selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


def confirm(cfg: dict) -> int:
    st = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    lc = st.get("last_close") or ["기록 없음", 0]
    fs = st.get("fuel") or {}
    body = "\n".join([
        "이게 뭐 알림: 대한항공(003490) 관심 종목 알림 설치 확인입니다. 매매 신호 아님.",
        f"{datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')} | 003490 | 대한항공",
        f"지금 결과/상태: 확정 종가 {lc[0]} {won(lc[1])}원, 200일선 {won(st['ma200']) if st.get('ma200') else '계산 전'}원. "
        f"제트유 대용 HO=F 기준 {fs.get('baseline', 0):.3f}달러. 모든 선 무장, 지금은 발송 대상 없음.",
        "지금 할 일: 없음. 평일 15:40 KST 에 26,500 이하 1차 지정가 검토 p3, 24,000 비관 p4, 32,500 기본 p3, 40,000 낙관 p3, "
        "우선주 할인율 28% p3, WTI 2일 110 p3, HO=F 기준 +10%, -15% p3 를 상태가 바뀔 때만 봅니다.",
        "나중에: 유상증자 p4, 합병 완료와 일정 정정 p3, 12/19 확인 안내, 2027-04-01 지연 p4, 3분기 연결 실적 11/30 까지, 4분기 1/14 안내. 종료 2027-10-31.",
        "익절/줄일 때: 해당 없음.",
        "하지 말 것: 이 설치 확인을 매수/매도 신호로 읽기.",
    ])
    title = f"{TAG} 설치 확인 | 매매 신호 아님"
    print(title)
    print(body)
    publish(resolve_topic("port", cfg.get("ntfy_topic")), title, body, 3, ["white_check_mark"], resolve_server(cfg.get("ntfy_server")),
            config_name="kal")
    return 0


def main() -> int:
    args = [a.lower() for a in sys.argv[1:]]
    mode = args[0] if args else "auto"
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    if mode == "selftest":
        return selftest(cfg)
    if mode == "confirm":
        return confirm(cfg)
    if mode not in {"auto", "dry"}:
        print(__doc__)
        return 2
    now = datetime.now(KST)
    ends = cfg.get("ends_kst")
    if ends and now > datetime.strptime(ends, "%Y-%m-%d %H:%M").replace(tzinfo=KST):
        print(f"kal ended {ends} KST; silent")
        return 0
    return run(mode, now, cfg)


if __name__ == "__main__":
    sys.exit(main())
