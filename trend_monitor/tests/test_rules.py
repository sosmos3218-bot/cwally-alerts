from trend_monitor.rules import DayView, ExitParams, PositionView, evaluate_exit


def _pos(**kwargs):
    base = dict(
        ticker="900001",
        name="테스트",
        avg_price=10000,
        remaining_qty=10,
        breakout_price=10000,
        entry_atr=200,
        stop_mode="pct",
        climax_taken=False,
    )
    base.update(kwargs)
    return PositionView(**base)


def _day(**kwargs):
    base = dict(open=10000, high=10100, low=9900, close=10000, ma=9800, atr_prev=200, max_high_since_entry=10100)
    base.update(kwargs)
    return DayView(**base)


def test_stop_beats_ma_and_climax():
    # 종가 9200 은 손절 9250 이하, 이평 아래, 음봉 조건도 겹칩니다.
    row = evaluate_exit(
        _pos(),
        _day(open=11000, high=11000, low=9000, close=9200, ma=11000, atr_prev=100, max_high_since_entry=13000),
        ExitParams(),
    )
    assert row["signal"] == "stop"
    assert row["sell_qty"] == 10


def test_ma_break_beats_climax_and_sells_all():
    row = evaluate_exit(
        _pos(),
        _day(open=12000, high=12500, low=10700, close=10800, ma=11000, atr_prev=400, max_high_since_entry=13000),
        ExitParams(),
    )
    assert row["signal"] == "ma_break"
    assert row["sell_qty"] == 10
    assert "전량" in row["reason"]


def test_climax_half_uses_prior_atr():
    row = evaluate_exit(
        _pos(),
        _day(open=12500, high=12600, low=11400, close=11500, ma=11000, atr_prev=400, max_high_since_entry=13000),
        ExitParams(),
    )
    assert row["signal"] == "climax_half"
    assert row["sell_qty"] == 5
    # 익일 방어선은 max(평단 10000, 이평 11000)
    assert row["defense_line"] == 11000


def test_panic_uses_close_not_low():
    params = ExitParams()
    scared_low = evaluate_exit(
        _pos(),
        _day(open=10000, high=10100, low=8000, close=10000, ma=9000, max_high_since_entry=10100),
        params,
    )
    assert scared_low["signal"] == "hold"
    panic = evaluate_exit(
        _pos(),
        _day(open=9000, high=9100, low=8600, close=8700, ma=9500, max_high_since_entry=10100),
        params,
    )
    assert panic["signal"] == "panic"
    assert panic["sell_qty"] == 10


def test_after_climax_defense_is_max_avg_and_ma():
    # 평단 10000, 이평 11000, 종가 10500. 옛 버그(max(손절, 평단))면 보유지만 이평이 더 높습니다.
    row = evaluate_exit(
        _pos(climax_taken=True),
        _day(close=10500, ma=11000, max_high_since_entry=13000),
        ExitParams(),
    )
    assert row["signal"] == "ma_break"
    assert row["defense_line"] == 11000

    # 이평이 평단보다 낮고 종가가 평단 아래면 손절(본전)이 우선입니다.
    row = evaluate_exit(
        _pos(avg_price=12000, climax_taken=True),
        _day(close=11500, ma=11000, max_high_since_entry=14000),
        ExitParams(),
    )
    assert row["signal"] == "stop"
    assert row["defense_line"] == 12000


def test_breadth_missing_blocks_climax_only():
    row = evaluate_exit(
        _pos(),
        _day(open=12500, high=12600, low=11400, close=11500, ma=11000, atr_prev=400, max_high_since_entry=13000),
        ExitParams(require_breadth=True),
        breadth_ok=None,
    )
    assert row["signal"] == "hold"
    assert "업종" in row["reason"]
