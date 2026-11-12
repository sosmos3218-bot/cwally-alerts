#!/usr/bin/env python3
"""TIGER 미국나스닥바이오 (KRX 203780) 일일 요약.

KRX 거래일 장 마감 뒤(평일 19:00 KST, cron-job.org 정시 dispatch + GitHub cron 0 10 * * 1-5 백업) 하루 1번, 메시지 하나에 4가지:
  1) 종가, 운용사(미래에셋) 일별 NAV, 괴리율 = (종가/NAV - 1) x 100  (참고 지표, 매매 신호 아님)
     그날 공식 NAV 는 다음 날 아침에 나오므로 19:00 엔 보통 전일(날짜 확인한) 공식 NAV 와 전일 괴리율,
     오늘 장 마감 iNAV 추정(네이버 totalInfos nav)과 추정 괴리율을 같이 적는다. GitHub 실행에선 미래에셋(403)을 건너뛴다.
  2) 하루 등락률 (|등락| >= 3% 강조)
  3) 원/달러 환율과 전일 대비 (|변동| >= 1% 강조)
  4) 상위 5종목 GILD, AMGN, VRTX, MRNA, REGN 최근 완료 일봉 등락 (|등락| >= 5% 강조) + 7일 안 실적 발표일
  5) 분할 구역 한 줄 (규칙 종가의 60일선, 200일선, 32,000원 거리).
강조가 하나라도 걸리면 priority 4(high) + rotating_light 태그, 아니면 3.
구역 이벤트 (2026-11-12 채팅 승인): 요약과 별개 메시지, 상태가 바뀔 때만, priority 4. 종가와 이동평균은 krx_close 정규장 일봉.
  1차 [203780] 1차 분할 구간: 종가 <= 60일선 + 추정 괴리율 <= +0.3%. 종가가 60일선 위로 돌아오면 재무장.
  2차 [203780] 2차 분할 구간: 종가 <= 200일선 + 같은 괴리 조건. 종가가 200일선 위로 돌아오면 재무장.
  확대 [203780] 비중 확대 검토: 1차 또는 2차가 켜진 상태에서 미 재무부 10년물이 3거래일 연속 4.60% 미만.
       10년물이 4.60% 이상이거나 종가가 20일선 위면 재무장.
  축소 [203780] 축소 경계: 종가 < 32,000원. 다음 거래일 종가도 200일선 아래면 [203780] 축소 확인. 200일선을 회복하면 해제.
어느 항목이든 조회에 실패하면 그 항목은 '확인 불가'로 적는다. 숫자를 만들어 넣지 않는다.
휴장일(kr_calendar 또는 네이버에 그 거래일 시세 없음)이면 보내지 않는다. 같은 거래일은 한 번만.
modes: auto(기본) | dry(발송 없음, state 안 씀) | force(같은 거래일 중복 검사 무시하고 발송) |
       install(설치 확인 1회, state 안 씀, 구역 이벤트 없음) | selftest(구역 이벤트 규칙 오프라인 검사, 발송 없음)
"""
from __future__ import annotations

import csv
import html
import io
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_calendar import is_trading_day, prev_trading_day, session_day  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

KST = timezone(timedelta(hours=9))
NAME = "k203780"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
UA = {"User-Agent": "Mozilla/5.0 (cwally-alerts; +mailto:sosmos3218@gmail.com)"}
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
NA = "확인 불가"


def num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").replace("+", "").replace("%", "").strip())
    except ValueError:
        return None


def get(url: str, headers: dict | None = None, data: bytes | None = None, opener=None, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=headers or UA, data=data)
    op = opener.open if opener else urllib.request.urlopen
    with op(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def ny_now() -> datetime:
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo("America/New_York"))
        except Exception:  # noqa: BLE001
            pass
    off = -5 if datetime.now(timezone.utc).month in (11, 12, 1, 2, 3) else -4
    return datetime.now(timezone(timedelta(hours=off)))


# ---------- 1, 2) 종가 + 등락률 (네이버) ----------
def naver_daily(code: str) -> dict[str, dict]:
    raw = json.loads(get(f"https://m.stock.naver.com/api/stock/{code}/price?pageSize=10&page=1"))
    out: dict[str, dict] = {}
    for r in raw if isinstance(raw, list) else []:
        d = str(r.get("localTradedAt") or "")[:10]
        close = num(r.get("closePrice"))
        if len(d) == 10 and close:
            out[d] = {"close": close, "chg": num(r.get("compareToPreviousClosePrice")), "pct": num(r.get("fluctuationsRatio"))}
    return out


# ---------- 1) 운용사 일별 NAV (미래에셋 기준가격 표) ----------
def mirae_nav(isin: str, day: date) -> dict[str, dict]:
    start = (day - timedelta(days=10)).strftime("%Y%m%d")
    end = day.strftime("%Y%m%d")
    body = urllib.parse.urlencode({"ksdFund": isin, "period": "", "startDate": start, "endDate": end}).encode()
    detail = f"https://investments.miraeasset.com/tigeretf/ko/product/search/detail/index.do?ksdFund={isin}"
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    try:  # 상품 페이지를 먼저 열어 세션 쿠키를 받는다 (브라우저와 같은 순서)
        get(detail, headers={"User-Agent": BROWSER_UA, "Accept": "text/html,application/xhtml+xml",
                             "Accept-Language": "ko-KR,ko;q=0.9"}, opener=opener)
    except Exception as exc:  # noqa: BLE001
        print(f"mirae detail page: {exc}", file=sys.stderr)
    page = get(
        "https://investments.miraeasset.com/tigeretf/ko/product/search/detail/price.ajax",
        headers={"User-Agent": BROWSER_UA, "X-Requested-With": "XMLHttpRequest",
                 "Accept": "text/html, */*; q=0.01", "Accept-Language": "ko-KR,ko;q=0.9",
                 "Origin": "https://investments.miraeasset.com",
                 "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                 "Referer": detail},
        data=body, opener=opener,
    )
    out: dict[str, dict] = {}
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", page, flags=re.S | re.I):
        cells = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.S | re.I)]
        if len(cells) < 4 or not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}", cells[0]):
            continue
        clean = [re.sub(r"^(상승|하락|보합)\s*", "", c) for c in cells]
        nav = num(clean[3])
        if nav:
            out[cells[0].replace(".", "-")] = {"close": num(clean[1]), "nav": nav}
    return out


def naver_nav_dated(code: str, close: float, prev_close: float | None, known_prev_nav: float | None) -> tuple[float, str]:
    """네이버 etfKeyIndicator 의 nav 는 운용사 일별 NAV 지만 날짜가 없다. 같이 오는 괴리율(deviationRate)로
    역산한 종가로 날짜를 정한다. 반환 (nav, "today" | "prev"):
      today = 역산 종가가 요약 거래일 종가와 맞고, 직전 거래일 종가와 구별되고, 직전 거래일 NAV 와 다름
      prev  = 역산 종가가 직전 거래일 종가와 맞음 (해외 ETF 는 그날 NAV 가 다음 날 아침에 나와 19:00 엔 보통 이 경우)
    둘 다 아니면 예외 (= 확인 불가)."""
    k = json.loads(get(f"https://m.stock.naver.com/api/stock/{code}/integration")).get("etfKeyIndicator") or {}
    nav, dev = num(k.get("nav")), num(k.get("deviationRate"))
    if not nav or dev is None:
        raise RuntimeError("네이버 nav 없음")
    dev = -abs(dev) if str(k.get("deviationSign") or "").strip() == "-" else abs(dev)
    implied = nav * (1 + dev / 100)
    tol = nav * 0.00005 + 0.6  # 괴리율이 소수 둘째 자리로 반올림된 만큼
    hit_today = abs(implied - close) <= tol
    hit_prev = prev_close is not None and abs(implied - prev_close) <= tol
    if hit_today and hit_prev:
        raise RuntimeError("전일 종가와 같아 NAV 날짜 구분 불가")
    if hit_prev:
        return nav, "prev"
    if not hit_today:
        raise RuntimeError(f"네이버 nav {nav:,.2f} 는 이 거래일이나 전 거래일 것이 아님(역산 종가 {implied:,.1f})")
    if known_prev_nav is not None and abs(nav - known_prev_nav) < 0.005:
        raise RuntimeError("직전 거래일 NAV 와 같음(아직 갱신 전)")
    return nav, "today"


def naver_nav(code: str, close: float, prev_close: float | None, known_prev_nav: float | None) -> float:
    """미래에셋 직접 조회가 막혔을 때만 쓰는 대체 경로 (krhc_monitor 도 이 계약으로 씀).
    그 거래일 NAV 로 확인될 때만 값을 돌려주고, 전일 NAV 이거나 날짜를 모르면 예외 (= 확인 불가)."""
    nav, which = naver_nav_dated(code, close, prev_close, known_prev_nav)
    if which != "today":
        implied_note = f"네이버 nav {nav:,.2f} 는 이 거래일 것이 아님(전 거래일 NAV)"
        raise RuntimeError(implied_note)
    return nav


def naver_inav(code: str) -> float:
    """네이버 integration totalInfos 의 code=='nav' (장중 실시간 추정 NAV, iNAV). 장 마감 뒤엔 마감 시점 추정값."""
    rows = json.loads(get(f"https://m.stock.naver.com/api/stock/{code}/integration")).get("totalInfos") or []
    for r in rows:
        if r.get("code") == "nav":
            v = num(r.get("value"))
            if v:
                return v
    raise RuntimeError("네이버 iNAV 없음")


# ---------- 3) 원/달러 (네이버 시장지표, 하나은행 고시) ----------
def usdkrw() -> dict[str, dict]:
    raw = json.loads(get("https://m.stock.naver.com/front-api/marketIndex/prices?category=exchange&reutersCode=FX_USDKRW&page=1&pageSize=10"))
    out: dict[str, dict] = {}
    for r in (raw or {}).get("result") or []:
        d = str(r.get("localTradedAt") or "")[:10]
        px = num(r.get("closePrice"))
        if len(d) == 10 and px:
            out[d] = {"px": px, "chg": num(r.get("fluctuations")), "pct": num(r.get("fluctuationsRatio"))}
    return out


# ---------- 4) 미국 상위 종목 ----------
def yahoo_last_move(sym: str, now_ny: datetime) -> tuple[date, float, float]:
    """(완료 일봉 날짜, 종가, 전일 대비 %) 진행 중인 오늘 봉은 뺀다."""
    raw = json.loads(get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=1mo&interval=1d"))
    res = raw["chart"]["result"][0]
    tz = now_ny.tzinfo
    bars: dict[date, float] = {}
    for t, c in zip(res.get("timestamp") or [], res["indicators"]["quote"][0].get("close") or []):
        if c is not None:
            bars[datetime.fromtimestamp(t, tz).date()] = float(c)
    meta = res.get("meta") or {}
    rmt, rmp = meta.get("regularMarketTime"), meta.get("regularMarketPrice")
    if rmt and rmp:
        mt = datetime.fromtimestamp(rmt, tz)
        if mt.hour * 60 + mt.minute >= 16 * 60:
            bars[mt.date()] = float(rmp)  # 마감 뒤 찍힌 정규장 종가로 null 봉 보정
    after_close = now_ny.hour * 60 + now_ny.minute >= 16 * 60 + 10
    rows = sorted((d, c) for d, c in bars.items() if d < now_ny.date() or (d == now_ny.date() and after_close))
    if len(rows) < 2:
        raise RuntimeError(f"{sym} 완료 일봉 {len(rows)}개")
    (_, prev), (d1, last) = rows[-2], rows[-1]
    return d1, last, (last / prev - 1) * 100


NASDAQ_H = {"User-Agent": BROWSER_UA, "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}


def nasdaq_last_move(sym: str) -> tuple[date, float, float]:
    d = json.loads(get(f"https://api.nasdaq.com/api/quote/{sym}/info?assetclass=stocks", headers=NASDAQ_H))["data"]["primaryData"]
    when = datetime.strptime(re.sub(r"\s+", " ", d["lastTradeTimestamp"]).strip()[:12].rstrip(), "%b %d, %Y").date()
    return when, float(num(d["lastSalePrice"].replace("$", ""))), float(num(d["percentageChange"]))


def yahoo_earnings(syms: list[str]) -> dict[str, tuple[list[date], bool]]:
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    try:
        get("https://fc.yahoo.com", opener=opener)
    except Exception:  # noqa: BLE001  (404여도 쿠키는 받음)
        pass
    crumb = get("https://query1.finance.yahoo.com/v1/test/getcrumb", opener=opener).strip()
    if not crumb or crumb.startswith("<"):
        raise RuntimeError("crumb empty")
    tz = ny_now().tzinfo
    out: dict[str, tuple[list[date], bool]] = {}
    for s in syms:
        try:
            d = json.loads(get(f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{s}?modules=calendarEvents&crumb={urllib.parse.quote(crumb)}", opener=opener))
            e = d["quoteSummary"]["result"][0]["calendarEvents"]["earnings"]
            ds = [datetime.fromtimestamp(x["raw"], tz).date() for x in e.get("earningsDate") or [] if x.get("raw")]
            if ds:
                out[s] = (ds, bool(e.get("isEarningsDateEstimate")))
        except Exception as exc:  # noqa: BLE001
            print(f"yahoo earnings {s}: {exc}", file=sys.stderr)
    return out


def nasdaq_earnings(sym: str) -> tuple[list[date], bool]:
    d = json.loads(get(f"https://api.nasdaq.com/api/analyst/{sym}/earnings-date", headers=NASDAQ_H))["data"]
    m = re.search(r":\s*([A-Z][a-z]{2} \d{1,2}, \d{4})", d.get("announcement") or "")
    if not m:
        raise RuntimeError("발표일 문구 없음")
    return [datetime.strptime(m.group(1), "%b %d, %Y").date()], "*" in (d.get("announcement") or "")


# ---------- 5) 구역 이벤트 (2026-11-12 채팅 승인): 상태가 바뀔 때만 별도 메시지 ----------
EV_DEFAULT = {"enabled": True, "prem_max_pct": 0.3, "ust10_below": 4.60, "ust10_days": 3, "cut_close": 32000}
UST10_URL = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/"
             "all/{ym}?type=daily_treasury_yield_curve&field_tdr_date_value_month={ym}&page&_format=csv")
EV_SIZE = "신규 한도는 총자산의 3%를 3회로 나눈 1회분(약 1%). ISA 계좌에서만. 추정 NAV 대비 +0.3% 이내 지정가, 시장가 금지. 보유 여부는 이 알림이 모릅니다."


def parse_ust10_csv(text: str) -> list[tuple[str, float]]:
    """재무부 Daily Treasury Par Yield Curve CSV 의 '10 Yr' 열. 최신순 (날짜, %)."""
    out: list[tuple[str, float]] = []
    for r in csv.DictReader(io.StringIO(text.lstrip("\ufeff"))):
        try:
            d = datetime.strptime((r.get("Date") or "").strip(), "%m/%d/%Y").date().isoformat()
        except ValueError:
            continue
        v = num(r.get("10 Yr"))
        if v is not None:
            out.append((d, v))
    return sorted(out, reverse=True)


def ust10_recent(day: date, n: int = 5) -> list[tuple[str, float]]:
    got: dict[str, float] = {}
    y, m = day.year, day.month
    for _ in range(2):
        for d, v in parse_ust10_csv(get(UST10_URL.format(ym=f"{y}{m:02d}"))):
            got[d] = v
        if len(got) >= n:
            break
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    if not got:
        raise RuntimeError("재무부 10년물 0건")
    return sorted(got.items(), reverse=True)[:n]


def sma(closes: list[float], n: int) -> float | None:
    return sum(closes[-n:]) / n if len(closes) >= n else None


def build_event_ctx(cfg: dict, skey: str, sess: date, inav: float | None, nav_val: float | None) -> dict:
    """정규장 종가(Daum, 실패 시 15:30 분봉)와 20/60/200일선, 괴리율, 미 10년물. 못 구하면 예외 = 이벤트 보류."""
    import krx_close  # scripts/ 가 sys.path 에 있음 (다른 monitor 와 같은 방식)

    bars = [b for b in krx_close.regular_bars(cfg["symbol"], 210, compare=False) if b.day <= skey]
    if not bars or bars[-1].day != skey:
        raise RuntimeError(f"정규장 일봉에 {skey} 없음")
    closes = [b.close for b in bars]
    if len(closes) < 60:
        raise RuntimeError(f"정규장 일봉 {len(closes)}개라 60일선 계산 불가({bars[-1].source})")
    close = closes[-1]
    prem, prem_src = None, ""
    if nav_val:
        prem, prem_src = (close / nav_val - 1) * 100, "공식 NAV"
    elif inav:
        prem, prem_src = (close / inav - 1) * 100, "iNAV 추정"
    ust: list[tuple[str, float]] = []
    ust_note = ""
    try:
        ust = ust10_recent(sess)
        if ust[0][0] < (sess - timedelta(days=5)).isoformat():
            ust_note = f"재무부 10년물 최신일 {ust[0][0]}가 오래됨"
            ust = []
    except Exception as exc:  # noqa: BLE001
        ust_note = f"재무부 10년물: {exc}"
    return {"skey": skey, "close": close, "src": bars[-1].source, "n": len(closes),
            "sma20": sma(closes, 20), "sma60": sma(closes, 60), "sma200": sma(closes, 200),
            "prem": prem, "prem_src": prem_src, "ust": ust, "ust_note": ust_note}


def zone_line(ctx: dict, ev: dict) -> str:
    def dist(v: float | None) -> str:
        return "계산 불가" if v is None else f"{v:,.0f}원({(ctx['close'] / v - 1) * 100:+.1f}%)"
    cut = float(ev["cut_close"])
    return (f"5) 분할 구역 (구역 알림은 상태가 바뀔 때만 따로 옴): 정규장 종가 {ctx['close']:,.0f}원, 60일선 {dist(ctx['sma60'])}, "
            f"200일선 {dist(ctx['sma200'])}, {cut:,.0f}원까지 {(ctx['close'] / cut - 1) * 100:+.1f}%")


def evaluate_events(ev: dict, st: dict, ctx: dict) -> list[dict]:
    """상태 st 를 제자리에서 바꾸고, 이번에 보낼 이벤트 목록을 돌려준다. 네트워크 없음."""
    out: list[dict] = []
    close, s20, s60, s200 = ctx["close"], ctx.get("sma20"), ctx.get("sma60"), ctx.get("sma200")
    prem = ctx.get("prem")
    gate = prem is not None and prem <= float(ev["prem_max_pct"])
    for key, line, label, title in (
        ("z1", s60, "60일선", "[203780] 1차 분할 구간 | 매수 검토, 주문 아님"),
        ("z2", s200, "200일선", "[203780] 2차 분할 구간 | 매수 검토, 주문 아님"),
    ):
        if line is None:
            continue
        if close > line:
            st[key + "_armed"] = True  # 종가가 그 선 위로 돌아오면 재무장
        elif st.get(key + "_armed", True) and gate:
            st[key + "_armed"] = False
            out.append({"key": key, "title": title, "label": label, "line": line})
    n_days, below = int(ev["ust10_days"]), float(ev["ust10_below"])
    ust = ctx.get("ust") or []
    active = st.get("z1_armed", True) is False or st.get("z2_armed", True) is False
    ust_ok = len(ust) >= n_days and all(v < below for _, v in ust[:n_days])
    if (ust and ust[0][1] >= below) or (s20 is not None and close > s20):
        st["expand_armed"] = True
    elif active and ust_ok and st.get("expand_armed", True):
        st["expand_armed"] = False
        out.append({"key": "expand", "title": "[203780] 비중 확대 검토 | 금리 조건, 주문 아님"})
    cut = float(ev["cut_close"])
    stage = st.get("cut_stage")
    if stage == "warn":
        if st.get("cut_warn_day") != ctx["skey"] and s200 is not None:  # 다음 거래일 판정
            if close < s200:
                st["cut_stage"] = "confirmed"
                out.append({"key": "cut_confirm", "title": "[203780] 축소 확인 | 주문 아님", "line": s200})
            else:
                st["cut_stage"] = None  # 200일선 위로 돌아왔으니 해제
    elif stage == "confirmed":
        if s200 is not None and close > s200:
            st["cut_stage"] = None
    elif close < cut:
        st["cut_stage"] = "warn"
        st["cut_warn_day"] = ctx["skey"]
        out.append({"key": "cut_warn", "title": f"[203780] 축소 경계 | 종가 {cut:,.0f}원 아래, 주문 아님", "line": s200})
    return out


def event_msg(stamp: str, ctx: dict, ev: dict, e: dict) -> str:
    def ln(v: float | None) -> str:
        return "계산 불가" if v is None else f"{v:,.0f}원"
    items = [f"1) {ctx['skey'][5:].replace('-', '/')} 정규장 종가 {ctx['close']:,.0f}원 ({ctx['src']} 일봉), "
             f"20일선 {ln(ctx['sma20'])}, 60일선 {ln(ctx['sma60'])}, 200일선 {ln(ctx['sma200'])}"]
    if ctx.get("prem") is not None:
        items.append(f"2) 괴리율 {fmt_pct(ctx['prem'])} ({ctx['prem_src']}, 기준 {fmt_pct(float(ev['prem_max_pct']))} 이내)")
    else:
        items.append("2) 괴리율 계산 불가 (공식 NAV와 iNAV 모두 없음)")
    if ctx.get("ust"):
        last = ctx["ust"][: int(ev["ust10_days"])]
        items.append("3) 미 10년물 (재무부 종가) " + ", ".join(f"{d[5:].replace('-', '/')} {v:.2f}%" for d, v in last)
                     + f", 확대 기준 {float(ev['ust10_below']):.2f}% 미만 {len(last)}거래일 연속")
    else:
        items.append(f"3) 미 10년물 계산 불가 ({ctx.get('ust_note') or '자료 없음'})")
    cut = float(ev["cut_close"])
    k = e["key"]
    reduce = f"익절/줄일 때: 종가 {cut:,.0f}원 아래면 축소 경계, 다음 거래일 종가도 200일선 아래면 축소 확인이 따로 옵니다."
    avoid = "하지 말 것: 시장가 매수, 추정 괴리 +0.3% 넘는 가격 추격, 10년물 4.60% 이상에서 비중 확대, 이 알림만 보고 한꺼번에 사기."
    if k in ("z1", "z2"):
        nth = "1차" if k == "z1" else "2차"
        what = f"이게 뭐 알림: 종가가 {e['label']} 이하로 내려와 {nth} 분할 구간에 들어갔습니다. 매수 검토 신호이고 주문이 아닙니다."
        action = f"{nth} 1회분만 검토. {EV_SIZE}"
        later = f"나중에: 종가가 {e['label']} 위로 돌아오면 다시 무장합니다. 그 전에는 같은 구간 알림이 다시 오지 않습니다."
    elif k == "expand":
        what = "이게 뭐 알림: 분할 구간이 켜진 상태에서 미 10년물이 3거래일 연속 4.60% 미만입니다. 비중 확대를 검토할 수 있습니다. 주문이 아닙니다."
        action = "이미 채운 분할분 뒤에 총자산 3% 한도 안에서만 검토. ISA 계좌에서만, 지정가."
        later = "나중에: 10년물이 4.60% 이상이거나 종가가 20일선 위로 오면 다시 무장합니다."
    elif k == "cut_warn":
        what = f"이게 뭐 알림: 종가가 {cut:,.0f}원 아래로 내려왔습니다. 축소 경계입니다. 주문이 아닙니다."
        action = f"오늘 팔 필요는 없습니다. 다음 거래일 종가가 200일선({ln(e.get('line'))}) 아래면 축소 확인이 옵니다."
        later = "나중에: 다음 거래일 19:00에 판정합니다. 200일선 위로 돌아오면 조용히 해제됩니다."
    else:
        what = "이게 뭐 알림: 종가 32,000원 이탈 뒤 다음 거래일에도 200일선 회복에 실패했습니다. 축소 확인입니다. 주문이 아닙니다."
        action = f"위성 비중 축소를 검토하세요. 200일선({ln(e.get('line'))}). 비중과 손절은 미리 정한 계획대로."
        later = "나중에: 종가가 200일선 위로 돌아오면 해제됩니다."
        reduce = "익절/줄일 때: 이번 알림이 축소 검토 시점입니다. 한 번에 다 팔지 말고 계획한 비율대로."
    return body(stamp, items, action, what=what, later=later, reduce=reduce, avoid=avoid)


def selftest() -> int:
    ev = dict(EV_DEFAULT)
    fails: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            fails.append(f"{name}: got {got!r} want {want!r}")

    def ctx(close, s20=35000.0, s60=34900.0, s200=33300.0, prem=0.1, ust=None, skey="2026-11-12"):
        if ust is None:
            ust = [("2026-11-12", 4.70), ("2026-11-11", 4.72), ("2026-11-10", 4.80)]
        return {"skey": skey, "close": close, "src": "daum", "n": 210, "sma20": s20, "sma60": s60, "sma200": s200,
                "prem": prem, "prem_src": "iNAV 추정", "ust": ust, "ust_note": ""}

    keys = lambda r: [e["key"] for e in r]  # noqa: E731
    st: dict = {}
    check("above both: nothing", keys(evaluate_events(ev, st, ctx(36000))), [])
    check("z1 fires", keys(evaluate_events(ev, st, ctx(34800))), ["z1"])
    check("z1 not twice", keys(evaluate_events(ev, st, ctx(34700))), [])
    check("z1 rearm above 60", (keys(evaluate_events(ev, st, ctx(35100))), st["z1_armed"]), ([], True))
    check("z1 fires again", keys(evaluate_events(ev, st, ctx(34800))), ["z1"])
    st = {}
    check("gate blocks z1", (keys(evaluate_events(ev, st, ctx(34800, prem=0.5))), st.get("z1_armed", True)), ([], True))
    check("gate none blocks", keys(evaluate_events(ev, st, ctx(34800, prem=None))), [])
    check("gate opens", keys(evaluate_events(ev, st, ctx(34800, prem=0.3))), ["z1"])
    st = {}
    check("z1+z2 same day", keys(evaluate_events(ev, st, ctx(33200))), ["z1", "z2"])
    st = {}
    low = [("2026-11-12", 4.55), ("2026-11-11", 4.58), ("2026-11-10", 4.59)]
    check("expand needs zone", keys(evaluate_events(ev, st, ctx(36000, ust=low))), [])
    st = {"z1_armed": False}
    check("expand needs 3 days", keys(evaluate_events(ev, st, ctx(34800, ust=low[:2]))), [])
    check("expand fires", keys(evaluate_events(ev, st, ctx(34800, ust=low))), ["expand"])
    check("expand not twice", keys(evaluate_events(ev, st, ctx(34800, ust=low))), [])
    hi = [("2026-11-13", 4.61)] + low
    check("expand rearm on 4.60+", (keys(evaluate_events(ev, st, ctx(34800, ust=hi))), st["expand_armed"]), ([], True))
    st = {"z2_armed": False, "expand_armed": False}
    check("expand rearm above sma20", (keys(evaluate_events(ev, st, ctx(35500, ust=low))), st["expand_armed"]), ([], True))
    st = {}
    r = evaluate_events(ev, st, ctx(31900, skey="2026-11-12"))
    check("cut warn", (keys(r), st["cut_stage"]), (["z1", "z2", "cut_warn"], "warn"))
    check("warn same day no confirm", keys(evaluate_events(ev, st, ctx(31800, skey="2026-11-12"))), [])
    r = evaluate_events(ev, st, ctx(31800, skey="2026-11-13"))
    check("cut confirm", (keys(r), st["cut_stage"]), (["cut_confirm"], "confirmed"))
    check("confirm not twice", keys(evaluate_events(ev, st, ctx(31700, skey="2026-11-16"))), [])
    check("confirm clears above 200", (keys(evaluate_events(ev, st, ctx(33500, skey="2026-11-17"))), st["cut_stage"]), ([], None))
    st = {"cut_stage": "warn", "cut_warn_day": "2026-11-12", "z1_armed": False}
    check("warn clears when back above 200", (keys(evaluate_events(ev, st, ctx(33500, skey="2026-11-13"))), st["cut_stage"]), ([], None))
    st = {}
    r = evaluate_events(ev, st, ctx(31900, s200=None))
    check("no sma200: z2 skipped, warn still", keys(r), ["z1", "cut_warn"])
    # 메시지 모양: 제목이 ops 로 새지 않고, 본문 순서가 규칙대로
    try:
        from ntfy_util import OPS_TITLE_RE
    except Exception:  # noqa: BLE001
        OPS_TITLE_RE = re.compile(r"조회\s?실패|수집\s?실패|파싱\s?실패|해석\s?불가|수급 누락|확인 불가|숫자 미확인|미게시")
    for e in (evaluate_events(dict(ev), {}, ctx(31900)) + [{"key": "expand", "title": "[203780] 비중 확대 검토 | 금리 조건, 주문 아님"},
                                                          {"key": "cut_confirm", "title": "[203780] 축소 확인 | 주문 아님", "line": 33300.0}]):
        check(f"title not ops {e['key']}", bool(OPS_TITLE_RE.search(e["title"])), False)
        check(f"title ticker {e['key']}", e["title"].startswith("[203780]"), True)
        m = event_msg("2026-11-12 19:00 KST", ctx(31900), ev, e)
        pos = [m.find(x) for x in ("이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것")]
        check(f"body order {e['key']}", pos == sorted(pos) and min(pos) >= 0, True)
    check("ust csv parse", parse_ust10_csv('Date,"1 Mo","10 Yr"\n11/10/2026,4.05,4.98\n11/09/2026,4.04,4.88\n'),
          [("2026-11-10", 4.98), ("2026-11-09", 4.88)])
    for f in fails:
        print("FAIL", f)
    print("k203780 selftest", "FAIL" if fails else "ok")
    return 1 if fails else 0


# ---------- 조립 ----------
def fmt_pct(v: float) -> str:
    return f"{v:+.2f}%"


def body(stamp: str, items: list[str], action: str, *, what: str | None = None, later: str | None = None,
         reduce: str | None = None, avoid: str | None = None) -> str:
    return "\n".join([
        f"{stamp} | 203780 | TIGER 미국나스닥바이오",
        what or "이게 뭐 알림: TIGER 미국나스닥바이오 거래일 장 마감 뒤 하루 1번 요약(종가와 NAV, 등락률, 원/달러, 상위 5종목). 매매 신호 아님.",
        "지금 결과/상태:",
        *items,
        f"지금 할 일: {action}",
        later or "나중에: 다음 거래일 19:00 무렵 다시 옵니다. 휴장일은 오지 않습니다. 실적일이 다가오면 그 종목 뉴스부터 확인.",
        reduce or "익절/줄일 때: 이 요약만으로 정하지 않기. 미리 정한 비중과 손절 기준대로.",
        avoid or "하지 말 것: 괴리율 하나만 보고 사거나 팔기, 하루 급등락 추격, 확인 불가 항목을 0이나 정상으로 읽기.",
    ])


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode not in {"auto", "dry", "force", "install", "selftest"}:
        print(f"unknown mode {mode}")
        return 2
    if mode == "selftest":
        return selftest()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    th = cfg["thresholds"]
    now = datetime.now(KST)
    now_ny = ny_now()
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    dry = mode == "dry"

    # 어느 거래일을 요약하나: 새벽 6시 전 늦은 실행은 전날, 장 마감(15:40) 전 수동 실행은 직전 거래일
    sess = session_day(now)
    close_h, close_m = map(int, cfg.get("market_close_kst", "15:40").split(":"))
    if sess == now.date() and (now.hour, now.minute) < (close_h, close_m):
        sess = prev_trading_day(sess)
    if not is_trading_day(sess):
        if mode == "auto":
            print(f"{sess} KRX 휴장일; 보내지 않음")
            return 0
        sess = prev_trading_day(sess)
    skey = sess.isoformat()
    print(f"session={skey} mode={mode}")

    state = {"updated_at": None, "last_sent_session": None, "fetch_fail_session": None}
    if STATE_PATH.exists():
        try:
            state.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    if mode == "auto" and state.get("last_sent_session") == skey:
        print(f"{skey} 이미 보냄; skip")
        return 0

    triggers: list[str] = []
    errs: list[str] = []

    # 1, 2) 가격
    px = None
    try:
        daily = naver_daily(cfg["symbol"])
        if not daily:
            raise RuntimeError("시세 0건")
        if skey not in daily:
            latest = max(daily)
            if latest < skey:
                print(f"네이버 최신 시세 {latest} < {skey}: 그날 거래 데이터 없음(휴장으로 판단); 보내지 않음")
                return 0
            raise RuntimeError(f"{skey} 행 없음")
        px = daily[skey]
    except Exception as exc:  # noqa: BLE001
        errs.append(f"네이버 가격: {exc}")

    if px is None:
        # 가격이 없으면 휴장인지도 모르므로 요약 대신 운영 알림 1회 (cwally-ops로 라우팅)
        title = "[203780] 가격 조회 실패 | 일일 요약 못 보냄"
        msg = body(stamp, [f"1) 종가: {NA} ({'; '.join(errs)})"], "증권사 앱에서 203780 종가를 직접 보세요.")
        print(title)
        print(msg)
        if dry:
            print("DRY RUN: no publish, state not written")
            return 0
        if mode == "install" or state.get("fetch_fail_session") != skey:
            publish(topic, title, msg, 3, ["warning"], server)
        if mode != "install":
            state["fetch_fail_session"] = skey
            state["updated_at"] = now.isoformat()
            STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0

    close = px["close"]
    # 1) NAV + 괴리율. 해외 ETF 라 그날 공식 NAV 는 다음 날 아침에 나온다. 19:00 엔 보통
    #    전일 공식 NAV(날짜 확인한 값) + 오늘 장 마감 iNAV 추정을 같이 보여 준다.
    prev_keys = sorted(d for d in daily if d < skey)
    pkey = prev_keys[-1] if prev_keys else None
    prev_close = daily[pkey]["close"] if pkey else None
    nav_hist = state.get("nav_by_session") or {}
    nav_val: float | None = None  # 그 거래일(skey) 공식 NAV
    nav_src = "미래에셋"
    prev_nav: float | None = None  # 직전 거래일(pkey) 공식 NAV
    nav_note = ""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print("미래에셋 건너뜀: GitHub runner IP 는 403 이라 네이버로 바로 감")
        nav_note = "미래에셋은 GitHub 실행에서 막힘"
    else:
        try:
            navs = mirae_nav(cfg["isin"], sess)
            if skey in navs:
                nav_val = navs[skey]["nav"]
            if pkey and pkey in navs:
                prev_nav = navs[pkey]["nav"]
            if nav_val is None:
                nav_note = f"미래에셋에 {skey} 기준가 아직 없음"
        except Exception as exc:  # noqa: BLE001  403 등은 예상된 대체 경로, 오류로 세지 않음
            print(f"미래에셋 NAV 조회 안 됨({exc}); 네이버로 대체")
            nav_note = "미래에셋 조회 안 됨"
    if nav_val is None:
        try:
            n, which = naver_nav_dated(cfg["symbol"], close, prev_close, nav_hist.get(pkey) if pkey else None)
            if which == "today":
                nav_val, nav_src = n, "네이버 표시값"
            elif prev_nav is None:
                prev_nav = n
        except Exception as exc:  # noqa: BLE001
            errs.append(f"네이버 NAV: {exc}")
            nav_note += ", 네이버 대체값도 날짜 확인 안 됨"
    inav: float | None = None
    try:
        inav = naver_inav(cfg["symbol"])
    except Exception as exc:  # noqa: BLE001
        errs.append(f"네이버 iNAV: {exc}")
    parts: list[str] = []
    if nav_val is not None:
        prem = (close / nav_val - 1) * 100
        src_txt = f" ({nav_src})" if nav_src != "미래에셋" else ""
        parts.append(f"운용사 NAV {nav_val:,.2f}원{src_txt}, 괴리율 {fmt_pct(prem)} (참고 지표, 매매 신호 아님)")
    else:
        if prev_nav is not None and pkey and prev_close:
            parts.append(f"전일({date.fromisoformat(pkey):%m/%d}) 운용사 NAV {prev_nav:,.2f}원, 전일 괴리율 {fmt_pct((prev_close / prev_nav - 1) * 100)}")
        else:
            parts.append(f"운용사 NAV {NA} ({nav_note.lstrip(', ')}), 괴리율 {NA}")
        if inav is not None:
            parts.append(f"오늘 장 마감 iNAV 추정 {inav:,.2f}원, 추정 괴리율 {fmt_pct((close / inav - 1) * 100)} (추정, 참고 지표)")
        else:
            parts.append(f"오늘 iNAV {NA}")
        parts.append(f"{sess:%m/%d} 공식 NAV는 다음 날 아침에 나옴")
    nav_txt = ", ".join(parts)
    item1 = f"1) {sess:%m/%d} 종가 {close:,.0f}원, {nav_txt}"

    # 2) 등락률
    pct = px.get("pct")
    if pct is None:
        item2 = f"2) 하루 등락률 {NA}"
    else:
        chg = px.get("chg")
        chg_txt = f" ({chg:+,.0f}원)" if chg is not None else ""
        hot = abs(pct) >= float(th["etf_day_pct"])
        item2 = f"2) 하루 등락률 {fmt_pct(pct)}{chg_txt}"
        if hot:
            item2 = f"2) [강조] 하루 등락률 {fmt_pct(pct)}{chg_txt}, ±{th['etf_day_pct']:g}% 이상"
            triggers.append(f"203780 {fmt_pct(pct)}")

    # 3) 원/달러
    try:
        fx = usdkrw()
        r = fx.get(skey)
        if not r:
            raise RuntimeError(f"{skey} 고시 없음 (최근 {max(fx) if fx else '없음'})")
        fpct = r["pct"]
        if fpct is None and r["chg"] is not None:
            fpct = r["chg"] / (r["px"] - r["chg"]) * 100
        chg_txt = f"{r['chg']:+.2f}원" if r["chg"] is not None else f"{NA}"
        pct_txt = fmt_pct(fpct) if fpct is not None else NA
        item3 = f"3) 원/달러 {r['px']:,.2f}원, 전일 대비 {chg_txt} ({pct_txt}), 하나은행 고시 매매기준율"
        if fpct is not None and abs(fpct) >= float(th["fx_day_pct"]):
            item3 = "3) [강조] " + item3[3:] + f", ±{th['fx_day_pct']:g}% 이상"
            triggers.append(f"원/달러 {fmt_pct(fpct)}")
    except Exception as exc:  # noqa: BLE001
        errs.append(f"환율: {exc}")
        item3 = f"3) 원/달러 {NA}"

    # 4) 상위 5종목
    syms = cfg["holdings"]["symbols"]
    names = cfg["holdings"]["names"]
    moves: dict[str, tuple[date, float, float] | None] = {}
    for s in syms:
        try:
            moves[s] = yahoo_last_move(s, now_ny)
        except Exception as exc:  # noqa: BLE001
            try:
                moves[s] = nasdaq_last_move(s)
                print(f"{s} yahoo 실패({exc}), nasdaq 사용", file=sys.stderr)
            except Exception as exc2:  # noqa: BLE001
                errs.append(f"{s} 시세: {exc}; {exc2}")
                moves[s] = None
    earn: dict[str, tuple[list[date], bool]] = {}
    try:
        earn = yahoo_earnings(syms)
    except Exception as exc:  # noqa: BLE001
        print(f"yahoo earnings 실패: {exc}", file=sys.stderr)
    for s in syms:
        if s not in earn:
            try:
                earn[s] = nasdaq_earnings(s)
            except Exception as exc:  # noqa: BLE001
                errs.append(f"{s} 실적일: {exc}")

    hdates = sorted({m[0] for m in moves.values() if m})
    h_hdr = f"4) 상위 5종목 ({'/'.join(f'{d:%m/%d}' for d in hdates)} 미국 종가 기준)" if hdates else "4) 상위 5종목"
    lines = [h_hdr + ":"]
    today_ny = now_ny.date()
    horizon = today_ny + timedelta(days=int(th["earnings_days"]))
    soon: list[str] = []
    for s in syms:
        m = moves.get(s)
        if m is None:
            mv = f"{s} {NA}"
        else:
            d1, last, chg = m
            mv = f"{s} {fmt_pct(chg)} (${last:,.2f})"
            if abs(chg) >= float(th["holding_day_pct"]):
                mv = f"[강조] {mv}, ±{th['holding_day_pct']:g}% 이상"
                triggers.append(f"{s} {fmt_pct(chg)}")
        if s not in earn:
            et = f"실적일 {NA}"
        else:
            ds, est = earn[s]
            within = [d for d in ds if today_ny <= d <= horizon]
            if within:
                dtxt = "~".join(f"{d:%m/%d}" for d in ds) if len(ds) > 1 else f"{ds[0]:%m/%d}"
                et = f"실적 발표 {dtxt}(미국){' 예상' if est else ''}, 7일 안"
                soon.append(f"{s} {dtxt}")
            else:
                et = "7일 안 실적 없음"
        lines.append(f"  {names.get(s, s)} {mv}, {et}")
    item4 = "\n".join(lines)

    # 5) 분할 구역 한 줄 + 구역 이벤트 (실패해도 일일 요약은 그대로 보낸다)
    ev = {**EV_DEFAULT, **(cfg.get("events") or {})}
    ev_state = dict(state.get("events") or {})
    ev_out: list[dict] = []
    ev_ctx: dict | None = None
    items = [item1, item2, item3, item4]
    if ev.get("enabled") and mode != "install":
        try:
            ev_ctx = build_event_ctx(cfg, skey, sess, inav, nav_val)
            ev_out = evaluate_events(ev, ev_state, ev_ctx)
            items.append(zone_line(ev_ctx, ev))
            if ev_ctx.get("ust_note"):
                errs.append(ev_ctx["ust_note"])
        except Exception as exc:  # noqa: BLE001
            errs.append(f"구역 판정: {exc}")
            items.append(f"5) 분할 구역: {NA} ({exc}). 구역 알림은 이번에 보류")
    if errs:
        print("errors: " + " | ".join(errs), file=sys.stderr)
    hot = bool(triggers)
    if mode == "install":
        title = "[203780] 설치 확인 | 매매 신호 아님"
    elif hot:
        title = f"[203780] {sess:%m/%d} 일일 요약 | 강조: {', '.join(triggers)}"
    else:
        title = f"[203780] {sess:%m/%d} 일일 요약 | 특이사항 없음, 매매 신호 아님"
    if hot:
        action = "강조 항목의 원인(종목 뉴스, 환율)부터 확인하세요. 주문은 미리 정한 계획대로만."
    else:
        action = "확인만 하세요. 할 일 없음."
    if soon:
        action += f" 7일 안 실적: {', '.join(soon)}."
    msg = body(stamp, items, action)
    priority = 4 if hot else 3
    tags = ["rotating_light", "dna"] if hot else ["dna"]
    print(f"title: {title}\npriority={priority} tags={tags}\n{msg}")
    ev_msgs = [(e["title"], event_msg(stamp, ev_ctx, ev, e)) for e in ev_out] if ev_ctx else []
    for et, em in ev_msgs:
        print(f"\nEVENT title: {et}\npriority=4 tags=['bell', 'dna']\n{em}")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    publish(topic, title, msg, priority, tags, server)
    if mode == "install":
        print("install confirmation sent (state not written)")
        return 0
    for et, em in ev_msgs:  # 요약 뒤에 별도 메시지. state 는 맨 끝에서 한 번만 쓴다
        publish(topic, et, em, 4, ["bell", "dna"], server)
    hist = dict(state.get("nav_by_session") or {})
    if nav_val is not None:
        hist[skey] = nav_val
    if prev_nav is not None and pkey:
        hist[pkey] = prev_nav  # 공식 NAV 는 그 값의 실제 날짜로 저장
    state["nav_by_session"] = {k: hist[k] for k in sorted(hist)[-10:]}
    state.update(last_sent_session=skey, fetch_fail_session=None, updated_at=now.isoformat())
    if ev_ctx is not None:
        state["events"] = ev_state
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
