#!/usr/bin/env python3
"""[309230] ACE 미국WideMoat동일가중 KRX 종가 선 + MOAT-QQQ 레짐 알림.

가격선 (configs/k309230.json lines):
  buy1 종가 <= 29,000 p4 (29,870 이상 재무장), buy2 <= 28,600 p5 (29,458 이상),
  trim >= 32,800 p3 (31,816 이하 재무장). 선마다 1번. MA20 회복, 분배 알림 없음.
종가: scripts/krx_close.py regular_bars (Daum KRX 정규장, NXT 미포함).
레짐: Yahoo MOAT 63거래일 수익률 - QQQ 63거래일 수익률(%p).
  +3.0 이상 진입 p3(품질 우위 매력 상승), +1.0 미만 중립.
  -3.0 이하 진입 p4(AI 모멘텀 재개 매력 하락), -1.0 초과 중립.
  조회 실패는 레짐만 스킵. 연속 실패는 cwally-ops 하루 1번.
참고줄: RSP-SPY 스프레드, 원/달러 (판정 아님).
첫 실행(state 시드됨): 발송 없음. ends_kst(2027-05-05 23:59) 뒤 조용히 종료.
modes: auto | dry | install | selftest ; --as-of "YYYY-MM-DD HH:MM" (dry 전용)
"""
from __future__ import annotations

import json
import re
import sys
import traceback
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402
import krx_close  # noqa: E402
from kr_calendar import KST, is_trading_day, session_day, session_close_ready  # noqa: E402

TAG = "[309230]"
NAME = "k309230"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
CLOSE_HM = (15, 40)
N_BARS = 40
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}
NY = ZoneInfo("America/New_York")


def krx_rows(code: str) -> list[dict]:
    bars = krx_close.regular_bars(code, N_BARS)
    return [{"day": b.day, "close": float(b.close), "src": b.source} for b in bars]


def confirmed(rows: list[dict], today: str, now: datetime) -> list[dict]:
    return [r for r in rows if session_close_ready(r["day"], today, now, CLOSE_HM)]


def hit(op: str, c: float, lvl: float) -> bool:
    return {"le": c <= lvl, "ge": c >= lvl, "gt": c > lvl, "lt": c < lvl}[op]


def yahoo_bars(symbol: str, now_ny: datetime | None = None) -> list[tuple[date, float]]:
    """완료 미국 일봉 (날짜, 종가) 오름차순. 진행 중 오늘 봉 제외."""
    now_ny = now_ny or datetime.now(NY)
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(symbol)
        + "?range=1y&interval=1d&includePrePost=false"
    )
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        res = json.loads(resp.read().decode())["chart"]["result"][0]
    out: dict[date, float] = {}
    for t, c in zip(res.get("timestamp") or [], res["indicators"]["quote"][0].get("close") or []):
        if c is None:
            continue
        out[datetime.fromtimestamp(t, NY).date()] = float(c)
    meta = res.get("meta") or {}
    rmt, rmp = meta.get("regularMarketTime"), meta.get("regularMarketPrice")
    if rmt and rmp:
        mt = datetime.fromtimestamp(rmt, NY)
        if mt.date() not in out and mt.hour * 60 + mt.minute >= 16 * 60:
            out[mt.date()] = float(rmp)
    today = now_ny.date()
    after_close = now_ny.hour * 60 + now_ny.minute >= 16 * 60 + 10
    return sorted((d, c) for d, c in out.items() if d < today or (d == today and after_close))


def yahoo_meta(ticker: str) -> dict[str, Any] | None:
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(ticker)
        + "?interval=1d&range=5d"
    )
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())["chart"]["result"][0].get("meta") or {}
    except Exception as exc:  # noqa: BLE001
        print(f"yahoo meta fail {ticker}: {exc}", file=sys.stderr)
        return None


def ret_spread(
    a: list[tuple[date, float]], b: list[tuple[date, float]], n: int
) -> tuple[float, date, float, float] | None:
    """a_n - b_n (%p) on common dates. Returns (spread, last_day, a_ret, b_ret)."""
    mb = {d: c for d, c in b}
    common = [(d, ca, mb[d]) for d, ca in a if d in mb]
    if len(common) <= n:
        return None
    d0, a0, b0 = common[-n - 1]
    d1, a1, b1 = common[-1]
    ar = (a1 / a0 - 1) * 100
    br = (b1 / b0 - 1) * 100
    return ar - br, d1, ar, br


def seed_price(cfg: dict, rows: list[dict], stamp: str) -> dict:
    row = rows[-1]
    return {
        "last_date": row["day"],
        "last_close": row["close"],
        "armed": {ln["key"]: not hit(ln["op"], row["close"], ln["level"]) for ln in cfg["lines"]},
        "seed_note": f"첫 실행 {stamp}: 마지막 KRX 확정 종가 {row['day']} {row['close']:,.0f}원",
    }


def evaluate_price(cfg: dict, ps: dict, rows: list[dict], today: str) -> list[dict]:
    out = []
    for i, r in enumerate(rows):
        d, c = r["day"], r["close"]
        if d <= ps["last_date"] or d < cfg["window_start"] or d > today:
            continue
        prev = rows[i - 1]["close"] if i else None
        for ln in cfg["lines"]:
            k = ln["key"]
            if hit(ln["op"], c, ln["level"]) and ps["armed"].get(k, True):
                ps["armed"][k] = False
                out.append({**ln, "day": d, "close": c, "prev": prev})
            elif hit(ln["rearm_op"], c, ln["rearm"]) and not ps["armed"].get(k, True):
                ps["armed"][k] = True
        ps["last_date"], ps["last_close"] = d, c
    return out


def evaluate_regime(
    cfg: dict, rs: dict, spread: float, day: date, *, force_seed_zone: str | None = None
) -> dict | None:
    """Update rs in place. Return event dict or None.
    force_seed_zone: if set on empty zone, just set without firing.
    """
    up_e = float(cfg["regime"]["up_enter"])
    up_r = float(cfg["regime"]["up_reset"])
    dn_e = float(cfg["regime"]["down_enter"])
    dn_r = float(cfg["regime"]["down_reset"])
    zone = rs.get("zone") or "mid"
    day_s = day.isoformat() if isinstance(day, date) else str(day)
    ev = None
    if force_seed_zone is not None and not rs.get("zone"):
        rs["zone"] = force_seed_zone
        rs["last_spread"] = round(spread, 2)
        rs["last_day"] = day_s
        return None
    if zone == "up":
        if spread < up_r:
            zone = "mid"
    elif zone == "down":
        if spread > dn_r:
            zone = "mid"
    if zone == "mid":
        if spread >= up_e:
            zone = "up"
            ev = {"key": "reg_up", "day": day_s, "spread": spread, "priority": 3,
                  "head": "품질 우위 매력 상승 (MOAT-QQQ)"}
        elif spread <= dn_e:
            zone = "down"
            ev = {"key": "reg_down", "day": day_s, "spread": spread, "priority": 4,
                  "head": "AI 모멘텀 재개 매력 하락 (MOAT-QQQ)"}
    rs["zone"] = zone
    rs["last_spread"] = round(spread, 2)
    rs["last_day"] = day_s
    return ev


def dist_txt(c: float, cfg: dict) -> str:
    parts = []
    for ln in cfg["lines"]:
        lvl = ln["level"]
        parts.append(f"{ln['tier_ko']} {lvl:,}원까지 {(c / lvl - 1) * 100:+.1f}%")
    return ", ".join(parts)


def body(stamp: str, hdr: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([
        f"{stamp} | 309230 ACE 미국WideMoat동일가중 | {hdr}",
        f"이게 뭐 알림: {what}",
        f"지금 결과/상태: {status}",
        f"지금 할 일: {action}",
        f"나중에: {later}",
        f"익절/줄일 때: {trim}",
        f"하지 말 것: {avoid}",
    ])


WHAT = (
    "ACE 미국WideMoat동일가중(309230, 환노출) KRX 정규장 종가 기준 분할/축소 검토와 "
    "MOAT 대비 QQQ 3개월 상대강도 레짐. 매매 지시 아님, 자동 주문 아님."
)
AVOID = (
    "매매 지시로 보기, 시장가 주문(유동성 제한), 한 번에 다 사기, 신용/미수, "
    "NXT 시간외 가격으로 판단, 원/달러 참고줄을 환율 알림으로 착각하기."
)


def ctx_refs(
    cfg: dict,
    *,
    bars_fn: Callable[[str], list[tuple[date, float]]] | None = None,
    meta_fn: Callable[[str], dict | None] | None = None,
) -> tuple[str, float | None, date | None, str | None]:
    """Returns (ctx_line, moat_qqq_spread, spread_day, err)."""
    rg = cfg["regime"]
    n = int(rg["lookback_bars"])
    err = None
    spread = None
    sday = None
    moat_ret = qqq_ret = None
    rsp_sp = None
    try:
        fetch = bars_fn or (lambda s: yahoo_bars(s))
        moat = fetch(rg["moat"])
        qqq = fetch(rg["qqq"])
        got = ret_spread(moat, qqq, n)
        if got is None:
            err = f"MOAT/QQQ 공통 일봉 부족(lookback {n})"
        else:
            spread, sday, moat_ret, qqq_ret = got
        try:
            rsp = fetch(rg["rsp"])
            spy = fetch(rg["spy"])
            g2 = ret_spread(rsp, spy, n)
            if g2:
                rsp_sp = g2[0]
        except Exception as exc:  # noqa: BLE001
            print(f"RSP/SPY ref fail: {exc}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        err = f"Yahoo 레짐 조회 실패: {str(exc)[:140]}"
    fx_s = "원/달러 확인 불가"
    mfn = meta_fn or yahoo_meta
    fx = mfn(cfg.get("fx_ticker", "USDKRW=X"))
    if fx:
        px = fx.get("regularMarketPrice") or fx.get("previousClose")
        if px is not None:
            fx_s = f"원/달러 {float(px):,.1f}"
    parts = []
    if spread is not None and sday is not None:
        parts.append(
            f"MOAT-QQQ 3M {spread:+.2f}%p"
            f"(MOAT {moat_ret:+.1f}% / QQQ {qqq_ret:+.1f}%, {sday.isoformat()} 미국)"
        )
    else:
        parts.append("MOAT-QQQ 3M 확인 불가")
    if rsp_sp is not None:
        parts.append(f"RSP-SPY 3M {rsp_sp:+.2f}%p(참고)")
    parts.append(fx_s + "(참고, 환율 선 아님)")
    return " / ".join(parts), spread, sday, err


def price_messages(cfg: dict, ev: list[dict], stamp: str, ctx: str) -> list[tuple[str, str, int]]:
    b = cfg["baseline"]
    out = []
    for e in ev:
        c = e["close"]
        base = (
            f"{e['day']} KRX 종가 {c:,.0f}원"
            + (f"(전일 {e['prev']:,.0f}원)" if e.get("prev") else "")
            + f". {dist_txt(c, cfg)}. "
            f"52주 종가 저 {b['low_52w_close']:,}원({b['low_52w_close_day']}) 대비 "
            f"{(c / b['low_52w_close'] - 1) * 100:+.1f}%, "
            f"종가 고 {b['high_52w_close']:,}원({b['high_52w_close_day']}) 대비 "
            f"{(c / b['high_52w_close'] - 1) * 100:+.1f}%."
        )
        title = f"{TAG} {e['head']}"
        st = base + f" {e['why']} 참고: {ctx}."
        if e["key"] == "trim":
            act = "일부 축소, 익절 검토 신호입니다(매매 지시 아님). 계획 비중만, 지정가로."
            later = f"{e['rearm']:,}원 이하 종가 뒤 다시 {e['level']:,}원 이상이면 다시 알림."
            trim = "장기 위성이면 전량 정리보다 일부만. AI 모멘텀 재개(레짐 DOWN)와 겹치면 축소 비중을 더 검토."
            hdr = "일부 축소 익절 검토"
        elif e["key"] == "buy2":
            act = "2차 비중 확대 검토 신호입니다(매매 지시 아님). 계획 몫만, 지정가, 신용/미수 없이."
            later = f"{e['rearm']:,}원 이상 종가 뒤 다시 {e['level']:,}원 이하로 오면 다시 알림."
            trim = "52주 저점 부근. 지수(MOAT) 약세인지 원/달러 약세인지 본 뒤 남은 몫만."
            hdr = "2차 비중 확대 검토"
        else:
            act = "1차 분할 매수 검토 신호입니다(매매 지시 아님). 계획 몫만, 지정가로 나눠서."
            later = f"{e['rearm']:,}원 이상 종가 뒤 다시 {e['level']:,}원 이하로 오면 다시 알림. 더 내려가면 2차(28,600원)에서 따로."
            trim = "한 번에 다 채우지 않기. 레짐 DOWN이면 매수 보류를 같이 검토."
            hdr = "1차 분할 매수 검토"
        out.append((title, body(stamp, hdr, WHAT, st, act, later, trim, AVOID), e["priority"]))
    return out


def regime_messages(cfg: dict, ev: dict, stamp: str, ctx: str, close: float | None) -> tuple[str, str, int]:
    c_s = f"최근 KRX 종가 {close:,.0f}원. {dist_txt(close, cfg)}. " if close else ""
    sp = ev["spread"]
    if ev["key"] == "reg_up":
        title = f"{TAG} 품질 우위 매력 상승 | MOAT-QQQ {sp:+.1f}%p"
        st = (
            f"{c_s}MOAT-QQQ 3개월 상대 {sp:+.2f}%p 가 +{cfg['regime']['up_enter']:.1f}%p 이상 진입"
            f"({ev['day']} 미국 봉 기준). 품질, 밸류 동일가중 상대 강세 국면. 참고: {ctx}."
        )
        act = "매력 상승 참고입니다. 가격선(buy1/buy2)과 따로 보세요. 추격 매수 신호 아님."
        later = f"스프레드가 +{cfg['regime']['up_reset']:.1f}%p 미만으로 내려가면 중립. 그 뒤 다시 +{cfg['regime']['up_enter']:.1f}%p 이상일 때만 재알림."
        trim = "이미 목표 비중이면 추가하지 않기. trim 선(32,800원)과 겹치면 비중만 점검."
        hdr = "레짐 매력 상승"
        pri = 3
    else:
        title = f"{TAG} AI 모멘텀 재개 매력 하락 | MOAT-QQQ {sp:+.1f}%p"
        st = (
            f"{c_s}MOAT-QQQ 3개월 상대 {sp:+.2f}%p 가 {cfg['regime']['down_enter']:.1f}%p 이하 진입"
            f"({ev['day']} 미국 봉 기준). AI 대형 모멘텀 대비 언더퍼폼 국면. 참고: {ctx}."
        )
        act = "매력 하락 참고입니다. 신규, 추가 매수 보류를 검토. 자동 매도 아님."
        later = f"스프레드가 {cfg['regime']['down_reset']:.1f}%p 초과로 올라가면 중립. 그 뒤 다시 {cfg['regime']['down_enter']:.1f}%p 이하일 때만 재알림."
        trim = "보유 중이면 trim 선과 목표 비중만 점검. 전량 정리 근거로 쓰지 않기."
        hdr = "레짐 매력 하락"
        pri = 4
    return title, body(stamp, hdr, WHAT, st, act, later, trim, AVOID), pri


def ops_msg(stamp: str, status: str) -> tuple[str, str]:
    return (
        f"{TAG} ACE 미국WideMoat동일가중 조회 실패 | 판정 건너뜀",
        body(
            stamp, "데이터 읽기 실패",
            "309230 가격/레짐 알림의 데이터 읽기 실패 안내. 하루 1번. 매매 신호 아님.",
            status,
            "증권 앱에서 309230 KRX 정규장 종가와 MOAT/QQQ를 직접 보세요.",
            "다음 19:00 슬롯에서 다시 읽고, 가격은 KRX 종가로 이어서 판정합니다.",
            "이 안내로 비중을 바꾸지 않기.",
            "NXT 시간외나 네이버 일봉으로 대신 판정하기.",
        ),
    )


def install_message(stamp: str) -> tuple[str, str]:
    return (
        f"{TAG} 설치 확인 | 매매 신호 아님",
        body(
            stamp, "설치 확인",
            "309230 ACE 미국WideMoat동일가중 알림 설치 확인용 테스트 발송입니다. 매매 신호가 아닙니다.",
            "평일 19:00 KST에 KRX 정규장 확정 종가로 29,000원 이하(1차 p4), 28,600원 이하(2차 p5), "
            "32,800원 이상(축소 p3)을 선마다 1번 보고, MOAT-QQQ 3개월 상대 +3%p/-3%p 레짐을 같이 봅니다.",
            "알림이 도착했는지 확인만 하세요.",
            "재무장: 29,870 / 29,458 / 31,816. 2027-05-05 23:59에 끝납니다.",
            "해당 없음.",
            "이 메시지를 매매 신호로 해석하기.",
        ),
    )


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(st: dict) -> None:
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def check_text(t: str, m: str) -> None:
    assert not re.search("[\u00b7\u318d\u2014]", t + m), t
    assert t.startswith(TAG) and "309230" in t, t


def run(
    mode: str,
    now: datetime,
    cfg: dict,
    pub=publish,
    rows_fn=None,
    bars_fn=None,
    meta_fn=None,
    state: dict | None = None,
) -> int:
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    topic, server = resolve_topic("port", cfg.get("ntfy_topic")), resolve_server(cfg.get("ntfy_server"))
    print(f"{stamp} {NAME} mode={mode}")
    if mode == "install":
        t, m = install_message(stamp)
        check_text(t, m)
        print(f"{t}\n{m}")
        pub(topic, t, m, 3, ["white_check_mark"], server, config_name=NAME)
        print("install confirmation sent (state untouched)")
        return 0
    sday = session_day(now)
    today = sday.isoformat()
    if today > cfg["window_end"]:
        print(f"{today} 감시 끝({cfg['window_end']}): 조용히 종료")
        return 0
    if not is_trading_day(sday) or today < cfg["window_start"]:
        print(f"{today} KRX 휴장 또는 시작 전: 조용히 종료")
        return 0
    st = load_state() if state is None else state
    out: list[tuple[str, str, int]] = []
    price_err = None
    rows: list[dict] = []
    try:
        rows = confirmed((rows_fn or krx_rows)(cfg["code"]), today, now)
        if session_close_ready(today, today, now, CLOSE_HM) and (not rows or rows[-1]["day"] != today):
            price_err = f"{today} KRX 종가가 아직 없음(마지막 {rows[-1]['day'] if rows else '없음'}). 오늘 가격 판정 건너뜀"
        if rows:
            c = rows[-1]["close"]
            print(
                f"latest KRX close {rows[-1]['day']} {c:,.0f} src={rows[-1].get('src')} "
                f"rows={len(rows)}: {dist_txt(c, cfg)}"
            )
        if not (st.get("price") or {}).get("last_date"):
            if rows and not price_err:
                st["price"] = seed_price(cfg, rows, stamp)
                print(f"first run price: state only, no alert: {st['price']}")
        elif rows:
            use = [r for r in rows if r["day"] != today] if price_err else rows
            pev = evaluate_price(cfg, st["price"], use, today)
            out.extend(price_messages(cfg, pev, stamp, "레짐 참고는 아래"))
            print(f"price: {st['price']} events={len(pev)}")
    except Exception as exc:  # noqa: BLE001
        price_err = f"KRX 정규장 일별 조회 실패: {str(exc)[:160]}. 가격 판정 건너뜀"

    ctx, spread, sp_day, reg_err = ctx_refs(cfg, bars_fn=bars_fn, meta_fn=meta_fn)
    print(f"ctx: {ctx}")
    if "regime" not in st:
        st["regime"] = {}
    rs = st["regime"]
    close = (st.get("price") or {}).get("last_close")
    if rows:
        close = rows[-1]["close"]
    if spread is not None and sp_day is not None:
        # advance last_day only once per US day; seed zone=up if empty and spread already in UP
        if not rs.get("zone"):
            if spread >= float(cfg["regime"]["up_enter"]):
                seed_z = "up"
            elif spread <= float(cfg["regime"]["down_enter"]):
                seed_z = "down"
            else:
                seed_z = "mid"
            evaluate_regime(cfg, rs, spread, sp_day, force_seed_zone=seed_z)
            print(f"first run regime seed zone={rs['zone']} spread={spread:+.2f} (no send)")
        elif rs.get("last_day") != sp_day.isoformat():
            rev = evaluate_regime(cfg, rs, spread, sp_day)
            if rev:
                # patch ctx into previously built price msgs? rebuild regime msg with full ctx
                out.append(regime_messages(cfg, rev, stamp, ctx, close))
            print(f"regime: {rs} event={rev}")
        else:
            rs["last_spread"] = round(spread, 2)
            print(f"regime same day: {rs}")
        st["regime_fail_streak"] = 0
    else:
        print("regime skipped:", reg_err)
        st["regime_fail_streak"] = int(st.get("regime_fail_streak") or 0) + 1

    # rewrite price message ctx with real ctx (they were built with placeholder)
    if out and ctx:
        fixed = []
        for t, m, pri in out:
            if "레짐 참고는 아래" in m:
                m = m.replace("레짐 참고는 아래", ctx)
            fixed.append((t, m, pri))
        out = fixed

    ops = None
    need_ops = False
    ops_why = []
    if price_err:
        print("ERR price:", price_err)
        need_ops = True
        ops_why.append(price_err)
    if reg_err and int(st.get("regime_fail_streak") or 0) >= 2:
        need_ops = True
        ops_why.append(reg_err + f" (연속 {st['regime_fail_streak']}회)")
    if need_ops and st.get("ops_day") != today:
        ops = ops_msg(stamp, " ".join(ops_why) + ". state 는 그대로.")
        st["ops_day"] = today

    for t, m, pri in out:
        check_text(t, m)
        print(f"TITLE: {t}\nPRIORITY: {pri} TOPIC: {route_topic(topic, t)}\n{m}\n")
    if ops:
        print(f"OPS TITLE: {ops[0]} -> {route_topic(topic, ops[0])}\n{ops[1]}\n")
    if not out and not ops:
        print("보낼 것 없음")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    for t, m, pri in out:
        tags = ["chart_with_downwards_trend"] if ("하락" in t or "매수" in t or "확대" in t) else ["chart_with_upwards_trend"]
        if "축소" in t or "하락" in t:
            tags = ["chart_with_downwards_trend"]
        pub(topic, t, m, pri, tags, server, config_name=NAME)
    if ops:
        try:
            pub(resolve_topic("ops"), ops[0], ops[1], 3, ["warning"], server, config_name=NAME)
        except Exception as exc:  # noqa: BLE001
            print(f"ops notice failed: {exc}")
    st["updated_at"] = stamp
    if state is None:
        save_state(st)
    return 0


def selftest() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    nopub = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no pub"))  # noqa: E731

    # --- price: seed between lines -> all armed
    hist = [{"day": f"2026-10-{i + 1:02d}", "close": 30000.0} for i in range(20)]
    hist.append({"day": "2026-11-05", "close": 31585.0})
    ps = seed_price(cfg, hist, "T")
    assert ps["armed"] == {"buy1": True, "buy2": True, "trim": True}, ps
    assert ps["last_close"] == 31585.0

    # committed seed + normal next day in range -> no events
    st0 = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    p0 = json.loads(json.dumps(st0["price"]))
    rows0 = hist[:-1] + [{"day": "2026-11-05", "close": 31585.0}, {"day": "2026-11-06", "close": 31200.0}]
    assert evaluate_price(cfg, p0, rows0, "2026-11-06") == [] and p0["last_date"] == "2026-11-06", p0

    # triggers + rearm
    ps2 = seed_price(cfg, [{"day": "2026-11-05", "close": 31585.0}], "T")
    seq = [
        {"day": "2026-11-06", "close": 29100.0},  # silent
        {"day": "2026-11-07", "close": 29000.0},  # buy1
        {"day": "2026-11-08", "close": 28600.0},  # buy2
        {"day": "2026-11-09", "close": 29870.0},  # rearm buy1
        {"day": "2026-11-10", "close": 29000.0},  # buy1 again
        {"day": "2026-11-11", "close": 29458.0},  # rearm buy2
        {"day": "2026-11-12", "close": 32800.0},  # trim
        {"day": "2026-11-13", "close": 31816.0},  # rearm trim
        {"day": "2026-11-14", "close": 32800.0},  # trim again
    ]
    ev = evaluate_price(cfg, ps2, [{"day": "2026-11-05", "close": 31585.0}] + seq, "2026-11-14")
    got = [(e["key"], e["day"]) for e in ev]
    expect = [
        ("buy1", "2026-11-07"),
        ("buy2", "2026-11-08"),
        ("buy1", "2026-11-10"),
        ("trim", "2026-11-12"),
        ("trim", "2026-11-14"),
    ]
    assert got == expect, got
    assert ps2["armed"] == {"buy1": True, "buy2": True, "trim": False}, ps2  # 29870/32800 also rearm buy2/buy1

    msgs = price_messages(cfg, ev, "T", "MOAT-QQQ 3M +1.00%p / RSP-SPY 3M +0.50%p(참고) / 원/달러 1,340.0(참고, 환율 선 아님)")
    assert msgs[0][0] == "[309230] 1차 29,000원 이하 분할 매수 검토" and msgs[0][2] == 4, msgs[0]
    assert msgs[1][0] == "[309230] 2차 28,600원 이하 비중 확대 검토" and msgs[1][2] == 5
    assert msgs[3][0] == "[309230] 32,800원 이상 일부 축소 익절 검토" and msgs[3][2] == 3
    for t, m, _ in msgs:
        check_text(t, m)
        assert "매매 지시 아님" in m and "309230" in m, m
        assert route_topic("cwally-port", t) == "cwally-port", t

    # --- regime hysteresis
    rs: dict = {"zone": "up", "last_spread": 9.83, "last_day": "2026-11-04"}
    # still up, no event
    assert evaluate_regime(cfg, rs, 5.0, date(2026, 11, 5)) is None and rs["zone"] == "up"
    # reset to mid
    assert evaluate_regime(cfg, rs, 0.5, date(2026, 11, 6)) is None and rs["zone"] == "mid"
    # enter up
    evu = evaluate_regime(cfg, rs, 3.0, date(2026, 11, 7))
    assert evu and evu["key"] == "reg_up" and rs["zone"] == "up", evu
    # reset mid then enter down
    assert evaluate_regime(cfg, rs, 0.0, date(2026, 11, 8)) is None and rs["zone"] == "mid"
    evd = evaluate_regime(cfg, rs, -3.0, date(2026, 11, 9))
    assert evd and evd["key"] == "reg_down" and rs["zone"] == "down", evd
    # seed force no send
    rs2: dict = {}
    assert evaluate_regime(cfg, rs2, 9.8, date(2026, 11, 4), force_seed_zone="up") is None
    assert rs2["zone"] == "up"

    tu, mu, pu = regime_messages(cfg, evu, "T", "ctx", 31585.0)
    td, md, pd_ = regime_messages(cfg, evd, "T", "ctx", 31585.0)
    assert pu == 3 and "매력 상승" in tu and "309230" in tu
    assert pd_ == 4 and "매력 하락" in td
    for t, m in ((tu, mu), (td, md), ops_msg("T", "x"), install_message("T")):
        check_text(t, m)
    assert route_topic("cwally-port", ops_msg("T", "x")[0]) == "cwally-ops"
    assert route_topic("cwally-port", install_message("T")[0]) == "cwally-port"

    # seeded state path: dry/auto with current close sends nothing
    before = STATE_PATH.read_text(encoding="utf-8")
    fake_rows = lambda _c: hist  # noqa: E731

    def fake_bars(sym: str):
        # ~90 weekdays ending 2026-11-04; MOAT outperforms so spread stays high (seeded UP)
        out = []
        d = date(2026, 6, 1)
        i = 0
        while d <= date(2026, 11, 4):
            if d.weekday() < 5:
                if sym == "MOAT":
                    px = 100.0 + i * 0.35
                elif sym == "QQQ":
                    px = 100.0 + i * 0.05
                elif sym == "RSP":
                    px = 100.0 + i * 0.18
                else:
                    px = 100.0 + i * 0.12
                out.append((d, px))
                i += 1
            d += timedelta(days=1)
        assert len(out) > 64, len(out)
        return out

    fake_meta = lambda _t: {"regularMarketPrice": 1340.0}  # noqa: E731
    assert run(
        "dry",
        datetime(2026, 11, 5, 19, 0, tzinfo=KST),
        cfg,
        pub=nopub,
        rows_fn=fake_rows,
        bars_fn=fake_bars,
        meta_fn=fake_meta,
    ) == 0
    sent: list = []
    mem = json.loads(before)
    assert run(
        "auto",
        datetime(2026, 11, 5, 19, 0, tzinfo=KST),
        cfg,
        pub=lambda *a, **k: sent.append(a[1]),
        rows_fn=fake_rows,
        bars_fn=fake_bars,
        meta_fn=fake_meta,
        state=mem,
    ) == 0
    assert sent == [], sent  # seed no-send
    assert mem["price"]["last_date"] == "2026-11-05"
    assert mem["regime"]["zone"] == "up"
    assert STATE_PATH.read_text(encoding="utf-8") == before

    # empty state seeds silently
    mem2: dict = {}
    assert run(
        "auto",
        datetime(2026, 11, 5, 19, 0, tzinfo=KST),
        cfg,
        pub=lambda *a, **k: sent.append(a[1]),
        rows_fn=fake_rows,
        bars_fn=fake_bars,
        meta_fn=fake_meta,
        state=mem2,
    ) == 0
    assert sent == [] and mem2["price"]["last_date"] == "2026-11-05" and mem2["regime"]["zone"] == "up"

    print("selftest ok")
    return 0


def main() -> int:
    args = sys.argv[1:]
    mode = (args[0] if args and not args[0].startswith("--") else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry", "install"}:
        print(f"usage: {NAME}_monitor.py <auto|dry|install|selftest> [--as-of 'YYYY-MM-DD HH:MM']")
        return 2
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    if "--as-of" in args:
        if mode != "dry":
            print("--as-of 는 dry 에서만")
            return 2
        now = datetime.strptime(args[args.index("--as-of") + 1], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    try:
        return run(mode, now, cfg)
    except Exception as exc:  # noqa: BLE001
        print(traceback.format_exc(), file=sys.stderr)
        if mode != "auto":
            return 1
        try:
            t, m = ops_msg(
                now.strftime("%Y-%m-%d %H:%M KST"),
                f"스크립트 오류 {type(exc).__name__}: {str(exc)[:200]}. state 는 그대로.",
            )
            publish(resolve_topic("ops"), t, m, 3, ["warning"], resolve_server(cfg.get("ntfy_server")), config_name=NAME)
        except Exception:  # noqa: BLE001
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
