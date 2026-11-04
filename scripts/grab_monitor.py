#!/usr/bin/env python3
"""[GRAB] Grab Holdings (NASDAQ: GRAB): price lines, earnings dates and Indonesia regulation news.

Merged 2026-11-03 from the old [GRAB] (09:00 KST daily bars 4.15 / 3.20 + Komdigi news) and
[GRAB-PX] (US-hours 2.85-2.95 / 2.73 / 2.67 + earnings dates). One config, one script, one state.

Cadence: attached to the existing tsla slot in port-alerts.yml (cron-job.org NY 30-min dispatch +
GitHub backup */30 13-21 * * 1-5 UTC); no new cron. Code gates to 09:30-16:00 America/New_York weekdays.
  price  : every in-session run, Yahoo regularMarketPrice (last regular-session price).
  events : first in-session run of each NY date: Grab IR press-release RSS (earnings date) and
           Google News RSS (Komdigi / Perpres delivery commission-cap rules).

Price lines alert on crossings only. Each line is armed when the price is on the far side of it;
it fires once when crossed and re-arms after the price goes back past it by rearm_pct.
A line the price is already beyond is seeded disarmed (state seeded from the current price),
so nothing fires just because the alert was installed.
  down : zone 2.85-2.95 (entry) p4 -> bear < 2.73 p4 -> low52 < 2.67 p5. Deepest only per run.
  up   : rec320 >= 3.20 (worst-case value recovered) p3 -> up415 >= 4.15 (fair value) p4. Highest only.

Modes: auto | force | dry [price] | selftest. dry never calls publish() and never writes state/.
Not investment advice.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import sys
import time
import traceback
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import append_ends, publish, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
NY = ZoneInfo("America/New_York")
CFG_PATH = ROOT / "configs" / "grab.json"
STATE_PATH = ROOT / "state" / "grab.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}
OPEN_MIN = 9 * 60 + 30
CLOSE_MIN = 16 * 60
OFF_SESSION = {"PRE", "POST", "PREPRE", "POSTPOST", "CLOSED"}
FORBIDDEN = ("\u00b7", "\u2014", "\u318d")  # middle dots, em dash; kept as escapes so copy stays free of them
DOWN = ["zone", "bear", "low52"]  # shallow -> deep
UP = ["rec320", "up415"]  # low -> high
ALL = DOWN + UP
MAX_SEEN = 600
TP = "익절/줄일 때: 이 알림은 매도 신호가 아닙니다. 익절 가격은 여기서 정하지 않습니다."
DONT = "하지 말 것: 전량 즉시 매수, 신용, 미수, 시장가 추격. 조회 실패를 매매 신호로 보지 말 것."

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


def load_state() -> dict:
    st: dict = {}
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                st = data
        except json.JSONDecodeError:
            pass
    st.setdefault("price", {}).setdefault("armed", {})
    if not isinstance(st.get("earn"), dict):
        st["earn"] = {}
    st.setdefault("news", {}).setdefault("seen", [])
    return st


def save_state(state: dict, dry: bool) -> None:
    if dry:
        return
    state["news"]["seen"] = list(state["news"].get("seen", []))[-MAX_SEEN:]
    state["updated_at"] = datetime.now(KST).isoformat(timespec="seconds")
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def http_get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def clean(text: str) -> str:
    """Strip forbidden characters from outside text (headlines)."""
    return (text or "").replace("\u00b7", "/").replace("\u318d", "/").replace("\u2014", "-").strip()


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
    kind = spec["kind"]
    if kind == "zone":
        return spec["low"] <= price <= spec["high"]
    if kind == "below":
        return price < spec["level"]
    return price >= spec["level"]  # above


def rearm_ok(spec: dict, price: float) -> bool:
    pct = float(spec.get("rearm_pct", 1.0)) / 100
    kind = spec["kind"]
    if kind == "zone":
        return price < spec["low"] * (1 - pct) or price > spec["high"] * (1 + pct)
    if kind == "below":
        return price >= spec["level"] * (1 + pct)
    return price < spec["level"] * (1 - pct)


def seed_armed(levels: dict, armed: dict, price: float) -> list[str]:
    """Fill missing lines from the current price: beyond the line -> disarmed (no install alert)."""
    added = []
    for k in ALL:
        if k not in armed:
            armed[k] = not hit(levels[k], price)
            added.append(k)
    return added


def evaluate(levels: dict, armed: dict, price: float) -> tuple[list[str], dict]:
    """Return (lines to send, new armed map). At most one down line (deepest) and one up line (highest)."""
    out = dict(armed)
    for key in ALL:
        if not out.get(key, True) and rearm_ok(levels[key], price):
            out[key] = True
    send = []
    for group in (DOWN, UP):
        fired = [k for k in group if out.get(k, True) and hit(levels[k], price)]
        if not fired:
            continue
        top = fired[-1]
        for k in group[: group.index(top) + 1]:
            if hit(levels[k], price):
                out[k] = False
        send.append(top)
    return send, out


def thesis_line(cfg: dict, price: float) -> str:
    th = cfg["thesis"]
    fv = (th["fair_value"] / price - 1) * 100
    wc = (price / th["worst_case"] - 1) * 100
    return f"적정가 {th['fair_value']:.2f}달러까지 {fv:+.1f}%, 최악 시나리오 {th['worst_case']:.2f}달러 대비 {wc:+.1f}%."


def price_message(cfg: dict, key: str, price: float, stamp: str, yahoo_low: float | None) -> tuple[str, str, int]:
    spec = cfg["levels"][key]
    kind = spec["kind"]
    if kind == "zone":
        cond = f"조건: 현재가 {spec['low']:.2f}~{spec['high']:.2f}달러 구간 진입"
    elif kind == "below":
        cond = f"조건: 현재가 {spec['level']:.2f}달러 아래로 내려감"
    else:
        cond = f"조건: 현재가 {spec['level']:.2f}달러 위로 올라감"
    low_txt = f" 야후 52주 저가 {yahoo_low:.2f}달러." if yahoo_low else ""
    lines = [
        f"이게 뭐 알림: {cfg['explain']}",
        f"지금 결과/상태: {spec['status']} 현재가 {price:.2f}달러. {cond}. {thesis_line(cfg, price)} 시각 {stamp}.{low_txt}",
        f"지금 할 일: {spec['todo']}",
        f"나중에: {spec['later']} 같은 선은 지날 때 한 번만 옵니다.",
        f"익절/줄일 때: {spec.get('trim', '이 알림은 매도 신호가 아닙니다.')}",
        DONT,
    ]
    return spec["title"], append_ends("\n".join(lines), "grab"), int(spec["priority"])


def fail_message(stamp: str) -> tuple[str, str]:
    title = "[GRAB] 시세 조회 실패 | 매매 신호 아님"
    lines = [
        "이게 뭐 알림: 그랩(GRAB) 현재가를 읽지 못했습니다. 매매 신호가 아닙니다.",
        f"지금 결과/상태: 시세 조회 실패. 현재가 없음. 시각 {stamp}.",
        "지금 할 일: 증권앱에서 GRAB 현재가를 직접 확인하세요.",
        "나중에: 다음 미국 정규장 30분 실행에서 다시 읽습니다. 실패 안내는 하루 한 번입니다.",
        "익절/줄일 때: 이번 안내는 매매 신호가 아닙니다.",
        "하지 말 것: 조회 실패를 매수나 매도 신호로 보지 말 것.",
    ]
    return title, append_ends("\n".join(lines), "grab")


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
        title = f"[GRAB] {q} 실적 발표일 확정 {when.isoformat()} | 일정 메모"
        status = f"그랩이 {q} 실적 발표일을 미국 {when.isoformat()} 장 마감 후로 공지했습니다(한국 {kst_hint} 아침 무렵).{px}"
        todo = "일정만 메모하세요. 실적 전 몰아서 사지 말고, 매수 구간 계획대로."
        later = "발표 7일 전과 1일 전에 다시 알립니다."
    else:
        d = int(tag[1:].rstrip("e"))
        left = (d if d > 0 else 0)
        if off:
            title = f"[GRAB] {q} 실적 D-{left} ({when.isoformat()}) | 일정 확인"
            status = f"{q} 실적 발표가 미국 {when.isoformat()} 장 마감 후입니다(한국 {kst_hint} 아침 무렵). 조건: 발표 {d}일 전 안내.{px}"
        else:
            title = f"[GRAB] {q} 실적 예상 시기 접근 {when.isoformat()} 전후 | 공식 일정 대기"
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
    return title, append_ends("\n".join(lines), "grab")



# ---------- news (Komdigi / Perpres delivery commission cap) ----------

def norm_title(t: str) -> str:
    t = re.sub(r"\s+-\s+[^-]{2,60}$", "", t.strip())  # ' - source' tail
    return re.sub(r"[^0-9a-z가-힣]+", "", t.lower())


def news_key(title: str) -> str:
    return hashlib.sha1(norm_title(title).encode()).hexdigest()[:16]


def classify_news(cfg: dict, title: str) -> int | None:
    """None = ignore, 4 = delivery commission cap, 3 = follow-up rule / related."""
    n = cfg["news"]
    if not (re.search(n["rule_re"], title, re.I) and re.search(n["scope_re"], title, re.I)):
        return None
    return 4 if re.search(n["delivery_re"], title, re.I) and re.search(n["cap_re"], title, re.I) else 3


def parse_rss(rss: str) -> list[dict]:
    out = []
    for item in re.findall(r"(?s)<item>(.*?)</item>", rss):
        t = re.search(r"(?s)<title>(.*?)</title>", item)
        lk = re.search(r"(?s)<link>(.*?)</link>", item)
        pd = re.search(r"(?s)<pubDate>(.*?)</pubDate>", item)
        if not t:
            continue
        title = clean(html.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", t.group(1))).strip())
        try:
            when = parsedate_to_datetime(pd.group(1)).astimezone(KST) if pd else None
        except Exception:  # noqa: BLE001
            when = None
        out.append({"title": title, "link": html.unescape(lk.group(1).strip()) if lk else "", "when": when})
    return out


def fetch_news(cfg: dict) -> list[dict]:
    items, errs = [], []
    for q in cfg["news"]["queries"]:
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
            {"q": q["q"] + " when:7d", "hl": q["hl"], "gl": q["gl"], "ceid": q["ceid"]})
        try:
            items += parse_rss(http_get(url))
        except Exception as exc:  # noqa: BLE001
            errs.append(f"{q['q']}: {exc.__class__.__name__}")
    if errs and len(errs) == len(cfg["news"]["queries"]):
        raise RuntimeError("; ".join(errs)[:200])
    return items


def pick_news(cfg: dict, items: list[dict], seen: set[str], now: datetime) -> list[dict]:
    cutoff = now - timedelta(hours=int(cfg["news"]["lookback_hours"]))
    out, keys = [], set()
    for it in items:
        if it["when"] is None or it["when"] < cutoff or it["when"] > now + timedelta(hours=1):
            continue
        pr = classify_news(cfg, it["title"])
        if pr is None:
            continue
        k = news_key(it["title"])
        if k in seen or k in keys or (it["link"] and it["link"] in seen):
            continue
        keys.add(k)
        out.append({**it, "prio": pr, "key": k})
    out.sort(key=lambda x: (-x["prio"], -x["when"].timestamp()))
    return out


def news_message(cfg: dict, picks: list[dict], stamp: str, price: float | None) -> tuple[str, str, int]:
    top = max(p["prio"] for p in picks)
    n = len(picks)
    if top >= 4:
        title = f"[GRAB] 인도네시아 배달 수수료 상한 뉴스 {n}건 | 규제 범위 확인"
    else:
        title = f"[GRAB] 인도네시아 ojol 후속 규정 뉴스 {n}건 | 참고"
    shown = picks[: int(cfg["news"]["max_items"])]
    more = f" 외 {n - len(shown)}건" if n > len(shown) else ""
    px = f" 현재가 {price:.2f}달러." if price else ""
    lines = [
        "이게 뭐 알림: 인도네시아 8% 수수료 상한(ojol)이 음식, 물품 배달로 넓어지는지(Komdigi, Perpres 후속 규정) 새 기사 알림. GRAB 최악 시나리오의 핵심 위험입니다.",
        f"지금 결과/상태: 조건: 규제 단어와 배달/후속 규정 단어가 함께 있는 새 제목 {n}건{more}.{px} 시각 {stamp}.",
        *[f"- {p['when']:%m/%d %H:%M} {p['title'][:120]} {p['link']}" for p in shown],
        "지금 할 일: " + ("기사 원문에서 배달(GrabFood, GoFood, 물품) 포함 여부, 시행 날짜, 확정인지 검토 단계인지 확인." if top >= 4
                       else "후속 규정 일정과 배달 포함 여부만 확인. 아직 매매 판단 근거 아님."),
        "나중에: 같은 기사(링크, 제목)는 다시 보내지 않습니다. 미국 거래일마다 첫 정규장 실행에서 한 번 봅니다.",
        "익절/줄일 때: 배달 확대가 확정되면 적정가 4.15달러 가정을 낮추고 비중 축소 검토.",
        "하지 말 것: 제목만 보고 주문, 검토/제안 단계 기사를 확정으로 읽기.",
    ]
    return title, append_ends("\n".join(lines), "grab"), top


def step_news(cfg: dict, state: dict, items: list[dict], now: datetime, stamp: str, price: float | None, dry: bool) -> list[tuple[str, str, int]]:
    ns = state["news"]
    seen = set(ns.get("seen") or [])
    picks = pick_news(cfg, items, seen, now)
    if not ns.get("seeded"):
        print(f"news baseline (no send): {len(picks)} matching")
        for p in picks[:5]:
            print("  -", p["title"][:120])
        if not dry:
            ns["seeded"] = stamp
            ns["seen"] = sorted(seen | {p["key"] for p in picks} | {p["link"] for p in picks if p["link"]})
        return []
    if not picks:
        print("news: no new matching item")
        return []
    if not dry:
        ns["seen"] = list(ns.get("seen") or []) + [p["key"] for p in picks] + [p["link"] for p in picks if p["link"]]
    return [news_message(cfg, picks, stamp, price)]




# ---------- FY Adjusted EBITDA rule (SEC EDGAR 6-K earnings exhibit) ----------

NUM = r"\(?-?[\d,]+(?:\.\d+)?\)?"


def html_to_text(raw: str) -> str:
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t).replace("\xa0", " ")
    return re.sub(r"\s+", " ", t)


def to_musd(num: str, unit: str) -> float:
    v = float(num.replace(",", ""))
    return v * 1000 if unit.lower().startswith("b") else v


def fmt_eok(musd: float) -> str:
    return f"{musd / 100:.2f}".rstrip("0").rstrip(".") + "억 달러"


def tnum(x: str) -> float:
    neg = x.startswith("(") or x.startswith("-")
    v = float(x.strip("()-").replace(",", ""))
    return -v if neg else v


def is_fy_release(text: str, fy: int) -> bool:
    """Q4 + full-year <fy> results release only. A Q3 release (nine-month tables) never matches."""
    head = text[:3000]
    if re.search(r"Nine months ended", text, re.I):
        return False
    if not re.search(rf"Fourth Quarter(?: and (?:Full Year )?| )(?:Full Year )?{fy}|Q4 {fy}", head, re.I):
        return False
    return bool(re.search(rf"(?:For the )?year ended December 31, {fy}|full year {fy}|FY ?{fy}", text, re.I))


def parse_fy_adj_ebitda(text: str, fy: int) -> tuple[float | None, str]:
    """Full-year group Adjusted EBITDA in US$ million. (None, reason) when missing or ambiguous.

    Sources, both must agree when both exist:
      prose : 'Adjusted EBITDA for the full year (2027) was $930 million' and close variants
              ('Full year 2027 Adjusted EBITDA of $0.93 billion'). Segment/Total Segment lines are excluded.
      table : the reconciliation row 'Adjusted EBITDA q_cur q_prev fy_cur fy_prev Regional corporate costs',
              third number, only when the text has 'year ended December 31, <fy>'.
    """
    money = r"\$\s?([\d,]+(?:\.\d+)?)\s*(million|billion)"
    pats = [
        rf"(?<!Segment )Adjusted EBITDA for the full year(?: (?:of )?{fy})? (?:was|of|came in at|reached|totaled|totalled|grew [^$]{{0,80}}?to|increased [^$]{{0,80}}?to)(?: a record(?: of)?)? {money}",
        rf"(?<!Segment )Adjusted EBITDA for {fy} (?:was|of|came in at|reached|totaled|totalled) {money}",
        rf"(?:full year|FY) ?{fy}(?:,)? (?<!Segment )Adjusted EBITDA (?:was|of|came in at|reached|grew [^$]{{0,80}}?to|increased [^$]{{0,80}}?to)(?: a record(?: of)?)? {money}",
    ]
    prose = set()
    for pat in pats:
        for m in re.finditer(pat, text, re.I):
            pre = text[max(0, m.start() - 8): m.start()]
            if "segment" in pre.lower():
                continue
            prose.add(round(to_musd(m.group(1), m.group(2)), 1))
    table = set()
    if is_fy_release(text, fy) and re.search(rf"year ended December 31,(?: \d{{4}})* {fy}", text, re.I):
        for m in re.finditer(rf"(?<!Segment )Adjusted EBITDA ({NUM}) ({NUM}) ({NUM}) ({NUM}) Regional corporate costs", text):
            table.add(round(tnum(m.group(3)), 1))
    if len(prose) > 1:
        return None, f"본문 숫자가 여러 개 ({', '.join(f'{v:g}' for v in sorted(prose))}백만 달러)"
    if len(table) > 1:
        return None, f"표 숫자가 여러 개 ({', '.join(f'{v:g}' for v in sorted(table))}백만 달러)"
    p = next(iter(prose), None)
    t = next(iter(table), None)
    if p is not None and t is not None and abs(p - t) > 1:
        return None, f"본문 {p:g}백만 달러와 표 {t:g}백만 달러가 다름"
    if p is None and t is None:
        return None, "연간 조정 EBITDA 문장과 표를 못 찾음"
    src = "본문+표 일치" if (p is not None and t is not None) else ("본문" if p is not None else "표")
    return (p if p is not None else t), src


def parse_guidance(text: str, fy: int) -> tuple[float | None, str]:
    """Next-year Adjusted EBITDA guidance midpoint from the Business Outlook table, US$ million."""
    money = r"\$\s?([\d,]+(?:\.\d+)?)\s*(million|billion)?"
    found = set()
    for m in re.finditer(rf"(?<!Segment ){fy} Adjusted EBITDA {money}(?:\s*(?:-|to|\u2013)\s*{money})?", text, re.I):
        u1, u2 = m.group(2), m.group(4)
        if not (u1 or u2):
            continue
        lo = to_musd(m.group(1), u1 or u2)
        hi = to_musd(m.group(3), u2 or u1) if m.group(3) else lo
        found.add((round(lo, 1), round(hi, 1)))
    if not found:
        return None, f"{fy} 조정 EBITDA 가이던스 문구를 못 찾음"
    if len(found) > 1:
        return None, "가이던스 숫자가 여러 개"
    lo, hi = next(iter(found))
    return (lo + hi) / 2, f"{lo:g}~{hi:g}백만 달러"


def sec_find_release(cfg: dict, fy: int, since: date, checked: list) -> tuple[str | None, str | None]:
    """Newest Grab 6-K exhibit (filed on/after since) that is the Q4 / full-year <fy> earnings release."""
    sc = cfg["fy_rule"]["sec"]
    ua = {"User-Agent": sc["user_agent"]}

    def get(url: str) -> str:
        time.sleep(0.3)  # SEC fair-access pacing
        req = urllib.request.Request(url, headers=ua)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", "replace")

    sub = json.loads(get(f"https://data.sec.gov/submissions/CIK{int(sc['cik']):010d}.json"))
    r = sub["filings"]["recent"]
    found: list[tuple[str, str]] = []
    n = 0
    for i in range(len(r["form"])):
        if r["form"][i] != "6-K" or r["filingDate"][i] < since.isoformat():
            continue
        acc = r["accessionNumber"][i]
        if acc in checked or not re.search(r"earning", r["primaryDocument"][i], re.I):
            continue  # Grab names its results 6-K cover 'a6-kxearningsrelease*.htm'
        n += 1
        if n > 6:
            break
        base = f"https://www.sec.gov/Archives/edgar/data/{int(sc['cik'])}/{acc.replace('-', '')}/"
        items = json.loads(get(base + "index.json"))["directory"]["item"]
        hit_doc = None
        for it in items:
            name = it["name"]
            if not name.lower().endswith(".htm") or name == r["primaryDocument"][i] or "index" in name:
                continue
            text = html_to_text(get(base + name))
            if is_fy_release(text, fy):
                hit_doc = (base + name, text)
                break
        if hit_doc:
            found.append(hit_doc)
        else:
            checked.append(acc)
    if not found:
        return None, None
    # Grab files the release twice (plain 6-K and the F-3 incorporated one without the outlook); keep the fuller text
    return max(found, key=lambda x: len(x[1]))


def fy_messages(cfg: dict, fy: int, value: float | None, why: str, url: str, stamp: str, price: float | None) -> tuple[str, str, int, str]:
    th = float(cfg["fy_rule"]["threshold_musd"])
    px = f" 현재가 {price:.2f}달러." if price else ""
    thr = f"기준 {fmt_eok(th)}(US${th:g}백만)"
    if value is None:
        title = f"[GRAB] FY{fy} 조정 EBITDA 자동 판독 안 됨 | 원문에서 직접 확인"
        status = f"FY{fy} 실적 보도자료는 찾았지만 연간 조정 EBITDA 를 자동으로 읽지 못했습니다({why}). {thr}.{px} 원문: {url}"
        todo = f"원문에서 'Adjusted EBITDA for the full year' 숫자를 직접 보고, {fmt_eok(th)} 미만이면 정리 검토, 이상이면 보유 논리 유지."
        pri, kind = 4, "manual"
    elif value < th:
        title = f"[GRAB] FY{fy} 조정 EBITDA {fmt_eok(value)}, 기준 9.3억 달러 미만 | 정리 검토"
        status = f"FY{fy} 연간 조정 EBITDA {fmt_eok(value)}(US${value:g}백만, {why}). 조건: {thr} 미만. 보유 논리가 깨졌습니다.{px} 원문: {url}"
        todo = "보유분 정리를 검토하세요. 지정가로 나눠서, 다음 미국 정규장에서."
        pri, kind = 5, "below"
    else:
        title = f"[GRAB] FY{fy} 조정 EBITDA {fmt_eok(value)}, 기준 9.3억 달러 이상 | 보유 논리 유지"
        status = f"FY{fy} 연간 조정 EBITDA {fmt_eok(value)}(US${value:g}백만, {why}). 조건: {thr} 이상. 보유 논리 유지.{px} 원문: {url}"
        todo = "보유 유지. 새 연간 가이던스와 인도네시아 규제 영향만 확인."
        pri, kind = 3, "holds"
    lines = [
        f"이게 뭐 알림: 그랩(GRAB) FY{fy} 연간 실적 기준 알림입니다. 연간 조정 EBITDA 가 9.3억 달러 미만이면 정리 검토, 이상이면 보유 논리 유지. 한 번만 옵니다.",
        f"지금 결과/상태: {status} 시각 {stamp}.",
        f"지금 할 일: {todo}",
        "나중에: 이 판정은 다시 보내지 않습니다. 가격선 알림은 그대로 돕니다.",
        "익절/줄일 때: 정리 검토가 나오면 한 번에 다 팔기보다 나눠서.",
        "하지 말 것: 분기 숫자를 연간 숫자로 읽기, 원문 확인 없이 주문.",
    ]
    return title, append_ends("\n".join(lines), "grab"), pri, kind


def guidance_message(cfg: dict, fy: int, mid: float | None, why: str, url: str, stamp: str) -> tuple[str, str, int, str]:
    th = float(cfg["fy_rule"]["threshold_musd"])
    if mid is None:
        title = f"[GRAB] FY{fy} 조정 EBITDA 가이던스 자동 판독 안 됨 | 원문 참고"
        status = f"가이던스를 자동으로 읽지 못했습니다({why}). 원문: {url}"
        pri, kind = 3, "manual"
    elif mid < th:
        title = f"[GRAB] FY{fy} 조정 EBITDA 가이던스 중간값 {fmt_eok(mid)}, 9.3억 달러 미만 | 조기 경고"
        status = f"회사 가이던스 {why}, 중간값 {fmt_eok(mid)}. 조건: 중간값이 기준 {fmt_eok(th)} 미만. 원문: {url}"
        pri, kind = 4, "below"
    else:
        title = f"[GRAB] FY{fy} 조정 EBITDA 가이던스 중간값 {fmt_eok(mid)}, 9.3억 달러 이상 | 참고"
        status = f"회사 가이던스 {why}, 중간값 {fmt_eok(mid)}. 조건: 중간값이 기준 {fmt_eok(th)} 이상. 원문: {url}"
        pri, kind = 2, "holds"
    lines = [
        f"이게 뭐 알림: FY{fy} 연간 조정 EBITDA 9.3억 달러 기준의 조기 경고입니다. 회사가 낸 FY{fy} 가이던스만 봅니다. 한 번만 옵니다.",
        f"지금 결과/상태: {status} 시각 {stamp}.",
        f"지금 할 일: {'추가 매수를 멈추고 FY' + str(fy) + ' 실적 전까지 비중을 다시 점검.' if kind == 'below' else '할 일 없음. 최종 판정은 FY' + str(fy) + ' 실적 발표 때.'}",
        f"나중에: 최종 판정은 FY{fy} 실적(예상 {fy + 1}년 2월)에서 한 번 옵니다.",
        "익절/줄일 때: 가이던스만으로 전량 정리하지 않습니다.",
        "하지 말 것: 가이던스를 확정 실적으로 읽기.",
    ]
    return title, append_ends("\n".join(lines), "grab"), pri, kind


def run_fy_rule(cfg: dict, state: dict, today: date, stamp: str, price: float | None, dry: bool, reported: set) -> list[str]:
    fr = cfg.get("fy_rule") or {}
    if not fr.get("enabled"):
        return []
    fy = int(fr["fy"])
    fst = state.setdefault("fy_rule", {})
    checked = fst.setdefault("checked", [])
    problems = []
    jobs = []
    g = fr.get("guidance") or {}
    if g.get("enabled") and not fst.get(f"guid{fy}") and today >= date.fromisoformat(g["start_ny"]):
        jobs.append(("guid", fy - 1, date.fromisoformat(g["start_ny"])))
    if not fst.get(f"fy{fy}") and today >= date.fromisoformat(fr["start_ny"]):
        jobs.append(("fy", fy, date.fromisoformat(fr["start_ny"])))
    for kind, rel_fy, since in jobs:
        try:
            url, text = sec_find_release(cfg, rel_fy, since, checked if kind == "fy" else fst.setdefault("checked_g", []))
        except Exception as exc:  # noqa: BLE001
            print(f"sec fail: {exc}", file=sys.stderr)
            problems.append("SEC 6-K")
            continue
        qkey = f"Q4 {rel_fy}"
        if not url:
            print(f"fy rule {kind}: no Q4 {rel_fy} release on SEC yet")
            first = fst.get(f"ir_seen_{rel_fy}")
            if qkey in reported and not first:
                fst[f"ir_seen_{rel_fy}"] = today.isoformat()
            elif first and (today - date.fromisoformat(first)).days >= int(fr.get("sec_wait_days", 4)) and kind == "fy":
                t, b, p, k = fy_messages(cfg, fy, None, "IR 에는 발표됐지만 SEC 6-K 원문을 못 찾음", cfg["earnings"]["ir_rss"], stamp, price)
                if _emit(t, b, p, resolve_topic("port"), dry, ["warning"]) and not dry:
                    fst[f"fy{fy}"] = {"result": k, "at": stamp}
            continue
        if kind == "fy":
            val, why = parse_fy_adj_ebitda(text, fy)
            print(f"fy rule: {url} -> {val} ({why})")
            t, b, p, k = fy_messages(cfg, fy, val, why, url, stamp, price)
        else:
            mid, why = parse_guidance(text, fy)
            print(f"guidance: {url} -> {mid} ({why})")
            t, b, p, k = guidance_message(cfg, fy, mid, why, url, stamp)
        if _emit(t, b, p, resolve_topic("port"), dry, ["bar_chart"]) and not dry:
            fst[f"{kind}{fy}" if kind == "fy" else f"guid{fy}"] = {"result": k, "value": val if kind == "fy" else mid, "url": url, "at": stamp}
    return problems


# ---------- compound rule: 8% delivery commission cap (A) + guidance cut (B) ----------

def cap8_candidate(cfg: dict, title: str) -> bool:
    """News title that LOOKS like the 8% delivery cap became final. Never treated as confirmed by itself."""
    c = cfg["cap8"]
    return bool(re.search(c["pct_re"], title, re.I) and re.search(cfg["news"]["delivery_re"], title, re.I)
                and re.search(c["confirm_re"], title, re.I) and not re.search(c["proposal_re"], title, re.I))


def cap8_status(cfg: dict, cst: dict) -> tuple[str | None, str | None]:
    """(status, since) where status is confirmed / candidate / None. Manual flag in configs/grab.json wins."""
    man = cfg["cap8"].get("manual") or {}
    if man.get("status") == "rejected":
        return None, None
    if man.get("status") == "confirmed" and man.get("on"):
        return "confirmed", man["on"]
    a = cst.get("a") or {}
    if a.get("status") == "candidate":
        return "candidate", a.get("since")
    return None, None


def cap8_candidate_message(cfg: dict, hits: list[dict], stamp: str) -> tuple[str, str, int]:
    shown = hits[:5]
    lines = [
        "이게 뭐 알림: 배달 수수료 8% 상한이 확정(서명, 공포, 시행)됐다고 읽히는 기사 알림입니다. 기사 제목만으로는 확정과 검토를 확실히 구분할 수 없어 후보로만 기록합니다.",
        f"지금 결과/상태: 8%, 배달, 확정 단어가 함께 있고 검토/제안 단어가 없는 새 제목 {len(hits)}건. A 조건을 후보로 기록. 시각 {stamp}.",
        *[f"- {h['when']:%m/%d %H:%M} {h['title'][:120]} {h['link']}" for h in shown],
        "지금 할 일: 원문에서 규정 번호(Permen, Perpres), 서명/공포 날짜, 음식/물품 배달 포함 여부 확인. 확정이면 봇에게 '8% 상한 확정'이라고 말하거나 configs/grab.json 의 cap8.manual 을 status confirmed, on 날짜, source 링크로 바꿉니다. 오보면 status rejected.",
        "나중에: 이 후보 알림은 한 번만 옵니다. 이후 실적에서 가이던스가 내려가면 비중 절반 축소 검토 알림(p5)이 옵니다.",
        "익절/줄일 때: 후보 단계에서는 줄이지 않습니다. 확정과 가이던스 하향이 겹칠 때 절반 축소 검토.",
        "하지 말 것: 제목만 보고 확정으로 읽기, 이 알림만으로 매도.",
    ]
    return "[GRAB] 배달 수수료 8% 상한 확정 가능성 | 원문 확인 필요", append_ends("\n".join(lines), "grab"), 4


OUTLOOK_M = r"\$\s?([\d,]+(?:\.\d+)?)\s*(million|billion)?"


def parse_outlook(text: str) -> dict:
    """Business Outlook rows -> {'2026 Adjusted EBITDA': {'lo','hi','mid','prev': [lo, hi] or None}} in US$ million."""
    i = text.find("Business Outlook")
    if i < 0:
        return {}
    sect = text[i: i + 2500]
    j = sect.find("Segment Financial")
    sect = sect[:j] if j > 0 else sect
    rng = rf"{OUTLOOK_M}\s*(?:-|to|\u2013)\s*{OUTLOOK_M}"
    out = {}
    for m in re.finditer(rf"(?<!Segment )(20\d\d) (Group Revenue|Revenue|Adjusted EBITDA) {rng}(?:[^()$]{{0,80}}?\(Previous: {rng})?", sect):
        def pair(a: int) -> list[float] | None:
            if not m.group(a):
                return None
            u = m.group(a + 1) or m.group(a + 3)
            u2 = m.group(a + 3) or m.group(a + 1)
            if not u:
                return None
            return [round(to_musd(m.group(a), u), 1), round(to_musd(m.group(a + 2), u2), 1)]
        cur = pair(3)
        if not cur:
            continue
        metric = "Revenue" if "Revenue" in m.group(2) else "Adjusted EBITDA"
        out[f"{m.group(1)} {metric}"] = {"lo": cur[0], "hi": cur[1], "mid": sum(cur) / 2, "prev": pair(7)}
    return out


def outlook_cuts(new: dict, stored: dict) -> list[str]:
    """Rows whose midpoint is below the previous outlook (inline 'Previous:' first, else the stored one)."""
    cuts = []
    for k, v in new.items():
        ref = v.get("prev") or ([stored[k]["lo"], stored[k]["hi"]] if k in stored else None)
        if not ref:
            continue
        rmid = sum(ref) / 2
        if v["mid"] < rmid * 0.999:
            cuts.append(f"{k} {v['lo']:g}~{v['hi']:g}백만 달러 (이전 {ref[0]:g}~{ref[1]:g}백만 달러)")
    return cuts


def sec_earnings_releases(cfg: dict, after: str | None) -> list[tuple[str, str, str]]:
    """[(filingDate, url, text)] one per filing date (fullest copy), earnings 6-Ks filed after `after`, oldest first."""
    sc = cfg["fy_rule"]["sec"]
    ua = {"User-Agent": sc["user_agent"]}

    def get(url: str) -> str:
        time.sleep(0.3)
        with urllib.request.urlopen(urllib.request.Request(url, headers=ua), timeout=30) as resp:
            return resp.read().decode("utf-8", "replace")

    r = json.loads(get(f"https://data.sec.gov/submissions/CIK{int(sc['cik']):010d}.json"))["filings"]["recent"]
    best: dict[str, tuple[str, str]] = {}
    n = 0
    for i in range(len(r["form"])):
        fd = r["filingDate"][i]
        if r["form"][i] != "6-K" or not re.search(r"earning", r["primaryDocument"][i], re.I):
            continue
        if after and fd <= after:
            continue
        n += 1
        if n > 4 or (after is None and best and fd not in best):
            break  # seed run: newest release only
        base = f"https://www.sec.gov/Archives/edgar/data/{int(sc['cik'])}/{r['accessionNumber'][i].replace('-', '')}/"
        for it in json.loads(get(base + "index.json"))["directory"]["item"]:
            name = it["name"]
            if name.lower().endswith(".htm") and name != r["primaryDocument"][i] and "index" not in name:
                text = html_to_text(get(base + name))
                if len(text) > len(best.get(fd, ("", ""))[1]):
                    best[fd] = (base + name, text)
    return [(fd, u, t) for fd, (u, t) in sorted(best.items())]


def cap8_b_message(cfg: dict, kind: str, a_status: str | None, a_links: list[str], cuts: list[str], url: str, stamp: str, price: float | None) -> tuple[str, str, int]:
    px = f" 현재가 {price:.2f}달러." if price else ""
    ev = " ".join(a_links[:3])
    if kind == "manual":
        title = "[GRAB] 실적 가이던스 자동 판독 안 됨 | 원문에서 하향 여부 직접 확인"
        status = f"새 실적 보도자료에서 Business Outlook 가이던스를 읽지 못했습니다. 원문: {url}{px}"
        todo = "원문 Business Outlook 에서 연간 매출, 조정 EBITDA 가이던스가 이전보다 내려갔는지 확인." + (f" 8% 상한 A 조건은 {'확정' if a_status == 'confirmed' else '후보'} 상태라 하향이면 비중 절반 축소 검토." if a_status else "")
        pri = 4 if a_status else 3
    elif kind == "compound":
        word = "확정" if a_status == "confirmed" else "확정 가능성(수동 확인 전)"
        title = f"[GRAB] 8% 상한 {'확정' if a_status == 'confirmed' else '확정 가능성'}에 가이던스 하향 | 비중 절반 축소 검토"
        status = f"A 배달 수수료 8% 상한 {word}, 근거 {ev}. B 가이던스 하향: {'; '.join(cuts)}. 원문: {url}{px}"
        todo = "보유 비중 절반 축소를 검토하세요. 다음 미국 정규장에서 지정가로 나눠서." + ("" if a_status == "confirmed" else " A 는 아직 후보라 규정 원문 확인을 먼저.")
        pri = 5
    else:
        title = "[GRAB] 실적 가이던스 하향, 8% 상한 확정 전 | 참고"
        status = f"B 가이던스 하향: {'; '.join(cuts)}. A 배달 수수료 8% 상한은 확정/후보 기록 없음. 원문: {url}{px}"
        todo = "하향 이유(인도네시아 규제, 경쟁, 환율)만 확인. 이것만으로는 비중 축소 신호 아님."
        pri = 3
    lines = [
        "이게 뭐 알림: 복합 규칙입니다. A 배달 수수료 8% 상한 확정 + B 그 뒤 실적에서 가이던스 하향이면 비중 절반 축소 검토. B 만이면 참고 알림.",
        f"지금 결과/상태: {status} 시각 {stamp}.",
        f"지금 할 일: {todo}",
        "나중에: 복합 알림과 B 단독 알림은 각각 한 번만 옵니다. FY2027 9.3억 달러 기준과 가격선 알림은 그대로 돕니다.",
        "익절/줄일 때: 절반 축소 뒤 남은 절반은 FY2027 조정 EBITDA 기준으로 판단.",
        "하지 말 것: 한 번에 전량 시장가 매도, 기사 제목만으로 A 확정 처리.",
    ]
    return title, append_ends("\n".join(lines), "grab"), pri


def run_cap8(cfg: dict, state: dict, new_picks: list[dict], stamp: str, price: float | None, dry: bool, news_seeded: bool) -> list[str]:
    c = cfg.get("cap8") or {}
    if not c.get("enabled"):
        return []
    cst = state.setdefault("cap8", {})
    a = cst.setdefault("a", {})
    today = stamp[:10]
    # A: candidate from new news titles (news check already de-duplicates articles)
    hits = [p for p in new_picks if cap8_candidate(cfg, p["title"])] if news_seeded else []
    if hits:
        if a.get("status") != "candidate":
            t, b, p = cap8_candidate_message(cfg, hits, stamp)
            if _emit(t, b, p, resolve_topic("port"), dry, ["warning"]) and not dry:
                a.update({"status": "candidate", "since": today})
        if not dry:
            a["links"] = (a.get("links") or []) + [h["link"] for h in hits if h["link"]][:10]
    status, since = cap8_status(cfg, cst)
    print(f"cap8 A status={status} since={since}")
    # B: new earnings releases on SEC
    try:
        rels = sec_earnings_releases(cfg, cst.get("last_filed"))
    except Exception as exc:  # noqa: BLE001
        print(f"cap8 sec fail: {exc}", file=sys.stderr)
        return ["SEC 6-K"]
    seed = cst.get("last_filed") is None
    stored = cst.get("outlook") or {}
    for fd, url, text in rels:
        ol = parse_outlook(text)
        cuts = outlook_cuts(ol, stored)
        print(f"cap8 B {fd} {url} outlook={ {k: (v['lo'], v['hi']) for k, v in ol.items()} } cuts={cuts}")
        msg = None
        if seed:
            print("cap8 B seed (no send)")
        elif not ol:
            msg = cap8_b_message(cfg, "manual", status, a.get("links") or [], [], url, stamp, price)
        elif cuts:
            a_on = status and since and fd >= since
            if a_on and not cst.get("compound"):
                msg = cap8_b_message(cfg, "compound", status, (a.get("links") or []) + ([c["manual"]["source"]] if status == "confirmed" and c["manual"].get("source") else []), cuts, url, stamp, price)
                key = "compound"
            elif not a_on and not cst.get("b_only"):
                msg = cap8_b_message(cfg, "b_only", None, [], cuts, url, stamp, price)
                key = "b_only"
        sent = True
        if msg:
            sent = _emit(*msg[:2], msg[2], resolve_topic("port"), dry, ["chart_with_downwards_trend"])
            if sent and not dry and cuts:
                cst[key] = {"at": stamp, "filed": fd, "url": url, "a": status, "cuts": cuts}
        if not dry and sent:
            for k, v in ol.items():
                stored[k] = {"lo": v["lo"], "hi": v["hi"], "filed": fd}
            cst["outlook"] = stored
            cst["last_filed"] = fd
    return []


# ---------- run ----------

def _emit(title: str, body: str, priority: int, topic: str, dry: bool, tags: list[str]) -> bool:
    if any(ch in title or ch in body for ch in FORBIDDEN):
        print(f"refusing send: forbidden punctuation in copy: {title}", file=sys.stderr)
        return False
    if "GRAB" not in title:
        print(f"refusing send: title without GRAB: {title}", file=sys.stderr)
        return False
    if dry:
        print(f"DRY RUN would send (p{priority}) to {topic}: {title}\n{body}\n")
        return True
    print(f"send p{priority}: {title}")
    publish(topic, title, body, priority, tags=tags, config_name="grab")
    return True


def ops_message(stamp: str, what: str) -> tuple[str, str]:
    title = "[GRAB] 일정/실적/뉴스 조회 실패 | 매매 신호 아님"
    lines = [
        f"이게 뭐 알림: 그랩 실적 일정, 실적 원문(SEC) 또는 인도네시아 규제 뉴스를 읽지 못했습니다({what}). 매매 신호가 아닙니다.",
        f"지금 결과/상태: 조회 실패. 시각 {stamp}.",
        "지금 할 일: 없음. 가격선 알림은 그대로 돕니다.",
        "나중에: 다음 미국 거래일 첫 실행에서 다시 읽습니다.",
        "익절/줄일 때: 해당 없음.",
        "하지 말 것: 조회 실패를 신호로 읽지 않기.",
    ]
    return title, append_ends("\n".join(lines), "grab")


def run_events(cfg: dict, state: dict, now_ny: datetime, stamp: str, price: float | None, dry: bool) -> list[str]:
    problems = []
    titles = fetch_ir_titles(cfg)
    reported: set = set()
    if titles is None:
        problems.append("IR 보도자료")
    else:
        announced, reported = parse_release_titles(titles)
        print(f"ir announced={ {k: v.isoformat() for k, v in announced.items()} } reported={sorted(reported)}")
        for item in earnings_plan(cfg, state["earn"], announced, reported, now_ny.date()):
            title, body = earnings_message(item, stamp, price)
            if _emit(title, body, int(cfg["earnings"].get("priority", 3)), resolve_topic("port"), dry, ["calendar"]) and not dry:
                mark_earn(state["earn"], item, cfg["earnings"].get("remind_days", [7, 1]))
    problems += run_fy_rule(cfg, state, now_ny.date(), stamp, price, dry, reported)
    try:
        items = fetch_news(cfg)
        print(f"news items={len(items)}")
        seeded = bool(state["news"].get("seeded"))
        new_picks = pick_news(cfg, items, set(state["news"].get("seen") or []), now_ny.astimezone(KST))
        for title, body, pri in step_news(cfg, state, items, now_ny.astimezone(KST), stamp, price, dry):
            _emit(title, body, pri, resolve_topic("port"), dry, ["motor_scooter"])
    except Exception as exc:  # noqa: BLE001
        print(f"news fail: {exc}", file=sys.stderr)
        problems.append("뉴스 피드")
        seeded, new_picks = False, []
    problems += run_cap8(cfg, state, new_picks, stamp, price, dry, seeded)
    return problems


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
        armed = dict(state["price"]["armed"])
        seeded = seed_armed(cfg["levels"], armed, price)
        if seeded:
            print(f"seeded from {price:.2f} (no send): { {k: armed[k] for k in seeded} }")
        keys, new_armed = evaluate(cfg["levels"], armed, price)
        keys = [k for k in keys if k not in seeded]
        if mode == "force" and not keys:
            keys = []
            for g in (DOWN, UP):
                f = [k for k in g if hit(cfg["levels"][k], price)]
                if f:
                    keys.append(f[-1])
        print(f"armed={state['price']['armed']} -> {new_armed} send={keys}")
        ok = True
        for key in keys:
            title, body, pri = price_message(cfg, key, price, stamp, ylow)
            tag = "chart_with_upwards_trend" if key in UP else "chart_with_downwards_trend"
            ok = _emit(title, body, pri, resolve_topic("port"), dry, [tag]) and ok
        if not keys:
            print("no new price line; silent")
        if not dry and ok:
            state["price"]["armed"] = new_armed
            state["price"]["last_price"] = price
            state["price"]["last_at"] = stamp

    if state.get("events_day") != day or dry or mode == "force":
        problems = run_events(cfg, state, now_ny, stamp, price, dry)
        if problems:
            title, body = ops_message(stamp, ", ".join(problems))
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
    stamp = "2026-11-04 00:00 KST"

    def check(label: str, got, want) -> None:
        nonlocal fails
        ok = got == want
        fails += not ok
        print(("ok  " if ok else "FAIL"), label, got, "" if ok else f"want {want}")

    # seeding from the current price: 2.86 is inside the zone and under 3.20 / 4.15
    armed: dict = {}
    seed_armed(lv, armed, 2.86)
    check("seed 2.86", armed, {"zone": False, "bear": True, "low52": True, "rec320": True, "up415": True})
    check("seed 2.86 sends nothing", evaluate(lv, armed, 2.86)[0], [])
    a2: dict = {}
    seed_armed(lv, a2, 3.50)
    check("seed 3.50 rec320 disarmed", a2["rec320"], False)
    check("seed 3.50 no up send", evaluate(lv, a2, 3.50)[0], [])

    allarm = {k: True for k in ALL}
    check("3.10 none", evaluate(lv, allarm, 3.10)[0], [])
    check("2.95 zone edge", evaluate(lv, allarm, 2.95)[0], ["zone"])
    check("2.85 zone edge", evaluate(lv, allarm, 2.85)[0], ["zone"])
    check("2.80 none", evaluate(lv, allarm, 2.80)[0], [])
    check("2.73 not below", evaluate(lv, allarm, 2.73)[0], [])
    check("2.72 bear", evaluate(lv, allarm, 2.72)[0], ["bear"])
    check("2.67 bear only", evaluate(lv, allarm, 2.67)[0], ["bear"])
    k, a = evaluate(lv, allarm, 2.60)
    check("2.60 deepest only", k, ["low52"])
    check("2.60 disarms bear", a["bear"], False)

    # once per crossing + re-arm
    k, a = evaluate(lv, allarm, 2.90)
    check("zone again silent", evaluate(lv, a, 2.88)[0], [])
    check("2.83 inside hysteresis", evaluate(lv, a, 2.83)[1]["zone"], False)
    check("2.99 rearms zone", evaluate(lv, a, 2.99)[1]["zone"], True)
    k, a = evaluate(lv, allarm, 2.70)
    check("2.75 bear not rearmed", evaluate(lv, a, 2.75)[1]["bear"], False)
    check("2.76 bear rearmed", evaluate(lv, a, 2.76)[1]["bear"], True)
    k, a = evaluate(lv, allarm, 2.66)
    check("2.69 low52 not rearmed", evaluate(lv, a, 2.69)[1]["low52"], False)
    check("2.70 low52 rearmed", evaluate(lv, a, 2.70)[1]["low52"], True)

    # upward lines
    _, a = evaluate(lv, armed, 2.86)
    k, a = evaluate(lv, a, 3.21)
    check("3.21 rec320 up-cross", k, ["rec320"])
    check("3.25 rec320 silent", evaluate(lv, a, 3.25)[0], [])
    check("3.11 rec320 not rearmed", evaluate(lv, a, 3.11)[1]["rec320"], False)
    check("3.09 rec320 rearmed", evaluate(lv, a, 3.09)[1]["rec320"], True)
    k, a = evaluate(lv, a, 4.15)
    check("4.15 highest only", k, ["up415"])
    check("4.15 disarms rec320", a["rec320"], False)
    check("4.04 up415 not rearmed", evaluate(lv, a, 4.04)[1]["up415"], False)
    check("4.02 up415 rearmed", evaluate(lv, a, 4.02)[1]["up415"], True)

    order = ["이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것", "꺼지는 때"]
    for key, px in (("zone", 2.90), ("bear", 2.70), ("low52", 2.60), ("rec320", 3.21), ("up415", 4.16)):
        title, body, pri = price_message(cfg, key, px, stamp, 2.67)
        check(f"{key} title GRAB", title.startswith("[GRAB] "), True)
        check(f"{key} price", f"{px:.2f}달러" in body, True)
        check(f"{key} condition", "조건:" in body, True)
        check(f"{key} no forbidden", any(ch in title + body for ch in FORBIDDEN), False)
        idx = [body.find(x) for x in order]
        check(f"{key} body order", idx, sorted(idx))
    check("low52 p5", price_message(cfg, "low52", 2.6, stamp, None)[2], 5)

    # earnings
    titles = [
        "Grab Reports Third Quarter 2026 Results, Raises Full-Year Guidance",
        "Grab to Announce Third Quarter 2026 Results on 2 November, 2026",
        "Grab to Announce Fourth Quarter 2024 Results on February 19, 2025",
        "Grab Reports Fourth Quarter and 2025 Results with First Full Year Net Profit",
    ]
    ann, rep = parse_release_titles(titles)
    check("announce dmy", ann.get("Q3 2026"), date(2026, 11, 2))
    check("announce mdy", ann.get("Q4 2024"), date(2025, 2, 19))
    check("reported Q4 2025", "Q4 2025" in rep, True)
    es: dict = {}
    check("estimate far: nothing", earnings_plan(cfg, es, {}, rep, date(2026, 11, 3)), [])
    plan = earnings_plan(cfg, es, {}, rep, date(2027, 2, 3))
    check("estimate D-7", [(p[0], p[1], p[2]) for p in plan], [("Q4 2026", "remind", "d7e")])
    for p in plan:
        mark_earn(es, p, [7, 1])
    check("estimate once", earnings_plan(cfg, es, {}, rep, date(2027, 2, 4)), [])
    off = {"Q4 2026": date(2027, 2, 11)}
    plan = earnings_plan(cfg, es, off, rep, date(2027, 1, 14))
    check("official confirm", [p[1] for p in plan], ["confirm"])
    for p in plan:
        mark_earn(es, p, [7, 1])
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
    for item in [("Q4 2026", "confirm", "", date(2027, 2, 11), True),
                 ("Q4 2026", "remind", "d1", date(2027, 2, 11), True),
                 ("Q4 2026", "remind", "d7e", date(2027, 2, 10), False)]:
        t, b = earnings_message(item, stamp, 2.9)
        check(f"earn {item[1]}{item[2]} title", t.startswith("[GRAB] "), True)
        check(f"earn {item[1]}{item[2]} no forbidden", any(ch in t + b for ch in FORBIDDEN), False)

    # news
    now = datetime(2026, 11, 4, 0, 0, tzinfo=KST)
    w = now - timedelta(hours=5)
    rss = "".join(f"<item><title>{html.escape(tt)}</title><link>{lk}</link><pubDate>{w.strftime('%a, %d %b %Y %H:%M:%S +0900')}</pubDate></item>"
                  for tt, lk in [
                      ("Komdigi Siapkan Aturan Komisi 8% untuk GoFood dan GrabFood - Kompas", "https://n/1"),
                      ("Aturan Turunan Perpres Ojol Bakal Terbit Sebelum Desember 2026 - Liputan6.com", "https://n/2"),
                      ("Komdigi Siapkan Aturan Komisi 8% untuk GoFood dan GrabFood - Detik", "https://n/3"),
                      ("Segini Penghasilan Ojol Kerja Seharian, Makin Lama Makin Kecil - CNBC Indonesia", "https://n/4"),
                      ("Komisi Ojol Turun: Solusi atau Ancaman bagi Industri? - KONTAN", "https://n/5"),
                      ("Komdigi \u00b7 Perpres ojol aturan turunan \u2014 kurir - X", "https://n/6"),
                  ])
    items = parse_rss(rss)
    picks = pick_news(cfg, items, set(), now)
    check("news picks", [p["link"] for p in picks], ["https://n/1", "https://n/2", "https://n/6"])
    check("news prio", [p["prio"] for p in picks], [4, 3, 3])
    st = load_state_blank()
    check("news first run seeds", step_news(cfg, st, items, now, stamp, 2.9, False), [])
    check("news seeded not resent", step_news(cfg, st, items, now, stamp, 2.9, False), [])
    st2 = load_state_blank()
    st2["news"]["seeded"] = "x"
    o = step_news(cfg, st2, items, now, stamp, 2.9, False)
    check("news one bundle p4", (len(o), o[0][2] if o else None), (1, 4))
    check("news title", o[0][0] if o else None, "[GRAB] 인도네시아 배달 수수료 상한 뉴스 3건 | 규제 범위 확인")
    check("news no forbidden", any(ch in o[0][0] + o[0][1] for ch in FORBIDDEN), False)
    check("news dedupe", step_news(cfg, st2, items, now, stamp, 2.9, False), [])
    t, _ = fail_message(stamp)
    check("fail title", t.startswith("[GRAB] ") and "조회 실패" in t, True)
    t, _ = ops_message(stamp, "x")
    check("ops title", t.startswith("[GRAB] ") and "조회 실패" in t, True)

    # FY Adjusted EBITDA rule: sample press-release text (above, below, billion, conflict, unparseable, real 2025 excerpt)
    tbl = "Unaudited Summary Three months ended December 31, For the year ended December 31, 2027 2026 2027 2026 "
    above = ("Grab Reports Fourth Quarter and 2027 Results. Segment Adjusted EBITDA for the full year was $1,400 million. "
             "Adjusted EBITDA for the full year was $1,050 million, an increase of $290 million compared to $760 million in 2026. "
             + tbl + "Legal, tax and regulatory settlement provisions (3) 5 (10) 12 Adjusted EBITDA 290 220 1,050 760 Regional corporate costs 99 96 380 368")
    below = ("Grab Reports Fourth Quarter and 2027 Results. Adjusted EBITDA for the full year was $0.85 billion. "
             "Adjusted EBITDA was $210 million for the quarter. "
             + tbl + "Adjusted EBITDA 210 220 850 760 Regional corporate costs 99 96 380 368")
    edge = "Grab Reports Fourth Quarter and 2027 Results. Full year 2027 Adjusted EBITDA of $930 million. " + tbl + "Adjusted EBITDA 240 220 930 760 Regional corporate costs 1 1 1 1"
    conflict = "Grab Reports Fourth Quarter and 2027 Results. Adjusted EBITDA for the full year was $900 million. " + tbl + "Adjusted EBITDA 240 220 950 760 Regional corporate costs 1 1 1 1"
    unparse = "Grab Reports Fourth Quarter and 2027 Results. Adjusted EBITDA was $250 million for the quarter. Mobility segment adjusted EBITDA for the full year was $1,000 million."
    tableonly = "Grab Reports Fourth Quarter and 2027 Results. " + tbl + "Adjusted EBITDA 240 220 1,020 760 Regional corporate costs 1 1 1 1"
    check("fy release detect", is_fy_release(above, 2027), True)
    check("fy release not other year", is_fy_release(above.replace("2027", "2029"), 2027), False)
    q3 = ("Grab Reports Third Quarter 2026 Results. Nine months ended September 30, For the year ended December 31, 2025 "
          "Adjusted EBITDA 160 120 560 352 Regional corporate costs 1 1 1 1")
    check("q3 release rejected", is_fy_release(q3, 2025), False)
    check("q3 table ignored", parse_fy_adj_ebitda(q3, 2025)[0], None)
    check("fy above", parse_fy_adj_ebitda(above, 2027), (1050.0, "본문+표 일치"))
    check("fy below billion", parse_fy_adj_ebitda(below, 2027), (850.0, "본문+표 일치"))
    check("fy edge 930", parse_fy_adj_ebitda(edge, 2027)[0], 930.0)
    check("fy table only", parse_fy_adj_ebitda(tableonly, 2027), (1020.0, "표"))
    check("fy conflict -> None", parse_fy_adj_ebitda(conflict, 2027)[0], None)
    check("fy unparseable -> None", parse_fy_adj_ebitda(unparse, 2027)[0], None)
    real = ("Grab Reports Fourth Quarter and 2025 Results with First Full Year Net Profit. Adjusted EBITDA for the full year was $500 million, an increase of $187 million compared to $313 million in 2024. "
            "Business Outlook Financial Measure Guidance FY 2026 Outlook 2026 Group Revenue $4.04 billion - $4.10 billion 20% - 22% growth YoY "
            "2026 Adjusted EBITDA $700 million - $720 million 40% - 44% growth YoY Financial Measure Guidance 3 Year Outlook 2025 - 2028 "
            "2028 Adjusted EBITDA $1.5 billion For the year ended December 31, 2025 2024 2025 2024 Legal, tax and regulatory settlement provisions "
            "(15) 43 (3) 48 Adjusted EBITDA 148 97 500 313 Regional corporate costs 96 87 368 350 Total Segment Adjusted EBITDA 244 184 868 663")
    check("real 2025 FY", parse_fy_adj_ebitda(real, 2025), (500.0, "본문+표 일치"))
    check("real 2026 guidance mid", parse_guidance(real, 2026)[0], 710.0)
    check("guidance 2028 single billion", parse_guidance(real, 2028)[0], 1500.0)
    check("guidance missing", parse_guidance(real, 2027)[0], None)
    check("guidance below", parse_guidance("2027 Adjusted EBITDA $880 million - $920 million", 2027)[0], 900.0)
    check("guidance billion range", parse_guidance("2027 Adjusted EBITDA $0.95 billion - $1.0 billion", 2027)[0], 975.0)
    check("fmt eok", (fmt_eok(930), fmt_eok(850), fmt_eok(1050)), ("9.3억 달러", "8.5억 달러", "10.5억 달러"))
    for val, why, want_pri, want_kind in ((850.0, "본문+표 일치", 5, "below"), (930.0, "본문", 3, "holds"), (1050.0, "표", 3, "holds"), (None, "숫자가 여러 개", 4, "manual")):
        t, b, pri, kind = fy_messages(cfg, 2027, val, why, "https://www.sec.gov/x", stamp, 3.1)
        check(f"fy msg {want_kind} kind/pri", (kind, pri), (want_kind, want_pri))
        check(f"fy msg {want_kind} GRAB", t.startswith("[GRAB] "), True)
        check(f"fy msg {want_kind} threshold", "9.3억 달러" in t + b, True)
        check(f"fy msg {want_kind} link", "https://www.sec.gov/x" in b, True)
        check(f"fy msg {want_kind} no forbidden", any(ch in t + b for ch in FORBIDDEN), False)
        check(f"fy msg {want_kind} stays on port", route_ok(t), True)
        if val is not None:
            check(f"fy msg {want_kind} figure", fmt_eok(val) in t, True)
    t, b, pri, kind = fy_messages(cfg, 2027, 850.0, "x", "u", stamp, None)
    check("below says 정리 검토", "정리 검토" in t, True)
    for mid, want in ((900.0, "below"), (975.0, "holds"), (None, "manual")):
        t, b, pri, kind = guidance_message(cfg, 2027, mid, "x", "u", stamp)
        check(f"guid {want}", kind, want)
        check(f"guid {want} clean", any(ch in t + b for ch in FORBIDDEN) or not t.startswith("[GRAB] ") or not route_ok(t), False)

    # ---- compound rule: 8% cap (A) + guidance cut (B) ----
    for ttl, want in (
        ("Permen Komdigi ditetapkan, komisi ojol dan kurir makanan maksimal 8 persen mulai berlaku", True),
        ("Indonesia issues regulation capping food delivery commission at 8%", True),
        ("Pemerintah usulkan komisi GoFood GrabFood 8 persen", False),
        ("Rancangan aturan komisi kurir 8% masih dikaji", False),
        ("Indonesia may cap food delivery commission at 8%", False),
        ("Aturan komisi ojol 8 persen resmi berlaku", False),
        ("Komisi 10 persen kurir makanan ditetapkan", False),
    ):
        check(f"cap8 cand {ttl[:40]}", cap8_candidate(cfg, ttl), want)
    q3 = ("Business Outlook Financial Measure Guidance FY 2026 Outlook 2026 Group Revenue $4.15 billion - $4.20 billion 23% - 25% growth YoY "
          "(Previous: $4.10 billion - $4.15 billion 22% - 23% growth YoY) 2026 Adjusted EBITDA $790 million - $800 million 58% - 60% growth YoY "
          "(Previous: $720 million - $740 million 44% - 48% growth YoY) The above guidance ... Segment Financial and Operational Highlights Segment Adjusted EBITDA 123 78")
    ol = parse_outlook(q3)
    check("outlook real Q3 2026 rows", sorted(ol), ["2026 Adjusted EBITDA", "2026 Revenue"])
    check("outlook ebitda mid", ol["2026 Adjusted EBITDA"]["mid"], 795.0)
    check("outlook revenue billion", (ol["2026 Revenue"]["lo"], ol["2026 Revenue"]["prev"]), (4150.0, [4100.0, 4150.0]))
    check("raise is not a cut", outlook_cuts(ol, {}), [])
    cut = q3.replace("$790 million - $800 million", "$700 million - $720 million")
    check("cut vs inline Previous", len(outlook_cuts(parse_outlook(cut), {})), 1)
    q4 = "Business Outlook Financial Measure Guidance 2027 Group Revenue $4.6 billion to $4.7 billion 2027 Adjusted EBITDA $1.00 billion - $1.05 billion Segment Financial"
    check("new year no previous, no cut", outlook_cuts(parse_outlook(q4), {}), [])
    q1 = "Business Outlook 2027 Group Revenue $4.6 billion - $4.7 billion 2027 Adjusted EBITDA $950 million - $980 million Segment Financial"
    st = {k: {"lo": v["lo"], "hi": v["hi"]} for k, v in parse_outlook(q4).items()}
    check("cut vs stored outlook", [x.split(" ")[1] for x in outlook_cuts(parse_outlook(q1), st)], ["Adjusted"])
    check("unparseable outlook", parse_outlook("Grab Reports First Quarter 2027 Results. Revenue grew 20%."), {})
    for kind, ast in (("compound", "confirmed"), ("compound", "candidate"), ("b_only", None), ("manual", "candidate"), ("manual", None)):
        t, b, p = cap8_b_message(cfg, kind, ast, ["https://example.com/a"], ["2027 Adjusted EBITDA x"], "https://sec/x", stamp, 3.0)
        check(f"cap8 {kind} {ast} prio", p, {"compound": 5, "b_only": 3}.get(kind, 4 if ast else 3))
        check(f"cap8 {kind} {ast} clean", any(ch in t + b for ch in FORBIDDEN) or not t.startswith("[GRAB] ") or not route_ok(t) or "https://sec/x" not in b, False)
    check("compound says 절반", "비중 절반 축소 검토" in cap8_b_message(cfg, "compound", "candidate", [], ["x"], "u", stamp, None)[0], True)
    t, b, p = cap8_candidate_message(cfg, [{"when": datetime(2027, 3, 1, tzinfo=KST), "title": "x", "link": "https://n/1"}], stamp)
    check("cap8 candidate p4 clean", (p, "8% 상한 확정 가능성" in t, "https://n/1" in b, route_ok(t), any(ch in t + b for ch in FORBIDDEN)), (4, True, True, True, False))
    # end-to-end with stubs: seed, B only, candidate, compound, once
    g = globals()
    real_sec, real_emit = g["sec_earnings_releases"], g["_emit"]
    sent: list = []
    feed: list = []
    g["sec_earnings_releases"] = lambda cfg_, after: [x for x in feed if not after or x[0] > after]
    g["_emit"] = lambda t, b, p, topic, dry, tags: sent.append((p, t)) or True
    try:
        s: dict = {}
        feed[:] = [("2027-02-10", "u1", q4)]
        run_cap8(cfg, s, [], "2027-02-11 00:00 KST", None, False, True)
        check("e2e seed silent", sent, [])
        feed.append(("2027-05-05", "u2", q1))
        run_cap8(cfg, s, [], "2027-05-06 00:00 KST", None, False, True)
        check("e2e B only p3", [p for p, _ in sent], [3])
        pick = {"when": datetime(2027, 6, 1, tzinfo=KST), "title": "Permen Komdigi ditetapkan, komisi kurir makanan maksimal 8 persen", "link": "https://n/2"}
        run_cap8(cfg, s, [pick], "2027-06-01 00:00 KST", None, False, True)
        check("e2e candidate p4", [p for p, _ in sent], [3, 4])
        q2 = q1.replace("$950 million - $980 million", "$900 million - $930 million")
        feed.append(("2027-08-04", "u3", q2))
        run_cap8(cfg, s, [], "2027-08-05 00:00 KST", None, False, True)
        check("e2e compound p5", [p for p, _ in sent], [3, 4, 5])
        check("e2e compound says 가능성", "확정 가능성" in sent[-1][1], True)
        feed.append(("2027-11-03", "u4", q2.replace("$900 million - $930 million", "$850 million - $880 million")))
        run_cap8(cfg, s, [], "2027-11-04 00:00 KST", None, False, True)
        check("e2e once each", len(sent), 3)
        feed.append(("2028-02-10", "u5", "Grab Reports Fourth Quarter 2027 Results"))
        run_cap8(cfg, s, [], "2028-02-11 00:00 KST", None, False, True)
        check("e2e unparseable manual p4", [p for p, _ in sent][-1], 4)
        s2: dict = {"cap8": {"last_filed": "2027-02-10", "outlook": st}}
        cfg2 = json.loads(json.dumps(cfg))
        cfg2["cap8"]["manual"] = {"status": "confirmed", "on": "2027-04-01", "source": "https://jdih/x"}
        feed[:] = [("2027-05-05", "u2", q1)]
        run_cap8(cfg2, s2, [], "2027-05-06 00:00 KST", None, False, True)
        check("e2e manual confirmed compound", (sent[-1][0], "확정에" in sent[-1][1]), (5, True))
    finally:
        g["sec_earnings_releases"], g["_emit"] = real_sec, real_emit

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
    for k in ALL:
        check(f"config title {k}", lv[k]["title"].startswith("[GRAB] "), True)

    edt_open = datetime(2026, 10, 30, 13, 30, tzinfo=timezone.utc).astimezone(NY)
    check("EDT open in", in_regular_session(edt_open), True)
    check("EST 14:30 UTC in", in_regular_session(datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc).astimezone(NY)), True)
    check("EST 13:30 UTC out", in_regular_session(datetime(2026, 11, 2, 13, 30, tzinfo=timezone.utc).astimezone(NY)), False)
    check("saturday out", in_regular_session(edt_open.replace(day=31)), False)

    print(f"selftest fails={fails}")
    return 1 if fails else 0


def route_ok(title: str) -> bool:
    from ntfy_util import route_topic
    return route_topic("cwally-port", title) == "cwally-port"


def load_state_blank() -> dict:
    return {"price": {"armed": {}}, "earn": {}, "news": {"seen": []}}


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
    dry = mode in {"dry", "test", "dry-run"}
    if dry:
        print("DRY RUN")
    try:
        return run(mode, price_arg)
    except Exception as exc:  # noqa: BLE001
        print(traceback.format_exc(), file=sys.stderr)
        if dry:
            return 1
        try:
            st = load_state()
            today = datetime.now(KST).strftime("%Y-%m-%d")
            if st.get("crash_day") == today:
                return 0
            stamp = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
            title = "[GRAB] 스크립트 오류 조회 실패 | 이번 실행 판정 못 함"
            body = append_ends("\n".join([
                "이게 뭐 알림: GRAB 알림 스크립트가 이번 실행에서 멈췄습니다. 매매 신호 아님.",
                f"지금 결과/상태: {type(exc).__name__}: {clean(str(exc))[:200]}. 시각 {stamp}.",
                "지금 할 일: 필요하면 GRAB 시세를 직접 보세요.",
                "나중에: 다음 30분 실행에서 다시 봅니다. 오류 안내는 하루 한 번.",
                "익절/줄일 때: 해당 없음.",
                "하지 말 것: 오류를 조건 미충족으로 읽기.",
            ]), "grab")
            publish(resolve_topic("ops"), title, body, 3, tags=["warning"], config_name="grab")
            st["crash_day"] = today
            save_state(st, False)
        except Exception:  # noqa: BLE001
            return 1
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
