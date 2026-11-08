#!/usr/bin/env python3
"""[PTJ200] SPY 200일선 ±1% 국면 전환 + 계좌별 매도/매수 비율 알림 (2026-11-08 사용자 요청).

TradingView 지표 'PTJ 200일선 ±1% 하이브리드'(PTJ_KR_US_Hybrid v6)와 같은 규칙:
  SPY 일봉 종가 > SMA200 x (1 + band)  -> 공격(BULL)
  SPY 일봉 종가 < SMA200 x (1 - band)  -> 방어(BEAR)
  그 사이                               -> 이전 국면 유지
SMA는 SPY 자체 일봉(Yahoo close, 분할수정, 배당 미수정)으로 계산한다.

평일 09:00 KST morning 슬롯. 새 cron 없음.
첫 실행은 5년 일봉으로 현재 국면만 기록(발송 없음). 이후 state의 마지막 처리일 다음 봉부터
순서대로 적용해 국면이 바뀌면 1번 발송(p4). 매달 첫 유효 실행에 상태 요약 1번(p2).
조회 실패는 fail_ops_after_runs 번 연속일 때 cwally-ops에 하루 1번.

modes: auto | dry(발송 없음, state 안 씀) | install(설치 확인 1회, state 안 씀) | selftest(네트워크 없음)
"""
from __future__ import annotations

import json
import math
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_topic  # noqa: E402
from kr_calendar import KST  # noqa: E402

NAME = "ptj200"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
ET = ZoneInfo("America/New_York")
TAG = "[PTJ200]"
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
BANNED = ("\u00b7", "\u2014")
LABEL = {1: "공격(BULL)", -1: "방어(BEAR)"}


# ---------------- state ----------------
def default_state() -> dict:
    return {
        "updated_at": None,
        "regime": None,
        "last_date": None,
        "last_close": None,
        "last_sma": None,
        "last_switch": None,
        "last_status_month": None,
        "fail": {"runs": 0, "notified_day": None},
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
        st["fail"] = {"runs": 0, "notified_day": None}
    return st


def save_state(path: Path, st: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------------- data ----------------
def http_json(url: str, timeout: int = 25) -> dict:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def expected_us_session(now: datetime) -> date:
    """Last completed US regular session (16:00 ET). Weekends roll back. Holidays are covered by stale_calendar_days."""
    et = now.astimezone(ET)
    d = et.date()
    if (et.hour, et.minute) < (16, 0):
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def parse_chart(payload: dict) -> list[tuple[date, float]]:
    res = payload["chart"]["result"][0]
    ts = res.get("timestamp") or []
    closes = (((res.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
    if len(ts) != len(closes):
        raise RuntimeError("시계열 길이 불일치")
    by: dict[date, float] = {}
    for t, c in zip(ts, closes):
        if c is None:
            continue
        cf = float(c)
        if math.isfinite(cf) and cf > 0:
            by[datetime.fromtimestamp(int(t), tz=ET).date()] = cf
    return sorted(by.items())


def yahoo_daily(symbol: str, range_: str) -> list[tuple[date, float]]:
    errs = []
    enc = urllib.parse.quote(symbol, safe="")
    for host in ("query1", "query2"):
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{enc}?range={range_}&interval=1d"
        try:
            return parse_chart(http_json(url))
        except Exception as exc:  # noqa: BLE001
            errs.append(f"{host}: {exc}")
    raise RuntimeError("; ".join(errs) or f"{symbol} empty")


def completed_bars(bars: list[tuple[date, float]], now: datetime, cfg: dict) -> list[tuple[date, float]]:
    exp = expected_us_session(now)
    out = [b for b in bars if b[0] <= exp]
    rules = cfg["rules"]
    if len(out) < int(rules["min_bars"]):
        raise RuntimeError(f"유효 봉 부족 {len(out)}")
    if (exp - out[-1][0]).days > int(rules["stale_calendar_days"]):
        raise RuntimeError(f"마지막 봉 {out[-1][0]} 이 기대 세션 {exp} 보다 오래됨")
    return out


# ---------------- rule ----------------
def series(bars: list[tuple[date, float]], cfg: dict) -> list[dict]:
    """Rows with SMA, from the first bar that has a full window."""
    n = int(cfg["rules"]["sma_len"])
    rows = []
    s = 0.0
    closes = [c for _, c in bars]
    for i, (d, c) in enumerate(bars):
        s += c
        if i >= n:
            s -= closes[i - n]
        if i >= n - 1:
            rows.append({"date": d, "close": c, "sma": s / n})
    return rows


def step(prev: int, close: float, sma: float, band_pct: float) -> int:
    if close > sma * (1 + band_pct / 100.0):
        return 1
    if close < sma * (1 - band_pct / 100.0):
        return -1
    return prev


def replay(rows: list[dict], start: int, band_pct: float) -> int:
    reg = start
    for r in rows:
        reg = step(reg, r["close"], r["sma"], band_pct)
    return reg


# ---------------- copy ----------------
def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def account_moves(acct: dict, frm: int, to: int, assets: dict) -> list[str]:
    a = acct["bull"] if frm == 1 else acct["bear"]
    b = acct["bull"] if to == 1 else acct["bear"]
    sells, buys = [], []
    for k in list(a) + [k for k in b if k not in a]:
        x, y = float(a.get(k, 0.0)), float(b.get(k, 0.0))
        name = assets.get(k, k)
        if abs(y - x) < 1e-9:
            continue
        if y < x:
            sells.append(f"{name} 전량 매도" if y == 0 else f"{name} {_pct(x)}에서 {_pct(y)}로 ({_pct(x - y)}p 매도)")
        else:
            buys.append(f"{name} {_pct(y)} 매수" if x == 0 else f"{name} {_pct(x)}에서 {_pct(y)}로 ({_pct(y - x)}p 매수)")
    when = "오늘 국내 정규장" if acct.get("market") == "kr" else "오늘 밤 미국 정규장"
    lines = [f"- {acct['label']} ({when})"]
    if sells:
        lines.append("  먼저 매도: " + ", ".join(sells))
    if buys:
        lines.append("  그 돈으로 매수: " + ", ".join(buys))
    if acct.get("note"):
        lines.append(f"  참고: {acct['note']}")
    return lines


def target_line(acct: dict, reg: int, assets: dict) -> str:
    w = acct["bull"] if reg == 1 else acct["bear"]
    return f"- {acct['label']}: " + ", ".join(f"{assets.get(k, k)} {_pct(v)}" for k, v in w.items())


def lines_text(close: float, sma: float, band: float) -> tuple[float, float, float]:
    return sma * (1 + band / 100), sma * (1 - band / 100), (close / sma - 1) * 100


def body(parts: list[tuple[str, str]]) -> str:
    return "\n".join(f"{k}: {v}" for k, v in parts)


def switch_message(cfg: dict, frm: int, to: int, row: dict, flips: int) -> tuple[str, str, int]:
    band = float(cfg["rules"]["band_pct"])
    up, lo, dist = lines_text(row["close"], row["sma"], band)
    assets = cfg["assets"]
    d = row["date"].isoformat()
    if to == -1:
        title = f"{TAG} SPY 200일선 -{band:g}% 이탈 마감 | 방어 전환, 계좌별 비중 변경"
        why = f"SPY {d} 종가 ${row['close']:.2f} 가 방어선 ${lo:.2f}(200일선 ${row['sma']:.2f} -{band:g}%) 아래로 마감. 이격 {dist:+.2f}%."
        nxt = f"공격 복귀는 SPY 종가가 200일선 +{band:g}%(지금 ${up:.2f}) 위로 마감할 때. 그 전엔 방어 비중 유지."
    else:
        title = f"{TAG} SPY 200일선 +{band:g}% 돌파 마감 | 공격 전환, 계좌별 비중 변경"
        why = f"SPY {d} 종가 ${row['close']:.2f} 가 공격선 ${up:.2f}(200일선 ${row['sma']:.2f} +{band:g}%) 위로 마감. 이격 {dist:+.2f}%."
        nxt = f"방어 전환은 SPY 종가가 200일선 -{band:g}%(지금 ${lo:.2f}) 아래로 마감할 때. 그 전엔 공격 비중 유지."
    if flips > 1:
        why += f" 놓친 봉 사이에 국면이 {flips}번 바뀌었고 최종 결과만 알림."
    todo = ["비율은 각 계좌 총액 기준. 자동 주문 아님."]
    for acct in cfg["accounts"]:
        todo += account_moves(acct, frm, to, assets)
    msg = body([
        ("이게 뭐 알림", "PTJ 200일선 ±1% 국면 전환. SPY 종가가 200일선 위아래 1% 밴드를 벗어나 마감할 때만 옴."),
        ("지금 결과", f"{LABEL[frm]}에서 {LABEL[to]}로 전환. {why}"),
        ("지금 할 일", "\n" + "\n".join(todo)),
        ("나중에", nxt),
        ("익절/줄일 때", "이 전략은 국면이 바뀔 때만 비중을 바꿈. 중간 익절 규칙 없음."),
        ("하지 말 것", "장중 이탈만 보고 매매하기(종가 기준). 레버리지(225040, SSO)를 IRP나 연금저축에 넣기. 실제 잔고가 목표와 다르면 위 비율 대신 목표 비중에 맞춰 조정."),
    ])
    return title, msg, 4


def status_message(cfg: dict, reg: int, row: dict, month: str) -> tuple[str, str, int]:
    band = float(cfg["rules"]["band_pct"])
    up, lo, dist = lines_text(row["close"], row["sma"], band)
    assets = cfg["assets"]
    title = f"{TAG} SPY {month} 월간 점검 | {LABEL[reg]} 유지, 목표 비중 확인"
    nxt_line = (f"방어 전환선 ${lo:.2f}(종가 기준)" if reg == 1 else f"공격 전환선 ${up:.2f}(종가 기준)")
    msg = body([
        ("이게 뭐 알림", "PTJ 200일선 ±1% 국면 월 1회 점검. 전환 알림이 아님."),
        ("지금 결과", f"{LABEL[reg]} 유지. SPY {row['date'].isoformat()} 종가 ${row['close']:.2f}, 200일선 ${row['sma']:.2f}, 이격 {dist:+.2f}%. 다음 {nxt_line}."),
        ("지금 할 일", "실제 비중이 아래 목표와 크게 어긋났으면 맞추기(계좌 총액 기준).\n" + "\n".join(target_line(a, reg, assets) for a in cfg["accounts"])),
        ("나중에", "국면이 바뀌면 그날 아침 9시 전환 알림이 따로 옴."),
        ("익절/줄일 때", "국면 전환 때만. 중간 익절 규칙 없음."),
        ("하지 말 것", "이 점검을 매수나 매도 신호로 읽기."),
    ])
    return title, msg, 2


def install_message(cfg: dict, reg: int | None, row: dict | None) -> tuple[str, str, int]:
    if row and reg is not None:
        band = float(cfg["rules"]["band_pct"])
        _, _, dist = lines_text(row["close"], row["sma"], band)
        now_txt = f"지금 국면 {LABEL[reg]}. SPY {row['date'].isoformat()} 종가 ${row['close']:.2f}, 200일선 ${row['sma']:.2f}, 이격 {dist:+.2f}%."
    else:
        now_txt = "지금 국면은 첫 정규 실행 때 기록."
    title = f"{TAG} SPY 설치 확인 | 매매 신호 아님"
    msg = body([
        ("이게 뭐 알림", "PTJ 200일선 ±1% 국면 전환 알림 설치 확인. 전환 때 계좌별(ISA, IRP, 연금저축, 미국 직투) 매도/매수 비율을 보냄."),
        ("지금 결과", now_txt + " 평일 09:00에 확인, 바뀔 때만 발송. 매달 첫 실행에 월간 점검 1번."),
        ("지금 할 일", "없음."),
        ("나중에", "전환 알림이 오면 국내 계좌는 그날 정규장, 미국 직투는 그날 밤 미국장에서 실행."),
        ("익절/줄일 때", "해당 없음."),
        ("하지 말 것", "이 설치 확인을 매수나 매도 신호로 읽기."),
    ])
    return title, msg, 2


def ops_message(err: str) -> tuple[str, str, int]:
    title = f"{TAG} SPY 일봉 조회 실패 | 국면 판정 보류"
    msg = body([
        ("이게 뭐 알림", "PTJ200 데이터 조회 실패 안내. 매매 신호 아님."),
        ("지금 결과", f"Yahoo SPY 일봉 조회 실패가 연속됨: {err[:200]}"),
        ("지금 할 일", "급하면 TradingView PTJ_KR_US_Hybrid 지표의 대시보드로 국면 확인."),
        ("나중에", "다음 실행이 놓친 봉을 순서대로 다시 적용함."),
        ("익절/줄일 때", "해당 없음."),
        ("하지 말 것", "이 안내를 매매 신호로 읽기."),
    ])
    return title, msg, 3


def _guard(title: str, msg: str) -> None:
    for ch in BANNED:
        if ch in title or ch in msg:
            raise RuntimeError(f"banned char {ch!r}")


# ---------------- run ----------------
def execute(cfg: dict, st: dict, bars: list[tuple[date, float]] | None, err: str | None,
            now: datetime, send) -> list[tuple[str, str, int, str]]:
    """Mutates st. send(topic, title, msg, priority). Returns sent list."""
    out = []
    today = now.astimezone(KST).date().isoformat()

    def emit(kind: str, t: str, m: str, p: int) -> None:
        _guard(t, m)
        topic = resolve_topic("ops" if kind == "ops" else "port", cfg.get("ntfy_topic"))
        send(topic, t, m, p)
        out.append((kind, t, m, p))

    if err is not None or not bars:
        st["fail"]["runs"] = int(st["fail"].get("runs") or 0) + 1
        if st["fail"]["runs"] >= int(cfg["rules"]["fail_ops_after_runs"]) and st["fail"].get("notified_day") != today:
            emit("ops", *ops_message(err or "봉 없음"))
            st["fail"]["notified_day"] = today
        return out
    st["fail"] = {"runs": 0, "notified_day": st["fail"].get("notified_day")}

    band = float(cfg["rules"]["band_pct"])
    rows = series(bars, cfg)
    last = rows[-1]
    month = now.astimezone(KST).strftime("%Y-%m")

    if st.get("regime") not in (1, -1) or not st.get("last_date"):
        reg = replay(rows, -1, band)  # seed: same start as Pine (var regime = -1)
        st.update({"regime": reg, "seed_at": now.isoformat(timespec="seconds"), "last_status_month": month})
    else:
        prev = int(st["regime"])
        last_d = date.fromisoformat(st["last_date"])
        new_rows = [r for r in rows if r["date"] > last_d]
        reg, flips = prev, 0
        for r in new_rows:
            nr = step(reg, r["close"], r["sma"], band)
            if nr != reg:
                flips += 1
            reg = nr
        if reg != prev:
            emit("switch", *switch_message(cfg, prev, reg, last, flips))
            st["last_switch"] = {"date": last["date"].isoformat(), "from": prev, "to": reg, "sent_at": now.isoformat(timespec="seconds")}
            st["last_status_month"] = month
        elif cfg["rules"].get("monthly_status") and st.get("last_status_month") != month:
            emit("status", *status_message(cfg, reg, last, month))
            st["last_status_month"] = month
        st["regime"] = reg
    st.update({
        "last_date": last["date"].isoformat(),
        "last_close": round(last["close"], 4),
        "last_sma": round(last["sma"], 4),
        "updated_at": now.isoformat(timespec="seconds"),
    })
    return out


def live(cfg: dict, now: datetime) -> tuple[list | None, str | None]:
    try:
        bars = yahoo_daily(cfg["rules"]["symbol"], cfg["rules"]["yahoo_range"])
        return completed_bars(bars, now, cfg), None
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "auto"
    if mode == "selftest":
        return selftest()
    if mode not in ("auto", "dry", "install"):
        print(f"unknown mode {mode}", file=sys.stderr)
        return 2
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    bars, err = live(cfg, now)

    if mode == "install":
        reg = row = None
        if bars:
            rows = series(bars, cfg)
            row = rows[-1]
            reg = replay(rows, -1, float(cfg["rules"]["band_pct"]))
        t, m, p = install_message(cfg, reg, row)
        _guard(t, m)
        publish(resolve_topic("port", cfg.get("ntfy_topic")), t, m, p, config_name=NAME)
        print(f"install sent: {t}")
        return 0

    st = load_state(STATE_PATH)
    if mode == "dry":
        def send(topic, t, m, p):
            print(f"DRY RUN would send to {topic} p{p}: {t}\n{m}\n")
        execute(cfg, json.loads(json.dumps(st)), bars, err, now, send)
        if bars:
            rows = series(bars, cfg)
            r = rows[-1]
            print(f"DRY RUN last {r['date']} close {r['close']:.2f} sma {r['sma']:.2f} "
                  f"seed-regime {LABEL[replay(rows, -1, float(cfg['rules']['band_pct']))]} state-regime {st.get('regime')}")
        else:
            print(f"DRY RUN fetch error: {err}")
        return 0

    def send(topic, t, m, p):
        publish(topic, t, m, p, config_name=NAME)

    try:
        sent = execute(cfg, st, bars, err, now, send)
    finally:
        save_state(STATE_PATH, st)
    for kind, t, _, p in sent:
        print(f"sent {kind} p{p}: {t}")
    if not sent:
        print(f"no send. regime={st.get('regime')} last_date={st.get('last_date')} err={err}")
    return 0


# ---------------- selftest (offline) ----------------
def _synthetic(n: int, start: date, f) -> list[tuple[date, float]]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append((d, f(len(out))))
        d += timedelta(days=1)
    return out


def selftest() -> int:
    fails: list[str] = []
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    band = float(cfg["rules"]["band_pct"])

    def ok(c: bool, msg: str) -> None:
        if not c:
            fails.append(msg)

    # step / hysteresis
    ok(step(-1, 101.01, 100, band) == 1, "up break")
    ok(step(-1, 100.5, 100, band) == -1, "inside keeps bear")
    ok(step(1, 99.5, 100, band) == 1, "inside keeps bull")
    ok(step(1, 98.9, 100, band) == -1, "down break")

    # SMA
    bars = _synthetic(260, date(2025, 1, 1), lambda i: 100.0 + i)
    rows = series(bars, cfg)
    ok(len(rows) == 61, f"rows {len(rows)}")
    ok(abs(rows[0]["sma"] - (100 + 199 / 2)) < 1e-9, "sma value")

    # account moves text
    acc = {a["id"]: a for a in cfg["accounts"]}
    isa = "\n".join(account_moves(acc["isa"], 1, -1, cfg["assets"]))
    ok("448290 12%에서 6%로 (6%p 매도)" in isa and "452360 8%에서 4%로 (4%p 매도)" in isa and "456610 10% 매수" in isa, f"isa moves {isa}")
    irp = "\n".join(account_moves(acc["irp"], 1, -1, cfg["assets"]))
    ok("360750 70%에서 50%로 (20%p 매도)" in irp and "0025N0 전량 매도" in irp, f"irp moves {irp}")
    us = "\n".join(account_moves(acc["us"], -1, 1, cfg["assets"]))
    ok("SPY 전량 매도" in us and "SSO(S&P500 2배) 100% 매수" in us and "오늘 밤 미국" in us, f"us moves {us}")

    now = datetime(2026, 11, 9, 9, 0, tzinfo=KST)
    sent: list = []

    def send(topic, t, m, p):
        sent.append((topic, t, p))

    with tempfile.TemporaryDirectory() as td:
        sp = Path(td) / "s.json"
        st = load_state(sp)
        # rising series -> bull at seed, no send
        up = _synthetic(300, date(2025, 8, 1), lambda i: 100.0 + i * 0.5)
        execute(cfg, st, up, None, now, send)
        ok(st["regime"] == 1 and not sent, f"seed {st['regime']} {sent}")
        # same bars again -> no send
        execute(cfg, st, up, None, now, send)
        ok(not sent, "rerun no send")
        # crash on new bar -> bear switch
        crash = up + [(up[-1][0] + timedelta(days=3), 100.0)]
        execute(cfg, st, crash, None, now, send)
        ok(len(sent) == 1 and sent[0][2] == 4 and "방어 전환" in sent[0][1] and sent[0][0] == "cwally-port", f"switch {sent}")
        ok(st["regime"] == -1, "regime after switch")
        # repeat -> no send (same month, status already marked)
        execute(cfg, st, crash, None, now, send)
        ok(len(sent) == 1, "no repeat")
        # next month -> status
        later = datetime(2026, 12, 1, 9, 0, tzinfo=KST)
        execute(cfg, st, crash, None, later, send)
        ok(len(sent) == 2 and sent[1][2] == 2 and "월간 점검" in sent[1][1], f"status {sent}")
        # fetch fails: first silent, second ops once
        execute(cfg, st, None, "boom", later, send)
        ok(len(sent) == 2, "first fail silent")
        execute(cfg, st, None, "boom", later, send)
        ok(len(sent) == 3 and sent[2][0] == "cwally-ops", f"ops {sent[-1]}")
        execute(cfg, st, None, "boom", later, send)
        ok(len(sent) == 3, "ops once a day")

    # stale / partial bar filter
    try:
        completed_bars(_synthetic(300, date(2025, 1, 1), lambda i: 100.0), now, cfg)
        fails.append("stale not caught")
    except RuntimeError:
        pass

    # copy texts carry no banned chars
    r = rows[-1]
    for t, m, _ in (switch_message(cfg, 1, -1, r, 1), switch_message(cfg, -1, 1, r, 2),
                    status_message(cfg, 1, r, "2026-12"), install_message(cfg, 1, r), ops_message("x")):
        try:
            _guard(t, m)
        except RuntimeError as exc:
            fails.append(f"guard {t}: {exc}")
        ok("SPY" in t, f"ticker in title {t}")

    if fails:
        print("SELFTEST FAIL:\n" + "\n".join(fails))
        return 1
    print("SELFTEST PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
