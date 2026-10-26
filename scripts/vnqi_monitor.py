#!/usr/bin/env python3
"""[VNQI] Vanguard Global ex-U.S. Real Estate ETF: price line + annual distribution (approved in chat 2026-10-26).

  1. Price: completed US close < 41.50 -> one alert per crossing (priority 4).
     Re-arm needs a 1% buffer (pcorpx / ratesbell style): back to armed only on a close above 41.915.
     The first run (no state) only records the current close, it never sends.
     Dedupe by the bar's own US/Eastern date (state last_date).
  2. Dividend: a new ex-date newer than state div_last_ex -> one alert with amount per share,
     whether it is >= 2.00 (the user's add-weight threshold) and the yield vs the latest close.
     Source: Nasdaq dividends API (4 decimals, declaration date), fallback Yahoo events=div.

Runs on the existing morning 09:00 KST slot (after the US close). No new cron.
Modes: auto | force (re-check the latest bar even if seen; still respects arm/ex-date state)
       | dry (print only, no ntfy, no state) | selftest | install (one install-confirmation send, no state).
Not investment advice.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
NY = ZoneInfo("America/New_York")
CFG = ROOT / "configs" / "vnqi.json"
STATE = ROOT / "state" / "vnqi.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts", "Accept": "application/json"}


def load_cfg() -> dict:
    return json.loads(CFG.read_text(encoding="utf-8"))


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(st: dict, dry: bool) -> None:
    if dry:
        return
    st["updated_at"] = datetime.now(KST).isoformat(timespec="seconds")
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def http_json(url: str, timeout: int = 20) -> dict:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


# ---------- fetch ----------

def parse_chart(res: dict, now_ny: datetime) -> tuple[list[tuple[date, float]], list[dict]]:
    """Completed daily bars (US/Eastern date, close) ascending, plus Yahoo dividends."""
    out: dict[date, float] = {}
    for t, c in zip(res.get("timestamp") or [], res["indicators"]["quote"][0].get("close") or []):
        if c is not None:
            out[datetime.fromtimestamp(t, NY).date()] = float(c)
    meta = res.get("meta") or {}
    rmt, rmp = meta.get("regularMarketTime"), meta.get("regularMarketPrice")
    if rmt and rmp:
        mt = datetime.fromtimestamp(rmt, NY)
        if mt.date() not in out and mt.hour * 60 + mt.minute >= 16 * 60:
            out[mt.date()] = float(rmp)
    today = now_ny.date()
    after_close = now_ny.hour * 60 + now_ny.minute >= 16 * 60 + 10
    bars = sorted((d, c) for d, c in out.items() if d < today or (d == today and after_close))
    divs = []
    for v in ((res.get("events") or {}).get("dividends") or {}).values():
        try:
            divs.append({"ex_date": str(datetime.fromtimestamp(int(v["date"]), NY).date()), "amount": float(v["amount"]),
                         "pay_date": None, "decl_date": None, "source": "Yahoo(소수 3자리 반올림)"})
        except (KeyError, TypeError, ValueError):
            continue
    return bars, sorted(divs, key=lambda x: x["ex_date"])


def fetch_chart(cfg: dict, now_ny: datetime) -> tuple[list[tuple[date, float]], list[dict], str]:
    errs = []
    for key in ("bars", "bars_fallback"):
        url = cfg["sources"].get(key)
        if not url:
            continue
        try:
            bars, divs = parse_chart(http_json(url)["chart"]["result"][0], now_ny)
            if bars:
                return bars, divs, "Yahoo " + ("query1" if key == "bars" else "query2")
            errs.append(f"{key}: no bars")
        except Exception as exc:
            errs.append(f"{key}: {exc}")
    raise RuntimeError("; ".join(errs))


def _us_date(s: str | None) -> str | None:
    try:
        return str(datetime.strptime((s or "").strip(), "%m/%d/%Y").date())
    except ValueError:
        return None


def parse_nasdaq(data: dict) -> list[dict]:
    rows = (((data or {}).get("data") or {}).get("dividends") or {}).get("rows") or []
    out = []
    for r in rows:
        ex = _us_date(r.get("exOrEffDate"))
        try:
            amt = float(str(r.get("amount", "")).replace("$", "").replace(",", ""))
        except ValueError:
            continue
        if not ex or amt <= 0 or str(r.get("type", "Cash")).lower() != "cash":
            continue
        out.append({"ex_date": ex, "amount": amt, "pay_date": _us_date(r.get("paymentDate")),
                    "decl_date": _us_date(r.get("declarationDate")), "source": "Nasdaq"})
    return sorted(out, key=lambda x: x["ex_date"])


def fetch_dividends(cfg: dict, yahoo_divs: list[dict]) -> tuple[list[dict], str]:
    try:
        rows = parse_nasdaq(http_json(cfg["sources"]["dividends"], 25))
        if rows:
            return rows, "Nasdaq"
        print("nasdaq dividends: empty rows; using Yahoo")
    except Exception as exc:
        print(f"nasdaq dividends fetch failed: {exc}; using Yahoo")
    return yahoo_divs, "Yahoo"


# ---------- core (pure) ----------

def hit(level: dict, close: float) -> bool:
    p = float(level["price"])
    return close <= p if level["op"] == "le" else close < p


def rearm_at(level: dict, buf_pct: float) -> float:
    return round(float(level["price"]) * (1 + buf_pct / 100), 4)


def step(levels: list[dict], buf_pct: float, armed: dict | None, close: float) -> tuple[dict, list[str]]:
    """One completed close. armed[key] True = watching. armed None = first run: init, nothing fires."""
    close = round(float(close), 4)
    if armed is None:
        return {lv["key"]: not hit(lv, close) for lv in levels}, []
    new, fired = dict(armed), []
    for lv in levels:
        k = lv["key"]
        if new.get(k, True):
            if hit(lv, close):
                new[k] = False
                fired.append(k)
        elif close > rearm_at(lv, buf_pct):
            new[k] = True  # quiet re-arm
    return new, fired


def new_dividends(divs: list[dict], last_ex: str | None) -> list[dict]:
    return [d for d in divs if last_ex is None or d["ex_date"] > last_ex]


def prev_dividend(divs: list[dict], hist: list[dict], ex: str) -> dict | None:
    pool = {d["ex_date"]: d for d in hist}
    pool.update({d["ex_date"]: d for d in divs})
    older = [pool[k] for k in sorted(pool) if k < ex]
    return older[-1] if older else None


def usd(v: float, nd: int = 2) -> str:
    return f"{v:,.{nd}f}달러"


def price_message(cfg: dict, fired: list[str], close: float, day: str, prev_close: float | None, last_div: dict | None) -> tuple[str, str, int]:
    lv = {x["key"]: x for x in cfg["levels"]}
    buf = float(cfg.get("rearm_buffer_pct", 1.0))
    top = max((lv[k] for k in fired), key=lambda x: x["priority"])
    ref = cfg.get("reference", {})
    th = float(cfg.get("dividend", {}).get("add_threshold", 2.0))
    title = f"[VNQI] 종가 {usd(close)}, {usd(top['price'])} 아래 | 한 번에 사지 말고 분배금 확인"
    chg = f", 전 거래일 대비 {(close / prev_close - 1) * 100:+.1f}%" if prev_close else ""
    ylin = ""
    if last_div:
        ylin = f" 직전 연 분배금 {usd(last_div['amount'], 4)}({last_div['ex_date']}) 기준 수익률 {last_div['amount'] / close * 100:.2f}%."
    lines = [
        f"{datetime.now(KST):%Y-%m-%d %H:%M} KST | VNQI | {cfg.get('display', 'VNQI')}",
        "이게 뭐 알림: " + cfg["explain"],
        f"지금 결과/상태: {top['label']}. 미국 종가 {usd(close)}({day} 미 동부{chg}). 선 {usd(top['price'])}, "
        f"{ref.get('as_of')} 기준 52주 저점 {usd(float(ref.get('w52_low', 0)))}.{ylin}",
        f"지금 할 일: 52주 저점권 아래입니다. 비중을 늘릴 생각이면 12월 새 분배금이 {usd(th)} 이상인지 본 뒤 나눠서 검토. 오늘 한 번에 넣지 않기.",
        f"나중에: 종가 {usd(rearm_at(top, buf))} 위로 회복한 뒤 다시 {usd(top['price'])} 아래로 오면 또 울림. 새 분배금이 나오면 따로 알림.",
        f"익절/줄일 때: 새 분배금이 {usd(th)} 아래로 줄고 가격도 {usd(top['price'])} 아래에 머물면 비중 축소 검토.",
        "하지 말 것: 하락 추격 물타기, 계획 금액 초과, 신용/미수, 이 알림만 보고 주문. 투자 권유 아님.",
    ]
    return title, "\n".join(lines), int(top["priority"])


def dividend_message(cfg: dict, d: dict, prev: dict | None, close: float | None, close_day: str | None) -> tuple[str, str, int]:
    th = float(cfg.get("dividend", {}).get("add_threshold", 2.0))
    ok = d["amount"] >= th
    amt = usd(d["amount"], 4)
    title = f"[VNQI] 새 분배금 주당 {amt} ({usd(th)} {'이상' if ok else '미만'}) | " + ("비중 추가 검토" if ok else "추가 매수 보류")
    dates = f"배당락일 {d['ex_date']}"
    if d.get("pay_date"):
        dates += f", 지급일 {d['pay_date']}"
    if d.get("decl_date"):
        dates += f", 선언일 {d['decl_date']}"
    yl = f"최근 종가 {usd(close)}({close_day}) 대비 수익률 {d['amount'] / close * 100:.2f}%." if close else "최근 종가 조회 실패로 수익률 계산 못 함."
    yoy = ""
    if prev:
        yoy = f" 직전 {usd(prev['amount'], 4)}({prev['ex_date']}) 대비 {(d['amount'] / prev['amount'] - 1) * 100:+.1f}%."
    todo = (f"{usd(th)} 이상이라 비중 추가 검토 조건 충족. 정해 둔 금액 안에서 나눠서 검토." if ok
            else f"{usd(th)} 미만이라 비중 추가 조건 아님. 보유 유지, 추가 매수 보류.")
    lines = [
        f"{datetime.now(KST):%Y-%m-%d %H:%M} KST | VNQI | {cfg.get('display', 'VNQI')}",
        "이게 뭐 알림: " + cfg["explain"],
        f"지금 결과/상태: 새 연 분배금 주당 {amt}, {dates} ({d.get('source', '')}). 기준 {usd(th)} {'이상' if ok else '미만'}. {yl}{yoy}",
        "지금 할 일: " + todo,
        f"나중에: 다음 분배금은 보통 내년 12월. 가격 {usd(float(cfg['levels'][0]['price']))} 아래 알림은 계속 켜져 있음.",
        f"익절/줄일 때: 분배금이 2년 연속 줄고 가격이 {usd(float(cfg['levels'][0]['price']))} 아래에 머물면 비중 축소 검토.",
        "하지 말 것: 분배금만 보고 배당락일 직전 몰빵 매수(배당락일에 가격이 그만큼 빠짐), 신용/미수. 투자 권유 아님.",
    ]
    return title, "\n".join(lines), 3


def install_message(cfg: dict, close: float | None, day: str | None, last_div: dict | None) -> tuple[str, str]:
    lv = cfg["levels"][0]
    th = float(cfg.get("dividend", {}).get("add_threshold", 2.0))
    now_txt = f"미국 종가 {usd(close)}({day}), 선 {usd(float(lv['price']))}까지 {(close / float(lv['price']) - 1) * 100:+.1f}%." if close else "시세 조회 실패."
    div_txt = f" 직전 연 분배금 {usd(last_div['amount'], 4)}({last_div['ex_date']})." if last_div else ""
    lines = [
        f"{datetime.now(KST):%Y-%m-%d %H:%M} KST | VNQI | 설치 확인",
        "이게 뭐 알림: " + cfg["explain"],
        f"지금 결과/상태: 설치 확인용 테스트 발송입니다. {now_txt}{div_txt}",
        "지금 할 일: 알림이 도착했는지 확인만 하세요. 매매할 일 없음.",
        f"나중에: 평일 09:00 KST 에 전날 미국 종가를 보고 {usd(float(lv['price']))} 아래로 새로 들어가면 1번, 새 분배금이 나오면 1번(주당 금액, {usd(th)} 이상 여부, 수익률).",
        "익절/줄일 때: 해당 없음.",
        "하지 말 것: 이 메시지를 매매 신호로 해석하기.",
    ]
    return "[VNQI] 설치 확인 | 매매 신호 아님", "\n".join(lines)


# ---------- selftest ----------

def selftest() -> int:
    cfg = load_cfg()
    L, buf = cfg["levels"], float(cfg["rearm_buffer_pct"])
    fails = 0

    def check(label, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(("ok  " if ok else "FAIL"), label, got, "" if ok else f"want {want}")

    check("init 42.71", step(L, buf, None, 42.71), ({"below4150": True}, []))
    check("init 41.0 no fire", step(L, buf, None, 41.0), ({"below4150": False}, []))
    A = {"below4150": True}
    check("41.50 exact no fire (strict <)", step(L, buf, A, 41.5)[1], [])
    check("41.49 fires", step(L, buf, A, 41.49)[1], ["below4150"])
    walk = [(42.0, []), (41.4, ["below4150"]), (41.2, []), (41.9, []), (41.915, []), (41.45, []), (41.92, []), (41.49, ["below4150"]), (45, []), (40, ["below4150"])]
    st = dict(A)
    for i, (c, want) in enumerate(walk):
        st, f = step(L, buf, st, c)
        check(f"walk{i} {c}", f, want)
    check("rearm price", rearm_at(L[0], buf), 41.915)
    nas = {"data": {"dividends": {"rows": [
        {"exOrEffDate": "12/18/2026", "type": "Cash", "amount": "$1.9800", "declarationDate": "12/16/2026", "paymentDate": "12/22/2026"},
        {"exOrEffDate": "12/19/2025", "type": "Cash", "amount": "$2.1561", "declarationDate": "12/17/2025", "paymentDate": "12/23/2025"},
        {"exOrEffDate": "N/A", "type": "Cash", "amount": "N/A"}]}}}
    rows = parse_nasdaq(nas)
    check("nasdaq parse", [(r["ex_date"], r["amount"]) for r in rows], [("2025-12-19", 2.1561), ("2026-12-18", 1.98)])
    check("new div after 2025-12-19", [d["ex_date"] for d in new_dividends(rows, "2025-12-19")], ["2026-12-18"])
    check("no new div", new_dividends(rows[:1], "2025-12-19"), [])
    hist = cfg["dividend"]["history"]
    t1, b1, _ = dividend_message(cfg, rows[1], prev_dividend(rows, hist, rows[1]["ex_date"]), 42.0, "2026-12-15")
    check("below threshold title", "미만" in t1 and "보류" in t1, True)
    hi = dict(rows[1], amount=2.21)
    t2, b2, _ = dividend_message(cfg, hi, prev_dividend(rows, hist, hi["ex_date"]), 42.0, "2026-12-15")
    check("above threshold title", "이상" in t2 and "비중 추가 검토" in t2, True)
    check("yield in body", "5.26%" in b2, True)
    check("exact 2.00 counts as 이상", "이상" in dividend_message(cfg, dict(rows[1], amount=2.0), None, 42.0, "x")[0], True)
    t3, b3, p3 = price_message(cfg, ["below4150"], 41.4, "2026-11-03", 41.8, hist[0])
    check("price priority 4", p3, 4)
    t4, b4 = install_message(cfg, 42.71, "2026-10-23", hist[0])
    for lab, txt in (("div", t1 + b1 + t2 + b2), ("price", t3 + b3), ("install", t4 + b4)):
        check(f"no middle dot / em-dash ({lab})", any(ch in txt for ch in "\u00b7\u2014"), False)
    print(t2)
    print(t3)
    print("selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


# ---------- main ----------

def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "force", "dry", "install"}:
        print(f"unknown mode {mode}")
        return 2
    dry = mode == "dry"
    cfg = load_cfg()
    now = datetime.now(KST)
    print(f"{now.isoformat(timespec='seconds')} vnqi mode={mode}")
    if dry:
        print("DRY RUN (no ntfy, no state write)")
    st = load_state()
    hist = cfg.get("dividend", {}).get("history") or []
    try:
        bars, ydivs, src = fetch_chart(cfg, now.astimezone(NY))
    except Exception as exc:
        print(f"bars fetch failed: {exc}")
        if mode == "install":
            return 1
        if not st.get("fail_notified"):
            title = "[VNQI] 시세 조회 실패 | 매매 신호 아님"
            msg = f"VNQI 일봉 조회 실패(야후 query1/query2): {exc}. 다음 09:00 실행에서 다시 봅니다. 분배금 확인도 다음 실행으로 미룸."
            if dry:
                print(f"DRY RUN would send (ops): {title}\n{msg}")
            else:
                publish(resolve_topic("ops"), title, msg, 3, tags=["warning"])
                st["fail_notified"] = True
                save_state(st, dry)
        return 0
    print(f"price source: {src}, {len(bars)} bars, last {bars[-1]}")
    divs, dsrc = fetch_dividends(cfg, ydivs)
    print(f"dividend source: {dsrc}, latest {divs[-1] if divs else None}")
    last_d, last_c = bars[-1]
    known = [d for d in divs if d["ex_date"] <= (st.get("div_last_ex") or "9999")] or hist
    last_div = known[-1] if known else None

    if mode == "install":
        title, msg = install_message(cfg, last_c, str(last_d), last_div)
        print(f"{title}\n{msg}")
        publish(resolve_topic("port"), title, msg, 3, tags=["white_check_mark"], config_name="vnqi")
        print("install confirmation sent (state untouched)")
        return 0

    st["fail_notified"] = False
    L, buf = cfg["levels"], float(cfg.get("rearm_buffer_pct", 1.0))

    # 1) price line
    if (now.date() - last_d).days > int(cfg.get("stale_days", 6)):
        print(f"stale bar {last_d}; skip price line")
    elif not st.get("initialized"):
        armed, _ = step(L, buf, None, last_c)
        st.update({"initialized": True, "armed": armed, "last_date": str(last_d), "last_close": round(last_c, 4),
                   "init_note": f"initialized from {last_d} close {last_c:.2f}, no send"})
        print(f"init from {last_d} close {last_c:.2f}: {armed} (no send)")
    else:
        armed = st.get("armed") or {lv["key"]: True for lv in L}
        prev_date = st.get("last_date")
        new_bars = [(d, c) for d, c in bars if prev_date is None or str(d) > prev_date]
        if mode == "force" and not new_bars:
            new_bars = bars[-1:]
        if not new_bars:
            print(f"no new bar after {prev_date}; armed={armed}; last close {last_c:.2f} ({last_d})")
        else:
            fired_all: list[str] = []
            prev_close = st.get("last_close")
            for d, c in new_bars:
                armed, fired = step(L, buf, armed, c)
                print(f"bar {d} close {c:.2f} -> fired={fired} armed={armed}")
                fired_all += [k for k in fired if k not in fired_all]
            d, c = new_bars[-1]
            if len(new_bars) > 1:
                prev_close = new_bars[-2][1]
            for lv in L:
                print(f"  line {lv['price']:.2f}: {'armed' if armed[lv['key']] else 'triggered'}, distance {(c / lv['price'] - 1) * 100:+.1f}%, rearm above {rearm_at(lv, buf):.3f}")
            if fired_all:
                title, msg, prio = price_message(cfg, fired_all, c, str(d), prev_close, last_div)
                if dry:
                    print(f"DRY RUN would send (p{prio}): {title}\n{msg}")
                else:
                    publish(resolve_topic("port"), title, msg, prio, tags=["chart_with_downwards_trend"], config_name="vnqi")
            else:
                print("no crossing; silent")
            st.update({"armed": armed, "last_date": str(d), "last_close": round(c, 4)})

    # 2) annual distribution
    if not divs:
        print("no dividend rows; skip dividend check")
    elif not st.get("div_last_ex"):
        st.update({"div_last_ex": divs[-1]["ex_date"], "div_last_amount": divs[-1]["amount"]})
        print(f"dividend init from {divs[-1]} (no send)")
    else:
        fresh = new_dividends(divs, st["div_last_ex"])
        if not fresh:
            print(f"no new distribution after {st['div_last_ex']}; silent")
        for dv in fresh:
            prev = prev_dividend(divs, hist, dv["ex_date"])
            title, msg, prio = dividend_message(cfg, dv, prev, last_c, str(last_d))
            if dry:
                print(f"DRY RUN would send (p{prio}): {title}\n{msg}")
            else:
                publish(resolve_topic("port"), title, msg, prio, tags=["moneybag"], config_name="vnqi")
            st.update({"div_last_ex": dv["ex_date"], "div_last_amount": dv["amount"], "div_last_source": dv.get("source")})
    save_state(st, dry)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
