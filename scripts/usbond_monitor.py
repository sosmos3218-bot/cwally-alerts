#!/usr/bin/env python3
"""[US-BOND] IEF(미국채 7-10년, 환헤지 없음) 판단 보조 알림. 매매 신호 아님, 자동 주문 아님.

기존 k203780 19:00 슬롯(cron-job.org 8698629 정시 dispatch + GitHub cron 0 10 * * 1-5 백업)에 붙는다.
1) 2차 분할 가격: 10년 >= base+25bp 또는 IEF 종가 <= base x (1-0.017) -> 1회.
   10년 < base+15bp 이고 IEF > 트리거 가격이면 다시 무장. 본문에 3M/6M USD 기대 수익
   R = y*h - D*dy + 0.5*C*dy^2 (y=0.0505, D=6.84, C=56, dy=현재 10년 - base).
2) 손익분기 환율: 원/달러 < 1,310 경고(우선순위 3), < 1,294 강한 경고(4). 1,320 위에서 다시 무장.
3) 역상관 체제: KRX 10거래일 전 대비 10년 +25bp 초과 그리고 원/달러 -2% 미만이 10거래일 연속 -> 1회.
   5거래일 연속 거짓이면 다시 무장.
4) FOMC 다음 날 요약: 결정일(미 동부) 다음 KST 날짜 19:00 실행에서 1회 (날짜는 config, 연준 페이지로 검증).
1-3 은 KRX 거래일(kr_calendar)만 판정. 10년/IEF 는 그 KST 날짜 전에 끝난 직전 미국 영업일 값.
데이터: 10년 FRED DGS10 -> Treasury.gov par yield, IEF Stooq -> Yahoo chart, 원/달러 ECOS 731Y001 매매기준율 -> 네이버.
조회 실패한 값은 본문 '확인 불가', 그 값이 필요한 조건은 판정 보류. 3번 연속 전 출처 실패와 스크립트 오류는 cwally-ops.
modes: auto | dry(발송 없음, state 안 씀, --as-of "YYYY-MM-DD HH:MM" 가능) | selftest(가짜 값, 네트워크 없음)
"""
from __future__ import annotations

import csv
import io
import json
import os
import sys
import traceback
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402
from kr_calendar import KST, is_trading_day, session_day  # noqa: E402

NAME = "usbond"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
NA = "확인 불가"
UA = {"User-Agent": "Mozilla/5.0 (cwally-alerts; +mailto:sosmos3218@gmail.com)"}
CATCHUP = 5
HIST_KEEP = 60


# ---------------- io ----------------
def load_state() -> dict:
    base = {"updated_at": None, "seeded": False, "base": {},
            "add2": {"armed": True, "last_day": None, "fired_day": None},
            "fx": {"level": 0, "last_day": None, "fired_day": None},
            "regime": {"armed": True, "last_day": None, "fired_day": None},
            "fomc_done": [], "hist": {}, "fail_runs": {}, "fail_notice": {}}
    if STATE_PATH.exists():
        try:
            base.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return base


def get(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def num(v) -> float | None:
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


# ---------------- 데이터 ----------------
def fred_dgs10(start: date) -> dict[str, float]:
    txt = get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10&cosd={start.isoformat()}", 30)
    out: dict[str, float] = {}
    rows = list(csv.reader(io.StringIO(txt)))
    if not rows or "DGS10" not in rows[0][-1]:
        raise RuntimeError("FRED csv 형식 다름")
    for r in rows[1:]:
        if len(r) >= 2:
            v = num(r[1])
            if v is not None:
                out[r[0]] = v
    return out


def treasury_10y(start: date, end: date) -> dict[str, float]:
    out: dict[str, float] = {}
    for y in sorted({start.year, end.year}):
        url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/"
               f"{y}/all?type=daily_treasury_yield_curve&field_tdr_date_value={y}&page&_format=csv")
        rows = list(csv.DictReader(io.StringIO(get(url))))
        for r in rows:
            v = num(r.get("10 Yr"))
            try:
                d = datetime.strptime(r.get("Date", ""), "%m/%d/%Y").date()
            except ValueError:
                continue
            if v is not None:
                out[d.isoformat()] = v
    if not out:
        raise RuntimeError("Treasury 10 Yr 없음")
    return out


def stooq_ief() -> dict[str, float]:
    txt = get("https://stooq.com/q/d/l/?s=ief.us&i=d")
    if not txt.startswith("Date"):
        raise RuntimeError("Stooq csv 아님(차단/확인 페이지)")
    out = {}
    for r in csv.DictReader(io.StringIO(txt)):
        v = num(r.get("Close"))
        if v is not None:
            out[r["Date"]] = v
    return out


def yahoo_ief(now: datetime) -> dict[str, float]:
    d = json.loads(get("https://query1.finance.yahoo.com/v8/finance/chart/IEF?range=3mo&interval=1d"))
    res = d["chart"]["result"][0]
    off = int(res["meta"].get("gmtoffset") or 0)
    tz = timezone(timedelta(seconds=off))
    loc_now = now.astimezone(tz)
    closes = res["indicators"]["quote"][0]["close"]
    out = {}
    for ts, c in zip(res["timestamp"], closes):
        if c is None:
            continue
        dd = datetime.fromtimestamp(ts, tz).date()
        # 진행 중인 정규장 봉은 제외 (동부 16:30 이후만 그날 종가로 인정)
        if dd > loc_now.date() or (dd == loc_now.date() and (loc_now.hour, loc_now.minute) < (16, 30)):
            continue
        out[dd.isoformat()] = round(float(c), 4)
    return out


def ecos_fx(start: date, end: date) -> dict[str, float]:
    key = os.environ.get("ECOS_API_KEY") or ""
    step = 100 if key else 10
    key = key or "sample"
    out: dict[str, float] = {}
    i = 1
    while i <= 400:
        url = (f"https://ecos.bok.or.kr/api/StatisticSearch/{urllib.parse.quote(key)}/json/kr/{i}/{i + step - 1}/"
               f"731Y001/D/{start:%Y%m%d}/{end:%Y%m%d}/0000001")
        d = json.loads(get(url))
        if "StatisticSearch" not in d:
            res = d.get("RESULT") or {}
            if res.get("CODE") == "INFO-200":
                break
            raise RuntimeError(f"ECOS 731Y001 {res.get('CODE')} {str(res.get('MESSAGE'))[:60]}")
        for r in d["StatisticSearch"].get("row") or []:
            v = num(r.get("DATA_VALUE"))
            t = str(r.get("TIME"))
            if v is not None and len(t) == 8:
                out[f"{t[:4]}-{t[4:6]}-{t[6:]}"] = v
        total = int(d["StatisticSearch"].get("list_total_count") or 0)
        i += step
        if i > total:
            break
    return out


def naver_fx() -> dict[str, float]:
    out = {}
    for page in (1, 2):
        d = json.loads(get("https://m.stock.naver.com/front-api/marketIndex/prices?category=exchange"
                           f"&reutersCode=FX_USDKRW&page={page}&pageSize=30"))
        for r in d.get("result") or []:
            v = num(r.get("closePrice"))
            if v is not None and r.get("localTradedAt"):
                out[str(r["localTradedAt"])[:10]] = v
    if not out:
        raise RuntimeError("네이버 FX_USDKRW 빈 응답")
    return out


def fetch_all(now: datetime, sess: date) -> dict:
    """각 데이터는 {날짜: (값, 출처)}. 앞 출처 우선, 앞 출처에 없는 날짜만 뒤 출처로 채운다
    (FRED DGS10 은 하루 늦게 올라오는 경우가 있어 Treasury.gov 같은 원자료로 채움)."""
    start = sess - timedelta(days=75)
    data: dict = {"errors": [], "ok": {}}

    def merge(key, tries):
        ser: dict[str, tuple[float, str]] = {}
        oks = []
        for label, fn in tries:
            try:
                got = fn()
                if not got:
                    raise RuntimeError("빈 응답")
                oks.append(label)
                for d, v in got.items():
                    ser.setdefault(d, (v, label))
            except Exception as exc:  # noqa: BLE001
                data["errors"].append(f"{label}: {type(exc).__name__} {str(exc)[:60]}")
        data[key] = ser
        data["ok"][key] = oks

    merge("y10", [("FRED DGS10", lambda: fred_dgs10(start)),
                  ("Treasury.gov par yield", lambda: treasury_10y(start, sess))])
    merge("ief", [("Stooq", stooq_ief), ("Yahoo chart", lambda: yahoo_ief(now))])
    merge("fx", [("ECOS 매매기준율", lambda: ecos_fx(start, sess)), ("네이버 은행 고시, ECOS 미반영", naver_fx)])
    return data


# ---------------- 값 고르기 ----------------
def last_before(ser: dict, d: str, max_age: int = 6) -> tuple[str, object] | None:
    """KST 날짜 d 저녁에 보이는 직전 미국 영업일 값 (미국 날짜 < d, 너무 오래된 값은 버림)."""
    lo = (date.fromisoformat(d) - timedelta(days=max_age)).isoformat()
    ks = [k for k in ser if lo <= k < d]
    if not ks:
        return None
    k = max(ks)
    return k, ser[k]


def trading_days_back(end: date, n: int) -> list[str]:
    out, d = [], end
    while len(out) < n:
        if is_trading_day(d):
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return sorted(out)


def trading_days_after(a: str | None, b: date, cap: int) -> list[str]:
    return [d for d in trading_days_back(b, cap) if a is None or d > a]


def implied(cfg: dict, dy_pct: float, h: float) -> float:
    b = cfg["bond"]
    dy = dy_pct / 100.0
    return b["sec_yield"] * h - b["duration"] * dy + 0.5 * b["convexity"] * dy * dy


# ---------------- 판정 (순수 함수) ----------------
def add2_levels(cfg: dict, base: dict) -> tuple[float, float, float]:
    g = cfg["add2"]
    return (base["y10"] + g["yield_up_bp"] / 100.0, base["ief"] * (1 - g["ief_drop_pct"] / 100.0),
            base["y10"] + g["rearm_yield_below_bp"] / 100.0)


def eval_add2(st: dict, y10: float | None, ief: float | None, cfg: dict, base: dict) -> str | None:
    ty, tp, ry = add2_levels(cfg, base)
    hit = (y10 is not None and y10 >= ty - 1e-9) or (ief is not None and ief <= tp + 1e-9)
    if st.get("armed", True):
        if hit:
            st["armed"] = False
            return "fire"
        return None
    if y10 is not None and ief is not None and y10 < ry - 1e-9 and ief > tp + 1e-9:
        st["armed"] = True
        return "rearm"
    return None


def eval_fx(st: dict, fx: float, cfg: dict) -> str | None:
    g = cfg["fx"]
    lvl = int(st.get("level", 0))
    if fx < g["strong_below"] and lvl < 2:
        st["level"] = 2
        return "strong"
    if fx < g["warn_below"] and lvl < 1:
        st["level"] = 1
        return "warn"
    if fx > g["rearm_above"] and lvl > 0:
        st["level"] = 0
        return "rearm"
    return None


def regime_cond(days: list[str], i: int, y10_at, fx_at, cfg: dict) -> bool | None:
    g = cfg["regime"]
    lb = int(g["lookback_trading_days"])
    if i - lb < 0:
        return None
    a, b = days[i - lb], days[i]
    ya, yb, fa, fb = y10_at(a), y10_at(b), fx_at(a), fx_at(b)
    if None in (ya, yb, fa, fb):
        return None
    return (yb - ya) * 100 > g["yield_up_bp"] and (fb / fa - 1) * 100 < g["fx_change_pct"]


def streaks(conds: list[bool | None], i: int) -> tuple[int, int]:
    """(i 에서 끝나는 연속 참 일수, 연속 거짓 일수). None 은 끊김."""
    t = f = 0
    j = i
    while j >= 0 and conds[j] is True:
        t += 1
        j -= 1
    j = i
    while j >= 0 and conds[j] is False:
        f += 1
        j -= 1
    return t, f


def eval_regime(st: dict, t: int, f: int, cfg: dict) -> str | None:
    g = cfg["regime"]
    if st.get("armed", True):
        if t >= int(g["persist_trading_days"]):
            st["armed"] = False
            return "fire"
        return None
    if f >= int(g["rearm_false_days"]):
        st["armed"] = True
        return "rearm"
    return None


def fomc_due(cfg: dict, done: list[str], now: datetime) -> list[str]:
    g = cfg["fomc"]
    dates = [d for k, v in g.items() if k.startswith("decision_dates_") for d in v]
    out = []
    for d in sorted(dates):
        send = date.fromisoformat(d) + timedelta(days=1)
        if d in done:
            continue
        today = now.date()
        if send <= today <= send + timedelta(days=int(g["catchup_days"])) and (today > send or now.hour >= 18):
            out.append(d)
    return out


# ---------------- 문구 ----------------
def body(stamp: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([f"{stamp} | US-BOND | IEF 미국채 7-10년 점검", f"이게 뭐 알림: {what}", f"지금 결과/상태: {status}",
                      f"지금 할 일: {action}", f"나중에: {later}", f"익절/줄일 때: {trim}", f"하지 말 것: {avoid}"])


def md(d: str | None) -> str:
    return f"{d[5:7]}/{d[8:]}" if d else NA


def s_y10(v) -> str:
    return f"{v[1]:.2f}% ({md(v[0])}, {v[2]})" if v else NA


def s_ief(v) -> str:
    return f"${v[1]:.2f} ({md(v[0])} 종가, {v[2]})" if v else NA


def s_fx(v) -> str:
    return f"{v[1]:,.1f}원 ({md(v[0])}, {v[2]})" if v else NA


def base_txt(base: dict) -> str:
    return (f"기준(빌드 시점 실제 값): 10년 {base['y10']:.2f}% ({md(base['y10_date'])}), "
            f"IEF ${base['ief']:.2f} ({md(base['ief_date'])}), 원/달러 {base['fx']:,.1f}원 ({md(base['fx_date'])})")


def ret_txt(cfg: dict, y10: float | None, base: dict) -> str:
    if y10 is None:
        return f"3M/6M 기대 USD 수익 {NA}"
    dy = y10 - base["y10"]
    r3, r6 = implied(cfg, dy, 0.25) * 100, implied(cfg, dy, 0.5) * 100
    return (f"기준 시점에 산 IEF 를 들고 3개월/6개월 뒤 10년이 지금 수준({dy * 100:+.0f}bp)이면 USD 수익 "
            f"3M {r3:+.2f}%, 6M {r6:+.2f}% (R = 0.0505h - 6.84dy + 0.5x56xdy², 환율 제외)")


def add2_msg(cfg, stamp, base, y10, ief, fx):
    ty, tp, ry = add2_levels(cfg, base)
    dyb = f", 기준 대비 {(y10[1] - base['y10']) * 100:+.0f}bp" if y10 else ""
    dpb = f", 기준 대비 {(ief[1] / base['ief'] - 1) * 100:+.2f}%" if ief else ""
    which = []
    if y10 and y10[1] >= ty - 1e-9:
        which.append(f"10년 {ty:.2f}% 이상")
    if ief and ief[1] <= tp + 1e-9:
        which.append(f"IEF ${tp:.2f} 이하")
    title = f"[US-BOND] IEF 2차 분할 가격 ({', '.join(which)}) | 2차 분할 검토"
    msg = body(stamp, f"10년 금리가 기준보다 +{cfg['add2']['yield_up_bp']}bp 이상 오르거나 IEF 종가가 기준보다 "
                      f"{cfg['add2']['ief_drop_pct']}% 이상 내려가면 1번 오는 2차 분할 가격 알림. 매매 신호 아님.",
               f"미국 10년 {s_y10(y10)}{dyb}. IEF {s_ief(ief)}{dpb}. 원/달러 {s_fx(fx)}. 트리거 10년 {ty:.2f}% 또는 IEF ${tp:.2f}. "
               f"{ret_txt(cfg, y10[1] if y10 else None, base)}. {base_txt(base)}.",
               "계획한 2차 분할 1회분만 검토. 원화로 사는 것이므로 원/달러가 1,310원 아래인지 같이 확인.",
               f"10년이 {ry:.2f}% 아래이고 IEF 가 ${tp:.2f} 위로 돌아오면 다시 무장, 그 뒤 다시 닿으면 다시 옵니다.",
               "원화 기준 손익(IEF 가격 x 환율)을 따로 보고 정하세요. 이 알림은 줄이는 신호가 아닙니다.",
               "남은 금액을 한 번에 다 넣기, 환율을 보지 않고 사기.")
    return title, msg, 3, ["chart_with_downwards_trend"]


def fx_msg(cfg, stamp, kind, base, fx, y10, ief):
    g = cfg["fx"]
    strong = kind == "strong"
    lim = g["strong_below"] if strong else g["warn_below"]
    title = (f"[US-BOND] IEF 원/달러 {fx[1]:,.1f}원 | 손익분기 환율 {lim:,}원 아래"
             + (" 강한 경고" if strong else " 경고"))
    msg = body(stamp, f"환헤지 없는 IEF 의 원화 손익분기 환율 기준(사용자 분석 {g['warn_below']:,}원 경고, {g['strong_below']:,}원 강한 경고)"
                      f" 아래로 원/달러가 내려가면 오는 알림. 매매 신호 아님.",
               f"원/달러 {s_fx(fx)}, 빌드 기준 {base['fx']:,.1f}원 대비 {(fx[1] / base['fx'] - 1) * 100:+.2f}%. "
               f"미국 10년 {s_y10(y10)}. IEF {s_ief(ief)}. {base_txt(base)}.",
               ("환율 하락이 이자와 가격 이익을 넘길 수 있는 구간. 추가 분할 매수는 멈추고 원화 기준 손익을 확인." if strong
                else "추가 분할 매수 전에 원화 기준 손익을 확인. 새로 사는 금액은 줄이는 쪽으로 검토."),
               f"원/달러가 {g['rearm_above']:,}원 위로 올라가면 다시 무장." + ("" if strong else f" {g['strong_below']:,}원 아래로 가면 한 번 더 옵니다."),
               "환율 하나로 전부 팔지 말고, 원화 손실 한도를 미리 정한 대로만 줄이세요.",
               "달러가 싸졌다고 바로 더 사기, 원화 손익을 보지 않고 판단하기.")
    return title, msg, (g["strong_priority"] if strong else g["warn_priority"]), (["rotating_light"] if strong else ["warning"])


def regime_msg(cfg, stamp, d, a, dy_bp, dfx_pct, t):
    g = cfg["regime"]
    title = f"[US-BOND] IEF 금리 상승 + 달러 약세 {t}거래일 지속 | 역상관 체제"
    msg = body(stamp, f"KRX {g['lookback_trading_days']}거래일 전 대비 미국 10년이 +{g['yield_up_bp']}bp 넘게 오르고 원/달러가 "
                      f"{g['fx_change_pct']}% 밑으로 내린 상태가 {g['persist_trading_days']}거래일 이어지면 1번 오는 알림. 매매 신호 아님.",
               f"{d} 기준 {a} 대비 10년 {dy_bp:+.0f}bp, 원/달러 {dfx_pct:+.2f}%. 연속 {t}거래일. "
               "환헤지 없는 IEF 는 가격 하락과 환율 하락이 함께 손실 방향인 구간.",
               "분할 매수 속도를 늦추고, 원화 기준 손익과 남은 분할 계획을 다시 확인.",
               f"조건이 {g['rearm_false_days']}거래일 연속 풀리면 다시 무장.",
               "이 알림만으로 줄이지 말고 원화 손실 한도 기준대로만.",
               "금리가 올랐다고 환율을 보지 않고 더 사기.")
    return title, msg, 4, ["warning"]


def fomc_msg(cfg, stamp, dec, post, pre, base, fx, ief, states):
    g = cfg["fomc"]
    if post and pre:
        dpre = (post[1] - pre[1]) * 100
        add1 = "충족" if dpre <= g["add1_max_bp"] + 1e-9 else "미충족"
        pre_txt = f"FOMC 전({md(pre[0])}) {pre[1]:.2f}% 대비 {dpre:+.0f}bp"
    else:
        add1, pre_txt = NA, f"FOMC 전 대비 {NA}"
    vsb = f"기준 대비 {(post[1] - base['y10']) * 100:+.0f}bp" if post else f"기준 대비 {NA}"
    head = f" | 10년 {post[1]:.2f}%" if post else ""
    title = f"[US-BOND] IEF FOMC 다음 날 요약 ({md(dec)} 결정){head} | 1차 분할 조건 {add1 if add1 != NA else '판정 보류'}"
    if add1 == "충족":
        act = "1차 분할 조건 충족(FOMC 뒤 10년이 FOMC 전보다 +25bp 넘게 오르지 않음). 계획한 1차분 검토."
    elif add1 == "미충족":
        act = "1차 분할 조건 미충족(FOMC 뒤 10년이 +25bp 넘게 오름). 1차분은 보류하고 2차 분할 가격 알림을 기다리기."
    else:
        act = "10년 값을 못 가져와 1차 분할 조건은 판정 보류. 직접 확인 전에는 매수하지 않기."
    msg = body(stamp, f"FOMC 결정일({dec}, 미 동부) 다음 날 19:00 에 1번 오는 요약. 판단 보조, 매매 신호 아님.",
               f"미국 10년 {s_y10(post)}, {vsb}, {pre_txt}. 원/달러 {s_fx(fx)}. IEF {s_ief(ief)}. "
               f"조건 상태: {states}. 1차 분할 조건 {add1}. {base_txt(base)}.",
               act, "다음 FOMC 다음 날 다시 옵니다. 그 사이엔 조건 1-3 이 닿을 때만 옵니다.",
               "원화 기준 손익을 따로 보고, 이 요약만으로 줄이지 마세요.",
               "점도표나 기사 해석만 보고 한 번에 사기.")
    return title, msg, 3, ["bank"]


# ---------------- main ----------------
def run(mode: str, now: datetime, cfg: dict) -> int:
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    today = now.strftime("%Y-%m-%d")
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    state = load_state()
    base = state.get("base") or {}
    if not base.get("y10"):
        print("state base 없음: seed 필요")
        return 2
    sess = session_day(now)
    trading = is_trading_day(sess)
    print(f"now={now:%Y-%m-%d %H:%M} session={sess} trading={trading} ecos_key={'yes' if os.environ.get('ECOS_API_KEY') else 'no(sample)'}")
    data = fetch_all(now, sess)
    y10s, iefs, fxs = data["y10"], data["ief"], data["fx"]
    if data["errors"]:
        print("source errors: " + " | ".join(data["errors"]), file=sys.stderr)
    for k in ("y10", "ief", "fx"):
        ser = data[k]
        tail = [(d, ser[d][0], ser[d][1]) for d in sorted(ser)[-3:]]
        print(f"{k}: ok={data['ok'][k]} last3={tail}")

    def y10_at(d):
        r = last_before(y10s, d)
        return (r[0], r[1][0], r[1][1]) if r else None

    def ief_at(d):
        r = last_before(iefs, d)
        return (r[0], r[1][0], r[1][1]) if r else None

    def fx_at(d):
        return (d, fxs[d][0], fxs[d][1]) if d in fxs else None

    out: list[tuple[str, str, int, list[str]]] = []

    # 전 출처 실패 횟수 (3번 연속이면 ops 하루 1회)
    fr = state.setdefault("fail_runs", {})
    for k, ok in (("y10", bool(y10s)), ("ief", bool(iefs)), ("fx", bool(fxs))):
        fr[k] = 0 if ok else int(fr.get(k, 0)) + 1
        if fr[k] >= int(cfg["ops_fail_after_runs"]) and state.setdefault("fail_notice", {}).get(k) != today:
            state["fail_notice"][k] = today
            what = {"y10": "미국 10년 금리", "ief": "IEF 종가", "fx": "원/달러 환율"}[k]
            out.append((f"[US-BOND] IEF {what} 조회 실패 {fr[k]}회 연속 | 조건 판정 보류",
                        body(stamp, "US-BOND 알림의 데이터 조회 실패 안내.",
                             f"{what} {NA}: 모든 출처 실패 {fr[k]}회 연속. {' | '.join(data['errors'])[:300]}",
                             "필요하면 증권사 앱이나 FRED/연준/한국은행 사이트에서 직접 보세요.", "다음 19:00 실행 때 다시 봅니다.",
                             "이번 안내로 비중을 바꾸지 마세요.", "조회 실패를 조건 미충족으로 읽기."), 3, ["warning"]))

    # hist 기록
    hist = state.setdefault("hist", {})
    if trading:
        d = sess.isoformat()
        y, p, f = y10_at(d), ief_at(d), fx_at(d)
        hist[d] = {"y10": y[1] if y else None, "y10_date": y[0] if y else None, "ief": p[1] if p else None,
                   "ief_date": p[0] if p else None, "fx": f[1] if f else None, "fx_src": f[2] if f else None}
        state["hist"] = dict(sorted(hist.items())[-HIST_KEEP:])

    lines = []
    if trading:
        # 1) 2차 분할 가격
        st = state["add2"]
        for d in trading_days_after(st.get("last_day"), sess, CATCHUP):
            y, p = y10_at(d), ief_at(d)
            if y is None and p is None:
                break
            res = eval_add2(st, y[1] if y else None, p[1] if p else None, cfg, base)
            st["last_day"] = d
            lines.append(f"add2 {d}: y10={y} ief={p} -> {res} armed={st['armed']}")
            if res == "fire":
                st["fired_day"] = d
                out.append(add2_msg(cfg, stamp, base, y, p, fx_at(d)))
        # 2) 손익분기 환율
        st = state["fx"]
        for d in trading_days_after(st.get("last_day"), sess, CATCHUP):
            f = fx_at(d)
            if f is None:
                break
            res = eval_fx(st, f[1], cfg)
            st["last_day"] = d
            lines.append(f"fx {d}: {f} -> {res} level={st['level']}")
            if res in ("warn", "strong"):
                st["fired_day"] = d
                out.append(fx_msg(cfg, stamp, res, base, f, y10_at(d), ief_at(d)))
        # 3) 역상관 체제
        st = state["regime"]
        g = cfg["regime"]
        days = trading_days_back(sess, int(g["lookback_trading_days"]) + int(g["persist_trading_days"]) + 15)
        yv = lambda d: (y10_at(d) or (None, None))[1]  # noqa: E731
        fv = lambda d: (fx_at(d) or (None, None, None))[1]  # noqa: E731
        conds = [regime_cond(days, i, yv, fv, cfg) for i in range(len(days))]
        for i, d in enumerate(days):
            if (st.get("last_day") and d <= st["last_day"]) or i < len(days) - CATCHUP:
                continue
            if conds[i] is None:
                lines.append(f"regime {d}: {NA}")
                break
            t, f_ = streaks(conds, i)
            res = eval_regime(st, t, f_, cfg)
            st["last_day"] = d
            lb = days[i - int(g["lookback_trading_days"])]
            dy_bp = (yv(d) - yv(lb)) * 100
            dfx = (fv(d) / fv(lb) - 1) * 100
            st["streak_true"], st["streak_false"] = t, f_
            lines.append(f"regime {d}: vs {lb} y10 {dy_bp:+.1f}bp fx {dfx:+.2f}% cond={conds[i]} t={t} f={f_} -> {res}")
            if res == "fire":
                st["fired_day"] = d
                out.append(regime_msg(cfg, stamp, d, lb, dy_bp, dfx, t))
    else:
        lines.append("not a KRX trading day: 조건 1-3 판정 안 함")

    # 4) FOMC 다음 날 요약 (KRX 휴일이어도 슬롯이 돌면 보냄)
    for dec in fomc_due(cfg, state.get("fomc_done", []), now):
        post = (dec, y10s[dec][0], y10s[dec][1]) if dec in y10s else None
        pr = last_before(y10s, dec)
        pre = (pr[0], pr[1][0], pr[1][1]) if pr else None
        ief = (dec, iefs[dec][0], iefs[dec][1]) if dec in iefs else None
        fx = fx_at(today) or fx_at(sess.isoformat())
        if fx is None and fxs:
            k = max(fxs)
            fx = (k, fxs[k][0], fxs[k][1] + ", 최근 값")
        ty, tp, _ = add2_levels(cfg, base)
        a2 = state["add2"]
        fxl = {0: "무장(1,310원 위)", 1: "1,310원 경고 발송 뒤", 2: "1,294원 강한 경고 발송 뒤"}[int(state["fx"].get("level", 0))]
        rg = state["regime"]
        states = (f"조건1 2차 분할 가격 {'무장' if a2.get('armed', True) else '발송 뒤 대기'}(트리거 10년 {ty:.2f}% 또는 IEF ${tp:.2f}), "
                  f"조건2 환율 {fxl}, 조건3 역상관 {'무장' if rg.get('armed', True) else '발송 뒤 대기'}"
                  f"(연속 {rg.get('streak_true', 0)}거래일)")
        out.append(fomc_msg(cfg, stamp, dec, post, pre, base, fx, ief, states))
        state.setdefault("fomc_done", []).append(dec)
        lines.append(f"fomc {dec}: post={post} pre={pre} ief={ief} fx={fx}")

    for ln in lines:
        print(ln)
    for title, msg, pri, tags in out:
        print(f"\n--- {'DRY ' if dry else ''}publish -> {route_topic(topic, title)} p{pri} {tags}\nTITLE: {title}\n{msg}")
        if not dry:
            publish(topic, title, msg, pri, tags, server)
    if not out:
        print("no alert")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    state["updated_at"] = now.isoformat()
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


def selftest(cfg: dict) -> int:
    """가짜 값으로 조건과 문구만 검사 (네트워크, ntfy, state 없음)."""
    stamp = "2026-11-02 19:00 KST"
    base = {"y10": 5.00, "y10_date": "2026-10-23", "ief": 91.05, "ief_date": "2026-10-23", "fx": 1324.6, "fx_date": "2026-10-23"}
    ty, tp, ry = add2_levels(cfg, base)
    assert abs(ty - 5.25) < 1e-9 and abs(tp - 89.50215) < 1e-6 and abs(ry - 5.15) < 1e-9
    st = {"armed": True}
    seq = [((5.10, 90.5), None), ((5.25, 90.0), "fire"), ((5.30, 89.0), None), ((5.14, 89.4), None),
           ((5.14, 89.6), "rearm"), ((5.20, 89.50), "fire"), ((5.14, None), None), ((5.10, 90.0), "rearm"), ((None, 89.0), "fire")]
    for (y, p), want in seq:
        got = eval_add2(st, y, p, cfg, base)
        assert got == want, (y, p, got, want)
    print("add2 ok")
    r3, r6 = implied(cfg, 0.25, 0.25), implied(cfg, 0.25, 0.5)
    assert abs(r3 - (0.0505 * 0.25 - 6.84 * 0.0025 + 28 * 0.0025 ** 2)) < 1e-12
    print(f"implied +25bp: 3M {r3 * 100:+.3f}% 6M {r6 * 100:+.3f}%")
    st = {"level": 0}
    seq = [(1315, None), (1309.9, "warn"), (1300, None), (1293.9, "strong"), (1290, None), (1315, None), (1320.1, "rearm"),
           (1290, "strong"), (1325, "rearm"), (1305, "warn")]
    for v, want in seq:
        got = eval_fx(st, v, cfg)
        assert got == want, (v, got, want)
    print("fx ok")
    # 역상관: 거래일 40개, 15번째부터 10년 매일 +4bp, 환율 매일 -0.3%
    days = trading_days_back(date(2026, 12, 30), 40)
    y = {d: 5.0 + max(0, i - 14) * 0.04 for i, d in enumerate(days)}
    fx = {d: 1330 * (0.997 ** max(0, i - 14)) for i, d in enumerate(days)}
    for i, d in enumerate(days):  # 33번째부터 조건 풀림 (금리 하락)
        if i >= 33:
            y[d] = y[days[32]] - (i - 32) * 0.10
    conds = [regime_cond(days, i, y.get, fx.get, cfg) for i in range(len(days))]
    st = {"armed": True}
    ev = []
    for i in range(len(days)):
        if conds[i] is None:
            continue
        t, f = streaks(conds, i)
        r = eval_regime(st, t, f, cfg)
        if r:
            ev.append((i, r, t, f))
    print("regime events:", ev, "conds:", "".join("-" if c is None else ("T" if c else "f") for c in conds))
    assert [e[1] for e in ev] == ["fire", "rearm"], ev
    assert ev[0][2] == 10 and ev[1][3] == 5
    # FOMC 날짜
    due = fomc_due(cfg, [], datetime(2026, 10, 29, 19, 0, tzinfo=KST))
    assert due == ["2026-10-28"], due
    assert fomc_due(cfg, [], datetime(2026, 10, 29, 11, 0, tzinfo=KST)) == []
    assert fomc_due(cfg, ["2026-10-28"], datetime(2026, 10, 29, 19, 0, tzinfo=KST)) == []
    assert fomc_due(cfg, [], datetime(2026, 12, 10, 19, 0, tzinfo=KST)) == ["2026-12-09"]
    assert fomc_due(cfg, [], datetime(2026, 10, 30, 19, 0, tzinfo=KST)) == ["2026-10-28"]  # 놓치면 다음 실행
    print("fomc ok")
    y10 = ("2026-10-30", 5.27, "FRED DGS10")
    ief = ("2026-10-30", 89.31, "Yahoo chart")
    fxv = ("2026-11-02", 1308.2, "ECOS 매매기준율")
    samples = [add2_msg(cfg, stamp, base, y10, ief, fxv), fx_msg(cfg, stamp, "warn", base, fxv, y10, ief),
               fx_msg(cfg, stamp, "strong", base, ("2026-11-02", 1290.0, "ECOS 매매기준율"), y10, ief),
               regime_msg(cfg, stamp, "2026-11-02", "2026-10-19", 27.0, -2.3, 10),
               fomc_msg(cfg, "2026-10-29 19:00 KST", "2026-10-28", ("2026-10-28", 5.08, "FRED DGS10"), ("2026-10-27", 5.02, "FRED DGS10"),
                        base, ("2026-10-29", 1321.0, "ECOS 매매기준율"), ("2026-10-28", 90.7, "Yahoo chart"), "조건1 무장"),
               fomc_msg(cfg, "2026-10-29 19:00 KST", "2026-10-28", None, None, base, None, None, "조건1 무장")]
    for t, m, p, tg in samples:
        assert "\u00b7" not in t + m and "\u2014" not in t + m, t
        assert route_topic("cwally-port", t) == "cwally-port", t
        print(f"-- sample (가짜 숫자)\nTITLE: {t}\nPRIORITY: {p} TAGS: {tg}\n{m}\n")
    assert route_topic("cwally-port", "[US-BOND] IEF 종가 조회 실패 3회 연속 | 조건 판정 보류") == "cwally-ops"
    print("SELFTEST OK: no network, no publish, no state")
    return 0


def main() -> int:
    args = sys.argv[1:]
    mode = (args[0] if args and not args[0].startswith("--") else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    if mode == "selftest":
        return selftest(cfg)
    if mode not in {"auto", "dry"}:
        print(f"unknown mode {mode}")
        return 2
    now = datetime.now(KST)
    if "--as-of" in args:
        if mode != "dry":
            print("--as-of 는 dry 에서만")
            return 2
        now = datetime.strptime(args[args.index("--as-of") + 1], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    try:
        return run(mode, now, cfg)
    except Exception as exc:  # noqa: BLE001  스크립트 오류 -> cwally-ops
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        if mode == "dry":
            return 1
        try:
            stamp = now.strftime("%Y-%m-%d %H:%M KST")
            publish(resolve_topic("ops"), "[US-BOND] IEF 스크립트 오류 | 이번 실행 판정 못 함",
                    body(stamp, "US-BOND 알림 스크립트 오류 안내.", f"{type(exc).__name__}: {str(exc)[:200]}. state 는 바꾸지 않음.",
                         "할 일 없음. 다음 19:00 실행 때 다시 돕니다.", "같은 오류가 이어지면 스크립트 점검 필요.",
                         "이번 안내로 비중을 바꾸지 마세요.", "오류를 조건 미충족으로 읽기."),
                    3, ["warning"], resolve_server(cfg.get("ntfy_server")))
        except Exception:  # noqa: BLE001
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
