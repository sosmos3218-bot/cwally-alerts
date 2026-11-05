#!/usr/bin/env python3
"""[K200] KOSPI200 close-based split-entry lines (approved in chat 2026-10-26). Reference only, not advice.

  l855  close <= 855             '1차 20% 구간' (p3)
  l830  819 <= close <= 830      '2차 30% 접근, 819 위 종가 확인' (p3); never while the 819 stop is on
  l819  close < 819              '2차 이후 중단, 재평가 (다음 지지 750 안팎)' (p4); turns the stop on
  l905  close > 905              '3차 50% 추세 확인' (p4)

Each line fires once per crossing and re-arms only after a 0.5% move back past it (configs/k200.json
rearm: 859.28 / 834.15 / 823.10 stop clear / 900.48). Several lines on one bar -> one combined push.
Only confirmed closes: today's bar only when Naver basic marketStatus == CLOSE and it is past 15:30 KST.
Missed bars are judged in date order on the next run; last_bar.day dedupes so a bar is judged once.

KOSPI E-plan 4 tranches (configs/k200.json kospi_tranches, 2026-11-05 사용자 위임 t55u), separate push:
  t1 KOSPI close <= 5,280 (20% of the E limit, p3)   t2 <= 5,050 (25%, p4)
  t3 <= 4,800 (25%, p4)                              t4 <= 4,400 (30%, p5, E limit full)
Each fires once and re-arms after a close >= level x 1.03. KOSPI index close = Naver index daily (same
confirmed-bar rule). The body converts each level to 069500 / 226490 prices from same-day regular closes
(scripts/krx_close.py, Daum days). Own cursor in state/k200.json "tranche"; seeded at the 11/5 close 5,383.80.

Runs inside flow-alerts.yml (weekday 15:40 KST). Modes: auto | dry (print only, no ntfy, no state) | selftest.
"""
from __future__ import annotations

import json
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


# ---------- core (pure) ----------

def hit(line: dict, c: float) -> bool:
    op, lv = line["op"], float(line["level"])
    if op == "le":
        return c <= lv
    if op == "lt":
        return c < lv
    if op == "gt":
        return c > lv
    if op == "band":
        return float(line["floor"]) <= c <= lv
    raise ValueError(op)


def rearmed(line: dict, c: float) -> bool:
    r = float(line["rearm"])
    return c >= r if line["rearm_op"] == "ge" else c <= r


def step(lines: list[dict], armed: dict, stop: bool, close: float) -> tuple[dict, bool, list[str]]:
    """One confirmed close -> (armed, stop, fired keys). Re-arms first (from the previous state), then fires."""
    c = round(float(close), 2)
    armed = dict(armed)
    by = {ln["key"]: ln for ln in lines}
    for ln in lines:
        k = ln["key"]
        if not armed.get(k, True) and rearmed(ln, c):
            armed[k] = True
    if stop and "l819" in by and rearmed(by["l819"], c):
        stop = False
    fired = []
    for ln in lines:
        k = ln["key"]
        if not armed.get(k, True) or not hit(ln, c):
            continue
        if k == "l830" and stop:
            continue  # suppressed until the 819 stop clears at 823.10
        armed[k] = False
        fired.append(k)
        if k == "l819":
            stop = True
    return armed, stop, fired


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


# ---------- info (never blocks) ----------

def etf_info(cfg: dict) -> str:
    try:
        h = {"User-Agent": "Mozilla/5.0", "Referer": cfg["sources"]["etf_referer"]}
        data = json.loads(fetch(cfg["sources"]["etf"], h, "euc-kr"))
        for it in data["result"]["etfItemList"]:
            if str(it.get("itemcode")) == cfg["etf_code"]:
                amt = it.get("amonut")
                amt_txt = f", 거래대금 {amt / 100:,.0f}억원" if isinstance(amt, (int, float)) else ""
                return f"{cfg['etf_name']}({cfg['etf_code']}) {it['nowVal']:,}원(NAV {it.get('nav', 0):,.0f}, {it.get('changeRate', 0):+.2f}%){amt_txt}"
        return f"{cfg['etf_name']} 목록에 없음"
    except Exception as exc:
        return f"{cfg['etf_name']} 조회 실패({type(exc).__name__})"


def index_value_info(cfg: dict, day: str) -> str:
    parts = []
    try:
        integ = json.loads(fetch(cfg["sources"]["integration"]))
        vals = {x.get("code"): x.get("value") for x in integ.get("totalInfos") or []}
        if vals.get("accumulatedTradingValue"):
            v = num(str(vals["accumulatedTradingValue"]).replace("백만", ""))
            if v:
                parts.append(f"코스피200 거래대금 {v / 1e6:,.1f}조원")
    except Exception:
        pass
    try:
        ch = json.loads(fetch(cfg["sources"]["chart"]))
        pts = [(p["localDate"], p.get("accumulatedTradingVolume")) for p in ch.get("priceInfos") or [] if p.get("accumulatedTradingVolume")]
        key = day.replace("-", "")
        idx = [i for i, (d, _) in enumerate(pts) if d == key]
        if idx and idx[0] >= 20:
            i = idx[0]
            avg = sum(v for _, v in pts[i - 20:i]) / 20
            parts.append(f"거래량 20일 평균 대비 {pts[i][1] / avg * 100:.0f}%(거래대금 20일 평균은 네이버 미제공)")
    except Exception:
        pass
    return ", ".join(parts)


# ---------- message ----------

STOP_TODO = ("추가매수 보류, 819 이탈 이유(코스피200 이익 추정치 하향 여부, 외국인 순매도 지속 여부)부터 재확인해 투자 논리 점검. "
             "1차 포함 이번 분할은 진행하지 않음. 다음 지지는 750 안팎으로 봄.")
BUY_WORDS = ("분할로 검토", "나눠서 검토", "1차 20%만")


def todo_text(fired: list[str], fl: list[dict]) -> str:
    """819 stop together with a buy line (gap day): stop wording only, no buy todo next to it."""
    if "l819" in fired and len(fired) > 1:
        return STOP_TODO
    return " ".join(ln["todo"] for ln in fl)


def message(cfg: dict, fired: list[str], day: str, close: float, prev: float | None, info: list[str], stamp: str) -> tuple[str, str, int]:
    by = {ln["key"]: ln for ln in cfg["lines"]}
    fl = [by[k] for k in fired]
    prio = max(int(ln["priority"]) for ln in fl)
    title = f"[K200] KOSPI200 종가 {close:,.2f} | " + " + ".join(ln["label"] for ln in fl)
    chg = f", 전일 대비 {(close / prev - 1) * 100:+.2f}%" if prev else ""
    rearm = " ".join(
        f"{ln['level']:g}선은 종가 {ln['rearm']:.2f} {'이상' if ln['rearm_op'] == 'ge' else '이하'}에서 다시 무장." for ln in fl)
    info_txt = ". ".join(x for x in info if x)
    body = "\n".join([
        f"{stamp} | K200 | 코스피200",
        "이게 뭐 알림: " + cfg["explain"],
        f"지금 결과/상태: {day} 코스피200 종가 {close:,.2f}{chg}. 새로 켜진 선: " + ", ".join(ln["label"] for ln in fl) + "." + (f" 참고: {info_txt}." if info_txt else ""),
        "지금 할 일: " + todo_text(fired, fl),
        "나중에: " + rearm + (" 819 이탈 중에는 2차 접근 알림을 보내지 않습니다." if "l819" in fired else ""),
        "익절/줄일 때: 905 초과 뒤 다시 900.48 아래로 밀리면 추세 확인을 취소로 보고 3차 보류.",
        "하지 말 것: 장중 가격으로 미리 매수, 한 번에 계획 금액 전부, 신용/미수, 이 알림만 보고 주문. 매매 권유 아님.",
    ])
    return title, body, prio


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
        "나중에: " + rearm + " A안(70%) 확대는 코스피200 종가가 20일선 위 2일 연속이고 20일 변동성 40% 미만일 때만(기존 규칙, 이 알림은 판정하지 않음).",
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

def selftest() -> int:
    cfg = load_json(CFG_PATH, {})
    L = cfg["lines"]
    A = {ln["key"]: True for ln in L}
    fails = 0

    def walk(label, seq, armed=None, stop=False):
        nonlocal fails
        armed = dict(armed or A)
        for i, (c, want) in enumerate(seq):
            armed, stop, f = step(L, armed, stop, c)
            ok = f == want
            fails += not ok
            print(("ok  " if ok else "FAIL"), f"{label}[{i}] close {c}: {f}" + ("" if ok else f" want {want}"), "stop" if stop else "")
        return armed, stop

    walk("seed 862.77 -> today 850 fires 1차 only", [(850.17, ["l855"])])
    walk("855 exact fires, 855.01 not", [(855.01, []), (855.0, ["l855"])])
    walk("buffer walk 855", [(850, ["l855"]), (858, []), (854, []), (859.27, []), (854, []), (859.28, []), (855, ["l855"])])
    walk("830 band", [(845, ["l855"]), (830, ["l830"]), (825, []), (834.14, []), (829, []), (834.15, []), (829, ["l830"])])
    walk("gap below 819: 1차 + stop, no 2차; 2차 only after the stop clears", [(815, ["l855", "l819"]), (818, []), (823.09, []), (825, ["l830"]), (823.09, [])])
    a, s = walk("819 stop suppresses 2차 then recovery", [(850, ["l855"]), (818.9, ["l819"]), (821, []), (823.10, ["l830"]), (826, []), (818, ["l819"])])
    walk("819 exact is 2차 not stop", [(819, ["l855", "l830"])])
    walk("905 break and re-arm", [(905, []), (905.01, ["l905"]), (910, []), (900.49, []), (906, []), (900.48, []), (906, ["l905"])])
    walk("combined same day after a rally (905 then crash)", [(906, ["l905"]), (816, ["l855", "l819"])])
    t, b, p = message(cfg, ["l855", "l819"], "2026-10-26", 816.0, 862.77, ["RISE 200(148020) 1원"], "2026-10-26 15:40 KST")
    ok = p == 4 and "1차 20% 구간 + 2차 이후 중단" in t and not any(ch in t + b for ch in "\u00b7\u2014")
    fails += not ok
    print("ok  " if ok else "FAIL", "combined message p4, no middle dot/em-dash:", t)
    todo = next(x for x in b.split("\n") if x.startswith("지금 할 일:"))
    ok = todo == "지금 할 일: " + STOP_TODO and not any(w in todo for w in BUY_WORDS)
    fails += not ok
    print("ok  " if ok else "FAIL", "combined 1차+819 stop: hold-off todo, no buy todo:", todo)
    _t, b1, _p = message(cfg, ["l819"], "2026-10-26", 818.0, 830.0, [], "2026-10-26 15:40 KST")
    ok = "지금 할 일: 2차 이후 분할은 멈추고 재평가. 다음 지지는 750 안팎으로 봄." in b1
    fails += not ok
    print("ok  " if ok else "FAIL", "819 alone keeps its own todo")
    _t, b2, _p = message(cfg, ["l855"], "2026-10-26", 850.0, 862.77, [], "2026-10-26 15:40 KST")
    ok = "지금 할 일: 계획한 1차 20%만 분할로 검토." in b2
    fails += not ok
    print("ok  " if ok else "FAIL", "pure 1차 buy todo unchanged")
    # confirmed bars + dedupe
    now = datetime(2026, 10, 26, 15, 40, tzinfo=KST)
    rows = [{"localTradedAt": "2026-10-26", "closePrice": "850.17"}, {"localTradedAt": "2026-10-23", "closePrice": "862.77"}, {"localTradedAt": "2026-10-22", "closePrice": "858.02"}]
    cases = [
        ("CLOSE at 15:40 -> today included", {"marketStatus": "CLOSE", "localTradedAt": "2026-10-26T15:30:00+09:00"}, now, "2026-10-26"),
        ("OPEN at 15:40 -> today excluded", {"marketStatus": "OPEN", "localTradedAt": "2026-10-26T15:30:00+09:00"}, now, "2026-10-23"),
        ("CLOSE but 15:20 -> excluded", {"marketStatus": "CLOSE", "localTradedAt": "2026-10-26T15:20:00+09:00"}, now.replace(hour=15, minute=20), "2026-10-23"),
    ]
    for label, basic, n, want_last in cases:
        got = confirmed_bars(rows, basic, n)[-1][0]
        ok = got == want_last
        fails += not ok
        print("ok  " if ok else "FAIL", label, got)
    # re-run of the same bar: run() with last_bar already = today -> no new bars
    st = {"armed": A, "stop": False, "last_bar": {"day": "2026-10-23", "close": 862.77}}
    bars = confirmed_bars(rows, cases[0][1], now)
    new = [b for b in bars if b[0] > st["last_bar"]["day"]]
    ok = [d for d, _ in new] == ["2026-10-26"]
    st["last_bar"] = {"day": "2026-10-26", "close": 850.17}
    again = [b for b in bars if b[0] > st["last_bar"]["day"]]
    ok = ok and again == []
    fails += not ok
    print("ok  " if ok else "FAIL", "same bar judged once (re-run finds no new bar)")
    # data fail: bad rows -> no bars, no crash
    ok = confirmed_bars([{"localTradedAt": "2026-10-26", "closePrice": "-"}], {"marketStatus": "CLOSE", "localTradedAt": "2026-10-26"}, now) == []
    fails += not ok
    print("ok  " if ok else "FAIL", "unparseable close ignored")
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
    if now.year != int(cfg.get("year_guard") or 2026):
        print("k200 year-guard silent")
        return 0
    if now.date() > datetime.strptime(cfg.get("expire_date") or "2026-12-31", "%Y-%m-%d").date():
        print("k200 expired silent")
        return 0
    if now.weekday() >= 5:
        print("k200 weekend silent")
        return 0
    state = load_json(STATE_PATH, {"armed": {}, "stop": False, "last_bar": None, "last_sent": None, "last_fail_day": None})
    rc = 0
    for part in (run_lines, run_tranches):  # a failure in one part does not skip the other
        try:
            part(cfg, state, now, stamp, dry)
        except Exception:
            traceback.print_exc()
            print(f"k200 {part.__name__} failed")
            rc = 1
    return rc


def run_lines(cfg: dict, state: dict, now: datetime, stamp: str, dry: bool) -> None:
    lines = cfg["lines"]
    armed = {ln["key"]: bool((state.get("armed") or {}).get(ln["key"], True)) for ln in lines}
    stop = bool(state.get("stop"))
    try:
        basic = json.loads(fetch(cfg["sources"]["basic"]))
        rows = json.loads(fetch(cfg["sources"]["price"]))
        bars = confirmed_bars(rows, basic, now)
        if not bars:
            raise ValueError("확정 종가 없음")
    except Exception as exc:
        day = now.strftime("%Y-%m-%d")
        title = "[K200] KOSPI200 시세 조회 실패 | 판정 보류"
        body = f"{stamp} | K200\n코스피200 시세 조회 실패: {exc}. 다음 평일 15:40 에 다시 보고, 놓친 종가는 그때 날짜 순서대로 판정합니다. 이 실패로 매매하지 마세요."
        print(title, "\n" + body)
        if not dry and state.get("last_fail_day") != day:
            publish(resolve_topic("port", cfg.get("ntfy_topic")), title, body, 3, ["warning"], resolve_server(cfg.get("ntfy_server")))  # routed to cwally-ops
            state["last_fail_day"] = day
            state["updated_at"] = stamp
            save(state, dry)
        return
    last_day = (state.get("last_bar") or {}).get("day") or ""
    prev_close = (state.get("last_bar") or {}).get("close")
    new_bars = [(d, c) for d, c in bars if d > last_day]
    print(f"{stamp} k200 status={basic.get('marketStatus')} last_bar={last_day} new={new_bars} armed={armed} stop={stop}")
    if not new_bars:
        print("k200 no new confirmed bar; silent")
        return
    fired_all: list[str] = []
    for d, c in new_bars:
        armed, stop, fired = step(lines, armed, stop, c)
        print(f"  bar {d} close {c:,.2f} -> fired {fired} stop={stop}")
        fired_all += [k for k in fired if k not in fired_all]
    day, close = new_bars[-1]
    if len(new_bars) > 1:
        prev_close = new_bars[-2][1]
    if fired_all:
        info = [index_value_info(cfg, day), etf_info(cfg)]
        title, body, prio = message(cfg, fired_all, day, close, prev_close, info, stamp)
        print(f"{'DRY RUN would send' if dry else 'send'} (p{prio}): {title}\n{body}")
        if not dry:
            publish(resolve_topic("port", cfg.get("ntfy_topic")), title, body, prio, ["chart_with_downwards_trend"], resolve_server(cfg.get("ntfy_server")))
            state["last_sent"] = {"at": stamp, "day": day, "fired": fired_all}
    else:
        print("k200 silent; no new crossing")
    state.update({"armed": armed, "stop": stop, "last_bar": {"day": day, "close": close}, "updated_at": stamp})
    save(state, dry)


if __name__ == "__main__":
    raise SystemExit(main())
