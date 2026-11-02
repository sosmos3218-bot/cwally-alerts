from datetime import date

import pandas as pd
import pytest

from trend_monitor.config import Config
from trend_monitor.data import fetch_fdr_bars, fetch_pykrx_bars, filter_universe, is_preferred_name, normalize_ohlcv
from trend_monitor.errors import TrendDataError


def test_preferred_and_universe_filter():
    assert is_preferred_name("삼성전자우")
    assert is_preferred_name("현대차2우B")
    assert not is_preferred_name("데모돌파")
    assert not is_preferred_name("SK리츠")
    listing = pd.DataFrame(
        [
            {"Code": "900001", "Name": "데모돌파", "Market": "KOSPI", "Marcap": 10, "Amount": 10},
            {"Code": "5935", "Name": "삼성전자우", "Market": "KOSPI", "Marcap": 10, "Amount": 10},
            {"Code": "900002", "Name": "데모스팩", "Market": "KOSDAQ", "Marcap": 10, "Amount": 10},
            {"Code": "900003", "Name": "콘넥스", "Market": "KONEX", "Marcap": 10, "Amount": 10},
            {"Code": "900004", "Name": "작은종목", "Market": "KOSDAQ", "Marcap": 1, "Amount": 1},
        ]
    )
    cfg = Config(download_min_marcap_krw=5, download_min_amount_krw=0)
    out = filter_universe(listing, cfg)
    assert out["Code"].tolist() == ["900001"]


def test_empty_frame_fails_loud():
    with pytest.raises(TrendDataError, match="가짜"):
        normalize_ohlcv(pd.DataFrame(), "005930")


def test_fdr_empty_does_not_invent(monkeypatch):
    import FinanceDataReader as fdr

    monkeypatch.setattr(fdr, "DataReader", lambda *a, **k: pd.DataFrame())
    with pytest.raises(TrendDataError, match="가짜"):
        fetch_fdr_bars("005930", date(2026, 10, 1), date(2026, 11, 2))


def test_fdr_exception_does_not_invent(monkeypatch):
    import FinanceDataReader as fdr

    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(fdr, "DataReader", boom)
    with pytest.raises(TrendDataError, match="가짜"):
        fetch_fdr_bars("005930", date(2026, 10, 1), date(2026, 11, 2))


def test_pykrx_requires_env(monkeypatch):
    monkeypatch.delenv("KRX_ID", raising=False)
    monkeypatch.delenv("KRX_PW", raising=False)
    with pytest.raises(TrendDataError, match="KRX_ID"):
        fetch_pykrx_bars("005930", date(2026, 10, 1), date(2026, 11, 2))


def test_stale_reference(monkeypatch):
    idx = pd.bdate_range("2026-10-01", "2026-10-30")
    frame = pd.DataFrame(
        {"Open": 1, "High": 2, "Low": 1, "Close": 1, "Volume": 1},
        index=idx,
    )

    def fake_load(code, start, end, source="fdr", cache_dir=None):
        return frame

    monkeypatch.setattr("trend_monitor.engine.data_mod.load_bars", fake_load)
    from trend_monitor.engine import load_live

    with pytest.raises(TrendDataError, match="마지막 봉"):
        load_live(Config(), date(2026, 11, 2), "fdr", [])
