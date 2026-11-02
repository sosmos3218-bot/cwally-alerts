from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from trend_monitor.calendar_krx import (
    SEOUL,
    assert_session_ready,
    default_trade_date,
    is_trading_day,
    now_kst,
    parse_trade_date,
    previous_trading_day,
)
from trend_monitor.errors import UserInputError


def test_parse_both_formats():
    assert parse_trade_date("20261102") == date(2026, 11, 2)
    assert parse_trade_date("2026-11-02") == date(2026, 11, 2)
    with pytest.raises(UserInputError):
        parse_trade_date("2026/11/02")
    with pytest.raises(UserInputError):
        parse_trade_date("202611")


def test_now_is_seoul():
    now = now_kst()
    assert now.tzinfo == SEOUL
    assert now.utcoffset().total_seconds() == 9 * 3600
    with pytest.raises(UserInputError):
        now_kst(datetime(2026, 11, 2, 15, 0))


def test_trading_days_and_extra_closures():
    assert is_trading_day(date(2026, 11, 2))
    assert not is_trading_day(date(2026, 11, 1))  # 일요일
    assert not is_trading_day(date(2026, 1, 1))
    assert not is_trading_day(date(2026, 10, 5))
    assert not is_trading_day(date(2026, 10, 9))
    # XKRX 4.13.2 는 이 날을 개장으로 두지만 KRX 휴장입니다.
    assert not is_trading_day(date(2026, 6, 3))
    assert not is_trading_day(date(2026, 7, 17))
    assert previous_trading_day(date(2026, 11, 2)) == date(2026, 10, 30)


def test_default_session_uses_seoul_close():
    kst = ZoneInfo("Asia/Seoul")
    assert default_trade_date(datetime(2026, 11, 2, 16, 0, tzinfo=kst)) == date(2026, 11, 2)
    assert default_trade_date(datetime(2026, 11, 2, 15, 0, tzinfo=kst)) == date(2026, 10, 30)
    assert default_trade_date(datetime(2026, 11, 1, 18, 0, tzinfo=kst)) == date(2026, 10, 30)


def test_partial_candle_refused_until_close():
    kst = ZoneInfo("Asia/Seoul")
    early = datetime(2026, 11, 2, 15, 0, tzinfo=kst)
    with pytest.raises(UserInputError, match="15:30"):
        assert_session_ready(date(2026, 11, 2), early, allow_partial=False)
    note = assert_session_ready(date(2026, 11, 2), early, allow_partial=True)
    assert note and "미완성" in note
    late = datetime(2026, 11, 2, 15, 30, tzinfo=kst)
    assert assert_session_ready(date(2026, 11, 2), late, allow_partial=False) is None
    with pytest.raises(UserInputError, match="거래일"):
        assert_session_ready(date(2026, 11, 1), late, allow_partial=False)
