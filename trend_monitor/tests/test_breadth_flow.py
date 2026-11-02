import csv
from datetime import date
from pathlib import Path

import pandas as pd

from trend_monitor.breadth import market_breadth
from trend_monitor.config import Config
from trend_monitor.engine import _allocate
from trend_monitor.flow import FlowClient, parse_signed_int, summarize_trend
from trend_monitor.watchlist import WatchItem, evaluate_watchlist, load_watchlist


def test_example_watchlist_is_empty():
    text = Path("trend_monitor/watchlist.example.csv").read_text(encoding="utf-8")
    rows = list(csv.DictReader(text.splitlines()))
    assert rows == []
    assert "ticker" in text and "pivot_price" in text and "stop_price" in text


def test_watchlist_pivot_and_stop():
    days = pd.bdate_range("2026-10-01", "2026-10-30")
    up = pd.DataFrame({"Open": 10, "High": 12, "Low": 9, "Close": 11, "Volume": 1}, index=days)
    down = up.copy()
    down.iloc[-1, down.columns.get_loc("Close")] = 8
    items = [
        WatchItem("900001", "위", 10, 7, ""),
        WatchItem("900002", "아래", 12, 9, ""),
    ]
    rows = evaluate_watchlist(items, {"900001": up, "900002": down}, date(2026, 10, 30))
    assert rows[0]["status"] == "pivot_break"
    assert rows[0]["pivot_break"] is True
    assert rows[0]["stop_hit"] is False
    assert rows[1]["status"] == "stop"
    missing = evaluate_watchlist([WatchItem("900003", "없음", 10, 5, "")], {}, date(2026, 10, 30))
    assert missing[0]["status"] == "unavailable"
    assert load_watchlist(None) == []


def test_breadth_and_halved_qty():
    days = pd.bdate_range("2026-06-01", periods=80)
    above = pd.DataFrame({"Close": range(100, 180), "Open": 1, "High": 1, "Low": 1, "Volume": 1}, index=days)
    below = pd.DataFrame({"Close": list(range(180, 100, -1)), "Open": 1, "High": 1, "Low": 1, "Volume": 1}, index=days)
    # 80일 상승은 종가 > 50일 평균, 하락은 종가 < 50일 평균.
    listing = pd.DataFrame(
        [
            {"Code": "000001", "Name": "위", "Market": "KOSPI", "Marcap": 200},
            {"Code": "000002", "Name": "아래", "Market": "KOSDAQ", "Marcap": 100},
        ]
    )
    cfg = Config(breadth_top_n=2, breadth_min_pct=30, breadth_ma_window=50)
    pct, note = market_breadth(listing, {"000001": above, "000002": below}, days[-1].date(), cfg)
    assert pct == 50.0
    assert "50.0%" in note
    low = 20.0
    cand = {
        "ticker": "000001",
        "name": "위",
        "market": "KOSPI",
        "close": 10000,
        "high52": 10000,
        "breakout_price": 10000,
        "distance_pct": 1.0,
        "atr14": 100,
        "base_days": 40,
        "base_depth_pct": 15,
        "stop": 9250,
        "eps_yoy_pct": None,
        "eps_note": "EPS unknown",
        "flow_note": "수급 확인 불가",
        "foreign_net_5d": None,
        "foreign_net_10d": None,
        "institution_net_5d": None,
        "institution_net_10d": None,
    }
    sized, risk = _allocate([dict(cand)], [], cfg, low)
    full, _ = _allocate([dict(cand)], [], cfg, 80.0)
    assert sized[0]["status"] == "ok"
    assert "절반" in sized[0]["reason"]
    assert sized[0]["qty"] == full[0]["qty"] // 2
    assert risk["cash_after"] > 0


def test_flow_sums_and_failure():
    assert parse_signed_int("+1,742,739") == 1742739
    assert parse_signed_int("-1,072,820") == -1072820
    rows = []
    for i in range(12):
        day = date(2026, 10, 1 + i) if i < 30 else date(2026, 10, 1)
        # 10월 1일부터 12일. 주말은 테스트에서 그냥 날짜로 둡니다.
        rows.append(
            {
                "bizdate": date(2026, 10, 1 + i).strftime("%Y%m%d") if i < 20 else "bad",
                "foreignerPureBuyQuant": "10",
                "organPureBuyQuant": "-3",
            }
        )
    # 10월 1일~12일만 유효. 기준일 10월 6일이면 1~6일 중 최근 5일은 2~6일 = 5일 * 10.
    rows = [
        {"bizdate": date(2026, 10, day).strftime("%Y%m%d"), "foreignerPureBuyQuant": "10", "organPureBuyQuant": "-3"}
        for day in range(1, 13)
    ]
    summary = summarize_trend(rows, date(2026, 10, 12))
    assert summary["foreign_net_5d"] == 50
    assert summary["foreign_net_10d"] == 100
    assert summary["institution_net_5d"] == -15
    assert summary["ok"] is True
    future = summarize_trend(rows, date(2026, 9, 1))
    assert future["ok"] is False

    def boom(url):
        raise RuntimeError("down")

    client = FlowClient(transport=boom, pause=0)
    missed = client.lookup("005930", date(2026, 11, 2))
    assert missed["foreign_net_5d"] is None
    assert missed["flow_note"] == "수급 확인 불가"
    # 같은 종목은 다시 호출하지 않습니다.
    assert client.lookup("005930", date(2026, 11, 2))["ok"] is False
    assert client._calls == 1
