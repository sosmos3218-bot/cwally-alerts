"""52주 신고가, 35~60일 베이스, 이평 정배열 스크리너.

기준일 종가와 그 이전 봉만 사용합니다. 미래 봉은 넣지 않습니다.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from trend_monitor.config import Config
from trend_monitor.indicators import sma, wilder_atr
from trend_monitor.ticks import round_to_tick


def prepare(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    out = df.copy()
    close = out["Close"].to_numpy(dtype=float)
    high = out["High"].to_numpy(dtype=float)
    low = out["Low"].to_numpy(dtype=float)
    volume = out["Volume"].to_numpy(dtype=float)
    out["atr14"] = wilder_atr(high, low, close, 14)
    for window in set(cfg.trend_mas) | {cfg.ma_window, 20, 50}:
        out[f"ma{window}"] = sma(close, window)
    out["prior_high"] = pd.Series(high).rolling(cfg.high52_days, min_periods=cfg.high52_days).max().shift(1).to_numpy()
    value = close * volume
    out["avg_value"] = pd.Series(value).rolling(cfg.liquidity_avg_days, min_periods=cfg.liquidity_avg_days).mean().to_numpy()
    return out


def _base(high: np.ndarray, low: np.ndarray, i: int, pivot_high: float, lookback: int) -> tuple[int, float] | None:
    start = i - lookback
    if start < 0:
        return None
    window = high[start:i]
    if len(window) == 0 or not math.isfinite(pivot_high) or pivot_high <= 0:
        return None
    hits = np.where(np.isclose(window, pivot_high, rtol=0.0, atol=1e-6))[0]
    if len(hits) == 0:
        top = float(np.max(window))
        if top <= 0:
            return None
        hits = np.where(window == top)[0]
        pivot_high = top
    pivot_idx = start + int(hits[-1])
    base_days = i - pivot_idx
    base_low = float(np.min(low[pivot_idx:i]))
    return base_days, base_low


def screen_at(frame: pd.DataFrame, i: int, cfg: Config) -> dict | None:
    """i 행(포함)까지만 의미가 있는 지표가 이미 prepare 되어 있어야 합니다."""
    row = frame.iloc[i]
    close = float(row["Close"])
    high = float(row["High"])
    prior = float(row["prior_high"]) if pd.notna(row["prior_high"]) else float("nan")
    if not math.isfinite(prior) or prior <= 0 or not math.isfinite(close) or close <= 0:
        return None
    if close < prior:
        return None
    fast, mid, slow = cfg.trend_mas
    ma_fast = float(row[f"ma{fast}"])
    ma_mid = float(row[f"ma{mid}"])
    ma_slow = float(row[f"ma{slow}"])
    if not all(math.isfinite(x) for x in (ma_fast, ma_mid, ma_slow)):
        return None
    if not (close > ma_fast > ma_mid > ma_slow):
        return None
    avg_value = float(row["avg_value"]) if pd.notna(row["avg_value"]) else float("nan")
    if not math.isfinite(avg_value) or avg_value < cfg.liquidity_min_avg_value_krw:
        return None
    atr = float(row["atr14"]) if pd.notna(row["atr14"]) else float("nan")
    if not math.isfinite(atr) or atr <= 0:
        return None
    high_a = frame["High"].to_numpy(dtype=float)
    low_a = frame["Low"].to_numpy(dtype=float)
    found = _base(high_a, low_a, i, prior, cfg.high52_days)
    if found is None:
        return None
    base_days, base_low = found
    if not (cfg.base_min_days <= base_days <= cfg.base_max_days):
        return None
    depth = (prior - base_low) / prior * 100.0
    if depth < cfg.base_min_depth_pct or depth > cfg.base_max_depth_pct:
        return None
    window_start = max(0, i - cfg.high52_days + 1)
    high52 = float(np.max(high_a[window_start : i + 1]))
    distance = (close - prior) / prior * 100.0
    if cfg.stop_mode == "atr":
        raw_stop = prior - cfg.atr_k * atr
    else:
        raw_stop = prior * (1 - cfg.stop_pct / 100.0)
    stop = round_to_tick(raw_stop)
    return {
        "close": close,
        "high52": high52,
        "breakout_price": prior,
        "distance_pct": distance,
        "atr14": atr,
        "base_days": int(base_days),
        "base_depth_pct": depth,
        "stop": stop,
        "high_today": high,
    }


def screen_frame(df: pd.DataFrame, cfg: Config) -> dict | None:
    need = max(cfg.high52_days + 1, cfg.trend_mas[-1])
    if len(df) < need:
        return None
    ready = prepare(df, cfg)
    return screen_at(ready, len(ready) - 1, cfg)


def iter_breakout_indexes(frame: pd.DataFrame, cfg: Config, start, end) -> list[int]:
    """prepare 된 프레임에서 기간 안의 돌파 인덱스만."""
    out = []
    fast, mid, slow = cfg.trend_mas
    for i in range(len(frame)):
        day = frame.index[i].date()
        if day < start or day > end:
            continue
        hit = screen_at(frame, i, cfg)
        if hit is not None:
            out.append(i)
        else:
            # screen_at 가 이미 추세와 유동성을 봅니다. 루프 비용은 돌파가 아닐 때
            # prior 비교에서 바로 빠집니다.
            del hit
    return out
