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
    title = "[GRAB] 일정/뉴스 조회 실패 | 매매 신호 아님"
    lines = [
        f"이게 뭐 알림: 그랩 실적 일정 또는 인도네시아 규제 뉴스를 읽지 못했습니다({what}). 매매 신호가 아닙니다.",
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
    if titles is None:
        problems.append("IR 보도자료")
    else:
        announced, reported = parse_release_titles(titles)
        print(f"ir announced={ {k: v.isoformat() for k, v in announced.items()} } reported={sorted(reported)}")
        for item in earnings_plan(cfg, state["earn"], announced, reported, now_ny.date()):
            title, body = earnings_message(item, stamp, price)
            if _emit(title, body, int(cfg["earnings"].get("priority", 3)), resolve_topic("port"), dry, ["calendar"]) and not dry:
                mark_earn(state["earn"], item, cfg["earnings"].get("remind_days", [7, 1]))
    try:
        items = fetch_news(cfg)
        print(f"news items={len(items)}")
        for title, body, pri in step_news(cfg, state, items, now_ny.astimezone(KST), stamp, price, dry):
            _emit(title, body, pri, resolve_topic("port"), dry, ["motor_scooter"])
    except Exception as exc:  # noqa: BLE001
        print(f"news fail: {exc}", file=sys.stderr)
        problems.append("뉴스 피드")
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
