from decimal import Decimal

import pytest

from trend_monitor.errors import UserInputError
from trend_monitor.ticks import round_to_tick, tick_size


def test_tick_bands():
    assert tick_size(1999) == 1
    assert tick_size(2000) == 5
    assert tick_size(4999) == 5
    assert tick_size(5000) == 10
    assert tick_size(20000) == 50
    assert tick_size(50000) == 100
    assert tick_size(200000) == 500
    assert tick_size(499999) == 500
    assert tick_size(500000) == 1000


def test_half_up_not_bankers():
    # 2002.5 / 5 = 400.5 -> 401 -> 2005. 파이썬 round 는 짝수 쪽으로 가서 이 경계가 다릅니다.
    assert round_to_tick(Decimal("2002.5")) == 2005
    assert round_to_tick(Decimal("2002.4")) == 2000
    assert round_to_tick(1999.5) == 2000
    assert round_to_tick(500000) == 500000
    assert round_to_tick(499750) == 500000


def test_rejects_non_positive():
    with pytest.raises(UserInputError):
        round_to_tick(0)
    with pytest.raises(UserInputError):
        round_to_tick(-5)
