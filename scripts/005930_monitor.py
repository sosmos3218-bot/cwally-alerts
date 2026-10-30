#!/usr/bin/env python3
"""[005930] 삼성전자 close-based plan alert on the k203780 19:00 KST slot.

This file is also the engine for scripts/000660_monitor.py (SK하이닉스), which loads it
and calls run("000660"). Each name has its own configs/<name>.json and state/<name>.json.

Events
  lines     per configs/<name>.json "lines": op le (close <= level), ge (close >= level), lt (close < level).
            Each line fires once. Re-arm only after the close moves more than rearm_pct (1%) back across
            the line (le/lt: close > level*1.01, ge: close < level*0.99). Every new trading day after
            state last_day is judged once, in order, so a late or double run never re-sends.
  us10y     only in configs/005930.json: Yahoo ^TNX >= warn_min (5.30%) -> one shared warning for
            005930 and 000660, re-arms below rearm_below (5.20%). 000660 never sends it.
  extra     configs "extra_lines" (2026-10-26, levels folded in from samup/hynixup/hynixlad/lgcns): notify-only
            levels in the same wording, never buy/trim/stop plan lines. Same op/fire-once rule, own state
            st["extra"][key]; per line: rearm_pct (default cfg rearm_pct; 0 = re-arm as soon as the close is back
            across), optional rearm_title/rearm_action (also send on re-arm, e.g. 회복), until (last session day),
            priority, buy_advice (action dropped when the close is under the stop line). Sent as their own
            message, so a plan-line message is unchanged.
  pair      optional configs "pair" {code, yahoo_symbol, short} + "ratio_lines" (2026-10-28, 0220WL): a second KRX
            symbol read the same way (Yahoo regular close only, hold on failure). Each session where BOTH regular closes
            exist is judged in order: discount % = (pair - main) / pair * 100, line ops le/ge/lt on that %, fire once,
            re-arm after moving back more than rearm_pp (%p). Own cursor st["ratio"]["last_day"]; a session where either
            close is missing stops the walk (judged later). Absent keys: nothing changes.
  notices   optional configs "notice_events" (dated filing checks with a checklist, window start..end, sent once when
            the filing is on the Naver list or on the end day as 공시 확인 필요, catch-up catch_up_days) and
            "notice_watch" (codes + title keywords, one alert per filing, dedupe by filing no in st["notice_seen"]).
  time      optional configs "time_checks" (2026-10-29, 0220wl_close): dated reminders with no filing lookup, each
            {id, start, end, title, checklist, optional what/later/trim/dont/catch_up_days/priority}. Sent once on the
            end day or the first run after it within catch_up_days (default 3), never at the window start; dedupe by id
            in st["time_done"]. Wording never says 공시 확인 필요.
  footer    optional configs "footer_note" (2026-10-29, 0220wl_close): one line appended to the end of every message
            this config sends. Absent keys (005930/000660/064400): nothing changes.
  earnings  on configs earnings.date (KST session day), one 실적 체크 message, flag earn_done[date];
            a late slot catches up within catch_up_days. Filing detection: Naver stock notice feed
            (KRX/DART disclosure mirror, no key) for a 영업(잠정)실적 title on/after the day before;
            not found or feed failed -> "공시 확인 필요". (005930 used this up to 2026 Q3; still used by 000660/064400.)
  earn_auto optional configs "earn_auto" (2026-10-30, 005930 only): quarterly earnings auto-check with no hardcoded date.
            The Naver notice feed gives the quarter (latest 영업(잠정)실적 filing month: Jan-Mar = previous Q4, Apr-Jun Q1,
            Jul-Sep Q2, Oct-Dec Q3) and the results day (기업설명회(IR) 개최 안내공시 일시, else the quarter's second results
            filing). On/after the results day: one filing notice (실적 체크), then on every KRX trading-day 19:00 run the
            디일렉 transcript "삼성전자 YYYY년 N분기 실적발표 컨퍼런스콜 전문" is looked up (scripts/earncall_lib.py, shared
            with secq3) and the 3 checks are judged 문제없음/경계/확인 불가 with a quoted sentence; one result per quarter
            (state earn_auto[YYYYQn].sent). No transcript within wait_trading_days -> one 확인 불가 message. Quarters before
            start_quarter are never sent automatically; mode backfill sends earn_auto.backfill (2026Q3) once.
Data: judgment uses only the KRX regular-session close from Yahoo chart <code>.KS (chart close +
regularMarketPrice stamped at/after 15:30 KST). Hold on failure: if Yahoo fails or has no regular close for
the session, nothing is judged from any other source; a 조회 실패 notice goes to cwally-ops (once a day) and
the session stays unprocessed (state last_day) so the next run catches it up. Naver's daily price is the NXT
after-market price (approved 2026-10-24 21:48 KST): it is only printed as a reference line in sent messages
("참고: NXT 애프터마켓 ..."), never judged. No new secret.

Modes: auto (slot) | force (ignore holiday) | dry (print, no ntfy, no state) | selftest (offline checks) |
       install (one install-confirmation send to cwally-port, no state) | seed (write state from live data, no ntfy) |
       backfill (005930: send the earn_auto.backfill quarter once, state earn_auto[q]) | backfill-dry (same, print only:
       no ntfy, no state) | earn-dry (print the live quarter detection and what the earn_auto step would send, no ntfy, no state)
"""
from __future__ import annotations

import copy
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_calendar import is_trading_day, session_day  # noqa: E402
from ntfy_util import append_ends, publish, resolve_server, resolve_topic  # noqa: E402
import earncall_lib as ec  # noqa: E402

KST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts krplan"}
FORBIDDEN = ("\u00b7", "\u2014", "\u318d", "\u30fb", "\u2027")  # middle dots, em-dash: never in message text


def scrub(text: str) -> str:
    """Outside text (filing titles) may carry a middle dot; replace it before it reaches a message."""
    for ch in FORBIDDEN:
        text = text.replace(ch, ", " if ch != "\u2014" else " ")
    return text
REGULAR_CLOSE_HM = (15, 30)
NXT_END_HM = (20, 0)
SEVERITY = ["stop", "trim2", "trim1", "buy3", "buy2", "buy1"]


# ---------- data ----------
def get_json(url: str):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def num(v) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return None


def yahoo_rows(cfg: dict, symbol: str, regular_only: bool = False) -> list[dict]:
    res = get_json(cfg["sources"]["yahoo"].format(symbol=urllib.parse.quote(symbol)))["chart"]["result"][0]
    ts = res.get("timestamp") or []
    closes = res["indicators"]["quote"][0]["close"]
    rows = {}
    for t, c in zip(ts, closes):
        if c is not None:
            rows[datetime.fromtimestamp(t, KST).date().isoformat()] = float(c)
    meta = res.get("meta") or {}
    if meta.get("regularMarketPrice") is not None and meta.get("regularMarketTime"):
        mt = datetime.fromtimestamp(meta["regularMarketTime"], KST)
        d = mt.date().isoformat()
        if regular_only and (mt.hour, mt.minute) < REGULAR_CLOSE_HM:
            # session not closed in Yahoo yet: today's candle is intraday, not a regular close
            rows = {k: v for k, v in rows.items() if k < d}
        elif d in rows or not rows or d > max(rows):
            rows[d] = float(meta["regularMarketPrice"])
    out = [{"day": d, "close": c} for d, c in sorted(rows.items())]
    if not out:
        raise RuntimeError(f"{symbol} 0건")
    return out


def naver_rows(cfg: dict) -> list[dict]:
    raw = get_json(cfg["sources"]["naver_price"].format(code=cfg["code"]))
    rows = []
    for r in raw if isinstance(raw, list) else []:
        d = str(r.get("localTradedAt") or "")[:10]
        c = num(r.get("closePrice"))
        if len(d) == 10 and c:
            rows.append({"day": d, "close": c})
    rows.sort(key=lambda x: x["day"])
    if not rows:
        raise RuntimeError("네이버 시세 0건")
    return rows


def tnx(cfg: dict) -> float:
    rows = yahoo_rows(cfg, cfg["us10y"]["ticker"])
    return rows[-1]["close"]


def html_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s.replace("&nbsp;", " ")).strip()


def find_filing(cfg: dict, day: str) -> dict | None:
    """Naver stock notice feed (no key). Returns {'title','datetime'} for an earnings filing on/after day-1."""
    raw = get_json(cfg["sources"]["naver_notice"].format(code=cfg["code"]))
    since = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    for r in raw if isinstance(raw, list) else []:
        title = str((r or {}).get("title") or "").strip()
        dt = str((r or {}).get("datetime") or "")
        if dt[:10] < since:
            continue
        if any(k in title for k in cfg["earnings"]["filing_keywords"]) and "예고" not in title:
            return {"title": scrub(title), "datetime": dt[:16].replace("T", " ")}
    return None


def regular_gap(rows: list[dict], now: datetime) -> str | None:
    """Session day whose regular close should exist by now but is missing from rows (hold), else None."""
    sday = session_day(now)
    closed = now.date() > sday or (now.hour, now.minute) >= REGULAR_CLOSE_HM
    if is_trading_day(sday) and closed and (not rows or rows[-1]["day"] < sday.isoformat()):
        return sday.isoformat()
    return None


def collect(cfg: dict, now: datetime | None = None) -> tuple[dict, list[str]]:
    """Rows come only from Yahoo regular closes. No other price source is ever judged (hold on failure)."""
    now = now or datetime.now(KST)
    data: dict = {"src": None}
    fails: list[str] = []
    try:
        data["rows"], data["src"] = yahoo_rows(cfg, cfg["yahoo_symbol"], regular_only=True), "Yahoo 정규장"
    except Exception as exc:
        data["rows"] = []
        fails.append(f"{cfg['code']} 정규장 종가 (Yahoo {str(exc)[:80]}; 판단 보류, 다음 실행에서 따라잡음)")
    else:
        gap = regular_gap(data["rows"], now)
        if gap:
            fails.append(f"{cfg['code']} {gap} 정규장 종가 없음 (Yahoo; 판단 보류, 다음 실행에서 따라잡음)")
    pair = cfg.get("pair")
    if pair:
        try:
            data["pair_rows"] = yahoo_rows(cfg, pair["yahoo_symbol"], regular_only=True)
        except Exception as exc:
            data["pair_rows"] = []
            fails.append(f"{pair['code']} 정규장 종가 (Yahoo {str(exc)[:80]}; 괴리율 판단 보류, 다음 실행에서 따라잡음)")
        else:
            gap = regular_gap(data["pair_rows"], now)
            if gap:
                fails.append(f"{pair['code']} {gap} 정규장 종가 없음 (Yahoo; 괴리율 판단 보류, 다음 실행에서 따라잡음)")
    if cfg.get("us10y"):
        try:
            data["us10y"] = tnx(cfg)
        except Exception as exc:
            fails.append(f"미국 10년물 ({exc})")
    return data, fails


def nxt_line(name: str, regular: float | None, nxt: float | None, now: datetime) -> str:
    """Reference only (never judged). 진행 중 before 20:00 KST of the session day, 마감 at/after."""
    if regular is None or nxt is None or abs(nxt - regular) < 0.5:
        return f"{name} NXT 정보 없음"
    sday = session_day(now)
    done = now.date() > sday or (now.hour, now.minute) >= NXT_END_HM
    label = "마감" if done else "진행 중"
    return f"{name} NXT 애프터마켓 ({now.strftime('%H:%M')} 기준, {label}) {fmt(nxt)}, 정규장 대비 {dist(nxt, regular):+.1f}%"


def nxt_ref(cfg: dict, rows: list[dict] | None, now: datetime) -> str:
    """Naver daily price for the session day = NXT after-market price; compared with the Yahoo regular close."""
    sday = session_day(now).isoformat()
    try:
        regular = next((r["close"] for r in (rows or []) if r["day"] == sday), None)
        if rows is None:
            regular = next((r["close"] for r in yahoo_rows(cfg, cfg["yahoo_symbol"], regular_only=True) if r["day"] == sday), None)
        nxt = next((r["close"] for r in naver_rows(cfg) if r["day"] == sday), None)
    except Exception as exc:
        print(f"nxt_ref {cfg['code']} skipped: {exc}")
        regular = nxt = None
    return nxt_line(cfg["short"], regular, nxt, now)


def ref_text(parts: list[str]) -> str:
    return "참고: " + "; ".join(parts) + ". 판단에는 정규장 종가만 씁니다."


# ---------- state ----------
def state_path(name: str) -> Path:
    return ROOT / "state" / f"{name}.json"


def default_state(cfg: dict) -> dict:
    st = {"updated_at": None, "seeded": False, "last_day": None, "last_close": None, "last_fail_day": None,
          "lines": {ln["key"]: {"armed": True, "fired_day": None} for ln in cfg["lines"]}, "earn_done": {}}
    if cfg.get("us10y"):
        st["us10y"] = {"armed": True, "fired_day": None, "last": None}
    if cfg.get("extra_lines"):
        st["extra"] = {ln["key"]: {"armed": True, "fired_day": None} for ln in cfg["extra_lines"]}
    if cfg.get("ratio_lines"):
        st["ratio"] = {"last_day": None, "last": None, "lines": {ln["key"]: {"armed": True, "fired_day": None} for ln in cfg["ratio_lines"]}}
    if cfg.get("notice_events") or cfg.get("notice_watch"):
        st["notice_done"], st["notice_seen"] = {}, []
    if cfg.get("time_checks"):
        st["time_done"] = {}
    return st


def load_state(cfg: dict) -> dict:
    st = default_state(cfg)
    p = state_path(cfg["name"])
    if p.exists():
        try:
            got = json.loads(p.read_text(encoding="utf-8"))
            for k, v in got.items():
                if k in ("lines", "extra") and isinstance(v, dict):
                    st.setdefault(k, {}).update(v)
                else:
                    st[k] = v
        except json.JSONDecodeError:
            pass
    return st


def save_state(cfg: dict, st: dict) -> None:
    p = state_path(cfg["name"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------- logic ----------
def hit(ln: dict, close: float) -> bool:
    return {"le": close <= ln["level"], "ge": close >= ln["level"], "lt": close < ln["level"]}[ln["op"]]


def rearm(ln: dict, close: float, pct: float) -> bool:
    if pct <= 0:
        return not hit(ln, close)
    f = pct / 100.0
    return close < ln["level"] * (1 - f) if ln["op"] == "ge" else close > ln["level"] * (1 + f)


def dist(close: float, level: float) -> float:
    return (close / level - 1.0) * 100.0


def eval_lines(cfg: dict, st: dict, rows: list[dict], today: str, now: datetime) -> list[dict]:
    events = []
    close_hm = tuple(map(int, cfg.get("market_close_kst", "15:30").split(":")))
    last = st.get("last_day") or ""
    for r in rows:
        if r["day"] <= last:
            continue
        if r["day"] > today or (r["day"] == today and (now.hour, now.minute) < close_hm):
            continue  # intraday, not a close yet
        for ln in cfg["lines"]:
            s = st["lines"].setdefault(ln["key"], {"armed": True, "fired_day": None})
            if s.get("armed", True) and hit(ln, r["close"]):
                s.update(armed=False, fired_day=r["day"])
                events.append({"kind": "line", "key": ln["key"], "line": ln, "day": r["day"], "close": r["close"]})
            elif not s.get("armed", True) and rearm(ln, r["close"], float(cfg.get("rearm_pct", 1.0))):
                s["armed"] = True
                print(f"{cfg['name']} {ln['key']} re-armed on {r['day']} close {r['close']:,.0f}")
        for ln in cfg.get("extra_lines") or []:
            if ln.get("until") and r["day"] > ln["until"]:
                continue
            s = st.setdefault("extra", {}).setdefault(ln["key"], {"armed": True, "fired_day": None})
            if s.get("armed", True) and hit(ln, r["close"]):
                s.update(armed=False, fired_day=r["day"])
                events.append({"kind": "extra", "key": ln["key"], "line": ln, "day": r["day"], "close": r["close"]})
            elif not s.get("armed", True) and rearm(ln, r["close"], float(ln.get("rearm_pct", cfg.get("rearm_pct", 1.0)))):
                s["armed"] = True
                print(f"{cfg['name']} extra {ln['key']} re-armed on {r['day']} close {r['close']:,.0f}")
                if ln.get("rearm_title"):
                    events.append({"kind": "extra_rearm", "key": ln["key"], "line": ln, "day": r["day"], "close": r["close"]})
        st["last_day"], st["last_close"] = r["day"], r["close"]
    return events


def discount(main: float, pair: float) -> float:
    return (pair - main) / pair * 100.0


def ratio_hit(ln: dict, v: float) -> bool:
    return {"le": v <= ln["level"], "ge": v >= ln["level"], "lt": v < ln["level"]}[ln["op"]]


def ratio_rearm(ln: dict, v: float, pp: float) -> bool:
    return v < ln["level"] - pp if ln["op"] == "ge" else v > ln["level"] + pp


def eval_ratio(cfg: dict, st: dict, rows: list[dict], pair_rows: list[dict], today: str, now: datetime) -> list[dict]:
    """configs ratio_lines on the same-session discount (pair - main) / pair. Hold (stop the walk) when either close is missing."""
    if not cfg.get("ratio_lines"):
        return []
    close_hm = tuple(map(int, cfg.get("market_close_kst", "15:30").split(":")))
    rs = st.setdefault("ratio", {"last_day": None, "last": None, "lines": {}})
    rs.setdefault("lines", {})
    pmap = {r["day"]: r["close"] for r in pair_rows or []}
    events = []
    for r in rows or []:
        if r["day"] <= (rs.get("last_day") or ""):
            continue
        if r["day"] > today or (r["day"] == today and (now.hour, now.minute) < close_hm):
            continue
        pc = pmap.get(r["day"])
        if not pc:
            break  # pair close missing for this session: hold, judged on a later run
        v = discount(r["close"], pc)
        for ln in cfg["ratio_lines"]:
            s = rs["lines"].setdefault(ln["key"], {"armed": True, "fired_day": None})
            if s.get("armed", True) and ratio_hit(ln, v):
                s.update(armed=False, fired_day=r["day"])
                events.append({"kind": "ratio", "key": ln["key"], "line": ln, "day": r["day"], "close": r["close"], "pair": pc, "v": v})
            elif not s.get("armed", True) and ratio_rearm(ln, v, float(ln.get("rearm_pp", cfg.get("ratio_rearm_pp", 1.0)))):
                s["armed"] = True
                print(f"{cfg['name']} ratio {ln['key']} re-armed on {r['day']} discount {v:.2f}%")
        rs.update(last_day=r["day"], last=round(v, 4))
    return events


def find_notice(cfg: dict, codes: list[str], keywords: list[str], since: str, exclude: list[str] | None = None, rows_fn=None) -> dict | None:
    for code in codes:
        for r in (rows_fn or notice_rows)(cfg, code):
            if r["datetime"][:10] >= since and any(k in r["title"] for k in keywords) and not any(x in r["title"] for x in exclude or []):
                return r
    return None


def notice_rows(cfg: dict, code: str) -> list[dict]:
    """Naver stock notice feed (KRX/KIND mirror, no key): [{'no','code','title','datetime'}], titles scrubbed."""
    raw = get_json(cfg["sources"]["naver_notice"].format(code=code))
    out = []
    for r in raw if isinstance(raw, list) else []:
        r = r or {}
        out.append({"no": str(r.get("no") or r.get("bizdateSeq") or ""), "code": code, "title": scrub(str(r.get("title") or "").strip()),
                    "datetime": str(r.get("datetime") or "")[:16].replace("T", " ")})
    return out


def eval_notices(cfg: dict, st: dict, today: str, fails: list[str], rows_fn=None) -> list[dict]:
    """notice_events (dated checks) and notice_watch (keyword filings, dedupe by filing no). Mutates st and fails."""
    out = []
    done = st.setdefault("notice_done", {})
    for ev in cfg.get("notice_events") or []:
        if ev["id"] in done or today < ev["start"]:
            continue
        if (date.fromisoformat(today) - date.fromisoformat(ev["end"])).days > int(ev.get("catch_up_days", 3)):
            continue
        since = (date.fromisoformat(ev["start"]) - timedelta(days=1)).isoformat()
        filing, ffail = None, None
        try:
            filing = find_notice(cfg, ev["codes"], ev["keywords"], since, ev.get("exclude"), rows_fn)
        except Exception as exc:
            ffail = str(exc)[:80]
        if filing or today >= ev["end"]:
            out.append({"kind": "notice_event", "ev": ev, "filing": filing, "ffail": ffail})
            done[ev["id"]] = today
    w = cfg.get("notice_watch")
    if w:
        seen = st.setdefault("notice_seen", [])
        for code in w["codes"]:
            try:
                rows = (rows_fn or notice_rows)(cfg, code)
            except Exception as exc:
                fails.append(f"{code} 공시 목록 (네이버 {str(exc)[:60]}; 다음 실행에서 다시 확인)")
                continue
            for r in sorted(rows, key=lambda x: x["datetime"]):
                if r["no"] and r["no"] not in seen and r["datetime"][:10] >= w["since"] and any(k in r["title"] for k in w["keywords"]):
                    seen.append(r["no"])
                    out.append({"kind": "notice_watch", "filing": r})
    return out


def eval_time_checks(cfg: dict, st: dict, today: str) -> list[dict]:
    """configs time_checks: dated reminders (no filing lookup). Once on/after the end day within catch_up_days."""
    out = []
    done = st.setdefault("time_done", {})
    for tc in cfg.get("time_checks") or []:
        if tc["id"] in done or today < tc["end"]:
            continue
        if (date.fromisoformat(today) - date.fromisoformat(tc["end"])).days > int(tc.get("catch_up_days", 3)):
            continue
        out.append({"kind": "time_check", "tc": tc})
        done[tc["id"]] = today
    return out


def eval_us10y(cfg: dict, st: dict, y: float | None, today: str) -> list[dict]:
    z = cfg.get("us10y")
    if not z or y is None:
        return []
    s = st.setdefault("us10y", {"armed": True, "fired_day": None, "last": None})
    s["last"] = y
    if s.get("armed", True) and y >= z["warn_min"] and s.get("fired_day") != today:
        s.update(armed=False, fired_day=today)
        return [{"kind": "us10y", "y": y, "day": today}]
    if not s.get("armed", True) and y < z["rearm_below"]:
        s["armed"] = True
        print(f"us10y re-armed at {y:.3f}")
    return []


def earnings_due(cfg: dict, st: dict, today: str) -> str | None:
    e = cfg.get("earnings") or {}
    d = e.get("date")
    if not d or d in (st.get("earn_done") or {}):
        return None
    late = (date.fromisoformat(today) - date.fromisoformat(d)).days
    return d if 0 <= late <= int(e.get("catch_up_days", 3)) else None


# ---------- messages ----------
def fmt(v: float) -> str:
    return f"{v:,.0f}원"


def ladder(cfg: dict, close: float | None) -> str:
    if close is None:
        return ""
    if cfg.get("ladder_all"):  # every plan line with its distance (no buy/trim/stop wording)
        return ", ".join(f"{ln.get('short', ln['title'])} {fmt(ln['level'])}(까지 {dist(ln['level'], close):+.1f}%)" for ln in cfg["lines"]) + "."
    buys = [ln for ln in cfg["lines"] if ln["op"] == "le" and ln["level"] < close]
    trims = [ln for ln in cfg["lines"] if ln["op"] == "ge" and ln["level"] > close]
    stop = next((ln for ln in cfg["lines"] if ln["key"] == "stop"), None)
    parts = []
    if buys:
        b = max(buys, key=lambda x: x["level"])
        parts.append(f"다음 매수선 {fmt(b['level'])}(까지 {dist(b['level'], close):+.1f}%)")
    if trims:
        t = min(trims, key=lambda x: x["level"])
        parts.append(f"다음 축소선 {fmt(t['level'])}(까지 {dist(t['level'], close):+.1f}%)")
    if stop:
        parts.append(f"손절선 {fmt(stop['level'])}(까지 {dist(stop['level'], close):+.1f}%)")
    return ", ".join(parts) + "."


def head(cfg: dict, stamp: str) -> str:
    return f"{stamp} | {cfg['code']} | {cfg['short']}"


def trim_text(cfg: dict) -> str:
    t = [ln for ln in cfg["lines"] if ln["op"] == "ge"]
    return ", ".join(f"{fmt(ln['level'])} 이상 {ln['title'].split()[-1]}" for ln in t)


def act_events(events: list[dict]) -> list[dict]:
    """Stop in the same message: drop buy-line actions so the 할 일 line has no buy advice next to the stop."""
    sup = {k for e in events for k in e["line"].get("suppress", [])}  # optional per-line (0220WL: review drops yield5)
    if sup:
        events = [e for e in events if e["key"] not in sup]
    if any(e["key"] == "stop" for e in events):
        return [e for e in events if not e["key"].startswith("buy")]
    return events


def build_lines(cfg: dict, events: list[dict], data: dict, stamp: str, ref: str | None = None) -> tuple[str, str, int]:
    events = sorted(events, key=lambda e: SEVERITY.index(e["key"]) if e["key"] in SEVERITY else 99)
    top = events[0]
    ln = top["line"]
    sign = {"le": "이하", "ge": "이상", "lt": "미만"}[ln["op"]]
    title = f"{cfg['title_prefix']} 종가 {fmt(ln['level'])} {sign} | {ln['title']}"
    pri = 5 if top["key"] == "stop" else 4
    hits = [f"{e['line']['label']}: 종가 {fmt(e['close'])}({e['day']}), 기준 {fmt(e['line']['level'])}, 기준 대비 {dist(e['close'], e['line']['level']):+.2f}%."
            for e in events]
    last_close = (data.get("rows") or [{}])[-1].get("close") if data.get("rows") else events[-1]["close"]
    if top["line"].get("later"):
        later = top["line"]["later"]
    elif top["key"] == "stop":
        later = "손절 뒤에는 이 계획의 매수선을 다시 쓰지 말고 계획을 새로 세우세요."
    elif top["key"].startswith("trim"):
        later = "남은 수량은 다음 축소선까지 보유. 축소선은 종가가 1% 넘게 아래로 내려왔다 와야 다시 울립니다."
    else:
        later = "다음 매수선까지 기다리세요. 같은 선은 종가가 1% 넘게 위로 올라갔다 와야 다시 울립니다."
    msg = "\n".join([
        head(cfg, stamp),
        "이게 뭐 알림: " + cfg.get("lines_what", f"{cfg['short']} 종가가 계획한 매수, 축소, 손절 선을 새로 넘을 때만 오는 알림입니다. 매수 지시가 아닙니다."),
        "지금 결과/상태: " + " ".join(hits) + yield_text(cfg, last_close) + f" {ladder(cfg, last_close)} (정규장 종가, {data.get('src') or '-'})" + pair_text(cfg, data),
        "지금 할 일: " + " ".join(e["line"]["action"] for e in act_events(events)),
        f"나중에: {later}",
        "익절/줄일 때: " + cfg.get("lines_trim", f"{trim_text(cfg)} 비중 축소.{cfg.get('trim_extra', ' 미국 10년물 5.30% 이상이면 분할 매수 중단.')}"),
        "하지 말 것: " + cfg.get("lines_dont", "한 번에 전액 매수, 신용 및 미수, 손절선 이탈 뒤 물타기, 조회 실패를 신호로 읽기."),
    ] + ([ref] if ref else []))
    return title, msg, pri


def build_extra(cfg: dict, events: list[dict], data: dict, stamp: str, ref: str | None = None) -> tuple[str, str, int]:
    """Notify-only levels (configs extra_lines): same 7-line wording as build_lines, no plan-line action."""
    sign = {"le": "이하", "ge": "이상", "lt": "미만"}

    def pri(e):
        return int(e["line"].get("rearm_priority", 3) if e["kind"] == "extra_rearm" else e["line"].get("priority", 4))
    events = sorted(events, key=lambda e: (-pri(e), e["line"]["level"]))
    top = events[0]
    ln = top["line"]
    if top["kind"] == "extra_rearm":
        title = f"{cfg['title_prefix']} 종가 {fmt(ln['level'])} 위 회복 | {ln['rearm_title']}"
    else:
        title = f"{cfg['title_prefix']} 종가 {fmt(ln['level'])} {sign[ln['op']]} | {ln['title']}"
    hits = [(f"{e['line']['label']} 회복: " if e["kind"] == "extra_rearm" else f"{e['line']['label']}: ")
            + f"종가 {fmt(e['close'])}({e['day']}), 기준 {fmt(e['line']['level'])}, 기준 대비 {dist(e['close'], e['line']['level']):+.2f}%."
            for e in events]
    last_close = (data.get("rows") or [{}])[-1].get("close") if data.get("rows") else events[-1]["close"]
    stop = next((x for x in cfg["lines"] if x["key"] == "stop"), None)
    under_stop = stop is not None and last_close is not None and last_close < stop["level"]
    acts = []
    for e in events:
        if under_stop and e["line"].get("buy_advice"):
            continue  # no buy advice under the stop line (same rule as act_events, eebf60d)
        acts.append(e["line"]["rearm_action"] if e["kind"] == "extra_rearm" else e["line"]["action"])
    if under_stop:
        acts.append(f"종가가 손절선 {fmt(stop['level'])} 아래입니다. 매수 조언 없음, 계획대로 손절 기준.")
    later = ln.get("later") or "같은 레벨은 종가가 반대로 돌아갔다 와야 다시 울립니다."
    msg = "\n".join([
        head(cfg, stamp),
        f"이게 뭐 알림: {ln.get('what') or cfg['short'] + ' 참고 레벨 알림'}. 계획 선(매수, 축소, 손절)과 별개인 알림 전용 레벨입니다. 매수 지시가 아닙니다.",
        "지금 결과/상태: " + " ".join(hits) + f" {ladder(cfg, last_close)} (정규장 종가, {data.get('src') or '-'})",
        "지금 할 일: " + " ".join(dict.fromkeys(acts)),
        f"나중에: {later}",
        f"익절/줄일 때: {trim_text(cfg)} 비중 축소.",
        "하지 말 것: " + (ln.get("dont") or "레벨 돌파만 보고 추격 매수, 신용 및 미수, 조회 실패를 신호로 읽기."),
    ] + ([ref] if ref else []))
    return title, msg, pri(top)


def yield_text(cfg: dict, close: float | None) -> str:
    """configs "yield" {dps, label, note}: dividend yield on the close, e.g. 0220WL minimum dividend 102원/yr."""
    y = cfg.get("yield")
    if not y or not close:
        return ""
    return f" {y['label']} {y['dps'] / close * 100:.2f}%(연 {fmt(y['dps'])} / 종가 {fmt(close)}{y.get('note', '')})."


def pair_last(data: dict) -> tuple | None:
    """Latest session with both regular closes: (day, main, pair, discount %)."""
    pmap = {r["day"]: r["close"] for r in data.get("pair_rows") or []}
    both = [(r["day"], r["close"], pmap[r["day"]]) for r in data.get("rows") or [] if pmap.get(r["day"])]
    if not both:
        return None
    d, m, p = both[-1]
    return d, m, p, discount(m, p)


def pair_text(cfg: dict, data: dict) -> str:
    if not cfg.get("pair"):
        return ""
    pl = pair_last(data)
    if not pl:
        return f" {cfg['pair']['short']} 종가 없음, 괴리율 판단 보류."
    d, m, p, v = pl
    return f" {cfg['pair']['short']} 종가 {fmt(p)}({d}), 괴리율 {v:.2f}%."


def build_ratio(cfg: dict, events: list[dict], data: dict, stamp: str, ref: str | None = None) -> tuple[str, str, int]:
    sign = {"le": "이하", "ge": "이상", "lt": "미만"}
    events = sorted(events, key=lambda e: (-int(e["line"].get("priority", 4)), e["day"]))
    top = events[0]
    ln = top["line"]
    title = f"{cfg['title_prefix']} 괴리율 {ln['level']:.1f}% {sign[ln['op']]} | {ln['title']}"
    hits = [f"{e['line']['label']}: {e['day']} 우선주 종가 {fmt(e['close'])}, {cfg['pair']['short']} 종가 {fmt(e['pair'])}, "
            f"괴리율 {e['v']:.2f}%(기준 {e['line']['level']:.1f}%, 차이 {e['v'] - e['line']['level']:+.2f}%p)." for e in events]
    last_close = (data.get("rows") or [{}])[-1].get("close") if data.get("rows") else top["close"]
    rt = cfg.get("ratio_text", {})
    msg = "\n".join([
        head(cfg, stamp),
        "이게 뭐 알림: " + rt.get("what", f"{cfg['short']}와 {cfg['pair']['short']}의 같은 날 정규장 종가 괴리율 알림입니다. 매수 지시가 아닙니다."),
        "지금 결과/상태: " + " ".join(hits) + yield_text(cfg, last_close) + f" {ladder(cfg, last_close)}",
        "지금 할 일: " + " ".join(dict.fromkeys(e["line"]["action"] for e in events)),
        "나중에: " + (ln.get("later") or rt.get("later", "같은 선은 괴리율이 1%p 넘게 반대로 돌아갔다 와야 다시 울립니다.")),
        "익절/줄일 때: " + rt.get("trim", "괴리율 알림만으로 줄이지 마세요."),
        "하지 말 것: " + rt.get("dont", "괴리율만 보고 추격 매매, 신용 및 미수, 조회 실패를 신호로 읽기."),
    ] + ([ref] if ref else []))
    return title, msg, int(ln.get("priority", 4))


def build_notice_event(cfg: dict, e: dict, data: dict, stamp: str, ref: str | None = None) -> tuple[str, str, int]:
    ev, filing, ffail = e["ev"], e["filing"], e["ffail"]
    page = cfg["sources"]["notice_page"].format(code=ev.get("page_code", ev["codes"][0]))
    if filing:
        fl, tail = f"공시 있음: {scrub(filing['title'])} ({filing['datetime']}).", "공시 확인됨"
    else:
        fl = "공시 자동 확인 안 됨, 공시 확인 필요" + (f" (공시 피드 조회 실패: {ffail})" if ffail else "") + "." + (f" {ev['missing_note']}" if ev.get("missing_note") else "")
        tail = "공시 확인 필요"
    checks = " ".join(f"{i}) {c}." for i, c in enumerate(ev["checklist"], 1))
    pl = pair_last(data)
    px = f" 최근 종가({pl[0]}): 우선주 {fmt(pl[1])}, {cfg['pair']['short']} {fmt(pl[2])}, 괴리율 {pl[3]:.2f}%." if cfg.get("pair") and pl else ""
    title = f"{cfg['title_prefix']} {ev['title']} | {tail}"
    msg = "\n".join([
        head(cfg, stamp),
        f"이게 뭐 알림: {ev['what']} 매수 지시가 아닙니다.",
        f"지금 결과/상태: {fl}{px} 공시 목록: {page}",
        f"지금 할 일: 다음을 확인하세요. {checks}",
        f"나중에: {ev['later']}",
        "익절/줄일 때: " + ev.get("trim", "이 체크 알림만으로 줄이지 마세요. 가격 선과 괴리율 알림 기준을 따르세요."),
        "하지 말 것: " + ev.get("dont", "발표 당일 급등락에 추격 매매, 체크 없이 따라가기, 신용 및 미수."),
    ] + ([ref] if ref else []))
    return title, msg, int(ev.get("priority", 4))


def build_time_check(cfg: dict, e: dict, data: dict, stamp: str, ref: str | None = None) -> tuple[str, str, int]:
    tc = e["tc"]
    checks = " ".join(f"{i}) {c}." for i, c in enumerate(tc["checklist"], 1))
    pl = pair_last(data)
    px = f" 최근 종가({pl[0]}): 우선주 {fmt(pl[1])}, {cfg['pair']['short']} {fmt(pl[2])}, 괴리율 {pl[3]:.2f}%." if cfg.get("pair") and pl else ""
    title = f"{cfg['title_prefix']} {tc['title']} | 날짜 점검"
    msg = "\n".join([
        head(cfg, stamp),
        "이게 뭐 알림: " + tc.get("what", f"{cfg['short']} 보유 계획에서 정해 둔 날짜 점검입니다. 공시나 가격 조건이 아니라 날짜로 오는 알림입니다.") + " 매수 지시가 아닙니다.",
        f"지금 결과/상태: 점검 기간 {tc['start']}~{tc['end']}가 끝났습니다.{px}",
        f"지금 할 일: 다음을 점검하세요. {checks}",
        "나중에: " + tc.get("later", "이 점검 알림은 한 번만 옵니다. 가격 선과 괴리율 알림은 계속 돕니다."),
        "익절/줄일 때: " + tc.get("trim", "체크리스트의 매도 검토 조건에 맞을 때만 정해 둔 수량을 검토하세요."),
        "하지 말 것: " + tc.get("dont", "점검 없이 날짜만 보고 매매, 신용 및 미수."),
    ] + ([ref] if ref else []))
    return title, msg, int(tc.get("priority", 4))


def build_notice_watch(cfg: dict, e: dict, data: dict, stamp: str, ref: str | None = None) -> tuple[str, str, int]:
    w, f = cfg["notice_watch"], e["filing"]
    pl = pair_last(data)
    px = f" 최근 종가({pl[0]}): 우선주 {fmt(pl[1])}, {cfg['pair']['short']} {fmt(pl[2])}, 괴리율 {pl[3]:.2f}%." if cfg.get("pair") and pl else ""
    f = dict(f, title=scrub(f["title"]))
    short_title = f["title"]
    for x in w.get("strip", []):
        short_title = short_title.replace(x, "")
    short_title = re.sub(r"\s+", " ", short_title).strip()
    title = f"{cfg['title_prefix']} {w['title']} | {short_title[:40]}"
    msg = "\n".join([
        head(cfg, stamp),
        f"이게 뭐 알림: {w['what']} 매수 지시가 아닙니다.",
        f"지금 결과/상태: 새 공시({f['code']}): {f['title']} ({f['datetime']}).{px} 공시 목록: {cfg['sources']['notice_page'].format(code=f['code'])}",
        f"지금 할 일: {w['todo']}",
        f"나중에: {w['later']}",
        "익절/줄일 때: " + w.get("trim", "공시 제목만으로 줄이지 마세요."),
        "하지 말 것: " + w.get("dont", "제목만 보고 추격 매매, 신용 및 미수."),
    ] + ([ref] if ref else []))
    return title, msg, int(w.get("priority", 4))


def build_us10y(cfg: dict, ev: dict, stamp: str, ref: str | None = None) -> tuple[str, str, int]:
    z = cfg["us10y"]
    title = f"{cfg['title_prefix']} 미국 10년물 {z['warn_min']:.2f}% 이상 | 분할 매수 중단"
    msg = "\n".join([
        f"{stamp} | 005930, 000660 공통 | 미국 10년물 금리",
        f"이게 뭐 알림: 삼성전자(005930)와 SK하이닉스(000660) 계획의 공통 금리 경고입니다. 한 번만 보냅니다.",
        f"지금 결과/상태: 미국 10년물(^TNX) {ev['y']:.3f}%, 기준 {z['warn_min']:.2f}% 이상, 거리 {ev['y'] - z['warn_min']:+.3f}%p.",
        f"지금 할 일: {z['action']} 두 종목 모두 해당.",
        f"나중에: {z['rearm_below']:.2f}% 아래로 내려와야 이 경고가 다시 무장됩니다.",
        "익절/줄일 때: 축소선에 아직 못 미쳐도 현금 비중을 먼저 늘리는 쪽을 검토하세요.",
        "하지 말 것: 금리 경고 중 매수선 알림만 보고 기계적으로 추가 매수하기, 신용 및 미수.",
    ] + ([ref] if ref else []))
    return title, msg, 4


def build_earnings(cfg: dict, d: str, filing: dict | None, ffail: str | None, stamp: str, ref: str | None = None) -> tuple[str, str, int]:
    e = cfg["earnings"]
    page = cfg["sources"]["notice_page"].format(code=cfg["code"])
    if filing:
        fl = f"실적 공시 있음: {filing['title']} ({filing['datetime']})."
        tail = "공시 확인됨"
    else:
        fl = "실적 공시 자동 확인 안 됨, 공시 확인 필요" + (f" (공시 피드 조회 실패: {ffail})" if ffail else "") + "."
        tail = "공시 확인 필요"
    checks = " ".join(f"{i}) {c}." for i, c in enumerate(cfg["checklist"], 1))
    title = f"{cfg['title_prefix']} 실적 체크 | {tail}"
    msg = "\n".join([
        head(cfg, stamp),
        f"이게 뭐 알림: {cfg['short']} {e['period']} 실적 발표일({d}) 체크리스트입니다. 매수 지시가 아닙니다.",
        f"지금 결과/상태: {fl} 공시 목록: {page}",
        f"지금 할 일: 실적과 컨퍼런스콜에서 다음 3가지를 확인하세요. {checks}",
        f"나중에: {cfg['plan_rule']} 모두 문제없으면 기존 매수선과 축소선 계획 유지.",
        f"익절/줄일 때: 하나라도 부정적이면 축소선({trim_text(cfg)})을 기다리지 말고 비중 축소 전환을 검토.",
        "하지 말 것: 발표 당일 급등락에 추격 매매, 체크 없이 매수선 알림만 따라가기, 신용 및 미수.",
    ] + ([ref] if ref else []))
    return title, msg, 4


def build_fail(cfg: dict, fails: list[str], stamp: str) -> tuple[str, str, int]:
    title = f"{cfg['title_prefix']} 조회 실패 | 조건 판단 보류"
    msg = "\n".join([
        head(cfg, stamp),
        f"이게 뭐 알림: {cfg['short']} 계획 알림이 일부 데이터를 못 읽었습니다. 매매 신호가 아닙니다.",
        "지금 결과/상태: 조회 실패: " + ", ".join(fails) + ".",
        "지금 할 일: 다음 거래일 19:00 재시도를 기다리세요. 밀린 거래일 종가는 다음 실행에서 순서대로 판단합니다.",
        "나중에: 읽힌 항목은 정상 판단했습니다.",
        "익절/줄일 때: 이 실패 알림만으로 줄이지 마세요.",
        "하지 말 것: 조회 실패를 조건 충족으로 읽기.",
    ])
    return title, msg, 3


def with_footer(cfg: dict, msg: str) -> str:
    """configs footer_note: one line at the very end of every message (absent: message unchanged)."""
    foot = cfg.get("footer_note")
    return f"{msg}\n{foot}" if foot else msg


def clean(title: str, msg: str) -> None:
    for ch in FORBIDDEN:
        if ch in title or ch in msg:
            raise ValueError(f"forbidden char U+{ord(ch):04X} in message: {title}")


# ---------- earn_auto: quarterly earnings call auto-check (2026-10-30, configs/005930.json) ----------
def earn_notice_rows(cfg: dict) -> list[dict]:
    """Naver notice feed with contents (IR notice carries the call date). Titles scrubbed."""
    raw = get_json(cfg["sources"]["naver_notice"].format(code=cfg["code"]))
    out = []
    for r in raw if isinstance(raw, list) else []:
        r = r or {}
        out.append({"no": str(r.get("no") or ""), "title": scrub(str(r.get("title") or "").strip()),
                    "datetime": str(r.get("datetime") or "")[:16].replace("T", " "), "contents": str(r.get("contents") or "")})
    return out


def earn_transcript(ea: dict, y: int, q: int, url: str | None = None, reader=None) -> tuple[str | None, str | None]:
    """(text, url) for that quarter: the fixed url first (only if its page title matches), then RSS + sitemap."""
    reader = reader or ec.read_src
    tconf = dict(ea["transcript"], title_re=ec.title_re_for(ea["transcript"]["title_re"], y, q))
    if url:
        try:
            raw = reader(url)
            head = raw.decode("utf-8", errors="ignore")[:200000] if raw else ""
            m = re.search(r'og:title"\s+content="([^"]+)"', head) or re.search(r"<title>(.*?)</title>", head, re.S)
            text = ec.transcript_body(raw) if raw else ""
            if m and re.search(tconf["title_re"], m.group(1)) and len(text) > 2000:
                return text, url
            print(f"earn transcript url {url}: title mismatch or short body; searching feeds")
        except Exception as exc:
            print(f"earn transcript url {url} failed: {exc}")
    return ec.find_transcript(tconf, reader=reader)


EARN_IO = {"notices": earn_notice_rows, "transcript": earn_transcript, "china": ec.china_news}


def trading_days_after(start: str, end: str) -> int:
    d, n = date.fromisoformat(start), 0
    stop = date.fromisoformat(end)
    while d < stop:
        d += timedelta(days=1)
        n += is_trading_day(d)
    return n


def earn_quarter(cfg: dict, rows: list[dict]) -> dict | None:
    """Quarter + results day from the notice feed (or configs earn_auto.override)."""
    ea = cfg["earn_auto"]
    ov = ea.get("override") or {}
    fil = [r for r in rows if len(r["datetime"]) >= 10 and any(k in r["title"] for k in ea["filing_keywords"]) and "예고" not in r["title"]]
    fil.sort(key=lambda r: r["datetime"])
    if ov.get("quarter"):
        y, q = ec.parse_qkey(ov["quarter"])
    elif fil:
        y, q = ec.quarter_of_filing(date.fromisoformat(fil[-1]["datetime"][:10]))
    else:
        return None
    qf = [r for r in fil if ec.quarter_of_filing(date.fromisoformat(r["datetime"][:10])) == (y, q)]
    first = qf[0]["datetime"][:10] if qf else None
    rd, src = (ov.get("results_day"), "설정값") if ov.get("quarter") and ov.get("results_day") else (None, None)
    if not rd and first:
        lo = (date.fromisoformat(first) - timedelta(days=1)).isoformat()
        hi = (date.fromisoformat(first) + timedelta(days=int(ea.get("ir_max_days", 45)))).isoformat()
        for r in rows:
            if ea["ir_keyword"] in r["title"] and r["datetime"][:10] >= lo:
                m = re.search(r"일시\s*(\d{4}-\d{2}-\d{2})", html_text(r.get("contents") or ""))
                if m and first <= m.group(1) <= hi:
                    rd, src = m.group(1), "IR 개최 안내공시"
                    break
    days = sorted({r["datetime"][:10] for r in qf})
    if not rd and len(days) >= 2:
        rd, src = days[-1], "두 번째 실적 공시"
    return {"key": ec.qkey(y, q), "y": y, "q": q, "label": ec.qlabel(y, q), "first_day": first, "results_day": rd,
            "results_src": src, "filings": qf, "url": ov.get("transcript_url") if ov.get("quarter") else None}


def earn_judge(ea: dict, text: str | None, now: datetime, china_fn=None) -> dict:
    i1 = ec.judge_item1(text, ea["item1"])
    i2 = ec.judge_item2(text, ea["item2"])
    asp = ec.judge_asp(text, float(ea["item3"]["asp_drop_pct"]))
    china = (china_fn or EARN_IO["china"])(ea["item3"]["china"], now)
    i3 = ec.judge_item3(asp, china)
    if not text:  # no transcript: every item is 확인 불가; the China keyword count is shown only as 참고
        i3 = dict(i3, status=ec.UNK, china_only=False)
    return {"items": [dict(i1, no=1), dict(i2, no=2), dict(i3, no=3)], "text": bool(text)}


def q_text(s: str | None, n: int = 80) -> str:
    return f'"{ec.cut(scrub(s or "").rstrip(". "), n)}"' if s else "없음"


def trims_short(cfg: dict) -> str:
    return ", ".join(f"{fmt(ln['level'])} {ln['title'].split()[-1]}" for ln in cfg["lines"] if ln["op"] == "ge")


def build_earn_result(cfg: dict, qi: dict, j: dict, url: str | None, stamp: str, ref: str | None = None) -> tuple[str, str, int]:
    ea = cfg["earn_auto"]
    i1, i2, i3 = j["items"]
    asp, china = i3["asp"], i3["china"]
    tail, todo = ec.conclusion(j["items"], trims_short(cfg), bool(ea["item3"]["china"].get("counts_as_negative")))
    ny, nq = ec.next_quarter(qi["y"], qi["q"])
    asp_vals = f"전분기 대비 D램 {asp['dram']['raw'] if asp.get('dram') else '못 읽음'}, 낸드 {asp['nand']['raw'] if asp.get('nand') else '못 읽음'}"
    if china.get("fail"):
        ch = f"중국 확인 불가(뉴스 조회 실패: {china['fail']})"
    elif china["n"]:
        h = china["hits"][0]
        ch = f"중국 {china['status']}(키워드): 최근 {ea['item3']['china'].get('window_days', 30)}일 뉴스 제목 키워드 {china['n']}건, 예 {q_text(h['title'], 60)}({h['day']}, 키워드 {h['kw']})"
    else:
        ch = f"중국 {china['status']}: 최근 {ea['item3']['china'].get('window_days', 30)}일 뉴스 제목 키워드 0건"
    if j["text"]:
        what = (f"이게 뭐 알림: {cfg['short']} {qi['label']} 실적 컨퍼런스콜 전문({ea['transcript']['source_label']}, 언론 받아쓰기라 공식 자료 아님)으로 "
                "실적 체크 3가지를 자동 판정했습니다. 분기마다 1번만 옵니다. 매수 지시가 아닙니다.")
        title = f"{cfg['title_prefix']} {qi['label']} 실적 자동 점검 | {tail}"
        i3_line = (f"- 3) 중국 YMTC/CXMT 출하와 분기 ASP 10% 이상 하락: {i3['status']}. ASP {asp['status']}({asp_vals}), 근거 {q_text(asp.get('evidence'), 70)}. "
                   f"{ch}. 중국 부분은 뉴스 제목 키워드 기준이라 최종 판단은 기사를 보고 직접 하세요.")
    else:
        what = (f"이게 뭐 알림: {cfg['short']} {qi['label']} 실적 발표({qi['results_day'] or qi['first_day']}) 뒤 {ea['wait_trading_days']}거래일 안에 "
                f"컨퍼런스콜 전문({ea['transcript']['source_label']})이 안 올라와 실적 체크 3가지를 자동 판정하지 못했습니다. 분기마다 1번만 옵니다. 매수 지시가 아닙니다.")
        title = f"{cfg['title_prefix']} {qi['label']} 실적 자동 점검 | 컨콜 전문 없음, 추가 매수 보류"
        i3_line = f"- 3) 중국 YMTC/CXMT 출하와 분기 ASP 10% 이상 하락: {i3['status']} (ASP 못 읽음). 참고로 {ch}. 중국 부분은 키워드 기준이라 직접 판단하세요."
    lines = [
        head(cfg, stamp),
        what,
        f"지금 결과/상태: 1번 {i1['status']}, 2번 {i2['status']}, 3번 {i3['status']}{'(중국 키워드)' if i3.get('china_only') else ''}. 확인 불가는 문제없음으로 세지 않습니다.",
        f"- 1) 2027년 HBM 장기공급계약(LTA) 물량 축소: {i1['status']}({i1['why']}). 근거 {q_text(i1.get('evidence'))}",
        f"- 2) CAPEX 강행과 판가 하락 암시: {i2['status']}({i2['why']}). 근거 {q_text(i2.get('evidence'))}"
        + (f", 투자 {q_text(i2['capex'], 60)}" if i2.get("capex") else ""),
        i3_line,
        f"지금 할 일: {todo}",
        f"나중에: 다음 자동 판정은 {ec.qlabel(ny, nq)} 실적 발표 뒤입니다(영업(잠정)실적 공시와 IR 안내공시로 발표일을 자동으로 찾음)."
        + (f" 전문: {url}" if url else ""),
        f"익절/줄일 때: 경계가 있으면 축소선({trims_short(cfg)})을 기다리지 말고 비중 축소 전환 검토. 모두 문제없으면 축소선 그대로.",
        "하지 말 것: 자동 판정만 믿고 한 번에 매매, 뉴스 제목만으로 결론, 확인 불가를 문제없음으로 읽기, 신용 및 미수.",
    ] + ([ref] if ref else [])
    body = "\n".join(lines)
    if len(body.encode("utf-8")) > 3600:  # ntfy 4096-byte limit incl. the end line: shorten quotes, never drop a section
        body = body.replace(q_text(i1.get("evidence")), q_text(i1.get("evidence"), 45)).replace(q_text(i2.get("evidence")), q_text(i2.get("evidence"), 45))
    pri = 4 if any(i["status"] != ec.OK for i in j["items"]) else 3
    return title, body, pri


def build_earn_notice(cfg: dict, qi: dict, filing: dict | None, stamp: str, ref: str | None = None) -> tuple[str, str, int]:
    ea = cfg["earn_auto"]
    page = cfg["sources"]["notice_page"].format(code=cfg["code"])
    if filing:
        fl, tail = f"실적 공시 있음: {filing['title']} ({filing['datetime']}).", "공시 확인됨"
    else:
        fl, tail = "실적 공시 자동 확인 안 됨, 공시 확인 필요.", "공시 확인 필요"
    checks = " ".join(f"{i}) {c}." for i, c in enumerate(cfg["checklist"], 1))
    msg = "\n".join([
        head(cfg, stamp),
        f"이게 뭐 알림: {cfg['short']} {qi['label']} 실적 발표일({qi['results_day']}, {qi['results_src']}) 체크리스트입니다. 매수 지시가 아닙니다.",
        f"지금 결과/상태: {fl} 공시 목록: {page}",
        f"지금 할 일: 실적과 컨퍼런스콜에서 다음 3가지를 보세요. {checks} 컨콜 전문이 올라오면 3가지를 자동 판정해서 1번 더 보냅니다"
        f"(발표일 뒤 {ea['wait_trading_days']}거래일까지, 없으면 확인 불가 1번).",
        f"나중에: {cfg['plan_rule']} 모두 문제없으면 기존 매수선과 축소선 계획 유지.",
        f"익절/줄일 때: 하나라도 부정적이면 축소선({trims_short(cfg)})을 기다리지 말고 비중 축소 전환을 검토.",
        "하지 말 것: 발표 당일 급등락에 추격 매매, 체크 없이 매수선 알림만 따라가기, 신용 및 미수.",
    ] + ([ref] if ref else []))
    return f"{cfg['title_prefix']} {qi['label']} 실적 체크 | {tail}", msg, 4


def earn_auto_step(cfg: dict, st: dict, now: datetime, stamp: str, ref_fn=None, io: dict | None = None) -> list[tuple]:
    """Mutates st["earn_auto"]; returns [(title, msg, pri, tags)]. Any lookup failure is silent (next 19:00 run retries)."""
    ea = cfg.get("earn_auto")
    if not ea:
        return []
    io = {**EARN_IO, **(io or {})}
    today = session_day(now).isoformat()
    try:
        rows = io["notices"](cfg)
    except Exception as exc:
        print(f"earn_auto: notice feed failed ({str(exc)[:80]}); retry next run")
        return []
    qi = earn_quarter(cfg, rows)
    if not qi:
        print("earn_auto: no results filing in the feed; silent")
        return []
    print(f"earn_auto: quarter {qi['key']} first filing {qi['first_day']} results day {qi['results_day']} ({qi['results_src']})")
    if ec.parse_qkey(qi["key"]) < ec.parse_qkey(ea["start_quarter"]):
        print(f"earn_auto: {qi['key']} is before start_quarter {ea['start_quarter']} (backfill only); silent")
        return []
    s = st.setdefault("earn_auto", {}).setdefault(qi["key"], {})
    if s.get("sent"):
        print(f"earn_auto: {qi['key']} already sent {s['sent']}; silent")
        return []
    sends = []
    rd = qi["results_day"]
    ref = ref_fn() if ref_fn else None
    if rd and today >= rd and not s.get("notice_sent"):
        lo = (date.fromisoformat(rd) - timedelta(days=1)).isoformat()
        filing = next((r for r in reversed(qi["filings"]) if r["datetime"][:10] >= lo), None)
        sends.append(build_earn_notice(cfg, qi, filing, stamp, ref) + (["clipboard"],))
        s.update(notice_sent=stamp, results_day=rd)
    start = rd or qi["first_day"]
    if not start or today < start:
        return sends
    if rd:
        late = trading_days_after(rd, today) >= int(ea["wait_trading_days"])
    else:
        late = (date.fromisoformat(today) - date.fromisoformat(start)).days >= int(ea["prelim_wait_days"])
    text, url = None, None
    try:
        text, url = io["transcript"](ea, qi["y"], qi["q"], qi.get("url"))
    except Exception as exc:
        print(f"earn_auto: transcript lookup failed ({str(exc)[:80]})")
    if not text and not late:
        print(f"earn_auto: {qi['key']} transcript not found yet; retry next trading day")
        return sends
    j = earn_judge(ea, text, now, io["china"])
    sends.append(build_earn_result(cfg, qi, j, url, stamp, ref) + (["bar_chart"],))
    s.update(sent=stamp, kind="judged" if text else "no_transcript", via="auto", url=url,
             verdict={str(i["no"]): i["status"] for i in j["items"]})
    return sends


def earn_backfill(cfg: dict, dry: bool, now: datetime | None = None, io: dict | None = None, ref_fn=None) -> int:
    """Send configs earn_auto.backfill (2026Q3) once. dry: print only (no ntfy, no state)."""
    io = {**EARN_IO, **(io or {})}
    ea = cfg["earn_auto"]
    bf = ea["backfill"]
    now = now or datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    st = load_state(cfg)
    y, q = ec.parse_qkey(bf["quarter"])
    prev = (st.get("earn_auto") or {}).get(bf["quarter"]) or {}
    if prev.get("sent"):
        print(f"backfill {bf['quarter']} already sent {prev['sent']}" + ("; dry run still prints the message" if dry else "; silent"))
        if not dry:
            return 0
    text, url = io["transcript"](ea, y, q, bf.get("transcript_url"))
    if not text:
        print(f"backfill {bf['quarter']}: transcript not found; nothing sent")
        return 1
    qi = {"key": bf["quarter"], "y": y, "q": q, "label": ec.qlabel(y, q), "results_day": bf["results_day"], "first_day": bf["results_day"]}
    j = earn_judge(ea, text, now, io["china"])
    ref = (ref_fn or (lambda: ref_text([nxt_ref(cfg, None, now)])))()
    title, msg, pri = build_earn_result(cfg, qi, j, url, stamp, ref)
    clean(title, msg)
    for i in j["items"]:
        print(f"item {i['no']}: {i['status']} | {i.get('why')} | evidence: {scrub(i.get('evidence') or '-')}")
        if i["no"] == 3:
            print(f"  ASP: {i['asp']['status']} | {i['asp']['why']} | dram={(i['asp'].get('dram') or {}).get('raw')} nand={(i['asp'].get('nand') or {}).get('raw')}")
            print(f"  China: {i['china']['status']} | n={i['china']['n']} fail={i['china'].get('fail')}")
            for h in i["china"]["hits"][:10]:
                print(f"    {h['day']} [{h['kw']}] {scrub(h['title'])}")
    full = append_ends(msg, cfg["name"])
    print(title)
    print(full)
    print(f"[{len(full.encode('utf-8'))} bytes] topic=cwally-port send={not dry}")
    if dry:
        print("DRY RUN: no ntfy, no state")
        return 0
    publish(resolve_topic("port", cfg.get("ntfy_topic")), title, msg, pri, ["bar_chart"], resolve_server(cfg.get("ntfy_server")))
    st.setdefault("earn_auto", {})[bf["quarter"]] = {"sent": stamp, "kind": "judged", "via": "backfill", "url": url,
                                                     "verdict": {str(i["no"]): i["status"] for i in j["items"]}}
    save_state(cfg, st)
    return 0


# ---------- run ----------
def load_cfg(name: str) -> dict:
    return json.loads((ROOT / "configs" / f"{name}.json").read_text(encoding="utf-8"))


def plan_sends(cfg: dict, st: dict, data: dict, fails: list[str], now: datetime, filing_fn=None, ref_fn=None, notice_fn=None,
               earn_io: dict | None = None) -> list[tuple]:
    """Mutates st; returns [(title, msg, pri, tags)]. Only data['rows'] (Yahoo regular closes) can fire a line;
    ref_fn(cfg, rows, now) only formats the NXT reference line of a message that is already being sent."""
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    today = session_day(now).isoformat()
    ref_fn = ref_fn or nxt_ref
    cache: dict = {}

    def ref(c: dict, rows) -> str:
        if c["code"] not in cache:
            cache[c["code"]] = ref_fn(c, rows, now)
        return cache[c["code"]]

    sends = []
    if cfg.get("earn_auto"):
        sends += earn_auto_step(cfg, st, now, stamp, lambda: ref_text([ref(cfg, data.get("rows"))]), earn_io)
    d = earnings_due(cfg, st, today)
    if d:
        filing, ffail = None, None
        try:
            filing = (filing_fn or find_filing)(cfg, d)
        except Exception as exc:
            ffail = str(exc)[:80]
        sends.append(build_earnings(cfg, d, filing, ffail, stamp, ref_text([ref(cfg, data.get("rows"))])) + (["clipboard"],))
        st.setdefault("earn_done", {})[d] = stamp
    ev_all = eval_lines(cfg, st, data.get("rows") or [], today, now)
    ev = [e for e in ev_all if e["kind"] == "line"]
    extra = [e for e in ev_all if e["kind"] != "line"]
    if ev:
        sends.append(build_lines(cfg, ev, data, stamp, ref_text([ref(cfg, data.get("rows"))])) + (["chart_with_downwards_trend"],))
    if extra:
        sends.append(build_extra(cfg, extra, data, stamp, ref_text([ref(cfg, data.get("rows"))])) + (["bell"],))
    if cfg.get("ratio_lines"):
        rev = eval_ratio(cfg, st, data.get("rows") or [], data.get("pair_rows") or [], today, now)
        if rev:
            sends.append(build_ratio(cfg, rev, data, stamp, ref_text([ref(cfg, data.get("rows"))])) + (["scales"],))
    if cfg.get("notice_events") or cfg.get("notice_watch"):
        fails = list(fails)
        for e in eval_notices(cfg, st, today, fails, notice_fn):
            if e["kind"] == "notice_event":
                sends.append(build_notice_event(cfg, e, data, stamp, ref_text([ref(cfg, data.get("rows"))])) + (["clipboard"],))
            else:
                sends.append(build_notice_watch(cfg, e, data, stamp, ref_text([ref(cfg, data.get("rows"))])) + (["page_facing_up"],))
    if cfg.get("time_checks"):
        for e in eval_time_checks(cfg, st, today):
            sends.append(build_time_check(cfg, e, data, stamp, ref_text([ref(cfg, data.get("rows"))])) + (["calendar"],))
    for e in eval_us10y(cfg, st, data.get("us10y"), today):
        parts = [ref(cfg, data.get("rows"))]
        for other in cfg.get("us10y", {}).get("ref_also", []):
            try:
                parts.append(ref(load_cfg(other), None))
            except Exception as exc:
                print(f"ref {other} skipped: {exc}")
        sends.append(build_us10y(cfg, e, stamp, ref_text(parts)) + (["warning"],))
    if fails and st.get("last_fail_day") != today:
        sends.append(build_fail(cfg, fails, stamp) + (["warning"],))
        st["last_fail_day"] = today
    if cfg.get("footer_note"):
        sends = [(t, with_footer(cfg, m), p, tags) for t, m, p, tags in sends]
    for t, m, _, _ in sends:
        clean(t, m)
    return sends


def install_msg(cfg: dict, st: dict, data: dict, fails: list[str], stamp: str) -> tuple[str, str]:
    rows = data.get("rows") or []
    close = rows[-1]["close"] if rows else None
    px = f"종가 {fmt(close)}({rows[-1]['day']}, {data.get('src')}). {ladder(cfg, close)}" if rows else "시세 없음."
    lines = ", ".join(f"{ln['title']} {fmt(ln['level'])} {({'le': '이하', 'ge': '이상', 'lt': '미만'})[ln['op']]}" for ln in cfg["lines"])
    y = f" 미국 10년물 {data['us10y']:.3f}% (경고 5.30% 이상, 이 알림이 두 종목 공통으로 1회 발송)." if data.get("us10y") is not None else ""
    if not cfg.get("us10y"):
        y = cfg.get("us10y_install_note", " 미국 10년물 5.30% 공통 경고는 [005930] 알림이 한 번만 보냅니다.")
    armed = [k for k, v in st["lines"].items() if v.get("armed", True)]
    title = f"{cfg['title_prefix']} 설치 확인 | 매매 신호 아님"
    msg = "\n".join([
        head(cfg, stamp),
        f"이게 뭐 알림: {cfg['short']} 종가 기준 계획 알림 설치 확인입니다. KRX 거래일 19:00에 선을 새로 넘을 때만 옵니다.",
        f"지금 결과/상태: {px}{y}" + (" 조회 실패: " + ", ".join(fails) + "." if fails else "") + f" 무장된 선: {', '.join(armed)}.",
        f"지금 할 일: 없음. 선: {lines}. 선마다 1회, 1% 넘게 반대로 돌아와야 재무장.",
        "나중에: " + cfg.get("install_later", f"{cfg['earnings']['date']} 19:00에 실적 체크 알림 1회(2027년 HBM LTA, CAPEX와 판가, 중국 YMTC/CXMT와 ASP)."),
        f"익절/줄일 때: {trim_text(cfg)} 비중 축소.",
        "하지 말 것: 이 설치 확인을 매수나 매도 신호로 읽기.",
    ])
    msg = with_footer(cfg, msg)
    clean(title, msg)
    return title, msg


def run(name: str, argv: list[str]) -> int:
    mode = (argv[1] if len(argv) > 1 else "auto").lower()
    cfg = load_cfg(name)
    if mode == "selftest":
        return selftest(name)
    if mode in {"backfill", "backfill-dry"}:
        if not cfg.get("earn_auto"):
            print(f"{name}: no earn_auto in config; nothing to backfill")
            return 1
        return earn_backfill(cfg, dry=mode == "backfill-dry")
    if mode == "earn-dry":
        if not cfg.get("earn_auto"):
            print(f"{name}: no earn_auto in config")
            return 0
        now = datetime.now(KST)
        print("DRY RUN (earn_auto only)")
        for t, m, _, _ in earn_auto_step(cfg, copy.deepcopy(load_state(cfg)), now, now.strftime("%Y-%m-%d %H:%M KST")):
            print(t)
            print(append_ends(m, name))
        return 0
    dry = mode in {"dry", "test", "dry-run"}
    if dry:
        print("DRY RUN")
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    day = session_day(now)
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    st = load_state(cfg)

    if mode == "install":
        data, fails = collect(cfg, now)
        title, msg = install_msg(cfg, st, data, fails, stamp)
        print(title)
        print(msg)
        publish(topic, title, msg, 3, ["white_check_mark"], server)
        return 0

    if not is_trading_day(day) and mode not in {"force", "dry", "seed"}:
        print(f"{day} KRX holiday; silent")
        return 0

    data, fails = collect(cfg, now)
    print(f"data src={data.get('src')} rows={(data.get('rows') or [])[-3:]} us10y={data.get('us10y')} fails={fails}")

    if mode == "seed" or not st.get("seeded"):
        tmp = default_state(cfg)
        tmp["last_day"] = None
        ev = eval_lines(cfg, tmp, data.get("rows") or [], day.isoformat(), now)
        y = eval_us10y(cfg, tmp, data.get("us10y"), day.isoformat())
        st.update({k: tmp[k] for k in ("lines", "last_day", "last_close")})
        if "extra" in tmp:
            st["extra"] = tmp["extra"]
        if "us10y" in tmp:
            st["us10y"] = tmp["us10y"]
        st["seeded"] = True
        st["seed_note"] = f"{stamp} 시드. 그때 이미 켜진 조건: {[e['key'] for e in ev] + (['us10y'] if y else []) or '없음'}"
        st["updated_at"] = now.isoformat()
        print(st["seed_note"])
        if not dry:
            save_state(cfg, st)
        return 0

    sends = plan_sends(cfg, st, data, fails, now)
    st["updated_at"] = now.isoformat()
    if not sends:
        print("no new condition; silent")
    for title, msg, pri, tags in sends:
        print(title)
        print(msg)
        print(f"topic={topic} send={not dry}")
        if not dry:
            publish(topic, title, msg, pri, tags, server)
    if not dry:
        save_state(cfg, st)
    return 0


# ---------- offline self-test ----------
def selftest(name: str) -> int:
    g = globals()
    orig = {k: g[k] for k in ("nxt_ref", "yahoo_rows", "naver_rows", "get_json")}

    def no_net(*a, **k):
        raise RuntimeError("offline selftest")
    g["nxt_ref"] = lambda c, rows, now: f"{c['short']} NXT 정보 없음"
    g["yahoo_rows"] = g["naver_rows"] = g["get_json"] = no_net
    try:
        return _selftest(name, orig)
    finally:
        g.update(orig)


def _selftest(name: str, orig: dict) -> int:
    g = globals()
    cfg = load_cfg(name)
    extras = cfg.pop("extra_lines", None)  # plan-line checks run on the plan lines only; extras checked below
    ok = True

    def check(label, got, want):
        nonlocal ok
        good = got == want
        ok &= good
        print(f"{'ok  ' if good else 'FAIL'} {label}: got={got} want={want}")

    def at(d: str, h: int = 19) -> datetime:
        return datetime.fromisoformat(d).replace(hour=h, tzinfo=KST)

    seed = load_state(cfg)  # committed seed must not fire on its own closes
    st = copy.deepcopy(seed)
    rows = [{"day": cfg["seed"]["day"], "close": cfg["seed"]["close"]}]
    check("seed close re-run silent", [s[0] for s in plan_sends(cfg, st, {"rows": rows}, [], at(cfg["seed"]["day"]), lambda c, d: None)], [])
    # The checks below are written against the config seed day (sessions after cfg seed.day). The committed state
    # moves on with every 19:00 run, so use a fresh state pinned to the config seed (fixed 2026-10-26: after the
    # 10-26 run committed last_day 2026-10-26 these checks failed on main).
    seed = default_state(cfg)
    seed.update(seeded=True, last_day=cfg["seed"]["day"], last_close=cfg["seed"]["close"])

    lv = {ln["key"]: ln["level"] for ln in cfg["lines"]}
    st = default_state(cfg)
    st["last_day"] = "2026-11-01"
    st["earn_done"] = {cfg["earnings"]["date"]: "x"}
    days = iter(f"2026-11-{i:02d}" for i in range(2, 31))

    def feed(close):
        d = next(days)
        ev = eval_lines(cfg, st, [{"day": d, "close": close}], d, at(d))
        return sorted(e["key"] for e in ev)

    full = all(k in lv for k in ("buy1", "buy2", "buy3", "trim1", "trim2", "stop"))
    tb = max((ln for ln in cfg["lines"] if ln["op"] == "le"), key=lambda x: x["level"])  # buy1 for 005930/000660
    tk, b1 = tb["key"], tb["level"]
    sp = lv["stop"]
    if full:
        _selftest_ladder(check, feed, lv)
    else:  # reduced ladder (064400: buy3, trim1, stop)
        t1 = min(ln["level"] for ln in cfg["lines"] if ln["op"] == "ge")
        tkey = next(ln["key"] for ln in cfg["lines"] if ln["op"] == "ge" and ln["level"] == t1)
        check(f"{tk} cross fires", feed(b1), [tk])
        check(f"{tk} again silent", feed(b1 - 1000), [])
        check("back above but < 1% no re-arm", feed(b1 * 1.009), [])
        check("down again still silent", feed(b1), [])
        check("> 1% above re-arms (silent)", feed(b1 * 1.011), [])
        check(f"{tk} fires again after re-arm", feed(b1 - 1000), [tk])
        check("stop at exact level silent (strict <)", feed(sp), [])
        check("stop fires", feed(sp - 1000), ["stop"])
        check("stop re-arm needs > 1% above", feed(sp * 1.005), [])
        check("stop silent while disarmed", feed(sp - 2000), [])
        check(f"rally to {tkey} (re-arms buys)", feed(t1), [tkey])
        check(f"{tkey} again silent", feed(t1 + 5000), [])
        check(f"{tkey} dip < 1% below no re-arm", feed(t1 * 0.995), [])
        check(f"{tkey} >1% below re-arms then cross fires", (feed(t1 * 0.985), feed(t1))[1], [tkey])
        check("stop fires again after re-arm", feed(sp - 1), sorted([tk, "stop"]))
    b2 = lv.get("buy2")
    tl = tb["title"]

    # same day twice / late double run: day already in last_day -> silent
    st2 = default_state(cfg)
    st2["last_day"] = "2026-10-23"
    r = [{"day": "2026-10-26", "close": b1 - 1}]
    check("first run fires", [e["key"] for e in eval_lines(cfg, st2, r, "2026-10-26", at("2026-10-26"))], [tk])
    check("second run same day silent", eval_lines(cfg, st2, r, "2026-10-26", at("2026-10-26", 22)), [])
    st3 = default_state(cfg)
    st3["last_day"] = "2026-10-23"
    check("intraday row not judged", eval_lines(cfg, st3, r, "2026-10-26", at("2026-10-26", 14)), [])
    k2, c2 = ("buy2", b2 - 1) if b2 else ("stop", sp - 1)
    check("catch-up of missed days in order",
          [(e["day"], e["key"]) for e in eval_lines(cfg, st3, [{"day": "2026-10-26", "close": b1 - 1}, {"day": "2026-10-27", "close": c2}], "2026-10-27", at("2026-10-27"))],
          [("2026-10-26", tk), ("2026-10-27", k2)])

    # SK hynix style: seed just above buy1, first close <= buy1 fires once
    st4 = copy.deepcopy(seed)
    check(f"first run close <= {tk} fires once",
          [e["key"] for e in eval_lines(cfg, st4, [{"day": "2026-10-26", "close": b1}], "2026-10-26", at("2026-10-26"))], [tk])
    _selftest_rest(name, cfg, seed, check, at, orig, tk, b1, tl, lv, sp)
    if extras:
        _selftest_extra(name, check, at)
    if load_cfg(name).get("earn_auto"):
        _selftest_earn(name, check, at)
    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def _selftest_extra(name, check, at) -> None:
    full = load_cfg(name)
    seed = load_state(full)
    nxt = (date.fromisoformat(seed["last_day"]) + timedelta(days=1)).isoformat()
    st = copy.deepcopy(seed)
    ev = eval_lines(full, st, [{"day": nxt, "close": seed["last_close"]}], nxt, at(nxt))
    check("extra: committed state, same close next day is silent", [e["key"] for e in ev], [])
    for ln in full["extra_lines"]:
        if hit(ln, seed["last_close"]):
            check(f"extra {ln['key']}: already across at migration, disarmed", seed["extra"][ln["key"]]["armed"], False)
        f = float(ln.get("rearm_pct", full.get("rearm_pct", 1.0))) / 100
        lvl = ln["level"]
        on = lvl if ln["op"] in ("le", "ge") else lvl - 1
        off = (lvl * (1 - f) - 1) if ln["op"] == "ge" else (lvl * (1 + f) + 1)
        mid = (lvl - 1) if ln["op"] == "ge" else (lvl + 1)  # back across but inside the re-arm buffer
        s = default_state(full)
        s["last_day"] = "2026-11-01"
        days = iter(f"2026-11-{i:02d}" for i in range(2, 31))
        plan = {x["key"]: x for x in full["lines"]}

        def feed(c):
            d = next(days)
            return [(e["kind"], e["key"]) for e in eval_lines({**full, "lines": [], "extra_lines": [ln]}, s, [{"day": d, "close": c}], d, at(d))]
        check(f"extra {ln['key']} {ln['op']} {lvl}: fires", feed(on), [("extra", ln["key"])])
        check(f"extra {ln['key']}: no repeat", feed(on), [])
        if f > 0:
            check(f"extra {ln['key']}: inside buffer stays disarmed", feed(mid), [])
        check(f"extra {ln['key']}: re-arm", feed(off), [("extra_rearm", ln["key"])] if ln.get("rearm_title") else [])
        check(f"extra {ln['key']}: fires again", feed(on), [("extra", ln["key"])])
        e = {"kind": "extra", "key": ln["key"], "line": ln, "day": "2026-11-20", "close": on}
        title, msg, _ = build_extra(full, [e], {"rows": [{"day": "2026-11-20", "close": on}]}, "2026-11-20 19:00 KST")
        clean(title, msg)
        check(f"extra {ln['key']}: title prefix", title.startswith(full["title_prefix"]), True)
        if ln.get("buy_advice") and "stop" in plan:
            low = plan["stop"]["level"] - 1
            _, m2, _ = build_extra(full, [dict(e, close=low)], {"rows": [{"day": "2026-11-20", "close": low}]}, "x")
            check(f"extra {ln['key']}: no buy advice under stop", ln["action"] in m2, False)


# ---- earn_auto fixtures (offline). 문제없음 / 경계 / 확인 불가 for each item, plus flow, dedupe, backfill.
EARN_FIX = {
    "ok": "\n".join([
        "이에 전분기 대비 회사 D램 평균판매가격(ASP)은 약 20%, 낸드 ASP는 10% 후반 상승했다.",
        "2026년 연간 시설 투자 규모는 84조원으로 전년 대비 증가할 것으로 예상된다.",
        "Q. 메모리 장기공급계약(LTA) 축소 우려는 없나요?",
        "2027년 HBM 공급 물량은 목표 수준의 가격으로 협상이 완료돼 이미 완판된 상태다.",
        "일부 고객과는 장기공급계약을 완료했고 추가 고객과 협상을 진행 중이다.",
        "부품 가격 상승에 따른 완제품 판매가격 인상 영향으로 스마트폰 수요가 감소했다.",
        "환율 하락의 영향으로 부품 사업 이익이 줄었다.",
    ]),
    "warn": "\n".join([
        "이에 전분기 대비 회사 D램 ASP는 10% 초반 하락했고 낸드 ASP는 한 자릿수 후반 하락했다.",
        "2027년 시설 투자는 큰 폭으로 증가할 것으로 예상된다.",
        "4분기 D램 가격은 하락 전환이 불가피할 것으로 보인다.",
        "일부 고객과 2027년 HBM 장기공급계약 물량을 재협상하고 있다.",
    ]),
    "neg": "\n".join([  # negated warn words, no CAPEX/price sentence at all -> item1 문제없음, item2 확인 불가
        "장기공급계약 물량 축소는 없다.",
        "D램 평균판매가격은 40% 중반 상승했다.",
    ]),
    "none": "회사는 주주환원을 검토하고 있다. 신제품 출시를 준비한다.",
}


def _china_rss(items: list[tuple[str, str]]) -> bytes:
    body = "".join(f"<item><title>{t}</title><pubDate>{d}</pubDate></item>" for t, d in items)
    return f"<rss><channel>{body}</channel></rss>".encode()


def _selftest_earn(name, check, at) -> None:
    cfg = load_cfg(name)
    ea = cfg["earn_auto"]
    now = at("2026-10-30")
    cconf = ea["item3"]["china"]
    no_china = lambda c, n: {"status": ec.OK, "fail": None, "hits": [], "n": 0}  # noqa: E731
    # item judgments on fixtures
    st = {k: [i["status"] for i in earn_judge(ea, v, now, no_china)["items"]] for k, v in EARN_FIX.items()}
    check("earn fixture ok: 문제없음 x3", st["ok"], [ec.OK, ec.OK, ec.OK])
    check("earn fixture warn: 경계 x3", st["warn"], [ec.WARN, ec.WARN, ec.WARN])
    check("earn fixture negated/no capex: 1 문제없음, 2 확인 불가, 3 확인 불가(낸드 없음)", st["neg"], [ec.OK, ec.UNK, ec.UNK])
    check("earn fixture unrelated text: 확인 불가 x3", st["none"], [ec.UNK, ec.UNK, ec.UNK])
    check("earn no transcript: 확인 불가 x3", [i["status"] for i in earn_judge(ea, None, now, no_china)["items"]], [ec.UNK] * 3)
    i1 = ec.judge_item1(EARN_FIX["ok"], ea["item1"])
    check("item1 evidence prefers 2027 HBM answer, never the question", ("2027년 HBM" in i1["evidence"], i1["evidence"].startswith("Q")), (True, False))
    i2 = ec.judge_item2(EARN_FIX["warn"], ea["item2"])
    check("item2 경계 quotes the price-decline and the CAPEX sentence", ("하락" in i2["evidence"], "시설 투자" in i2["capex"]), (True, True))
    check("item2 ok: set price/FX decline lines are not a memory price hint", ec.judge_item2(EARN_FIX["ok"], ea["item2"])["why"], "설비 투자 확대 문장은 있으나 판가 하락 암시 없음")
    a = ec.judge_asp(EARN_FIX["warn"], -10.0)
    check("item3 ASP D램 -12% <= -10% 경계", (a["status"], a["dram"]["pct"]), (ec.WARN, -12.0))
    check("item3 ASP -8% only is not 경계", ec.judge_asp("D램 ASP는 한 자릿수 후반 하락했다. 낸드 ASP는 소폭 상승했다.", -10.0)["status"], ec.OK)
    # China news keywords (window, names, keyword, dedupe, failure)
    rss = _china_rss([("CXMT, 물량 공세로 삼전닉스 압박 - 뉴스1", "Tue, 20 Oct 2026 03:00:00 GMT"),
                      ("CXMT, 물량 공세로 삼전닉스 압박 - 다른매체", "Tue, 20 Oct 2026 04:00:00 GMT"),
                      ("YMTC 신임 대표 선임 - A", "Wed, 21 Oct 2026 03:00:00 GMT"),
                      ("삼성전자 출하 확대 - B", "Wed, 21 Oct 2026 03:00:00 GMT"),
                      ("CXMT 저가 공급 - C", "Mon, 01 Sep 2026 03:00:00 GMT")])
    ch = ec.china_news(cconf, now, lambda u: rss)
    check("china: keyword + name in 30 days, dedupe by headline", (ch["status"], ch["n"], ch["hits"][0]["kw"]), (ec.WARN, 1, "물량 공세"))
    check("china: no hit -> 문제없음", ec.china_news(cconf, now, lambda u: _china_rss([("YMTC 신임 대표 - A", "Wed, 21 Oct 2026 03:00:00 GMT")]))["status"], ec.OK)

    def down(u):
        raise RuntimeError("HTTP 503")
    check("china: feed failure -> 확인 불가", ec.china_news(cconf, now, down)["status"], ec.UNK)
    j = earn_judge(ea, EARN_FIX["ok"], now, lambda c, n: ch)
    check("ok transcript + China keyword -> item3 경계(china_only)", (j["items"][2]["status"], j["items"][2]["china_only"]), (ec.WARN, True))
    trims = trims_short(cfg)
    check("conclusion: China keyword only -> 보류", ec.conclusion(j["items"], trims)[0], "추가 매수 보류, 중국 기사 확인")
    check("conclusion: counts_as_negative -> 중단", ec.conclusion(j["items"], trims, True)[0], "추가 매수 중단, 비중 축소 검토")
    jw = earn_judge(ea, EARN_FIX["warn"], now, no_china)
    check("conclusion: 경계 -> 추가 매수 중단, 축소선 언급", (ec.conclusion(jw["items"], trims)[0], "260,000원 30%" in ec.conclusion(jw["items"], trims)[1]),
          ("추가 매수 중단, 비중 축소 검토", True))
    jn = earn_judge(ea, EARN_FIX["neg"], now, no_china)
    check("conclusion: 확인 불가 mixed -> names items, 보류", ec.conclusion(jn["items"], trims), ("추가 매수 보류", ec.conclusion(jn["items"], trims)[1]))
    check("conclusion: 확인 불가 names 2, 3", "확인 불가 2, 3번" in ec.conclusion(jn["items"], trims)[1], True)
    check("conclusion: all ok -> 기존 계획 유지", ec.conclusion(earn_judge(ea, EARN_FIX["ok"], now, no_china)["items"], trims)[0], "기존 계획 유지")
    # quarter detection from the notice feed
    ir = {"no": "3", "title": "삼성전자(주) 기업설명회(IR) 개최(안내공시)", "datetime": "2026-10-08 08:05",
          "contents": "<style>.x{}</style><td>1. 일시 및 장소</td><td>일시</td><td>2026-10-29</td><td>10:00</td>"}
    rows_q3 = [{"no": "5", "title": "삼성전자(주) 연결재무제표기준영업(잠정)실적(공정공시)", "datetime": "2026-10-29 06:50", "contents": ""},
               {"no": "4", "title": "삼성전자(주) (정정)연결재무제표기준영업(잠정)실적(공정공시)", "datetime": "2026-10-29 06:50", "contents": ""}, ir,
               {"no": "2", "title": "삼성전자(주) 연결재무제표기준영업(잠정)실적(공정공시)", "datetime": "2026-10-08 07:44", "contents": ""},
               {"no": "1", "title": "삼성전자(주) 연결재무제표기준영업(잠정)실적(공정공시)", "datetime": "2026-07-30 06:50", "contents": ""}]
    q = earn_quarter(cfg, rows_q3)
    check("quarter: Oct filings -> 2026Q3, results day from IR notice", (q["key"], q["first_day"], q["results_day"], q["results_src"]),
          ("2026Q3", "2026-10-08", "2026-10-29", "IR 개최 안내공시"))
    q = earn_quarter(cfg, [r for r in rows_q3 if r is not ir])
    check("quarter: no IR -> second results filing day", (q["results_day"], q["results_src"]), ("2026-10-29", "두 번째 실적 공시"))
    pre = {"no": "6", "title": "삼성전자(주) 연결재무제표기준영업(잠정)실적(공정공시)", "datetime": "2027-01-07 07:40", "contents": ""}
    ir4 = dict(ir, no="7", datetime="2027-01-07 08:05", contents="일시 2027-01-28 10:00")
    q4 = earn_quarter(cfg, [ir4, pre] + rows_q3)
    check("quarter: Jan 2027 prelim -> 2026Q4 (label, next)", (q4["key"], q4["label"], q4["results_day"], ec.next_quarter(q4["y"], q4["q"])),
          ("2026Q4", "2026년 4분기", "2027-01-28", (2027, 1)))
    q4b = earn_quarter(cfg, [pre] + rows_q3)
    check("quarter: prelim only -> results day unknown", (q4b["key"], q4b["results_day"]), ("2026Q4", None))
    ov = dict(cfg, earn_auto=dict(ea, override={"quarter": "2027Q1", "results_day": "2027-04-30", "transcript_url": "u"}))
    check("quarter: config override", (earn_quarter(ov, rows_q3)["key"], earn_quarter(ov, rows_q3)["results_day"], earn_quarter(ov, rows_q3)["url"]),
          ("2027Q1", "2027-04-30", "u"))
    check("title_re: comma variant and the real title", [bool(re.search(ec.title_re_for(ea["transcript"]["title_re"], 2026, 3), t)) for t in
          ("삼성전자 2026년 3분기 실적발표 컨퍼런스콜 전문", "삼성전자, 2026년 3분기 실적발표 컨퍼런스콜 전문", "SK하이닉스 2026년 3분기 실적발표 컨퍼런스콜 전문",
           "삼성전자 2026년 2분기 실적발표 컨퍼런스콜 전문")], [True, True, False, False])
    # flow: Q4 2026 (start_quarter) on the 19:00 slots
    found = {"text": None}
    io = {"notices": lambda c: [ir4, pre] + rows_q3,
          "transcript": lambda e, y, qq, url=None: (found["text"], "https://www.thelec.kr/news/articleView.html?idxno=1" if found["text"] else None),
          "china": no_china}
    s = {"earn_auto": {}}

    def step(d, h=19):
        return [t for t, *_ in earn_auto_step(cfg, s, at(d, h), f"{d} {h}:00 KST", None, io)]
    check("flow: before results day silent", step("2027-01-27"), [])
    check("flow: results day, no transcript yet -> filing notice only",
          step("2027-01-28"), [f"{cfg['title_prefix']} 2026년 4분기 실적 체크 | 공시 확인 필요"])
    check("flow: next day still no transcript -> silent", step("2027-01-29"), [])
    found["text"] = EARN_FIX["warn"]
    check("flow: transcript found -> one judged result", step("2027-02-01"), [f"{cfg['title_prefix']} 2026년 4분기 실적 자동 점검 | 추가 매수 중단, 비중 축소 검토"])
    check("flow: state marks the quarter", (s["earn_auto"]["2026Q4"]["kind"], s["earn_auto"]["2026Q4"]["verdict"]), ("judged", {"1": ec.WARN, "2": ec.WARN, "3": ec.WARN}))
    check("flow: second run same quarter silent", step("2027-02-02"), [])
    found["text"], s = None, {"earn_auto": {}}
    step("2027-01-28")
    days = ["2027-01-29", "2027-02-01", "2027-02-02", "2027-02-03", "2027-02-04", "2027-02-05"]
    got = [step(d) for d in days]
    check(f"flow: no transcript within {ea['wait_trading_days']} trading days -> one 확인 불가 message on the 5th",
          got, [[], [], [], [], [f"{cfg['title_prefix']} 2026년 4분기 실적 자동 점검 | 컨콜 전문 없음, 추가 매수 보류"], []])
    check("flow: no-transcript kind", (s["earn_auto"]["2026Q4"]["kind"], s["earn_auto"]["2026Q4"]["verdict"]), ("no_transcript", {"1": ec.UNK, "2": ec.UNK, "3": ec.UNK}))
    s = {"earn_auto": {}}
    io_q3 = dict(io, notices=lambda c: rows_q3)
    found["text"] = EARN_FIX["ok"]
    check("flow: 2026Q3 is before start_quarter -> never automatic", [t for t, *_ in earn_auto_step(cfg, s, at("2026-10-30"), "x", None, io_q3)], [])

    def feed_down(c):
        raise RuntimeError("HTTP 500")
    check("flow: notice feed down -> silent, no state", (earn_auto_step(cfg, s, at("2027-01-28"), "x", None, dict(io, notices=feed_down)), s), ([], {"earn_auto": {}}))
    # messages: six-item order (bullet lines skipped), forbidden chars, routing, size
    qi = {"key": "2026Q3", "y": 2026, "q": 3, "label": "2026년 3분기", "results_day": "2026-10-29", "first_day": "2026-10-08", "results_src": "IR 개최 안내공시"}
    dirty = dict(ch, hits=[dict(ch["hits"][0], title="CXMT, 삼전\u00b7하닉 제치고 \u2014 물량 공세 - X")])
    msgs = [build_earn_result(cfg, qi, earn_judge(ea, t, now, (lambda c, n, d=dirty: d)), "https://x", "2026-10-30 19:00 KST", "참고: x.")
            for t in (EARN_FIX["ok"], EARN_FIX["warn"], EARN_FIX["neg"], None, EARN_FIX["ok"] + "\n고성능\u00b7고용량 장기공급계약을 체결했다.")]
    msgs.append(build_earn_notice(cfg, qi, None, "2026-10-30 19:00 KST", "참고: x."))
    order = ["이게 뭐 알림:", "지금 결과/상태:", "지금 할 일:", "나중에:", "익절/줄일 때:", "하지 말 것:"]
    bad_order = [t for t, m, _ in msgs if [x.split(":")[0] + ":" for x in m.split("\n")[1:] if not x.startswith(("- ", "참고:"))] != order]
    check("earn messages: six-item order", bad_order, [])
    check("earn messages: no middle dot / em-dash", [t for t, m, _ in msgs if any(c in t + m for c in FORBIDDEN)], [])
    from ntfy_util import route_topic
    check("earn messages: all routed to cwally-port (titles avoid ops words)", sorted({route_topic("cwally-port", t) for t, *_ in msgs}), ["cwally-port"])
    check("earn messages: under 3,700 bytes with the end line", [t for t, m, _ in msgs if len(append_ends(m, name).encode("utf-8")) > 3700], [])
    check("earn messages: China part says keyword-based, manual", all("키워드" in m and "직접" in m for _, m, _ in msgs[:5]), True)
    check("earn no-transcript message says 확인 불가 and 보류", ("확인 불가" in msgs[3][1], "보류" in msgs[3][0]), (True, True))
    # backfill: dry writes nothing; real send writes only earn_auto[2026Q3] once; missing transcript sends nothing
    g = globals()
    import tempfile
    keep = {k: g[k] for k in ("state_path", "publish")}
    sent = []
    bio = {"transcript": lambda e, y, qq, url=None: (EARN_FIX["ok"], url), "china": no_china}
    with tempfile.TemporaryDirectory() as tmp:
        sp = Path(tmp) / f"{name}.json"
        sp.write_text(json.dumps(load_state(cfg), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        before = sp.read_text(encoding="utf-8")
        g["state_path"] = lambda n: sp
        g["publish"] = lambda *a, **k: sent.append(a[1])
        try:
            rc = earn_backfill(cfg, False, now, dict(bio, transcript=lambda *a, **k: (None, None)))
            check("backfill: no transcript -> rc 1, no send, no state", (rc, sent, sp.read_text(encoding="utf-8") == before), (1, [], True))
            rc = earn_backfill(cfg, True, now, bio, lambda: "참고: x.")
            check("backfill dry: rc 0, no send, state file unchanged", (rc, sent, sp.read_text(encoding="utf-8") == before), (0, [], True))
            rc = earn_backfill(cfg, False, now, bio, lambda: "참고: x.")
            after = json.loads(sp.read_text(encoding="utf-8"))
            check("backfill real: one send, title", (rc, sent), (0, [f"{cfg['title_prefix']} 2026년 3분기 실적 자동 점검 | 기존 계획 유지"]))
            check("backfill real: only earn_auto added", ({k: v for k, v in after.items() if k != "earn_auto"} == json.loads(before), sorted(after["earn_auto"])),
                  (True, ["2026Q3"]))
            rc = earn_backfill(cfg, False, now, bio, lambda: "참고: x.")
            check("backfill again: silent", (rc, len(sent)), (0, 1))
        finally:
            g.update(keep)
    check("2026Q3 backfill mark also blocks the automatic path", earn_auto_step(dict(cfg, earn_auto=dict(ea, start_quarter="2026Q3")),
          {"earn_auto": {"2026Q3": {"sent": "x"}}}, at("2026-10-30"), "x", None, io_q3), [])
    # secq3 keeps the same functions (moved, not changed)
    import secq3_monitor as sq
    check("secq3 uses the shared parser objects", (sq.asp_pick is ec.asp_pick, sq.judge_check2 is ec.judge_check2, sq.transcript_body is ec.transcript_body), (True, True, True))


def _selftest_ladder(check, feed, lv) -> None:
    b1, b2, b3, t1, t2, sp = (lv[k] for k in ("buy1", "buy2", "buy3", "trim1", "trim2", "stop"))
    check("buy1 cross fires", feed(b1), ["buy1"])
    check("buy1 again silent", feed(b1 - 1000), [])
    check("back above but < 1% no re-arm", feed(b1 * 1.009), [])
    check("down again still silent", feed(b1), [])
    check("> 1% above re-arms (silent)", feed(b1 * 1.011), [])
    check("buy1 fires again after re-arm", feed(b1 - 1000), ["buy1"])
    check("gap to buy2+buy3", feed(b3), ["buy2", "buy3"])
    check("stop at exact level silent (strict <)", feed(sp), [])
    check("stop fires", feed(sp - 1000), ["stop"])
    check("stop re-arm needs > 1% above", feed(sp * 1.005), [])
    check("stop silent while disarmed", feed(sp - 2000), [])
    check("rally to trim1 (re-arms buys)", feed(t1), ["trim1"])
    check("trim1 again silent", feed(t1 + 5000), [])
    check("trim2 fires", feed(t2), ["trim2"])
    check("trim1 dip < 1% below no re-arm", feed(t1 * 0.995), [])
    check("trim1 >1% below re-arms then cross fires", (feed(t1 * 0.985), feed(t1))[1], ["trim1"])
    check("stop fires again after re-arm", feed(sp - 1), ["buy1", "buy2", "buy3", "stop"])


def _selftest_rest(name, cfg, seed, check, at, orig, tk, b1v, tl, lv, sp) -> None:
    g = globals()
    # us10y (005930 only)
    if cfg.get("us10y"):
        s = default_state(cfg)
        seq = [(5.25, 0), (5.30, 1), (5.35, 0), (5.21, 0), (5.19, 0), (5.31, 1), (5.31, 0)]
        got = [len(eval_us10y(cfg, s, y, f"2026-11-{i + 2:02d}")) for i, (y, _) in enumerate(seq)]
        check("us10y fire once + re-arm < 5.20", got, [w for _, w in seq])
        s = default_state(cfg)
        check("us10y same-day twice", [len(eval_us10y(cfg, s, 5.4, "2026-11-02")), len(eval_us10y(cfg, s, 5.4, "2026-11-02"))], [1, 0])
    else:
        check(f"{name} has no us10y", "us10y" in default_state(cfg), False)

    # earnings
    ed = date.fromisoformat(cfg["earnings"]["date"])
    s = copy.deepcopy(seed)
    titles = lambda sends: [t for t, *_ in sends]  # noqa: E731
    fake = {"title": "연결재무제표기준영업(잠정)실적(공정공시)", "datetime": f"{ed} 07:30"}
    check("day before: no earnings", titles(plan_sends(cfg, s, {}, [], at((ed - timedelta(1)).isoformat()), lambda c, d: fake)), [])
    t = titles(plan_sends(cfg, s, {}, [], at(ed.isoformat()), lambda c, d: fake))
    check("earnings day fires (filing found)", t, [f"{cfg['title_prefix']} 실적 체크 | 공시 확인됨"])
    check("earnings second run silent", titles(plan_sends(cfg, s, {}, [], at(ed.isoformat(), 23), lambda c, d: fake)), [])
    s = copy.deepcopy(seed)
    check("late after-midnight run = same session day",
          titles(plan_sends(cfg, s, {}, [], datetime.combine(ed + timedelta(1), datetime.min.time(), KST).replace(hour=1), lambda c, d: None)),
          [f"{cfg['title_prefix']} 실적 체크 | 공시 확인 필요"])
    s = copy.deepcopy(seed)

    def boom(c, d):
        raise RuntimeError("timeout")
    check("catch-up next day, feed failure -> 확인 필요",
          titles(plan_sends(cfg, s, {}, [], at((ed + timedelta(1)).isoformat()), boom)), [f"{cfg['title_prefix']} 실적 체크 | 공시 확인 필요"])
    s = copy.deepcopy(seed)
    check("too late (> catch_up_days) silent", titles(plan_sends(cfg, s, {}, [], at((ed + timedelta(5)).isoformat()), boom)), [])

    # messages: forbidden chars, body order, fail notice routing
    stamp = "2026-10-26 19:00 KST"
    all_msgs = []
    for k in [k for k in SEVERITY if k in lv]:
        ln = next(x for x in cfg["lines"] if x["key"] == k)
        all_msgs.append(build_lines(cfg, [{"kind": "line", "key": k, "line": ln, "day": "2026-10-26", "close": ln["level"]}], {"rows": [{"day": "2026-10-26", "close": ln["level"]}], "src": "Yahoo"}, stamp))
    all_msgs.append(build_earnings(cfg, str(ed), None, "x", stamp))
    all_msgs.append(build_fail(cfg, ["x"], stamp))
    if cfg.get("us10y"):
        all_msgs.append(build_us10y(cfg, {"y": 5.31}, stamp))
    all_msgs.append(install_msg(cfg, copy.deepcopy(seed), {"rows": [{"day": "2026-10-23", "close": cfg["seed"]["close"]}], "src": "Yahoo", "us10y": 4.99}, [], stamp) + (3,))
    order = ["이게 뭐 알림:", "지금 결과/상태:", "지금 할 일:", "나중에:", "익절/줄일 때:", "하지 말 것:"]
    bad = [t for t, m, _ in all_msgs if any(ch in t + m for ch in FORBIDDEN)]
    check("no middle dot / em-dash", bad, [])
    check("six-item body order", [t for t, m, _ in all_msgs if [ln.split(":")[0] + ":" for ln in m.split("\n")[1:7]] != order], [])
    check("titles have prefix", [t for t, *_ in all_msgs if not t.startswith(cfg["title_prefix"] + " ")], [])
    from ntfy_util import route_topic
    check("fail notice -> cwally-ops", route_topic("cwally-port", build_fail(cfg, ["x"], stamp)[0]), "cwally-ops")
    check("earnings 확인 필요 stays on cwally-port", route_topic("cwally-port", build_earnings(cfg, str(ed), None, None, stamp)[0]), "cwally-port")
    check("stop title", all_msgs[0][0], f"{cfg['title_prefix']} 종가 {fmt(sp)} 미만 | 손절선 이탈")
    # stop together with buy lines (gap or catch-up): 할 일 has only the stop action, no buy advice
    evs = [{"kind": "line", "key": ln["key"], "line": ln, "day": "2026-10-26", "close": sp - 1} for ln in cfg["lines"] if ln["key"].startswith("buy") or ln["key"] == "stop"]
    _t, _m, _p = build_lines(cfg, evs, {"rows": [{"day": "2026-10-26", "close": sp - 1}], "src": "Yahoo"}, stamp)
    todo = next(x for x in _m.split("\n") if x.startswith("지금 할 일:"))
    stop_act = next(x["action"] for x in cfg["lines"] if x["key"] == "stop")
    check("stop+buy combined: 할 일 = stop action only", todo, "지금 할 일: " + stop_act)
    check("stop+buy combined: no buy advice", [w for w in ("분할 매수", "분할 매수만", "실행.", "사세요") if w in todo.replace(stop_act, "")], [])
    check("stop+buy combined: p5", _p, 5)
    # ---- hold on failure: Yahoo down, Naver (NXT) has a price below buy1 -> nothing judged, ops notice, catch-up next run
    def yahoo_down(*a, **k):
        raise RuntimeError("HTTP 503")
    g["yahoo_rows"] = yahoo_down
    g["naver_rows"] = lambda c: [{"day": "2026-10-26", "close": b1v - 1000}]
    d1, f1 = collect(cfg, at("2026-10-26"))
    check("yahoo fail: no rows from naver", d1.get("rows"), [])
    s = copy.deepcopy(seed)
    sends = plan_sends(cfg, s, d1, f1, at("2026-10-26"), lambda c, d: None, lambda c, rows, now: "x")
    check("yahoo fail: only the failure notice", [t for t, *_ in sends], [f"{cfg['title_prefix']} 조회 실패 | 조건 판단 보류"])
    check("yahoo fail: notice routed to cwally-ops", route_topic("cwally-port", sends[0][0]), "cwally-ops")
    check("yahoo fail: session left unprocessed", s["last_day"], cfg["seed"]["day"])
    check(f"yahoo fail: {tk} still armed", s["lines"][tk]["armed"], True)
    def yrows(rows):  # stock rows from Yahoo; ^TNX stays quiet at 4.9%
        return lambda c, sym, regular_only=False: [{"day": rows[-1]["day"], "close": 4.9}] if sym.startswith("^") else rows
    g["yahoo_rows"] = yrows([{"day": cfg["seed"]["day"], "close": cfg["seed"]["close"]},
                             {"day": "2026-10-26", "close": b1v - 1000}, {"day": "2026-10-27", "close": b1v - 500}])
    d2, f2 = collect(cfg, at("2026-10-27"))
    sends = plan_sends(cfg, s, d2, f2, at("2026-10-27"), lambda c, d: None, lambda c, rows, now: "x")
    check("next run catches up 10-26 once, no fail notice", [(t, "2026-10-26" in m) for t, m, *_ in sends],
          [(f"{cfg['title_prefix']} 종가 {fmt(b1v)} 이하 | {tl}", True)])
    check("catch-up last_day", s["last_day"], "2026-10-27")
    # Yahoo up but missing the session's regular close -> hold
    g["yahoo_rows"] = yrows([{"day": cfg["seed"]["day"], "close": cfg["seed"]["close"]}])
    d3, f3 = collect(cfg, at("2026-10-26"))
    check("missing regular close -> hold notice", any("2026-10-26 정규장 종가 없음" in f for f in f3), True)
    s = copy.deepcopy(seed)
    plan_sends(cfg, s, d3, f3, at("2026-10-26"), lambda c, d: None, lambda c, rows, now: "x")
    check("missing regular close: session unprocessed", s["last_day"], cfg["seed"]["day"])
    check("before 15:30 no hold notice", collect(cfg, at("2026-10-26", 14))[1], [])
    check("holiday (Sat) no hold notice", collect(cfg, at("2026-10-24"))[1], [])
    # Yahoo meta stamped before 15:30 -> today's candle dropped (intraday is not a regular close)
    g["get_json"] = lambda url: {"chart": {"result": [{"timestamp": [int(at("2026-10-23", 9).timestamp()), int(at("2026-10-26", 9).timestamp())],
                                                       "indicators": {"quote": [{"close": [215500.0, 200000.0]}]},
                                                       "meta": {"regularMarketPrice": 200000.0, "regularMarketTime": int(at("2026-10-26", 14).timestamp())}}]}}
    check("intraday Yahoo candle dropped", [r["day"] for r in orig["yahoo_rows"](cfg, cfg["yahoo_symbol"], regular_only=True)], ["2026-10-23"])
    g["get_json"] = lambda url: {"chart": {"result": [{"timestamp": [int(at("2026-10-26", 9).timestamp())], "indicators": {"quote": [{"close": [201000.0]}]},
                                                       "meta": {"regularMarketPrice": 200500.0, "regularMarketTime": int(at("2026-10-26", 15).replace(minute=30).timestamp())}}]}}
    check("regular close after 15:30 kept", orig["yahoo_rows"](cfg, cfg["yahoo_symbol"], regular_only=True), [{"day": "2026-10-26", "close": 200500.0}])

    # ---- NXT reference line: format, label, never fires
    reg = cfg["seed"]["close"]
    nx = round(reg * 0.989 / 500) * 500
    pct = f"{(nx / reg - 1) * 100:+.1f}%"
    check("ref 진행 중 (19:00)", nxt_line(cfg["short"], reg, nx, at("2026-10-26")),
          f"{cfg['short']} NXT 애프터마켓 (19:00 기준, 진행 중) {fmt(nx)}, 정규장 대비 {pct}")
    check("ref 마감 (20:00)", nxt_line(cfg["short"], reg, nx, at("2026-10-26", 20)),
          f"{cfg['short']} NXT 애프터마켓 (20:00 기준, 마감) {fmt(nx)}, 정규장 대비 {pct}")
    check("ref 마감 (late run 01:10 next day)", "마감" in nxt_line(cfg["short"], reg, nx, at("2026-10-27", 1)), True)
    check("ref equal -> NXT 정보 없음", nxt_line(cfg["short"], reg, reg, at("2026-10-26")), f"{cfg['short']} NXT 정보 없음")
    check("ref unavailable -> NXT 정보 없음", nxt_line(cfg["short"], reg, None, at("2026-10-26")), f"{cfg['short']} NXT 정보 없음")
    g["naver_rows"] = lambda c: [{"day": "2026-10-26", "close": float(nx)}]
    check("nxt_ref uses session row", orig["nxt_ref"](cfg, [{"day": "2026-10-26", "close": float(reg)}], at("2026-10-26")),
          f"{cfg['short']} NXT 애프터마켓 (19:00 기준, 진행 중) {fmt(nx)}, 정규장 대비 {pct}")
    g["naver_rows"] = yahoo_down
    check("nxt_ref naver down -> 정보 없음", orig["nxt_ref"](cfg, [{"day": "2026-10-26", "close": float(reg)}], at("2026-10-26")), f"{cfg['short']} NXT 정보 없음")
    s = copy.deepcopy(seed)
    low_ref = lambda c, rows, now: nxt_line(c["short"], reg, lv["stop"] - 1, now)  # noqa: E731
    check("NXT below every buy/stop line alone never fires",
          plan_sends(cfg, s, {"rows": [{"day": "2026-10-26", "close": float(reg)}], "src": "Yahoo 정규장"}, [], at("2026-10-26"), lambda c, d: None, low_ref), [])
    s = copy.deepcopy(seed)
    sends = plan_sends(cfg, s, {"rows": [{"day": "2026-10-26", "close": float(b1v)}], "src": "Yahoo 정규장"}, [], at("2026-10-26"),
                       lambda c, d: None, lambda c, rows, now: nxt_line(c["short"], float(b1v), float(b1v) * 0.99, now))
    last = sends[0][1].split("\n")[-1]
    check("price message ends with the 참고 line", last.startswith("참고: ") and "진행 중" in last and "정규장 대비 -1.0%" in last, True)
    check("참고 line has no middle dot / em-dash", any(ch in last for ch in FORBIDDEN), False)
    if cfg.get("us10y"):
        s = copy.deepcopy(seed)
        sends = plan_sends(cfg, s, {"rows": [], "us10y": 5.31}, [], at("2026-10-26"), lambda c, d: None,
                           lambda c, rows, now: f"{c['short']} NXT 정보 없음")
        check("10Y message ref covers both stocks", sends[0][1].split("\n")[-1], "참고: 삼성전자 NXT 정보 없음; SK하이닉스 NXT 정보 없음. 판단에는 정규장 종가만 씁니다.")
    s = copy.deepcopy(seed)
    sends = plan_sends(cfg, s, {"rows": []}, [], at(cfg["earnings"]["date"]), lambda c, d: None, lambda c, rows, now: f"{c['short']} NXT 정보 없음")
    check("earnings message has 참고 line", sends[0][1].split("\n")[-1].startswith("참고: "), True)
    print("SAMPLE:", last)
    print("SAMPLE:", all_msgs[-4 if cfg.get("us10y") else -3][0])


if __name__ == "__main__":
    raise SystemExit(run("005930", sys.argv))
