#!/usr/bin/env python3
"""[GRAB-PX] Grab Holdings (NASDAQ: GRAB) intraday price lines and earnings dates, US regular hours.

Separate from [GRAB] (grab_monitor.py: 09:00 KST daily-close lines 4.15 / 3.20 and the
Indonesia Komdigi / Perpres delivery commission-cap news). This alert does not duplicate that news.

Price (Yahoo last price, every run inside 09:30-16:00 America/New_York weekdays):
  zone  : 2.85 <= last <= 2.95  first buy zone, p4
  bear  : last < 2.73           DBS bear-case target, p4
  low52 : last < 2.67           52-week low (2026-10-26) break, p5
Each line fires once per crossing. It is disarmed when it fires and re-armed only after
the price leaves the zone / line by rearm_pct (1%). When several lines fire in one run,
only the deepest is sent and the shallower below-lines are disarmed too.

Earnings (first in-session run of each NY date): Grab IR press release RSS (JSON feed fallback).
"Grab to Announce <Q> Quarter <YYYY> Results on <date>" -> one 발표일 확정 notice, then D-7 and
D-1 reminders. Before the official notice, the config estimate (Q4 2026 ~ 2027-02-10) gives one
예상 시기 reminder at D-7. A quarter with a "Grab Reports ..." release is done.

Attached to the existing tsla slot (job=tsla) in port-alerts.yml; no new cron.
Modes: auto | force | dry [price] | selftest. dry never calls publish() and never writes state/.
Not investment advice.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import append_ends, publish, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
NY = ZoneInfo("America/New_York")
CFG_PATH = ROOT / "configs" / "grabpx.json"
STATE_PATH = ROOT / "state" / "grabpx.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}
OPEN_MIN = 9 * 60 + 30
CLOSE_MIN = 16 * 60
OFF_SESSION = {"PRE", "POST", "PREPRE", "POSTPOST", "CLOSED"}
FORBIDDEN = ("\u00b7", "\u2014")  # middle dot, em dash; kept as escapes so copy stays free of them
DEPTH = ["zone", "bear", "low52"]  # shallow -> deep
DONT = "하지 말 것: 전량 즉시 매수, 신용, 미수, 시장가 추격. 조회 실패를 매매 신호로 보지 말 것."
TP = "익절/줄일 때: 이 알림은 매도 신호가 아닙니다. 익절 가격은 여기서 정하지 않습니다."

QUARTER_WORD = {"First": 1, "Second": 2, "Third": 3, "Fourth": 4}
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}
ANNOUNCE_RE = re.compile(
    r"Grab to Announce (First|Second|Third|Fourth) Quarter(?: and Full Year)? (\d{4})"
    r"(?: and Full Year \d{4})? Results on (.+)$", re.I)
REPORTS_RE = re.compile(r"Grab Reports .*?(First|Second|Third|Fourth) Quarter(?: and (?:Full Year )?)?\s?(\d{4})", re.I)



# ---------- io ----------

def load_cfg() -> dict:
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


def default_state() -> dict:
    return {
        "price": {"armed": {k: True for k in DEPTH}, "last_price": None, "last_at": None},
        "earn": {},
        "events_day": None,
        "fail_day": None,
        "updated_at": None,
    }


def load_state() -> dict:
    base = default_state()
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k, v in data.items():
                    base[k] = v
        except json.JSONDecodeError:
            pass
    base.setdefault("price", {}).setdefault("armed", {})
    for k in DEPTH:
        base["price"]["armed"].setdefault(k, True)
    if not isinstance(base.get("earn"), dict):
        base["earn"] = {}
    return base


def save_state(state: dict, dry: bool) -> None:
    if dry:
        return
    state["updated_at"] = datetime.now(KST).isoformat(timespec="seconds")
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def http_get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def clean(text: str) -> str:
    """Strip the two forbidden characters from outside text (headlines)."""
    return (text or "").replace("\u00b7", ", ").replace("\u2014", "-").strip()


# ---------- session + price ----------

def in_regular_session(now_ny: datetime) -> bool:
    if now_ny.weekday() >= 5:
        return False
    minutes = now_ny.hour * 60 + now_ny.minute
    return OPEN_MIN <= minutes <= CLOSE_MIN


def fetch_last(ticker: str) -> tuple[float | None, str | None, float | None]:
    """(last regular price, marketState, fiftyTwoWeekLow). price None = fetch failed."""
    last_exc: Exception | None = None
    for host in (
        "https://query1.finance.yahoo.com/v8/finance/chart/",
        "https://query2.finance.yahoo.com/v8/finance/chart/",
    ):
        try:
            payload = json.loads(http_get(f"{host}{ticker}?interval=1m&range=1d"))
            result = (payload.get("chart") or {}).get("result") or []
            if not result:
                raise ValueError("empty chart result")
            meta = result[0].get("meta") or {}
            raw = meta.get("regularMarketPrice")
            if raw is None:
                raise ValueError("regularMarketPrice missing")
            price = float(raw)
            if price != price or price <= 0:
                raise ValueError(f"bad price {raw!r}")
            state = meta.get("marketState")
            low = meta.get("fiftyTwoWeekLow")
            return price, (str(state) if state else None), (float(low) if low else None)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            print(f"yahoo fail {host}: {exc}", file=sys.stderr)
    print(f"yahoo failed both hosts: {last_exc}", file=sys.stderr)
    return None, None, None


def hit(spec: dict, price: float) -> bool:
    if spec["kind"] == "zone":
        return spec["low"] <= price <= spec["high"]
    return price < spec["level"]


def rearm_ok(spec: dict, price: float) -> bool:
    pct = float(spec.get("rearm_pct", 1.0)) / 100
    if spec["kind"] == "zone":
        return price < spec["low"] * (1 - pct) or price > spec["high"] * (1 + pct)
    return price >= spec["level"] * (1 + pct)


def evaluate(levels: dict, armed: dict, price: float) -> tuple[str | None, dict]:
    """Return (line to send or None, new armed map)."""
    out = dict(armed)
    for key in DEPTH:
        if not out.get(key, True) and rearm_ok(levels[key], price):
            out[key] = True
    fired = [k for k in DEPTH if out.get(k, True) and hit(levels[k], price)]
    for k in fired:
        out[k] = False
    if not fired:
        return None, out
    deepest = fired[-1]
    # a deeper break also disarms shallower below-lines that are under water
    for k in DEPTH[: DEPTH.index(deepest)]:
        if hit(levels[k], price):
            out[k] = False
    return deepest, out


def price_message(cfg: dict, key: str, price: float, stamp: str, yahoo_low: float | None) -> tuple[str, str, int]:
    spec = cfg["levels"][key]
    if spec["kind"] == "zone":
        cond = f"조건: 현재가 {spec['low']:.2f}~{spec['high']:.2f}달러 구간 진입"
    else:
        cond = f"조건: 현재가 {spec['level']:.2f}달러 미만"
    low_txt = f" 야후 52주 저가 {yahoo_low:.2f}달러." if yahoo_low else ""
    lines = [
        f"이게 뭐 알림: {cfg['explain']}",
        f"지금 결과/상태: {spec['status']} 현재가 {price:.2f}달러. {cond}. 시각 {stamp}.{low_txt}",
        f"지금 할 일: {spec['todo']}",
        f"나중에: {spec['later']} 같은 선은 한 번 지날 때 한 번만 옵니다.",
        TP,
        DONT,
    ]
    body = append_ends("\n".join(lines), "grabpx")
    return spec["title"], body, int(spec["priority"])


def fail_message(stamp: str) -> tuple[str, str]:
    title = "[GRAB-PX] 시세 조회 실패 | 매매 신호 아님"
    lines = [
        "이게 뭐 알림: 그랩(GRAB) 현재가를 읽지 못했습니다. 매매 신호가 아닙니다.",
        f"지금 결과/상태: 시세 조회 실패. 현재가 없음. 시각 {stamp}.",
        "지금 할 일: 증권앱에서 GRAB 현재가를 직접 확인하세요.",
        "나중에: 다음 미국 정규장 30분 실행에서 다시 읽습니다. 실패 안내는 하루 한 번입니다.",
        "익절/줄일 때: 이번 안내는 매매 신호가 아닙니다.",
        "하지 말 것: 조회 실패를 매수나 매도 신호로 보지 말 것.",
    ]
    return title, append_ends("\n".join(lines), "grabpx")


# ---------- earnings ----------

def parse_when(text: str) -> date | None:
    text = text.strip().rstrip(".")
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+),?\s+(\d{4})", text)  # 2 November, 2026
    if m and m.group(2).lower() in MONTHS:
        return date(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)))
    m = re.search(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", text)  # February 19, 2025
    if m and m.group(1).lower() in MONTHS:
        return date(int(m.group(3)), MONTHS[m.group(1).lower()], int(m.group(2)))
    return None


def parse_release_titles(titles: list[str]) -> tuple[dict, set]:
    """-> ({'Q4 2026': date}, {'Q3 2026', ...reported})"""
    announced, reported = {}, set()
    for t in titles:
        m = ANNOUNCE_RE.search(t)
        if m:
            when = parse_when(m.group(3))
            if when:
                announced[f"Q{QUARTER_WORD[m.group(1).title()]} {m.group(2)}"] = when
            continue
        m = REPORTS_RE.search(t)
        if m:
            reported.add(f"Q{QUARTER_WORD[m.group(1).title()]} {m.group(2)}")
    return announced, reported


def fetch_ir_titles(cfg: dict) -> list[str] | None:
    ec = cfg["earnings"]
    try:
        root = ET.fromstring(http_get(ec["ir_rss"]))
        titles = [(it.findtext("title") or "").strip() for it in root.iter("item")]
        if titles:
            return titles
    except Exception as exc:  # noqa: BLE001
        print(f"ir rss fail: {exc}", file=sys.stderr)
    try:
        data = json.loads(http_get(ec["ir_json"]))
        return [r.get("Headline", "") for r in data.get("GetPressReleaseListResult", [])]
    except Exception as exc:  # noqa: BLE001
        print(f"ir json fail: {exc}", file=sys.stderr)
    return None


def qkey_sort(q: str) -> tuple[int, int]:
    n, y = q.split()
    return int(y), int(n[1])


def earnings_plan(cfg: dict, earn_state: dict, announced: dict, reported: set, today: date) -> list[tuple[str, str, str, date, bool]]:
    """Return a list of (quarter, kind, label, date, official) to send. kind: confirm | remind."""
    ec = cfg["earnings"]
    out = []
    est = ec.get("next_estimate") or {}
    cands = dict(announced)
    official = set(announced)
    if est.get("quarter") and est["quarter"] not in cands and est["quarter"] not in reported:
        cands[est["quarter"]] = date.fromisoformat(est["ny_date"])
    last_rep = (ec.get("last_reported") or {}).get("quarter")
    for q in sorted(cands, key=qkey_sort):
        if q in reported or (last_rep and qkey_sort(q) <= qkey_sort(last_rep)):
            continue
        when = cands[q]
        if when < today:
            continue
        st = earn_state.get(q) or {}
        is_off = q in official
        if is_off and st.get("confirmed") != when.isoformat():
            out.append((q, "confirm", "", when, True))
        days = (when - today).days
        due = [d for d in sorted(ec.get("remind_days", [7, 1])) if days <= d]
        if not is_off:
            due = [d for d in due if d >= 7]  # estimate: only the early heads-up
        if due:
            d = due[0]
            tag = f"d{d}{'' if is_off else 'e'}"
            sent = st.get("reminded") or []
            if tag not in sent and not (not is_off and any(s.startswith(f"d{d}") for s in sent)):
                out.append((q, "remind", tag, when, is_off))
    return out


def mark_earn(earn_state: dict, item: tuple, remind_days: list[int]) -> None:
    q, kind, tag, when, off = item
    st = earn_state.setdefault(q, {})
    if kind == "confirm":
        st["confirmed"] = when.isoformat()
        return
    sent = set(st.get("reminded") or [])
    d = int(tag[1:].rstrip("e"))
    for x in remind_days:  # a closer reminder also covers the farther ones
        if x >= d:
            sent.add(f"d{x}{'' if off else 'e'}")
    st["reminded"] = sorted(sent)


def earnings_message(item: tuple, stamp: str, price: float | None) -> tuple[str, str]:
    q, kind, tag, when, off = item
    kst_hint = (when + timedelta(days=1)).strftime("%m/%d")
    px = f" 현재가 {price:.2f}달러." if price else ""
    if kind == "confirm":
        title = f"[GRAB-PX] {q} 실적 발표일 확정 {when.isoformat()} | 일정 메모"
        status = f"그랩이 {q} 실적 발표일을 미국 {when.isoformat()} 장 마감 후로 공지했습니다(한국 {kst_hint} 아침 무렵).{px}"
        todo = "일정만 메모하세요. 실적 전 몰아서 사지 말고, 매수 구간 계획대로."
        later = "발표 7일 전과 1일 전에 다시 알립니다."
    else:
        d = int(tag[1:].rstrip("e"))
        left = (d if d > 0 else 0)
        if off:
            title = f"[GRAB-PX] {q} 실적 D-{left} ({when.isoformat()}) | 일정 확인"
            status = f"{q} 실적 발표가 미국 {when.isoformat()} 장 마감 후입니다(한국 {kst_hint} 아침 무렵). 조건: 발표 {d}일 전 안내.{px}"
        else:
            title = f"[GRAB-PX] {q} 실적 예상 시기 접근 {when.isoformat()} 전후 | 공식 일정 대기"
            status = f"{q} 실적이 {when.isoformat()} 전후로 예상됩니다(추정, 공식 공지 전). 조건: 예상일 {d}일 전 안내.{px}"
        todo = "실적 전 비중을 늘리지 말고, 인도네시아 수수료 규제 영향과 가이던스를 볼 준비만."
        later = "공식 발표일이 공지되면 확정 알림과 1일 전 알림이 옵니다." if not off else "발표 뒤 가격선 알림은 그대로 이어집니다."
    lines = [
        "이게 뭐 알림: 그랩 홀딩스(GRAB) 분기 실적 일정 알림입니다. 투자 권유 아님.",
        f"지금 결과/상태: {status} 시각 {stamp}.",
        f"지금 할 일: {todo}",
        f"나중에: {later}",
        TP,
        "하지 말 것: 실적 직전 몰빵, 신용, 시장가 추격.",
    ]
    return title, append_ends("\n".join(lines), "grabpx")


# ---------- run ----------

def _emit(title: str, body: str, priority: int, topic: str, dry: bool, tags: list[str]) -> bool:
    if any(ch in title or ch in body for ch in FORBIDDEN):
        print(f"refusing send: forbidden punctuation in copy: {title}", file=sys.stderr)
        return False
    if dry:
        print(f"DRY RUN would send (p{priority}) to {topic}: {title}\n{body}\n")
        return True
    print(f"send p{priority}: {title}")
    publish(topic, title, body, priority, tags=tags, config_name="grabpx")
    return True


def run_events(cfg: dict, state: dict, now_ny: datetime, stamp: str, price: float | None, dry: bool, mode: str) -> list[str]:
    problems = []
    today = now_ny.date()
    # earnings (Komdigi / Perpres delivery-cap news stays in the existing [GRAB] morning alert, not here)
    titles = fetch_ir_titles(cfg)
    if titles is None:
        problems.append("IR 보도자료")
    else:
        announced, reported = parse_release_titles(titles)
        print(f"ir announced={ {k: v.isoformat() for k, v in announced.items()} } reported={sorted(reported)}")
        for item in earnings_plan(cfg, state["earn"], announced, reported, today):
            title, body = earnings_message(item, stamp, price)
            if _emit(title, body, int(cfg["earnings"].get("priority", 3)), resolve_topic("port"), dry, ["calendar"]) and not dry:
                mark_earn(state["earn"], item, cfg["earnings"].get("remind_days", [7, 1]))
    return problems


def ops_events_message(problems: list[str], stamp: str) -> tuple[str, str]:
    what = ", ".join(problems)
    title = "[GRAB-PX] 실적 일정 조회 실패 | 매매 신호 아님"
    lines = [
        f"이게 뭐 알림: 그랩 IR 보도자료(실적 일정)를 읽지 못했습니다({what}). 매매 신호가 아닙니다.",
        f"지금 결과/상태: 조회 실패. 시각 {stamp}.",
        "지금 할 일: 없음. 가격선 알림은 그대로 돕니다.",
        "나중에: 다음 미국 거래일 첫 실행에서 다시 읽습니다.",
        "익절/줄일 때: 해당 없음.",
        "하지 말 것: 조회 실패를 신호로 읽지 않기.",
    ]
    return title, append_ends("\n".join(lines), "grabpx")


def run(mode: str, price_arg: float | None, now_ny: datetime | None = None) -> int:
    dry = mode in {"dry", "test", "dry-run"}
    cfg = load_cfg()
    now_ny = now_ny or datetime.now(NY)
    stamp = now_ny.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")
    day = now_ny.date().isoformat()
    print(f"{stamp} ny={now_ny:%Y-%m-%d %H:%M %Z} mode={mode} day={day}")
    session = in_regular_session(now_ny)
    if price_arg is None and not session and not dry:
        print("outside NY regular session; silent")
        return 0
    if not session:
        print("outside NY regular session; dry continues for the check only")

    state = load_state()
    if price_arg is None:
        price, market_state, ylow = fetch_last(cfg["ticker"])
    else:
        price, market_state, ylow = price_arg, "REGULAR", None
    print(f"price={price} marketState={market_state} yahoo52wLow={ylow}")

    if price is None:
        if state.get("fail_day") != day or mode == "force" or dry:
            title, body = fail_message(stamp)
            _emit(title, body, 3, resolve_topic("ops"), dry, ["warning"])
            if not dry:
                state["fail_day"] = day
        else:
            print("fetch failed; ops notice already sent today")
    elif market_state in OFF_SESSION and price_arg is None and not dry:
        print(f"marketState {market_state}; not a regular last price; price lines skipped")
    else:
        armed = state["price"]["armed"]
        key, new_armed = evaluate(cfg["levels"], armed, price)
        if mode == "force" and key is None:
            fired = [k for k in DEPTH if hit(cfg["levels"][k], price)]
            key = fired[-1] if fired else None
        print(f"armed={armed} -> {new_armed} fire={key}")
        sent_ok = True
        if key:
            title, body, pri = price_message(cfg, key, price, stamp, ylow)
            sent_ok = _emit(title, body, pri, resolve_topic("port"), dry, ["chart_with_downwards_trend"])
        else:
            print("no new price line; silent")
        if not dry and sent_ok:
            state["price"]["armed"] = new_armed
            state["price"]["last_price"] = price
            state["price"]["last_at"] = stamp

    if state.get("events_day") != day or dry or mode == "force":
        problems = run_events(cfg, state, now_ny, stamp, price, dry, mode)
        if problems:
            title, body = ops_events_message(problems, stamp)
            _emit(title, body, 2, resolve_topic("ops"), dry, ["warning"])
        if not dry:
            state["events_day"] = day
    else:
        print("events already checked this NY day")

    save_state(state, dry)
    if dry:
        print("DRY RUN: no ntfy, no state write")
    return 0


# ---------- selftest ----------

def selftest() -> int:
    cfg = load_cfg()
    lv = cfg["levels"]
    fails = 0
    stamp = "2026-11-03 23:30 KST"

    def check(label: str, got, want) -> None:
        nonlocal fails
        ok = got == want
        fails += not ok
        print(("ok  " if ok else "FAIL"), label, got, "" if ok else f"want {want}")

    allarm = {k: True for k in DEPTH}
    check("3.10 none", evaluate(lv, allarm, 3.10)[0], None)
    check("2.95 zone edge", evaluate(lv, allarm, 2.95)[0], "zone")
    check("2.85 zone edge", evaluate(lv, allarm, 2.85)[0], "zone")
    check("2.90 zone", evaluate(lv, allarm, 2.90)[0], "zone")
    check("2.80 none", evaluate(lv, allarm, 2.80)[0], None)
    check("2.73 not below", evaluate(lv, allarm, 2.73)[0], None)
    check("2.72 bear", evaluate(lv, allarm, 2.72)[0], "bear")
    check("2.67 not below low", evaluate(lv, allarm, 2.67)[0], "bear")
    k, a = evaluate(lv, allarm, 2.60)
    check("2.60 deepest low52", k, "low52")
    check("2.60 disarms bear too", a["bear"], False)

    # once per crossing
    k, a = evaluate(lv, allarm, 2.90)
    check("zone fires", k, "zone")
    check("zone again silent", evaluate(lv, a, 2.88)[0], None)
    check("2.83 still inside hysteresis", evaluate(lv, a, 2.83)[1]["zone"], False)
    k2, a2 = evaluate(lv, a, 2.99)
    check("2.99 rearms zone", a2["zone"], True)
    check("re-entry fires", evaluate(lv, a2, 2.94)[0], "zone")
    k, a = evaluate(lv, allarm, 2.70)
    check("bear fires", k, "bear")
    check("bear stays quiet", evaluate(lv, a, 2.71)[0], None)
    check("2.75 not rearmed", evaluate(lv, a, 2.75)[1]["bear"], False)
    check("2.76 rearms bear", evaluate(lv, a, 2.76)[1]["bear"], True)
    k, a = evaluate(lv, allarm, 2.66)
    check("low52 fires", k, "low52")
    check("2.69 low52 not rearmed", evaluate(lv, a, 2.69)[1]["low52"], False)
    check("2.70 low52 rearmed", evaluate(lv, a, 2.70)[1]["low52"], True)

    for key, px in (("zone", 2.90), ("bear", 2.70), ("low52", 2.60)):
        title, body, pri = price_message(cfg, key, px, stamp, 2.67)
        check(f"{key} title GRAB", "GRAB" in title, True)
        check(f"{key} price in body", f"{px:.2f}달러" in body, True)
        check(f"{key} condition in body", "조건:" in body, True)
        check(f"{key} no forbidden", any(ch in title + body for ch in FORBIDDEN), False)
        order = ["이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것", "꺼지는 때"]
        idx = [body.find(x) for x in order]
        check(f"{key} body order", idx, sorted(idx))
    check("low52 priority", price_message(cfg, "low52", 2.6, stamp, None)[2], 5)

    # earnings parsing
    titles = [
        "Grab Reports Third Quarter 2026 Results, Raises Full-Year Guidance",
        "Grab to Announce Third Quarter 2026 Results on 2 November, 2026",
        "Grab to Announce Fourth Quarter 2024 Results on February 19, 2025",
        "Grab Reports Fourth Quarter and 2025 Results with First Full Year Net Profit",
    ]
    ann, rep = parse_release_titles(titles)
    check("announce dmy", ann.get("Q3 2026"), date(2026, 11, 2))
    check("announce mdy", ann.get("Q4 2024"), date(2025, 2, 19))
    check("reported Q3 2026", "Q3 2026" in rep, True)
    check("reported Q4 2025", "Q4 2025" in rep, True)

    es: dict = {}
    check("estimate far: nothing", earnings_plan(cfg, es, {}, rep, date(2026, 11, 3)), [])
    plan = earnings_plan(cfg, es, {}, rep, date(2027, 2, 3))
    check("estimate D-7", [(p[0], p[1], p[2]) for p in plan], [("Q4 2026", "remind", "d7e")])
    for p in plan:
        mark_earn(es, p, [7, 1])
    check("estimate D-7 once", earnings_plan(cfg, es, {}, rep, date(2027, 2, 4)), [])
    check("estimate no D-1", earnings_plan(cfg, es, {}, rep, date(2027, 2, 9)), [])
    off = {"Q4 2026": date(2027, 2, 11)}
    plan = earnings_plan(cfg, es, off, rep, date(2027, 1, 14))
    check("official confirm", [(p[1]) for p in plan], ["confirm"])
    for p in plan:
        mark_earn(es, p, [7, 1])
    check("confirm once", earnings_plan(cfg, es, off, rep, date(2027, 1, 15)), [])
    plan = earnings_plan(cfg, es, off, rep, date(2027, 2, 4))
    check("official D-7", [p[2] for p in plan], ["d7"])
    for p in plan:
        mark_earn(es, p, [7, 1])
    plan = earnings_plan(cfg, es, off, rep, date(2027, 2, 10))
    check("official D-1", [p[2] for p in plan], ["d1"])
    for p in plan:
        mark_earn(es, p, [7, 1])
    check("D-1 once", earnings_plan(cfg, es, off, rep, date(2027, 2, 10)), [])
    check("after reported silent", earnings_plan(cfg, {}, off, rep | {"Q4 2026"}, date(2027, 2, 10)), [])
    late = earnings_plan(cfg, {}, off, rep, date(2027, 2, 10))
    check("late start sends only D-1", [p[2] for p in late if p[1] == "remind"], ["d1"])
    st2: dict = {}
    for p in late:
        mark_earn(st2, p, [7, 1])
    check("D-1 covers D-7", st2["Q4 2026"]["reminded"], ["d1", "d7"])
    for item in [("Q4 2026", "confirm", "", date(2027, 2, 11), True),
                 ("Q4 2026", "remind", "d1", date(2027, 2, 11), True),
                 ("Q4 2026", "remind", "d7e", date(2027, 2, 10), False)]:
        t, b = earnings_message(item, stamp, 2.9)
        check(f"earn {item[1]}{item[2]} GRAB", "GRAB" in t, True)
        check(f"earn {item[1]}{item[2]} no forbidden", any(ch in t + b for ch in FORBIDDEN), False)
        check(f"earn {item[1]}{item[2]} price", "2.90달러" in b, True)

    t, b = fail_message(stamp)
    check("fail title GRAB", "GRAB" in t, True)
    check("fail routes ops", "조회 실패" in t, True)

    def walk(obj) -> None:
        nonlocal fails
        if isinstance(obj, str) and any(ch in obj for ch in FORBIDDEN):
            fails += 1
            print("FAIL config punctuation", obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(cfg)
    for k in DEPTH:
        check(f"config title {k} GRAB", "GRAB" in lv[k]["title"], True)

    edt_open = datetime(2026, 10, 30, 13, 30, tzinfo=timezone.utc).astimezone(NY)
    est_open = datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc).astimezone(NY)
    check("EDT open in", in_regular_session(edt_open), True)
    check("EST open in", in_regular_session(est_open), True)
    check("EST 13:30 UTC out", in_regular_session(datetime(2026, 11, 2, 13, 30, tzinfo=timezone.utc).astimezone(NY)), False)
    check("saturday out", in_regular_session(edt_open.replace(day=31)), False)

    print(f"selftest fails={fails}")
    return 1 if fails else 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = (argv[0] if argv else "auto").lower()
    if mode in {"selftest", "self-test"}:
        return selftest()
    if mode not in {"auto", "force", "dry", "test", "dry-run"}:
        print(f"unknown mode {mode}", file=sys.stderr)
        return 2
    price_arg = None
    rest = argv[1:]
    if rest:
        raw = rest[1] if rest[0] == "--price" and len(rest) > 1 else rest[0]
        try:
            price_arg = float(raw)
        except ValueError:
            print(f"bad price {raw!r}", file=sys.stderr)
            return 2
    if mode in {"dry", "test", "dry-run"}:
        print("DRY RUN")
    return run(mode, price_arg)


if __name__ == "__main__":
    raise SystemExit(main())
