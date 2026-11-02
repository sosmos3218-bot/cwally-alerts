from trend_monitor.config import Config
from trend_monitor.ntfy_summary import assert_plain_korean, build_message, delay_message


def _payload():
    return {
        "demo": False,
        "trade_date": "2026-11-02",
        "holdings": [
            {
                "ticker": "005930",
                "name": "삼성전자",
                "signal": "stop",
                "sell_qty": 10,
                "close": 70000,
                "defense_line": 72000,
            }
        ],
        "candidates": [
            {
                "ticker": "000660",
                "name": "SK하이닉스",
                "status": "ok",
                "close": 140000,
                "stop": 130000,
                "qty": 3,
            }
        ],
        "risk": {"breadth_pct_above_ma50": 13.0},
    }


def test_title_has_codes_and_names():
    title, body = build_message(_payload(), Config(ma_window=50, breadth_min_pct=30))
    assert "005930" in title and "삼성전자" in title
    assert "000660" in title and "SK하이닉스" in title
    assert "손절" in body and "50일선" not in body.split("시장폭")[0] or "손절" in body
    assert "진입 140000" in body
    assert "1R 3주" in body
    assert "절반" in body
    assert "시장폭 13.0%" in body
    assert_plain_korean(title)
    assert_plain_korean(body)


def test_kospi_fallback_and_no_forbidden_marks():
    title, body = build_message(
        {
            "demo": False,
            "trade_date": "2026-11-02",
            "holdings": [],
            "candidates": [],
            "risk": {"breadth_pct_above_ma50": 13.0},
        },
        Config(),
    )
    assert title == "[TREND] KOSPI 추세점검 11/02: 후보 0, 시장폭 13.0%"
    assert "KOSPI" in title
    assert "005930" not in title
    assert_plain_korean(title + body)
    for mark in ("·", "・", "—", "–"):
        assert mark not in title
        assert mark not in body
    delay_title, delay_body = delay_message("2026-11-02", "마지막 봉 2026-10-30")
    assert "KOSPI" in delay_title
    assert_plain_korean(delay_title + delay_body)
