#!/usr/bin/env python3
"""[453810] KODEX 인도Nifty50 ISA 3회 분할 매수 계획 점검 (2026-11-03, 사용자 계획 붙여 넣기, 판단 위임).

평일 09:00 KST (port-alerts morning 슬롯에 부착). 전일 확정 종가 기준:
  ETF 453810 (네이버 수정주가 일봉), Brent BZ=F, Nifty 50 ^NSEI, USD/INR INR=X (Yahoo 일봉, 거래소 시간대 기준 끝난 날만).
config lines: 선마다 상태가 바뀔 때만 1번, rearm 으로 재무장. Brent 85, 120 선은 10거래일 연속 조건.
avg_price 가 들어오면 평균가 -20% p4 '비중 축소 검토' 선이 켜짐. 리마인더는 날짜가 지난 첫 실행에서 1번.
같은 실행에서 걸린 것은 한 건으로 묶음. 첫 실행(또는 새 선)은 조용히 시드. 데이터 조회 실패는 하루 1번 cwally-ops.
Nifty PE, FII 흐름은 믿을 만한 무료 데이터가 없어 자동 점검하지 않음.
usage: k453810_monitor.py <auto|dry> | selftest | confirm
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "k453810.json"
STATE_PATH = ROOT / "state" / "k453810.json"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
TAG = "[453810]"
BAD_WORDS = ("분할매수", "분할 매수", "매수 기회", "매수 구간", "저가 매수", "사세요", "진입", "담을", "비중 확대", "지금 매수",
             "매수 검토", "매수하세요", "사 모으", "물타기", "추가 매수 하", "집행")


def get(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as resp:
        return resp.read()


def etf_rows(code: str, now: datetime) -> list[tuple[str, float]]:
    raw = get(f"https://fchart.stock.naver.com/sise.nhn?symbol={code}&timeframe=day&count=30&requestType=0").decode("euc-kr", "ignore")
    rows = sorted((f"{d[:4]}-{d[4:6]}-{d[6:]}", float(c)) for d, c in re.findall(r'item data="(\d{8})\|[^|]*\|[^|]*\|[^|]*\|([\d.]+)\|', raw))
    today = now.strftime("%Y-%m-%d")
    return [r for r in rows if r[0] < today or (r[0] == today and (now.hour, now.minute) >= (15, 31))]


def yahoo_rows(symbol: str, close_hm: str, now: datetime) -> list[tuple[str, float]]:
    """Completed daily closes [(exchange-local date, close)], None bars dropped."""
    d = json.loads(get(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?range=3mo&interval=1d"))["chart"]["result"][0]
    tz = ZoneInfo(d["meta"].get("exchangeTimezoneName") or "UTC")
    loc = now.astimezone(tz)
    hh, mm = (int(x) for x in close_hm.split(":"))
    out = {}
    for t, c in zip(d.get("timestamp") or [], d["indicators"]["quote"][0].get("close") or []):
        if c is None:
            continue
        day = datetime.fromtimestamp(t, tz).date()
        if day < loc.date() or (day == loc.date() and (loc.hour, loc.minute) >= (hh, mm)):
            out[day.isoformat()] = float(c)
    return sorted(out.items())


# ---------- 판정 ----------
OPS = {"le": lambda v, x: v <= x, "lt": lambda v, x: v < x, "ge": lambda v, x: v >= x, "gt": lambda v, x: v > x}


def streak(closes: list[float], op: str, level: float) -> int:
    n = 0
    for v in reversed(closes):
        if not OPS[op](v, level):
            break
        n += 1
    return n


def line_hit(ln: dict, closes: list[float]) -> bool:
    days = int(ln.get("days") or 1)
    return bool(closes) and streak(closes, ln["op"], ln["level"]) >= days


def all_lines(cfg: dict) -> list[dict]:
    lines = list(cfg["lines"])
    avg = cfg.get("avg_price")
    if avg:
        a = cfg["avg_line"]
        lv = float(avg) * (1 + a["pct"] / 100)
        lines.append({"key": "avg20", "src": "etf", "op": "le", "level": lv, "rearm_op": "ge", "rearm": lv * (1 + a["rearm_pct"] / 100),
                      "priority": a["priority"], "warn": True, "label": f"평균가 대비 {a['pct']}% ({lv:,.0f}) 이하, 비중 축소 검토"})
    return lines


def judge(lines: list[dict], armed: dict, series: dict[str, list[float]]) -> list[dict]:
    """series: src -> completed closes (oldest first). Missing src: line untouched. New key seeds silently."""
    fired = []
    for ln in lines:
        cl = series.get(ln["src"])
        if not cl:
            continue
        k, hit = ln["key"], line_hit(ln, cl)
        if k not in armed:
            armed[k] = not hit
            continue
        if armed[k] and hit:
            armed[k] = False
            fired.append(ln)
        elif not armed[k] and OPS[ln["rearm_op"]](cl[-1], ln["rearm"]):
            armed[k] = True
    return fired


def due_reminders(cfg: dict, sent: dict, today: str) -> list[dict]:
    out = []
    for r in cfg["reminders"]:
        if r["date"] <= today and not sent.get(r["key"]):
            sent[r["key"]] = today
            if r.get("only_if_no_avg") and cfg.get("avg_price"):
                continue
            out.append(r)
    return out


# ---------- 문구 ----------
def fmt(v, nd=0):
    return "확인 불가" if v is None else f"{v:,.{nd}f}"


def status_line(ctx: dict) -> str:
    e, b, n, i = ctx.get("etf"), ctx.get("brent"), ctx.get("nifty"), ctx.get("inr")
    parts = [f"ETF {e[0]} 종가 {fmt(e[1])}원" if e else "ETF 확인 불가",
             (f"Brent {b[0]} {fmt(b[1], 2)}달러(85 이하 연속 {ctx.get('b85', 0)}일, 120 이상 연속 {ctx.get('b120', 0)}일)" if b else "Brent 확인 불가"),
             f"Nifty 50 {n[0]} {fmt(n[1], 1)}" if n else "Nifty 확인 불가",
             f"USD/INR {i[0]} {fmt(i[1], 2)}" if i else "USD/INR 확인 불가"]
    return ", ".join(parts)


def checklist(ctx: dict) -> str:
    e = (ctx.get("etf") or [None, None])[1]
    b = (ctx.get("brent") or [None, None])[1]
    n = (ctx.get("nifty") or [None, None])[1]
    i = (ctx.get("inr") or [None, None])[1]

    def yn(ok):
        return "확인 불가" if ok is None else ("맞음" if ok else "아님")
    t2 = [f"Brent 85 이하 10거래일 {yn(None if b is None else ctx.get('b85', 0) >= 10)}",
          f"Nifty 23,000 이상 {yn(None if n is None else n >= 23000)}",
          f"ETF 10,800 이하 {yn(None if e is None else e <= 10800)}", "FII 순매수 전환 직접 확인"]
    t3 = [f"Brent 80 이하 {yn(None if b is None else b <= 80)}", f"USD/INR 93 이하 {yn(None if i is None else i <= 93)}",
          f"Nifty 24,000 이상 {yn(None if n is None else n >= 24000)}",
          f"ETF 10,500 이하 또는 12,000 이상 {yn(None if e is None else (e <= 10500 or e >= 12000))}", "Nifty PE 17 이하 직접 확인"]
    return "2차 조건: " + ", ".join(t2) + ". 3차 조건: " + ", ".join(t3) + "."


def message(cfg: dict, fired: list[dict], rems: list[dict], ctx: dict, stamp: str) -> tuple[str, str, int]:
    items = [(ln["priority"], ln["label"], ln) for ln in fired] + [(3, r["label"], r) for r in rems]
    items.sort(key=lambda x: -x[0])
    warn = any(ln.get("warn") for ln in fired)
    top = items[0]
    if top[2] in fired and top[2]["src"] == "etf" and ctx.get("etf"):
        head = f"ETF 종가 {fmt(ctx['etf'][1])} | {top[1]}"
    else:
        head = top[1]
    title = f"{TAG} {head}" + (f" 외 {len(items) - 1}건" if len(items) > 1 else "")
    acts = []
    for ln in fired:
        if ln["key"] == "etf52wlow":
            acts.append("52주 신저가입니다. 추가 매수 중단하고 관망하세요. 인도 경상수지와 유가 원인을 확인.")
        elif ln["key"] == "brent120":
            acts.append("유가 120달러가 2주 이어졌습니다. 인도 논리가 깨졌는지(경상수지, 지정학) 확인하고 전량 손절 기준을 검토하세요.")
        elif ln["key"] == "inr100":
            acts.append("루피 약세가 빨라졌습니다. 환손실 한도를 넘었는지 판단하세요.")
        elif ln["key"] == "avg20":
            acts.append("평균가 대비 -20% 입니다. 비중 축소를 검토하세요(기계적 손절 아님).")
        elif ln["key"] == "nifty23000":
            acts.append("2차 조건 중 Nifty 지지 조건이 깨졌습니다. 다른 2차 조건이 남아 있는지 확인하세요.")
        else:
            acts.append(f"{ln['label']}: 계획표의 해당 차수 조건 하나가 맞았습니다. 다른 조건과 ISA 한도를 같이 보고 차수 실행 여부를 판단하세요.")
    for r in rems:
        acts.append(f"{r['label']}: {r['text']}")
    if rems or not warn:
        acts.append(f"주문 메모: {cfg['plan']['order_hints']}.")
    body = "\n".join([
        "이게 뭐 알림: KODEX 인도Nifty50(453810) ISA 3회 분할 계획의 조건이 바뀌었거나 점검일입니다. 전일 확정 종가 기준이고 매매 지시가 아닙니다.",
        f"{stamp} | 453810 | KODEX 인도Nifty50",
        f"지금 결과/상태: {status_line(ctx)}. {checklist(ctx)}",
        "지금 할 일: " + " ".join(acts),
        "나중에: 같은 선은 다시 무장된 뒤에만 다시 옵니다. 다음 분배 지급기준일 2027-01-29 예정(분기, 1, 4, 7, 10월 말). "
        f"Nifty PE 와 FII 흐름은 자동 점검하지 않습니다. 종료 {cfg['ends_kst'][:10]}.",
        ("익절/줄일 때: 손절은 기계적으로 하지 않습니다. 유가 120 장기화나 지정학으로 논리가 깨질 때만 정리, 인도 비중이 포트폴리오 10%를 넘으면 초과분 매도."),
        "하지 말 것: 9:00~9:30 장 초반 주문, 괴리율 +1% 이상에서 주문, 신용, 미수, 경고를 반등 기회로 읽기.",
    ])
    return title, body, max(x[0] for x in items)


def ops(stamp: str, what: str, err: str) -> tuple[str, str, int]:
    body = "\n".join([
        "이게 뭐 알림: 453810 알림의 데이터 조회 실패 안내. 매매 신호 아님.",
        f"{stamp} | 453810 | KODEX 인도Nifty50",
        f"지금 결과/상태: {what} 확인 불가 ({err[:150]}).",
        "지금 할 일: 필요하면 네이버 증권, Yahoo 에서 직접 보세요.",
        "나중에: 다음 평일 09:00 에 다시 봅니다.",
        "익절/줄일 때: 이 안내로 판단을 바꾸지 않기.",
        "하지 말 것: 조회 실패를 조건 미충족으로 읽기.",
    ])
    return f"{TAG} {what} 조회 실패 | 판정 보류", body, 3


# ---------- 실행 ----------
def run(mode: str, now: datetime, cfg: dict) -> int:
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    today = now.strftime("%Y-%m-%d")
    st = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    series: dict[str, list[float]] = {}
    ctx: dict = {}
    fails = []
    try:
        rows = etf_rows(cfg["code"], now)
        series["etf"], ctx["etf"] = [c for _, c in rows], list(rows[-1])
    except Exception as exc:  # noqa: BLE001
        fails.append(("ETF", str(exc)))
    for src, m in cfg["markets"].items():
        try:
            rows = yahoo_rows(m["symbol"], m["close_hm"], now)
            if not rows:
                raise ValueError("확정 종가 없음")
            series[src], ctx[src] = [c for _, c in rows], list(rows[-1])
        except Exception as exc:  # noqa: BLE001
            fails.append((m["symbol"], str(exc)))
    if series.get("brent"):
        ctx["b85"], ctx["b120"] = streak(series["brent"], "le", 85), streak(series["brent"], "ge", 120)
    last = st.setdefault("last_dates", {})
    cur = {k: ctx[k][0] for k in ("etf", "brent", "nifty", "inr") if ctx.get(k)}
    fresh = {k: v for k, v in series.items() if cur.get(k) != last.get(k)}  # judge a source only on a new bar
    armed = st.setdefault("armed", {})
    fired = judge(all_lines(cfg), armed, fresh)
    for k in [k for k in armed if k not in {ln["key"] for ln in all_lines(cfg)}]:
        armed.pop(k)
    last.update(cur)
    rems = due_reminders(cfg, st.setdefault("reminders_sent", {}), today) if st.get("seeded") else []
    st["seeded"] = st.get("seeded") or today
    st["last_values"] = {k: ctx[k] for k in ("etf", "brent", "nifty", "inr") if ctx.get(k)}
    st["brent_streak"] = {"le85": ctx.get("b85"), "ge120": ctx.get("b120")}
    print(f"ctx={ {k: ctx.get(k) for k in ('etf', 'brent', 'nifty', 'inr', 'b85', 'b120')} } armed={armed} fresh={sorted(fresh)}")
    out = []
    if fired or rems:
        out.append(message(cfg, fired, rems, ctx, stamp))
    if fails and st.get("fail_day") != today:
        st["fail_day"] = today
        out.append(ops(stamp, ", ".join(w for w, _ in fails), "; ".join(e for _, e in fails)))
    st["updated_at"] = stamp
    for t, m, pri in out:
        print(t)
        print(m)
        if not dry:
            publish(resolve_topic("port", cfg.get("ntfy_topic")), t, m, pri, ["warning"] if pri >= 4 else ["chart_with_upwards_trend"],
                    resolve_server(cfg.get("ntfy_server")), config_name="k453810")
            st["last_sent"] = {"at": stamp, "title": t}
    if not out:
        print("no alert")
    if dry:
        print("DRY RUN: no publish, state not written")
    else:
        STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


def selftest(cfg: dict) -> int:
    fails = 0

    def check(label, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(("ok  " if ok else "FAIL"), label, "" if ok else f"got {got!r} want {want!r}")
    L = cfg["lines"]
    by = {ln["key"]: ln for ln in L}
    armed: dict = {}
    now = {"etf": [11040.0], "brent": [117.6, 104.77], "nifty": [23440.8], "inr": [94.9]}
    check("seed silent", judge(L, armed, now), [])
    check("seed all armed", all(armed.values()), True)

    def walk(key, vals, src_hist=None):
        a = {key: True}
        out = []
        hist = list(src_hist or [])
        for v in vals:
            hist.append(v)
            out.append(bool(judge([by[key]], a, {by[key]["src"]: hist})))
        return out
    check("etf 10,800 / re-arm 11,100", walk("etf10800", [10900, 10800, 10700, 11099, 10790, 11100, 10800]), [False, True, False, False, False, False, True])
    check("etf 10,500 / re-arm 10,800", walk("etf10500", [10501, 10500, 10799, 10400, 10800, 10450]), [False, True, False, False, False, True])
    check("etf 52w low strict < 10,315 / re-arm 10,600", walk("etf52wlow", [10315, 10314, 10599, 10200, 10600, 10300]), [False, True, False, False, False, True])
    check("etf 12,000 / re-arm < 11,700", walk("etf12000", [11999, 12000, 11700, 12100, 11699, 12000]), [False, True, False, False, False, True])
    b = [90.0] * 3 + [85.0] * 9
    check("brent 85 needs 10 days", walk("brent85", [84.9], b), [True])
    check("brent 85 nine days not yet", walk("brent85", [86.0] + [85.0] * 9), [False] * 10)
    a = {"brent85": False}
    judge([by["brent85"]], a, {"brent": [85.0] * 10 + [88.0]})
    s1 = a["brent85"]
    judge([by["brent85"]], a, {"brent": [85.0] * 10 + [88.1]})
    check("brent 85 re-arm only > 88", (s1, a["brent85"]), (False, True))
    a = {"brent85": False}
    h = [85.0] * 10
    for x in (88.1,) + (85.0,) * 10:
        h.append(x)
        r = judge([by["brent85"]], a, {"brent": h})
    check("brent 85 re-armed after 88.1 then 10 more days fires", bool(r), True)
    check("brent 80 / re-arm > 83", walk("brent80", [80.1, 80, 83, 79, 83.1, 80]), [False, True, False, False, False, True])
    check("brent 120 x10 days p4", walk("brent120", [120.0] * 10), [False] * 9 + [True])
    check("brent 120 re-arm < 115", walk("brent120", [115.0, 114.9] + [121.0] * 10, [120.0] * 10)[-1], True)
    check("nifty 23,000 strict / re-arm 23,300", walk("nifty23000", [23000, 22999, 23299, 22900, 23300, 22990]), [False, True, False, False, False, True])
    check("nifty 24,000 / re-arm < 23,700", walk("nifty24000", [23999, 24000, 23700, 24100, 23699, 24000]), [False, True, False, False, False, True])
    check("inr 93 / re-arm > 94", walk("inr93", [93.01, 93, 94, 92.5, 94.01, 93]), [False, True, False, False, False, True])
    check("inr 100 / re-arm < 98", walk("inr100", [99.9, 100, 98, 101, 97.9, 100]), [False, True, False, False, False, True])
    acfg = {**cfg, "avg_price": 11000}
    al = all_lines(acfg)
    check("avg line on", (al[-1]["key"], round(al[-1]["level"])), ("avg20", 8800))
    a2 = dict(armed)
    check("avg line seeds silent", judge(al, a2, {"etf": [11040.0]}), [])
    check("avg -20% fires", [x["key"] for x in judge(al, a2, {"etf": [8790.0]})], ["etf10800", "etf10500", "etf52wlow", "avg20"])
    sent: dict = {}
    check("no reminder before", due_reminders(cfg, sent, "2026-11-26"), [])
    check("11/27 reminder", [r["key"] for r in due_reminders(cfg, sent, "2026-11-27")], ["r1127"])
    check("once", due_reminders(cfg, sent, "2026-11-30"), [])
    check("11/27 skipped when avg set", [r["key"] for r in due_reminders(acfg, {}, "2026-11-27")], [])
    check("catch-up after missed day", [r["key"] for r in due_reminders(cfg, {"r1127": "x"}, "2026-12-16")], ["r1215"])
    ctx = {"etf": ["2026-12-14", 10700.0], "brent": ["2026-12-12", 84.0], "nifty": ["2026-12-14", 23100.0], "inr": ["2026-12-12", 94.2],
           "b85": 10, "b120": 0}
    rem = [r for r in cfg["reminders"] if r["key"] == "r1215"]
    t1, b1, p1 = message(cfg, [by["etf10800"], by["brent85"]], rem, ctx, "2026-12-15 09:00 KST")
    check("combo title", (t1, p1), ("[453810] ETF 종가 10,700 | 10,800 이하, 2차 트리거(ETF 가격 조건) 외 2건", 3))
    check("checklist", all(x in b1 for x in ("Brent 85 이하 10거래일 맞음", "Nifty 23,000 이상 맞음", "ETF 10,800 이하 맞음", "USD/INR 93 이하 아님",
                                             "85 이하 연속 10일", "iNAV 대비 -0.3~0.5%", "괴리율 +1% 이상이면 보류", "10시 이후")), True)
    rem3 = [r for r in cfg["reminders"] if r["key"] == "r0315"]
    t3, b3, _ = message(cfg, [], rem3, ctx, "s")
    check("3/15 rule text", "3개월 연속 미충족 시 2차 금액의 절반만 시간 기반" in b3, True)
    warns = [by[k] for k in ("etf52wlow", "brent120", "inr100", "nifty23000")]
    ws = [message(cfg, [w], [], ctx, "s") for w in warns] + [message(acfg, [al[-1]], [], ctx, "s"), message(cfg, warns, [], ctx, "s")]
    check("warning priorities", [p for _, _, p in ws], [4, 4, 4, 3, 4, 4])
    check("no buy wording in warnings", [t for t, b, _ in ws if any(w in t + b for w in BAD_WORDS)], [])
    check("52w title", ws[0][0], "[453810] ETF 종가 10,700 | 10,315 하회, 52주 신저가 경신, 추가 매수 중단 관망")
    allm = [t1, b1, t3, b3] + [x for t, b, _ in ws for x in (t, b)]
    check("ticker in titles", all(t.startswith("[453810]") for t in [t1, t3] + [t for t, _, _ in ws]), True)
    check("first line 이게 뭐 알림", all(b.split("\n")[0].startswith("이게 뭐 알림:") for b in [b1, b3] + [b for _, b, _ in ws]), True)
    check("six parts", [x.split(":")[0] for x in b1.split("\n")[2:]], ["지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"])
    check("no dot/dash", any(ch in "".join(allm) for ch in "\u00b7\u2014"), False)
    check("streak", (streak([90, 85, 84, 85], "le", 85), streak([84, 86], "le", 85)), (3, 0))
    print("selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


def confirm(cfg: dict) -> int:
    st = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    ctx = {k: v for k, v in (st.get("last_values") or {}).items()}
    ctx["b85"], ctx["b120"] = (st.get("brent_streak") or {}).get("le85") or 0, (st.get("brent_streak") or {}).get("ge120") or 0
    body = "\n".join([
        "이게 뭐 알림: KODEX 인도Nifty50(453810) 분할 계획 알림 설치 확인입니다. 매매 신호 아님.",
        f"{datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')} | 453810 | KODEX 인도Nifty50",
        f"지금 결과/상태: {status_line(ctx)}. 모든 선 무장, 지금은 발송 대상 없음.",
        "지금 할 일: 없음. 평일 09:00 KST 에 ETF 10,800, 10,500, 12,000(p3), 10,315 하회(p4), Brent 85 2주, 80(p3), 120 2주(p4), "
        "Nifty 23,000, 24,000(p3), USD/INR 93(p3), 100(p4)을 상태가 바뀔 때만 봅니다.",
        "나중에: 점검일 11/27, 12/15, 1/15, 2/15, 3/15. 평균가를 알려 주면 -20% 비중 축소 검토 선이 켜집니다. Nifty PE, FII 흐름은 자동 점검 안 함. 종료 2027-04-30.",
        "익절/줄일 때: 해당 없음.",
        "하지 말 것: 이 설치 확인을 매수/매도 신호로 읽기.",
    ])
    title = f"{TAG} KODEX 인도Nifty50 설치 확인 | 매매 신호 아님"
    print(title)
    print(body)
    publish(resolve_topic("port", cfg.get("ntfy_topic")), title, body, 3, ["white_check_mark"], resolve_server(cfg.get("ntfy_server")),
            config_name="k453810")
    return 0


def main() -> int:
    args = [a.lower() for a in sys.argv[1:]]
    mode = args[0] if args else "auto"
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    if mode == "selftest":
        return selftest(cfg)
    if mode == "confirm":
        return confirm(cfg)
    if mode not in {"auto", "dry"}:
        print(__doc__)
        return 2
    now = datetime.now(KST)
    ends = cfg.get("ends_kst")
    if ends and now > datetime.strptime(ends, "%Y-%m-%d %H:%M").replace(tzinfo=KST):
        print(f"k453810 ended {ends} KST; silent")
        return 0
    return run(mode, now, cfg)


if __name__ == "__main__":
    sys.exit(main())
