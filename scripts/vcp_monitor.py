#!/usr/bin/env python3
"""[VCP] KOSPI 주간 VCP 후보 (B2R): 관찰 목록, 매매 신호 아님.

Sunday 21:00 KST on the existing portlight slot (no new cron).
B2R = Minervini 8 (RS >= 70) + relaxed O'Neil base (depth 5~40%, close >= base midpoint,
>= 10 bars since the 65-bar peak, peak bar = 0) + regime: KS11 close > its 200-day SMA.
Logic is copied line for line from the backtest code (vcp-screener krx_hybrid_vcp_screener.py
evaluate_stock/oneil_base/weighted_return/vcp_score and backtest3y/bt3y.py comp/label) and was
parity-checked against it on the same price data before install (2026-11-03).

Data:
  universe  top 200 per market (KOSPI, KOSDAQ incl. GLOBAL) by Marcap, fdr.StockListing('KRX'),
            fallback fdr's GitHub listing snapshot (KRX endpoint often answers 500)
  stocks    Daum days via scripts/krx_close.py (regular 15:30 close, AGENTS.md rule), split
            adjusted with prevClosingPrice (same method as the backtest's marcap adjustment)
  index     KS11 close from fdr (index closes are exempt from the krx_close rule)

  python scripts/vcp_monitor.py auto      # scheduled: Sunday only, once per ISO week
  python scripts/vcp_monitor.py dry       # compute + print the would-be message; no ntfy, no state
  python scripts/vcp_monitor.py selftest  # offline checks
"""
from __future__ import annotations

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import krx_close  # noqa: E402
from ntfy_util import publish  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "vcp.json"
STATE_PATH = ROOT / "state" / "vcp.json"
KST = timezone(timedelta(hours=9))
LISTING_CACHE_URL = ("https://raw.githubusercontent.com/FinanceData/fdr_krx_data_cache/"
                     "refs/heads/master/data/listing/krx/{date}.csv")
PREF_RE = re.compile(r"우[BC]?$")
SPAC_RE = re.compile(r"스팩|제\d+호")
FORBIDDEN = ("\u00b7", "\u2014")

# B2R parameters (fixed before the 2010~2017 check; do not retune)
P = dict(top_n=200, bars=320, min_bars=260, rs_min=70, c7_mult=0.75, base_len=65,
         min_depth=5.0, max_depth=40.0, min_bars_since_peak=10,
         vol10_ratio_cap=0.70, vol5_ratio_cap=0.60, workers=8, max_fail_ratio=0.10, max_lines=25)


# ---------- shared B2R logic (same as the backtest) ----------
def weighted_return(c: pd.Series) -> float:
    c0 = c.iloc[-1]
    r = lambda n: c0 / c.iloc[-1 - n] - 1  # noqa: E731
    return 0.4 * r(63) + 0.2 * r(126) + 0.2 * r(189) + 0.2 * r(252)


def minervini7(d: pd.DataFrame) -> bool:
    """c1..c7 of the Minervini 8 (c8 = RS percentile is applied across the universe)."""
    c = d["Close"]
    close = c.iloc[-1]
    sma50, sma150, sma200 = (c.rolling(n).mean() for n in (50, 150, 200))
    s50, s150, s200 = sma50.iloc[-1], sma150.iloc[-1], sma200.iloc[-1]
    hi252 = d["High"].iloc[-252:].max()
    lo252 = d["Low"].iloc[-252:].min()
    conds = [close > s150 and close > s200, s150 > s200, s200 > sma200.iloc[-21],
             s50 > s150 and s50 > s200, close > s50, close >= 1.30 * lo252, close >= P["c7_mult"] * hi252]
    return all(bool(x) for x in conds)


def relaxed_base(d: pd.DataFrame) -> tuple[bool, float, int, float]:
    win = d.iloc[-P["base_len"]:]
    pk_i = int(np.argmax(win["High"].values))
    peak = win["High"].iloc[pk_i]
    low_after = win["Low"].iloc[pk_i:].min()
    depth = (peak - low_after) / peak * 100
    mid = (peak + low_after) / 2
    bsp = len(win) - 1 - pk_i
    ok = P["min_depth"] <= depth <= P["max_depth"] and d["Close"].iloc[-1] >= mid and bsp >= P["min_bars_since_peak"]
    return bool(ok), float(depth), int(bsp), float(mid)


def _range_pct(seg: pd.DataFrame) -> float:
    h, l = seg["High"].max(), seg["Low"].min()
    return (h - l) / h * 100 if h > 0 else np.nan


def vcp_score(d: pd.DataFrame) -> int:
    """Display only (not part of B2R). Same 0~100 score as the screener."""
    score = 0
    t1, t2, t3 = _range_pct(d.iloc[-60:-40]), _range_pct(d.iloc[-40:-20]), _range_pct(d.iloc[-20:])
    score += 20 if t1 > t2 else 0
    score += 20 if t2 > t3 else 0
    r10 = _range_pct(d.iloc[-10:])
    score += 15 if r10 <= 8 else (8 if r10 <= 12 else 0)
    pc = d["Close"].shift(1)
    tr = pd.concat([d["High"] - d["Low"], (d["High"] - pc).abs(), (d["Low"] - pc).abs()], axis=1).max(axis=1)
    ap = tr.ewm(alpha=1 / 14, adjust=False).mean() / d["Close"] * 100
    score += 15 if ap.iloc[-1] < ap.iloc[-31] else 0
    v = d["Volume"]
    v50 = v.iloc[-50:].mean()
    if v50 > 0:
        score += 15 if v.iloc[-10:].mean() / v50 <= P["vol10_ratio_cap"] else 0
        score += 15 if v.iloc[-5:].mean() / v50 <= P["vol5_ratio_cap"] else 0
    return int(score)


def screen(data: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """data: code -> adjusted OHLCV up to the as-of date (oldest first). Returns one row per evaluable stock."""
    rows = []
    for code, d in data.items():
        if d is None or len(d) < P["min_bars"]:
            continue
        ok_b, depth, bsp, mid = relaxed_base(d)
        rows.append(dict(Code=code, WR=weighted_return(d["Close"]), M7=minervini7(d), Base=ok_b,
                         Depth=depth, BSP=bsp, Close=float(d["Close"].iloc[-1]), VCP=vcp_score(d)))
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["RS"] = (df.WR.rank(pct=True) * 99).round(1)
    df["B2"] = df.M7 & (df.RS >= P["rs_min"]) & df.Base
    return df


# ---------- data ----------
def adjust_daum(payload: dict, code: str) -> pd.DataFrame:
    """Daum days rows -> split-adjusted OHLCV (oldest first). Return = tradePrice / prevClosingPrice - 1."""
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("Daum 일별 형식 아님")
    rec = {}
    for r in rows:
        sym = str(r.get("symbolCode") or "")
        if sym and sym != "A" + code:
            raise RuntimeError(f"Daum 종목 불일치 {sym}")
        day = str(r.get("date") or "")[:10]
        c = r.get("tradePrice")
        if len(day) != 10 or not c:
            continue
        vol = float(r.get("accTradeVolume") or 0)
        o, h, l = (float(r.get(k) or 0) for k in ("openingPrice", "highPrice", "lowPrice"))
        if vol <= 0 or o <= 0:
            o = h = l = float(c)
        rec[day] = (o, h, l, float(c), vol, float(r.get("prevClosingPrice") or 0))
    if not rec:
        raise RuntimeError(f"{code} Daum 일별 없음")
    df = pd.DataFrame.from_dict(rec, orient="index", columns=["Open", "High", "Low", "Close", "Volume", "Prev"]).sort_index()
    df.index = pd.to_datetime(df.index)
    ret = np.where(df["Prev"] > 0, df["Close"] / df["Prev"] - 1, 0.0)
    ret[0] = 0.0
    adj = np.cumprod(1 + ret)
    adj = adj / adj[-1] * df["Close"].iloc[-1]
    f = adj / df["Close"].values
    out = pd.DataFrame({"Open": df["Open"] * f, "High": df["High"] * f, "Low": df["Low"] * f,
                        "Close": adj, "Volume": df["Volume"]}, index=df.index)
    return out


def daum_days(code: str, n: int) -> pd.DataFrame:
    url = (f"https://finance.daum.net/api/quote/A{code}/days"
           f"?symbolCode=A{code}&page=1&perPage={n}&pagination=true")
    raw = json.loads(krx_close._get(url, {"Referer": f"https://finance.daum.net/quotes/A{code}"}).decode("utf-8", "replace"))
    return adjust_daum(raw, code)


def _with_timeout(fn, *a, timeout=60, tries=3):
    last = None
    for i in range(tries):
        ex = ThreadPoolExecutor(max_workers=1)
        try:
            return ex.submit(fn, *a).result(timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2 * (i + 1))
        finally:
            ex.shutdown(wait=False)
    raise last  # type: ignore[misc]


def load_listing(now: datetime) -> tuple[pd.DataFrame, str]:
    import FinanceDataReader as fdr
    try:
        df = _with_timeout(fdr.StockListing, "KRX", timeout=60, tries=2)
        if "Marcap" in df.columns and len(df) > 1000:
            return df, "fdr.StockListing('KRX')"
    except Exception as exc:  # noqa: BLE001
        print(f"vcp: StockListing failed ({type(exc).__name__}: {str(exc)[:120]}); GitHub snapshot fallback", file=sys.stderr)
    for back in range(10):
        day = (now - timedelta(days=back)).strftime("%Y-%m-%d")
        try:
            df = _with_timeout(lambda u: pd.read_csv(u, index_col=0, dtype={"Code": str, "MarketId": str}),
                               LISTING_CACHE_URL.format(date=day), timeout=60, tries=1)
            return df.reset_index(drop=True), f"fdr_krx_data_cache {day}.csv"
        except Exception:  # noqa: BLE001
            continue
    raise RuntimeError("종목 목록 조회 실패 (StockListing, GitHub 스냅숏 모두 실패)")


def universe(df: pd.DataFrame) -> pd.DataFrame:
    if "MarketId" in df.columns:
        mk = df["MarketId"].map({"STK": "KOSPI", "KSQ": "KOSDAQ"})
    else:
        mk = df["Market"].replace({"KOSDAQ GLOBAL": "KOSDAQ"})
    df = df.assign(Market=mk)
    df = df[df["Market"].isin(["KOSPI", "KOSDAQ"])]
    df = df[~df["Name"].astype(str).str.contains(PREF_RE) & ~df["Name"].astype(str).str.contains(SPAC_RE)]
    df = df.assign(Marcap=pd.to_numeric(df["Marcap"], errors="coerce")).dropna(subset=["Marcap"])
    parts = [df[df.Market == m].sort_values("Marcap", ascending=False).head(P["top_n"]) for m in ("KOSPI", "KOSDAQ")]
    return pd.concat(parts)[["Code", "Name", "Market"]].reset_index(drop=True)


def kospi_regime(now: datetime) -> tuple[pd.Timestamp, float, float]:
    import FinanceDataReader as fdr
    ks = _with_timeout(fdr.DataReader, "KS11", (now - timedelta(days=500)).strftime("%Y-%m-%d"), timeout=60)["Close"].dropna()
    today = pd.Timestamp(now.strftime("%Y-%m-%d"))
    if (now.hour, now.minute) < krx_close.READY:
        ks = ks[ks.index < today]
    if len(ks) < 200:
        raise RuntimeError("KOSPI 지수 조회 실패 (200일 미만)")
    return ks.index[-1], float(ks.iloc[-1]), float(ks.rolling(200).mean().iloc[-1])


# ---------- message ----------
def build_message(asof: pd.Timestamp, k_close: float, k_sma: float, picks: pd.DataFrame, n_eval: int) -> tuple[str, str]:
    n = len(picks)
    title = f"[VCP] KOSPI 주간 VCP 후보 {n}종목"
    gap = (k_close / k_sma - 1) * 100
    lines = [
        "이게 뭐 알림: 시가총액 상위 KOSPI 200, KOSDAQ 200 종목 중 B2R 조건(Minervini 8 + 완화 베이스, KOSPI 200일선 위) 통과 종목입니다.",
        "관찰 목록입니다. 매매 신호가 아닙니다 (근거: 20거래일 보유 백테스트, 같은 날 전체 평균 대비 약 +1%p).",
        f"지금 결과: {asof:%Y-%m-%d} 종가 기준, KOSPI {k_close:,.2f} / 200일선 {k_sma:,.2f} ({gap:+.1f}%), 평가 {n_eval}종목 중 {n}종목 통과 (RS 높은 순).",
    ]
    show = picks.head(P["max_lines"])
    for i, r in enumerate(show.itertuples(), 1):
        lines.append(f"{i}. {r.Name} {r.Code} {r.Market} | 종가 {r.Close:,.0f} | 깊이 {r.Depth:.1f}% | 고점 후 {r.BSP}거래일 | RS {r.RS:.0f} | VCP {r.VCP}")
    if n > len(show):
        lines.append(f"외 {n - len(show)}종목은 생략 (RS 낮은 순).")
    lines += [
        "지금 할 일: 관심 있는 종목만 차트와 뉴스를 확인하세요.",
        "나중에: 다음 주 일요일 21:00에 목록이 다시 옵니다. KOSPI가 200일선 아래면 알림이 없습니다.",
        "익절/줄일 때: 해당 없음 (관찰 목록).",
        "하지 말 것: 목록만 보고 바로 매수하지 마세요. 평균 우위가 작고 개별 종목 변동이 큽니다.",
    ]
    body = "\n".join(lines)
    return title, body


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(mode: str) -> int:
    now = datetime.now(KST)
    week = now.strftime("%G-W%V")
    state = load_state()
    if mode == "auto":
        if now.weekday() != 6:
            print("vcp silent; Sunday 21:00 KST only (not_sunday)")
            return 0
        if state.get("weekly_done_week") == week:
            print(f"vcp already done for {week}; skip")
            return 0
    asof, k_close, k_sma = kospi_regime(now)
    print(f"vcp as-of {asof:%Y-%m-%d} KOSPI {k_close:.2f} SMA200 {k_sma:.2f}")
    done = dict(weekly_done_week=week, updated_at=now.isoformat(), asof=f"{asof:%Y-%m-%d}",
                kospi=round(k_close, 2), kospi_sma200=round(k_sma, 2))
    if k_close <= k_sma:
        print("vcp: KOSPI <= 200일선, 발송 없음 (regime off)")
        if mode == "auto":
            save_state({**state, **done, "last_result": "regime_off", "last_count": 0})
        if mode != "dry":
            return 0
    raw, src = load_listing(now)
    uni = universe(raw)
    print(f"vcp universe {len(uni)} from {src}")

    def one(code):
        try:
            return code, _with_timeout(daum_days, code, P["bars"], timeout=60, tries=2)
        except Exception as exc:  # noqa: BLE001
            print(f"vcp fetch fail {code}: {type(exc).__name__}: {str(exc)[:100]}", file=sys.stderr)
            return code, None

    with ThreadPoolExecutor(max_workers=P["workers"]) as ex:
        got = dict(ex.map(one, uni.Code))
    failed = [c for c, d in got.items() if d is None]
    if len(failed) > P["max_fail_ratio"] * len(uni):
        raise RuntimeError(f"Daum 일별 조회 실패 {len(failed)}/{len(uni)}")
    data, stale = {}, 0
    for c, d in got.items():
        if d is None:
            continue
        d = d[d.index <= asof]
        if len(d) == 0 or d.index[-1] != asof:
            stale += 1
            continue
        data[c] = d
    res = screen(data)
    print(f"vcp evaluated {len(res)} (fetch fail {len(failed)}, no bar on as-of {stale})")
    picks = res[res.B2].merge(uni, on="Code").sort_values(["RS", "VCP"], ascending=False)
    title, body = build_message(asof, k_close, k_sma, picks, len(res))
    if any(ch in title + body for ch in FORBIDDEN):
        raise RuntimeError("금지 문자(가운뎃점, 엠대시) 포함")
    if mode == "dry":
        print("DRY RUN (no ntfy, no state)")
        print("regime:", "on" if k_close > k_sma else "off (auto would send nothing)")
        print("TITLE:", title)
        print(body)
        return 0
    if picks.empty:
        print("vcp: 통과 0종목, 발송 없음")
        save_state({**state, **done, "last_result": "zero_pass", "last_count": 0})
        return 0
    publish("cwally-port", title, body, priority=3, tags=["chart_with_upwards_trend"])
    save_state({**state, **done, "last_result": "sent", "last_count": int(len(picks)),
                "last_codes": picks.Code.tolist()[:50]})
    return 0


def selftest() -> int:
    ok = True

    def check(label, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + label)
        ok = ok and bool(cond)

    idx = pd.bdate_range("2025-01-01", periods=300)
    # steady uptrend, 10% pullback from a peak 40 bars ago, partial recovery
    close = np.concatenate([np.linspace(100, 200, 260), np.linspace(200, 180, 10), np.linspace(180, 195, 30)])
    d = pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99, "Close": close, "Volume": 1000.0}, index=idx)
    okb, depth, bsp, mid = relaxed_base(d)
    check(f"relaxed base pass (depth {depth:.1f}, bars since peak {bsp})", okb and 5 <= depth <= 40 and bsp >= 10)
    d2 = d.copy()
    d2.iloc[-3, d2.columns.get_loc("High")] = 260.0
    okb2, _, bsp2, _ = relaxed_base(d2)
    check(f"fresh peak 2 bars ago fails (bars since peak {bsp2})", (not okb2) and bsp2 == 2)
    check("minervini7 on uptrend", minervini7(d))
    check("weighted return positive", weighted_return(d["Close"]) > 0)
    payload = {"data": [
        {"symbolCode": "A010120", "date": "2026-04-14 00:00:00", "tradePrice": 185600, "prevClosingPrice": 179200,
         "openingPrice": 200000, "highPrice": 200000, "lowPrice": 183100, "accTradeVolume": 2147811},
        {"symbolCode": "A010120", "date": "2026-04-13 00:00:00", "tradePrice": 179200, "prevClosingPrice": 157600,
         "openingPrice": 171200, "highPrice": 188300, "lowPrice": 171200, "accTradeVolume": 2570662},
        {"symbolCode": "A010120", "date": "2026-04-10 00:00:00", "tradePrice": 788000, "prevClosingPrice": 788000,
         "openingPrice": 788000, "highPrice": 788000, "lowPrice": 788000, "accTradeVolume": 0}]}
    a = adjust_daum(payload, "010120")
    check(f"split adjust 5:1 ({a.Close.iloc[0]:.0f} -> {a.Close.iloc[1]:.0f})", abs(a.Close.iloc[0] - 157600) < 1 and abs(a.Close.iloc[-1] - 185600) < 1)
    picks = pd.DataFrame([dict(Code="005930", Name="삼성전자", Market="KOSPI", Close=228000.0, Depth=12.3, BSP=15, RS=91.0, VCP=55)])
    t, b = build_message(pd.Timestamp("2026-11-06"), 4100.0, 3500.0, picks, 380)
    check("title format", t == "[VCP] KOSPI 주간 VCP 후보 1종목")
    check("no forbidden chars", not any(ch in t + b for ch in FORBIDDEN))
    check("body order", b.index("이게 뭐 알림") < b.index("지금 결과") < b.index("지금 할 일") < b.index("나중에") < b.index("익절/줄일 때") < b.index("하지 말 것"))
    big = pd.concat([picks] * 60, ignore_index=True)
    t2, b2 = build_message(pd.Timestamp("2026-11-06"), 4100.0, 3500.0, big, 380)
    check(f"60 picks body size {len(b2.encode())} bytes < 3600", len(b2.encode()) < 3600)
    return 0 if ok else 1


if __name__ == "__main__":
    m = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if m == "selftest":
        raise SystemExit(selftest())
    if m not in {"auto", "dry"}:
        print(f"unknown mode {m}; use auto | dry | selftest", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(run(m))
