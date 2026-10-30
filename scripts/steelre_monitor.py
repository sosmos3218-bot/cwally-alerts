#!/usr/bin/env python3
"""[STEELRE] POSCO홀딩스(005490), 고려아연(010130) 재진입 판정. 2026-11-19 18:00 KST 1회.

2026-10-30 채팅 승인 t24u (카드 t24s2). 저장소 밖 루틴 대신 cwally-alerts 안에서 만든 1회 알림.
on-time: cron-job.org one-shot (job=oneshot, oneshot_pick) / 백업: port-alerts.yml cron 0 9 19 11 *.
11/3, 11/6 morning 슬롯(09:00)에서 컨센서스, 목표가 스냅샷만 조용히 저장(snap).

종목마다 조건 5개를 충족 / 미충족 / 확인 불가 로 적고 라벨을 정한다(규칙은 configs/steelre.json rules.note):
  필수 A  52주 KRX 종가 저점 뒤 10거래일 동안 더 낮은 종가 없음 (Daum KRX 일봉, NXT 제외)
  필수 B  평균 목표가, 2026 연간(E) 영업이익, EPS 가 10/30 기준 대비 5% 넘게 하락하지 않음
  선택 1  종가 >= 저점 종가 x1.05
  선택 2  외국인 5거래일 누적 순매수 > 0
  선택 3  20거래일 수익률이 코스피보다 높음
  라벨    판정 불가 (A 또는 B 확인 불가) / 재검토 가능 (A, B 충족 + 선택 2개 이상) / 보류 유지
고려아연은 경영권분쟁 공시, 3분기 실적 공시, 아연 가격 블록을 덧붙인다(라벨은 바꾸지 않음).

modes:
  auto      2026-11-19 18:00 ~ 18:30 KST 에만 1번 발송 (state sent). 그 밖은 조용히 끝
  snap      snapshot_days 의 morning 슬롯에서만 스냅샷 저장, 발송 없음
  dry       지금 시점 판정을 출력만 (ntfy 없음, state 안 씀)
  selftest  가짜 값, 네트워크 없음
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "steelre.json"
STATE_PATH = ROOT / "state" / "steelre.json"
UA = "Mozilla/5.0 (cwally-alerts; +mailto:sosmos3218@gmail.com)"
TAG = "[STEELRE]"
OK, NO, NA = "충족", "미충족", "확인 불가"
L_GO, L_NA, L_HOLD = "재검토 가능", "판정 불가", "보류 유지"
END_LINE = "꺼지는 때: 2026-11-19 18:30 KST 이후 자동 발송 없음"
BANNED_RE = re.compile("[\u00b7\u318d\u2014]")
KIND = "https://kind.krx.co.kr"
KIND_VIEW = KIND + "/common/disclsviewer.do?method=search&acptno={acpt}"
DISPUTE_RE = re.compile(r"경영권|소송|판결|가처분|공개매수|주주총회|의결권|최대주주|대량보유|자기주식|자사주|신주|유상증자|제3자배정|합병|분할")
RESULT_RE = re.compile(r"영업\s*\(잠정\)\s*실적|분기보고서")


# ---------- io ----------

def now_kst() -> datetime:
    return datetime.now(KST)


def load_cfg() -> dict:
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


def load_state() -> dict:
    st = {"sent": False, "sent_at": None, "snaps": {}, "ops_day": None}
    try:
        st.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return st


def save_state(st: dict, now: datetime) -> None:
    st["updated_at"] = now.strftime("%Y-%m-%d %H:%M KST")
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def http(url: str, referer: str | None = None, data: bytes | None = None, timeout: int = 25) -> str:
    h = {"User-Agent": UA}
    if referer:
        h["Referer"] = referer
    if data is not None:
        h["Content-Type"] = "application/x-www-form-urlencoded"
        h["X-Requested-With"] = "XMLHttpRequest"
    err: Exception | None = None
    for _ in range(3):
        try:
            req = urllib.request.Request(url, data=data, headers=h, method="POST" if data else "GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="ignore")
        except Exception as exc:  # noqa: BLE001
            err = exc
    raise RuntimeError(f"{url.split('?')[0]}: {err}")


def jget(url: str, referer: str | None = None):
    return json.loads(http(url, referer))


def num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").replace("+", ""))
    except ValueError:
        return None


def text_of(html: str) -> str:
    from html import unescape
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html or "")
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", unescape(t)).strip()


def clean(t: str) -> str:
    t = re.sub(r"\s*[ㆍ·•‧∙]\s*", ", ", t or "")
    return re.sub(r"\s*[—―]\s*", " ", t)


# ---------- sources ----------

def daum_daily(code: str, pages: int = 3) -> list[tuple[str, float]]:
    """KRX 정규장 종가 [(YYYY-MM-DD, close)] 오래된 순. Daum 일봉(거래량이 KRX만이라 NXT 제외)."""
    out: dict[str, float] = {}
    ref = f"https://finance.daum.net/quotes/A{code}"
    for p in range(1, pages + 1):
        raw = jget(f"https://finance.daum.net/api/quote/A{code}/days?symbolCode=A{code}&page={p}&perPage=100&pagination=true", ref)
        for r in raw.get("data") or []:
            c = num(r.get("tradePrice"))
            d = str(r.get("date") or "")[:10]
            if c and d:
                out[d] = c
    return sorted(out.items())


def kospi_daily() -> list[tuple[str, float]]:
    raw = jget("https://finance.daum.net/api/market_index/days?page=1&perPage=40&market=KOSPI&pagination=true",
               "https://finance.daum.net/domestic/kospi")
    return sorted((str(r["date"])[:10], float(r["tradePrice"])) for r in raw.get("data") or [] if r.get("tradePrice"))


def foreign_flows(code: str) -> tuple[list[tuple[str, float]], str]:
    """[(YYYYMMDD 또는 YYYY-MM-DD, 외국인 순매수 주)] 최신 순, 출처."""
    try:
        raw = jget(f"https://m.stock.naver.com/api/stock/{code}/trend?pageSize=10")
        rows = [(str(r.get("bizdate")), num(r.get("foreignerPureBuyQuant"))) for r in raw if isinstance(r, dict)]
        rows = [(d, v) for d, v in rows if v is not None]
        if rows:
            return rows, "네이버"
    except Exception as exc:  # noqa: BLE001
        print(f"naver trend {code}: {exc}")
    raw = jget(f"https://finance.daum.net/api/investor/days?symbolCode=A{code}&page=1&perPage=10&pagination=true",
               f"https://finance.daum.net/quotes/A{code}")
    rows = [(str(r.get("date"))[:10], num(r.get("foreignStraightPurchaseVolume"))) for r in raw.get("data") or []]
    return [(d, v) for d, v in rows if v is not None], "Daum"


def consensus(code: str) -> dict:
    """{target, target_date, fy_op, fy_eps, q4_op, q4_src, q3_actual}. 못 읽은 칸은 없음."""
    out: dict = {}
    integ = jget(f"https://m.stock.naver.com/api/stock/{code}/integration")
    ci = integ.get("consensusInfo") or {}
    if num(ci.get("priceTargetMean")):
        out["target"] = num(ci.get("priceTargetMean"))
        out["target_date"] = ci.get("createDate")
    an = jget(f"https://m.stock.naver.com/api/stock/{code}/finance/annual")["financeInfo"]
    cons = {t["key"]: t.get("isConsensus") == "Y" for t in an["trTitleList"]}
    if cons.get("202612"):
        for r in an["rowList"]:
            v = num((r["columns"].get("202612") or {}).get("value"))
            if r["title"] == "영업이익" and v is not None:
                out["fy_op"] = v
            elif r["title"] == "EPS" and v is not None:
                out["fy_eps"] = v
    q = jget(f"https://m.stock.naver.com/api/stock/{code}/finance/quarter")["financeInfo"]
    qc = {t["key"]: t.get("isConsensus") == "Y" for t in q["trTitleList"]}
    ops = {}
    for r in q["rowList"]:
        if r["title"] == "영업이익":
            ops = {k: num(v.get("value")) for k, v in r["columns"].items()}
    out["q3_actual"] = ("202609" in qc and not qc["202609"])
    if qc.get("202612") and ops.get("202612") is not None:
        out["q4_op"], out["q4_src"] = ops["202612"], "4분기(E)"
    elif out.get("fy_op") is not None and all(ops.get(k) is not None for k in ("202603", "202606", "202609")):
        out["q4_op"] = out["fy_op"] - ops["202603"] - ops["202606"] - ops["202609"]
        out["q4_src"] = "연간(E) - 1~3분기"
    return out


def kind_rows(code: str, since: str, until: str) -> list[dict]:
    import urllib.parse
    form = {"method": "searchDetailsSub", "currentPageSize": "100", "pageIndex": "1", "orderMode": "1", "orderStat": "D",
            "forward": "details_sub", "searchCodeType": "", "repIsuSrtCd": "A" + code, "allRepIsuSrtCd": "",
            "oldSearchCorpName": "", "disclosureType": "", "disTypevalue": "", "reportNm": "", "reportCd": "",
            "searchCorpName": code, "business": "", "marketType": "", "settlementMonth": "", "securities": "",
            "submitOblgNm": "", "enterprise": "", "fromDate": since, "toDate": until, "reportNmTemp": "", "bfrDsclsType": "on"}
    page = http(KIND + "/disclosure/details.do", referer=KIND + "/disclosure/details.do?method=searchDetailsMain",
                data=urllib.parse.urlencode(form).encode())
    out, rows = [], 0
    for row in re.findall(r"(?s)<tr[^>]*>(.*?)</tr>", page):
        a = re.search(r"openDisclsViewer\('(\d{14})'", row)
        if not a:
            continue
        rows += 1
        t = text_of(row)
        dm = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})", t)
        title = t[dm.end():].strip() if dm else t
        title = re.sub(r"\s*(공시차트|주가차트).*$", "", title).strip()
        out.append({"acptno": a.group(1), "title": title, "filed": f"{dm.group(1)} {dm.group(2)}" if dm else "",
                    "url": KIND_VIEW.format(acpt=a.group(1))})
    if rows == 0 and "조회된 결과값이 없습니다" not in page and "<table" not in page:
        raise RuntimeError("KIND 응답 형식 이상")
    return sorted(out, key=lambda r: r["acptno"])


def kind_body(acpt: str) -> str:
    viewer = http(KIND_VIEW.format(acpt=acpt))
    m = re.search(r"<option value='(\d+)\|Y'", viewer) or re.search(r"<option value='(\d+)\|", viewer)
    if not m:
        raise RuntimeError("KIND 본문 문서번호 없음")
    sc = http(f"{KIND}/common/disclsviewer.do?method=searchContents&docNo={m.group(1)}")
    p = re.search(r"setPath\('[^']*','([^']+\.htm)'", sc)
    if not p:
        raise RuntimeError("KIND 본문 경로 없음")
    return text_of(http(p.group(1), timeout=60))


UNIT_EOK = {"조원": 0.0001, "억원": 1.0, "백만원": 100.0, "천원": 100000.0}


def parse_result(body: str) -> dict | None:
    """영업(잠정)실적 본문 -> 당해실적 억원 {rev, op, net, period}."""
    t = re.sub(r"\s+", " ", body or "")
    um = re.search(r"단위\s*[:：]\s*(조원|억원|백만원|천원)", t)
    if not um:
        return None
    div = UNIT_EOK[um.group(1)]
    out: dict = {}
    pm = re.search(r"당기실적\s*(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})", t)
    if pm:
        out["period"] = f"{pm.group(1)} ~ {pm.group(2)}"
    for key, lab in (("rev", r"매출액"), ("op", r"영업이익"), ("net", r"당기순이익")):
        m = re.search(lab + r"\s*당해실적\s*(-?[\d,]+(?:\.\d+)?)", t)
        if m:
            out[key] = round(float(m.group(1).replace(",", "")) / div)
    return out if "op" in out else None


def zinc() -> list[tuple[str, float]]:
    raw = jget("https://m.stock.naver.com/front-api/marketIndex/prices?category=metals&reutersCode=CMZN0&page=1&pageSize=25")
    rows = [(str(r.get("localTradedAt"))[:10], num(r.get("closePrice"))) for r in raw.get("result") or []]
    return sorted((d, v) for d, v in rows if v)


# ---------- judgement (pure) ----------

def pct(a: float | None, b: float | None) -> float | None:
    if a is None or not b:
        return None
    return (a / b - 1) * 100


def judge_a(daily: list[tuple[str, float]], today: str, cfg_rules: dict) -> tuple[str, str, dict]:
    if not daily:
        return NA, "KRX 종가를 못 읽음", {}
    lo_day = (date.fromisoformat(today) - timedelta(days=cfg_rules["a_low_window_days"])).isoformat()
    win = [(d, c) for d, c in daily if lo_day < d <= today]
    if len(win) < 20:
        return NA, f"KRX 종가 {len(win)}일치뿐", {}
    low = min(c for _, c in win)
    low_day = max(d for d, c in win if c == low)
    after = [c for d, c in win if d > low_day]
    need = cfg_rules["a_hold_trading_days"]
    info = {"low": low, "low_day": low_day, "after": len(after), "close": win[-1][1], "last_day": win[-1][0]}
    span = "52주" if win[0][0] <= (date.fromisoformat(today) - timedelta(days=350)).isoformat() else f"{win[0][0]} 이후"
    head = f"{span} 최저 종가 {low:,.0f}원 ({low_day[5:].replace('-', '/')})"
    if len(after) < need:
        return NO, f"{head}, 저점 뒤 {len(after)}거래일 (10거래일 필요)", info
    return OK, f"{head}, 저점 뒤 {len(after)}거래일 동안 더 낮은 종가 없음", info


def judge_b(code: str, cur: dict | None, base: dict, stock: dict, today: str, result_found: bool | None,
            rules: dict) -> tuple[str, str]:
    if today < stock["judge_from"]:
        return NA, f"판정 가능일 {stock['judge_from'][5:].replace('-', '/')} 전 (실적 {stock['earnings_day'][5:].replace('-', '/')})"
    if result_found is False:
        return NA, "3분기 실적 공시를 KIND/DART에서 못 찾음"
    if result_found is None:
        return NA, "3분기 실적 공시 확인 실패"
    if not cur:
        return NA, "컨센서스 조회 실패"
    parts, ok = [], True
    for key, lab, lim in (("target", "평균 목표가", rules["b_target_drop_max_pct"]),
                          ("fy_op", "2026 영업이익(E)", rules["b_consensus_drop_max_pct"]),
                          ("fy_eps", "2026 EPS(E)", rules["b_consensus_drop_max_pct"])):
        ch = pct(cur.get(key), base.get(key))
        if ch is None:
            return NA, f"{lab} 조회 실패 (기준 10/30)"
        parts.append(f"{lab} {ch:+.1f}%")
        if round(ch, 6) < -lim:
            ok = False
    txt = ", ".join(parts) + " (기준 10/30, 5% 넘게 하락하면 미충족)"
    return (OK if ok else NO), txt


def judge_opt1(a_info: dict, mult: float, daily: list[tuple[str, float]]) -> tuple[str, str]:
    if not a_info:
        return NA, "저점 없음"
    need = a_info["low"] * mult
    close = a_info["close"]
    ma20 = sum(c for _, c in daily[-20:]) / 20 if len(daily) >= 20 else None
    ref = f", 참고 20일선 {ma20:,.0f}원" if ma20 else ""
    st = OK if close >= round(need, 6) else NO
    return st, f"종가 {close:,.0f}원, 기준 저점 x{mult:.2f} = {need:,.0f}원{ref}"


def judge_opt2(rows: list[tuple[str, float]], days: int, src: str) -> tuple[str, str]:
    if len(rows) < days:
        return NA, "외국인 순매수 조회 실패"
    s = sum(v for _, v in rows[:days])
    d0, d1 = rows[days - 1][0], rows[0][0]
    fmt = lambda d: f"{d[4:6]}/{d[6:8]}" if len(d) == 8 else d[5:].replace("-", "/")  # noqa: E731
    return (OK if s > 0 else NO), f"외국인 {fmt(d0)}~{fmt(d1)} {days}거래일 누적 {s:+,.0f}주 ({src})"


def ret_n(series: list[tuple[str, float]], n: int, upto: str | None = None) -> tuple[float, str, str] | None:
    s = [x for x in series if upto is None or x[0] <= upto]
    if len(s) <= n:
        return None
    a, b = s[-1 - n], s[-1]
    return (b[1] / a[1] - 1) * 100, a[0], b[0]


def judge_opt3(daily: list[tuple[str, float]], kospi: list[tuple[str, float]], n: int) -> tuple[str, str]:
    if not daily or not kospi:
        return NA, "코스피 또는 종가 조회 실패"
    r = ret_n(daily, n)
    if not r:
        return NA, "종가 부족"
    k = ret_n(kospi, n, upto=r[2])
    if not k or k[1] != r[1]:
        return NA, "코스피 날짜가 맞지 않음"
    diff = r[0] - k[0]
    return (OK if diff > 0 else NO), f"20거래일 {r[0]:+.1f}%, 코스피 {k[0]:+.1f}%, 차이 {diff:+.1f}%p"


def label(a: str, b: str, opts: list[str], need: int = 2) -> str:
    if a == NA or b == NA:
        return L_NA
    if a == OK and b == OK and sum(1 for o in opts if o == OK) >= need:
        return L_GO
    return L_HOLD


# ---------- message ----------

def eok(v) -> str:
    return f"{v:,.0f}억" if v is not None else "없음"


def stock_block(name: str, res: dict) -> list[str]:
    lines = [f"{name}: {res['label']}"]
    for key, lab in (("A", "필수 A 신저가 후 10거래일"), ("B", "필수 B 목표가, 컨센서스"),
                     ("O1", "선택 1 가격 회복"), ("O2", "선택 2 외국인 5일"), ("O3", "선택 3 코스피 대비")):
        st, why = res[key]
        lines.append(f"- {lab}: {st}. {why}")
    c = res.get("cons") or {}
    base = res.get("base") or {}
    if c:
        q4 = f", 4분기 영업이익 {eok(c.get('q4_op'))} ({c.get('q4_src')}, 10/30 {eok(base.get('q4_op_implied'))})" if c.get("q4_op") is not None else ""
        tgt = f"{c['target']:,.0f}원 ({c.get('target_date')})" if c.get("target") else "없음"
        lines.append(f"- 참고: 평균 목표가 {tgt}, 10/30 {base.get('target', 0):,.0f}원. 2026 영업이익(E) {eok(c.get('fy_op'))}{q4}")
    for snap in res.get("snaps") or []:
        lines.append(f"- 중간 스냅샷 {snap}")
    lines.append("- 목표가 변경 횟수: 확인 불가 (자동 집계 안 함, 추정 안 함)")
    return lines


def short_title(t: str, corp: str = "고려아연") -> str:
    t = clean(t).strip()
    if t.startswith(corp + " "):
        t = t[len(corp) + 1:]
    if t.endswith(" " + corp):
        t = t[: -len(corp) - 1]
    return t.strip()


def kz_block(kz: dict) -> list[str]:
    lines = ["고려아연 추가 확인 (라벨은 바꾸지 않음):"]
    if kz.get("err"):
        lines.append(f"- 공시: 확인 불가 ({kz['err']})")
    else:
        disp = kz.get("dispute") or []
        if disp:
            lines.append(f"- 경영권분쟁 관련 공시 (10/30부터 {len(disp)}건, 최근 6건):")
            for r in disp[-6:]:
                lines.append(f"  {r['filed']} {short_title(r['title'])} {r['url']}")
        else:
            lines.append("- 경영권분쟁 관련 공시 (10/30부터): 없음")
        res = kz.get("result") or []
        if res:
            for r in res[-2:]:
                p = r.get("parsed")
                nums = (f" 매출 {eok(p.get('rev'))}, 영업이익 {eok(p.get('op'))}, 순이익 {eok(p.get('net'))}" if p else " 숫자 자동으로 못 읽음, 원문 확인")
                lines.append(f"- 3분기 실적 공시: {r['filed']} {short_title(r['title'])}.{nums} {r['url']}")
        else:
            lines.append("- 3분기 실적 공시: KIND에서 못 찾음")
    lines.append("- 악재 여부는 원문 확인")
    z = kz.get("zinc") or []
    if len(z) >= 6:
        last = z[-1]
        c5 = pct(last[1], z[-6][1])
        c20 = pct(last[1], z[-21][1]) if len(z) >= 21 else None
        t20 = f", 20일 {c20:+.1f}%" if c20 is not None else ""
        lines.append(f"- 아연 LME 현물(네이버 시장지표): {last[1]:,.0f}달러/톤 ({last[0][5:].replace('-', '/')}), 5일 {c5:+.1f}%{t20}")
    else:
        lines.append("- 아연 가격: 확인 불가")
    return lines


def message(results: dict, cfg: dict, kz: dict | None, now: datetime, dry_note: str = "") -> tuple[str, str, int]:
    names = cfg["stocks"]
    labs = [f"{names[c]['name']} {results[c]['label']}" for c in names]
    title = f"{TAG} 철강, 비철 재진입 판정 | " + ", ".join(labs)
    lines = [f"{now.strftime('%Y-%m-%d %H:%M')} KST | STEELRE | 재진입 판정 1회" + dry_note,
             "이게 뭐 알림: POSCO홀딩스와 고려아연을 다시 살 만한지 조건 5개로 본 1회 판정. [STEEL] 9/15 가격대보다 이 결과를 먼저 봄.",
             "지금 결과/상태:"]
    for c in names:
        lines += stock_block(names[c]["name"], results[c])
    if kz is not None:
        lines += kz_block(kz)
    go = [names[c]["name"] for c in names if results[c]["label"] == L_GO]
    na = [names[c]["name"] for c in names if results[c]["label"] == L_NA]
    if go:
        todo = f"{', '.join(go)}: 재진입 재검토. 공시 원문과 실적을 읽고 분할로만 다시 계획."
    elif na:
        todo = f"{', '.join(na)}: 확인 불가 항목을 직접 확인하기 전까지 보류."
    else:
        todo = "둘 다 보류 유지. 새로 사지 말고 현금 유지."
    lines += [f"지금 할 일: {todo}",
              "판정 규칙: A 또는 B 확인 불가면 판정 불가, A와 B 충족 + 선택 3개 중 2개 이상이면 재검토 가능, 나머지는 보류 유지. 가격은 Daum KRX 정규장 종가(NXT 제외).",
              "나중에: [STEEL] 가격대(9/15 기준)는 이 결과를 본 뒤 다시 정함. 고려아연 3분기 숫자는 [KZ] 알림과 함께 보기.",
              "하지 말 것: 라벨만 보고 한 번에 매수, 확인 불가 항목을 추정으로 채우기.",
              END_LINE]
    pri = 4 if go else 3
    return title, "\n".join(lines), pri


def check_text(t: str, m: str) -> None:
    assert not BANNED_RE.search(t + m), "banned char"
    assert "매수" not in t and "신호" not in t, t


# ---------- run ----------

def snap_line(day: str, s: dict) -> str:
    tgt = f"{s['target']:,.0f}원" if s.get("target") else "없음"
    return f"{day[5:].replace('-', '/')}: 평균 목표가 {tgt}, 2026 영업이익(E) {eok(s.get('fy_op'))}, EPS(E) {s.get('fy_eps', 0):,.0f}원"


def evaluate(cfg: dict, st: dict, now: datetime) -> tuple[dict, dict | None, list[str]]:
    today = now.date().isoformat()
    rules, base = cfg["rules"], cfg["baseline"]
    errs: list[str] = []
    try:
        kospi = kospi_daily()
    except Exception as exc:  # noqa: BLE001
        kospi = []
        errs.append(f"코스피: {exc}")
    results, kz = {}, None
    for code, stock in cfg["stocks"].items():
        try:
            daily = [x for x in daum_daily(code) if x[0] <= today]
        except Exception as exc:  # noqa: BLE001
            daily = []
            errs.append(f"{code} 일봉: {exc}")
        a = judge_a(daily, today, rules)
        try:
            cur = consensus(code)
        except Exception as exc:  # noqa: BLE001
            cur = None
            errs.append(f"{code} 컨센서스: {exc}")
        rows = None
        try:
            rows = kind_rows(code, cfg["filings_since"], today)
            found = any(RESULT_RE.search(r["title"]) for r in rows)
        except Exception as exc:  # noqa: BLE001
            errs.append(f"{code} KIND: {exc}")
            found = None
        if found is not True and code != "010130" and cur and cur.get("q3_actual"):
            found = True  # POSCO: KIND 실패 시 네이버 분기 실적(확정 열)로 대신 확인
        b = judge_b(code, cur, base[code], stock, today, found, rules)
        try:
            fl, src = foreign_flows(code)
        except Exception as exc:  # noqa: BLE001
            fl, src = [], ""
            errs.append(f"{code} 외국인: {exc}")
        res = {"A": a[:2], "B": b, "O1": judge_opt1(a[2], rules["opt1_rebound_mult"], daily),
               "O2": judge_opt2(fl, rules["opt2_foreign_days"], src), "O3": judge_opt3(daily, kospi, rules["opt3_rel_days"]),
               "cons": cur, "base": base[code],
               "snaps": [snap_line(d, v[code]) for d, v in sorted((st.get("snaps") or {}).items()) if code in v]}
        res["label"] = label(res["A"][0], res["B"][0], [res["O1"][0], res["O2"][0], res["O3"][0]], rules["need_options"])
        results[code] = res
        if stock.get("extra_block"):
            kz = {"dispute": [], "result": []}
            if rows is None:
                kz["err"] = "KIND 목록 조회 실패"
            else:
                kz["dispute"] = [r for r in rows if DISPUTE_RE.search(r["title"])]
                for r in rows:
                    if RESULT_RE.search(r["title"]):
                        try:
                            r["parsed"] = parse_result(kind_body(r["acptno"])) if "잠정" in r["title"] else None
                        except Exception as exc:  # noqa: BLE001
                            r["parsed"] = None
                            errs.append(f"KIND 본문: {exc}")
                        kz["result"].append(r)
            try:
                kz["zinc"] = zinc()
            except Exception as exc:  # noqa: BLE001
                kz["zinc"] = []
                errs.append(f"아연: {exc}")
    return results, kz, errs


def all_failed(results: dict) -> bool:
    return all(r["A"][0] == NA and r["O3"][0] == NA for r in results.values())


def ops_send(st: dict, now: datetime, errs: list[str], cfg: dict, dry: bool) -> None:
    day = now.date().isoformat()
    if st.get("ops_day") == day:
        return
    t = f"{TAG} 시세 조회 실패 | 재진입 판정 못 함"
    m = "\n".join([f"{now.strftime('%Y-%m-%d %H:%M')} KST | STEELRE",
                   "이게 뭐 알림: 11/19 재진입 판정용 시세를 못 읽음. 매매 신호 아님.",
                   "지금 결과/상태: " + clean("; ".join(errs)[:400]),
                   "지금 할 일: Daum, 네이버에서 POSCO홀딩스와 고려아연 종가를 직접 확인.",
                   "하지 말 것: 이 안내로 매매.", END_LINE])
    check_text(t, m)
    if dry:
        print(f"DRY ops: {t}\n{m}")
        return
    publish(route_topic("cwally-ops", t), t, m, 3, ["warning"], resolve_server(cfg.get("ntfy_server")), config_name="steelre")
    st["ops_day"] = day


def do_snap(cfg: dict, now: datetime, dry: bool) -> int:
    today = now.date().isoformat()
    if today not in cfg["snapshot_days"] and not dry:
        print(f"steelre snap: {today} not a snapshot day; silent")
        return 0
    st = load_state()
    if today in (st.get("snaps") or {}) and not dry:
        print("steelre snap: already taken")
        return 0
    snap = {}
    for code in cfg["stocks"]:
        try:
            c = consensus(code)
            snap[code] = {k: c.get(k) for k in ("target", "target_date", "fy_op", "fy_eps", "q4_op", "q4_src")}
        except Exception as exc:  # noqa: BLE001
            print(f"snap {code} failed: {exc}")
    print(f"steelre snap {today}: {json.dumps(snap, ensure_ascii=False)}")
    if dry or not snap:
        print("DRY RUN or empty: state not written")
        return 0
    st.setdefault("snaps", {})[today] = snap
    save_state(st, now)
    return 0


def run(mode: str, now: datetime | None = None) -> int:
    cfg = load_cfg()
    now = now or now_kst()
    if mode == "snap":
        return do_snap(cfg, now, dry=False)
    dry = mode == "dry"
    vt = datetime.strptime(cfg["verdict_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    end = datetime.strptime(cfg["window_end_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    st = load_state()
    if not dry:
        if now.year != cfg["event_year"]:
            print("steelre: year guard; silent")
            return 0
        if now > end:
            print("steelre: ended (after 2026-11-19 18:30 KST); silent")
            return 0
        if now < vt:
            print("steelre: before verdict time; silent")
            return 0
        if st.get("sent"):
            print(f"steelre: already sent {st.get('sent_at')}; silent")
            return 0
    results, kz, errs = evaluate(cfg, st, now)
    for e in errs:
        print(f"note: {e}")
    if all_failed(results):
        ops_send(st, now, errs, cfg, dry)
        if not dry:
            save_state(st, now)
        return 0
    t, m, pri = message(results, cfg, kz, now, " (dry, 지금 시점 미리보기)" if dry else "")
    check_text(t, m)
    print(f"priority={pri} topic={route_topic(cfg['ntfy_topic'], t)}\n{t}\n{m}")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    st["sent"] = True  # 먼저 표시: 발송 뒤 저장 실패로 두 번 가지 않게
    st["sent_at"] = now.strftime("%Y-%m-%d %H:%M KST")
    st["labels"] = {c: results[c]["label"] for c in results}
    save_state(st, now)
    publish(cfg["ntfy_topic"], t, m, pri, ["factory", "mag"], resolve_server(cfg.get("ntfy_server")), config_name="steelre")
    return 0


# ---------- selftest ----------

def selftest() -> int:
    n = 0

    def check(lab, got, want):
        nonlocal n
        assert got == want, f"{lab}: {got!r} != {want!r}"
        n += 1

    rules = load_cfg()["rules"]
    d0 = date(2026, 1, 2)
    days = [(d0 + timedelta(days=i)).isoformat() for i in range(330) if (d0 + timedelta(days=i)).weekday() < 5]

    def series(low_idx_from_end: int, low=250000.0, rest=300000.0):
        s = [(d, rest) for d in days]
        i = len(s) - 1 - low_idx_from_end
        s[i] = (s[i][0], low)
        return s

    today = days[-1]
    # A: low exactly 10 trading days ago -> 충족; 9 -> 미충족; low today -> 미충족
    check("A10", judge_a(series(10), today, rules)[0], OK)
    check("A9", judge_a(series(9), today, rules)[0], NO)
    check("A0", judge_a(series(0), today, rules)[0], NO)
    check("Aempty", judge_a([], today, rules)[0], NA)
    check("Ashort", judge_a(series(3)[-10:], today, rules)[0], NA)
    # tie: same low twice, later one counts (conservative)
    s = series(30)
    s[-5] = (s[-5][0], 250000.0)
    check("Atie", judge_a(s, today, rules)[0], NO)
    check("Atie_day", judge_a(s, today, rules)[2]["low_day"], s[-5][0])
    # outside 365 days not used
    old = [("2025-06-01", 100000.0)] + series(15)
    check("A365", judge_a(old, today, rules)[2]["low"], 250000.0)
    # opt1
    a = judge_a(series(12, low=100.0, rest=105.0), today, rules)[2]
    check("O1eq", judge_opt1(a, 1.05, series(12, 100.0, 105.0))[0], OK)
    a = judge_a(series(12, low=100.0, rest=104.9), today, rules)[2]
    check("O1lt", judge_opt1(a, 1.05, series(12, 100.0, 104.9))[0], NO)
    # opt2
    check("O2pos", judge_opt2([("20261119", 10), ("20261118", -5), ("20261117", 1), ("20261116", 0), ("20261113", 0)], 5, "네이버")[0], OK)
    check("O2zero", judge_opt2([("20261119", 0)] * 5, 5, "네이버")[0], NO)
    check("O2short", judge_opt2([("20261119", 9)] * 4, 5, "네이버")[0], NA)
    # opt3
    st = [(d, 100.0 + i) for i, d in enumerate(days)]
    kp = [(d, 100.0) for d in days]
    check("O3up", judge_opt3(st, kp, 20)[0], OK)
    check("O3down", judge_opt3(st, [(d, 100.0 + 2 * i) for i, d in enumerate(days)], 20)[0], NO)
    check("O3na", judge_opt3(st, [], 20)[0], NA)
    # B
    base = {"target": 100.0, "fy_op": 100.0, "fy_eps": 100.0}
    stock = {"judge_from": "2026-11-16", "earnings_day": "2026-11-02"}
    check("Bearly", judge_b("x", base, base, stock, "2026-11-15", True, rules)[0], NA)
    check("Bok", judge_b("x", {"target": 95.0, "fy_op": 95.0, "fy_eps": 99.0}, base, stock, "2026-11-19", True, rules)[0], OK)
    check("Bdrop", judge_b("x", {"target": 94.9, "fy_op": 100.0, "fy_eps": 100.0}, base, stock, "2026-11-19", True, rules)[0], NO)
    check("Bnores", judge_b("x", base, base, stock, "2026-11-19", False, rules)[0], NA)
    check("Bmiss", judge_b("x", {"target": 100.0}, base, stock, "2026-11-19", True, rules)[0], NA)
    check("Bnone", judge_b("x", None, base, stock, "2026-11-19", True, rules)[0], NA)
    # label
    check("L_go", label(OK, OK, [OK, OK, NO]), L_GO)
    check("L_one", label(OK, OK, [OK, NA, NO]), L_HOLD)
    check("L_na_a", label(NA, OK, [OK, OK, OK]), L_NA)
    check("L_na_b", label(OK, NA, [OK, OK, OK]), L_NA)
    check("L_no_a", label(NO, OK, [OK, OK, OK]), L_HOLD)
    # parse result body
    body = ("연결재무제표기준영업(잠정)실적(공정공시) ※ 동 정보는 잠정치 단위 : 억원, % 당기실적 2026-07-01 ~ 2026-09-30 "
            "매출액 당해실적 41,000 전기실적 40,000 영업이익 당해실적 3,800 전기실적 5,871 당기순이익 당해실적 2,500")
    check("parse", parse_result(body), {"period": "2026-07-01 ~ 2026-09-30", "rev": 41000, "op": 3800, "net": 2500})
    check("parse_none", parse_result("없음"), None)
    check("short", short_title("고려아연 소송 등의 제기ㆍ신청(경영권 분쟁 소송) 고려아연"), "소송 등의 제기, 신청(경영권 분쟁 소송)")
    # message + end guard
    cfg = load_cfg()
    mk = lambda lab: {"A": (OK, "a"), "B": (OK, "b"), "O1": (OK, "o"), "O2": (NO, "o"), "O3": (NA, "o"), "label": lab,  # noqa: E731
                      "cons": {"target": 1.0, "fy_op": 1.0, "q4_op": 1.0, "q4_src": "x", "target_date": "2026-11-18"}, "base": {"target": 1.0}, "snaps": ["11/03: x"]}
    kz = {"dispute": [{"filed": "2026-10-30 17:38", "title": "소송 등의 제기ㆍ신청(경영권 분쟁 소송)", "url": "u"}],
          "result": [{"filed": "2026-11-05 16:00", "title": "연결재무제표기준영업(잠정)실적(공정공시)", "url": "u", "parsed": {"op": 3800}}],
          "zinc": [(f"2026-10-{i:02d}", 3000.0 + i) for i in range(1, 25)]}
    now = datetime(2026, 11, 19, 18, 0, tzinfo=KST)
    for labs in ((L_GO, L_HOLD), (L_NA, L_NA), (L_HOLD, L_HOLD)):
        t, m, p = message({"005490": mk(labs[0]), "010130": mk(labs[1])}, cfg, kz, now)
        check_text(t, m)
        check("ends", m.splitlines()[-1], END_LINE)
        check("topic", route_topic("cwally-port", t), "cwally-port")
        check("pri", p, 4 if L_GO in labs else 3)
        assert "악재 여부는 원문 확인" in m and "목표가 변경 횟수: 확인 불가" in m
    t, m, _ = message({"005490": mk(L_HOLD), "010130": mk(L_HOLD)}, cfg, {"err": "x", "zinc": []}, now)
    assert "아연 가격: 확인 불가" in m
    check_text(t, m)
    # end guard / dedupe without network (auto returns before evaluate)
    import io
    import contextlib
    global load_state
    real = load_state
    try:
        for when, sent, want in ((datetime(2026, 11, 19, 18, 31, tzinfo=KST), False, "ended"),
                                 (datetime(2026, 11, 19, 17, 59, tzinfo=KST), False, "before"),
                                 (datetime(2026, 11, 19, 18, 10, tzinfo=KST), True, "already sent"),
                                 (datetime(2027, 11, 19, 18, 10, tzinfo=KST), False, "year guard")):
            load_state = lambda s=sent: {"sent": s, "snaps": {}}  # noqa: E731
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = run("auto", when)
            check(f"guard {want}", (rc, want in buf.getvalue()), (0, True))
    finally:
        load_state = real
    print(f"selftest {n}/{n}")
    return 0


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode == "snapdry":
        return do_snap(load_cfg(), now_kst(), dry=True)
    if mode not in ("auto", "dry", "snap"):
        print(f"unknown mode {mode}")
        return 2
    return run(mode)


if __name__ == "__main__":
    raise SystemExit(main())
