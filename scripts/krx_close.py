#!/usr/bin/env python3
"""KRX regular-session daily bars (15:30 close).

Naver daily candles mix NXT and 시간외 into that day's close. The gap is still
there the next afternoon, so "after 18:00" does not make the candle official.
Checked 2026-11-03: 031980 15:30 bar and Daum days tradePrice were 183,000 on
11/02 and 165,400 on 11/03, while fchart / siseJson / m.stock .../price close
were 183,900 and 167,400.

Primary: Daum finance quote days (tradePrice is the regular close even while
the quote endpoint's tradePrice is the after-hours print).
Fallback: Naver minute chart, the 15:30 bar. Minute history is short, so a
long moving average is left short instead of filled with the contaminated daily
candle. Failures are logged. A Naver daily gap on the latest day is logged and
never used as the close.

  python scripts/krx_close.py 031980 005930 000660
  python scripts/krx_close.py selftest
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (cwally-alerts; +mailto:sosmos3218@gmail.com)"}
CODE_RE = re.compile(r"^[0-9A-Z]{6}$")
READY = (15, 31)  # regular session ends 15:30; the 15:30 bar is the close


class Bar:
    __slots__ = ("day", "open", "high", "low", "close", "volume", "source")

    def __init__(self, day: str, open_: float, high: float, low: float, close: float,
                 volume: float | None, source: str):
        self.day = day
        self.open = open_
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume
        self.source = source

    def __repr__(self) -> str:
        return f"Bar({self.day} c={self.close} src={self.source})"


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _get(url: str, headers: dict | None = None, timeout: int = 20) -> bytes:
    last: Exception | None = None
    for i in range(3):
        try:
            h = dict(UA)
            if headers:
                h.update(headers)
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i < 2:
                time.sleep(0.4 * (i + 1))
    raise last  # type: ignore[misc]


def _check_code(code: str) -> str:
    code = str(code or "").strip()
    if not CODE_RE.fullmatch(code):
        raise ValueError(f"종목코드 형식 아님: {code!r}")
    return code


def bars_from_daum(code: str, payload) -> list[Bar]:
    """Oldest-first bars from one Daum days payload. Raises on a code mismatch."""
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("Daum 일별 형식 아님")
    by: dict[str, Bar] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        sym = str(r.get("symbolCode") or "")
        if sym and sym != "A" + code:
            raise RuntimeError(f"Daum 종목 불일치 {sym}")
        day = str(r.get("date") or "")[:10]
        close = r.get("tradePrice")
        if len(day) != 10 or close is None:
            continue
        vol = r.get("accTradeVolume")
        by[day] = Bar(
            day, float(r.get("openingPrice") or close), float(r.get("highPrice") or close),
            float(r.get("lowPrice") or close), float(close),
            None if vol is None else float(vol), "daum",
        )
    return [by[d] for d in sorted(by)]


def _daum_bars(code: str, n: int) -> list[Bar]:
    per = 100
    pages = max(1, (n + per - 1) // per)
    by: dict[str, Bar] = {}
    for page in range(1, pages + 1):
        url = (f"https://finance.daum.net/api/quote/A{code}/days"
               f"?symbolCode=A{code}&page={page}&perPage={per}&pagination=true")
        raw = json.loads(_get(url, {"Referer": f"https://finance.daum.net/quotes/A{code}"}).decode("utf-8", "replace"))
        got = bars_from_daum(code, raw)
        if not got:
            break
        for b in got:
            by[b.day] = b
        if len(got) < per:
            break
    if not by:
        raise RuntimeError(f"{code} Daum 일별 없음")
    return [by[d] for d in sorted(by)]


def _minute_bar(code: str, day: str) -> Bar | None:
    """Regular-session OHLC from minute bars. Close is the 15:30 bar."""
    ymd = day.replace("-", "")
    url = (f"https://api.stock.naver.com/chart/domestic/item/{code}/minute"
           f"?startDateTime={ymd}0900&endDateTime={ymd}1531")
    try:
        rows = json.loads(_get(url).decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        _log(f"krx_close {code} minute {day} failed ({type(exc).__name__})")
        return None
    if not isinstance(rows, list):
        return None
    reg = [r for r in rows if str(r.get("localDateTime") or "").startswith(ymd)
           and str(r.get("localDateTime") or "") <= ymd + "153000"]
    close_r = next((r for r in reg if str(r.get("localDateTime")) == ymd + "153000"), None)
    if not close_r or close_r.get("currentPrice") is None:
        return None
    try:
        o = float(reg[0]["openPrice"])
        h = max(float(r["highPrice"]) for r in reg)
        lo = min(float(r["lowPrice"]) for r in reg)
        c = float(close_r["currentPrice"])
        v = sum(float(r.get("accumulatedTradingVolume") or 0) for r in reg)
    except (TypeError, ValueError, KeyError):
        return None
    return Bar(day, o, h, lo, c, v, "minute1530")


def minute_bars(code: str, n: int = 8, now: datetime | None = None) -> list[Bar]:
    """Up to n recent regular-session bars from the 15:30 minute bar. Oldest first."""
    import kr_calendar  # local import: calendar has no dependency back here

    code = _check_code(code)
    now = now.astimezone(KST) if now and now.tzinfo else (now or datetime.now(KST))
    d = now.date()
    if (now.hour, now.minute) < READY:
        d -= timedelta(days=1)
    out: list[Bar] = []
    guard = 0
    while len(out) < n and guard < n * 4 + 12:
        guard += 1
        if kr_calendar.is_trading_day(d):
            bar = _minute_bar(code, d.isoformat())
            if bar:
                out.append(bar)
            elif out:
                break
        d -= timedelta(days=1)
        if d.year < now.year - 1:
            break
    out.reverse()
    return out


def naver_daily_closes(code: str, n: int = 6) -> dict[str, float]:
    """Naver m.stock daily closePrice. For the gap log only. Not a regular close."""
    code = _check_code(code)
    raw = json.loads(_get(f"https://m.stock.naver.com/api/stock/{code}/price?pageSize={n}&page=1").decode("utf-8", "replace"))
    out = {}
    for r in raw if isinstance(raw, list) else []:
        day = str(r.get("localTradedAt") or "")[:10]
        try:
            c = float(str(r.get("closePrice")).replace(",", ""))
        except (TypeError, ValueError):
            continue
        if len(day) == 10:
            out[day] = c
    return out


def _log_gap(code: str, bar: Bar) -> None:
    try:
        nv = naver_daily_closes(code, 4).get(bar.day)
    except Exception as exc:  # noqa: BLE001
        _log(f"krx_close {code} naver compare skipped ({type(exc).__name__})")
        return
    if nv is None:
        _log(f"krx_close {code} {bar.day} naver_daily missing")
        return
    if abs(nv - bar.close) >= 0.5:
        _log(f"krx_close {code} {bar.day} naver_daily={nv:g} regular={bar.close:g} gap={nv - bar.close:+.0f}")


def regular_bars(code: str, n: int = 30, *, compare: bool = True) -> list[Bar]:
    """Oldest-first official regular-session bars. Today's row is included.

    Before 15:31 that row is still the session in progress. Callers that need
    a finished close should pass the result through completed().
    """
    code = _check_code(code)
    n = int(n)
    if n < 1:
        raise ValueError("n >= 1")
    source = "daum"
    try:
        bars = _daum_bars(code, n)
    except Exception as exc:  # noqa: BLE001
        _log(f"krx_close {code} daum failed ({type(exc).__name__}: {exc}); minute 15:30 fallback")
        bars = minute_bars(code, min(n, 8))
        source = "minute1530"
        if not bars:
            raise RuntimeError(f"{code} 정규장 종가 없음 (Daum 실패, 15:30 분봉도 없음)") from exc
        if len(bars) < n:
            _log(f"krx_close {code} minute fallback {len(bars)}/{n} (짧은 이력, 오염된 일봉으로 채우지 않음)")
    bars = bars[-n:]
    last = bars[-1]
    _log(f"krx_close {code} source={source} n={len(bars)} latest={last.day} close={last.close:g}")
    if compare and source == "daum":
        _log_gap(code, last)
    return bars


def completed(bars: list[Bar], now: datetime, ready: tuple[int, int] = READY) -> list[Bar]:
    """Drop today's bar until ready (default 15:31 KST). `now` is KST wall time."""
    if now.tzinfo:
        now = now.astimezone(KST)
    today = now.strftime("%Y-%m-%d")
    late = (now.hour, now.minute) >= tuple(ready)
    return [b for b in bars if b.day < today or (b.day == today and late)]


def session_closes(code: str, n: int = 30, now: datetime | None = None,
                   ready: tuple[int, int] = READY, *, compare: bool = True) -> list[tuple[str, float]]:
    """Completed (day, close), oldest first."""
    now = now.astimezone(KST) if now and now.tzinfo else (now or datetime.now(KST))
    return [(b.day, b.close) for b in completed(regular_bars(code, n, compare=compare), now, ready)]


def ymd(day: str) -> str:
    return day.replace("-", "")


def selftest() -> int:
    payload = {"data": [
        {"symbolCode": "A031980", "date": "2026-11-03 00:00:00", "tradePrice": 165400,
         "openingPrice": 175900, "highPrice": 176000, "lowPrice": 165000, "accTradeVolume": 193165},
        {"symbolCode": "A031980", "date": "2026-11-02 00:00:00", "tradePrice": 183000,
         "openingPrice": 184100, "highPrice": 187750, "lowPrice": 174000, "accTradeVolume": 175633},
    ]}
    bars = bars_from_daum("031980", payload)
    assert [b.day for b in bars] == ["2026-11-02", "2026-11-03"], bars
    assert bars[-1].close == 165400 and bars[-1].open == 175900
    assert bars[-1].source == "daum"
    early = datetime(2026, 11, 3, 15, 20, tzinfo=KST)
    late = datetime(2026, 11, 3, 15, 31, tzinfo=KST)
    assert [b.day for b in completed(bars, early)] == ["2026-11-02"]
    assert [b.day for b in completed(bars, late)] == ["2026-11-02", "2026-11-03"]
    try:
        bars_from_daum("005930", payload)
        raise AssertionError("mismatch should raise")
    except RuntimeError as exc:
        assert "불일치" in str(exc)
    try:
        _check_code("123")
        raise AssertionError("bad code should raise")
    except ValueError:
        pass
    print("krx_close selftest ok")
    return 0


def _live(codes: list[str]) -> int:
    """Print regular close vs Naver daily vs the 15:30 bar for a few names."""
    for code in codes:
        bars = regular_bars(code, 4, compare=False)
        nv = {}
        try:
            nv = naver_daily_closes(code, 4)
        except Exception as exc:  # noqa: BLE001
            _log(f"{code} naver daily failed ({type(exc).__name__})")
        print(f"== {code} source={bars[-1].source}")
        for b in bars[-3:]:
            minute = _minute_bar(code, b.day)
            mc = None if minute is None else minute.close
            nv_c = nv.get(b.day)
            gap = None if nv_c is None else nv_c - b.close
            same = "" if mc is None else (" minute=ok" if abs(mc - b.close) < 0.5 else f" minute={mc:g} DIFF")
            print(f"  {b.day} regular={b.close:g} naver_daily={nv_c} gap={gap}{same}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        raise SystemExit(selftest())
    raise SystemExit(_live(sys.argv[1:] or ["031980", "005930", "000660", "068270"]))
