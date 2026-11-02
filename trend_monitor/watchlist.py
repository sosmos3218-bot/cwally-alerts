"""관심 종목. 예시 파일은 헤더만 있고 종목을 채우지 않습니다.

실제 파일은 watchlist.csv 또는 TREND_WATCHLIST_PATH 입니다.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from trend_monitor.config import ROOT
from trend_monitor.errors import UserInputError

COLUMNS = ["ticker", "name", "pivot_price", "stop_price", "notes"]


@dataclass
class WatchItem:
    ticker: str
    name: str
    pivot_price: float
    stop_price: float
    notes: str


def resolve_watchlist_path(explicit: str | None) -> Path | None:
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise UserInputError(f"관심 종목 파일이 없습니다: {path}")
        return path
    env = (os.environ.get("TREND_WATCHLIST_PATH") or "").strip()
    if env:
        path = Path(env)
        if not path.is_file():
            raise UserInputError(f"TREND_WATCHLIST_PATH 파일을 읽지 못했습니다: {path}")
        return path
    default = ROOT / "watchlist.csv"
    if default.is_file():
        return default
    return None


def load_watchlist(path: Path | None) -> list[WatchItem]:
    if path is None:
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise UserInputError(f"관심 종목 CSV 헤더가 없습니다: {path}")
        missing = [col for col in COLUMNS if col not in reader.fieldnames]
        if missing:
            raise UserInputError(f"관심 종목 CSV 에 칸이 없습니다: {', '.join(missing)}")
        out: list[WatchItem] = []
        for line_no, row in enumerate(reader, start=2):
            if not any((value or "").strip() for value in row.values()):
                continue
            ticker = str(row.get("ticker") or "").strip()
            if ticker.isdigit():
                ticker = ticker.zfill(6)
            if not ticker:
                raise UserInputError(f"관심 종목 {line_no}행 종목코드가 없습니다.")
            try:
                pivot = float(str(row.get("pivot_price") or "").replace(",", ""))
                stop = float(str(row.get("stop_price") or "").replace(",", ""))
            except ValueError as exc:
                raise UserInputError(f"{ticker} 피벗 또는 손절이 숫자가 아닙니다.") from exc
            if pivot <= 0 or stop <= 0:
                raise UserInputError(f"{ticker} 피벗과 손절은 0보다 커야 합니다.")
            out.append(
                WatchItem(
                    ticker=ticker,
                    name=str(row.get("name") or ticker).strip() or ticker,
                    pivot_price=pivot,
                    stop_price=stop,
                    notes=str(row.get("notes") or "").strip(),
                )
            )
    return out


def evaluate_watchlist(items: list[WatchItem], frames: dict, target) -> list[dict]:
    rows = []
    for item in items:
        df = frames.get(item.ticker)
        base = {
            "ticker": item.ticker,
            "name": item.name,
            "pivot_price": item.pivot_price,
            "stop_price": item.stop_price,
            "notes": item.notes,
            "close": None,
            "pivot_break": False,
            "stop_hit": False,
            "status": "unavailable",
            "foreign_net_5d": None,
            "foreign_net_10d": None,
            "institution_net_5d": None,
            "institution_net_10d": None,
            "reason": "시세가 없어 피벗과 손절을 확인하지 못했습니다.",
        }
        if df is None or len(df) == 0:
            rows.append(base)
            continue
        upto = df[df.index <= pd.Timestamp(target)]
        if len(upto) == 0 or upto.index[-1].date() != target:
            last = upto.index[-1].date().isoformat() if len(upto) else "없음"
            base["reason"] = f"마지막 봉 {last} 이 기준일과 다릅니다. 가격을 만들지 않습니다."
            rows.append(base)
            continue
        close = float(upto["Close"].iloc[-1])
        pivot_break = close >= item.pivot_price
        stop_hit = close <= item.stop_price
        if pivot_break and stop_hit:
            status = "both"
            text = "종가가 피벗 이상이고 손절 이하입니다."
        elif pivot_break:
            status = "pivot_break"
            text = "종가가 피벗 이상입니다. 돌파."
        elif stop_hit:
            status = "stop"
            text = "종가가 손절 이하입니다."
        else:
            status = "inside"
            text = "종가가 피벗 미만, 손절 초과입니다."
        base.update(
            {
                "close": close,
                "pivot_break": pivot_break,
                "stop_hit": stop_hit,
                "status": status,
                "reason": text,
            }
        )
        rows.append(base)
    return rows
