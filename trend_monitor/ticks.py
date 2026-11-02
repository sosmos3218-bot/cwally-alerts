"""KRX 주식 호가단위.

유가증권시장, 코스닥시장 공통 (법령 안내 2026-07-15, KRX 코스닥 매매제도).
2,000원 미만 1원, 2,000원 이상 5원, 5,000원 이상 10원, 20,000원 이상 50원,
50,000원 이상 100원, 200,000원 이상 500원, 500,000원 이상 1,000원.
경계는 이상/미만입니다. 500,000원은 1,000원 단위입니다.
ETF, ETN, ELW 는 5원 단위이나 이 모니터는 보통주만 다룹니다.
반올림은 decimal ROUND_HALF_UP (0.5 는 0에서 멀어지는 쪽).
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from trend_monitor.errors import UserInputError

# (상한 미만, 호가). 마지막 구간은 상한이 없습니다.
_BANDS: tuple[tuple[Decimal | None, Decimal], ...] = (
    (Decimal("2000"), Decimal("1")),
    (Decimal("5000"), Decimal("5")),
    (Decimal("20000"), Decimal("10")),
    (Decimal("50000"), Decimal("50")),
    (Decimal("200000"), Decimal("100")),
    (Decimal("500000"), Decimal("500")),
    (None, Decimal("1000")),
)


def as_decimal(price: Decimal | int | float | str) -> Decimal:
    if isinstance(price, Decimal):
        return price
    if isinstance(price, int):
        return Decimal(price)
    if isinstance(price, float):
        return Decimal(str(price))
    text = str(price).strip().replace(",", "")
    return Decimal(text)


def tick_size(price: Decimal | int | float | str) -> Decimal:
    d = as_decimal(price)
    if d <= 0:
        raise UserInputError(f"호가단위를 계산할 수 없습니다. 가격이 0 이하입니다: {price}")
    for upper, tick in _BANDS:
        if upper is None or d < upper:
            return tick
    return Decimal("1000")


def round_to_tick(price: Decimal | int | float | str) -> int:
    """가격을 그 가격대 호가단위로 반올림합니다 (ROUND_HALF_UP)."""
    d = as_decimal(price)
    if d <= 0:
        raise UserInputError(f"호가 반올림을 할 수 없습니다. 가격이 0 이하입니다: {price}")
    tick = tick_size(d)
    units = (d / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(units * tick)
