"""하루 점검 조립. 네트워크는 load_live 쪽에만 있고, 판단은 프레임을 받습니다."""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path

import pandas as pd

from trend_monitor import data as data_mod
from trend_monitor import holdings as holdings_mod
from trend_monitor import report as report_mod
from trend_monitor.breadth import market_breadth
from trend_monitor.config import ROOT, Config
from trend_monitor.dart import KEY_PRESENT_MESSAGE, MISSING_KEY_MESSAGE, DartClient, api_key_from_env
from trend_monitor.errors import TrendDataError
from trend_monitor.flow import FlowClient
from trend_monitor.indicators import wilder_atr
from trend_monitor.rules import DayView, PositionView, evaluate_exit
from trend_monitor.screener import prepare, screen_at
from trend_monitor.sizing import size_order
from trend_monitor.ticks import round_to_tick
from trend_monitor.watchlist import evaluate_watchlist

STATE_PATH = ROOT / "state" / "positions.json"
OUTPUT_DIR = ROOT / "output"


def _price(value) -> int | float:
    if isinstance(value, int):
        return value
    number = float(value)
    if math.isfinite(number) and abs(number - round(number)) < 1e-6:
        return int(round(number))
    return round(number, 2)


def _pct(value) -> float:
    return round(float(value), 2)


def load_live(cfg: Config, target: date, source: str, holding_list, cache_dir: Path | None = None, extra_codes: list[str] | None = None) -> tuple[dict, pd.DataFrame, list[str]]:
    warnings: list[str] = []
    start = data_mod.history_start(target, cfg)
    needed = []
    for ticker in list(cfg.reference_tickers) + [h.ticker for h in holding_list]:
        if ticker not in needed:
            needed.append(ticker)
    frames: dict = {}
    for ticker in needed:
        try:
            frames[ticker] = data_mod.load_bars(ticker, start, target, source, cache_dir)
        except TrendDataError as exc:
            raise TrendDataError(f"필수 종목 {ticker} 시세를 읽지 못했습니다. {exc}") from exc
    refs = data_mod.require_reference(frames, cfg.reference_tickers, target)
    if len(refs) < len(cfg.reference_tickers):
        warnings.append("일부 기준 종목 봉이 기준일과 다릅니다. 맞는 종목: " + ", ".join(refs))

    listing = data_mod.load_listing("fdr" if source == "fdr" else "fdr")
    universe = data_mod.filter_universe(listing, cfg)
    codes = [c for c in universe["Code"].tolist() if c not in frames]
    print(f"유니버스 {len(universe)}종목, 추가 수신 {len(codes)}", flush=True)
    fetched, failures = data_mod.download_many(codes, start, target, source, cfg.workers, cache_dir)
    frames.update(fetched)
    if codes:
        ratio = len(failures) / len(codes)
        if failures and (ratio > cfg.max_fetch_fail_ratio and len(failures) > 10):
            sample = ", ".join(f"{c}:{m}" for c, m in failures[:5])
            raise TrendDataError(
                f"시세 실패 {len(failures)}/{len(codes)} ({ratio:.1%}). 가짜 가격으로 대체하지 않습니다. {sample}"
            )
        if failures:
            warnings.append(f"시세 실패 {len(failures)}종목은 후보에서 빼었습니다. 가격은 만들지 않았습니다.")
    for ticker in extra_codes or []:
        if ticker in frames:
            continue
        try:
            frames[ticker] = data_mod.load_bars(ticker, start, target, source, cache_dir)
        except TrendDataError as exc:
            warnings.append(f"관심종목 {ticker} 시세를 읽지 못했습니다. {exc}")
    if cfg.download_min_marcap_krw > 0 or cfg.download_min_amount_krw > 0:
        warnings.append(
            "다운로드 전 시가총액 또는 당일 거래대금 하한을 적용했습니다. "
            "하한을 0으로 두면 보통주 전체를 받습니다."
        )
    return frames, universe, warnings


def _entry_atr(df: pd.DataFrame, buy: date, ticker: str) -> tuple[float, date]:
    upto = df[df.index <= pd.Timestamp(buy)]
    if len(upto) < 20:
        raise TrendDataError(f"{ticker} 매수일 {buy.isoformat()} 까지 봉이 부족해 ATR 을 계산하지 않습니다.")
    used = upto.index[-1].date()
    if (buy - used).days > 7:
        raise TrendDataError(
            f"{ticker} 매수일 {buy.isoformat()} 근처 봉이 없습니다. 마지막 봉 {used.isoformat()}. 날짜를 바꾸지 않습니다."
        )
    atr = wilder_atr(upto["High"], upto["Low"], upto["Close"], 14)
    value = float(atr[-1])
    if not math.isfinite(value) or value <= 0:
        raise TrendDataError(f"{ticker} 매수일 ATR 이 없습니다. 값을 만들지 않습니다.")
    return value, used


def _breadth(ticker: str, cfg: Config, frames: dict, target: date) -> bool | None:
    if not cfg.sector_breadth_enabled:
        return None
    group = next((g for g in cfg.sector_groups if ticker in g.tickers), None)
    if group is None:
        return None
    peers = [code for code in group.tickers if code != ticker]
    if not peers:
        return None
    seen = 0
    above = 0
    for code in peers:
        df = frames.get(code)
        if df is None or len(df) < cfg.ma_window:
            continue
        upto = df[df.index <= pd.Timestamp(target)]
        if len(upto) < cfg.ma_window or upto.index[-1].date() != target:
            continue
        ma = float(upto["Close"].tail(cfg.ma_window).mean())
        seen += 1
        if float(upto["Close"].iloc[-1]) > ma:
            above += 1
    if seen == 0:
        return None
    return (above / seen) >= cfg.sector_breadth_min_ratio


def _eval_holding(pos, df: pd.DataFrame, cfg: Config, target: date, frames: dict) -> dict:
    upto = df[df.index <= pd.Timestamp(target)]
    if len(upto) == 0 or upto.index[-1].date() != target:
        last = upto.index[-1].date().isoformat() if len(upto) else "없음"
        raise TrendDataError(
            f"{pos.ticker} 마지막 봉 {last} 이 기준일 {target.isoformat()} 과 다릅니다. 가짜 가격으로 대체하지 않습니다."
        )
    if len(upto) < max(cfg.ma_window, 16):
        raise TrendDataError(f"{pos.ticker} 봉이 {len(upto)}개라 이평과 ATR 을 계산하지 않습니다.")
    entry_atr = pos.entry_atr
    if entry_atr is None:
        entry_atr, _used = _entry_atr(upto, pos.buy_date, pos.ticker)
    if pos.stop_mode == "atr" and entry_atr <= 0:
        raise TrendDataError(f"{pos.ticker} ATR 손절인데 진입 ATR 이 없습니다.")
    ready = prepare(upto, cfg)
    i = len(ready) - 1
    atr_prev = float(ready["atr14"].iloc[i - 1])
    ma = float(ready[f"ma{cfg.ma_window}"].iloc[i])
    if not math.isfinite(ma):
        raise TrendDataError(f"{pos.ticker} 이평이 없습니다. 값을 만들지 않습니다.")
    buy_ts = pd.Timestamp(pos.buy_date)
    since = ready[ready.index >= buy_ts]
    if len(since) == 0:
        since = ready.tail(1)
    max_high = float(since["High"].max())
    row = ready.iloc[i]
    if not math.isfinite(atr_prev):
        atr_prev = float("nan")
    decision = evaluate_exit(
        PositionView(
            ticker=pos.ticker,
            name=pos.name,
            avg_price=pos.avg_price,
            remaining_qty=pos.remaining_qty,
            breakout_price=pos.breakout_price,
            entry_atr=float(entry_atr or 0),
            stop_mode=pos.stop_mode,
            climax_taken=pos.climax_taken,
        ),
        DayView(
            open=float(row["Open"]),
            high=float(row["High"]),
            low=float(row["Low"]),
            close=float(row["Close"]),
            ma=ma,
            atr_prev=atr_prev,
            max_high_since_entry=max_high,
        ),
        cfg.exit_params(),
        breadth_ok=_breadth(pos.ticker, cfg, frames, target),
    )
    remaining_after = pos.remaining_qty - int(decision["sell_qty"])
    decision.update(
        {
            "ticker": pos.ticker,
            "name": pos.name,
            "close": _price(row["Close"]),
            "avg_price": _price(pos.avg_price),
            "_remaining_before": pos.remaining_qty,
            "_remaining_after": remaining_after,
        }
    )
    return decision


def _screen_all(frames: dict, universe: pd.DataFrame, cfg: Config, target: date) -> list[dict]:
    meta = {row.Code: row for row in universe.itertuples(index=False)}
    found = []
    checked = 0
    for code, df in frames.items():
        info = meta.get(code)
        if info is None:
            continue
        upto = df[df.index <= pd.Timestamp(target)]
        if len(upto) == 0 or upto.index[-1].date() != target:
            continue
        checked += 1
        if len(upto) < max(cfg.high52_days + 1, cfg.trend_mas[-1]):
            continue
        hit = screen_at(prepare(upto, cfg), len(upto) - 1, cfg)
        if hit is None:
            continue
        hit = dict(hit)
        hit["ticker"] = code
        hit["name"] = str(info.Name)
        hit["market"] = data_mod.output_market(str(info.Market))
        found.append(hit)
    print(f"스크리너 검사 {checked}종목, 조건 통과 {len(found)}", flush=True)
    return found


def _attach_eps(cands: list[dict], cfg: Config, target: date, cache_dir: Path) -> list[str]:
    warnings = []
    key = api_key_from_env()
    if not key:
        print(MISSING_KEY_MESSAGE, flush=True)
        warnings.append(MISSING_KEY_MESSAGE)
        for cand in cands:
            cand["eps_yoy_pct"] = None
            cand["eps_note"] = "EPS unknown"
        return warnings
    print(KEY_PRESENT_MESSAGE, flush=True)
    client = DartClient(key, cache_dir / "dart", min_interval=cfg.dart_min_interval_sec)
    for cand in cands:
        pct, note = client.eps_yoy(cand["ticker"], target)
        cand["eps_yoy_pct"] = None if pct is None else round(float(pct), 2)
        cand["eps_note"] = note
        if client.auth_failed:
            warnings.append(client.auth_message)
            for rest in cands:
                rest.setdefault("eps_yoy_pct", None)
                rest.setdefault("eps_note", client.auth_message)
            break
    if client.auth_message and client.auth_failed:
        pass
    return warnings


def _allocate(cands: list[dict], holdings_rows: list[dict], cfg: Config, breadth_pct: float | None) -> tuple[list[dict], dict]:
    account = float(cfg.account_krw)
    mv = 0.0
    open_risk = 0.0
    for row in holdings_rows:
        qty = int(row["_remaining_before"])
        close = float(row["close"])
        mv += close * qty
        open_risk += max(0.0, close - float(row["defense_line"])) * qty
    if cfg.cash_krw is None:
        cash = max(0.0, account - mv)
    else:
        cash = float(cfg.cash_krw)
    room = max(0.0, account * cfg.max_total_open_risk_pct / 100.0 - open_risk)
    spent = 0.0
    ordered = sorted(cands, key=lambda c: (c["distance_pct"], c["ticker"]))
    out = []
    for cand in ordered:
        entry = round_to_tick(cand["close"])
        stop = int(cand["stop"])
        distance = float(cand["distance_pct"])
        eps = cand.get("eps_yoy_pct")
        eps_note = cand.get("eps_note") or "EPS unknown"
        flow_note = cand.get("flow_note") or "수급 확인 불가"
        base = (
            f"52주 고점 돌파, 베이스 {cand['base_days']}일, 깊이 {_pct(cand['base_depth_pct'])}%. {eps_note} {flow_note}"
        )
        chase = distance > cfg.chase_max_distance_pct
        eps_block = cfg.eps_require and (eps is None or eps < cfg.eps_min_yoy_pct)
        target = 0
        if entry > stop:
            target = round_to_tick(entry + 3 * (entry - stop))
        if chase:
            out.append(_cand_row(cand, entry, 0, 0.0, target, "chase", base + f" 돌파가 대비 { _pct(distance) }% 로 추격이라 수량 제외."))
            continue
        if eps_block:
            why = "EPS 조건 미달 또는 unknown 이라 수량 제외." if eps is None else f"EPS 전년비 {eps}% 가 {cfg.eps_min_yoy_pct:g}% 미만."
            out.append(_cand_row(cand, entry, 0, 0.0, target, "excluded", base + " " + why))
            continue
        qty, note = size_order(
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
            out.append(_cand_row(cand, entry, 0, 0.0, target, "excluded", base + " " + note))
            continue
        halve = breadth_pct is not None and breadth_pct < cfg.breadth_min_pct
        if halve:
            qty = qty // 2
            note += f" 시장 폭 {breadth_pct}% 가 {cfg.breadth_min_pct:g}% 미만이라 수량을 절반으로 줄였습니다."
        if qty < 1:
            out.append(_cand_row(cand, entry, 0, 0.0, target, "excluded", base + " " + note))
            continue
        cost = qty * entry
        risk_add = qty * (entry - stop)
        cash -= cost
        room -= risk_add
        spent += cost
        open_risk += risk_add
        weight = qty * entry / account * 100.0
        out.append(_cand_row(cand, entry, qty, weight, target, "ok", base + " " + note))
    out.sort(key=lambda row: ({"ok": 0, "chase": 1, "excluded": 2}[row["status"]], row["distance_pct"], row["ticker"]))
    risk = {
        "total_open_risk_pct": round(open_risk / account * 100.0, 4) if account else 0.0,
        "cash_after": int(round(cash)),
        "exposure_pct": round((mv + spent) / account * 100.0, 4) if account else 0.0,
    }
    return out, risk


def _cand_row(cand, entry, qty, weight, target, status, reason) -> dict:
    eps = cand.get("eps_yoy_pct")
    return {
        "ticker": cand["ticker"],
        "name": cand["name"],
        "market": cand["market"],
        "close": _price(entry),
        "high52": _price(round_to_tick(cand["high52"])),
        "breakout_price": _price(round_to_tick(cand["breakout_price"])),
        "distance_pct": _pct(cand["distance_pct"]),
        "atr14": round(float(cand["atr14"]), 2),
        "base_days": int(cand["base_days"]),
        "base_depth_pct": _pct(cand["base_depth_pct"]),
        "eps_yoy_pct": None if eps is None else round(float(eps), 2),
        "stop": int(cand["stop"]),
        "qty": int(qty),
        "weight_pct": round(float(weight), 2),
        "target_3r": int(target),
        "status": status,
        "reason": " ".join(reason.split()),
        "foreign_net_5d": cand.get("foreign_net_5d"),
        "foreign_net_10d": cand.get("foreign_net_10d"),
        "institution_net_5d": cand.get("institution_net_5d"),
        "institution_net_10d": cand.get("institution_net_10d"),
    }


def _public_holding(row: dict) -> dict:
    return {key: row[key] for key in report_mod.HOLDING_KEYS}


def _attach_flow(rows: list[dict], target: date, demo: bool, client: FlowClient | None) -> None:
    fields = ("foreign_net_5d", "foreign_net_10d", "institution_net_5d", "institution_net_10d")
    for row in rows:
        if demo or client is None:
            for key in fields:
                row[key] = None
            row["flow_note"] = "DEMO 수급 확인 불가" if demo else "수급 확인 불가"
            continue
        info = client.lookup(row["ticker"], target)
        for key in fields:
            row[key] = info.get(key)
        row["flow_note"] = info.get("flow_note") or "수급 확인 불가"


def _watch_public(row: dict) -> dict:
    return {key: row.get(key) for key in report_mod.WATCH_KEYS}


def build_payload(
    *,
    cfg: Config,
    target: date,
    frames: dict,
    universe: pd.DataFrame,
    holding_list,
    warnings: list[str],
    source_label: str,
    demo: bool,
    generated_at: str,
    state_path: Path,
    apply_state: bool,
    watch_items: list | None = None,
) -> dict:
    holding_rows = [_eval_holding(pos, frames[pos.ticker], cfg, target, frames) for pos in holding_list]
    cands = _screen_all(frames, universe, cfg, target)
    cache_root = ROOT / "cache"
    if demo:
        for cand in cands:
            cand["eps_yoy_pct"] = None
            cand["eps_note"] = "DEMO EPS unknown"
        warnings.append("DEMO. 오프라인 픽스처이며 실매매 신호가 아닙니다.")
    else:
        warnings.extend(_attach_eps(cands, cfg, target, cache_root))
    flow = None if demo else FlowClient()
    _attach_flow(cands, target, demo, flow)
    breadth_pct, breadth_note = market_breadth(universe, frames, target, cfg)
    warnings.append(breadth_note)
    sized, risk = _allocate(cands, holding_rows, cfg, breadth_pct)
    risk["breadth_pct_above_ma50"] = breadth_pct
    watch_rows = evaluate_watchlist(list(watch_items or []), frames, target)
    _attach_flow(watch_rows, target, demo, flow)
    for row in watch_rows:
        note = row.get("flow_note") or "수급 확인 불가"
        row["reason"] = f"{row['reason']} {note}".strip()
    state = holdings_mod.load_state(state_path)
    holdings_mod.suggest_and_maybe_apply(holding_rows, state, target.isoformat(), state_path, apply_state)
    if apply_state:
        warnings.append(f"상태를 저장했습니다: {state_path}")
    payload = {
        "schema_version": 1,
        "generated_at": generated_at,
        "trade_date": target.isoformat(),
        "demo": bool(demo),
        "params": cfg.public_params(),
        "data_status": {"source": source_label, "ok": True, "warnings": warnings, "watchlist": [_watch_public(row) for row in watch_rows]},
        "holdings": [_public_holding(row) for row in holding_rows],
        "candidates": sized,
        "risk": risk,
    }
    report_mod.validate_payload(payload)
    return payload
