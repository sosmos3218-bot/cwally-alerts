"""보유 종목 매도 규칙. 종가 기준.

우선순위: 패닉 > 손절 > 이평 이탈 > 클라이맥스.
패닉은 종가만 보고 장중 저가는 쓰지 않습니다.
클라이맥스 진폭은 오늘을 뺀 ATR14 와 비교합니다.
클라이맥스 체결 뒤 익일 방어선은 max(평단, 이평) 입니다.
클라이맥스 봉이 동시에 이평 아래면 전량 매도(이평이 우선)입니다.
"""

from __future__ import annotations

from dataclasses import dataclass

from trend_monitor.ticks import round_to_tick


@dataclass(frozen=True)
class ExitParams:
    stop_pct: float = 7.5
    atr_k: float = 2.0
    panic_enabled: bool = True
    panic_pct: float = 12.0
    climax_min_gain_pct: float = 20.0
    climax_body_pct: float = -4.5
    climax_range_atr: float = 2.0
    climax_sell_fraction: float = 0.5
    ma_arm_gain_pct: float = 8.0
    require_breadth: bool = False


@dataclass(frozen=True)
class PositionView:
    ticker: str
    name: str
    avg_price: float
    remaining_qty: int
    breakout_price: float
    entry_atr: float
    stop_mode: str  # pct | atr
    climax_taken: bool


@dataclass(frozen=True)
class DayView:
    open: float
    high: float
    low: float
    close: float
    ma: float
    atr_prev: float
    max_high_since_entry: float


def initial_stop(pos: PositionView, params: ExitParams) -> int:
    if pos.stop_mode == "atr":
        raw = pos.breakout_price - params.atr_k * pos.entry_atr
    elif pos.stop_mode == "pct":
        raw = pos.breakout_price * (1 - params.stop_pct / 100.0)
    else:
        raise ValueError(f"손절 방식을 모릅니다: {pos.stop_mode}")
    return round_to_tick(raw)


def _max_gain_pct(pos: PositionView, day: DayView) -> float:
    if pos.avg_price <= 0:
        return 0.0
    return (day.max_high_since_entry - pos.avg_price) / pos.avg_price * 100.0


def defense_price(pos: PositionView, day: DayView, params: ExitParams, *, climax_after: bool) -> int:
    """다음 날 방어선. 클라이맥스 반영 후면 max(평단, 이평)."""
    stop = initial_stop(pos, params)
    ma_line = round_to_tick(day.ma)
    if climax_after:
        return round_to_tick(max(pos.avg_price, ma_line))
    if _max_gain_pct(pos, day) >= params.ma_arm_gain_pct:
        return max(stop, ma_line)
    return stop


def evaluate_exit(
    pos: PositionView,
    day: DayView,
    params: ExitParams,
    breadth_ok: bool | None = None,
) -> dict:
    """breadth_ok: True 충족, False 미달, None 데이터 없음. require_breadth 일 때만 봅니다."""
    if pos.remaining_qty <= 0:
        return _row(pos, day, params, "hold", 0, "잔량이 0입니다. 매도할 주식이 없습니다.", climax_after=pos.climax_taken)

    stop = initial_stop(pos, params)
    ma_line = round_to_tick(day.ma)
    gain = _max_gain_pct(pos, day)
    ma_armed = pos.climax_taken or gain >= params.ma_arm_gain_pct
    close = day.close

    panic_line = None
    if params.panic_enabled:
        panic_line = round_to_tick(pos.breakout_price * (1 - params.panic_pct / 100.0))

    # 손절선: 클라이맥스 뒤에는 평단도 손절 바닥입니다. 이평은 별도 규칙입니다.
    stop_floor = stop
    if pos.climax_taken:
        stop_floor = max(stop, round_to_tick(pos.avg_price))

    panic_hit = panic_line is not None and close <= panic_line
    stop_hit = close <= stop_floor
    ma_hit = ma_armed and close < ma_line

    body_pct = ((close - day.open) / day.open * 100.0) if day.open else 0.0
    span = day.high - day.low
    candle = (
        (not pos.climax_taken)
        and gain >= params.climax_min_gain_pct
        and body_pct <= params.climax_body_pct
        and day.atr_prev == day.atr_prev
        and day.atr_prev > 0
        and span >= params.climax_range_atr * day.atr_prev
    )
    breadth_note = ""
    if params.require_breadth:
        if breadth_ok is True:
            breadth_note = " 업종 폭 조건 충족."
        elif breadth_ok is False:
            candle = False
            breadth_note = " 업종 폭이 기준 미달이라 클라이맥스를 보지 않습니다."
        else:
            candle = False
            breadth_note = " 업종 데이터를 못 구해 클라이맥스를 보지 않습니다."

    if panic_hit:
        return _row(
            pos,
            day,
            params,
            "panic",
            pos.remaining_qty,
            (
                f"종가 {int(round(close)):,}원이 패닉선 {panic_line:,}원 이하입니다. "
                "종가 기준이며 장중 저가는 쓰지 않습니다. 잔량 전량 매도."
            ),
            climax_after=pos.climax_taken,
        )
    if stop_hit:
        why = (
            f"종가 {int(round(close)):,}원이 손절 {stop_floor:,}원 이하입니다. 잔량 전량 매도."
        )
        if pos.climax_taken and stop_floor >= round_to_tick(pos.avg_price):
            why = (
                f"종가 {int(round(close)):,}원이 클라이맥스 이후 방어 바닥 {stop_floor:,}원 이하입니다. "
                "잔량 전량 매도."
            )
        return _row(pos, day, params, "stop", pos.remaining_qty, why, climax_after=pos.climax_taken)
    if ma_hit:
        extra = ""
        if candle:
            extra = " 같은 봉이 클라이맥스 음봉이어도 이평 이탈이 우선이라 전량입니다."
        return _row(
            pos,
            day,
            params,
            "ma_break",
            pos.remaining_qty,
            (
                f"종가 {int(round(close)):,}원이 이평 {ma_line:,}원 아래입니다. "
                f"잔량 전량 매도.{extra}{breadth_note}"
            ),
            climax_after=pos.climax_taken,
        )
    if candle:
        sell = int(pos.remaining_qty * params.climax_sell_fraction)
        # 소수점 이하는 버립니다. 1주면 절반이 0이라 신호만 알리고 수량은 0으로 둡니다.
        if sell < 1:
            return _row(
                pos,
                day,
                params,
                "hold",
                0,
                "클라이맥스 음봉이지만 잔량 1주 미만이라 50% 수량이 0입니다." + breadth_note,
                climax_after=False,
            )
        left = pos.remaining_qty - sell
        return _row(
            pos,
            day,
            params,
            "climax_half",
            sell,
            (
                f"최대 수익 {gain:.1f}% 이후 장대음봉(몸통 {body_pct:.1f}%, 범위 {span:.0f}, "
                f"전일까지 ATR {day.atr_prev:.1f}). 잔량 {pos.remaining_qty}주의 절반 {sell}주 매도. "
                f"남은 {left}주의 익일 방어선은 평단과 이평 중 높은 값입니다.{breadth_note}"
            ),
            climax_after=True,
        )

    line = defense_price(pos, day, params, climax_after=pos.climax_taken)
    return _row(
        pos,
        day,
        params,
        "hold",
        0,
        f"보유 유지. 익일 방어선 {line:,}원.{breadth_note}",
        climax_after=pos.climax_taken,
    )


def _row(pos, day, params, signal, sell_qty, reason, *, climax_after: bool) -> dict:
    ret = (day.close - pos.avg_price) / pos.avg_price * 100.0 if pos.avg_price else 0.0
    return {
        "signal": signal,
        "sell_qty": int(sell_qty),
        "stop": initial_stop(pos, params) if not pos.climax_taken else max(initial_stop(pos, params), round_to_tick(pos.avg_price)),
        "ma": round_to_tick(day.ma),
        "defense_line": defense_price(pos, day, params, climax_after=climax_after),
        "reason": reason.strip(),
        "return_pct": round(ret, 2),
    }
