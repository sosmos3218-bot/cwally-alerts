#!/usr/bin/env python3
"""[245340] TIGER 미국다우존스30 주간 추가 편입 검토. 매수 신호 아님.

KRX 거래일 19:00 k203780 슬롯. ISO 주마다 유효한 판단 1번(그 주에 아직 없으면 다음 거래일이 따라잡음).
휴장일은 침묵. 새 cron 없음.

조건(전부 엄격한 초과):
  1) DIA 수정 총수익(Yahoo adjclose, 분할+분배)이 63 공통 거래일에서 SPY와 SCHD를 각각 이김
  2) 같은 정의의 126 공통 거래일에서도 둘 다 이김
  3) DIA 분할수정 보통 종가(Yahoo close, 배당 수정 아님)가 200거래일 단순평균보다 위
  4) 서로 다른 ISO 주 2번 연속으로 위 조건이 유효하게 참
같으면 미충족. 같은 주는 두 번 세지 않음. 무효이거나 빠진 주는 2주를 이어 붙이지 않음.
참인 동안 반복 발송 없음. 유효한 주에서 조건이 깨져야 재무장.
adjclose가 없으면 무수정 종가로 총수익을 대체하지 않고 그 실행은 무효.
245340 종가/NAV/환율은 알림 후보일 때만 붙이고, 없어도 상대성과 알림은 유지.

modes: auto | dry(발송 없음, state 안 씀) | selftest(네트워크 없음)
"""
from __future__ import annotations

import copy
import json
import math
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402
from kr_calendar import KST, is_trading_day, session_day  # noqa: E402

NAME = "k245340"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
ET = ZoneInfo("America/New_York")
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
TAG = "[245340]"
CODE = "245340"
NAME_KR = "TIGER 미국다우존스30"
BANNED = ("\u00b7", "\u2014")
SYMS = ("DIA", "SPY", "SCHD")


def default_state() -> dict:
    return {
        "updated_at": None,
        "seed": "pending",
        "seed_note": "검증된 2주 연속 기준이 없어 대기. 참인 첫 유효 주만 1회차. 거짓이거나 무효인 주는 세지 않음. 소급 발송 없음.",
        "last_eval_week": None,
        "streak": 0,
        "last_true_week": None,
        "latched": False,
        "last_signal_week": None,
        "signal_weeks": None,
        "fail": {"days": 0, "last": None, "notified": False},
        "last_check": None,
    }


def load_state(path: Path) -> dict:
    st = default_state()
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                st.update(raw)
        except json.JSONDecodeError:
            pass
    if not isinstance(st.get("fail"), dict):
        st["fail"] = {"days": 0, "last": None, "notified": False}
    return st


def save_state(path: Path, st: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def http_json(url: str, timeout: int = 25) -> dict:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _num(v) -> float | None:
    if v is None:
        return None
    try:
        x = float(str(v).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


# ---------------- 달력 ----------------
def iso_week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def prev_iso_week(week: str) -> str:
    y, w = week.split("-W")
    monday = date.fromisocalendar(int(y), int(w), 1)
    prev = monday - timedelta(days=7)
    return iso_week(prev)


def expected_us_session(now: datetime) -> date:
    """Last completed US regular session (16:00 America/New_York). Weekends roll back.

    19:00 KST is 05:00 EST or 06:00 EDT, before the US cash close, so the session
    is the previous NYSE weekday. US holidays are not listed: a bar up to
    stale_calendar_days older than this date is still fresh.
    """
    et = now.astimezone(ET)
    d = et.date()
    if (et.hour, et.minute) < (16, 0):
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def asof_label(session: date) -> str:
    close_et = datetime.combine(session, time(16, 0), tzinfo=ET)
    kst = close_et.astimezone(KST)
    return (
        f"기준: 미국 정규장 {session.isoformat()} 16:00 ET"
        f" ({kst.strftime('%Y-%m-%d %H:%M')} KST) 확정 일봉"
    )


def ended(cfg: dict, now: datetime) -> bool:
    end = datetime.strptime(cfg["ends_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    return now.astimezone(KST) > end


# ---------------- 시세 정의 ----------------
def parse_chart(payload: dict) -> dict:
    """Yahoo chart v8 -> dates, raw close, adjclose, splits.

    adjclose is required. A missing or empty adjclose raises. A null adjclose
    on one bar drops that bar. Close is never copied in as a total-return substitute.
    """
    res = payload["chart"]["result"][0]
    ts = res.get("timestamp") or []
    quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    adj_block = (res.get("indicators") or {}).get("adjclose") or []
    if not adj_block or not isinstance(adj_block, list) or not (adj_block[0] or {}).get("adjclose"):
        raise RuntimeError("adjclose 없음")
    adjs = adj_block[0]["adjclose"]
    if len(adjs) != len(ts) or len(closes) != len(ts):
        raise RuntimeError("시계열 길이 불일치")
    by: dict[date, tuple[float, float]] = {}
    for t, c, a in zip(ts, closes, adjs):
        if c is None or a is None:
            continue
        cf, af = float(c), float(a)
        if not (math.isfinite(cf) and math.isfinite(af) and cf > 0 and af > 0):
            continue
        by[datetime.fromtimestamp(int(t), tz=ET).date()] = (cf, af)
    if len(by) < 2:
        raise RuntimeError("유효 봉 부족")
    dates = sorted(by)
    splits = []
    for sp in ((res.get("events") or {}).get("splits") or {}).values():
        num, den = float(sp["numerator"]), float(sp["denominator"])
        if num <= 0 or den <= 0:
            raise RuntimeError("분할 비율 오류")
        splits.append(
            {
                "date": datetime.fromtimestamp(int(sp["date"]), tz=ET).date(),
                "numerator": num,
                "denominator": den,
            }
        )
    return {
        "dates": dates,
        "close": [by[d][0] for d in dates],
        "adj": [by[d][1] for d in dates],
        "splits": splits,
    }


def _rel(a: float, b: float) -> float:
    return abs(math.log(a / b))


def apply_splits(dates: list[date], closes: list[float], splits: list[dict]) -> list[float]:
    """Split-adjusted ordinary closes (dividends stay out).

    Yahoo quote.close is already split-adjusted (NVDA 10:1 on 2024-06-10 stayed
    continuous). No splits in range: return close unchanged. A split whose jump
    matches the raw ratio is adjusted backward. A jump that matches neither the
    raw ratio nor 'already adjusted' raises, so SMA is not guessed.
    """
    if len(dates) != len(closes):
        raise RuntimeError("분할 길이 불일치")
    if not splits or len(dates) < 2:
        return list(closes)
    raw_at: list[tuple[int, float]] = []
    for sp in splits:
        ratio = sp["numerator"] / sp["denominator"]
        if ratio == 1:
            continue
        idx = next((i for i, d in enumerate(dates) if d >= sp["date"]), None)
        if idx is None or idx == 0:
            continue  # entirely after, or before this window
        if dates[idx] > sp["date"] + timedelta(days=4):
            continue
        raw_at.append((idx, ratio))
    if not raw_at:
        return list(closes)
    # one classification per ex-date index
    seen: dict[int, str] = {}
    ratios: dict[int, float] = {}
    for idx, ratio in raw_at:
        jump = closes[idx] / closes[idx - 1]
        if jump <= 0:
            raise RuntimeError("분할 수정 구분 불가")
        raw_jump = 1.0 / ratio
        r_raw, r_one = _rel(jump, raw_jump), _rel(jump, 1.0)
        if r_raw < 0.25 and r_raw < r_one:
            kind = "raw"
        elif r_one < 0.35 and r_one < r_raw:
            kind = "adj"
        else:
            raise RuntimeError("분할 수정 구분 불가")
        prev = seen.get(idx)
        if prev and prev != kind:
            raise RuntimeError("분할 수정 구분 불가")
        seen[idx] = kind
        ratios[idx] = ratio
    if any(k == "raw" for k in seen.values()) and any(k == "adj" for k in seen.values()):
        raise RuntimeError("분할 수정 구분 불가")
    if all(k == "adj" for k in seen.values()):
        return list(closes)
    factor = 1.0
    out = [0.0] * len(closes)
    for i in range(len(closes) - 1, -1, -1):
        out[i] = closes[i] * factor
        if seen.get(i) == "raw":
            factor *= 1.0 / ratios[i]
    return out


def dividend_adjusted(px: list[float], adj: list[float], cfg: dict) -> bool:
    """True when adj is dividend-adjusted on the same split basis as px.

    Yahoo rebases adjclose so the latest adj equals the latest close, and older
    adjclose is lower by distributions. Identical series is not a total return.
    """
    rules = cfg["rules"]
    if len(px) < 2 or len(px) != len(adj):
        return False
    if min(px[0], px[-1], adj[0], adj[-1]) <= 0:
        return False
    if abs(adj[-1] / px[-1] - 1.0) > float(rules["dividend_last_ratio_tol"]):
        return False
    return (adj[0] / px[0]) < float(rules["dividend_old_ratio_max"])


def align(frames: dict[str, list[tuple[date, float, float]]], expected: date) -> list[dict]:
    """Inner-join sessions. Drop dates after the expected completed session and non-finite rows."""
    maps: dict[str, dict[date, tuple[float, float]]] = {}
    for sym, rows in frames.items():
        m: dict[date, tuple[float, float]] = {}
        for d, px, adj in rows:
            if d > expected:
                continue
            if not (math.isfinite(px) and math.isfinite(adj) and px > 0 and adj > 0):
                continue
            m[d] = (px, adj)
        maps[sym] = m
    if not maps:
        return []
    common = set.intersection(*(set(m) for m in maps.values())) if maps else set()
    out = []
    for d in sorted(common):
        row: dict = {"date": d}
        for sym, m in maps.items():
            px, adj = m[d]
            row[sym] = {"px": px, "adj": adj}
        out.append(row)
    return out


def evaluate_rows(rows: list[dict], cfg: dict, expected: date) -> dict:
    rules = cfg["rules"]
    if not rows:
        return {"valid": False, "reason": "공통 거래일 없음"}
    last = rows[-1]["date"]
    if last > expected:
        return {"valid": False, "reason": "미래 봉"}
    slack = int(rules["stale_calendar_days"])
    if (expected - last).days > slack:
        return {"valid": False, "reason": f"오래됨 {last.isoformat()} 기대 {expected.isoformat()}"}
    need = int(rules["min_sessions"])
    if len(rows) < need:
        return {"valid": False, "reason": f"공통 거래일 {len(rows)}개, 필요 {need}"}
    n3 = int(rules["ret_3m_sessions"])
    n6 = int(rules["ret_6m_sessions"])
    sma_n = int(rules["sma_sessions"])
    if len(rows) <= max(n3, n6, sma_n):
        return {"valid": False, "reason": "창보다 짧은 시계열"}

    def ret(sym: str, n: int) -> float:
        a1 = rows[-1][sym]["adj"]
        a0 = rows[-1 - n][sym]["adj"]
        if a0 <= 0 or a1 <= 0 or not (math.isfinite(a0) and math.isfinite(a1)):
            raise RuntimeError("수정 종가 오류")
        return a1 / a0 - 1.0

    rets: dict[str, dict] = {}
    flags: dict[str, bool] = {}
    for n, name in ((n3, "m3"), (n6, "m6")):
        block = {sym: ret(sym, n) for sym in SYMS}
        block["base"] = rows[-1 - n]["date"].isoformat()
        rets[str(n)] = block
        flags[f"{name}_spy"] = block["DIA"] > block["SPY"]
        flags[f"{name}_schd"] = block["DIA"] > block["SCHD"]
    px = [r["DIA"]["px"] for r in rows[-sma_n:]]
    if any((not math.isfinite(v)) or v <= 0 for v in px):
        return {"valid": False, "reason": "200일 종가 오류"}
    sma = sum(px) / sma_n
    last_px = px[-1]
    above = last_px > sma
    flags["sma"] = above
    cond = all(flags.values())
    return {
        "valid": True,
        "reason": "",
        "cond": cond,
        "asof": last.isoformat(),
        "n": len(rows),
        "ret": rets,
        "sma": {"px": last_px, "sma": sma, "above": above, "n": sma_n},
        "flags": flags,
    }


def frames_from_parsed(parsed_by_sym: dict[str, dict], cfg: dict) -> dict[str, list[tuple[date, float, float]]]:
    frames = {}
    for sym, parsed in parsed_by_sym.items():
        ordinary = apply_splits(parsed["dates"], parsed["close"], parsed["splits"])
        if not dividend_adjusted(ordinary, parsed["adj"], cfg):
            raise RuntimeError(f"{sym} 배당 수정 시계열 없음")
        frames[sym] = list(zip(parsed["dates"], ordinary, parsed["adj"]))
    return frames


# ---------------- 주간 상태 ----------------
def advance(st: dict, week: str, cond: bool) -> bool:
    """Record one valid weekly check. Return True only on the 2-week edge.

    Caller must not call this twice for the same ISO week.
    """
    st["last_eval_week"] = week
    if not cond:
        st["streak"] = 0
        st["last_true_week"] = None
        st["latched"] = False
        st["signal_weeks"] = None
        return False
    prev = prev_iso_week(week)
    if st.get("last_true_week") == prev and int(st.get("streak") or 0) >= 1:
        st["streak"] = int(st["streak"]) + 1
    else:
        st["streak"] = 1
    st["last_true_week"] = week
    if int(st["streak"]) >= 2 and not st.get("latched"):
        st["latched"] = True
        st["last_signal_week"] = week
        st["signal_weeks"] = [prev, week]
        return True
    return False


def note_fail(st: dict, today: str, cfg: dict) -> bool:
    """Count a failed attempt. True once when consecutive failures hit the threshold."""
    f = st.setdefault("fail", {"days": 0, "last": None, "notified": False})
    gap_days = int(cfg["rules"]["fail_gap_reset_days"])
    need = int(cfg["rules"]["fail_ops_after_days"])
    if f.get("last") != today:
        last = f.get("last")
        gap = False
        if last:
            delta = (date.fromisoformat(today) - date.fromisoformat(last)).days
            gap = delta > gap_days
        if gap or not last:
            f["days"] = 1
            if gap:
                f["notified"] = False
        else:
            f["days"] = int(f.get("days") or 0) + 1
        f["last"] = today
    if int(f["days"]) >= need and not f.get("notified"):
        f["notified"] = True
        return True
    return False


def note_ok(st: dict) -> None:
    st["fail"] = {"days": 0, "last": None, "notified": False}


# ---------------- 본문 ----------------
def _pct(x: float) -> str:
    return f"{x * 100:+.2f}%"


def _pp(x: float) -> str:
    return f"{x * 100:+.2f}%p"


def _krw(x: float) -> str:
    return f"{x:,.0f}원" if abs(x - round(x)) < 0.05 else f"{x:,.2f}원"


def body(stamp: str, status: str, action: str, later: str, avoid: str) -> str:
    lines = [
        f"{stamp} | {CODE} | {NAME_KR}",
        "이게 뭐 알림: 245340(TIGER 미국다우존스30)을 들고 있지 않은 상태에서, "
        "다우 DIA 수정 총수익이 S&P500 SPY와 배당 SCHD를 약 3개월(63 공통 거래일)과 "
        "약 6개월(126 공통 거래일) 모두 이기고, DIA 분할수정 종가가 200거래일 평균보다 위인 주가 "
        "서로 다른 2주 연속일 때만 오는 검토 알림. 매수 지시 아님.",
        f"지금 결과/상태: {status}",
        f"지금 할 일: {action}",
        f"나중에: {later}",
        "익절/줄일 때: 이번 알림은 줄이라는 뜻이 아님. 이미 가진 지수, 배당 ETF와 종목이 겹치면 그 비중 안에서만 따로 판단.",
        f"하지 말 것: {avoid}",
    ]
    return "\n".join(lines)


def review_text(metrics: dict, weeks: list[str], ctx: dict | None) -> tuple[str, str, str, str]:
    n3 = "63"
    n6 = "126"
    # keys are the configured session counts; callers pass metrics from evaluate_rows
    keys = [k for k in metrics["ret"] if k != "base"]
    keys.sort(key=lambda s: int(s))
    n3, n6 = keys[0], keys[1]
    r3, r6 = metrics["ret"][n3], metrics["ret"][n6]
    sma = metrics["sma"]
    asof = date.fromisoformat(metrics["asof"])

    def block(n: str, r: dict) -> str:
        return (
            f"{n} 공통 거래일(시작 {r['base']}): "
            f"DIA {_pct(r['DIA'])}, SPY {_pct(r['SPY'])}, SCHD {_pct(r['SCHD'])}. "
            f"DIA-SPY {_pp(r['DIA'] - r['SPY'])}, DIA-SCHD {_pp(r['DIA'] - r['SCHD'])}."
        )

    trend = "위" if sma["above"] else "아래 또는 같음"
    status = (
        f"{asof_label(asof)}. "
        f"{block(n3, r3)} {block(n6, r6)} "
        f"200거래일 추세: DIA 분할수정 종가 {sma['px']:.2f}달러, "
        f"단순평균 {sma['sma']:.2f}달러, 평균 {trend}. "
        f"2주 확인: {weeks[0]} 와 {weeks[1]} 연속 유효. "
        "수익률은 배당과 분할이 반영된 수정 종가 비율이고, 200일선은 배당을 빼지 않은 분할수정 종가의 평균이다. "
        "둘을 섞어 비교하지 않음. "
        + context_sentence(ctx)
    )
    action = "추가 편입 검토, 매수 신호/저평가 판정 아님. 수량이나 보유 교체는 정하지 않음."
    later = "조건이 유효한 주에 깨지면 다시 무장된다. 그 뒤 서로 다른 2주가 연속으로 다시 참일 때만 또 알림. 참인 동안은 매주 다시 보내지 않음."
    avoid = (
        "이 알림만 보고 매수 수량을 정하거나 보유 상품을 245340으로 바꾸기. "
        "저평가라고 읽기. DIA, SPY, SCHD 달러 수익을 245340 원화 수익으로 보기. "
        "겹치는 보유, 환율, 보수, 괴리가 판단을 바꿀 수 있음. 앞으로 손실이 날 수 있음."
    )
    return status, action, later, avoid


def context_sentence(ctx: dict | None) -> str:
    """Optional 245340 close / NAV / FX. Missing pieces stay explicit and do not drop the signal."""
    if not ctx:
        return "245340 정규장 종가 확인 불가. NAV 확인 불가. 원달러 확인 불가. 판정은 미국 대리지표만 사용."
    bits = []
    close = ctx.get("close")
    if close and close.get("px"):
        bits.append(f"245340 정규장 종가 {_krw(float(close['px']))} ({close.get('day')}, krx_close)")
    else:
        bits.append("245340 정규장 종가 확인 불가")
    nav = ctx.get("nav")
    if nav and nav.get("nav"):
        extra = ""
        if nav.get("dev") is not None:
            extra = f", 네이버 괴리 {float(nav['dev']):+.2f}%p"
        bits.append(f"NAV {_krw(float(nav['nav']))} (네이버, {nav.get('note') or '날짜 미확인'}{extra})")
    else:
        bits.append("NAV 확인 불가")
    fx = ctx.get("fx")
    if fx and fx.get("px"):
        stale = " 오래됨" if fx.get("stale") else ""
        bits.append(f"원달러 {float(fx['px']):,.2f}원 ({fx.get('day')}, Yahoo USDKRW=X{stale})")
    else:
        bits.append("원달러 확인 불가")
    return (
        "참고(판정에 안 씀): "
        + ". ".join(bits)
        + ". 이 값은 245340 원화 수익이 아니고 DIA, SPY, SCHD 달러 수정 총수익의 대리지표다."
    )


def ops_message(stamp: str, days: int, reason: str) -> tuple[str, str, int]:
    title = f"{TAG} 미국 ETF 시계열 조회 실패 | {days}일 연속, 이번 주 판단 건너뜀"
    msg = body(
        stamp,
        f"DIA, SPY, SCHD 시계열을 {days}일 연속 읽지 못했다({reason}). 조건 미충족이 아님. 그 주는 2주 연속에 넣지 않음.",
        "다음 KRX 거래일 19:00 재시도를 기다리세요.",
        "읽히면 그 주 판단은 한 번만 기록됩니다.",
        "조회 실패를 추가 편입 신호로 읽기.",
    )
    return title, msg, 3


def review_message(stamp: str, metrics: dict, weeks: list[str], ctx: dict | None) -> tuple[str, str, int]:
    status, action, later, avoid = review_text(metrics, weeks, ctx)
    title = f"{TAG} 추가 편입 검토 | 2주 연속, 매수 신호 아님"
    return title, body(stamp, status, action, later, avoid), 3


def _guard_text(title: str, msg: str) -> None:
    blob = title + "\n" + msg
    for ch in BANNED:
        if ch in blob:
            raise RuntimeError(f"banned char {ch!r}")


# ---------------- 라이브 조회 ----------------
def yahoo_chart(symbol: str, range_: str) -> dict:
    errs = []
    enc = urllib.parse.quote(symbol, safe="")
    for host in ("query1", "query2"):
        url = (
            f"https://{host}.finance.yahoo.com/v8/finance/chart/{enc}"
            f"?range={range_}&interval=1d&events=div%7Csplit&includeAdjustedClose=true"
        )
        try:
            return parse_chart(http_json(url))
        except Exception as exc:  # noqa: BLE001
            errs.append(f"{host}: {exc}")
    raise RuntimeError("; ".join(errs) or f"{symbol} empty")


def live_fetch(now: datetime, cfg: dict) -> dict:
    expected = expected_us_session(now)
    parsed = {sym: yahoo_chart(sym, cfg["rules"]["yahoo_range"]) for sym in SYMS}
    frames = frames_from_parsed(parsed, cfg)
    out = evaluate_rows(align(frames, expected), cfg, expected)
    out["expected_us"] = expected.isoformat()
    return out


def live_context(now: datetime, session: date) -> dict:
    ctx: dict = {"close": None, "nav": None, "fx": None}
    try:
        import krx_close

        bars = [(d, p) for d, p in krx_close.session_closes(CODE, 5, now) if d <= session.isoformat()]
        if bars:
            ctx["close"] = {"day": bars[-1][0], "px": float(bars[-1][1])}
    except Exception as exc:  # noqa: BLE001
        print(f"245340 close skipped: {exc}", file=sys.stderr)
    try:
        raw = http_json(f"https://m.stock.naver.com/api/stock/{CODE}/integration")
        k = raw.get("etfKeyIndicator") or {}
        nav = _num(k.get("nav"))
        dev = _num(k.get("deviationRate"))
        if nav and nav > 0:
            ctx["nav"] = {"nav": nav, "dev": dev, "note": "날짜 미확인"}
    except Exception as exc:  # noqa: BLE001
        print(f"245340 nav skipped: {exc}", file=sys.stderr)
    try:
        ctx["fx"] = yahoo_fx(session)
    except Exception as exc:  # noqa: BLE001
        print(f"usdkrw skipped: {exc}", file=sys.stderr)
    return ctx


def yahoo_fx(session: date) -> dict:
    url = "https://query1.finance.yahoo.com/v8/finance/chart/USDKRW=X?interval=1d&range=10d"
    res = http_json(url)["chart"]["result"][0]
    ts = res.get("timestamp") or []
    closes = (res.get("indicators") or {}).get("quote", [{}])[0].get("close") or []
    rows = []
    for t, c in zip(ts, closes):
        if c is None:
            continue
        cf = float(c)
        if not math.isfinite(cf) or cf <= 0:
            continue
        rows.append((datetime.fromtimestamp(int(t), tz=ET).date(), cf))
    if not rows:
        raise RuntimeError("원달러 없음")
    day, px = rows[-1]
    return {"day": day.isoformat(), "px": px, "stale": (session - day).days > 5}


# ---------------- 실행 ----------------
def execute(
    mode: str,
    now: datetime,
    cfg: dict,
    *,
    fetch,
    context,
    publish_fn,
    state_path: Path,
) -> int:
    """dry never publishes and never writes state, including errors, expiry, and seed paths."""
    now = now.astimezone(KST)
    dry = mode == "dry"
    if ended(cfg, now):
        print("ended")
        return 0
    day = session_day(now)
    if not is_trading_day(day):
        print(f"{day.isoformat()} KRX holiday; silent")
        return 0
    week = iso_week(day)
    st = load_state(state_path)
    if st.get("last_eval_week") == week:
        print(f"already checked {week}")
        return 0

    try:
        obs = fetch(now, cfg)
    except Exception as exc:  # noqa: BLE001
        obs = {"valid": False, "reason": str(exc)[:180]}
    if not isinstance(obs, dict) or "valid" not in obs:
        obs = {"valid": False, "reason": "판단 결과 없음"}

    new = copy.deepcopy(st)
    alerts: list[tuple[str, str, int]] = []
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    if not obs.get("valid"):
        reason = str(obs.get("reason") or "조회 실패")[:180]
        print(f"invalid {week}: {reason}", file=sys.stderr)
        if note_fail(new, day.isoformat(), cfg):
            alerts.append(ops_message(stamp, int(new["fail"]["days"]), reason))
        new["last_check"] = {"week": week, "day": day.isoformat(), "valid": False, "reason": reason}
    else:
        note_ok(new)
        fire = advance(new, week, bool(obs.get("cond")))
        summary = {
            "week": week,
            "day": day.isoformat(),
            "valid": True,
            "cond": bool(obs.get("cond")),
            "asof": obs.get("asof"),
            "streak": new.get("streak"),
            "latched": new.get("latched"),
            "flags": obs.get("flags"),
        }
        new["last_check"] = summary
        print("check " + json.dumps(summary, ensure_ascii=False, default=str))
        if fire:
            ctx = None
            try:
                ctx = context(now, day)
            except Exception as exc:  # noqa: BLE001
                print(f"context skipped: {exc}", file=sys.stderr)
            weeks = list(new.get("signal_weeks") or [])
            alerts.append(review_message(stamp, obs, weeks, ctx))

    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    for title, msg, pri in alerts:
        _guard_text(title, msg)
        routed = route_topic(topic, title)
        print(f"TITLE: {title}\nPRIORITY: {pri} TOPIC: {routed}\n{msg}\n")
        if not dry:
            publish_fn(topic, title, msg, pri, ["mag"], server)
    if not alerts:
        print("no alert")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    new["updated_at"] = now.isoformat()
    save_state(state_path, new)
    return 0


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry"}:
        print(f"unknown mode {mode}")
        return 2
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)

    def _publish(topic, title, msg, pri, tags, server):
        publish(topic, title, msg, pri, tags, server, config_name=NAME)

    return execute(
        mode,
        now,
        cfg,
        fetch=live_fetch,
        context=lambda now, day: live_context(now, day),
        publish_fn=_publish,
        state_path=STATE_PATH,
    )


# ---------------- selftest ----------------
def selftest() -> int:
    import urllib.request as ureq

    real_open = ureq.urlopen

    def blocked(*_a, **_k):
        raise RuntimeError("network blocked")

    ureq.urlopen = blocked
    state_before = STATE_PATH.read_bytes()
    fails: list[str] = []
    try:
        return _selftest_body(fails, state_before)
    finally:
        ureq.urlopen = real_open
        if STATE_PATH.read_bytes() != state_before:
            print("FAIL repo state file changed during selftest")
            return 1


def _selftest_body(fails: list[str], state_before: bytes) -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    n_ok = 0

    def check(name, got, want):
        nonlocal n_ok
        ok = got == want
        print(("ok   " if ok else "FAIL ") + f"{name}: got={got!r} want={want!r}")
        if ok:
            n_ok += 1
        else:
            fails.append(name)

    rules = cfg["rules"]
    check("ends", cfg["ends_kst"], "2027-11-07 23:59")
    check("min sessions", rules["min_sessions"], 300)
    check("windows", (rules["ret_3m_sessions"], rules["ret_6m_sessions"], rules["sma_sessions"]), (63, 126, 200))
    check("topic", cfg["ntfy_topic"], "cwally-port")
    seed = json.loads(state_before.decode("utf-8"))
    check(
        "seed pending, no retroactive streak",
        (seed.get("seed"), seed.get("streak"), seed.get("latched"), seed.get("last_eval_week"), seed.get("last_true_week")),
        ("pending", 0, False, None, None),
    )

    # calendar
    mon = date(2026, 11, 9)
    check("iso week Monday", iso_week(mon), iso_week(mon))
    check("week padded", iso_week(mon).split("-W")[1].isdigit() and len(iso_week(mon).split("-W")[1]) == 2, True)
    nxt = date(2026, 11, 16)
    check("next week is prev+1", prev_iso_week(iso_week(nxt)), iso_week(mon))
    check("year boundary prev", prev_iso_week("2027-W01"), iso_week(date.fromisocalendar(2027, 1, 1) - timedelta(days=7)))
    check("Nov 9 trading", is_trading_day(mon), True)
    check("Chuseok holiday", is_trading_day(date(2026, 9, 24)), False)
    check("Sunday holiday", is_trading_day(date(2026, 11, 8)), False)

    # 19:00 KST Monday = 05:00 EST, expected session is previous Friday
    now_mon = datetime(2026, 11, 9, 19, 0, tzinfo=KST)
    check("expected US session Monday 19:00 KST", expected_us_session(now_mon), date(2026, 11, 6))
    # Saturday before the open still sees Friday
    check(
        "expected US session Saturday",
        expected_us_session(datetime(2026, 11, 7, 15, 40, tzinfo=KST)),
        date(2026, 11, 6),
    )
    # Friday 19:00 KST is before Friday's US close, so Thursday
    check(
        "expected US session Friday 19:00 KST",
        expected_us_session(datetime(2026, 11, 6, 19, 0, tzinfo=KST)),
        date(2026, 11, 5),
    )
    check(
        "asof EST label",
        asof_label(date(2026, 11, 6)),
        "기준: 미국 정규장 2026-11-06 16:00 ET (2026-11-07 06:00 KST) 확정 일봉",
    )
    check(
        "asof EDT label",
        asof_label(date(2026, 7, 6)),
        "기준: 미국 정규장 2026-07-06 16:00 ET (2026-07-07 05:00 KST) 확정 일봉",
    )
    # late backup before 06:00 KST still belongs to the previous session day
    late = datetime(2026, 11, 10, 2, 0, tzinfo=KST)
    check("session_day rolls back before 06:00", session_day(late), date(2026, 11, 9))

    # parser: no adjclose, no close fallback
    def chart(rows, splits=None, adj_key=True, null_adj_at=None):
        ts, close, adj = [], [], []
        base = datetime(2024, 1, 2, 14, 30, tzinfo=ZoneInfo("UTC"))
        for i, (c, a) in enumerate(rows):
            ts.append(int((base + timedelta(days=i)).timestamp()))
            close.append(c)
            if null_adj_at is not None and i == null_adj_at:
                adj.append(None)
            else:
                adj.append(a)
        indicators = {"quote": [{"close": close}]}
        if adj_key:
            indicators["adjclose"] = [{"adjclose": adj}]
        events = {}
        if splits:
            events["splits"] = {
                str(i): {
                    "date": int(datetime.combine(d, time(14, 30), tzinfo=ZoneInfo("UTC")).timestamp()),
                    "numerator": num,
                    "denominator": den,
                    "splitRatio": f"{num:g}:{den:g}",
                }
                for i, (d, num, den) in enumerate(splits)
            }
        return {"chart": {"result": [{"timestamp": ts, "indicators": indicators, "events": events}]}}

    try:
        parse_chart(chart([(10, 9), (10, 9)], adj_key=False))
        check("missing adjclose raises", False, True)
    except RuntimeError as exc:
        check("missing adjclose raises", "adjclose" in str(exc), True)

    try:
        parse_chart(chart([(10, None), (11, None)], null_adj_at=0))
        # second bar may still parse; force both null
        parse_chart({"chart": {"result": [{"timestamp": [1, 2], "indicators": {"quote": [{"close": [10, 11]}], "adjclose": [{"adjclose": [None, None]}]}}]}})
        check("all-null adjclose raises", False, True)
    except RuntimeError as exc:
        check("all-null adjclose raises", "adjclose" in str(exc) or "유효" in str(exc), True)

    parsed_drop = parse_chart(chart([(10, 9.0), (11, None), (12, 10.0)], null_adj_at=1))
    check("null adj bar dropped, close not substituted", len(parsed_drop["dates"]), 2)
    check("kept adj is not the raw close", parsed_drop["adj"][0] != parsed_drop["close"][0], True)

    # splits
    d0 = date(2024, 6, 7)
    days = [d0 + timedelta(days=i) for i in range(4)]
    # 10:1 raw jump
    raw_px = [100.0, 110.0, 12.0, 13.0]
    adj_px = apply_splits(days, raw_px, [{"date": days[2], "numerator": 10.0, "denominator": 1.0}])
    check("10:1 raw adjusted", [round(x, 4) for x in adj_px], [10.0, 11.0, 12.0, 13.0])
    already = [10.0, 11.0, 12.0, 13.0]
    check(
        "already adjusted not double counted",
        apply_splits(days, already, [{"date": days[2], "numerator": 10.0, "denominator": 1.0}]),
        already,
    )
    try:
        apply_splits(days, [20.0, 22.0, 15.4, 16.0], [{"date": days[2], "numerator": 2.0, "denominator": 1.0}])
        check("ambiguous split raises", False, True)
    except RuntimeError as exc:
        check("ambiguous split raises", "구분 불가" in str(exc), True)
    two = apply_splits(days, [20.0, 22.0, 11.0, 12.0], [{"date": days[2], "numerator": 2.0, "denominator": 1.0}])
    check("2:1 raw adjusted", [round(x, 4) for x in two], [10.0, 11.0, 11.0, 12.0])

    # dividend gate on split-adjusted ordinary price
    ordinary = [100.0] * 10
    adj_same = [100.0] * 10
    check("identical series is not total return", dividend_adjusted(ordinary, adj_same, cfg), False)
    adj_div = [97.0] + [100.0] * 9
    check("older adj below close passes", dividend_adjusted(ordinary, adj_div, cfg), True)
    # price flat, adj up: total return uses adj
    check("adj return differs from flat price", round(adj_div[-1] / adj_div[0] - 1, 4) != 0, True)

    # alignment: missing day, future day, NaN omitted
    expected = date(2026, 11, 6)
    a = date(2026, 11, 4)
    b = date(2026, 11, 5)
    c = date(2026, 11, 6)
    future = date(2026, 11, 9)
    frames = {
        "DIA": [(a, 1.0, 1.0), (b, 1.0, 1.0), (c, 1.0, 1.0), (future, 9.0, 9.0)],
        "SPY": [(a, 1.0, 1.0), (c, 1.0, 1.0), (future, 9.0, 9.0)],  # missing b
        "SCHD": [(a, 1.0, 1.0), (b, 1.0, 1.0), (c, 1.0, float("nan")), (future, 9.0, 9.0)],
    }
    aligned = align(frames, expected)
    check("align drops missing, NaN, and future", [r["date"] for r in aligned], [a])

    # full rows for the heuristic
    end = date(2026, 11, 6)
    n = int(rules["min_sessions"])

    def make_rows(last=end):
        days = []
        cur = last
        while len(days) < n:
            if cur.weekday() < 5:
                days.append(cur)
            cur -= timedelta(days=1)
        days.reverse()
        rows = []
        for d in days:
            rows.append({s: {"px": 100.0, "adj": 100.0} for s in SYMS} | {"date": d})
        return rows

    def fresh(last=end):
        rows = make_rows(last)
        for sym, last_adj in (("DIA", 120.0), ("SPY", 110.0), ("SCHD", 105.0)):
            rows[-1][sym]["adj"] = last_adj
        rows[-1]["DIA"]["px"] = 101.0
        return rows

    # winning adj, SMA strictly above (last px > 100, other 199 at 100)
    win = fresh()
    m = evaluate_rows(win, cfg, end)
    check("fixture valid", m["valid"], True)
    check("all four gaps and sma", m["cond"], True)
    check(
        "flag names",
        m["flags"],
        {"m3_spy": True, "m3_schd": True, "m6_spy": True, "m6_schd": True, "sma": True},
    )
    # 3m DIA == SPY (not strict), 6m DIA still beats both.
    # by lifting only the 63-day SPY endpoint via base, not the shared last.
    # Shared last means both windows use the same endpoint. Change the -63 base only.
    rows = fresh()
    rows[-1 - 63]["SPY"]["adj"] = 100.0 * 110.0 / 120.0  # SPY 3m = 120%? last 110 / base = DIA's 20%.
    # DIA 3m = 120/100 - 1 = 20%. SPY 3m equals 20% when base = 110/1.20 = 91.666.
    rows[-1 - 63]["SPY"]["adj"] = 110.0 / 1.20
    m = evaluate_rows(rows, cfg, end)
    check("3m DIA==SPY fails strict", (m["valid"], m["flags"]["m3_spy"], m["flags"]["m3_schd"], m["flags"]["m6_spy"], m["flags"]["m6_schd"], m["cond"]), (True, False, True, True, True, False))

    rows = fresh()
    rows[-1 - 63]["SCHD"]["adj"] = 105.0 / 1.20  # SCHD 3m = 20% == DIA
    m = evaluate_rows(rows, cfg, end)
    check("3m DIA==SCHD fails strict", (m["flags"]["m3_spy"], m["flags"]["m3_schd"], m["flags"]["m6_spy"], m["flags"]["m6_schd"], m["cond"]), (True, False, True, True, False))

    rows = fresh()
    # 6m DIA == SPY. DIA 6m is 20%. Set SPY -126 base so SPY 6m = 20% while 3m (base -63 = 100) stays 10%.
    rows[-1 - 126]["SPY"]["adj"] = 110.0 / 1.20
    m = evaluate_rows(rows, cfg, end)
    check("6m DIA==SPY fails strict", (m["flags"]["m3_spy"], m["flags"]["m3_schd"], m["flags"]["m6_spy"], m["flags"]["m6_schd"], m["cond"]), (True, True, False, True, False))

    rows = fresh()
    rows[-1 - 126]["SCHD"]["adj"] = 105.0 / 1.20
    m = evaluate_rows(rows, cfg, end)
    check("6m DIA==SCHD fails strict", (m["flags"]["m3_spy"], m["flags"]["m3_schd"], m["flags"]["m6_spy"], m["flags"]["m6_schd"], m["cond"]), (True, True, True, False, False))

    rows = fresh()
    rows[-1]["DIA"]["px"] = 100.0
    m = evaluate_rows(rows, cfg, end)
    check("SMA equal is not above", (m["sma"]["above"], m["flags"]["sma"], m["cond"]), (False, False, False))
    check("SMA boundary value", round(m["sma"]["sma"], 6), 100.0)

    rows = fresh()
    rows[-1]["DIA"]["px"] = 100.0001
    m = evaluate_rows(rows, cfg, end)
    check("SMA one tick above passes", (m["sma"]["above"], m["cond"]), (True, True))

    # price return would say DIA wins, adj says it does not: must not fall back
    rows = fresh()
    rows[-1]["DIA"]["px"] = 200.0  # price +100%
    rows[-1]["DIA"]["adj"] = 100.0  # adj flat, loses to SPY/SCHD
    rows[-1]["SPY"]["px"] = 100.0
    m = evaluate_rows(rows, cfg, end)
    check("no unadjusted total-return fallback", (m["flags"]["m3_spy"], m["cond"]), (False, False))

    # adj wins while ordinary price does not beat (DIA px lags, adj leads)
    rows = fresh()
    rows[-1]["DIA"]["px"] = 101.0
    rows[-1]["SPY"]["px"] = 180.0  # price return SPY wins
    rows[-1]["SCHD"]["px"] = 180.0
    m = evaluate_rows(rows, cfg, end)
    check("total return uses adj not ordinary price", (m["cond"], m["flags"]["sma"]), (True, True))

    # stale / short
    m = evaluate_rows(fresh(), cfg, end + timedelta(days=10))
    check("stale rejected", m["valid"], False)
    short = fresh()[:299]
    m = evaluate_rows(short, cfg, short[-1]["date"])
    check("short history rejected", m["valid"], False)
    m = evaluate_rows(fresh(), cfg, end)
    check("asof is common last date", m["asof"], end.isoformat())

    # weekly state machine
    def blank():
        return default_state()

    w45, w46, w47 = iso_week(date(2026, 11, 2)), iso_week(date(2026, 11, 9)), iso_week(date(2026, 11, 16))
    check("sample weeks distinct", len({w45, w46, w47}) == 3 and prev_iso_week(w46) == w45 and prev_iso_week(w47) == w46, True)

    st = blank()
    check("week1 true no fire", advance(st, w45, True), False)
    check("week1 streak", (st["streak"], st["latched"], st["last_eval_week"]), (1, False, w45))
    check("week2 true fires once", advance(st, w46, True), True)
    check("latched", (st["streak"], st["latched"], st["signal_weeks"]), (2, True, [w45, w46]))
    check("still true does not repeat", advance(st, w47, True), False)
    check("stays latched", st["latched"], True)

    st = blank()
    advance(st, w45, True)
    check("gap week does not bridge", advance(st, w47, True), False)
    check("gap resets streak to 1", (st["streak"], st["last_true_week"], st["latched"]), (1, w47, False))

    st = blank()
    advance(st, w45, True)
    held = (st["streak"], st["last_true_week"], st["latched"], st["last_eval_week"])
    # invalid week must not be passed to advance; streak unchanged so it cannot bridge
    check("invalid leaves streak untouched", held, (1, w45, False, w45))
    check("after invalid, next week is not 2", advance(st, w47, True), False)
    check("after invalid streak is 1", st["streak"], 1)

    st = blank()
    advance(st, w45, True)
    advance(st, w46, True)
    check("valid false rearms", advance(st, w47, False), False)
    check("rearmed clear", (st["latched"], st["streak"], st["last_true_week"]), (False, 0, None))
    w48 = iso_week(date(2026, 11, 23))
    w49 = iso_week(date(2026, 11, 30))
    check("after rearm first week silent", advance(st, w48, True), False)
    check("after rearm second week fires", advance(st, w49, True), True)

    # same-week duplicate is an execute concern; advance is not called twice.
    # fail counter
    st = blank()
    check("fail day1 silent", note_fail(st, "2026-11-09", cfg), False)
    check("fail same day not double counted", note_fail(st, "2026-11-09", cfg), False)
    check("fail days still 1", st["fail"]["days"], 1)
    check("fail day2 ops once", note_fail(st, "2026-11-10", cfg), True)
    check("fail day3 no second ops", note_fail(st, "2026-11-11", cfg), False)
    note_ok(st)
    check("success resets fail", st["fail"]["days"], 0)
    note_fail(st, "2026-11-09", cfg)
    check("gap resets fail streak", note_fail(st, "2026-11-20", cfg), False)
    check("gap days is 1", st["fail"]["days"], 1)
    title, msg, pri = ops_message("s", 2, "adjclose 없음")
    check("ops title routes ops", route_topic("cwally-port", title), "cwally-ops")
    check("ops title has ticker and failure word", (title.startswith(TAG), "조회 실패" in title), (True, True))

    # message content
    metrics = evaluate_rows(fresh(), cfg, end)
    ctx = {
        "close": {"day": "2026-11-09", "px": 33695.0},
        "nav": {"nav": 33989.08, "dev": 0.52, "note": "날짜 미확인"},
        "fx": {"day": "2026-11-06", "px": 1430.12, "stale": False},
    }
    title, msg, pri = review_message("2026-11-16 19:00 KST", metrics, [w45, w46], ctx)
    check("review title", title, f"{TAG} 추가 편입 검토 | 2주 연속, 매수 신호 아님")
    check("priority review", pri, 3)
    for phrase in (
        "추가 편입 검토, 매수 신호/저평가 판정 아님",
        "63 공통 거래일",
        "126 공통 거래일",
        "DIA",
        "SPY",
        "SCHD",
        "200거래일",
        w45,
        w46,
        "16:00 ET",
        "KST",
        "245340 원화 수익",
        "앞으로 손실이 날 수 있음",
        "환율",
        "보수",
        "괴리",
        "수량",
        "정규장 종가",
        "날짜 미확인",
    ):
        check(f"body has {phrase}", phrase in msg, True)
    check("body order", [ln.split(":")[0] for ln in msg.splitlines()[1:7]], [
        "이게 뭐 알림",
        "지금 결과/상태",
        "지금 할 일",
        "나중에",
        "익절/줄일 때",
        "하지 말 것",
    ])
    check("no banned chars", any(ch in title + msg for ch in BANNED), False)
    check("review stays on port", route_topic("cwally-port", title), "cwally-port")
    # gaps present
    check("relative gap marker", "%p" in msg, True)
    bare = review_message("2026-11-16 19:00 KST", metrics, [w45, w46], None)[1]
    check("missing context is honest", ("종가 확인 불가" in bare and "NAV 확인 불가" in bare and "원달러 확인 불가" in bare), True)

    print("FIXTURE sample (not a live send)")
    print(title)
    print(msg)

    # dry / expiry / holiday / same-week / error do not touch state or publish
    import tempfile

    def run_case(name, mode, now, state, fetch, context, want_pub, want_disk_same, want_streak=None):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "k245340.json"
            save_state(path, copy.deepcopy(state))
            before = path.read_bytes()
            pubs = []

            def pub(*a, **k):
                pubs.append(a)

            def ctx_fn(now, day):
                if context == "raise":
                    raise RuntimeError("context down")
                return context

            rc = execute(mode, now, cfg, fetch=fetch, context=ctx_fn, publish_fn=pub, state_path=path)
            after = path.read_bytes()
            disk_same = after == before
            check(f"{name} rc", rc, 0)
            check(f"{name} publish count", len(pubs), want_pub)
            check(f"{name} disk frozen" if want_disk_same else f"{name} disk written", disk_same, want_disk_same)
            if want_streak is not None and not want_disk_same:
                got = json.loads(after.decode())
                check(f"{name} streak", got.get("streak"), want_streak)
            return pubs

    good_obs = evaluate_rows(fresh(), cfg, expected_us_session(datetime(2026, 11, 9, 19, 0, tzinfo=KST)))
    # Monday 2026-11-09 19:00 expects US session 2026-11-06, and fresh() ends that day.
    check("monday obs asof matches expected", good_obs["asof"], "2026-11-06")

    def fetch_ok(now, cfg_):
        exp = expected_us_session(now)
        return evaluate_rows(fresh(exp), cfg_, exp)

    def fetch_bad(now, cfg_):
        raise RuntimeError("adjclose 없음")

    pending = blank()
    run_case(
        "dry fire path",
        "dry",
        datetime(2026, 11, 16, 19, 0, tzinfo=KST),
        {**pending, "streak": 1, "last_true_week": iso_week(date(2026, 11, 9)), "last_eval_week": iso_week(date(2026, 11, 9))},
        fetch_ok,
        {"close": None, "nav": None, "fx": None},
        0,
        True,
    )
    run_case("dry error", "dry", datetime(2026, 11, 9, 19, 0, tzinfo=KST), pending, fetch_bad, None, 0, True)
    run_case("dry expired", "dry", datetime(2027, 11, 8, 19, 0, tzinfo=KST), pending, fetch_bad, None, 0, True)
    run_case("auto expired no write", "auto", datetime(2027, 11, 8, 19, 0, tzinfo=KST), pending, fetch_bad, None, 0, True)
    run_case("holiday silent", "auto", datetime(2026, 11, 8, 19, 0, tzinfo=KST), pending, fetch_bad, None, 0, True)
    run_case(
        "dry same week",
        "dry",
        datetime(2026, 11, 10, 19, 0, tzinfo=KST),
        {**pending, "last_eval_week": iso_week(date(2026, 11, 9)), "streak": 1},
        fetch_bad,
        None,
        0,
        True,
    )
    run_case(
        "auto week1 writes streak 1 no send",
        "auto",
        datetime(2026, 11, 9, 19, 0, tzinfo=KST),
        pending,
        fetch_ok,
        None,
        0,
        False,
        1,
    )
    # context failure still publishes on the edge, and does not block
    pubs = run_case(
        "auto edge sends even if context fails",
        "auto",
        datetime(2026, 11, 16, 19, 0, tzinfo=KST),
        {**pending, "streak": 1, "last_true_week": iso_week(date(2026, 11, 9)), "last_eval_week": iso_week(date(2026, 11, 9))},
        fetch_ok,
        "raise",
        1,
        False,
        2,
    )
    if pubs:
        check("sent title is review not buy", pubs[0][1], f"{TAG} 추가 편입 검토 | 2주 연속, 매수 신호 아님")
        check("context failure still says unavailable", "확인 불가" in pubs[0][2], True)

    # two autos in the same week do not increment twice
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "k245340.json"
        save_state(path, blank())
        pubs = []

        def pub(*a, **k):
            pubs.append(a)

        execute("auto", datetime(2026, 11, 9, 19, 0, tzinfo=KST), cfg, fetch=fetch_ok, context=lambda *a: None, publish_fn=pub, state_path=path)
        mid = json.loads(path.read_text(encoding="utf-8"))
        calls = {"n": 0}

        def fetch_count(now, cfg_):
            calls["n"] += 1
            return fetch_ok(now, cfg_)

        execute("auto", datetime(2026, 11, 10, 19, 0, tzinfo=KST), cfg, fetch=fetch_count, context=lambda *a: None, publish_fn=pub, state_path=path)
        end_st = json.loads(path.read_text(encoding="utf-8"))
        check("same week no second increment", (mid["streak"], end_st["streak"], calls["n"], len(pubs)), (1, 1, 0, 0))

    # two failing days then a later success in the same week counts as week 1, ops once, dry not involved
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "k245340.json"
        save_state(path, blank())
        pubs = []

        def pub(*a, **k):
            pubs.append(a[1])

        execute("auto", datetime(2026, 11, 9, 19, 0, tzinfo=KST), cfg, fetch=fetch_bad, context=lambda *a: None, publish_fn=pub, state_path=path)
        execute("auto", datetime(2026, 11, 10, 19, 0, tzinfo=KST), cfg, fetch=fetch_bad, context=lambda *a: None, publish_fn=pub, state_path=path)
        execute("auto", datetime(2026, 11, 11, 19, 0, tzinfo=KST), cfg, fetch=fetch_ok, context=lambda *a: None, publish_fn=pub, state_path=path)
        st = json.loads(path.read_text(encoding="utf-8"))
        check("two fails then valid week is streak 1", st["streak"], 1)
        check("ops once", pubs, [f"{TAG} 미국 ETF 시계열 조회 실패 | 2일 연속, 이번 주 판단 건너뜀"])
        check("valid week consumed", st["last_eval_week"], iso_week(date(2026, 11, 11)))
        check("fail reset after valid", st["fail"]["days"], 0)

    check("repo state still pending bytes", STATE_PATH.read_bytes(), state_before)
    print(f"SELFTEST {'PASS' if not fails else 'FAIL'}: {len(fails)} failed, {n_ok} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
