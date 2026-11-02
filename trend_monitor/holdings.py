"""보유 CSV 와 상태 JSON.

실보유 파일은 저장소의 holdings.csv 또는 환경변수 TREND_HOLDINGS_PATH 입니다.
상태 파일은 --apply-state 일 때만 기록합니다.
"""

from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from trend_monitor.config import ROOT, normalize_stop_mode
from trend_monitor.errors import UserInputError

SEOUL = ZoneInfo("Asia/Seoul")
COLUMNS = [
    "ticker",
    "name",
    "buy_date",
    "avg_price",
    "qty",
    "breakout_price",
    "entry_atr14",
    "stop_mode",
    "climax_taken",
    "remaining_qty",
    "notes",
]


@dataclass
class Holding:
    ticker: str
    name: str
    buy_date: date
    avg_price: float
    qty: int
    breakout_price: float
    entry_atr: float | None
    stop_mode: str
    climax_taken: bool
    remaining_qty: int
    notes: str


def resolve_holdings_path(explicit: str | None) -> Path | None:
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise UserInputError(f"보유 파일이 없습니다: {path}")
        return path
    env = (os.environ.get("TREND_HOLDINGS_PATH") or "").strip()
    if env:
        path = Path(env)
        if not path.is_file():
            raise UserInputError(f"TREND_HOLDINGS_PATH 파일을 읽지 못했습니다: {path}")
        return path
    default = ROOT / "holdings.csv"
    if default.is_file():
        return default
    return None


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"y", "yes", "true", "1", "예", "참"}


def _num(value: str | None, label: str, ticker: str) -> float:
    text = str(value or "").strip().replace(",", "")
    if text == "":
        raise UserInputError(f"{ticker} {label} 이 비었습니다.")
    try:
        return float(text)
    except ValueError as exc:
        raise UserInputError(f"{ticker} {label} 이 숫자가 아닙니다: {value}") from exc


def load_holdings(path: Path | None, default_stop: str) -> list[Holding]:
    if path is None:
        return []
    out: list[Holding] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise UserInputError(f"보유 CSV 헤더가 없습니다: {path}")
        missing = [c for c in ("ticker", "buy_date", "avg_price", "qty", "breakout_price") if c not in reader.fieldnames]
        if missing:
            raise UserInputError(f"보유 CSV 에 칸이 없습니다: {', '.join(missing)}")
        for line_no, row in enumerate(reader, start=2):
            if not any((v or "").strip() for v in row.values()):
                continue
            ticker = str(row.get("ticker") or "").strip()
            if ticker.isdigit():
                ticker = ticker.zfill(6)
            if not ticker:
                raise UserInputError(f"{line_no}행 종목코드가 없습니다.")
            from trend_monitor.calendar_krx import parse_trade_date

            try:
                buy = parse_trade_date(str(row.get("buy_date") or ""))
            except UserInputError as exc:
                raise UserInputError(f"{ticker} 매수일: {exc}") from exc
            avg = _num(row.get("avg_price"), "평균단가", ticker)
            qty = int(_num(row.get("qty"), "수량", ticker))
            breakout = _num(row.get("breakout_price"), "돌파가", ticker)
            if avg <= 0 or qty <= 0 or breakout <= 0:
                raise UserInputError(f"{ticker} 평균단가, 수량, 돌파가는 0보다 커야 합니다.")
            atr_raw = str(row.get("entry_atr14") or "").strip()
            entry_atr = float(atr_raw.replace(",", "")) if atr_raw else None
            if entry_atr is not None and entry_atr <= 0:
                raise UserInputError(f"{ticker} entry_atr14 는 비우거나 0보다 커야 합니다.")
            mode = normalize_stop_mode(row.get("stop_mode"), default_stop)
            rem_raw = str(row.get("remaining_qty") or "").strip()
            remaining = int(float(rem_raw.replace(",", ""))) if rem_raw else qty
            if remaining < 0 or remaining > qty:
                raise UserInputError(f"{ticker} 잔량이 수량 범위를 벗어났습니다.")
            out.append(
                Holding(
                    ticker=ticker,
                    name=str(row.get("name") or ticker).strip(),
                    buy_date=buy,
                    avg_price=avg,
                    qty=qty,
                    breakout_price=breakout,
                    entry_atr=entry_atr,
                    stop_mode=mode,
                    climax_taken=_truthy(row.get("climax_taken")),
                    remaining_qty=remaining,
                    notes=str(row.get("notes") or "").strip(),
                )
            )
    return out


def load_state(path: Path) -> dict:
    if not path.is_file():
        return {"version": 1, "applied": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("applied", {}), dict):
        raise UserInputError(f"상태 파일 형식이 아닙니다: {path}")
    data.setdefault("version", 1)
    data.setdefault("applied", {})
    return data


def apply_state(holdings: list[Holding], state: dict) -> list[str]:
    notes = []
    applied = state.get("applied") or {}
    for pos in holdings:
        item = applied.get(pos.ticker)
        if not isinstance(item, dict):
            continue
        if "climax_taken" in item:
            pos.climax_taken = bool(item["climax_taken"])
        if item.get("remaining_qty") is not None:
            pos.remaining_qty = int(item["remaining_qty"])
        notes.append(f"상태 적용 {pos.ticker} 잔량 {pos.remaining_qty} 클라이맥스 {pos.climax_taken}")
    return notes


def write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now(SEOUL).isoformat(timespec="seconds")
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def suggest_and_maybe_apply(holdings_out: list[dict], state: dict, trade_date: str, path: Path, apply: bool) -> dict:
    """클라이맥스와 전량 매도 제안. apply 일 때만 파일에 반영합니다."""
    applied = dict(state.get("applied") or {})
    for row in holdings_out:
        ticker = row["ticker"]
        if row["signal"] == "climax_half":
            prev = applied.get(ticker) or {}
            base_qty = int(prev.get("remaining_qty", row.get("_remaining_before", 0)))
            # sell_qty 는 이번 판단 수량. 남은 수량은 호출 전에 계산해 _remaining_after 로 넘깁니다.
            applied[ticker] = {
                "climax_taken": True,
                "remaining_qty": int(row.get("_remaining_after", 0)),
                "trade_date": trade_date,
                "reason": "climax_half",
            }
            del base_qty
        elif row["signal"] in {"stop", "ma_break", "panic"}:
            applied[ticker] = {
                "climax_taken": True if row["signal"] != "stop" else bool((applied.get(ticker) or {}).get("climax_taken", False)),
                "remaining_qty": 0,
                "trade_date": trade_date,
                "reason": row["signal"],
            }
    new_state = {"version": 1, "applied": applied}
    if apply:
        write_state(path, new_state)
    return new_state
