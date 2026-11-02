import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from trend_monitor.cli import main
from trend_monitor.config import Config, load_config
from trend_monitor.demo_data import DEMO_DAY, build_breakout, load_demo, sessions_ending, write_fixtures
from trend_monitor.engine import build_payload
from trend_monitor.errors import TrendDataError
from trend_monitor.indicators import atr_excluding_today, wilder_atr
from trend_monitor.ntfy_summary import build_title, maybe_publish
from trend_monitor.report import CANDIDATE_KEYS, HOLDING_KEYS, RISK_KEYS, TOP_KEYS, validate_payload
from trend_monitor.screener import screen_frame


def test_stop_mode_choices():
    # -7.5% 를 값으로 받지 않습니다. pct, atr 만 허용합니다.
    try:
        main(["--stop-mode", "-7.5%"])
        raised = False
    except SystemExit as exc:
        raised = exc.code != 0
    assert raised


def test_bad_date_and_partial_exit_codes(monkeypatch):
    assert main(["--date", "2026-11-01"]) == 2
    kst = ZoneInfo("Asia/Seoul")
    monkeypatch.setattr("trend_monitor.cli.now_kst", lambda clock=None: datetime(2026, 11, 2, 15, 0, tzinfo=kst))
    assert main(["--date", "2026-11-02"]) == 2


def test_missing_bars_exit_code(monkeypatch, capsys):
    def boom(*a, **k):
        raise TrendDataError("005930 시세가 비어 있습니다. 가짜 가격으로 대체하지 않습니다.")

    monkeypatch.setattr("trend_monitor.engine.data_mod.load_bars", boom)
    code = main(["--date", "20261102"])
    err = capsys.readouterr().err
    assert code == 1
    assert "가짜" in err


def test_breakout_fixture_and_atr_excludes_today():
    days = sessions_ending(DEMO_DAY, 280)
    frame = build_breakout(days)
    hit = screen_frame(frame, Config())
    assert hit is not None
    assert hit["base_days"] == 45
    assert hit["distance_pct"] < 5
    # 오늘 범위만 키우면 오늘을 뺀 ATR 은 그대로입니다.
    high = frame["High"].to_numpy(dtype=float).copy()
    low = frame["Low"].to_numpy(dtype=float).copy()
    close = frame["Close"].to_numpy(dtype=float)
    before = atr_excluding_today(high, low, close)
    high[-1] = high[-1] + 5000
    low[-1] = max(1, low[-1] - 5000)
    after = atr_excluding_today(high, low, close)
    assert before == after
    full = wilder_atr(high, low, close)
    assert full[-1] != full[-2]


def test_future_bar_does_not_change_past_signal():
    days = sessions_ending(DEMO_DAY, 280)
    frame = build_breakout(days)
    cfg = Config()
    hit = screen_frame(frame, cfg)
    assert hit is not None
    extra = frame.copy()
    future = pd.Timestamp("2026-11-02")
    extra.loc[future] = {"Open": 50000, "High": 80000, "Low": 50000, "Close": 80000, "Volume": 1_000_000}
    extra = extra.sort_index()
    from trend_monitor.screener import prepare, screen_at

    again = screen_at(prepare(extra, cfg), len(frame) - 1, cfg)
    assert again is not None
    assert again["breakout_price"] == hit["breakout_price"]
    assert again["base_days"] == hit["base_days"]
    assert again["distance_pct"] == hit["distance_pct"]


def test_demo_offline_schema_and_ntfy_disabled(tmp_path, monkeypatch, capsys):
    write_fixtures()
    def boom(*a, **k):
        raise AssertionError("network")

    monkeypatch.setattr("requests.get", boom)
    monkeypatch.setattr("requests.post", boom)
    code = main(["--demo", "--output-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "DEMO" in out
    payload = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert list(payload.keys()) == TOP_KEYS
    assert payload["demo"] is True
    assert payload["trade_date"] == DEMO_DAY.isoformat()
    validate_payload(payload)
    assert any(row["signal"] == "stop" for row in payload["holdings"])
    assert any(row["signal"] == "climax_half" for row in payload["holdings"])
    assert any(row["status"] == "ok" for row in payload["candidates"])
    text = (tmp_path / f"{DEMO_DAY.isoformat()}.md").read_text(encoding="utf-8")
    assert text.startswith("# DEMO")
    sent = []
    notice = maybe_publish(payload, load_config(), allow_send=False, sender=lambda *a: sent.append(a))
    assert notice["sent"] is False
    assert sent == []
    assert "900001" in notice["title"] or "KOSPI" in notice["title"]
    title = build_title({"holdings": [], "candidates": [], "demo": False, "trade_date": "2026-11-02"})
    assert title.endswith("KOSPI")
    assert title.startswith("[TREND]")


def test_apply_state_only_when_asked(tmp_path):
    write_fixtures()
    book = load_demo()
    cfg = load_config()
    state = tmp_path / "positions.json"
    payload = build_payload(
        cfg=cfg,
        target=book["trade_date"],
        frames=book["frames"],
        universe=book["universe"],
        holding_list=__import__("trend_monitor.holdings", fromlist=["load_holdings"]).load_holdings(book["holdings_path"], "pct"),
        warnings=[],
        source_label="demo-fixtures",
        demo=True,
        generated_at="2026-11-02T16:00:00+09:00",
        state_path=state,
        apply_state=False,
    )
    assert not state.exists()
    assert any(row["signal"] == "climax_half" for row in payload["holdings"])
    build_payload(
        cfg=cfg,
        target=book["trade_date"],
        frames=book["frames"],
        universe=book["universe"],
        holding_list=__import__("trend_monitor.holdings", fromlist=["load_holdings"]).load_holdings(book["holdings_path"], "pct"),
        warnings=[],
        source_label="demo-fixtures",
        demo=True,
        generated_at="2026-11-02T16:00:00+09:00",
        state_path=state,
        apply_state=True,
    )
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["applied"]["900012"]["climax_taken"] is True
    assert saved["applied"]["900012"]["remaining_qty"] == 5


def test_readme_has_no_forbidden_punctuation():
    text = Path("trend_monitor/README.md").read_text(encoding="utf-8")
    for bad in ("·", "・", "—", "–"):
        assert bad not in text


def test_output_columns_constant():
    assert "eps_yoy_pct" in CANDIDATE_KEYS
    assert "defense_line" in HOLDING_KEYS
    assert RISK_KEYS == ["total_open_risk_pct", "cash_after", "exposure_pct", "breadth_pct_above_ma50"]
