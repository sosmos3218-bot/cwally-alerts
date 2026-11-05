#!/usr/bin/env python3
"""[079900] 전진건설로봇 이벤트 알림 (일일 요약 없음).

조건 1) 가격 구간 (네이버 일별 시세의 장중 저가 + 현재가/종가)
  - zone : 저가 <= 35,000원  = 33,000~35,000 분할매수 검토 구간 진입. 1회 발송
  - break: 저가 <  33,000원  = 구간 이탈. 따로 1회 발송
  각 조건은 한 번 울리면 꺼지고, 울린 날 이후 어떤 날의 종가(장중이면 현재가)가 재무장 가격
  (기준의 +2%: 35,700 / 33,700) 위로 올라가야 다시 무장된다. GitHub 실행이 늦거나 빠져도
  마지막으로 본 날부터의 일별 행(저가)을 순서대로 다시 보므로 장중 터치를 놓치지 않는다.
조건 2) 정기보고서 (분기/반기/사업보고서) 새 제출
  - 목록: KIND(kind.krx.co.kr) 회사별 공시 검색. DART 와 같은 정기보고서가 같은 날 올라온다.
  - 본문: KIND 공시뷰어 원문에서 연결(없으면 별도) 포괄손익계산서를 읽어 영업이익률 계산.
    분기 단독(3개월) 값이 있으면 그것으로 판정, 사업보고서는 연간 - 3분기 누적으로 4분기 단독 계산.
  - 링크: DART 공시검색에서 같은 보고서의 rcpNo 를 찾으면 DART 원문 링크, 못 찾으면 KIND 뷰어 링크.
  - 중복 방지: KIND 접수번호(acptno). DART rcpNo 와 번호 체계가 다르다.
modes: auto(기본) | dry(발송 없음, state 안 씀) | sim(가짜 데이터로 메시지 견본만 출력, 네트워크/발송/state 없음)
매매 신호가 아니다. 자동 주문 아님.
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
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402
from kr_calendar import session_close_ready  # noqa: E402

KST = timezone(timedelta(hours=9))
NAME = "k079900"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
NA = "확인 불가"
KIND = "https://kind.krx.co.kr"
DART_VIEW = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcp}"
KIND_VIEW = KIND + "/common/disclsviewer.do?method=search&acptno={acpt}"
UNIT = {"원": 1e-8, "천원": 1e-5, "백만원": 1e-2, "억원": 1.0}  # -> 억원
REPORT_RE = re.compile(r"(분기보고서|반기보고서|사업보고서)")


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
    s = str(v).replace(",", "").replace("+", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def load_state() -> dict:
    base = {"updated_at": None, "last_day": None, "zone": {"armed": True, "fired_day": None},
            "break": {"armed": True, "fired_day": None}, "reports_seeded": False, "seen_acptno": [],
            "fail_notice": {}}
    if STATE_PATH.exists():
        try:
            base.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return base


# ---------------- 가격 ----------------
def naver_rows(code: str) -> list[dict]:
    """정규장 OHLC. 이름은 기존 호출부(rfm0219b0)를 유지한다."""
    import krx_close
    rows = [{"day": b.day, "low": b.low, "close": b.close, "high": b.high} for b in krx_close.regular_bars(code, 20)]
    if not rows:
        raise RuntimeError("시세 0건")
    return rows


def eval_price(cfg: dict, state: dict, rows: list[dict], today: str, now: datetime) -> list[dict]:
    """state 를 갱신하고 새로 켜진 이벤트 목록을 돌려준다."""
    pz = cfg["price"]
    start = state.get("last_day") or rows[-1]["day"]
    events = []
    close_hm = tuple(map(int, cfg.get("market_close_kst", "15:30").split(":")))
    for r in rows:
        if r["day"] < start:
            continue
        live = not session_close_ready(r["day"], today, now, close_hm)
        for key, thr, rearm, strict in (("zone", pz["zone_max"], pz["zone_rearm"], False),
                                        ("break", pz["break_below"], pz["break_rearm"], True)):
            st = state[key]
            hit = r["low"] < thr if strict else r["low"] <= thr
            if st.get("armed", True) and hit and st.get("fired_day") != r["day"]:
                st.update(armed=False, fired_day=r["day"])
                events.append({"key": key, "day": r["day"], "low": r["low"], "close": r["close"], "live": live})
            elif not st.get("armed", True) and st.get("fired_day") and r["day"] > st["fired_day"] and r["close"] > rearm:
                st["armed"] = True
                print(f"{key} re-armed on {r['day']} (close {r['close']:,.0f} > {rearm:,.0f})")
    state["last_day"] = rows[-1]["day"]
    return events


# ---------------- 정기보고서 ----------------
def kind_reports(cfg: dict, since: str, until: str) -> list[dict]:
    form = {
        "method": "searchDetailsSub", "currentPageSize": "100", "pageIndex": "1", "orderMode": "1", "orderStat": "D",
        "forward": "details_sub", "searchCodeType": "", "repIsuSrtCd": "A" + cfg["code"], "allRepIsuSrtCd": "",
        "oldSearchCorpName": "", "disclosureType": "", "disTypevalue": "", "reportNm": "보고서", "reportCd": "",
        "searchCorpName": cfg["code"], "business": "", "marketType": "", "settlementMonth": "", "securities": "",
        "submitOblgNm": "", "enterprise": "", "fromDate": since, "toDate": until, "reportNmTemp": "", "bfrDsclsType": "on",
    }
    page = http(KIND + "/disclosure/details.do", data=form,
                headers={"X-Requested-With": "XMLHttpRequest", "Referer": KIND + "/disclosure/details.do?method=searchDetailsMain"})
    out = []
    seen_rows = 0
    for row in re.findall(r"(?s)<tr[^>]*>(.*?)</tr>", page):
        acpt = re.search(r"openDisclsViewer\('(\d{14})'", row)
        if not acpt:
            continue
        seen_rows += 1
        t = text_of(row)
        if cfg["name_kr"] not in t:
            continue
        m = re.search(r"((?:\[[^\]]+\]\s*)*)(분기보고서|반기보고서|사업보고서)\s*\([^)]*\)\s*\((\d{4})\.(\d{2})\)", t)
        if not m:
            continue
        dm = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})", t)
        out.append({"acptno": acpt.group(1), "kind": m.group(2), "tags": m.group(1).strip(),
                    "period": f"{m.group(3)}.{m.group(4)}", "filed": f"{dm.group(1)} {dm.group(2)}" if dm else "",
                    "title": m.group(0).strip()})
    if seen_rows == 0 and "조회된 결과값이 없습니다" not in page and "<table" not in page:
        raise RuntimeError("KIND 응답 형식 이상")
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


NUM_TOK = r"(?:\(?-?[\d,]+(?:\.\d+)?\)?|-)"


def _vals(seg: str, label_re: str, n: int) -> list[float] | None:
    m = re.search(r"(?<![가-힣A-Za-z])(?:" + label_re + r")\s+((?:" + NUM_TOK + r"\s+){1," + str(n) + r"})", seg + " ")
    if not m:
        return None
    out = []
    for tok in m.group(1).split()[:n]:
        if tok == "-":
            out.append(0.0)
            continue
        neg = tok.startswith("(") or tok.startswith("-")
        v = num(tok.strip("()").lstrip("-"))
        if v is None:
            return None
        out.append(-v if neg else v)
    return out


REV_RE = r"매출액|수익\(매출액\)|영업수익|매출|수익"
OP_RE = r"영업이익\(손실\)|영업이익\(영업손실\)|영업손익|영업이익|영업손실"


def parse_is(text: str) -> dict:
    """연결(없으면 별도) 포괄손익계산서 -> {basis, interim, rev:[...], op:[...] (억원)}."""
    for basis, heads in (("연결", ("연결 포괄손익계산서", "연결포괄손익계산서", "연결 손익계산서", "연결손익계산서")),
                         ("별도", ("포괄손익계산서", "손익계산서"))):
        for h in heads:
            for m in re.finditer(re.escape(h) + r"\s+제\s*\d+\s*기", text):
                if basis == "별도" and text[max(0, m.start() - 3):m.start()].endswith("연결 "):
                    continue
                seg = text[m.start():m.start() + 6000]
                um = re.search(r"\(단위\s*[:：]\s*(원|천원|백만원|억원)\s*\)", seg)
                if not um:
                    continue
                body = seg[um.end():]
                first = re.search(r"(?<![가-힣])(?:" + REV_RE + r")\s+" + NUM_TOK, body)
                if not first:
                    continue
                header = body[:first.start()]
                interim = "3개월" in header
                n = 4 if interim else 3
                rev, op = _vals(body, REV_RE, n), _vals(body, OP_RE, n)
                if not rev or not op:
                    continue
                f = UNIT[um.group(1)]
                return {"basis": basis, "interim": interim, "rev": [v * f for v in rev], "op": [v * f for v in op],
                        "unit": um.group(1)}
    raise RuntimeError("포괄손익계산서 매출/영업이익 행 없음")


def dart_link(cfg: dict, rep: dict) -> str | None:
    """DART 공시검색에서 같은 보고서(종류, 기간, 접수일)의 rcpNo. 못 찾으면 None."""
    day = rep["filed"][:10].replace("-", "")
    q = urllib.parse.urlencode([("currentPage", "1"), ("maxResults", "30"), ("textCrpNm", cfg["name_kr"]),
                                ("startDate", day), ("endDate", day),
                                ("publicType", "A001"), ("publicType", "A002"), ("publicType", "A003")])
    page = http(f"https://dart.fss.or.kr/dsab007/detailSearch.ax?{q}", tries=2, timeout=20)
    for row in re.findall(r"(?s)<tr[^>]*>(.*?)</tr>", page):
        rcp = re.search(r"rcpNo=(\d{14})", row)
        t = text_of(row)
        if rcp and rep["kind"] in t and f"({rep['period']})" in t:
            return DART_VIEW.format(rcp=rcp.group(1))
    return None


def margin_label(m: float, cfg: dict) -> str:
    g = cfg["dart"]
    if m >= g["recover_min_pct"]:
        return "회복 지속"
    if m < g["hold_below_pct"]:
        return f"매수 보류 검토 (영업이익률 {g['hold_below_pct']:g}% 미만)"
    return "중립"


def analyze_report(cfg: dict, rep: dict, all_reps: list[dict], fetch=kind_doc_text) -> dict:
    """{'lines': [...], 'label': str|None, 'qm': float|None, 'cm': float|None, 'qname': str}"""
    yr, mo = rep["period"].split(".")
    qn = {"03": 1, "06": 2, "09": 3, "12": 4}[mo]
    qname = f"{yr}년 {qn}분기"
    st = parse_is(fetch(rep["acptno"]))
    basis = st["basis"]
    rev, op = st["rev"], st["op"]
    lines: list[str] = []
    q_rev = q_op = None
    if st["interim"]:
        q_rev, q_op = rev[0], op[0]
        c_rev, c_op = rev[1], op[1]
    else:
        c_rev, c_op = rev[0], op[0]
        if qn == 1:
            q_rev, q_op = c_rev, c_op
    cum_name = {1: "1분기", 2: "상반기", 3: "1~3분기", 4: "연간"}[qn]
    if qn == 4:
        # 4분기 단독 = 연간 - 같은 해 3분기 누적 (3분기 보고서를 다시 읽는다)
        prior = [r for r in all_reps if r["kind"] == "분기보고서" and r["period"] == f"{yr}.09"]
        try:
            if not prior:
                raise RuntimeError(f"{yr}.09 분기보고서 목록에 없음")
            p = parse_is(fetch(prior[0]["acptno"]))
            if not p["interim"] or p["basis"] != basis:
                raise RuntimeError("3분기 보고서 표 형식 다름")
            q_rev, q_op = c_rev - p["rev"][1], c_op - p["op"][1]
        except Exception as exc:  # noqa: BLE001
            lines.append(f"4분기 단독: {NA} ({exc})")
    qm = q_op / q_rev * 100 if q_rev else None
    cm = c_op / c_rev * 100 if c_rev else None
    if qm is not None:
        how = "연간 - 3분기 누적" if qn == 4 else ("3개월" if st["interim"] else "1분기 = 누적")
        lines.insert(0, f"{qname} 단독({basis}, {how}): 매출 {q_rev:,.0f}억, 영업이익 {q_op:,.0f}억, 영업이익률 {qm:.2f}%")
    if cm is not None:
        lines.append(f"{cum_name} 누적({basis}): 매출 {c_rev:,.0f}억, 영업이익 {c_op:,.0f}억, 영업이익률 {cm:.2f}%")
    use = qm if qm is not None else cm
    basis_txt = f"{qname} 단독 기준" if qm is not None else f"{cum_name} 누적 기준(분기 단독 계산 안 됨)"
    return {"lines": lines, "label": margin_label(use, cfg) if use is not None else None, "use": use,
            "basis_txt": basis_txt, "qname": qname, "basis": basis}


# ---------------- 메시지 ----------------
def body(stamp: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([
        f"{stamp} | 079900 | 전진건설로봇",
        f"이게 뭐 알림: {what}",
        f"지금 결과/상태: {status}",
        f"지금 할 일: {action}",
        f"나중에: {later}",
        f"익절/줄일 때: {trim}",
        f"하지 말 것: {avoid}",
    ])


def price_msg(cfg: dict, ev: dict, stamp: str) -> tuple[str, str, int, list[str]]:
    pz = cfg["price"]
    d = datetime.strptime(ev["day"], "%Y-%m-%d")
    px_name = "현재가" if ev["live"] else "종가"
    px = f"{d:%m/%d} 장중 저가 {ev['low']:,.0f}원, {px_name} {ev['close']:,.0f}원"
    note = "35,000원, 33,000원은 차트 지지선 아님, 밸류에이션 기준 구간."
    trim = "구간 이탈(33,000원 미만)이 나오면 분할매수를 멈추고 이유부터 확인. 미리 정한 손절 기준대로."
    if ev["key"] == "zone":
        title = f"[079900] 분할매수 검토 구간 진입 | 저가 {ev['low']:,.0f}원 (35,000원 이하)"
        status = f"{px}. 기준 35,000원 이하 터치(33,000~35,000원 분할매수 검토 구간). {note}"
        action = ("컨센서스 실적 추정치(올해, 내년 영업이익)가 하향되지 않았는지 먼저 확인. 그대로면 미리 정한 1차 분할만 검토. "
                  "엠티알(MTR) 손실, 자금 지원 뉴스도 같이 확인.")
        later = f"이 구간 알림은 종가가 {pz['zone_rearm']:,.0f}원 위로 올라갔다 와야 다시 옵니다. 33,000원 미만이면 이탈 알림이 따로 옵니다."
        pri, tags = 4, ["moneybag"]
    else:
        title = f"[079900] 분할매수 구간 이탈 | 저가 {ev['low']:,.0f}원 (33,000원 미만)"
        status = f"{px}. 기준 33,000원 미만(33,000~35,000원 구간 아래로 이탈). {note}"
        action = "추가 분할은 멈추고, 실적 추정치 하향이나 엠티알(MTR), 이연 판매 관련 악재가 있었는지 확인. 이유를 모르면 기다리기."
        later = f"이탈 알림은 종가가 {pz['break_rearm']:,.0f}원 위로 회복했다 와야 다시 옵니다."
        pri, tags = 4, ["warning"]
    msg = body(stamp, "전진건설로봇 가격이 밸류에이션 기준 구간에 닿을 때만 오는 알림(장중 저가 기준). 매매 신호 아님, 자동 주문 아님.",
               status, action, later, trim,
               "가격만 보고 한 번에 다 사기, 실적 추정치 하향 확인 없이 물타기, 35,000원과 33,000원을 차트 지지선으로 믿기.")
    return title, msg, pri, tags, None


def report_msg(cfg: dict, rep: dict, res: dict | None, err: str | None, link: str, stamp: str) -> tuple[str, str, int, list[str]]:
    tag = f"{rep['tags']} " if rep["tags"] else ""
    name = f"{tag}{rep['kind']} ({rep['period']})"
    manual = "수동 확인: 엠티알(MTR) 손실과 자금 지원(대여, 보증, 증자), 이연 판매분의 매출/현금 전환(매출채권, 계약부채, 영업현금흐름)."
    if res and res.get("label"):
        title = f"[079900] {name} 제출 | {res['basis_txt'].split(' 기준')[0]} 영업이익률 {res['use']:.1f}%, {res['label']}"
        status = (f"{name} 제출 {rep['filed']} KST. 판정({res['basis_txt']}): {res['label']}. "
                  + " / ".join(res["lines"]) + f". 기준: 18% 이상 회복 지속, 15% 미만 매수 보류 검토, 사이는 중립. 링크 {link}")
        pri = 4 if res["use"] < cfg["dart"]["hold_below_pct"] else 3
    else:
        title = f"[079900] {name} 제출 | 영업이익률 자동 계산 안 됨, 원문 확인"
        status = f"{name} 제출 {rep['filed']} KST. 영업이익률 {NA} ({err or '표를 읽지 못함'}). 링크 {link}"
        pri = 3
    msg = body(stamp, "전진건설로봇 분기/반기/사업보고서가 새로 나오면 1번 오는 알림. 영업이익률로 회복 여부를 가늠. 매매 신호 아님.",
               status, f"원문에서 영업이익률 숫자를 한 번 더 확인. {manual}",
               "다음 정기보고서가 나오면 다시 옵니다. 15% 미만이 2분기 연속이면 비중 계획을 다시 봅니다.",
               "영업이익률 15% 미만이 이어지거나 엠티알(MTR) 손실, 자금 지원이 커지면 비중 축소 검토.",
               "영업이익률 한 숫자만 보고 사거나 팔기, 이연 판매 매출을 현금 확인 없이 믿기.")
    return title, msg, pri, ["page_facing_up"], rep["acptno"]


# ---------------- main ----------------
def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    if mode == "sim":
        return simulate(cfg, stamp)
    if mode not in {"auto", "dry"}:
        print(f"unknown mode {mode}")
        return 2
    dry = mode == "dry"
    state = load_state()
    today = now.strftime("%Y-%m-%d")
    out: list[tuple[str, str, int, list[str]]] = []

    def ops_once(key: str, title: str, msg: str) -> None:
        fn = state.setdefault("fail_notice", {})
        if fn.get(key) != today:
            out.append((title, msg, 3, ["warning"], None))
            fn[key] = today

    # 1) 가격
    try:
        rows = naver_rows(cfg["code"])
        print("rows:", [(r["day"], r["low"], r["close"]) for r in rows[-3:]], "last_day:", state.get("last_day"))
        for ev in eval_price(cfg, state, rows, today, now):
            out.append(price_msg(cfg, ev, stamp))
    except Exception as exc:  # noqa: BLE001
        print(f"price fetch failed: {exc}", file=sys.stderr)
        ops_once("price", "[079900] 가격 조회 실패 | 구간 판정 못 함",
                 body(stamp, "전진건설로봇 가격 구간 알림의 데이터 조회 실패 안내.", f"네이버 시세 {NA} ({exc}).",
                      "증권사 앱에서 가격을 직접 보세요.", "다음 실행 때 다시 봅니다.", "이번 안내로 비중을 바꾸지 마세요.",
                      "조회 실패를 구간 밖으로 읽기."))

    # 2) 정기보고서
    try:
        since = (now - timedelta(days=int(cfg["dart"]["lookback_days"]))).strftime("%Y-%m-%d")
        reps = kind_reports(cfg, since, today)
        print("reports:", [(r["acptno"], r["title"], r["filed"]) for r in reps])
        seen = set(state.get("seen_acptno") or [])
        if not state.get("reports_seeded"):
            state["seen_acptno"] = sorted(seen | {r["acptno"] for r in reps})
            state["reports_seeded"] = True
            print("reports seeded (no alert):", state["seen_acptno"])
            reps = []
        for rep in sorted((r for r in reps if r["acptno"] not in seen), key=lambda r: r["acptno"]):
            res, err = None, None
            try:
                res = analyze_report(cfg, rep, reps)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)[:160]
            link = None
            try:
                link = dart_link(cfg, rep)
            except Exception as exc:  # noqa: BLE001
                print(f"dart link failed: {exc}", file=sys.stderr)
            link = link or f"{KIND_VIEW.format(acpt=rep['acptno'])} (KIND 원문, DART 링크 못 찾음)"
            out.append(report_msg(cfg, rep, res, err, link, stamp))
            state["seen_acptno"] = sorted(set(state.get("seen_acptno") or []) | {rep["acptno"]})
    except Exception as exc:  # noqa: BLE001
        print(f"KIND fetch failed: {exc}", file=sys.stderr)
        ops_once("kind", "[079900] 정기보고서 목록 조회 실패 | 공시 판정 못 함",
                 body(stamp, "전진건설로봇 정기보고서 알림의 데이터 조회 실패 안내.", f"KIND 공시 목록 {NA} ({exc}).",
                      "필요하면 DART에서 전진건설로봇 공시를 직접 보세요.", "다음 실행 때 다시 봅니다.",
                      "이번 안내로 비중을 바꾸지 마세요.", "조회 실패를 새 보고서 없음으로 읽기."))

    for item in out:
        title, msg, pri, tags = item[0], item[1], item[2], item[3]
        key = item[4] if len(item) > 4 else None
        print(f"TITLE: {title}\nPRIORITY: {pri} TOPIC: {route_topic(topic, title)}\n{msg}\n")
        if not dry:
            publish(topic, title, msg, pri, tags, server, dedup_key=str(key) if key else None)
    if not out:
        print(f"no alert (zone={state['zone']} break={state['break']})")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    state["updated_at"] = now.isoformat()
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


def simulate(cfg: dict, stamp: str) -> int:
    """가짜 시세/보고서로 메시지 견본만 출력 (네트워크, ntfy, state 없음)."""
    st = {"last_day": "2026-10-22", "zone": {"armed": True, "fired_day": None}, "break": {"armed": True, "fired_day": None}}
    rows = [{"day": "2026-10-22", "low": 37350, "close": 38300},
            {"day": "2026-10-23", "low": 34650, "close": 35200},
            {"day": "2026-10-26", "low": 34100, "close": 34500},
            {"day": "2026-10-27", "low": 32800, "close": 33100}]
    for upto in range(1, len(rows) + 1):
        evs = eval_price(cfg, st, rows[:upto], rows[upto - 1]["day"], datetime(2026, 10, 27, 18, 0, tzinfo=KST))
        print(f"-- sim run through {rows[upto - 1]['day']}: events={[(e['key'], e['day']) for e in evs]}")
        for ev in evs:
            t, m, p, tg = price_msg(cfg, ev, stamp)
            print(f"TITLE: {t}\nPRIORITY: {p}\n{m}\n")
    rep = {"acptno": "20261113000999", "kind": "분기보고서", "tags": "", "period": "2026.09", "filed": "2026-11-13 15:40",
           "title": "분기보고서(일반법인) (2026.09)"}
    fake = {"lines": ["2026년 3분기 단독(연결, 3개월): 매출 455억, 영업이익 64억, 영업이익률 14.07%",
                      "1~3분기 누적(연결): 매출 1,388억, 영업이익 203억, 영업이익률 14.63%"],
            "label": margin_label(14.07, cfg), "use": 14.07, "basis_txt": "2026년 3분기 단독 기준"}
    t, m, p, _ = report_msg(cfg, rep, fake, None, "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=(예시)", stamp)
    print(f"-- sim report (가짜 숫자)\nTITLE: {t}\nPRIORITY: {p}\n{m}\n")
    t, m, p, _ = report_msg(cfg, rep, None, "포괄손익계산서 매출/영업이익 행 없음", KIND_VIEW.format(acpt=rep["acptno"]) + " (KIND 원문, DART 링크 못 찾음)", stamp)
    print(f"-- sim report parse failure\nTITLE: {t}\nPRIORITY: {p}\n{m}\nSIM: no publish, no state")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
