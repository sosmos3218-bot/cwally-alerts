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

Runs inside flow-alerts.yml (weekday 15:40 KST). Modes: auto | dry (print only, no ntfy, no state) | selftest.
"""
from __future__ import annotations

import json
import sys
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

def message(cfg: dict, fired: list[str], day: str, close: float, prev: float | None, info: list[str], stamp: str) -> tuple[str, str, int]:
    by = {ln["key"]: ln for ln in cfg["lines"]}
    fl = [by[k] for k in fired]
    prio = max(int(ln["priority"]) for ln in fl)
    title = f"[K200] 종가 {close:,.2f} | " + " + ".join(ln["label"] for ln in fl)
    chg = f", 전일 대비 {(close / prev - 1) * 100:+.2f}%" if prev else ""
    rearm = " ".join(
        f"{ln['level']:g}선은 종가 {ln['rearm']:.2f} {'이상' if ln['rearm_op'] == 'ge' else '이하'}에서 다시 무장." for ln in fl)
    info_txt = ". ".join(x for x in info if x)
    body = "\n".join([
        f"{stamp} | K200 | 코스피200",
        "이게 뭐 알림: " + cfg["explain"],
        f"지금 결과/상태: {day} 코스피200 종가 {close:,.2f}{chg}. 새로 켜진 선: " + ", ".join(ln["label"] for ln in fl) + "." + (f" 참고: {info_txt}." if info_txt else ""),
        "지금 할 일: " + " ".join(ln["todo"] for ln in fl),
        "나중에: " + rearm + (" 819 이탈 중에는 2차 접근 알림을 보내지 않습니다." if "l819" in fired else ""),
        "익절/줄일 때: 905 초과 뒤 다시 900.48 아래로 밀리면 추세 확인을 취소로 보고 3차 보류.",
        "하지 말 것: 장중 가격으로 미리 매수, 한 번에 계획 금액 전부, 신용/미수, 이 알림만 보고 주문. 매매 권유 아님.",
    ])
    return title, body, prio


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
    lines = cfg["lines"]
    state = load_json(STATE_PATH, {"armed": {}, "stop": False, "last_bar": None, "last_sent": None, "last_fail_day": None})
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
        title = "[K200] 시세 조회 실패 | 판정 보류"
        body = f"{stamp} | K200\n코스피200 시세 조회 실패: {exc}. 다음 평일 15:40 에 다시 보고, 놓친 종가는 그때 날짜 순서대로 판정합니다. 이 실패로 매매하지 마세요."
        print(title, "\n" + body)
        if not dry and state.get("last_fail_day") != day:
            publish(resolve_topic("port", cfg.get("ntfy_topic")), title, body, 3, ["warning"], resolve_server(cfg.get("ntfy_server")))  # routed to cwally-ops
            state["last_fail_day"] = day
            state["updated_at"] = stamp
            save(state, dry)
        return 0
    last_day = (state.get("last_bar") or {}).get("day") or ""
    prev_close = (state.get("last_bar") or {}).get("close")
    new_bars = [(d, c) for d, c in bars if d > last_day]
    print(f"{stamp} k200 status={basic.get('marketStatus')} last_bar={last_day} new={new_bars} armed={armed} stop={stop}")
    if not new_bars:
        print("k200 no new confirmed bar; silent")
        return 0
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
