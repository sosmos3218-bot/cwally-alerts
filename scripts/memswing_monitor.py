#!/usr/bin/env python3
"""[MEMSWING] 삼성전자, SK하이닉스 스윙 진입/보유 일일 점검.

기존 평일 09:00 KST morning 슬롯. 직전 거래일 확정 수급으로 판정.
상태 변경(진입/손절/10일 만료/선택적 3일 노트)과 월요일(또는 그주 첫 거래일)
주간 요약만 cwally-port로 보낸다. 수급 미게시 2거래일 연속은 cwally-ops.

modes:
  daily|auto  스케줄 판정 (morning 슬롯). auto는 만료 oneshot 백업 호출도 동일 처리.
  dry         발송/state 쓰기 없음. MEMSWING_NOW 또는 --now 로 시각 지정 가능.
  sim         네트워크 없음. --now 와 --inject JSON으로 전이만 검증. state 쓰기 없음.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_calendar import is_trading_day, prev_trading_day  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "memswing.json"
STATE_PATH = ROOT / "state" / "memswing.json"
UA = "Mozilla/5.0 (cwally-alerts memswing; +mailto:sosmos3218@gmail.com)"
EOK = 100_000_000  # 1억원


# ---------- calendar helpers ----------

def next_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def trading_days_after(start: date, end: date) -> int:
    """Trading days strictly after start, up to and including end. 0 if end<=start."""
    if end <= start:
        return 0
    n, d = 0, start
    while d < end:
        d = next_trading_day(d)
        n += 1
        if d >= end:
            return n if d == end else n - 1
    return n


def first_trading_weekday_of_iso_week(d: date) -> date:
    monday = d - timedelta(days=d.weekday())
    cur = monday
    for _ in range(7):
        if is_trading_day(cur):
            return cur
        cur += timedelta(days=1)
    return monday


def iso_week_key(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def weekly_due(today: date, last_weekly_iso: str | None) -> bool:
    key = iso_week_key(today)
    if last_weekly_iso == key:
        return False
    return today == first_trading_weekday_of_iso_week(today)


# ---------- io ----------

def num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").replace("+", "").replace("%", ""))
    except ValueError:
        return None


def load_json(path: Path, fallback: dict) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            out = dict(fallback)
            out.update(data)
            return out
    except (OSError, json.JSONDecodeError):
        pass
    return dict(fallback)


def default_state(cfg: dict) -> dict:
    stocks = {}
    for code in cfg.get("stocks") or {}:
        stocks[code] = {
            "status": "idle",
            "signal_date": None,
            "entry_close": None,
            "entry_foreign_shares": None,
            "day3_sent": False,
        }
    return {
        "stocks": stocks,
        "last_weekly_iso": None,
        "missing_streak": 0,
        "last_missing_notice_day": None,
        "last_data_day": None,
        "last_title": None,
        "updated_at": None,
    }


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE_PATH)


def get_json(url: str, referer: str | None = None):
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    last = None
    for i in range(3):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8", errors="ignore"))
        except Exception as exc:
            last = exc
            time.sleep(2 * (i + 1))
    raise last


# ---------- fetch ----------

def fetch_stock(code: str, cfg: dict, want: date) -> dict:
    """Fetch foreign flow + KRX close for want and the prior trading day.

    Primary flow: Daum investor foreignStraightPurchaseVolume.
    KRX close: Daum investor tradePrice (same confirmed session as flow), with
    Daum quote days as fallback. Naver close is never used (may include NXT).
    Naver trend: flow cross-check / flow fallback only.
    """
    src = cfg["sources"]
    ref = src["referer"].format(code=code)
    day = want.strftime("%Y-%m-%d")
    day_compact = want.strftime("%Y%m%d")
    prev = prev_trading_day(want)
    prev_s = prev.strftime("%Y-%m-%d")
    prev_c = prev.strftime("%Y%m%d")
    out: dict = {
        "code": code,
        "day": day,
        "prev_day": prev_s,
        "posted": False,
        "errors": [],
        "quote_close": None,
        "investor_close": None,
        "close": None,
        "f_today": None,
        "f_prev": None,
        "naver_f_today": None,
        "naver_f_prev": None,
        "rows_flow": [],  # last up to 5 for weekly: [{day, shares, close}]
    }

    # Daum investor (flow + close)
    try:
        inv = get_json(src["investor"].format(code=code), ref).get("data") or []
        by = {}
        for r in inv:
            ds = str(r.get("date", "")).replace("-", "")[:8]
            if len(ds) == 8:
                iso = f"{ds[:4]}-{ds[4:6]}-{ds[6:8]}"
                by[iso] = {
                    "shares": num(r.get("foreignStraightPurchaseVolume")),
                    "close": num(r.get("tradePrice")),
                }
        if day in by:
            out["posted"] = True
            out["f_today"] = by[day]["shares"]
            out["investor_close"] = by[day]["close"]
        if prev_s in by:
            out["f_prev"] = by[prev_s]["shares"]
        # keep newest 5 for weekly
        for iso in sorted(by.keys(), reverse=True)[:5]:
            out["rows_flow"].append({
                "day": iso,
                "shares": by[iso]["shares"],
                "close": by[iso]["close"],
            })
    except Exception as exc:
        out["errors"].append(f"daum investor: {exc}")

    # Daum quote days (KRX close fallback / cross-check)
    try:
        q = get_json(src["krx_close"].format(code=code), ref).get("data") or []
        for r in q:
            ds = str(r.get("date", "")).replace("-", "")[:8]
            if ds == day_compact:
                out["quote_close"] = num(r.get("tradePrice"))
                break
    except Exception as exc:
        out["errors"].append(f"daum quote: {exc}")

    # Choose KRX close: prefer investor tradePrice when quote diverges >2%
    # (observed 2026-10-19: quote 218000 vs investor/Naver 223500).
    inv_c, q_c = out.get("investor_close"), out.get("quote_close")
    if inv_c is not None and q_c is not None and inv_c > 0 and abs(q_c - inv_c) / inv_c > 0.02:
        out["close"] = inv_c
        out["close_source"] = "daum_investor(quote_diverged)"
        out["errors"].append(f"quote_diverge quote={q_c} investor={inv_c}")
    elif q_c is not None:
        out["close"] = q_c
        out["close_source"] = "daum_quote"
    elif inv_c is not None:
        out["close"] = inv_c
        out["close_source"] = "daum_investor"
    else:
        out["close_source"] = None

    # Naver trend: flow cross-check / fallback
    try:
        rows = get_json(src["trend"].format(code=code))
        rows = [r for r in rows if isinstance(r, dict) and r.get("bizdate")]
        byn = {r["bizdate"]: r for r in rows}
        if day_compact in byn:
            out["naver_f_today"] = num(byn[day_compact].get("foreignerPureBuyQuant"))
            if not out["posted"]:
                out["posted"] = True
            if out["f_today"] is None:
                out["f_today"] = out["naver_f_today"]
                out["errors"].append("flow_fallback=naver")
        if prev_c in byn:
            out["naver_f_prev"] = num(byn[prev_c].get("foreignerPureBuyQuant"))
            if out["f_prev"] is None:
                out["f_prev"] = out["naver_f_prev"]
    except Exception as exc:
        out["errors"].append(f"naver trend: {exc}")

    return out


def sign_ok(primary: float | None, cross: float | None) -> bool:
    if primary is None:
        return False
    if cross is None:
        return True
    return (primary > 0) == (cross > 0)


# ---------- format ----------

def fmt_shares(v: float | None) -> str:
    return "확인 불가" if v is None else f"{v:+,.0f}주"


def fmt_won(v: float | None) -> str:
    return "확인 불가" if v is None else f"{v:,.0f}원"


def fmt_eok(shares: float | None, close: float | None) -> str:
    if shares is None or close is None:
        return "확인 불가"
    return f"{shares * close / EOK:+,.1f}억"


def fmt_pct(part: float, whole: float) -> str:
    if not whole:
        return "확인 불가"
    return f"{(part / whole - 1) * 100:+.1f}%"


# ---------- judgment ----------

def stock_state(cfg_stock: dict, st: dict) -> dict:
    return {
        "status": st.get("status") or "idle",
        "signal_date": st.get("signal_date"),
        "entry_close": st.get("entry_close"),
        "entry_foreign_shares": st.get("entry_foreign_shares"),
        "day3_sent": bool(st.get("day3_sent")),
        "ntfy_entry_id": st.get("ntfy_entry_id"),
        "threshold": cfg_stock["threshold"],
        "stop": cfg_stock["stop"],
        "target": cfg_stock.get("target"),
        "display": cfg_stock["display"],
        "short": cfg_stock["short"],
    }


def evaluate_stock(
    code: str,
    cfg_stock: dict,
    st: dict,
    data: dict,
    hold_min: int,
    hold_max: int,
) -> dict:
    """Return events and next stock state. Pure besides reading inputs."""
    cur = stock_state(cfg_stock, st)
    close = data.get("close")
    f0, f1 = data.get("f_today"), data.get("f_prev")
    buy2 = (
        f0 is not None and f0 > 0 and sign_ok(f0, data.get("naver_f_today"))
        and f1 is not None and f1 > 0 and sign_ok(f1, data.get("naver_f_prev"))
    )
    above = close is not None and close > cur["threshold"]
    below_stop = close is not None and close < cur["stop"]
    foreign_sell = f0 is not None and f0 < 0

    events: list[dict] = []
    nxt = dict(cur)
    data_day = date.fromisoformat(data["day"])
    days_held = 0
    if cur["status"] == "signal" and cur.get("signal_date"):
        days_held = trading_days_after(date.fromisoformat(cur["signal_date"]), data_day)

    if cur["status"] == "idle":
        if buy2 and above:
            nxt["status"] = "signal"
            nxt["signal_date"] = data["day"]
            nxt["entry_close"] = close
            nxt["entry_foreign_shares"] = f0
            nxt["day3_sent"] = False
            events.append({
                "kind": "entry",
                "priority": 4,
                "code": code,
                "close": close,
                "f_today": f0,
                "f_prev": f1,
                "days_held": 0,
            })
    elif cur["status"] == "signal":
        if below_stop:
            events.append({
                "kind": "stop",
                "priority": 5,
                "code": code,
                "close": close,
                "f_today": f0,
                "days_held": days_held,
                "foreign_sell": foreign_sell,
            })
            nxt["status"] = "idle"
            nxt["signal_date"] = None
            nxt["entry_close"] = None
            nxt["entry_foreign_shares"] = None
            nxt["day3_sent"] = False
        elif days_held >= hold_max:
            events.append({
                "kind": "exit_due",
                "priority": 4,
                "code": code,
                "close": close,
                "f_today": f0,
                "days_held": days_held,
            })
            nxt["status"] = "idle"
            nxt["signal_date"] = None
            nxt["entry_close"] = None
            nxt["entry_foreign_shares"] = None
            nxt["day3_sent"] = False
        elif days_held >= hold_min and not cur.get("day3_sent"):
            events.append({
                "kind": "day3",
                "priority": 3,
                "code": code,
                "close": close,
                "f_today": f0,
                "days_held": days_held,
            })
            nxt["day3_sent"] = True
        # foreign sell while holding: summary only (unless stop above)

    snap = {
        "code": code,
        "status": nxt["status"],
        "prev_status": cur["status"],
        "signal_date": nxt.get("signal_date") or cur.get("signal_date"),
        "days_held": days_held if cur["status"] == "signal" else (
            trading_days_after(date.fromisoformat(nxt["signal_date"]), data_day)
            if nxt.get("status") == "signal" and nxt.get("signal_date") else 0
        ),
        "close": close,
        "f_today": f0,
        "f_prev": f1,
        "buy2": buy2,
        "above": above,
        "below_stop": below_stop,
        "foreign_sell": foreign_sell,
        "threshold": cur["threshold"],
        "stop": cur["stop"],
        "target": cur.get("target"),
        "display": cur["display"],
        "short": cur["short"],
        "rows_flow": data.get("rows_flow") or [],
        "entry_close": nxt.get("entry_close") or cur.get("entry_close"),
    }
    # persist fields only
    persist = {
        "status": nxt["status"],
        "signal_date": nxt.get("signal_date"),
        "entry_close": nxt.get("entry_close"),
        "entry_foreign_shares": nxt.get("entry_foreign_shares"),
        "day3_sent": bool(nxt.get("day3_sent")),
    }
    if nxt.get("ntfy_entry_id") or cur.get("ntfy_entry_id"):
        persist["ntfy_entry_id"] = nxt.get("ntfy_entry_id") or cur.get("ntfy_entry_id")
    return {"events": events, "persist": persist, "snap": snap}


# ---------- messages ----------

def body_lines(stamp: str, what: str, result: str, todo: str, later: str, take: str, dont: str) -> str:
    return "\n".join([
        f"{stamp} | 삼성전자, SK하이닉스 스윙 점검",
        f"이게 뭐 알림: {what}",
        f"지금 결과/상태: {result}",
        f"지금 할 일: {todo}",
        f"나중에: {later}",
        f"익절/줄일 때: {take}",
        f"하지 말 것: {dont}",
    ])


def event_message(ev: dict, snap: dict, cfg: dict, stamp: str) -> tuple[str, str, int]:
    name, short = snap["display"], snap["short"]
    close, stop, thr = snap["close"], snap["stop"], snap["threshold"]
    kind = ev["kind"]
    hold_max = cfg["hold_max_days"]
    hold_min = cfg["hold_min_days"]

    if kind == "entry":
        title = f"[MEMSWING] 005930/000660 {short} 진입 조건 충족 | 분할 진입 검토"
        result = (
            f"{name} idle->signal. KRX 종가 {fmt_won(close)} (기준 {fmt_won(thr)} 초과). "
            f"외인 직전일 {fmt_shares(ev.get('f_prev'))}, 당일 {fmt_shares(ev.get('f_today'))} "
            f"(2일 연속 순매수). 금액 당일 약 {fmt_eok(ev.get('f_today'), close)}."
        )
        todo = (
            f"장중 손절선({fmt_won(stop)}) 위 유지를 확인하고 1/2만 분할 진입 검토. "
            f"갭 상승 3% 이상이면 추격 말고 눌림 대기."
        )
        later = f"보유 창은 {hold_min}~{hold_max}거래일. 3일 노트와 10일 만료, 손절을 이 알림이 이어서 본다."
        take = f"1차 {fmt_won(snap.get('target'))}에서 절반 익절, 나머지는 5일선 종가 이탈 시 정리."
        dont = "장후 NXT나 시간외로 판정 바꾸기, 추격, 신용, 미수. 투자 권유 아님."
        what = "외국인 순매수 2거래일 연속과 기준가 위 종가로 스윙 진입 조건이 새로 충족됨."
    elif kind == "stop":
        title = f"[MEMSWING] 005930/000660 {short} 손절선 이탈 | 정리 검토"
        result = (
            f"{name} signal->idle (손절). KRX 종가 {fmt_won(close)} < 손절 {fmt_won(stop)}. "
            f"보유 {ev.get('days_held')}거래일. 당일 외인 {fmt_shares(ev.get('f_today'))}."
        )
        todo = "보유분이 있으면 손절 기준으로 정리 검토. 신규 재진입은 조건이 다시 충족될 때까지 보류."
        later = "외인 2일 연속 순매수와 기준가 위 종가가 다시 나오면 진입 알림이 다시 온다."
        take = "손절 구간에서는 물타기하지 말 것."
        dont = "손절 이탈을 일시 조정으로 보고 비중 확대. 신용, 미수. 투자 권유 아님."
        what = "스윙 보유 중 종가가 손절선 아래로 마감해 청산 조건이 충족됨."
    elif kind == "exit_due":
        title = f"[MEMSWING] 005930/000660 {short} {hold_max}거래일 만료 | 청산 검토"
        result = (
            f"{name} signal->idle (만료). 보유 {ev.get('days_held')}거래일(한도 {hold_max}). "
            f"KRX 종가 {fmt_won(close)}, 손절 {fmt_won(stop)}. 당일 외인 {fmt_shares(ev.get('f_today'))}."
        )
        todo = "스윙 창이 끝났으니 잔량 청산 또는 규칙을 새로 정할지 검토."
        later = "같은 조건이 다시 충족되면 새 진입 알림이 온다."
        take = f"목표가 {fmt_won(snap.get('target'))} 근처면 잔량도 정리 검토."
        dont = "만료 알림을 신규 매수 신호로 읽기. 신용, 미수. 투자 권유 아님."
        what = f"스윙 보유 창 {hold_max}거래일에 도달해 청산 검토가 필요함."
    else:  # day3
        title = f"[MEMSWING] 005930/000660 {short} {hold_min}거래일 도달 | 조기 청산창 열림"
        result = (
            f"{name} 보유 {ev.get('days_held')}거래일. 종가 {fmt_won(close)}, "
            f"손절까지 {fmt_pct(stop, close) if close else '확인 불가'}, "
            f"기준까지 {fmt_pct(thr, close) if close else '확인 불가'}. "
            f"당일 외인 {fmt_shares(ev.get('f_today'))}."
        )
        todo = f"이제 {hold_min}~{hold_max}거래일 창 안이다. 계획한 익절/부분 청산을 점검."
        later = f"{hold_max}거래일 만료 또는 손절 이탈 때 다시 알린다. 외인 순매도 전환은 월요일 요약에 넣는다."
        take = f"1차 {fmt_won(snap.get('target'))}에서 절반, 나머지는 추세 이탈 시."
        dont = "3일 노트를 신규 진입 신호로 읽기. 투자 권유 아님."
        what = f"스윙 보유 {hold_min}거래일에 도달해 가장 빠른 청산창이 열림."

    msg = body_lines(stamp, what, result, todo, later, take, dont)
    return title, msg, ev["priority"]


def weekly_message(snaps: list[dict], stamp: str, week_key: str) -> tuple[str, str, int]:
    lines = []
    for s in snaps:
        rows = s.get("rows_flow") or []
        flow_bits = []
        for r in rows[:5]:
            flow_bits.append(
                f"{r['day'][5:].replace('-', '/')} {fmt_shares(r.get('shares'))}"
                f"({fmt_eok(r.get('shares'), r.get('close') or s.get('close'))})"
            )
        flow_txt = ", ".join(flow_bits) if flow_bits else "확인 불가"
        st = s["status"]
        held = s.get("days_held") or 0
        close, stop, thr = s.get("close"), s["stop"], s["threshold"]
        dist_stop = fmt_pct(stop, close) if close else "확인 불가"
        dist_thr = fmt_pct(thr, close) if close else "확인 불가"
        sell_note = ""
        if st == "signal" and s.get("foreign_sell"):
            sell_note = " 외인 순매도 전환(요약만)."
        lines.append(
            f"- {s['display']}: 종가 {fmt_won(close)}, 상태 {st}"
            f"{'(보유 ' + str(held) + '일)' if st == 'signal' else ''}, "
            f"손절까지 {dist_stop}, 기준까지 {dist_thr}. 외인 최근5: {flow_txt}.{sell_note}"
        )
    title = f"[MEMSWING] 005930/000660 주간 요약 {week_key} | 상태 점검"
    msg = body_lines(
        stamp,
        "삼성전자, SK하이닉스 스윙 주간 요약. 상태 변경이 없어도 주 1회 보낸다.",
        "\n" + "\n".join(lines),
        "상태와 손절/기준 거리만 확인하고, 조건이 바뀐 종목만 대응.",
        "다음 주 첫 거래일에 다시 요약. 진입/손절/만료는 그때그때 따로 온다.",
        "목표가 근처면 절반 익절 규칙을 유지.",
        "주간 요약을 신규 매수 신호로 읽기. 투자 권유 아님.",
    )
    return title, msg, 2


def missing_ops_message(stamp: str, data_day: str, streak: int, err: str | None) -> tuple[str, str, int]:
    title = f"[MEMSWING] 005930/000660 직전 거래일 수급 미게시 | 판정 보류"
    detail = f"오류: {err[:160]}" if err else f"{data_day} 확정 수급 행 없음"
    msg = body_lines(
        stamp,
        "직전 거래일 확정 외국인 수급을 확인해 스윙 조건을 판정하려 했으나 데이터가 없음.",
        f"{detail}. 연속 미확인 {streak}거래일.",
        "신규 진입/청산 판단을 보류하고, 증권 앱에서 해당일 외인 수급을 직접 확인.",
        "다음 평일 09:00에 다시 시도. 이틀 연속이면 이 안내가 한 번 간다.",
        "보유분은 종가 기준 손절선(삼성 204,500원, 하이닉스 1,238,000원)만 지키세요.",
        "조회 실패나 미게시를 매수 또는 매도 신호로 읽기. 투자 권유 아님.",
    )
    return title, msg, 4


# ---------- main ----------

def parse_now(raw: str | None) -> datetime:
    if raw:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    env = os.environ.get("MEMSWING_NOW")
    if env:
        return datetime.strptime(env, "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    return datetime.now(KST)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="memswing_monitor")
    p.add_argument("mode", nargs="?", default="daily",
                   help="daily|auto|dry|sim (default daily)")
    p.add_argument("--now", dest="now", default=None,
                   help="KST 'YYYY-MM-DD HH:MM' override")
    p.add_argument("--inject", dest="inject", default=None,
                   help="sim only: JSON file with per-code day data (no network)")
    return p


def load_inject(path: str) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit("inject must be a JSON object keyed by code")
    return data


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    mode = (args.mode or "daily").lower()
    if mode not in {"daily", "auto", "dry", "sim"}:
        print(f"unknown mode {mode}; use daily|auto|dry|sim")
        return 2
    dry = mode in {"dry", "sim"}
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = load_json(STATE_PATH, default_state(cfg))
    # ensure per-stock keys exist
    state.setdefault("stocks", {})
    for code in cfg["stocks"]:
        state["stocks"].setdefault(code, {
            "status": "idle", "signal_date": None, "entry_close": None,
            "entry_foreign_shares": None, "day3_sent": False,
        })

    now = parse_now(args.now)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    today = now.date()

    if not is_trading_day(today):
        print(f"{today} not a KRX trading day; silent")
        return 0

    data_day = prev_trading_day(today)
    print(f"mode={mode} run={today} data_day={data_day} dry={dry}")

    # Fetch or inject
    data: dict[str, dict] = {}
    fetch_err: str | None = None
    if mode == "sim":
        if not args.inject:
            print("sim requires --inject JSON")
            return 2
        injected = load_inject(args.inject)
        for code in cfg["stocks"]:
            raw = injected.get(code) or {}
            data[code] = {
                "code": code,
                "day": raw.get("day") or data_day.isoformat(),
                "prev_day": raw.get("prev_day") or prev_trading_day(data_day).isoformat(),
                "posted": bool(raw.get("posted", True)),
                "errors": list(raw.get("errors") or []),
                "close": num(raw.get("close")),
                "f_today": num(raw.get("f_today")),
                "f_prev": num(raw.get("f_prev")),
                "naver_f_today": num(raw.get("naver_f_today")),
                "naver_f_prev": num(raw.get("naver_f_prev")),
                "rows_flow": raw.get("rows_flow") or [],
                "close_source": "inject",
            }
            print(json.dumps(data[code], ensure_ascii=False))
    else:
        try:
            for code in cfg["stocks"]:
                data[code] = fetch_stock(code, cfg, data_day)
                print(json.dumps({k: data[code].get(k) for k in (
                    "code", "day", "posted", "close", "close_source", "f_today", "f_prev",
                    "naver_f_today", "naver_f_prev", "errors")}, ensure_ascii=False))
        except Exception as exc:
            fetch_err = str(exc)
            print(f"fetch error: {fetch_err}")

    posted = fetch_err is None and all(d.get("posted") and d.get("f_today") is not None for d in data.values())
    # also need close for judgment; treat missing close like missing data for that stock
    if posted and any(d.get("close") is None for d in data.values()):
        posted = False
        fetch_err = fetch_err or "KRX close missing for one or more names"

    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    alerts: list[tuple[str, str, int]] = []
    state_dirty = False
    new_state = json.loads(json.dumps(state))  # deep copy via json

    if not posted:
        streak = int(new_state.get("missing_streak") or 0) + 1
        new_state["missing_streak"] = streak
        print(f"data missing for {data_day}; streak={streak}")
        if streak >= 2 and new_state.get("last_missing_notice_day") != data_day.isoformat():
            title, msg, prio = missing_ops_message(stamp, data_day.isoformat(), streak, fetch_err)
            alerts.append((title, msg, prio))
            new_state["last_missing_notice_day"] = data_day.isoformat()
            state_dirty = True
        else:
            # persist streak so day-2 can fire; counts as state change for missing tracking
            state_dirty = True
        # still fall through to send/write logic
    else:
        if int(new_state.get("missing_streak") or 0) != 0:
            state_dirty = True
        new_state["missing_streak"] = 0
        hold_min = int(cfg.get("hold_min_days") or 3)
        hold_max = int(cfg.get("hold_max_days") or 10)
        snaps = []
        for code, rule in cfg["stocks"].items():
            st = (state.get("stocks") or {}).get(code) or {}
            result = evaluate_stock(code, rule, st, data[code], hold_min, hold_max)
            snaps.append(result["snap"])
            for ev in result["events"]:
                title, msg, prio = event_message(ev, result["snap"], cfg, stamp)
                alerts.append((title, msg, prio))
                state_dirty = True
            prev_p = json.dumps(st, sort_keys=True, ensure_ascii=False)
            new_p = json.dumps(result["persist"], sort_keys=True, ensure_ascii=False)
            if prev_p != new_p:
                state_dirty = True
            new_state.setdefault("stocks", {})[code] = result["persist"]

        # Monday (or first trading weekday) weekly summary
        if weekly_due(today, new_state.get("last_weekly_iso")):
            week_key = iso_week_key(today)
            title, msg, prio = weekly_message(snaps, stamp, week_key)
            alerts.append((title, msg, prio))
            new_state["last_weekly_iso"] = week_key
            state_dirty = True
            print(f"weekly summary due {week_key}")
        else:
            print(f"weekly not due (last={new_state.get('last_weekly_iso')})")

        new_state["last_data_day"] = data_day.isoformat()

    for title, msg, prio in alerts:
        print(title)
        print(msg)
        print(f"topic={route_topic(topic, title)} priority={prio} send={not dry}")
        if not dry:
            publish(topic, title, msg, prio, ["chart_with_upwards_trend"], server)
            new_state["last_title"] = title

    if not alerts:
        print("no alert (no state change / not weekly)")

    new_state["updated_at"] = now.isoformat(timespec="seconds")
    if dry:
        print("DRY/SIM: no ntfy, no state write")
        return 0
    if state_dirty or alerts:
        save_state(new_state)
        print(f"state written -> {STATE_PATH}")
    else:
        print("state unchanged (no send, no transition)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
