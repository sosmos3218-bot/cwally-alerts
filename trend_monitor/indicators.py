"""ATR(Wilder) 과 단순이동평균. 오늘 값은 그날까지의 데이터만 사용합니다."""

from __future__ import annotations

import numpy as np
import pandas as pd


def wilder_atr(high, low, close, n: int = 14) -> np.ndarray:
    """고전 Wilder ATR.

    TR 은 전일 종가가 있는 둘째 봉부터 계산합니다.
    첫 ATR 은 TR n개의 단순평균이고, 그 뒤는 (이전 ATR * (n-1) + TR) / n 입니다.
    반환 배열은 입력과 길이가 같고, 준비되기 전에는 nan 입니다.
    """
    high_a = np.asarray(high, dtype=float)
    low_a = np.asarray(low, dtype=float)
    close_a = np.asarray(close, dtype=float)
    m = len(close_a)
    atr = np.full(m, np.nan)
    if m < n + 1:
        return atr
    tr = np.full(m, np.nan)
    for i in range(1, m):
        tr[i] = max(
            high_a[i] - low_a[i],
            abs(high_a[i] - close_a[i - 1]),
            abs(low_a[i] - close_a[i - 1]),
        )
    atr[n] = float(np.mean(tr[1 : n + 1]))
    for i in range(n + 1, m):
        atr[i] = (atr[i - 1] * (n - 1) + tr[i]) / n
    return atr


def sma(close, window: int) -> np.ndarray:
    series = pd.Series(np.asarray(close, dtype=float))
    return series.rolling(window, min_periods=window).mean().to_numpy()


def atr_excluding_today(high, low, close, n: int = 14) -> float:
    """당일 TR 을 넣지 않은 ATR. 클라이맥스 진폭 비교에 씁니다."""
    atr = wilder_atr(high, low, close, n)
    if len(atr) < 2 or np.isnan(atr[-2]):
        return float("nan")
    return float(atr[-2])
