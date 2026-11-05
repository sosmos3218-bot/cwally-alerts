#!/usr/bin/env python3
"""[K200] KOSPI200 E-plan alert (reference only, not advice).

2026-11-05 t56u: the old KOSPI200 split lines (855 1차, 819~830 2차, <819 stop, >905 3차; 2026-10-26) are removed.
Under the E-plan (위기관망: KOSPI200 ETF 25%, cash 75%) buying is driven only by the KOSPI tranches below,
and the upside line is replaced by the scale-up rule:

  scaleup  KOSPI200 regular close > its 20-day SMA on 2 consecutive trading days (each day vs its own SMA20)
           AND 20-day realized vol (sample stdev of daily log returns x sqrt(252)) < 40% on the 2nd day
           -> '[K200] KOSPI200 069500 A안(70%) 상향 검토' p4, once; re-armed by a close below its SMA20.
           KOSPI200 closes = Naver index daily (configs/k200.json sources.price, 60 bars).

KOSPI E-plan 4 tranches (configs/k200.json kospi_tranches, 2026-11-05 사용자 위임 t55u), separate push:
  t1 KOSPI close <= 5,280 (20% of the E limit, p3)   t2 <= 5,050 (25%, p4)
  t3 <= 4,800 (25%, p4)                              t4 <= 4,400 (30%, p5, E limit full)
Each fires once and re-arms after a close >= level x 1.03. KOSPI index close = Naver index daily. The body
converts each level to 069500 / 226490 prices from same-day regular closes (scripts/krx_close.py, Daum days).

Only confirmed closes: today's bar only when Naver basic marketStatus == CLOSE and it is past 15:30 KST.
Missed bars are judged in date order on the next run; each part keeps its own last_bar cursor in
state/k200.json ("scaleup", "tranche"). Active through 2027-02-28 23:59 KST (year_guard 2026/2027).

Runs inside flow-alerts.yml (weekday 15:40 KST). Modes: auto | dry (print only, no ntfy, no state) | selftest.
"""
from __future__ import annotations

import json
import math
import sys
import traceback
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "k200.json"
STATE_PATH = ROOT / "state" / "k200.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


def load_json(path: Path, fallback: dict) -> dict:
    out = dict(fallback)
    if path.exists():
        try:
            out.update(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return out


def fetch(url: str, headers: dict | None = None, enc: str = "utf-8", timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=headers or UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode(enc, errors="ignore")


def num(text) -> float | None:
    s = str(text if text is not None else "").replace(",", "").replace("+", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def save(state: dict, dry: bool) -> None:
    if dry:
        return
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------- expiry ----------

def active(cfg: dict, now: datetime) -> bool:
    """True while the alert may run: year in year_guard and not past expire_date 23:59:59 KST."""
    yg = cfg.get("year_guard") or [2026]
    years = {int(y) for y in (yg if isinstance(yg, list) else [yg])}
    end = datetime.strptime(cfg.get("expire_date") or "2027-02-28", "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=KST)
    return now.year in years and now <= end


# ---------- confirmed bars ----------

def confirmed_bars(rows: list, basic: dict, now: datetime) -> list[tuple[str, float]]:
    """(day, close) ascending. Today's row only if the market is CLOSE and it is past 15:30 KST."""
    today = now.strftime("%Y-%m-%d")
    status = str((basic or {}).get("marketStatus") or "").upper()
    basic_day = str((basic or {}).get("localTradedAt") or "")[:10]
    closed_today = status == "CLOSE" and basic_day == today and (now.hour, now.minute) >= (15, 30)
    out = {}
    for r in rows or []:
        d = str(r.get("localTradedAt") or "")[:10]
        c = num(r.get("closePrice"))
        if not d or c is None or c <= 0:
            continue
        if d < today or (d == today and closed_today):
            out[d] = c
    return sorted(out.items())


# ---------- A안(70%) scale-up rule (2026-11-05 t56u) ----------

def sma(closes: list[float], i: int, n: int) -> float | None:
    if i - n + 1 < 0:
        return None
    return sum(closes[i - n + 1:i + 1]) / n


def rvol(closes: list[float], i: int, n: int) -> float | None:
    """Annualized n-day realized vol: sample stdev of the last n daily log returns x sqrt(252)."""
    if i - n < 0:
        return None
    lr = [math.log(closes[j] / closes[j - 1]) for j in range(i - n + 1, i + 1)]
    m = sum(lr) / n
    return math.sqrt(sum((x - m) ** 2 for x in lr) / (n - 1)) * math.sqrt(252)


def scale_eval(sc: dict, closes: list[float], i: int, armed: bool) -> tuple[bool, bool, dict]:
    """Judge bar i -> (armed, fired, info). Re-arm (close < SMA20) first, then fire. Raises if history is short."""
    n, vn, k = int(sc["ma_days"]), int(sc["vol_days"]), int(sc["days_above"])
    days = []
    for j in range(i - k + 1, i + 1):
        m = sma(closes, j, n)
        if m is None:
            raise ValueError(f"이력 부족(SMA{n} 계산에 {n}봉 필요)")
        days.append((closes[j], m))
    v = rvol(closes, i, vn)
    if v is None:
        raise ValueError(f"이력 부족(변동성 계산에 {vn + 1}봉 필요)")
    c, m = days[-1]
    if not armed and c < m:
        armed = True
    cond = all(cc > mm for cc, mm in days) and v * 100 < float(sc["vol_max_pct"])
    fired = armed and cond
    if fired:
        armed = False
    return armed, fired, {"days": days, "vol": v, "cond": cond}


def scale_message(cfg: dict, series: list[tuple[str, float]], i: int, info: dict, etf_close: float | None, stamp: str) -> tuple[str, str, int]:
    sc = cfg["scaleup"]
    k = int(sc["days_above"])
    code = sc["etf_code"]
    title = f"[K200] KOSPI200 {code} A안(70%) 상향 검토"
    two = ", ".join(f"{series[i - k + 1 + n][0]} 종가 {c:,.2f} (20일선 {m:,.2f}, {(c / m - 1) * 100:+.2f}%)" for n, (c, m) in enumerate(info["days"]))
    etf = f" 같은 날 {code} {sc['etf_name']} 정규장 종가 {etf_close:,.0f}원." if etf_close else f" {code} 같은 날 정규장 종가 없음(표시 안 함)."
    body = "\n".join([
        f"{stamp} | K200 | 코스피200 A안 상향 조건",
        f"이게 뭐 알림: E안(위기관망, 코스피200 ETF 25%)에서 A안(70%)으로 올릴 조건이 처음 맞은 날 1번 알립니다. 조건: 코스피200 정규장 종가가 20일선 위 {k}일 연속, 20일 변동성 {float(sc['vol_max_pct']):g}% 미만. 매매 지시 아님.",
        f"지금 결과/상태: {two}. 20일 변동성 {info['vol'] * 100:.1f}% (일간 로그수익률 표준편차 연율화, 252일).{etf}",
        f"지금 할 일: A안 상향은 한 번에 하지 말고 {code} 지정가를 여러 날로 나눠 단계적으로 검토. 남은 E안 코스피 차수 지정가는 겹치지 않게 같이 정리.",
        "나중에: 종가가 20일선 아래로 마감하면 다시 무장하고, 그 뒤 조건이 다시 맞으면 다시 알림.",
        "익절/줄일 때: 상향 뒤 종가가 20일선 아래로 다시 마감하면 늘린 부분부터 줄일지 기존 국면 규칙대로 점검.",
        "하지 말 것: 장중 가격으로 판단, 시장가로 한 번에 70%까지, 신용/미수, 이 알림만 보고 주문. 매매 지시 아님.",
    ])
    for ch in ("\u00b7", "\u2014"):
        title, body = title.replace(ch, ", "), body.replace(ch, ", ")
    return title, body, int(sc["priority"])


def etf_close_on(code: str, day: str, now: datetime) -> float | None:
    import krx_close  # local import keeps selftest offline
    try:
        return dict(krx_close.session_closes(code, 10, now)).get(day)
    except Exception as exc:
        print(f"k200 scaleup {code} close fetch failed: {type(exc).__name__}: {exc}")
        return None


def run_scaleup(cfg: dict, state: dict, now: datetime, stamp: str, dry: bool) -> None:
    sc = cfg.get("scaleup") or {}
    if not sc.get("enabled"):
        print("k200 scaleup disabled")
        return
    ss = state.get("scaleup") or {"armed": True, "last_bar": None, "last_sent": None, "last_fail_day": None}
    armed = bool(ss.get("armed", True))
    try:
        basic = json.loads(fetch(cfg["sources"]["basic"]))
        rows = json.loads(fetch(cfg["sources"]["price"]))
        series = confirmed_bars(rows, basic, now)
        if not series:
            raise ValueError("확정 종가 없음")
    except Exception as exc:
        day = now.strftime("%Y-%m-%d")
        title = "[K200] KOSPI200 시세 조회 실패 | 판정 보류"
        body = f"{stamp} | K200\n코스피200 시세 조회 실패: {exc}. 다음 평일 15:40 에 다시 보고, 놓친 종가는 그때 날짜 순서대로 판정합니다. 이 실패로 매매하지 마세요."
        print(title, "\n" + body)
        if not dry and ss.get("last_fail_day") != day:
            publish(resolve_topic("port", cfg.get("ntfy_topic")), title, body, 3, ["warning"], resolve_server(cfg.get("ntfy_server")))  # routed to cwally-ops
            ss["last_fail_day"] = day
            state["scaleup"] = ss
            save(state, dry)
        return
    closes = [c for _, c in series]
    last_day = (ss.get("last_bar") or {}).get("day") or ""
    new = [i for i, (d, _) in enumerate(series) if d > last_day]
    print(f"{stamp} k200 scaleup KOSPI200 status={basic.get('marketStatus')} bars={len(series)} last_bar={last_day} new={[series[i] for i in new]} armed={armed}")
    if not new:
        info = None
        try:
            _a, _f, info = scale_eval(sc, closes, len(closes) - 1, armed)
        except ValueError as exc:
            print(f"k200 scaleup latest: {exc}")
        if info:
            print(f"k200 scaleup latest {series[-1][0]} close {closes[-1]:,.2f} SMA20 {info['days'][-1][1]:,.2f} vol20 {info['vol'] * 100:.1f}% cond={info['cond']}")
        print("k200 scaleup no new confirmed bar; silent")
        return
    fired_at = None
    for i in new:
        armed, fired, info = scale_eval(sc, closes, i, armed)  # short history raises -> cursor not advanced
        print(f"  bar {series[i][0]} close {closes[i]:,.2f} SMA20 {info['days'][-1][1]:,.2f} vol20 {info['vol'] * 100:.1f}% cond={info['cond']} -> fired={fired} armed={armed}")
        if fired:
            fired_at = (i, info)
    if fired_at:
        i, info = fired_at
        etf_close = etf_close_on(sc["etf_code"], series[i][0], now)
        title, body, prio = scale_message(cfg, series, i, info, etf_close, stamp)
        print(f"{'DRY RUN would send' if dry else 'send'} (p{prio}): {title}\n{body}")
        if not dry:
            publish(resolve_topic("port", cfg.get("ntfy_topic")), title, body, prio, ["chart_with_upwards_trend"], resolve_server(cfg.get("ntfy_server")))
            ss["last_sent"] = {"at": stamp, "day": series[i][0]}
    else:
        print("k200 scaleup silent; condition not newly met")
    ss.update({"armed": armed, "last_bar": {"day": series[-1][0], "close": closes[-1]}, "updated_at": stamp})
    state["scaleup"] = ss
    save(state, dry)


# ---------- KOSPI E-plan 4 tranches (2026-11-05 t55u) ----------

def tranche_step(tranches: list[dict], armed: dict, close: float) -> tuple[dict, list[str]]:
    """One confirmed KOSPI close -> (armed, fired keys). Re-arm (close >= rearm) first, then fire (close <= level)."""
    c = round(float(close), 2)
    armed = dict(armed)
    fired = []
    for t in tranches:
        k = t["key"]
        if not armed.get(k, True) and c >= float(t["rearm"]):
            armed[k] = True
    for t in tranches:
        k = t["key"]
        if armed.get(k, True) and c <= float(t["level"]):
            armed[k] = False
            fired.append(k)
    return armed, fired


def etf_equiv(etf_close: float, kospi_close: float, level: float, tick: int = 5) -> int:
    """ETF price that matches a KOSPI level at today's ETF/KOSPI ratio, floored to the tick."""
    raw = float(etf_close) * float(level) / float(kospi_close)
    return int(raw // tick * tick)


def _won(v: float) -> str:
    return f"{v:,.0f}원"


def _lv(v: float) -> str:
    return f"{v:,.0f}"


def equiv_text(tc: dict, t: dict, day: str, close: float, etf_closes: dict) -> str:
    parts = []
    for e in tc["etfs"]:
        ec = etf_closes.get(e["code"])
        if ec:
            parts.append(f"{e['code']} 약 {_won(etf_equiv(ec, close, t['level'], int(tc.get('etf_tick') or 5)))}")
        else:
            parts.append(f"{e['code']} 환산 못 함({day} 정규장 종가 없음)")
    return ", ".join(parts)


def tranche_message(tc: dict, fired: list[str], day: str, close: float, prev: float | None,
                    etf_closes: dict, armed: dict, stamp: str) -> tuple[str, str, int]:
    by = {t["key"]: t for t in tc["tranches"]}
    fl = [by[k] for k in fired]
    prio = max(int(t["priority"]) for t in fl)
    codes = " ".join(e["code"] for e in tc["etfs"])
    title = "[K200] KOSPI " + codes + " " + " + ".join(f"{t['no']}차 {_lv(t['level'])}" for t in fl) + " 종가 도달"
    chg_pct = (close / prev - 1) * 100 if prev else None
    big = chg_pct is not None and chg_pct < -float(tc.get("big_drop_pct") or 3.0)
    full = "t4" in fired
    plan = ", ".join(f"{t['no']}차 {_lv(t['level'])} {t['share']}%" for t in tc["tranches"])
    etf_now = []
    for e in tc["etfs"]:
        ec = etf_closes.get(e["code"])
        etf_now.append(f"{e['code']} {e['name']} 종가 {_won(ec)} (코스피 대비 비율 {ec / close:.2f})" if ec
                       else f"{e['code']} {e['name']} {day} 정규장 종가 없음")
    hits = " ".join(f"{t['no']}차 {_lv(t['level'])} 도달, E 한도의 {t['share']}%. 환산가: {equiv_text(tc, t, day, close, etf_closes)}." for t in fl)
    nxt = [t for t in tc["tranches"] if armed.get(t["key"], True) and t["level"] < close]
    nxt_txt = (" 다음 차수 미리 걸 가격(오늘 비율): " + "; ".join(
        f"{t['no']}차 {_lv(t['level'])} -> {equiv_text(tc, t, day, close, etf_closes)}" for t in nxt) + ".") if nxt else ""
    todo = ("지정가는 미리 걸어 두는 방식. 위 환산가 근처에 그 차수 비중만 지정가로. 환산가는 오늘 종가 비율이라 내일 다시 계산해서 주문 가격 확인."
            + nxt_txt)
    if full:
        todo += " 4차까지 다 찼으므로 E 한도 다 참. E안 안에서는 더 사지 않음."
    if big:
        todo += f" 오늘은 {chg_pct:+.2f}% 하락일: 시장가로 사지 않음, 걸어 둔 지정가만."
    rearm = " ".join(f"{t['no']}차는 종가 {t['rearm']:,.2f} 이상에서 다시 무장." for t in fl)
    body = "\n".join([
        f"{stamp} | K200 | 코스피 E안 4분할",
        f"이게 뭐 알림: 코스피 E안(위기관망) 한도 안 4분할 지정가 계획({plan})에서 정규장 종가가 차수 가격 이하로 마감한 차수를 1번 알립니다. 장중 저가와 NXT 는 안 셉니다. 매매 지시 아님.",
        f"지금 결과/상태: {day} 코스피 종가 {close:,.2f}" + (f", 전일 대비 {chg_pct:+.2f}%" if chg_pct is not None else "") + ". "
        + hits + " 같은 날 ETF: " + ", ".join(etf_now) + "." + (" E 한도 다 참(4차까지 합 100%)." if full else ""),
        "지금 할 일: " + todo,
        "나중에: " + rearm + " A안(70%) 확대는 코스피200 종가가 20일선 위 2일 연속이고 20일 변동성 40% 미만일 때만(이 알림의 A안 상향 검토 알림이 따로 판정).",
        "익절/줄일 때: E안 한도(코스피200 ETF 25%) 안에서만. 줄이는 판단은 기존 국면 규칙을 따름.",
        "하지 말 것: 3% 넘게 빠진 날 시장가 매수, 신용/미수, E 한도 초과, 장중 가격으로 차수 판단, 이 알림만 보고 주문. 매매 지시 아님.",
    ])
    for ch in ("\u00b7", "\u2014"):
        title, body = title.replace(ch, ", "), body.replace(ch, ", ")
    return title, body, prio


def etf_same_day(tc: dict, day: str, now: datetime) -> dict:
    """{code: regular close on `day`} from krx_close (Daum days, 15:30 minute fallback). Missing -> None."""
    import krx_close  # local import keeps selftest offline
    out = {}
    for e in tc["etfs"]:
        try:
            out[e["code"]] = dict(krx_close.session_closes(e["code"], 10, now)).get(day)
        except Exception as exc:
            print(f"k200 tranche {e['code']} close fetch failed: {type(exc).__name__}: {exc}")
            out[e["code"]] = None
    return out


def run_tranches(cfg: dict, state: dict, now: datetime, stamp: str, dry: bool) -> None:
    tc = cfg.get("kospi_tranches") or {}
    if not tc.get("enabled"):
        print("k200 tranche disabled")
        return
    tr = tc["tranches"]
    ts = state.get("tranche") or {"armed": {}, "last_bar": None, "last_sent": None, "last_fail_day": None}
    armed = {t["key"]: bool((ts.get("armed") or {}).get(t["key"], True)) for t in tr}
    try:
        basic = json.loads(fetch(tc["sources"]["basic"]))
        rows = json.loads(fetch(tc["sources"]["price"]))
        bars = confirmed_bars(rows, basic, now)
        if not bars:
            raise ValueError("확정 종가 없음")
    except Exception as exc:
        day = now.strftime("%Y-%m-%d")
        title = "[K200] KOSPI 시세 조회 실패 | 4분할 판정 보류"
        body = f"{stamp} | K200\n코스피 지수 조회 실패: {exc}. 다음 평일 15:40 에 다시 보고, 놓친 종가는 그때 날짜 순서대로 판정합니다. 이 실패로 매매하지 마세요."
        print(title, "\n" + body)
        if not dry and ts.get("last_fail_day") != day:
            publish(resolve_topic("port", cfg.get("ntfy_topic")), title, body, 3, ["warning"], resolve_server(cfg.get("ntfy_server")))  # routed to cwally-ops
            ts["last_fail_day"] = day
            state["tranche"] = ts
            save(state, dry)
        return
    last_day = (ts.get("last_bar") or {}).get("day") or ""
    new = [(i, d, c) for i, (d, c) in enumerate(bars) if d > last_day]
    print(f"{stamp} k200 tranche KOSPI status={basic.get('marketStatus')} last_bar={last_day} new={[(d, c) for _, d, c in new]} armed={armed}")
    if not new:
        print("k200 tranche no new confirmed KOSPI bar; silent")
        return
    fired_all: list[str] = []
    for _i, d, c in new:
        armed, fired = tranche_step(tr, armed, c)
        print(f"  KOSPI bar {d} close {c:,.2f} -> fired {fired}")
        fired_all += [k for k in fired if k not in fired_all]
    i, day, close = new[-1]
    prev = bars[i - 1][1] if i > 0 else (ts.get("last_bar") or {}).get("close")
    if fired_all:
        etf_closes = etf_same_day(tc, day, now)
        title, body, prio = tranche_message(tc, fired_all, day, close, prev, etf_closes, armed, stamp)
        print(f"{'DRY RUN would send' if dry else 'send'} (p{prio}): {title}\n{body}")
        if not dry:
            publish(resolve_topic("port", cfg.get("ntfy_topic")), title, body, prio, ["chart_with_downwards_trend"], resolve_server(cfg.get("ntfy_server")))
            ts["last_sent"] = {"at": stamp, "day": day, "fired": fired_all}
    else:
        print("k200 tranche silent; no new crossing")
    ts.update({"armed": armed, "last_bar": {"day": day, "close": close}, "updated_at": stamp})
    state["tranche"] = ts
    save(state, dry)


def tranche_selftest(cfg: dict) -> int:
    tc = cfg["kospi_tranches"]
    tr = tc["tranches"]
    fails = 0

    def chk(ok: bool, label: str) -> None:
        nonlocal fails
        fails += not ok
        print("ok  " if ok else "FAIL", label)

    want = {"t1": (5280.0, 3, 5438.40, 20), "t2": (5050.0, 4, 5201.50, 25), "t3": (4800.0, 4, 4944.00, 25), "t4": (4400.0, 5, 4532.00, 30)}
    got = {t["key"]: (t["level"], t["priority"], t["rearm"], t["share"]) for t in tr}
    chk(got == want, f"tranche config levels/priority/re-arm/share {got}")
    chk(all(abs(t["rearm"] - round(t["level"] * 1.03, 2)) < 0.005 for t in tr), "re-arm = level x 1.03")
    chk(sum(t["share"] for t in tr) == 100, "shares sum to 100% of the E limit")

    def walk(label, seq, armed=None):
        nonlocal fails
        armed = dict(armed or {t["key"]: True for t in tr})
        for n, (c, w) in enumerate(seq):
            armed, f = tranche_step(tr, armed, c)
            ok = f == w
            fails += not ok
            print(("ok  " if ok else "FAIL"), f"tranche {label}[{n}] close {c}: {f}" + ("" if ok else f" want {w}"))
        return armed

    walk("seed 11/5 5,383.80 fires nothing", [(5383.80, [])])
    walk("5,280 exact fires 1차, 5,280.01 not", [(5280.01, []), (5280.0, ["t1"])])
    walk("1차 once, re-arm only at 5,438.40", [(5270, ["t1"]), (5200, []), (5438.39, []), (5270, []), (5438.40, []), (5279, ["t1"])])
    walk("gap day 5,000 fires 1차 + 2차 together", [(5000, ["t1", "t2"]), (4990, [])])
    walk("crash to 4,390 fires all four", [(4390, ["t1", "t2", "t3", "t4"])])
    walk("stepwise down", [(5100, ["t1"]), (5050, ["t2"]), (4801, []), (4800, ["t3"]), (4400.01, []), (4400, ["t4"])])
    walk("4차 re-arm 4,532 then refire", [(4399, ["t1", "t2", "t3", "t4"]), (4531.99, []), (4532, []), (4400, ["t4"])])
    chk(etf_equiv(84610, 5383.80, 5280) == 82975, f"069500 equiv 84,610 x 5,280 / 5,383.80 floored to 5 won = {etf_equiv(84610, 5383.80, 5280)}")
    chk(etf_equiv(55490, 5383.80, 5280) == 54420, f"226490 equiv = {etf_equiv(55490, 5383.80, 5280)}")
    stamp = "2026-11-06 15:40 KST"
    closes = {"069500": 84610.0, "226490": 55490.0}
    a, f = tranche_step(tr, {t["key"]: True for t in tr}, 5270.0)
    t1, b1, p1 = tranche_message(tc, f, "2026-11-06", 5270.0, 5383.80, closes, a, stamp)
    chk(t1 == "[K200] KOSPI 069500 226490 1차 5,280 종가 도달" and p1 == 3, f"1차 title/p3: {t1} p{p1}")
    chk("E 한도의 20%" in b1 and "069500 약 " in b1 and "226490 약 " in b1 and "미리 걸어" in b1, "1차 body: share, ETF equivalents, pre-placed limit")
    chk("시장가" in b1 and "신용/미수" in b1 and "매매 지시 아님" in b1, "1차 body: no market buy, no margin, not an order")
    chk("하락일" not in b1 and "E 한도 다 참" not in b1, "1차 -2.11% day: no big-drop line, not full")
    chk("다음 차수 미리 걸 가격" in b1 and "2차 5,050" in b1, "1차 body lists next tranche prices")
    a, f = tranche_step(tr, {t["key"]: True for t in tr}, 4390.0)
    t4, b4, p4 = tranche_message(tc, f, "2026-11-06", 4390.0, 4600.0, closes, a, stamp)
    chk(p4 == 5 and t4.startswith("[K200] KOSPI 069500 226490 1차 5,280 + 2차 5,050 + 3차 4,800 + 4차 4,400"), f"gap to 4차: one message p5: {t4}")
    chk("E 한도 다 참" in b4 and "하락일" in b4, "4차 body: E limit full + big-drop (-4.57%) line")
    _t, b2, p2 = tranche_message(tc, ["t2"], "2026-11-06", 5040.0, 5300.0, {"069500": None, "226490": 55000.0}, {"t1": False, "t2": False, "t3": True, "t4": True}, stamp)
    chk(p2 == 4 and "069500 환산 못 함" in b2 and "226490 약 " in b2, "2차 p4, missing ETF close -> no guessed price")
    chk(not any(ch in t1 + b1 + t4 + b4 + b2 for ch in "\u00b7\u2014"), "no middle dot / em-dash")
    chk(not any(w in t1 + t4 for w in ("조회 실패", "확인 불가", "미게시")), "signal titles avoid ops words (stay on cwally-port)")
    now = datetime(2026, 11, 6, 15, 40, tzinfo=KST)
    rows = [{"localTradedAt": "2026-11-06", "closePrice": "5,270.00"}, {"localTradedAt": "2026-11-05", "closePrice": "5,383.80"}]
    st_bars = confirmed_bars(rows, {"marketStatus": "OPEN", "localTradedAt": "2026-11-06T15:40:00+09:00"}, now)
    chk([d for d, _ in st_bars] == ["2026-11-05"], "KOSPI bar while market OPEN: today excluded")
    seed = json.loads(STATE_PATH.read_text(encoding="utf-8")).get("tranche") or {}
    ok_seed = (seed.get("last_bar") or {}).get("day") == "2026-11-05" and all((seed.get("armed") or {}).get(t["key"]) for t in tr)
    new = [(d, c) for d, c in st_bars if d > (seed.get("last_bar") or {}).get("day", "")]
    chk(ok_seed and new == [], "seed state: 11/5 bar already judged, all armed, first run cannot fire on 11/5")
    return fails


# ---------- selftest ----------

# ---------- selftest ----------

REAL_1105 = [("2026-10-06", 1095.83), ("2026-10-07", 1073.63), ("2026-10-08", 1044.52), ("2026-10-12", 1004.92),
             ("2026-10-13", 942.07), ("2026-10-14", 936.82), ("2026-10-15", 860.7), ("2026-10-16", 878.16),
             ("2026-10-19", 891.3), ("2026-10-20", 858.78), ("2026-10-21", 891.04), ("2026-10-22", 858.02),
             ("2026-10-23", 862.77), ("2026-10-26", 851.51), ("2026-10-27", 881.07), ("2026-10-28", 946.4),
             ("2026-10-29", 935.87), ("2026-10-30", 899.78), ("2026-11-02", 920.63), ("2026-11-03", 885.67),
             ("2026-11-04", 889.25), ("2026-11-05", 837.78)]  # Naver KPI200 daily closes fetched 2026-11-05


def selftest() -> int:
    cfg = load_json(CFG_PATH, {})
    sc = cfg["scaleup"]
    fails = 0

    def chk(ok: bool, label: str) -> None:
        nonlocal fails
        fails += not ok
        print("ok  " if ok else "FAIL", label)

    chk("lines" not in cfg and "etf_code" not in cfg, "old 855/830/819/905 lines removed from config")
    chk(sc["ma_days"] == 20 and sc["vol_days"] == 20 and sc["days_above"] == 2 and sc["vol_max_pct"] == 40 and sc["priority"] == 4, "scaleup config 20MA, 20d vol < 40%, 2 days, p4")
    # real 11/5 data: SMA20 903.85, vol 62.1%, below MA -> no fire
    rc = [c for _, c in REAL_1105]
    a, f, info = scale_eval(sc, rc, len(rc) - 1, True)
    chk(round(info["days"][-1][1], 2) == 903.85 and round(info["vol"] * 100, 1) == 62.1 and not f and a, f"real 11/5: SMA20 {info['days'][-1][1]:.2f}, vol {info['vol'] * 100:.1f}%, no fire")
    try:
        scale_eval(sc, rc[:20], 19, True)
        chk(False, "short history raises")
    except ValueError:
        chk(True, "short history (20 closes) raises, cursor would not advance")

    # synthetic calm series: 21 bars drifting down (below MA), then a calm rise
    base = [100.0 * (0.999 ** n) for n in range(25)]

    def run(seq, armed=True):
        out = []
        closes = list(base)
        for c in seq:
            closes.append(c)
            armed, fired, inf = scale_eval(sc, closes, len(closes) - 1, armed)
            out.append((fired, armed, inf["cond"]))
        return out

    last = base[-1]
    r = run([last * 0.999, last * 1.02, last * 1.025, last * 1.03, last * 1.035])
    chk([x[0] for x in r] == [False, False, True, False, False], f"below, 1 day above (no), 2nd day above fires once, stays above silent: {[x[0] for x in r]}")
    r = run([last * 1.02, last * 1.025, last * 1.03, last * 0.96, last * 1.03, last * 1.035])
    chk([x[0] for x in r] == [False, True, False, False, False, True], f"re-arm after a close below SMA20, then 2 days above refires: {[x[0] for x in r]}")
    r = run([last * 1.02, last * 0.97, last * 1.03])
    chk([x[0] for x in r] == [False, False, False], "above, below, above: not 2 consecutive -> no fire")
    # high vol: alternating +-4% then above MA two days -> vol >= 40% -> no fire
    hv = [100.0]
    for n in range(24):
        hv.append(hv[-1] * (1.04 if n % 2 else 0.96))
    hv += [hv[-1] * 1.08, hv[-1] * 1.10]
    _a, f1, i1 = scale_eval(sc, hv, len(hv) - 1, True)
    chk(not f1 and i1["vol"] * 100 >= 40 and all(c > m for c, m in i1["days"]), f"2 days above MA but vol {i1['vol'] * 100:.1f}% >= 40% -> no fire")
    # vol exactly at limit is not < 40
    chk(rvol([100.0, 101.0, 100.0], 2, 2) is not None, "rvol computes on minimal input")
    # message
    series = [(f"d{n}", c) for n, c in enumerate(base + [last * 1.02, last * 1.025])]
    closes = [c for _, c in series]
    _a, _f, inf = scale_eval(sc, closes, len(closes) - 1, True)
    t, b, p = scale_message(cfg, series, len(series) - 1, inf, 98765.0, "2027-01-05 15:40 KST")
    chk(t == "[K200] KOSPI200 069500 A안(70%) 상향 검토" and p == 4, f"title/p4: {t}")
    lines_ = b.split("\n")
    order = ["이게 뭐 알림:", "지금 결과/상태:", "지금 할 일:", "나중에:", "익절/줄일 때:", "하지 말 것:"]
    chk([ln.split(":")[0] + ":" for ln in lines_[1:7]] == order, "six-part body order")
    chk("20일선" in b and "20일 변동성" in b and "d25 종가" in b and "d26 종가" in b and "98,765원" in b, "body shows SMA20, vol, the 2 closes, 069500 close")
    chk("지정가" in b and "신용/미수" in b and "매매 지시 아님" in b and "시장가" in b, "body: staged limit orders, no margin, not an order")
    chk(not any(ch in t + b for ch in "\u00b7\u2014"), "no middle dot / em-dash")
    _t, b2, _p = scale_message(cfg, series, len(series) - 1, inf, None, "2027-01-05 15:40 KST")
    chk("069500 같은 날 정규장 종가 없음" in b2, "missing 069500 close is stated, not guessed")
    # seed state
    st = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    ss = st.get("scaleup") or {}
    chk(ss.get("armed") is True and (ss.get("last_bar") or {}).get("day") == "2026-11-05" and "armed" not in st and "stop" not in st,
        "seed: scaleup armed, last_bar 11/5 (837.78 below SMA20), old line state removed")
    # expiry
    chk(active(cfg, datetime(2026, 11, 6, 15, 40, tzinfo=KST)), "active 2026-11-06")
    chk(active(cfg, datetime(2027, 1, 4, 15, 40, tzinfo=KST)), "active 2027-01-04 (year_guard allows 2027)")
    chk(active(cfg, datetime(2027, 2, 28, 23, 59, tzinfo=KST)), "active 2027-02-28 23:59")
    chk(not active(cfg, datetime(2027, 3, 1, 0, 0, tzinfo=KST)), "off 2027-03-01 00:00")
    chk(not active(cfg, datetime(2028, 1, 3, 15, 40, tzinfo=KST)), "off 2028")
    chk(cfg.get("ends_kst") == "2027-02-28 23:59" and cfg.get("expire_date") == "2027-02-28", "ends_kst / expire_date 2027-02-28")
    # confirmed bars
    now = datetime(2026, 10, 26, 15, 40, tzinfo=KST)
    rows = [{"localTradedAt": "2026-10-26", "closePrice": "850.17"}, {"localTradedAt": "2026-10-23", "closePrice": "862.77"}]
    chk(confirmed_bars(rows, {"marketStatus": "CLOSE", "localTradedAt": "2026-10-26T15:30:00+09:00"}, now)[-1][0] == "2026-10-26", "CLOSE at 15:40 -> today included")
    chk(confirmed_bars(rows, {"marketStatus": "OPEN", "localTradedAt": "2026-10-26T15:30:00+09:00"}, now)[-1][0] == "2026-10-23", "OPEN at 15:40 -> today excluded")
    chk(confirmed_bars(rows, {"marketStatus": "CLOSE", "localTradedAt": "2026-10-26T15:20:00+09:00"}, now.replace(minute=20))[-1][0] == "2026-10-23", "CLOSE but 15:20 -> excluded")
    chk(confirmed_bars([{"localTradedAt": "2026-10-26", "closePrice": "-"}], {"marketStatus": "CLOSE", "localTradedAt": "2026-10-26"}, now) == [], "unparseable close ignored")
    fails += tranche_selftest(cfg)
    print("selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


# ---------- main ----------

def main() -> int:
    args = [a.lower() for a in sys.argv[1:]]
    if "selftest" in args:
        return selftest()
    dry = "dry" in args
    cfg = load_json(CFG_PATH, {})
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    if dry:
        print("DRY RUN (no ntfy, no state write)")
    if not active(cfg, now):
        print("k200 year-guard / expired silent")
        return 0
    if now.weekday() >= 5:
        print("k200 weekend silent")
        return 0
    state = load_json(STATE_PATH, {})
    rc = 0
    for part in (run_scaleup, run_tranches):  # a failure in one part does not skip the other
        try:
            part(cfg, state, now, stamp, dry)
        except Exception:
            traceback.print_exc()
            print(f"k200 {part.__name__} failed")
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
