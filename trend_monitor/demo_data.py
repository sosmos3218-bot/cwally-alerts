"""오프라인 데모 픽스처.

가격은 규칙을 보여 주기 위한 가짜 숫자입니다. 실체결가가 아닙니다.
데모 실행은 이 CSV 만 읽고 네트워크를 쓰지 않습니다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from trend_monitor.calendar_krx import is_trading_day

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
DEMO_DAY = date(2026, 10, 30)
OHLCV = FIXTURE_DIR / "ohlcv"


def sessions_ending(end: date, count: int) -> list[date]:
    days: list[date] = []
    probe = end
    while len(days) < count:
        if is_trading_day(probe):
            days.append(probe)
        probe = date.fromordinal(probe.toordinal() - 1)
        if (end - probe).days > count * 3:
            raise RuntimeError("거래일을 충분히 모으지 못했습니다.")
    days.reverse()
    return days


def _frame(days: list[date], open_, high, low, close, volume) -> pd.DataFrame:
    high = np.maximum(high, np.maximum(open_, close))
    low = np.minimum(low, np.minimum(open_, close))
    df = pd.DataFrame(
        {
            "Open": np.round(open_, 2),
            "High": np.round(high, 2),
            "Low": np.round(low, 2),
            "Close": np.round(close, 2),
            "Volume": volume.astype(float),
        },
        index=pd.to_datetime(days),
    )
    df.index.name = "Date"
    return df


def build_breakout(days: list[date]) -> pd.DataFrame:
    """45거래일 베이스 뒤 종가가 직전 52주 고점을 1% 넘기는 경로."""
    n = len(days)
    pivot = n - 1 - 45
    close = np.zeros(n)
    high = np.zeros(n)
    low = np.zeros(n)
    open_ = np.zeros(n)
    for i in range(pivot):
        close[i] = 4000 + (9600 - 4000) * (i / max(pivot - 1, 1))
        open_[i] = close[i]
        high[i] = min(close[i] * 1.01, 9900)
        low[i] = close[i] * 0.99
    open_[pivot] = 9800
    high[pivot] = 10000
    low[pivot] = 9700
    close[pivot] = 9900
    bottom = pivot + 20
    for i in range(pivot + 1, n - 1):
        if i <= bottom:
            t = (i - pivot) / (bottom - pivot)
            close[i] = 9900 + (8200 - 9900) * t
        else:
            t = (i - bottom) / max(n - 2 - bottom, 1)
            close[i] = 8200 + (9800 - 8200) * t
        open_[i] = close[i]
        high[i] = min(close[i] * 1.015, 9950)
        low[i] = close[i] * 0.985
    low[bottom] = 8200
    high[bottom] = 8500
    close[bottom] = 8300
    open_[bottom] = 8400
    open_[-1] = 9900
    high[-1] = 10200
    low[-1] = 9800
    close[-1] = 10100
    volume = np.full(n, 700_000.0)
    return _frame(days, open_, high, low, close, volume)


def build_hold(days: list[date]) -> pd.DataFrame:
    n = len(days)
    close = np.linspace(10000, 10600, n)
    open_ = close
    high = close + 80
    low = close - 80
    return _frame(days, open_, high, low, close, np.full(n, 100_000.0))


def build_stop(days: list[date]) -> pd.DataFrame:
    n = len(days)
    close = np.full(n, 10000.0)
    close[-1] = 9000
    open_ = close.copy()
    open_[-1] = 9100
    high = close + 50
    high[-1] = 9150
    low = close - 50
    low[-1] = 8900
    return _frame(days, open_, high, low, close, np.full(n, 100_000.0))


def build_climax(days: list[date]) -> pd.DataFrame:
    n = len(days)
    close = np.full(n, 10000.0)
    close[-6:] = [11000, 11600, 12200, 12800, 13000, 12200]
    open_ = close.copy()
    open_[-1] = 13000
    high = close + 100
    high[-1] = 13200
    low = close - 100
    low[-1] = 11800
    # 이전 봉의 범위는 좁게 두어 오늘을 뺀 ATR 이 작습니다.
    high[:-1] = np.maximum(high[:-1], close[:-1] + 80)
    low[:-1] = np.minimum(low[:-1], close[:-1] - 80)
    return _frame(days, open_, high, low, close, np.full(n, 100_000.0))


def write_fixtures() -> None:
    long_days = sessions_ending(DEMO_DAY, 280)
    short_days = long_days[-80:]
    OHLCV.mkdir(parents=True, exist_ok=True)
    frames = {
        "900001": build_breakout(long_days),
        "900010": build_hold(short_days),
        "900011": build_stop(short_days),
        "900012": build_climax(short_days),
    }
    for code, df in frames.items():
        df.to_csv(OHLCV / f"{code}.csv", date_format="%Y-%m-%d")
    listing = pd.DataFrame(
        [
            {"Code": "900001", "Name": "데모돌파", "Market": "KOSPI", "Marcap": 500_000_000_000, "Amount": 8_000_000_000},
            {"Code": "900010", "Name": "데모보유", "Market": "KOSPI", "Marcap": 100_000_000_000, "Amount": 1_000_000_000},
            {"Code": "900011", "Name": "데모손절", "Market": "KOSDAQ", "Marcap": 80_000_000_000, "Amount": 1_000_000_000},
            {"Code": "900012", "Name": "데모클라이맥스", "Market": "KOSDAQ", "Marcap": 80_000_000_000, "Amount": 1_000_000_000},
        ]
    )
    listing.to_csv(FIXTURE_DIR / "listing.csv", index=False)
    holdings = FIXTURE_DIR / "holdings.csv"
    holdings.write_text(
        "ticker,name,buy_date,avg_price,qty,breakout_price,entry_atr14,stop_mode,climax_taken,remaining_qty,notes\n"
        f"900010,데모보유,{short_days[0].isoformat()},10000,10,10000,200,pct,N,10,데모 보유\n"
        f"900011,데모손절,{short_days[0].isoformat()},10000,8,10000,200,pct,N,8,데모 손절\n"
        f"900012,데모클라이맥스,{short_days[-15].isoformat()},10000,10,10000,200,pct,N,10,데모 클라이맥스\n",
        encoding="utf-8",
    )


def load_demo() -> dict:
    listing_path = FIXTURE_DIR / "listing.csv"
    holdings_path = FIXTURE_DIR / "holdings.csv"
    if not listing_path.is_file() or not holdings_path.is_file():
        from trend_monitor.errors import TrendDataError

        raise TrendDataError("데모 픽스처가 없습니다. 가격을 만들지 않습니다.")
    listing = pd.read_csv(listing_path, dtype={"Code": str})
    frames = {}
    for path in sorted(OHLCV.glob("*.csv")):
        df = pd.read_csv(path, parse_dates=["Date"], index_col="Date")
        frames[path.stem] = df
    if "900001" not in frames:
        from trend_monitor.errors import TrendDataError

        raise TrendDataError("데모 돌파 픽스처가 없습니다.")
    return {
        "trade_date": DEMO_DAY,
        "frames": frames,
        "universe": listing,
        "holdings_path": holdings_path,
        "warnings": ["DEMO. 오프라인 픽스처이며 실매매 신호가 아닙니다."],
    }


if __name__ == "__main__":
    write_fixtures()
    print(f"fixtures -> {FIXTURE_DIR}")
