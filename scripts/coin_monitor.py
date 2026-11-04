#!/usr/bin/env python3
"""COIN(Coinbase) 분할 계획 단계 조건 알림 (change-only). 2026-11-04 t49u 재설계.

morning 슬롯(평일 09:00 KST). 미국 직전 완료 일봉 종가만 본다(진행 중인 봉은 제외). 수량이나 비중은 말하지 않는다.
- 경계: 종가 <= 135달러(52주 저점 135.46 이탈). 매수 신호 아님. 재무장: 종가 > 142
- 하향 확인: 종가 < 130달러가 2거래일 연속. "하향 신호, 저가 매수 보류". 해제(재무장): 종가 >= 130이 2거래일 연속
- 2차-A 저가 매수 규칙: 종가 <= 125를 찍은 것만으로는 사지 않는다(그때는 "보류" 안내 1회).
  최근 lookback(15)거래일 안에 종가 <= 125가 있었고, 그 뒤 종가 >= 130이 2거래일 연속(=하향 확인 해제)일 때만 1회.
  하향 확인이 풀리지 않은 상태에서는 절대 저가 매수 문구를 보내지 않는다. 재무장: 종가 > 140
- 2차-B 추세 매수 규칙: 종가 >= 178 AND 종가 >= 50일 이동평균 AND BTC-USD 완료 일봉(UTC) 종가 >= 90,000. 재무장: 종가 < 170
- 상단: 종가 >= 220 (기존 그대로). 재무장: 종가 < 205
- 4분기 실적: SEC에서 2026년 4분기 실적(10-K 또는 8-K Item 2.02)을 찾아 거래 매출을 3분기(10-Q 590,224천 달러)와 비교, 1회.
  10-K 연간 "Total transaction revenue" - 9개월 누계(10-Q 1,945,205천 달러) = 4분기(정확값). 10-K가 없으면 8-K 첨부(덱 또는
  주주서한) 문장 "Transaction revenue ... $NNN million/M"과 "Q/Q" 방향 문장으로 판독. 숫자가 3분기와 1백만 달러 안이거나
  출처끼리 방향이 어긋나면 판독하지 않고 수동 확인 안내(1회), 창 안에서 다음 실행 때 다시 시도.
modes: auto(기본) | dry(발송 없음, state 안 씀) | install(설치 확인 1회, state 안 씀) | selftest(오프라인)
       | earnings-dry [10-K 접수번호 9개월누계천달러 직전분기천달러] (판독만 출력)
"""
from __future__ import annotations

import html
import json
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

KST = timezone(timedelta(hours=9))
NAME = "coin"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
UA = {"User-Agent": "Mozilla/5.0 (cwally-alerts; +mailto:sosmos3218@gmail.com)"}
SEC_UA = {"User-Agent": "cwally-alerts sosmos3218@gmail.com"}


def ny_now() -> datetime:
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo("America/New_York"))
        except Exception:  # noqa: BLE001
            pass
    off = -5 if datetime.now(timezone.utc).month in (11, 12, 1, 2, 3) else -4
    return datetime.now(timezone(timedelta(hours=off)))


STATE_KEYS = {
    "updated_at": None,
    "warn_armed": True,
    "down_active": False,
    "a_armed": True,
    "a_hold_sent": False,
    "b_armed": True,
    "top_armed": True,
    "earn_parsed": False,
    "earn_manual_sent": False,
    "last_bar_date": None,
    "last_close": None,
    "fetch_fail_date": None,
}


def load_state() -> dict:
    base = dict(STATE_KEYS)
    if STATE_PATH.exists():
        try:
            old = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            base.update({k: v for k, v in old.items() if k in STATE_KEYS})  # 10/22 설계의 키(buy/break/pre/post)는 버림
        except json.JSONDecodeError:
            pass
    return base


def _yahoo(symbol: str) -> dict:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1y&interval=1d"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())["chart"]["result"][0]


def fetch_bars(symbol: str, now_ny: datetime) -> list[tuple[date, float]]:
    """완료 일봉 (미국 동부 날짜, 종가) 오름차순. 진행 중인 오늘 봉은 뺀다."""
    res = _yahoo(symbol)
    tz = now_ny.tzinfo
    out: dict[date, float] = {}
    for t, c in zip(res.get("timestamp") or [], res["indicators"]["quote"][0].get("close") or []):
        if c is None:
            continue
        out[datetime.fromtimestamp(t, tz).date()] = float(c)
    # 직전 거래일 일봉 close가 한동안 null로 오므로, 장 마감 뒤 찍힌 regularMarketPrice로 채운다
    meta = res.get("meta") or {}
    rmt, rmp = meta.get("regularMarketTime"), meta.get("regularMarketPrice")
    if rmt and rmp:
        mt = datetime.fromtimestamp(rmt, tz)
        if mt.date() not in out and mt.hour * 60 + mt.minute >= 16 * 60:
            out[mt.date()] = float(rmp)
    today = now_ny.date()
    after_close = now_ny.hour * 60 + now_ny.minute >= 16 * 60 + 10
    return sorted((d, c) for d, c in out.items() if d < today or (d == today and after_close))


def fetch_btc() -> tuple[date, float]:
    """BTC-USD 완료 일봉(UTC 날짜) 마지막 종가. 오늘(UTC) 진행 봉은 뺀다."""
    res = _yahoo("BTC-USD")
    today = datetime.now(timezone.utc).date()
    rows = sorted((datetime.fromtimestamp(t, timezone.utc).date(), float(c))
                  for t, c in zip(res.get("timestamp") or [], res["indicators"]["quote"][0].get("close") or [])
                  if c is not None and datetime.fromtimestamp(t, timezone.utc).date() < today)
    if not rows:
        raise RuntimeError("BTC 완료 일봉 없음")
    return rows[-1]


INTRO = ("이게 뭐 알림: COIN 분할 계획 단계 점검(11/4 재설계). 135달러 경계, 130달러 하향 확인, 2차-A 저가 매수 규칙(125달러 뒤 130달러 회복), "
         "2차-B 추세 매수 규칙(178달러, 50일선, BTC 9만), 220달러 상단, 4분기 실적 거래 매출 판정이 새로 켜질 때만 옴. 자동 주문 아님.")
TRIM = "종가 220달러 이상인데 거래 매출과 서비스 매출이 함께 늘지 않으면 줄일 기준부터 정하기. 하향 확인이 켜져 있으면 신규 매수는 멈추고 보유분만 점검."
AVOID = "하향 확인 중 물타기, 장중 급등 추격, 비트코인 가격만 보고 매수, 이 알림만 보고 주문, 레버리지."


def body(stamp: str, status: str, action: str, later: str, trim: str = TRIM, avoid: str = AVOID) -> str:
    return "\n".join([f"{stamp} | COIN | Coinbase Global", INTRO, f"지금 결과/상태: {status}", f"지금 할 일: {action}",
                      f"나중에: {later}", f"익절/줄일 때: {trim}", f"하지 말 것: {avoid}"])


def pct(a: float, b: float) -> float:
    return (a / b - 1) * 100


def sma(closes: list[float], n: int) -> float | None:
    return sum(closes[-n:]) / n if len(closes) >= n else None


# ---------- 가격 규칙 ----------

def evaluate(cfg: dict, state: dict, rows: list[tuple[date, float]], btc: tuple[date, float] | None, btc_err: str,
             stamp: str) -> list[tuple[str, str, int, list[str]]]:
    """완료 일봉 rows(오름차순)와 state로 가격 규칙을 판정하고 state를 갱신한다. 알림 목록을 돌려준다."""
    r = cfg["rules"]
    w, dn, a, b, top = r["warn"], r["down"], r["buy_a"], r["buy_b"], r["top"]
    closes = [c for _, c in rows]
    last_d, close = rows[-1]
    prev = closes[-2] if len(closes) >= 2 else None
    ma = sma(closes, int(b["sma_days"]))
    lo52 = min(closes) if len(closes) >= 150 else None
    status = f"COIN 종가 ${close:,.2f}({last_d:%m/%d} 미국)"
    if prev:
        status += f", 전일 대비 {pct(close, prev):+.2f}%"
    if lo52:
        status += f", 종가 기준 52주 저점 ${lo52:,.2f}"
    if ma:
        status += f", {b['sma_days']}일선 ${ma:,.2f}"
    status += f", BTC ${btc[1]:,.0f}({btc[0]:%m/%d} UTC)" if btc else f", BTC 조회 실패({btc_err})"
    status += "."
    out: list[tuple[str, str, int, list[str]]] = []

    n_dn = int(dn["consec_days"])
    last_n = closes[-n_dn:]
    down_now = len(last_n) == n_dn and all(c < dn["close_below"] for c in last_n)
    reclaim = len(last_n) == n_dn and all(c >= dn["clear_close"] for c in last_n)
    lb = int(a["lookback_days"])
    window = closes[-(lb + n_dn):-n_dn] if len(closes) > n_dn else []
    touched = any(c <= a["touch_close"] for c in window)

    # 1. 경계
    if close <= w["close_max"] and state["warn_armed"]:
        out.append(("[COIN] 135달러 경계 | 매수 신호 아님",
                    body(stamp, status + f" 종가가 {w['close_max']:.0f}달러 이하, 52주 저점(135.46) 부근을 깼습니다.",
                         "경계만 하세요. 이것은 매수 신호가 아닙니다. 다음 단계는 130달러 아래 2거래일(하향 확인)입니다.",
                         f"종가가 {w['rearm_close']:.0f}달러 위로 올라가면 다시 무장됩니다."), 4, ["warning"]))
        state["warn_armed"] = False
    elif not state["warn_armed"] and close > w["rearm_close"]:
        state["warn_armed"] = True

    # 2. 하향 확인 (2차-A보다 먼저 판정)
    if down_now and not state["down_active"]:
        out.append(("[COIN] 하향 확인 | 저가 매수 보류",
                    body(stamp, status + f" 종가가 {n_dn}거래일 연속 {dn['close_below']:.0f}달러 아래입니다. 하향 신호입니다.",
                         "저가 매수는 보류. 신규 매수를 멈추고 보유분과 실적 전망(거래 매출, 가이던스)을 점검하세요.",
                         f"종가 {dn['clear_close']:.0f}달러 이상이 {n_dn}거래일 연속이면 해제됩니다. 그 전에는 2차-A(125달러) 저가 매수 규칙이 보류 상태입니다."),
                    4, ["chart_with_downwards_trend"]))
        state["down_active"] = True

    # 3. 2차-A 저가 매수 규칙
    if close <= a["touch_close"] and state["a_armed"] and not state["a_hold_sent"]:
        why = "하향 확인이 켜져 있어" if state["down_active"] else "아직 바닥 확인 전이라"
        out.append(("[COIN] 2차-A 대기 | 125달러 도달, 저가 매수 보류",
                    body(stamp, status + f" 종가가 {a['touch_close']:.0f}달러 이하입니다. {why} 2차-A 저가 매수 규칙은 보류 상태입니다.",
                         "지금은 사지 마세요. 하향 조건이 유효한 동안 저가 매수는 하지 않습니다.",
                         f"최근 {lb}거래일 안에 125달러 이하를 찍은 뒤 종가 {dn['clear_close']:.0f}달러 이상이 {n_dn}거래일 연속이면 "
                         "하향 확인이 풀리고 2차-A 조건 충족 알림이 따로 옵니다."), 3, ["hourglass"]))
        state["a_hold_sent"] = True
    if reclaim:
        if state["a_armed"] and touched:
            out.append(("[COIN] 2차-A 조건 충족 | 저가 매수 규칙 검토",
                        body(stamp, status + f" 최근 {lb}거래일 안에 {a['touch_close']:.0f}달러 이하를 찍은 뒤 종가 {dn['clear_close']:.0f}달러 이상이 "
                             f"{n_dn}거래일 연속입니다. 하향 확인은 해제됐습니다.",
                             "계획의 2차-A 단계 조건이 맞습니다. 실적 전망이 그대로인지 확인한 뒤 미리 정한 계획 안에서만 지정가로 검토하세요.",
                             f"종가가 {a['rearm_close']:.0f}달러 위로 올라가면 다시 무장됩니다. 다시 {dn['close_below']:.0f}달러 아래 {n_dn}거래일이면 하향 확인이 다시 옵니다."),
                        4, ["chart_with_upwards_trend"]))
            state["a_armed"] = False
        if state["down_active"]:
            print("하향 확인 해제")
        state["down_active"] = False
    if close > a["rearm_close"]:  # 140 위로 올라가면 새 국면: 2차-A 재무장, 보류 안내도 다시 1회 가능
        state["a_armed"] = True
        state["a_hold_sent"] = False

    # 4. 2차-B 추세 매수 규칙
    b_ok = bool(close >= b["close_min"] and ma and close >= ma and btc and btc[1] >= b["btc_min"])
    if b_ok and state["b_armed"]:
        out.append(("[COIN] 2차-B 조건 충족 | 추세 매수 규칙 검토",
                    body(stamp, status + f" 종가 {b['close_min']:.0f}달러 이상, {b['sma_days']}일선 이상, BTC {b['btc_min']:,.0f}달러 이상이 함께 맞았습니다.",
                         "계획의 2차-B 단계 조건이 맞습니다. 장중 급등 추격 말고 미국 정규장 지정가로, 미리 정한 계획 안에서만 검토하세요.",
                         f"종가가 {b['rearm_close']:.0f}달러 아래로 내려가면 다시 무장됩니다. 2차-A와는 따로 판정합니다."),
                    4, ["rocket"]))
        state["b_armed"] = False
    elif not state["b_armed"] and close < b["rearm_close"]:
        state["b_armed"] = True

    # 5. 상단
    if close >= top["close_min"] and state["top_armed"]:
        out.append(("[COIN] 220달러 도달 | 추격 말고 점검",
                    body(stamp, status + f" 종가가 {top['close_min']:.0f}달러(기준 시나리오 상단)에 닿았습니다.",
                         "추격 매수 대신 거래 매출, 서비스 매출, 조정 EBITDA가 같이 회복됐는지 확인. 줄일 기준부터 정하기.",
                         f"종가가 {top['rearm_close']:.0f}달러 아래로 내려가면 다시 무장됩니다."), 4, ["trophy"]))
        state["top_armed"] = False
    elif not state["top_armed"] and close < top["rearm_close"]:
        state["top_armed"] = True

    state.update(last_bar_date=last_d.isoformat(), last_close=close, fetch_fail_date=None)
    state["_status"] = status
    return out


# ---------- 4분기 실적 ----------

def edgar_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=SEC_UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def html_to_text(raw: str) -> str:
    t = re.sub(r"<script.*?</script>|<style.*?</style>", "", raw, flags=re.S)
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    return re.sub(r"\s+", " ", t)


def annual_tx_k(text: str) -> float:
    """10-K 본문의 'Total transaction revenue $ 4,055,390 $ 3,986,113 ...' 첫 숫자(천 달러, 당해 연도)."""
    m = re.search(r"Total transaction revenue \$? ?([\d,]{5,})", text)
    if not m:
        raise ValueError("10-K에 Total transaction revenue 행 없음")
    return float(m.group(1).replace(",", ""))


def exhibit_tx(text: str) -> tuple[float | None, int | None]:
    """8-K 첨부(덱 또는 주주서한) 문장에서 (거래 매출 백만 달러, Q/Q 방향 +1/-1/0)."""
    m = re.search(r"Transaction revenue (?:for Q4 )?(?:was |of )?\$([\d,.]+) ?(M\b|million|B\b|billion)", text)
    val = None
    if m:
        val = float(m.group(1).replace(",", "")) * (1000 if m.group(2)[0] == "B" else 1)
    d = re.search(r"Transaction revenue[^.•]{0,80}?\b(up|down|increased|decreased|declined|decline|grew|growth|rose|fell|flat)\b[^.•]{0,30}Q/Q", text)
    sign = None
    if d:
        sign = {"up": 1, "increased": 1, "grew": 1, "growth": 1, "rose": 1, "flat": 0}.get(d.group(1), -1)
    return val, sign


def judge_q4(ann_k: float | None, ex_val: float | None, ex_sign: int | None, ref: dict) -> dict:
    """거래 매출 4분기 vs 3분기. {'ok': bool, 'q4': 백만, 'how': str, 'why': str}. ok False면 수동 확인."""
    q3 = ref["q3_tx_k"] / 1000
    if ann_k is not None:
        q4 = (ann_k - ref["nine_month_tx_k"]) / 1000
        if not (0.3 * q3 <= q4 <= 3 * q3):
            return {"ok": False, "q4": q4, "how": "10-K", "why": f"10-K 연간에서 뺀 4분기 {q4:,.1f}백만이 비정상 범위"}
        if ex_val is not None and abs(ex_val - q4) > max(5.0, 0.02 * q4):
            return {"ok": False, "q4": q4, "how": "10-K", "why": f"10-K 계산 {q4:,.1f}와 첨부 문장 {ex_val:,.0f}이 다름"}
        return {"ok": True, "q4": q4, "how": "10-K 연간 - 9개월 누계", "why": ""}
    if ex_val is None:
        return {"ok": False, "q4": None, "how": "", "why": "10-K 없음, 첨부에서 거래 매출 문장 못 찾음"}
    if abs(ex_val - q3) <= 1.0:
        return {"ok": False, "q4": ex_val, "how": "8-K 첨부", "why": "반올림 숫자가 3분기와 1백만 달러 안이라 방향 판정 불가"}
    if ex_sign is not None and ex_sign != (1 if ex_val > q3 else -1):
        return {"ok": False, "q4": ex_val, "how": "8-K 첨부", "why": "숫자와 Q/Q 문장 방향이 어긋남"}
    return {"ok": True, "q4": ex_val, "how": "8-K 첨부 문장(반올림)", "why": ""}


def find_q4_filings(cfg: dict, today: date) -> tuple[str | None, str | None, str]:
    """(10-K 접수번호, 8-K 접수번호, 8-K 제출일). 창(search_from~) 안의 첫 공시."""
    e, ed = cfg["earnings"], cfg["edgar"]
    sub = json.loads(edgar_get(f"https://data.sec.gov/submissions/CIK{ed['cik']}.json"))["filings"]["recent"]
    lo = date.fromisoformat(e["search_from"])
    rows = list(zip(sub["form"], sub["filingDate"], sub["accessionNumber"], sub["items"], sub["reportDate"]))
    tenk = [(d, a) for f, d, a, it, rd in rows if f == "10-K" and rd == e["period_end"]]
    eightk = [(d, a) for f, d, a, it, rd in rows if f == "8-K" and ed["item"] in it.split(",") and date.fromisoformat(d) >= lo]
    k = min(tenk)[1] if tenk else None
    ek = min(eightk) if eightk else (None, None)
    return k, ek[1], ek[0] or ""


def read_q4(cfg: dict, today: date) -> tuple[dict | None, str]:
    """공시가 아직 없으면 (None, 사유). 있으면 (판정, 출처)."""
    cik = int(cfg["edgar"]["cik"])
    k, ek, ed = find_q4_filings(cfg, today)
    if not k and not ek:
        return None, "4분기 실적 공시 아직 없음"
    ann = None
    src = []
    if k:
        items = json.loads(edgar_get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{k.replace('-', '')}/index.json"))["directory"]["item"]
        main = max((i for i in items if i["name"].endswith(".htm") and not re.match(r"R\d+\.htm$", i["name"]) and "index" not in i["name"]),
                   key=lambda i: int(i.get("size") or 0))
        try:
            ann = annual_tx_k(html_to_text(edgar_get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{k.replace('-', '')}/{main['name']}").decode("utf-8", "replace")))
            src.append(f"10-K {k}")
        except ValueError as exc:
            src.append(f"10-K {k} 판독 실패({exc})")
    ex_val = ex_sign = None
    if ek:
        items = json.loads(edgar_get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{ek.replace('-', '')}/index.json"))["directory"]["item"]
        for it in sorted((i for i in items if i["name"].endswith(".htm") and not re.match(r"R\d+\.htm$", i["name"]) and "index" not in i["name"]),
                         key=lambda i: -int(i.get("size") or 0))[:4]:
            v, s = exhibit_tx(html_to_text(edgar_get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{ek.replace('-', '')}/{it['name']}").decode("utf-8", "replace")))
            if v is not None or s is not None:
                ex_val, ex_sign = v, s
                src.append(f"8-K {ek}({ed}) {it['name']}")
                break
        else:
            src.append(f"8-K {ek}({ed}) 첨부에서 거래 매출 문장 없음")
    return judge_q4(ann, ex_val, ex_sign, cfg["earnings"]["reference"]), "; ".join(src)


def earnings_msgs(j: dict, src: str, stamp: str, status: str, ref: dict) -> tuple[str, str, int, list[str]]:
    q3 = ref["q3_tx_k"] / 1000
    if j["ok"]:
        up = j["q4"] > q3
        title = "[COIN] 4분기 거래 매출 증가 | 3차 조건 충족" if up else "[COIN] 4분기 거래 매출 감소 | 3차 보류"
        act = ("계획의 3차 단계 조건(거래 매출 전 분기 대비 증가)이 맞습니다. 가이던스와 구독 및 서비스 매출도 같이 확인한 뒤 미리 정한 계획 안에서만 검토하세요."
               if up else "3차 단계는 보류. 다음 분기 실적까지 거래 매출 회복을 기다리세요.")
        st = (f"4분기 거래 매출 ${j['q4']:,.1f}백만, 3분기 ${q3:,.1f}백만 대비 {pct(j['q4'], q3):+.1f}%({j['how']}). 출처 SEC {src}. {status}")
        return title, body(stamp, st, act, "가격 조건 알림은 계속 옵니다. 거래 매출 외 항목은 직접 확인하세요."), 4, ["bar_chart"]
    st = f"4분기 실적 공시는 나왔지만 자동 판정은 하지 않았습니다({j['why']}). 출처 SEC {src}. {status}"
    act = (f"회사 IR({ref['ir_url']}) 주주서한이나 SEC 10-K에서 4분기 거래 매출을 직접 보고 3분기 ${q3:,.1f}백만 달러"
           "(10-Q, Total transaction revenue)보다 늘었는지 확인하세요. 늘었으면 3차 조건 충족, 아니면 보류.")
    return "[COIN] 4분기 실적 나옴 | 거래 매출 직접 확인", body(stamp, st, act, "다음 실행 때 자동 판독을 다시 시도합니다."), 4, ["bar_chart"]


def earnings_step(cfg: dict, state: dict, today: date, stamp: str, status: str, reader=None) -> list[tuple[str, str, int, list[str]]]:
    e = cfg["earnings"]
    if state["earn_parsed"] or not (date.fromisoformat(e["search_from"]) <= today <= date.fromisoformat(e["search_to"])):
        return []
    try:
        j, src = (reader or read_q4)(cfg, today)
    except Exception as exc:  # noqa: BLE001
        print(f"earnings read error: {type(exc).__name__}: {exc}")
        return []
    if j is None:
        print(src)
        return []
    if j["ok"]:
        state["earn_parsed"] = True
        return [earnings_msgs(j, src, stamp, status, e["reference"])]
    if not state["earn_manual_sent"]:
        state["earn_manual_sent"] = True
        return [earnings_msgs(j, src, stamp, status, e["reference"])]
    return []


# ---------- 실행 ----------

def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode == "selftest":
        return selftest()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    today_kst = now.date()
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))

    if mode == "earnings-dry":
        if len(sys.argv) > 4:  # 지난 10-K로 시험: 접수번호, 9개월 누계(천), 직전분기(천)
            ref = dict(cfg["earnings"]["reference"], nine_month_tx_k=float(sys.argv[3]), q3_tx_k=float(sys.argv[4]))
            k = sys.argv[2]
            items = json.loads(edgar_get(f"https://www.sec.gov/Archives/edgar/data/{int(cfg['edgar']['cik'])}/{k.replace('-', '')}/index.json"))["directory"]["item"]
            main_doc = max((i for i in items if i["name"].endswith(".htm") and not re.match(r"R\d+\.htm$", i["name"]) and "index" not in i["name"]),
                           key=lambda i: int(i.get("size") or 0))
            ann = annual_tx_k(html_to_text(edgar_get(f"https://www.sec.gov/Archives/edgar/data/{int(cfg['edgar']['cik'])}/{k.replace('-', '')}/{main_doc['name']}").decode("utf-8", "replace")))
            j, src = judge_q4(ann, None, None, ref), f"10-K {k}"
            cfg = dict(cfg, earnings=dict(cfg["earnings"], reference=ref))
        else:
            j, src = read_q4(cfg, today_kst)
        print(j, src)
        if j:
            t, m, _, _ = earnings_msgs(j, src, stamp, "(dry)", cfg["earnings"]["reference"])
            print(t)
            print(m)
        print("DRY RUN: no publish, state not written")
        return 0

    end = cfg.get("ends_kst")
    if mode != "install" and end and now > datetime.strptime(end, "%Y-%m-%d %H:%M").replace(tzinfo=KST):
        print(f"ended at {end}; silent")
        return 0

    rows: list[tuple[date, float]] = []
    err = ""
    try:
        rows = fetch_bars(cfg["symbol"], ny_now())
        if len(rows) < 3:
            raise RuntimeError(f"COIN 완료 일봉 {len(rows)}개")
        if (ny_now().date() - rows[-1][0]).days > int(cfg["stale_days"]):
            raise RuntimeError(f"COIN 마지막 봉 {rows[-1][0]} 너무 오래됨")
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
        rows = []
    btc, btc_err = None, ""
    try:
        btc = fetch_btc()
    except Exception as exc:  # noqa: BLE001
        btc_err = type(exc).__name__

    if mode == "install":
        st = dict(STATE_KEYS)
        if rows:
            evaluate(cfg, st, rows, btc, btc_err, stamp)
        publish(topic, "[COIN] 설치 확인 | 매매 신호 아님",
                body(stamp, f"평일 09:00 KST morning 슬롯. {st.get('_status', 'COIN 조회 실패')}", "설치 확인만 하세요.",
                     "가격 조건은 새로 켜질 때만 옵니다. 조회 실패는 cwally-ops로 갑니다."), 3, ["information_source"], server)
        return 0

    dry = mode == "dry"
    state = load_state()
    alerts: list[tuple[str, str, int, list[str]]] = []
    if rows:
        alerts += evaluate(cfg, state, rows, btc, btc_err, stamp)
        status = state.pop("_status")
    else:
        status = f"COIN 조회 실패({err})"
        if state.get("fetch_fail_date") != today_kst.strftime("%Y%m%d"):
            alerts.append(("[COIN] 조회 실패 | 조건 미확인",
                           body(stamp, status, "증권사 앱에서 COIN 종가를 직접 보세요.", "다음 평일 09:00에 다시 봅니다.",
                                "이번 알림으로 계획을 바꾸지 마세요.", "조회 실패를 조건 충족이나 정상으로 읽기."), 3, ["warning"]))
            state["fetch_fail_date"] = today_kst.strftime("%Y%m%d")
    alerts += earnings_step(cfg, state, today_kst, stamp, status)
    print(f"{status} state={ {k: state[k] for k in ('warn_armed', 'down_active', 'a_armed', 'a_hold_sent', 'b_armed', 'top_armed')} }")

    for title, msg, pri, tags in alerts:
        print(title)
        print(msg)
        if not dry:
            publish(topic, title, msg, pri, tags, server)
    if not alerts:
        print("no alert")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    state["updated_at"] = now.isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


# ---------- 오프라인 selftest ----------

def selftest() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    fails = []

    def check(name, got, want):
        ok = got == want
        print(("ok   " if ok else "FAIL ") + f"{name}: got={got!r} want={want!r}")
        if not ok:
            fails.append(name)

    hist: dict = {}

    def run(path: list[float], btc: float | None = 80000.0, base: float = 150.0, n_base: int = 60, state=None):
        """base 종가 n_base일 뒤 path를 하루씩 넣으며 알림 제목을 날짜별로 모은다. state를 주면 그 실행의 종가 이력을 이어 씀."""
        st = dict(STATE_KEYS) if state is None else state
        closes = hist[id(st)] if state is not None else [base] * n_base
        hist[id(st)] = closes
        titles = []
        d0 = date(2026, 8, 1)
        for c in path:
            closes.append(c)
            rows = [(d0 + timedelta(days=i), x) for i, x in enumerate(closes)]
            out = evaluate(cfg, st, rows, (date(2026, 11, 3), btc) if btc else None, "stub", "S")
            st.pop("_status", None)
            titles.append([t for t, _, _, _ in out])
        return titles, st

    T = lambda s: "[COIN] " + s  # noqa: E731
    r = cfg["rules"]
    check("config thresholds", (r["warn"]["close_max"], r["down"]["close_below"], r["down"]["consec_days"], r["buy_a"]["touch_close"],
                                r["buy_b"]["close_min"], r["buy_b"]["sma_days"], r["buy_b"]["btc_min"], r["top"]["close_min"], cfg["ends_kst"]),
          (135.0, 130.0, 2, 125.0, 178.0, 50, 90000.0, 220.0, "2027-03-31 23:59"))

    t, st = run([145.45, 140.0])
    check("145 then 140: silent", t, [[], []])
    t, st = run([136.0, 135.0, 134.0, 141.0, 143.0, 134.5])
    check("rule1 <=135 once, re-arm >142, fires again", t,
          [[], [T("135달러 경계 | 매수 신호 아님")], [], [], [], [T("135달러 경계 | 매수 신호 아님")]])
    t, st = run([134.0, 129.0, 131.0, 129.5, 128.0, 127.0])
    check("rule2 needs 2 consecutive <130 (129,131 no; 129.5,128 yes), once", t,
          [[T("135달러 경계 | 매수 신호 아님")], [], [], [], [T("하향 확인 | 저가 매수 보류")], []])
    check("rule2 active in state", st["down_active"], True)

    # contradiction case: 하향 확인 중 125 이하 -> 보류 문구만, 매수 문구 없음
    t, st = run([134.0, 129.0, 128.0, 124.0, 122.0, 126.0, 129.0])
    flat = [x for day in t for x in day]
    check("contradiction: rule2 then <=125 gives hold, never a plain buy",
          (t[2], t[3], any("2차-A 조건 충족" in x for x in flat)),
          ([T("하향 확인 | 저가 매수 보류")], [T("2차-A 대기 | 125달러 도달, 저가 매수 보류")], False))
    hold_msg = evaluate(cfg, dict(STATE_KEYS, down_active=True),
                        [(date(2026, 8, 1) + timedelta(days=i), c) for i, c in enumerate([150.0] * 60 + [128.0, 124.0])], None, "stub", "S")
    hm = [m for tt, m, _, _ in hold_msg if "2차-A 대기" in tt][0]
    check("hold message says downgrade active and rule on hold", ("하향 확인이 켜져 있어" in hm, "보류" in hm, "사지 마세요" in hm), (True, True, True))
    t2, st2 = run([131.0, 132.0], state=st)
    check("rule3 2차-A fires only after 2 closes >=130 following the <=125 touch; clears rule2", (t2, st2["down_active"]),
          ([[], [T("2차-A 조건 충족 | 저가 매수 규칙 검토")]], False))
    t3, st3 = run([131.0, 133.0, 135.0], state=st2)
    check("rule3 once (no repeat while not re-armed)", t3, [[], [], []])
    t4, st4 = run([141.0, 124.0, 131.0, 132.0], state=st3)
    check("rule3 re-arms >140, new touch: hold again (no rule2 since one day <130), then fires",
          t4, [[], [T("2차-A 대기 | 125달러 도달, 저가 매수 보류")], [], [T("2차-A 조건 충족 | 저가 매수 규칙 검토")]])
    t5, _ = run([135.0] * 3 + [131.0, 132.0])
    check("rule3 no touch <=125: reclaim 130 alone never fires 2차-A", any("2차-A" in x for d in t5 for x in d), False)
    t6, _ = run([124.0] + [126.0] * 16 + [131.0, 132.0])
    check("rule3 touch older than lookback(15): no 2차-A", any("2차-A 조건 충족" in x for d in t6 for x in d), False)

    # 2차-B
    t, st = run([170.0, 180.0], btc=91000.0, base=160.0)
    check("rule4 178 + 50DMA + BTC 9만: fires", t, [[], [T("2차-B 조건 충족 | 추세 매수 규칙 검토")]])
    t, _ = run([180.0], btc=89999.0, base=160.0)
    check("rule4 BTC below 90k: silent", t, [[]])
    t, _ = run([180.0], btc=None, base=160.0)
    check("rule4 BTC fetch failed: silent", t, [[]])
    t, _ = run([179.0], btc=95000.0, base=185.0)
    check("rule4 close below 50DMA: silent", t, [[]])
    t, st = run([180.0, 182.0, 169.0, 181.0], btc=95000.0, base=160.0)
    check("rule4 once, re-arm <170, fires again; separate key from 2차-A", (t, st["a_armed"]),
          ([[T("2차-B 조건 충족 | 추세 매수 규칙 검토")], [], [], [T("2차-B 조건 충족 | 추세 매수 규칙 검토")]], True))
    t, st = run([221.0, 215.0, 204.0, 222.0], btc=95000.0, base=200.0)
    check("rule5 220 once, re-arm <205", [[y for y in x if "220" in y] for x in t if any("220" in y for y in x)],
          [[T("220달러 도달 | 추격 말고 점검")], [T("220달러 도달 | 추격 말고 점검")]])

    # no quantity / weight words, no middle dot / em-dash
    all_msgs = []
    for path, btc in (([134.0, 129.0, 128.0, 124.0, 131.0, 132.0], 80000.0), ([180.0, 221.0], 95000.0)):
        st = dict(STATE_KEYS)
        closes = [150.0] * 60
        for c in path:
            closes.append(c)
            all_msgs += evaluate(cfg, st, [(date(2026, 8, 1) + timedelta(days=i), x) for i, x in enumerate(closes)], (date(2026, 11, 3), btc), "", "S")
    text = " ".join(t + m for t, m, _, _ in all_msgs)
    check("messages: no 비중/수량/주/% of holdings, no middle dot or em-dash",
          (bool(re.search(r"비중|수량|\d+주\b|전액|한도", text.replace("52주", ""))), "\u00b7" in text, "\u2014" in text, all(t.startswith("[COIN]") for t, _, _, _ in all_msgs)),
          (False, False, False, True))

    # 4분기 실적 판정
    ref = cfg["earnings"]["reference"]
    check("ref Q3 tx and 9M from 10-Q", (ref["q3_tx_k"], ref["nine_month_tx_k"]), (590224.0, 1945205.0))
    tenk = "Revenue Transaction revenue Consumer, net 3,100,000 Total transaction revenue $ 2,700,000 $ 4,055,390 $ (1,355,390) (33) %"
    check("annual_tx_k reads first number", annual_tx_k(tenk), 2700000.0)
    check("annual_tx_k real FY2025 row", annual_tx_k("Total transaction revenue $ 4,055,390 $ 3,986,113 $ 69,277 2 %"), 4055390.0)
    j = judge_q4(4055390.0, None, None, dict(ref, nine_month_tx_k=3072738.0, q3_tx_k=1046260.0))
    check("FY2025 10-K minus 9M2025 = Q4 2025 982.7 (letter says $983M, down 6% Q/Q)", (j["ok"], round(j["q4"], 1), j["q4"] < 1046.26), (True, 982.7, True))
    check("exhibit: shareholder letter phrasing", exhibit_tx("Transaction revenue for Q4 was $983 million , down 6% Q/Q as"), (983.0, -1))
    check("exhibit: deck phrasing", exhibit_tx("Transaction revenue $590M Adjusted EBITDA1 $333M ... Transaction revenue declined 1% Q/Q as"), (590.0, -1))
    check("exhibit: Q1 deck phrasing", exhibit_tx("Transaction revenue $756M x Transaction revenue decline of 23% Q/Q"), (756.0, -1))
    check("exhibit: up phrasing", exhibit_tx("Transaction revenue was $650 million, up 10% Q/Q"), (650.0, 1))
    check("judge exhibit only: 650 > 590.2 -> ok up", (judge_q4(None, 650.0, 1, ref)["ok"]), True)
    check("judge exhibit only: 590 rounded within 1M of 590.2 -> manual", judge_q4(None, 590.0, -1, ref)["ok"], False)
    check("judge exhibit only: number vs Q/Q sentence disagree -> manual", judge_q4(None, 650.0, -1, ref)["ok"], False)
    check("judge 10-K vs exhibit mismatch -> manual", judge_q4(2600000.0, 900.0, 1, ref)["ok"], False)
    check("judge nothing -> manual", judge_q4(None, None, None, ref)["ok"], False)

    def estep(reader, state, day="2027-02-12"):
        return [t for t, _, _, _ in earnings_step(cfg, state, date.fromisoformat(day), "S", "st", reader=reader)]

    st = dict(STATE_KEYS)
    check("earnings before window: silent", estep(lambda c, d: (_ for _ in ()).throw(AssertionError("called")), st, "2027-01-10"), [])
    check("earnings no filing yet: silent", estep(lambda c, d: (None, "없음"), st), [])
    bad = judge_q4(None, None, None, ref)
    check("earnings manual fallback once", (estep(lambda c, d: (bad, "8-K x"), st), estep(lambda c, d: (bad, "8-K x"), st)),
          (["[COIN] 4분기 실적 나옴 | 거래 매출 직접 확인"], []))
    good = judge_q4(590224.0 + 1945205.0 + 30000.0, None, None, ref)
    check("earnings retry after manual, then verdict once", (estep(lambda c, d: (good, "10-K y"), st), estep(lambda c, d: (good, "10-K y"), st), st["earn_parsed"]),
          (["[COIN] 4분기 거래 매출 증가 | 3차 조건 충족"], [], True))
    down = judge_q4(590224.0 + 1945205.0 - 40000.0, None, None, ref)
    check("earnings down verdict title", estep(lambda c, d: (down, "10-K z"), dict(STATE_KEYS)), ["[COIN] 4분기 거래 매출 감소 | 3차 보류"])
    check("earnings read error: silent, retry later", estep(lambda c, d: (_ for _ in ()).throw(OSError("net")), dict(STATE_KEYS)), [])
    mm = earnings_msgs(bad, "8-K x", "S", "st", ref)
    check("manual title not routed to ops (port)", bool(re.search(r"조회\s?실패|파싱\s?실패|확인 불가|해석\s?불가", mm[0])), False)
    check("old state keys dropped on load", sorted(k for k in json.loads('{"buy_armed": false, "top_armed": true}') if k in STATE_KEYS), ["top_armed"])

    print(f"SELFTEST {'PASS' if not fails else 'FAIL'}: {len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
