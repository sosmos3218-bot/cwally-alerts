"""시세. FinanceDataReader 가 기본이고, pykrx 는 KRX_ID 와 KRX_PW 가 있을 때만 선택입니다.

빈 시세, 오래된 시세, 부족한 시세는 오류입니다. 가격을 만들지 않습니다.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from trend_monitor.config import Config
from trend_monitor.errors import TrendDataError

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache" / "bars"


def norm_code(value) -> str:
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    if text.isdigit():
        return text.zfill(6)
    return text


def last_bar_date(df: pd.DataFrame) -> date:
    if df is None or len(df) == 0:
        raise TrendDataError("시세가 비어 있습니다. 가짜 가격으로 대체하지 않습니다.")
    ts = df.index[-1]
    if getattr(ts, "tzinfo", None) is not None:
        ts = ts.tz_convert("Asia/Seoul")
    return ts.date()


def slice_through(df: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    idx = df.index
    if getattr(idx, "tz", None) is not None:
        df = df.copy()
        df.index = idx.tz_convert("Asia/Seoul").tz_localize(None)
    mask = (df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))
    return df.loc[mask]


def normalize_ohlcv(df: pd.DataFrame, code: str) -> pd.DataFrame:
    if df is None or len(df) == 0:
        raise TrendDataError(f"{code} 시세가 비어 있습니다. 가짜 가격으로 대체하지 않습니다.")
    out = df.copy()
    if not isinstance(out.index, pd.DatetimeIndex):
        if "Date" in out.columns:
            out = out.set_index("Date")
        elif "날짜" in out.columns:
            out = out.set_index("날짜")
        else:
            raise TrendDataError(f"{code} 시세에 날짜가 없습니다. 컬럼: {list(out.columns)}")
    idx = pd.to_datetime(out.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert("Asia/Seoul").tz_localize(None)
    out.index = idx.normalize()
    out.index.name = "Date"
    found = {}
    aliases = {
        "Open": ("Open", "open", "시가"),
        "High": ("High", "high", "고가"),
        "Low": ("Low", "low", "저가"),
        "Close": ("Close", "close", "종가"),
        "Volume": ("Volume", "volume", "거래량"),
    }
    for eng, names in aliases.items():
        for name in names:
            if name in out.columns:
                found[name] = eng
                break
        else:
            raise TrendDataError(f"{code} 시세 컬럼이 부족합니다: {list(out.columns)}")
    out = out.rename(columns=found)[["Open", "High", "Low", "Close", "Volume"]]
    out = out.apply(pd.to_numeric, errors="coerce").dropna()
    out = out[~out.index.duplicated(keep="last")].sort_index()
    if out.empty:
        raise TrendDataError(f"{code} 숫자 시세가 없습니다. 가짜 가격으로 대체하지 않습니다.")
    bad = (out["High"] < out["Low"]) | (out["Close"] <= 0) | (out["Open"] <= 0)
    out = out.loc[~bad]
    if out.empty:
        raise TrendDataError(f"{code} 시세 값이 깨져 있습니다. 가짜 가격으로 대체하지 않습니다.")
    return out


def fetch_fdr_bars(code: str, start: date, end: date) -> pd.DataFrame:
    try:
        import FinanceDataReader as fdr
    except ImportError as exc:
        raise TrendDataError(
            "FinanceDataReader 가 없습니다. trend_monitor/requirements.txt 를 설치하세요."
        ) from exc
    try:
        raw = fdr.DataReader(norm_code(code), start.isoformat(), end.isoformat())
    except Exception as exc:
        raise TrendDataError(
            f"{code} FinanceDataReader 조회 실패: {exc}. 가짜 가격으로 대체하지 않습니다."
        ) from exc
    return normalize_ohlcv(raw, code)


def fetch_pykrx_bars(code: str, start: date, end: date) -> pd.DataFrame:
    user = (os.environ.get("KRX_ID") or "").strip()
    password = (os.environ.get("KRX_PW") or "").strip()
    if not user or not password:
        raise TrendDataError(
            "pykrx 를 쓰려면 환경변수 KRX_ID 와 KRX_PW 가 필요합니다. "
            "값이 없으면 시세를 만들지 않고 멈춥니다."
        )
    try:
        from pykrx import stock
    except ImportError as exc:
        raise TrendDataError("pykrx 패키지가 없습니다. KRX 시세를 만들지 않습니다.") from exc
    try:
        raw = stock.get_market_ohlcv(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), norm_code(code))
    except Exception as exc:
        raise TrendDataError(f"{code} pykrx 조회 실패: {exc}. 가짜 가격으로 대체하지 않습니다.") from exc
    return normalize_ohlcv(raw, code)


def fetch_bars(code: str, start: date, end: date, source: str = "fdr") -> pd.DataFrame:
    if source == "pykrx":
        return fetch_pykrx_bars(code, start, end)
    if source != "fdr":
        raise TrendDataError(f"시세 출처를 모릅니다: {source}")
    return fetch_fdr_bars(code, start, end)


def _read_cache(path: Path) -> pd.DataFrame | None:
    if not path.is_file():
        return None
    df = pd.read_csv(path, parse_dates=["Date"], index_col="Date")
    return normalize_ohlcv(df, path.stem)


def _write_cache(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, date_format="%Y-%m-%d")


def load_bars(code: str, start: date, end: date, source: str = "fdr", cache_dir: Path | None = None) -> pd.DataFrame:
    cache = (cache_dir or CACHE) / f"{norm_code(code)}.csv"
    cached = None
    if cache.is_file():
        try:
            cached = _read_cache(cache)
        except TrendDataError:
            cached = None
        if cached is not None and len(cached) and cached.index.min().date() <= start and last_bar_date(cached) >= end:
            return slice_through(cached, start, end)
    fresh = fetch_bars(code, start, end, source)
    if cached is not None and len(cached):
        merged = pd.concat([cached, fresh])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    else:
        merged = fresh
    _write_cache(cache, merged)
    if last_bar_date(merged) < start:
        raise TrendDataError(f"{code} 시세가 {start.isoformat()} 이전에 끝납니다. 가짜 가격으로 대체하지 않습니다.")
    return slice_through(merged, start, end)


def load_listing(source: str = "fdr") -> pd.DataFrame:
    if source == "pykrx":
        raise TrendDataError("상장 목록은 FinanceDataReader StockListing 을 사용합니다. pykrx 목록은 쓰지 않습니다.")
    try:
        import FinanceDataReader as fdr
    except ImportError as exc:
        raise TrendDataError("FinanceDataReader 가 없습니다.") from exc
    try:
        raw = fdr.StockListing("KRX")
    except Exception as exc:
        raise TrendDataError(f"KRX 상장 목록 조회 실패: {exc}. 종목을 만들지 않습니다.") from exc
    if raw is None or len(raw) == 0:
        raise TrendDataError("KRX 상장 목록이 비어 있습니다. 종목을 만들지 않습니다.")
    return raw


def is_preferred_name(name: str) -> bool:
    compact = str(name or "").replace(" ", "")
    if "우선주" in compact:
        return True
    import re

    return re.search(r"\d*우[B-Z]?$", compact) is not None


def is_spac_name(name: str) -> bool:
    return "스팩" in str(name or "").replace(" ", "")


def is_etf_name(name: str) -> bool:
    text = str(name or "").upper()
    return "ETF" in text or "ETN" in text


def filter_universe(listing: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    frame = listing.copy()
    frame["Code"] = frame["Code"].map(norm_code)
    frame["Name"] = frame["Name"].astype(str)
    markets = {"KOSPI", "KOSDAQ", "KOSDAQ GLOBAL"}
    frame = frame[frame["Market"].isin(markets)]
    frame = frame[~frame["Name"].map(is_preferred_name)]
    frame = frame[~frame["Name"].map(is_spac_name)]
    frame = frame[~frame["Name"].map(is_etf_name)]
    frame["Marcap"] = pd.to_numeric(frame.get("Marcap"), errors="coerce")
    frame["Amount"] = pd.to_numeric(frame.get("Amount"), errors="coerce")
    if cfg.download_min_marcap_krw > 0 or cfg.download_min_amount_krw > 0:
        marcap_ok = frame["Marcap"].fillna(0) >= cfg.download_min_marcap_krw
        amount_ok = frame["Amount"].fillna(0) >= cfg.download_min_amount_krw
        if cfg.download_min_marcap_krw <= 0:
            marcap_ok = False
        if cfg.download_min_amount_krw <= 0:
            amount_ok = False
        frame = frame[marcap_ok | amount_ok]
    frame = frame.drop_duplicates(subset=["Code"])
    return frame.reset_index(drop=True)


def output_market(name: str) -> str:
    if name == "KOSPI":
        return "KOSPI"
    return "KOSDAQ"


def download_many(
    codes: list[str],
    start: date,
    end: date,
    source: str = "fdr",
    workers: int = 8,
    cache_dir: Path | None = None,
) -> tuple[dict[str, pd.DataFrame], list[tuple[str, str]]]:
    out: dict[str, pd.DataFrame] = {}
    failures: list[tuple[str, str]] = []
    if not codes:
        return out, failures

    def one(code: str):
        last_error = None
        for attempt in range(3):
            try:
                return load_bars(code, start, end, source, cache_dir)
            except TrendDataError as exc:
                last_error = exc
                time.sleep(0.4 * (attempt + 1))
        raise last_error

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(one, code): code for code in codes}
        done = 0
        total = len(futures)
        for fut in as_completed(futures):
            code = futures[fut]
            done += 1
            try:
                out[code] = fut.result()
            except Exception as exc:  # 개별 종목 실패는 모아서 비율로 판단합니다.
                failures.append((code, str(exc)))
            if done % 50 == 0 or done == total:
                print(f"시세 진행 {done}/{total} 실패 {len(failures)}", flush=True)
    return out, failures


def history_start(target: date, cfg: Config) -> date:
    return target - timedelta(days=int(cfg.history_calendar_days))


def require_reference(frames: dict[str, pd.DataFrame], tickers: tuple[str, ...], target: date) -> list[str]:
    """기준 종목 중 하나라도 기준일 봉이 있어야 합니다. 전부 아니면 오류."""
    ok = []
    details = []
    for ticker in tickers:
        df = frames.get(ticker)
        if df is None or len(df) == 0:
            details.append(f"{ticker} 시세 없음")
            continue
        last = last_bar_date(df)
        if last != target:
            details.append(f"{ticker} 마지막 봉 {last.isoformat()} 이 기준일 {target.isoformat()} 과 다름")
            continue
        ok.append(ticker)
    if not ok:
        raise TrendDataError(
            "기준 종목의 마지막 봉이 기준일과 다릅니다. 시세가 없거나 늦습니다. "
            "가짜 가격으로 대체하지 않습니다. " + "; ".join(details)
        )
    return ok
