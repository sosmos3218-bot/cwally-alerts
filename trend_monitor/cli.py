"""명령줄. 휴장이 아니면 요약 1통을 보냅니다. --dry-run 은 출력만 합니다."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from trend_monitor import holdings as holdings_mod
from trend_monitor import ntfy_summary
from trend_monitor import report as report_mod
from trend_monitor.calendar_krx import assert_session_ready, default_trade_date, is_trading_day, now_kst, parse_trade_date
from trend_monitor.config import apply_cli, load_config
from trend_monitor.engine import OUTPUT_DIR, STATE_PATH, build_payload, load_live
from trend_monitor.errors import TrendError
from trend_monitor.report import stamp
from trend_monitor.watchlist import load_watchlist, resolve_watchlist_path


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="한국 주식 추세 모니터. 종가 기준, 실데이터.")
    p.add_argument("--date", default=None, help="기준일 YYYY-MM-DD 또는 YYYYMMDD. 비우면 서울 기준 최근 마감 거래일")
    p.add_argument("--demo", action="store_true", help="오프라인 픽스처. 모든 출력에 DEMO 표시")
    p.add_argument("--allow-partial", action="store_true", help="15:30 이전 미완성 봉 허용")
    p.add_argument("--account", type=int, default=None)
    p.add_argument("--risk-pct", type=float, default=None, dest="risk_pct")
    p.add_argument("--max-weight", type=float, default=None)
    p.add_argument("--max-total-risk", type=float, default=None)
    p.add_argument("--cash", type=int, default=None)
    p.add_argument("--stop-mode", choices=("pct", "atr"), default=None, help="pct 또는 atr. 마이너스 기호로 시작하지 않습니다.")
    p.add_argument("--ma-window", type=int, choices=(20, 50), default=None)
    p.add_argument("--holdings", default=None, help="보유 CSV. 비우면 TREND_HOLDINGS_PATH, 그다음 holdings.csv")
    p.add_argument("--watchlist", default=None, help="관심 종목 CSV. 비우면 TREND_WATCHLIST_PATH, 그다음 watchlist.csv")
    p.add_argument("--config", default=None)
    p.add_argument("--source", choices=("fdr", "pykrx"), default="fdr")
    p.add_argument("--apply-state", action="store_true", help="이번 매도 제안을 상태 JSON 에 반영")
    p.add_argument("--workers", type=int, default=None)
    p.add_argument("--dry-run", action="store_true", help="ntfy 제목과 본문만 출력하고 보내지 않습니다.")
    p.add_argument("--output-dir", default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return _run(args)
    except TrendError as exc:
        print(str(exc), file=sys.stderr)
        return exc.code


def _run(args) -> int:
    cfg = apply_cli(load_config(Path(args.config) if args.config else None), args)
    output_dir = Path(args.output_dir) if args.output_dir else OUTPUT_DIR
    if args.demo:
        from trend_monitor.demo_data import load_demo

        book = load_demo()
        target = parse_trade_date(args.date) if args.date else book["trade_date"]
        now = now_kst()
        # 데모 기준일이 과거면 마감 시각 검사는 통과합니다. 미래나 휴장이면 멈춥니다.
        note = assert_session_ready(target, now, args.allow_partial) if target >= now.date() else None
        warnings = list(book["warnings"])
        if note:
            warnings.append(note)
        if target != book["trade_date"] and args.date:
            # 픽스처에 그 날짜 봉이 있는지는 판단 단계에서 확인합니다.
            pass
        holdings = holdings_mod.load_holdings(book["holdings_path"], cfg.stop_mode)
        state_path = output_dir / "demo-state.json"
        frames = book["frames"]
        universe = book["universe"]
        source = "demo-fixtures"
        for pos in holdings:
            if pos.ticker not in frames:
                from trend_monitor.errors import TrendDataError

                raise TrendDataError(f"데모 픽스처에 {pos.ticker} 시세가 없습니다.")
        watch_items = load_watchlist(resolve_watchlist_path(args.watchlist))
    else:
        now = now_kst()
        if args.date:
            target = parse_trade_date(args.date)
        elif not is_trading_day(now.date()):
            print(f"{now.date().isoformat()} 은 KRX 휴장입니다. 알림을 보내지 않습니다.", flush=True)
            return 0
        else:
            target = default_trade_date(now)
        if not is_trading_day(target):
            print(f"{target.isoformat()} 은 KRX 휴장입니다. 알림을 보내지 않습니다.", flush=True)
            return 0
        note = assert_session_ready(target, now, args.allow_partial)
        warnings = []
        if note:
            warnings.append(note)
        path = holdings_mod.resolve_holdings_path(args.holdings)
        holdings = holdings_mod.load_holdings(path, cfg.stop_mode)
        if not holdings:
            print("보유 없음", flush=True)
        watch_items = load_watchlist(resolve_watchlist_path(args.watchlist))
        state_path = STATE_PATH
        state_notes = holdings_mod.apply_state(holdings, holdings_mod.load_state(state_path))
        warnings.extend(state_notes)
        frames, universe, live_warnings, stale = load_live(
            cfg, target, args.source, holdings, extra_codes=[item.ticker for item in watch_items]
        )
        warnings.extend(live_warnings)
        if stale:
            notice = ntfy_summary.publish_delay(
                target.isoformat(), stale, allow_send=not args.dry_run
            )
            _print_notice(notice, skipped=bool(args.dry_run))
            print("최신 봉이 기준일과 다릅니다. 추세 요약은 보내지 않습니다.", flush=True)
            return 0
        source = "FinanceDataReader" if args.source == "fdr" else "pykrx"
        if args.source == "fdr":
            source = "FinanceDataReader 0.9.202 KRX 일봉 (종목코드 기본 경로는 Naver 일봉)"
    if not args.demo:
        pass
    else:
        state_notes = holdings_mod.apply_state(holdings, holdings_mod.load_state(state_path))
        warnings.extend(state_notes)
    payload = build_payload(
        cfg=cfg,
        target=target,
        frames=frames,
        universe=universe,
        holding_list=holdings,
        warnings=warnings,
        source_label=source,
        demo=bool(args.demo),
        generated_at=stamp(now),
        state_path=state_path,
        apply_state=bool(args.apply_state),
        watch_items=watch_items,
    )
    paths = report_mod.write_outputs(payload, output_dir)
    allow_send = (not args.dry_run) and (not args.demo) and cfg.ntfy_enabled
    notice = ntfy_summary.maybe_publish(payload, cfg, allow_send=allow_send)
    _print_notice(notice, skipped=not allow_send)
    recovery = ntfy_summary.maybe_publish_recovery(payload, cfg, output_dir, allow_send=allow_send)
    if recovery:
        _print_notice(recovery, skipped=not allow_send)
    if args.demo:
        print("DEMO 실매매 신호가 아닙니다. 오프라인 픽스처입니다.", flush=True)
    print(
        f"기준일 {payload['trade_date']} 보유 {len(payload['holdings'])} 후보 {len(payload['candidates'])} "
        f"시장폭 {payload['risk']['breadth_pct_above_ma50']}",
        flush=True,
    )
    print(f"JSON {paths['latest']}", flush=True)
    return 0


def _print_notice(notice: dict, skipped: bool) -> None:
    print("NTFY TITLE", flush=True)
    print(notice["title"], flush=True)
    print("NTFY BODY", flush=True)
    print(notice["body"], flush=True)
    if skipped:
        print("발송하지 않음", flush=True)
    else:
        print("ntfy 1통 발송", flush=True)


if __name__ == "__main__":
    sys.exit(main())
