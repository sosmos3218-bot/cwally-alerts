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
  earnings  on configs earnings.date (KST session day), one 실적 체크 message, flag earn_done[date];
            a late slot catches up within catch_up_days. Filing detection: Naver stock notice feed
            (KRX/DART disclosure mirror, no key) for a 영업(잠정)실적 title on/after the day before;
            not found or feed failed -> "공시 확인 필요".
Data: Yahoo chart <code>.KS daily closes, Naver daily price rows as fallback (476760 pattern). No new secret.

Modes: auto (slot) | force (ignore holiday) | dry (print, no ntfy, no state) | selftest (offline checks) |
       install (one install-confirmation send to cwally-port, no state) | seed (write state from live data, no ntfy)
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
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts krplan"}
FORBIDDEN = ("\u00b7", "\u2014", "\u318d", "\u30fb", "\u2027")  # middle dots, em-dash: never in message text


def scrub(text: str) -> str:
    """Outside text (filing titles) may carry a middle dot; replace it before it reaches a message."""
    for ch in FORBIDDEN:
        text = text.replace(ch, ", " if ch != "\u2014" else " ")
    return text
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


def yahoo_rows(cfg: dict, symbol: str) -> list[dict]:
    res = get_json(cfg["sources"]["yahoo"].format(symbol=urllib.parse.quote(symbol)))["chart"]["result"][0]
    ts = res.get("timestamp") or []
    closes = res["indicators"]["quote"][0]["close"]
    rows = {}
    for t, c in zip(ts, closes):
        if c is not None:
            rows[datetime.fromtimestamp(t, KST).date().isoformat()] = float(c)
    meta = res.get("meta") or {}
    if meta.get("regularMarketPrice") is not None and meta.get("regularMarketTime"):
        d = datetime.fromtimestamp(meta["regularMarketTime"], KST).date().isoformat()
        if d in rows or not rows or d > max(rows):
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


def collect(cfg: dict) -> tuple[dict, list[str]]:
    data: dict = {"src": None}
    fails: list[str] = []
    try:
        data["rows"], data["src"] = yahoo_rows(cfg, cfg["yahoo_symbol"]), "Yahoo"
    except Exception as exc:
        try:
            data["rows"], data["src"] = naver_rows(cfg), "네이버"
            print(f"yahoo failed ({exc}); using naver")
        except Exception as exc2:
            fails.append(f"{cfg['code']} 시세 (Yahoo {exc}, 네이버 {exc2})")
    if cfg.get("us10y"):
        try:
            data["us10y"] = tnx(cfg)
        except Exception as exc:
            fails.append(f"미국 10년물 ({exc})")
    return data, fails


# ---------- state ----------
def state_path(name: str) -> Path:
    return ROOT / "state" / f"{name}.json"


def default_state(cfg: dict) -> dict:
    st = {"updated_at": None, "seeded": False, "last_day": None, "last_close": None, "last_fail_day": None,
          "lines": {ln["key"]: {"armed": True, "fired_day": None} for ln in cfg["lines"]}, "earn_done": {}}
    if cfg.get("us10y"):
        st["us10y"] = {"armed": True, "fired_day": None, "last": None}
    return st


def load_state(cfg: dict) -> dict:
    st = default_state(cfg)
    p = state_path(cfg["name"])
    if p.exists():
        try:
            got = json.loads(p.read_text(encoding="utf-8"))
            for k, v in got.items():
                if k == "lines" and isinstance(v, dict):
                    st["lines"].update(v)
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
        st["last_day"], st["last_close"] = r["day"], r["close"]
    return events


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


def build_lines(cfg: dict, events: list[dict], data: dict, stamp: str) -> tuple[str, str, int]:
    events = sorted(events, key=lambda e: SEVERITY.index(e["key"]) if e["key"] in SEVERITY else 99)
    top = events[0]
    ln = top["line"]
    sign = {"le": "이하", "ge": "이상", "lt": "미만"}[ln["op"]]
    title = f"{cfg['title_prefix']} 종가 {fmt(ln['level'])} {sign} | {ln['title']}"
    pri = 5 if top["key"] == "stop" else 4
    hits = [f"{e['line']['label']}: 종가 {fmt(e['close'])}({e['day']}), 기준 {fmt(e['line']['level'])}, 기준 대비 {dist(e['close'], e['line']['level']):+.2f}%."
            for e in events]
    last_close = (data.get("rows") or [{}])[-1].get("close") if data.get("rows") else events[-1]["close"]
    if top["key"] == "stop":
        later = "손절 뒤에는 이 계획의 매수선을 다시 쓰지 말고 계획을 새로 세우세요."
    elif top["key"].startswith("trim"):
        later = "남은 수량은 다음 축소선까지 보유. 축소선은 종가가 1% 넘게 아래로 내려왔다 와야 다시 울립니다."
    else:
        later = "다음 매수선까지 기다리세요. 같은 선은 종가가 1% 넘게 위로 올라갔다 와야 다시 울립니다."
    msg = "\n".join([
        head(cfg, stamp),
        f"이게 뭐 알림: {cfg['short']} 종가가 계획한 매수, 축소, 손절 선을 새로 넘을 때만 오는 알림입니다. 매수 지시가 아닙니다.",
        "지금 결과/상태: " + " ".join(hits) + f" {ladder(cfg, last_close)} (시세: {data.get('src') or '-'})",
        "지금 할 일: " + " ".join(e["line"]["action"] for e in events),
        f"나중에: {later}",
        f"익절/줄일 때: {trim_text(cfg)} 비중 축소. 미국 10년물 5.30% 이상이면 분할 매수 중단.",
        "하지 말 것: 한 번에 전액 매수, 신용 및 미수, 손절선 이탈 뒤 물타기, 조회 실패를 신호로 읽기.",
    ])
    return title, msg, pri


def build_us10y(cfg: dict, ev: dict, stamp: str) -> tuple[str, str, int]:
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
    ])
    return title, msg, 4


def build_earnings(cfg: dict, d: str, filing: dict | None, ffail: str | None, stamp: str) -> tuple[str, str, int]:
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
    ])
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


def clean(title: str, msg: str) -> None:
    for ch in FORBIDDEN:
        if ch in title or ch in msg:
            raise ValueError(f"forbidden char U+{ord(ch):04X} in message: {title}")


# ---------- run ----------
def load_cfg(name: str) -> dict:
    return json.loads((ROOT / "configs" / f"{name}.json").read_text(encoding="utf-8"))


def plan_sends(cfg: dict, st: dict, data: dict, fails: list[str], now: datetime, filing_fn=None) -> list[tuple]:
    """Mutates st; returns [(title, msg, pri, tags)]."""
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    today = session_day(now).isoformat()
    sends = []
    d = earnings_due(cfg, st, today)
    if d:
        filing, ffail = None, None
        try:
            filing = (filing_fn or find_filing)(cfg, d)
        except Exception as exc:
            ffail = str(exc)[:80]
        sends.append(build_earnings(cfg, d, filing, ffail, stamp) + (["clipboard"],))
        st.setdefault("earn_done", {})[d] = stamp
    ev = eval_lines(cfg, st, data.get("rows") or [], today, now)
    if ev:
        sends.append(build_lines(cfg, ev, data, stamp) + (["chart_with_downwards_trend"],))
    for e in eval_us10y(cfg, st, data.get("us10y"), today):
        sends.append(build_us10y(cfg, e, stamp) + (["warning"],))
    if fails and st.get("last_fail_day") != today:
        sends.append(build_fail(cfg, fails, stamp) + (["warning"],))
        st["last_fail_day"] = today
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
        y = " 미국 10년물 5.30% 공통 경고는 [005930] 알림이 한 번만 보냅니다."
    armed = [k for k, v in st["lines"].items() if v.get("armed", True)]
    title = f"{cfg['title_prefix']} 설치 확인 | 매매 신호 아님"
    msg = "\n".join([
        head(cfg, stamp),
        f"이게 뭐 알림: {cfg['short']} 종가 기준 계획 알림 설치 확인입니다. KRX 거래일 19:00에 선을 새로 넘을 때만 옵니다.",
        f"지금 결과/상태: {px}{y}" + (" 조회 실패: " + ", ".join(fails) + "." if fails else "") + f" 무장된 선: {', '.join(armed)}.",
        f"지금 할 일: 없음. 선: {lines}. 선마다 1회, 1% 넘게 반대로 돌아와야 재무장.",
        f"나중에: {cfg['earnings']['date']} 19:00에 실적 체크 알림 1회(2027년 HBM LTA, CAPEX와 판가, 중국 YMTC/CXMT와 ASP).",
        f"익절/줄일 때: {trim_text(cfg)} 비중 축소.",
        "하지 말 것: 이 설치 확인을 매수나 매도 신호로 읽기.",
    ])
    clean(title, msg)
    return title, msg


def run(name: str, argv: list[str]) -> int:
    mode = (argv[1] if len(argv) > 1 else "auto").lower()
    cfg = load_cfg(name)
    if mode == "selftest":
        return selftest(name)
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
        data, fails = collect(cfg)
        title, msg = install_msg(cfg, st, data, fails, stamp)
        print(title)
        print(msg)
        publish(topic, title, msg, 3, ["white_check_mark"], server)
        return 0

    if not is_trading_day(day) and mode not in {"force", "dry", "seed"}:
        print(f"{day} KRX holiday; silent")
        return 0

    data, fails = collect(cfg)
    print(f"data src={data.get('src')} rows={(data.get('rows') or [])[-3:]} us10y={data.get('us10y')} fails={fails}")

    if mode == "seed" or not st.get("seeded"):
        tmp = default_state(cfg)
        tmp["last_day"] = None
        ev = eval_lines(cfg, tmp, data.get("rows") or [], day.isoformat(), now)
        y = eval_us10y(cfg, tmp, data.get("us10y"), day.isoformat())
        st.update({k: tmp[k] for k in ("lines", "last_day", "last_close")})
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
    cfg = load_cfg(name)
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

    lv = {ln["key"]: ln["level"] for ln in cfg["lines"]}
    st = default_state(cfg)
    st["last_day"] = "2026-11-01"
    st["earn_done"] = {cfg["earnings"]["date"]: "x"}
    days = iter(f"2026-11-{i:02d}" for i in range(2, 31))

    def feed(close):
        d = next(days)
        ev = eval_lines(cfg, st, [{"day": d, "close": close}], d, at(d))
        return sorted(e["key"] for e in ev)

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

    # same day twice / late double run: day already in last_day -> silent
    st2 = default_state(cfg)
    st2["last_day"] = "2026-10-23"
    r = [{"day": "2026-10-26", "close": b1 - 1}]
    check("first run fires", [e["key"] for e in eval_lines(cfg, st2, r, "2026-10-26", at("2026-10-26"))], ["buy1"])
    check("second run same day silent", eval_lines(cfg, st2, r, "2026-10-26", at("2026-10-26", 22)), [])
    st3 = default_state(cfg)
    st3["last_day"] = "2026-10-23"
    check("intraday row not judged", eval_lines(cfg, st3, r, "2026-10-26", at("2026-10-26", 14)), [])
    check("catch-up of missed days in order",
          [(e["day"], e["key"]) for e in eval_lines(cfg, st3, [{"day": "2026-10-26", "close": b1 - 1}, {"day": "2026-10-27", "close": b2 - 1}], "2026-10-27", at("2026-10-27"))],
          [("2026-10-26", "buy1"), ("2026-10-27", "buy2")])

    # SK hynix style: seed just above buy1, first close <= buy1 fires once
    st4 = copy.deepcopy(seed)
    check("first run close <= buy1 fires once",
          [e["key"] for e in eval_lines(cfg, st4, [{"day": "2026-10-26", "close": b1}], "2026-10-26", at("2026-10-26"))], ["buy1"])

    # us10y (005930 only)
    if cfg.get("us10y"):
        s = default_state(cfg)
        seq = [(5.25, 0), (5.30, 1), (5.35, 0), (5.21, 0), (5.19, 0), (5.31, 1), (5.31, 0)]
        got = [len(eval_us10y(cfg, s, y, f"2026-11-{i + 2:02d}")) for i, (y, _) in enumerate(seq)]
        check("us10y fire once + re-arm < 5.20", got, [w for _, w in seq])
        s = default_state(cfg)
        check("us10y same-day twice", [len(eval_us10y(cfg, s, 5.4, "2026-11-02")), len(eval_us10y(cfg, s, 5.4, "2026-11-02"))], [1, 0])
    else:
        check("000660 has no us10y", "us10y" in default_state(cfg), False)

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
    for k in SEVERITY:
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
    print("SAMPLE:", all_msgs[-4 if cfg.get("us10y") else -3][0])
    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(run("005930", sys.argv))
