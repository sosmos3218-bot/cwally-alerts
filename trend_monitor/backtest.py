"""매도 규칙과 스크리너를 과거 일봉에 다시 돌립니다.

신호일 종가까지로만 판단하고, 체결은 다음 거래일 시가입니다.
현재 상장 종목만 쓰므로 상장폐지 종목이 빠진 생존 편향이 있습니다.
수수료, 세금, 슬리피지는 넣지 않습니다.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from trend_monitor import data as data_mod
from trend_monitor.config import ROOT, apply_cli, load_config
from trend_monitor.errors import TrendError
from trend_monitor.rules import DayView, PositionView, evaluate_exit
from trend_monitor.screener import prepare, screen_at
from trend_monitor.sizing import size_order

SUMMARY_PATH = ROOT / "output" / "backtest-summary.json"


def _price_on(frame: pd.DataFrame, day: date):
    ts = pd.Timestamp(day)
    if ts not in frame.index:
        return None
    return frame.loc[ts]


def collect(frames: dict[str, pd.DataFrame], names: dict, markets: dict, cfg, start: date, end: date):
    ready = {}
    signals = []
    for n, (code, df) in enumerate(frames.items(), start=1):
        if len(df) < cfg.trend_mas[-1] + 5:
            continue
        prep = prepare(df, cfg)
        ready[code] = prep
        dates = prep.index
        close = prep["Close"].to_numpy(dtype=float)
        prior = prep["prior_high"].to_numpy(dtype=float)
        for i in range(len(prep)):
            if not (prior[i] == prior[i]) or close[i] < prior[i]:
                continue
            day = dates[i].date()
            if day < start or day > end:
                continue
            hit = screen_at(prep, i, cfg)
            if hit is None:
                continue
            item = dict(hit)
            item["ticker"] = code
            item["name"] = names.get(code, code)
            item["market"] = markets.get(code, "")
            item["date"] = day
            signals.append(item)
        if n % 200 == 0:
            print(f"신호 검색 {n}/{len(frames)} 누적 {len(signals)}", flush=True)
    print(f"신호 {len(signals)}건", flush=True)
    return ready, signals


class _Pos:
    def __init__(self, ticker, qty, entry, entry_date, stop, breakout, entry_atr, stop_mode):
        self.ticker = ticker
        self.qty = qty
        self.initial_qty = qty
        self.avg = entry
        self.entry_date = entry_date
        self.stop = stop
        self.breakout = breakout
        self.entry_atr = entry_atr
        self.stop_mode = stop_mode
        self.climax_taken = False
        self.max_high = entry
        self.realized = 0.0
        self.last_close = entry
        self.risk_per_share = max(0.0, entry - stop)


def _decision(pos: _Pos, bar, prep: pd.DataFrame, cfg):
    loc = prep.index.get_loc(pd.Timestamp(bar.name.date() if hasattr(bar, "name") else bar.name))
    # bar is a Series with .name timestamp
    return loc


def simulate(ready: dict, signals: list[dict], cfg, start: date, end: date) -> dict:
    by_day = defaultdict(list)
    for sig in signals:
        if start <= sig["date"] <= end:
            by_day[sig["date"]].append(sig)
    dates = []
    seen = set()
    for frame in ready.values():
        for ts in frame.index:
            day = ts.date()
            if start <= day <= end and day not in seen:
                seen.add(day)
                dates.append(day)
    dates.sort()
    cash = float(cfg.account_krw)
    account = float(cfg.account_krw)
    positions: list[_Pos] = []
    pending_entries: list[dict] = []
    pending_exits: list[tuple[_Pos, int, str]] = []
    closed = []
    equity = []

    def bar_of(ticker, day):
        frame = ready.get(ticker)
        if frame is None:
            return None
        ts = pd.Timestamp(day)
        if ts not in frame.index:
            return None
        return frame.loc[ts]

    def mark_price(pos: _Pos, day: date) -> float:
        bar = bar_of(pos.ticker, day)
        if bar is not None:
            return float(bar["Close"])
        return pos.last_close

    for day in dates:
        still = []
        for pos, qty, reason in pending_exits:
            bar = bar_of(pos.ticker, day)
            if bar is None or pos not in positions:
                still.append((pos, qty, reason))
                continue
            fill = float(bar["Open"])
            sell = min(qty, pos.qty)
            pos.realized += (fill - pos.avg) * sell
            cash += fill * sell
            pos.qty -= sell
            if reason == "climax_half":
                pos.climax_taken = True
            if pos.qty <= 0:
                risk = pos.risk_per_share * pos.initial_qty
                r_mult = pos.realized / risk if risk else 0.0
                closed.append(
                    {
                        "ticker": pos.ticker,
                        "entry_date": pos.entry_date.isoformat(),
                        "exit_date": day.isoformat(),
                        "pnl": round(pos.realized, 2),
                        "r": round(r_mult, 4),
                        "reason": reason,
                    }
                )
                positions.remove(pos)
        pending_exits = [(p, q, r) for p, q, r in still if p in positions]

        pending_entries.sort(key=lambda s: (s["distance_pct"], s["ticker"]))
        held = {p.ticker for p in positions}
        for sig in pending_entries:
            if sig["ticker"] in held:
                continue
            bar = bar_of(sig["ticker"], day)
            if bar is None:
                continue
            entry = float(bar["Open"])
            stop = int(sig["stop"])
            if entry <= stop:
                continue
            if (entry - sig["breakout_price"]) / sig["breakout_price"] * 100.0 > cfg.chase_max_distance_pct:
                continue
            mtm = 0.0
            for pos in positions:
                px = float(bar_of(pos.ticker, day)["Open"]) if bar_of(pos.ticker, day) is not None else pos.last_close
                floor = pos.stop
                if pos.climax_taken:
                    floor = max(floor, pos.avg)
                mtm += max(0.0, px - floor) * pos.qty
            room = max(0.0, account * cfg.max_total_open_risk_pct / 100.0 - mtm)
            qty, _note = size_order(
                entry=entry,
                stop=stop,
                account=account,
                risk_pct=cfg.risk_pct,
                max_weight_pct=cfg.max_position_weight_pct,
                max_stop_distance_pct=cfg.max_stop_distance_pct,
                cash=cash,
                risk_room=room,
            )
            if qty < 1:
                continue
            cash -= entry * qty
            positions.append(
                _Pos(sig["ticker"], qty, entry, day, stop, float(sig["breakout_price"]), float(sig["atr14"]), cfg.stop_mode)
            )
            held.add(sig["ticker"])
        pending_entries = []

        eq = cash
        for pos in list(positions):
            bar = bar_of(pos.ticker, day)
            if bar is None:
                eq += pos.qty * pos.last_close
                continue
            pos.last_close = float(bar["Close"])
            pos.max_high = max(pos.max_high, float(bar["High"]))
            eq += pos.qty * pos.last_close
            prep = ready[pos.ticker]
            loc = prep.index.get_loc(pd.Timestamp(day))
            if isinstance(loc, slice):
                continue
            loc = int(loc)
            if loc < 1:
                continue
            atr_prev = float(prep["atr14"].iloc[loc - 1])
            ma = float(prep[f"ma{cfg.ma_window}"].iloc[loc])
            if ma != ma:
                continue
            decision = evaluate_exit(
                PositionView(
                    ticker=pos.ticker,
                    name=pos.ticker,
                    avg_price=pos.avg,
                    remaining_qty=pos.qty,
                    breakout_price=pos.breakout,
                    entry_atr=pos.entry_atr,
                    stop_mode=pos.stop_mode,
                    climax_taken=pos.climax_taken,
                ),
                DayView(
                    open=float(bar["Open"]),
                    high=float(bar["High"]),
                    low=float(bar["Low"]),
                    close=float(bar["Close"]),
                    ma=ma,
                    atr_prev=atr_prev,
                    max_high_since_entry=pos.max_high,
                ),
                cfg.exit_params(),
                breadth_ok=None,
            )
            if decision["signal"] == "hold" or decision["sell_qty"] <= 0:
                continue
            pending_exits.append((pos, int(decision["sell_qty"]), decision["signal"]))
        equity.append(eq)
        for sig in by_day.get(day, []):
            pending_entries.append(sig)

    wins = [t for t in closed if t["pnl"] > 0]
    avg_r = sum(t["r"] for t in closed) / len(closed) if closed else 0.0
    peak = account
    max_dd = 0.0
    for eq in equity:
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak * 100.0)
    return {
        "trades": len(closed),
        "win_rate_pct": round(len(wins) / len(closed) * 100.0, 2) if closed else 0.0,
        "avg_r": round(avg_r, 4),
        "max_drawdown_pct": round(max_dd, 2),
        "open_positions": len(positions),
        "ending_equity": round(equity[-1], 2) if equity else account,
    }


def run_backtest(cfg, frames, names, markets, start: date, end: date) -> dict:
    ready, signals = collect(frames, names, markets, cfg, start, end)
    ma20 = simulate(ready, signals, cfg, start, end)
    ma50_cfg = cfg
    from dataclasses import replace

    ma50_cfg = replace(cfg, ma_window=50)
    ma50 = simulate(ready, signals, ma50_cfg, start, end)
    return {"ma20": ma20, "ma50": ma50, "signals": len(signals)}


def _parse_day(text: str | None, fallback: date) -> date:
    if not text:
        return fallback
    from trend_monitor.calendar_krx import parse_trade_date

    return parse_trade_date(text)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="추세 규칙 백테스트. 실데이터, 미래 봉 미사용.")
    p.add_argument("--end", default=None, help="마지막 기준일 YYYY-MM-DD 또는 YYYYMMDD")
    p.add_argument("--start", default=None, help="신호 시작일. 비우면 끝 날짜에서 약 2년 전")
    p.add_argument("--config", default=None)
    p.add_argument("--source", choices=("fdr", "pykrx"), default="fdr")
    p.add_argument("--workers", type=int, default=None)
    p.add_argument("--output", default=str(SUMMARY_PATH))
    args = p.parse_args(argv)
    try:
        from trend_monitor.calendar_krx import default_trade_date

        cfg = load_config(Path(args.config) if args.config else None)
        if args.workers:
            cfg = apply_cli(cfg, argparse.Namespace(workers=args.workers, account=None, risk_pct=None, max_weight=None, max_total_risk=None, cash=None, stop_mode=None, ma_window=None))
        end = _parse_day(args.end, default_trade_date())
        start = _parse_day(args.start, end - timedelta(days=730))
        hist = data_mod.history_start(end, cfg)
        print(f"백테스트 신호 {start.isoformat()} ~ {end.isoformat()}, 시세 시작 {hist.isoformat()}", flush=True)
        listing = data_mod.load_listing()
        universe = data_mod.filter_universe(listing, cfg)
        codes = universe["Code"].tolist()
        for ticker in cfg.reference_tickers:
            if ticker not in codes:
                codes.append(ticker)
        frames, failures = data_mod.download_many(codes, hist, end, args.source, cfg.workers)
        if codes and len(failures) / len(codes) > cfg.max_fetch_fail_ratio and len(failures) > 10:
            from trend_monitor.errors import TrendDataError

            raise TrendDataError(f"시세 실패가 많습니다: {len(failures)}/{len(codes)}. 가격을 만들지 않습니다.")
        names = {row.Code: str(row.Name) for row in universe.itertuples(index=False)}
        markets = {row.Code: data_mod.output_market(str(row.Market)) for row in universe.itertuples(index=False)}
        # 기준일 이후 봉이 섞이지 않게 자릅니다.
        trimmed = {}
        for code, df in frames.items():
            part = df[df.index <= pd.Timestamp(end)]
            if len(part):
                trimmed[code] = part
        result = run_backtest(cfg, trimmed, names, markets, start, end)
        summary = {
            "schema_version": 1,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "account_krw": cfg.account_krw,
            "universe": len(trimmed),
            "fetch_failures": len(failures),
            "signals": result["signals"],
            "survivorship": "현재 상장 종목만 사용했습니다. 상장폐지 종목은 빠져 있습니다.",
            "fill": "신호 다음 거래일 시가에 진입하고, 청산 신호 다음 거래일 시가에 팝니다.",
            "costs": "수수료, 세금, 슬리피지는 넣지 않았습니다.",
            "lookahead": "신호와 청산 판단은 그날 종가까지의 봉만 사용합니다.",
            "ma20": result["ma20"],
            "ma50": result["ma50"],
        }
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"ma20": summary["ma20"], "ma50": summary["ma50"], "signals": summary["signals"]}, ensure_ascii=False), flush=True)
        print(f"요약 {out}", flush=True)
        return 0
    except TrendError as exc:
        print(str(exc), file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
