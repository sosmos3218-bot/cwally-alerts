import json

from trend_monitor.config import Config
from trend_monitor.ntfy_summary import (
    maybe_publish_recovery,
    previous_breadth,
    recovery_message,
)


def _payload(day, pct, demo=False):
    return {"demo": demo, "trade_date": day, "risk": {"breadth_pct_above_ma50": pct}}


def _write(folder, day, pct, demo=False):
    (folder / f"{day}.json").write_text(json.dumps(_payload(day, pct, demo)), encoding="utf-8")


CFG = Config(breadth_min_pct=30, breadth_top_n=200, breadth_ma_window=50)


def test_previous_breadth_picks_latest_earlier_real_file(tmp_path):
    _write(tmp_path, "2026-10-29", 24.0)
    _write(tmp_path, "2026-10-30", 16.0)
    _write(tmp_path, "2026-11-02", 13.0)
    _write(tmp_path, "2026-11-03", 99.0, demo=True)
    (tmp_path / "latest.json").write_text("{}", encoding="utf-8")
    assert previous_breadth("2026-11-03", tmp_path) == ("2026-11-02", 13.0)
    assert previous_breadth("2026-11-02", tmp_path) == ("2026-10-30", 16.0)
    assert previous_breadth("2026-10-29", tmp_path) is None


def test_previous_breadth_skips_demo_and_broken(tmp_path):
    _write(tmp_path, "2026-10-30", 20.0)
    _write(tmp_path, "2026-11-02", 50.0, demo=True)
    (tmp_path / "2026-11-01.json").write_text("not json", encoding="utf-8")
    assert previous_breadth("2026-11-03", tmp_path) == ("2026-10-30", 20.0)


def test_crossing_up_sends_once():
    msg = recovery_message(_payload("2026-11-04", 31.5), CFG, ("2026-11-03", 28.0))
    assert msg is not None
    title, body = msg
    assert title.startswith("[TREND] 시장폭 30% 회복 11/04")
    assert "31.5%" in title and "28.0%" in title
    assert "꺼지는 때:" in body
    assert "·" not in title + body and "—" not in title + body


def test_no_message_when_already_above_or_still_below_or_missing():
    assert recovery_message(_payload("d", 35.0), CFG, ("p", 31.0)) is None
    assert recovery_message(_payload("d", 29.9), CFG, ("p", 20.0)) is None
    assert recovery_message(_payload("d", None), CFG, ("p", 20.0)) is None
    assert recovery_message(_payload("d", 40.0), CFG, None) is None
    assert recovery_message(_payload("d", 40.0, demo=True), CFG, ("p", 10.0)) is None


def test_exactly_at_limit_counts_as_recovery():
    assert recovery_message(_payload("2026-11-04", 30.0), CFG, ("2026-11-03", 29.99)) is not None


def test_publish_respects_allow_send_and_sender(tmp_path):
    _write(tmp_path, "2026-11-03", 20.0)
    payload = _payload("2026-11-04", 33.0)
    dry = maybe_publish_recovery(payload, CFG, tmp_path, allow_send=False)
    assert dry and dry["sent"] is False
    calls = []
    sent = maybe_publish_recovery(
        payload, CFG, tmp_path, allow_send=True, sender=lambda *a: calls.append(a)
    )
    assert sent["sent"] is True and len(calls) == 1 and calls[0][0] == "cwally-port"
    quiet = _payload("2026-11-04", 12.0)
    assert maybe_publish_recovery(quiet, CFG, tmp_path, allow_send=True, sender=lambda *a: calls.append(a)) is None
    assert len(calls) == 1
