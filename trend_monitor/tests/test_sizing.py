from trend_monitor.sizing import size_order


def test_risk_weight_cash_and_total_caps():
    # 1억의 1% = 100만. 주당 위험 5,000원이면 위험 수량은 200.
    qty, note = size_order(
        entry=50_000,
        stop=45_000,
        account=100_000_000,
        risk_pct=1,
        max_weight_pct=20,
        max_stop_distance_pct=10,
        cash=100_000_000,
        risk_room=6_000_000,
    )
    assert qty == 200
    assert "수량 200" in note

    # 주당 위험이 100원이면 위험 수량은 1만, 비중 20% 는 400주.
    qty, note = size_order(
        entry=50_000,
        stop=49_900,
        account=100_000_000,
        risk_pct=1,
        max_weight_pct=20,
        max_stop_distance_pct=10,
        cash=100_000_000,
        risk_room=6_000_000,
    )
    assert qty == 400
    assert "비중" in note

    # 총 위험 여유가 50만원이면 주당 5,000원 기준 100주.
    qty, note = size_order(
        entry=50_000,
        stop=45_000,
        account=100_000_000,
        risk_pct=1,
        max_weight_pct=20,
        max_stop_distance_pct=10,
        cash=100_000_000,
        risk_room=500_000,
    )
    assert qty == 100
    assert "총 위험" in note

    # 현금 100만원이면 50,000원 기준 20주.
    qty, _ = size_order(
        entry=50_000,
        stop=45_000,
        account=100_000_000,
        risk_pct=1,
        max_weight_pct=20,
        max_stop_distance_pct=10,
        cash=1_000_000,
        risk_room=6_000_000,
    )
    assert qty == 20


def test_stop_distance_over_10_percent_is_zero():
    qty, note = size_order(
        entry=10_000,
        stop=8_900,
        account=100_000_000,
        risk_pct=1,
        max_weight_pct=20,
        max_stop_distance_pct=10,
        cash=100_000_000,
        risk_room=6_000_000,
    )
    assert qty == 0
    assert "10" in note

    qty, _ = size_order(
        entry=10_000,
        stop=9_000,
        account=100_000_000,
        risk_pct=1,
        max_weight_pct=100,
        max_stop_distance_pct=10,
        cash=100_000_000,
        risk_room=100_000_000,
    )
    assert qty == 1000
