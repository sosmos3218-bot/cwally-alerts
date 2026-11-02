"""시가총액 상위 종목의 50일선 위 비율."""

from __future__ import annotations

import pandas as pd

from trend_monitor.config import Config


def market_breadth(universe: pd.DataFrame, frames: dict, target, cfg: Config) -> tuple[float | None, str]:
    """상위 N종목 중 당일 종가가 50일 이평보다 높은 비율(퍼센트).

    시세를 계산할 수 없는 종목은 분모에서 뺍니다. 한 종목도 못 세면 None.
    """
    if universe is None or len(universe) == 0:
        return None, "상장 목록이 없어 시장 폭을 계산하지 못했습니다."
    ranked = universe.copy()
    ranked["Marcap"] = pd.to_numeric(ranked["Marcap"], errors="coerce")
    ranked = ranked.sort_values(["Marcap", "Code"], ascending=[False, True])
    top = ranked.head(int(cfg.breadth_top_n))
    window = int(cfg.breadth_ma_window)
    above = 0
    counted = 0
    missing = 0
    for row in top.itertuples(index=False):
        code = str(row.Code)
        df = frames.get(code)
        if df is None or len(df) == 0:
            missing += 1
            continue
        upto = df[df.index <= pd.Timestamp(target)]
        if len(upto) < window or upto.index[-1].date() != target:
            missing += 1
            continue
        ma = float(upto["Close"].tail(window).mean())
        close = float(upto["Close"].iloc[-1])
        if ma != ma:
            missing += 1
            continue
        counted += 1
        if close > ma:
            above += 1
    if counted == 0:
        return None, (
            f"시가총액 상위 {len(top)}종목의 {window}일선을 계산하지 못했습니다. 시장 폭 unknown."
        )
    pct = round(above / counted * 100.0, 2)
    note = (
        f"시가총액 상위 {len(top)}종목 중 {window}일선을 계산한 {counted}종목에서 "
        f"{above}종목({pct}%)이 {window}일선 위입니다."
    )
    if missing:
        note += f" 시세가 부족한 {missing}종목은 분모에서 뺐습니다."
    if pct < cfg.breadth_min_pct:
        note += f" {cfg.breadth_min_pct:g}% 미만이라 신규 수량을 절반으로 줄입니다."
    return pct, note
