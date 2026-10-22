#!/usr/bin/env python3
"""TIGER 미국나스닥바이오 (KRX 203780) 일일 요약.

KRX 거래일 장 마감 뒤(평일 19:00 KST, cron-job.org 정시 dispatch + GitHub cron 0 10 * * 1-5 백업) 하루 1번, 메시지 하나에 4가지:
  1) 종가, 운용사(미래에셋) 일별 NAV, 괴리율 = (종가/NAV - 1) x 100  (참고 지표, 매매 신호 아님)
  2) 하루 등락률 (|등락| >= 3% 강조)
  3) 원/달러 환율과 전일 대비 (|변동| >= 1% 강조)
  4) 상위 5종목 GILD, AMGN, VRTX, MRNA, REGN 최근 완료 일봉 등락 (|등락| >= 5% 강조) + 7일 안 실적 발표일
강조가 하나라도 걸리면 priority 4(high) + rotating_light 태그, 아니면 3.
어느 항목이든 조회에 실패하면 그 항목은 '확인 불가'로 적는다. 숫자를 만들어 넣지 않는다.
휴장일(kr_calendar 또는 네이버에 그 거래일 시세 없음)이면 보내지 않는다. 같은 거래일은 한 번만.
modes: auto(기본) | dry(발송 없음, state 안 씀) | force(같은 거래일 중복 검사 무시하고 발송) |
       install(설치 확인 1회, state 안 씀)
"""
from __future__ import annotations

import html
import json
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


def naver_nav(code: str, close: float, prev_close: float | None, known_prev_nav: float | None) -> float:
    """미래에셋 직접 조회가 막혔을 때만 쓰는 대체 경로. 네이버 etfKeyIndicator 의 nav 는 운용사 일별 NAV 지만
    날짜가 없다. 같이 오는 괴리율(deviationRate)로 역산한 종가가 요약 거래일 종가와 맞고, 직전 거래일 종가와는
    구별되고, 직전 거래일에 쓴 NAV 와 다를 때만 그 거래일 NAV 로 인정한다. 아니면 예외 (= 확인 불가)."""
    k = json.loads(get(f"https://m.stock.naver.com/api/stock/{code}/integration")).get("etfKeyIndicator") or {}
    nav, dev = num(k.get("nav")), num(k.get("deviationRate"))
    if not nav or dev is None:
        raise RuntimeError("네이버 nav 없음")
    dev = -abs(dev) if str(k.get("deviationSign") or "").strip() == "-" else abs(dev)
    implied = nav * (1 + dev / 100)
    tol = nav * 0.00005 + 0.6  # 괴리율이 소수 둘째 자리로 반올림된 만큼
    if abs(implied - close) > tol:
        raise RuntimeError(f"네이버 nav {nav:,.2f} 는 이 거래일 것이 아님(역산 종가 {implied:,.1f})")
    if prev_close is not None and abs(implied - prev_close) <= tol:
        raise RuntimeError("전일 종가와 같아 NAV 날짜 구분 불가")
    if known_prev_nav is not None and abs(nav - known_prev_nav) < 0.005:
        raise RuntimeError("직전 거래일 NAV 와 같음(아직 갱신 전)")
    return nav


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


# ---------- 조립 ----------
def fmt_pct(v: float) -> str:
    return f"{v:+.2f}%"


def body(stamp: str, items: list[str], action: str) -> str:
    return "\n".join([
        f"{stamp} | 203780 | TIGER 미국나스닥바이오",
        "이게 뭐 알림: TIGER 미국나스닥바이오 거래일 장 마감 뒤 하루 1번 요약(종가와 NAV, 등락률, 원/달러, 상위 5종목). 매매 신호 아님.",
        "지금 결과/상태:",
        *items,
        f"지금 할 일: {action}",
        "나중에: 다음 거래일 19:00 무렵 다시 옵니다. 휴장일은 오지 않습니다. 실적일이 다가오면 그 종목 뉴스부터 확인.",
        "익절/줄일 때: 이 요약만으로 정하지 않기. 미리 정한 비중과 손절 기준대로.",
        "하지 말 것: 괴리율 하나만 보고 사거나 팔기, 하루 급등락 추격, 확인 불가 항목을 0이나 정상으로 읽기.",
    ])


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode not in {"auto", "dry", "force", "install"}:
        print(f"unknown mode {mode}")
        return 2
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
    # 1) NAV + 괴리율 (미래에셋 기준가격 표 -> 막히면 날짜 검증한 네이버 nav)
    nav_val: float | None = None
    nav_src = "미래에셋"
    nav_note = ""
    try:
        navs = mirae_nav(cfg["isin"], sess)
        row = navs.get(skey)
        if row:
            nav_val = row["nav"]
        else:
            nav_note = f"미래에셋에 {skey} 기준가 아직 없음, 최근 {max(navs) if navs else '없음'}"
    except Exception as exc:  # noqa: BLE001
        errs.append(f"미래에셋 NAV: {exc}")
        nav_note = "미래에셋 조회 실패"
    if nav_val is None:
        prev_keys = sorted(d for d in daily if d < skey)
        prev_close = daily[prev_keys[-1]]["close"] if prev_keys else None
        nav_hist = state.get("nav_by_session") or {}
        prev_nav = nav_hist.get(prev_keys[-1]) if prev_keys else None
        try:
            nav_val = naver_nav(cfg["symbol"], close, prev_close, prev_nav)
            nav_src = "네이버 표시값, 미래에셋 직접 조회 안 됨"
        except Exception as exc:  # noqa: BLE001
            errs.append(f"네이버 NAV: {exc}")
            nav_note += f", 네이버 대체값도 날짜 확인 안 됨"
    if nav_val is not None:
        prem = (close / nav_val - 1) * 100
        src_txt = f" ({nav_src})" if nav_src != "미래에셋" else ""
        nav_txt = f"운용사 NAV {nav_val:,.2f}원{src_txt}, 괴리율 {fmt_pct(prem)} (참고 지표, 매매 신호 아님)"
    else:
        nav_txt = f"운용사 NAV {NA} ({nav_note}), 괴리율 {NA}"
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

    items = [item1, item2, item3, item4]
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
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    publish(topic, title, msg, priority, tags, server)
    if mode == "install":
        print("install confirmation sent (state not written)")
        return 0
    hist = dict(state.get("nav_by_session") or {})
    if nav_val is not None:
        hist[skey] = nav_val
    state["nav_by_session"] = {k: hist[k] for k in sorted(hist)[-10:]}
    state.update(last_sent_session=skey, fetch_fail_session=None, updated_at=now.isoformat())
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
