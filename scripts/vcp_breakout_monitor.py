#!/usr/bin/env python3
"""[VCP] 관심 종목 돌파 알림 (주간 VCP 점검에서 "관심 유지"로 남은 종목).

Weekday 15:40 KST on the existing 0090b0 slot (port-alerts.yml, cron 40 6 * * 1-5 + cron-job.org
on-time dispatch, slot_guard once per KST day). No new cron.

Watchlist: configs/vcp_breakout.json "watchlist" (empty = log only). The Sunday review adds entries
with a config-only commit. Each entry expires after rules.expire_trading_days (default 60) KRX trading
days from added_kst, or its own "expire_trading_days". Expired entries are recorded in state and skipped
quietly; the config is not changed by this script.

Breakout (same as the screener's is_breakout): today's regular-session close > highest High of the prior
20 sessions (pivot) AND today's volume >= 1.5 x the prior 50-session average volume.
Close source: Daum days via scripts/krx_close.py, split adjusted with prevClosingPrice (same as
vcp_monitor.py); volume is adjusted by the same factor. Today's Daum row is cross-checked with the Naver
15:30 minute bar (regular session incl. the closing auction); on a mismatch the minute bar wins.
Checked 2026-11-03: Daum tradePrice and accTradeVolume equal the 09:00~15:30 minute bars (5 stocks,
11/02 and 11/03), so after-hours trades are not in Daum's daily row.

One send per stock per breakout, then a cooldown of rules.cooldown_trading_days (20) in state.
Non-KRX trading days: silent. Fetch failure for a watchlist stock: exit 1 (workflow "Notify ops on failure").

  python scripts/vcp_breakout_monitor.py auto
  python scripts/vcp_breakout_monitor.py dry [--watch 098120:마이크로컨텍솔:KOSDAQ,089860] [--asof YYYY-MM-DD]   # no ntfy, no state
  python scripts/vcp_breakout_monitor.py selftest
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kr_calendar  # noqa: E402
import krx_close  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NAME = "vcp_breakout"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
KST = timezone(timedelta(hours=9))
FORBIDDEN = ("\u00b7", "\u2014")
BARS = 260


def trading_days_between(a: date, b: date) -> int:
    """Number of KRX trading days d with a < d <= b."""
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if kr_calendar.is_trading_day(d):
            n += 1
    return n


def add_trading_days(a: date, n: int) -> date:
    d = a
    while n > 0:
        d += timedelta(days=1)
        if kr_calendar.is_trading_day(d):
            n -= 1
    return d


def fetch_adjusted(code: str):
    """Daum days -> adjusted OHLCV DataFrame (oldest first), volume adjusted by the split factor."""
    import pandas as pd
    import vcp_monitor as V
    url = (f"https://finance.daum.net/api/quote/A{code}/days"
           f"?symbolCode=A{code}&page=1&perPage={BARS}&pagination=true")
    raw = json.loads(krx_close._get(url, {"Referer": f"https://finance.daum.net/quotes/A{code}"}).decode("utf-8", "replace"))
    d = V.adjust_daum(raw, code)
    rawc = {str(r.get("date") or "")[:10]: float(r.get("tradePrice") or 0) for r in raw.get("data") or []}
    rc = pd.Series({pd.Timestamp(k): v for k, v in rawc.items() if v}).reindex(d.index)
    d["Volume"] = d["Volume"] / (d["Close"] / rc)
    return d


def check_today(code: str, d, day: str):
    """Cross-check / fill today's bar with the Naver 15:30 minute bar. Returns (df, note)."""
    import pandas as pd
    mb = krx_close._minute_bar(code, day)
    ts = pd.Timestamp(day)
    if ts in d.index:
        if mb is None:
            return d, "daum (분봉 대조 없음)"
        c, v = float(d.at[ts, "Close"]), float(d.at[ts, "Volume"])
        if abs(c - mb.close) < 0.5 and (mb.volume or 0) <= v * 1.01:
            return d, "daum = 15:30 분봉"
        print(f"vcp_breakout {code} {day} daum c={c:g} v={v:g} vs minute c={mb.close:g} v={mb.volume:g}; minute wins", file=sys.stderr)
        d = d.copy()
        d.loc[ts, ["Open", "High", "Low", "Close", "Volume"]] = [mb.open, mb.high, mb.low, mb.close, mb.volume]
        return d, "15:30 분봉 (Daum 미확정)"
    if mb is None:
        return d, None
    row = pd.DataFrame({"Open": [mb.open], "High": [mb.high], "Low": [mb.low], "Close": [mb.close],
                        "Volume": [mb.volume]}, index=[ts])
    return pd.concat([d, row]).sort_index(), "15:30 분봉 (Daum 행 없음)"


def evaluate(d, rules: dict) -> dict:
    c = d["Close"]
    close = float(c.iloc[-1])
    n_hi, n_v = int(rules["pivot_days"]), int(rules["vol_avg_days"])
    pivot = float(d["High"].iloc[-(n_hi + 1):-1].max())
    v50 = float(d["Volume"].iloc[-(n_v + 1):-1].mean())
    vol = float(d["Volume"].iloc[-1])
    ratio = vol / v50 if v50 > 0 else float("nan")
    mas = {n: (float(c.rolling(n).mean().iloc[-1]) if len(c) >= n else None) for n in (50, 150, 200)}
    # close - pivot >= 0.5 won: the adjusted series carries float noise (an equal close must not count as a break)
    return dict(close=close, pivot=pivot, vol=vol, v50=v50, ratio=ratio, above=(close / pivot - 1) * 100,
                mas=mas, breakout=bool(close - pivot >= 0.5 and vol >= float(rules["vol_mult"]) * v50))


def won(x: float) -> str:
    return f"{x:,.0f}"


def build_message(w: dict, day: str, ev: dict, expiry: date, cooldown: int) -> tuple[str, str]:
    title = f"[VCP] {w['code']} {w['name']} 돌파"
    ma_txt = []
    for n in (50, 150, 200):
        m = ev["mas"][n]
        ma_txt.append(f"{n}일선 {won(m)} ({(ev['close'] / m - 1) * 100:+.1f}%, {'위' if ev['close'] > m else '아래'})" if m else f"{n}일선 자료 부족")
    lines = [
        f"이게 뭐 알림: 주간 VCP 점검에서 관심 유지로 남은 {w['name']}({w['code']}, {w.get('market', '')})가 거래량을 동반해 직전 20일 고가를 넘었습니다.",
        "관심 목록 돌파일 뿐 매수 지시가 아닙니다. 며칠 안에 종가가 피벗 아래로 돌아오면 실패 경고입니다.",
        f"지금 결과/상태: {day} 정규장 종가 {won(ev['close'])}원, 돌파한 직전 20일 고가(피벗) {won(ev['pivot'])}원, 피벗 대비 {ev['above']:+.1f}%, "
        f"거래량 {won(ev['vol'])}주로 50일 평균의 {ev['ratio']:.2f}배.",
        "이동평균: " + ", ".join(ma_txt) + ".",
        f"지금 할 일: 차트와 뉴스를 확인하세요. 종가가 피벗 {won(ev['pivot'])}원 아래로 다시 내려가는지 며칠 지켜보세요.",
        f"나중에: 같은 종목은 {cooldown}거래일 동안 다시 알리지 않습니다. 관심 목록 만료 {expiry:%Y-%m-%d}.",
        "익절/줄일 때: 해당 없음 (관심 목록 알림).",
        "하지 말 것: 알림만 보고 바로 매수하지 마세요. 피벗 아래 종가가 나오면 돌파 실패로 봅니다.",
    ]
    return title, "\n".join(lines)


def load_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def run(mode: str, watch_override: list[dict] | None = None, asof: str | None = None) -> int:
    cfg = load_json(CFG_PATH)
    rules = cfg["rules"]
    state = load_json(STATE_PATH) if mode == "auto" else json.loads(json.dumps(load_json(STATE_PATH)))
    state.setdefault("stocks", {})
    state.setdefault("expired", {})
    watch = watch_override if watch_override is not None else list(cfg.get("watchlist") or [])
    now = datetime.now(KST)
    if asof:
        day = date.fromisoformat(asof)
    else:
        day = kr_calendar.session_day(now)
        if day == now.date() and (now.hour, now.minute) < krx_close.READY:
            print("vcp_breakout: before 15:31 KST; today's close not final, silent")
            return 0
    if not kr_calendar.is_trading_day(day):
        print(f"vcp_breakout: {day} is not a KRX trading day; silent")
        return 0
    if not watch:
        print("vcp_breakout: watchlist empty; silent")
        return 0
    from ntfy_util import publish
    sday = day.isoformat()
    failed, sent = [], 0
    for w in watch:
        code = str(w["code"]).zfill(6)
        w = {**w, "code": code}
        try:
            added = date.fromisoformat(str(w.get("added_kst") or sday)[:10])
        except ValueError:
            added = day
        exp_n = int(w.get("expire_trading_days") or rules["expire_trading_days"])
        expiry = add_trading_days(added, exp_n)
        if day > expiry:
            if code not in state["expired"]:
                state["expired"][code] = {"added_kst": str(w.get("added_kst")), "expired_on": expiry.isoformat()}
            print(f"vcp_breakout {code}: expired {expiry} (added {added}); skip")
            continue
        try:
            d = fetch_adjusted(code)
            d, note = check_today(code, d[d.index <= sday], sday)
        except Exception as exc:  # noqa: BLE001
            print(f"vcp_breakout {code}: fetch failed {type(exc).__name__}: {str(exc)[:120]}", file=sys.stderr)
            failed.append(code)
            continue
        if note is None or d.index[-1].strftime("%Y-%m-%d") != sday:
            print(f"vcp_breakout {code}: no regular-session bar for {sday}; skip", file=sys.stderr)
            failed.append(code)
            continue
        if len(d) < int(rules["vol_avg_days"]) + 1:
            print(f"vcp_breakout {code}: history too short ({len(d)} bars); skip")
            continue
        ev = evaluate(d, rules)
        print(f"vcp_breakout {code} {w.get('name', '')} {sday} close {won(ev['close'])} pivot {won(ev['pivot'])} "
              f"({ev['above']:+.1f}%) vol x{ev['ratio']:.2f} breakout={ev['breakout']} [{note}]")
        if not ev["breakout"]:
            continue
        st = state["stocks"].get(code) or {}
        last = st.get("last_breakout_day")
        cd = int(rules["cooldown_trading_days"])
        if last and (last == sday or trading_days_between(date.fromisoformat(last), day) < cd):
            print(f"vcp_breakout {code}: breakout but in cooldown (last {last}); silent")
            continue
        title, body = build_message(w, sday, ev, expiry, cd)
        if any(ch in title + body for ch in FORBIDDEN):
            raise RuntimeError("금지 문자(가운뎃점, 엠대시) 포함")
        if mode == "dry":
            print("DRY RUN (no ntfy, no state)\nTITLE:", title)
            print(body)
        else:
            publish(cfg.get("ntfy_topic", "cwally-port"), title, body, priority=3, tags=["rocket"],
                    server=cfg.get("ntfy_server"), config_name=NAME)
            sent += 1
        state["stocks"][code] = {"last_breakout_day": sday, "close": ev["close"], "pivot": ev["pivot"],
                                 "vol_ratio": round(ev["ratio"], 2)}
    if mode == "auto":
        state["last_run_day"] = sday
        state["updated_at"] = now.isoformat()
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"vcp_breakout done {sday}: watch {len(watch)}, sent {sent}, failed {failed}")
    if failed:
        print(f"vcp_breakout: 조회 실패 {failed}", file=sys.stderr)
        return 1
    return 0


def selftest() -> int:
    import numpy as np
    import pandas as pd
    fails = []

    def check(label, got, want):
        ok = got == want
        print(("PASS " if ok else "FAIL ") + label + ("" if ok else f" got={got!r} want={want!r}"))
        if not ok:
            fails.append(label)

    rules = load_json(CFG_PATH)["rules"]
    idx = pd.bdate_range("2026-01-01", periods=230)
    close = np.concatenate([np.linspace(100, 150, 200), np.full(29, 145.0), [152.0]])
    vol = np.concatenate([np.full(229, 1000.0), [1600.0]])
    d = pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99, "Close": close, "Volume": vol}, index=idx)
    ev = evaluate(d, rules)
    check("breakout close > prior 20d high and vol 1.6x", ev["breakout"], True)
    check("pivot = prior 20d high (146.45)", round(ev["pivot"], 2), 146.45)
    d2 = d.copy(); d2.iloc[-1, d2.columns.get_loc("Volume")] = 1400.0
    check("vol 1.4x no breakout", evaluate(d2, rules)["breakout"], False)
    d4 = d.copy(); d4.iloc[-1, d4.columns.get_loc("Close")] = float(d4["High"].iloc[-21:-1].max()) + 1e-9
    check("close equal to pivot (float noise) no breakout", evaluate(d4, rules)["breakout"], False)
    d3 = d.copy(); d3.iloc[-1, d3.columns.get_loc("Close")] = 146.0
    check("close below pivot no breakout", evaluate(d3, rules)["breakout"], False)
    check("trading days 11/03 -> 11/10", trading_days_between(date(2026, 11, 3), date(2026, 11, 10)), 5)
    check("60 trading days after 2026-11-03", add_trading_days(date(2026, 11, 3), 60).isoformat(), "2027-01-29")
    w = {"code": "098120", "name": "마이크로컨텍솔", "market": "KOSDAQ"}
    t, b = build_message(w, "2026-11-10", ev, date(2027, 1, 29), 20)
    check("title", t, "[VCP] 098120 마이크로컨텍솔 돌파")
    check("no dot/dash", any(ch in t + b for ch in FORBIDDEN), False)
    order = ["이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"]
    check("body order", [b.index(k) for k in order] == sorted(b.index(k) for k in order), True)
    check("watchlist line", "매수 지시가 아닙니다" in b and "피벗 아래" in b, True)
    check("body size < 3000 bytes", len(b.encode()) < 3000, True)
    payload = {"data": [
        {"symbolCode": "A010120", "date": "2026-04-13 00:00:00", "tradePrice": 179200, "prevClosingPrice": 157600,
         "openingPrice": 171200, "highPrice": 188300, "lowPrice": 171200, "accTradeVolume": 2500000},
        {"symbolCode": "A010120", "date": "2026-04-09 00:00:00", "tradePrice": 788000, "prevClosingPrice": 780000,
         "openingPrice": 780000, "highPrice": 790000, "lowPrice": 770000, "accTradeVolume": 100000}]}
    import vcp_monitor as V
    a = V.adjust_daum(payload, "010120")
    rc = pd.Series([788000.0, 179200.0], index=a.index)
    check("split volume x5", round(float((a["Volume"] / (a["Close"] / rc)).iloc[0])), 500000)
    # run(): send once, then cooldown; expired entry skipped quietly; empty watchlist silent; state only in auto
    import tempfile
    from unittest import mock
    import ntfy_util
    global STATE_PATH
    keep = STATE_PATH
    sent = []
    days = pd.bdate_range(end="2026-11-10", periods=230)
    dd = d.copy(); dd.index = days
    with tempfile.TemporaryDirectory() as tmp:
        STATE_PATH = Path(tmp) / "s.json"
        STATE_PATH.write_text("{}", encoding="utf-8")
        wl = [{"code": "098120", "name": "마이크로컨텍솔", "market": "KOSDAQ", "added_kst": "2026-11-08", "source": "t"},
              {"code": "089860", "name": "롯데렌탈", "market": "KOSPI", "added_kst": "2026-06-01", "source": "t"}]
        with mock.patch.object(sys.modules[__name__], "fetch_adjusted", lambda c: dd), \
             mock.patch.object(sys.modules[__name__], "check_today", lambda c, x, day: (x, "t")), \
             mock.patch.object(ntfy_util, "publish", lambda *a, **k: sent.append(a[1])):
            rc1 = run("auto", wl, "2026-11-10")
            rc2 = run("auto", wl, "2026-11-10")
            dd2 = dd.copy(); dd2.index = pd.bdate_range(end="2026-11-17", periods=230)
            with mock.patch.object(sys.modules[__name__], "fetch_adjusted", lambda c: dd2):
                rc3 = run("auto", wl, "2026-11-17")
            rc4 = run("auto", [], "2026-11-10")
        st = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    STATE_PATH = keep
    check("send once, same-day rerun and cooldown silent", (sent, rc1, rc2, rc3, rc4), (["[VCP] 098120 마이크로컨텍솔 돌파"], 0, 0, 0, 0))
    check("expired entry recorded in state only", sorted(st["expired"]), ["089860"])
    check("cooldown state", st["stocks"]["098120"]["last_breakout_day"], "2026-11-10")
    print("selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


def main() -> int:
    args = sys.argv[1:]
    mode = (args[0] if args else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry"}:
        print(__doc__)
        return 2
    watch, asof = None, None
    if mode == "dry":
        if "--watch" in args:
            watch = []
            for item in args[args.index("--watch") + 1].split(","):
                parts = (item.strip().split(":") + ["", ""])[:3]
                if parts[0]:
                    watch.append({"code": parts[0], "name": parts[1], "market": parts[2], "added_kst": None, "source": "dry"})
        if "--asof" in args:
            asof = args[args.index("--asof") + 1]
    return run(mode, watch, asof)


if __name__ == "__main__":
    sys.exit(main())
