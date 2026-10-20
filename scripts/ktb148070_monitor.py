#!/usr/bin/env python3
"""One-shot: 2026-10-22 BOK MPC (금통위) decision -> KIWOOM 국고채10년 (148070) split-buy verdict.

Runs once at 2026-10-22 15:30 KST (configs/ktb148070.json oneshot_kst, picker job=oneshot;
GitHub cron "30 6 22 10 *" is the late fallback). Rewritten 2026-10-20 (chat approval 11:58 KST)
so the body reflects the real decision and the day's yields instead of a hardcoded text.

1. MPC decision for meeting_date only (never the previous meeting):
   a. official: BOK press release "통화정책방향(2026.10.22)" (news list -> view page), parsed
      decision sentence + dissent sentence
   b. official: BOK base-rate table (a row dated meeting_date = rate change)
   c. official: MPC schedule page row for meeting_date has the press file (= published)
   d. second source: Google News RSS headlines limited to the meeting window (vote)
   If none settles it -> "확인 불가" (title routes to cwally-ops via ntfy_util.route_topic).
2. Yields: Naver bond 10y/3y (realtime value for today + daily closes for 1d / 1w change),
   ECOS 817Y002 cross-check when ECOS_API_KEY is set.
3. ETF: Naver price / NAV / premium, issuer (Kiwoom) YTM and duration table.
4. Rule-based verdict from configs/ktb148070.json "rules": 확대 검토 / 소액 분할 유지 / 대기.

modes:
  auto (default)  send once inside the window; state/ktb148070.json dedup (records ntfy id)
  dry             fetch real data, print what it found and the title/body; no ntfy, no state write
  force           like auto but ignores the sent flag (manual retry); still year-guarded
  sim <scenario>  injected data, print title/body; no network, no ntfy, no state write
                  scenarios: hold, hold_dovish, hold_hawkish, cut, cut_low, hike, rally,
                             fail_decision, fail_yield, news_only_hike, conflict
Not investment advice.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "ktb148070.json"
STATE_PATH = ROOT / "state" / "ktb148070.json"
UA = {"User-Agent": "Mozilla/5.0 (cwally-alerts; +mailto:sosmos3218@gmail.com)"}
BOK = "https://www.bok.or.kr"
DEC_KO = {"hold": "동결", "cut": "인하", "hike": "인상"}


# ---------- small helpers ----------

def fetch(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def get_json(url: str, timeout: int = 20):
    return json.loads(fetch(url, timeout))


def text_of(html: str) -> str:
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html or "", flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", unescape(t)).strip()


def num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").replace("+", "").replace("%", ""))
    except ValueError:
        return None


def on_grid(rate: float) -> bool:
    return abs(rate * 100 - round(rate * 4) * 25) < 0.01


def pct2(v) -> str:
    return "확인 불가" if v is None else f"{v:.2f}%"


def pct3(v) -> str:
    return "확인 불가" if v is None else f"{v:.3f}%"


def bp(v) -> str:
    return "확인 불가" if v is None else f"{v:+.0f}bp"


def won(v) -> str:
    return "확인 불가" if v is None else f"{v:,.0f}원"


# ---------- state ----------

def read_state() -> dict:
    base = {"sent": False, "sent_at": None, "ntfy_id": None, "title": None,
            "problem_key": None, "problem_ntfy_id": None}
    if STATE_PATH.exists():
        try:
            base.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return base


def save_state(state: dict, now: datetime) -> None:
    state["updated_at"] = now.isoformat(timespec="seconds")
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------- 1. MPC decision ----------

DEC_HOLD_RE = re.compile(r"기준금리를\s*(?:현\s*수준|현재의?)\s*\(?\s*(\d\.\d{1,2})\s*%\s*\)?\s*(?:수준)?\s*(?:에서|으로|로)?\s*유지")
DEC_MOVE_RE = re.compile(
    r"기준금리를\s*(?:현재의\s*)?(\d\.\d{1,2})\s*%\s*(?:수준)?\s*에서\s*(\d\.\d{1,2})\s*%\s*로\s*(상향|하향|인상|인하)")
DISSENT_HEAD_RE = re.compile(r"금번\s*기준금리\s*\S+\s*결정에\s*대해.{0,400}?(?:나타냈다|나타내었다|이루어졌다|찬성하였다|$)")
DISSENT_ONE_RE = re.compile(r"([가-힣]{2,4})\s*위원은\s*기준금리를\s*(\d\.\d{1,2})\s*%\s*로\s*(?:유지|인상|인하|상향|하향|조정)")


def parse_release(text: str) -> dict | None:
    """Decision + dissent from the BOK 통화정책방향 text (HTML view or PDF text)."""
    t = re.sub(r"\s+", " ", text or "")
    out = None
    m = DEC_MOVE_RE.search(t)
    if m:
        old, new = float(m.group(1)), float(m.group(2))
        if old != new:
            out = {"decision": "hike" if new > old else "cut", "rate": new, "prev": old}
    if out is None:
        m = DEC_HOLD_RE.search(t)
        if m:
            r = float(m.group(1))
            out = {"decision": "hold", "rate": r, "prev": r}
    if out is None:
        return None
    out["dissent"] = parse_dissent(t, out["rate"])
    return out


def parse_dissent(t: str, rate: float) -> dict:
    """{'text': short Korean, 'minority': 'hike'|'cut'|None, 'unanimous': bool|None}"""
    if re.search(r"만장일치", t):
        return {"text": "만장일치", "minority": None, "unanimous": True}
    head = DISSENT_HEAD_RE.search(t)
    seg = head.group(0) if head else ""
    hits = DISSENT_ONE_RE.findall(seg or t)
    if not hits:
        if head:
            return {"text": "소수의견 없음(결정문 기준)", "minority": None, "unanimous": True}
        return {"text": "소수의견 확인 안 됨", "minority": None, "unanimous": None}
    parts, mins = [], set()
    for name, r in hits:
        r = float(r)
        side = "hike" if r > rate else ("cut" if r < rate else None)
        if side:
            mins.add(side)
        parts.append(f"{name} 위원 {r:.2f}% 주장")
    minority = mins.pop() if len(mins) == 1 else None
    return {"text": "소수의견: " + ", ".join(parts), "minority": minority, "unanimous": False}


def bok_release(cfg: dict) -> dict:
    """Official press release for meeting_date. {'status': found|absent|error, ...}"""
    src = cfg["sources"]
    title_re = re.compile(src["bok_release_title_re"])
    try:
        lst = fetch(src["bok_news_list"], 25)
    except Exception as exc:
        return {"status": "error", "error": f"목록 {type(exc).__name__}"}
    link = None
    for m in re.finditer(r'<a[^>]+href="([^"]*nttId=\d+[^"]*)"[^>]*>(.*?)</a>', lst, re.S):
        if title_re.search(text_of(m.group(2))):
            link = unescape(m.group(1))
            break
    if not link:
        return {"status": "absent"}
    nid = re.search(r"nttId=(\d+)", link).group(1)
    board = re.search(r"/bbs/(\w+)/", link)
    url = f"{BOK}/portal/bbs/{board.group(1) if board else 'P0000559'}/view.do?nttId={nid}&menuNo=200690"
    try:
        body = text_of(fetch(url, 25))
    except Exception as exc:
        return {"status": "error", "error": f"본문 {type(exc).__name__}", "url": url}
    parsed = parse_release(body)
    if not parsed:
        return {"status": "unparsed", "url": url}
    return {"status": "found", "url": url, **parsed}


def parse_base_table(html: str) -> list[tuple[str, float]]:
    """[(YYYY-MM-DD, rate)] newest first from the BOK base-rate history table."""
    t = text_of(html)
    i = t.find("변경일자")
    rows = []
    for y, mo, d, r in re.findall(r"(20\d\d)\s+(\d{2})월\s*(\d{2})일\s+(\d{1,2}\.\d{2})", t[i if i >= 0 else 0:]):
        rows.append((f"{y}-{mo}-{d}", float(r)))
    return rows


def bok_base_table(cfg: dict) -> dict:
    try:
        rows = parse_base_table(fetch(cfg["sources"]["bok_base_rate_table"], 25))
    except Exception as exc:
        return {"status": "error", "error": type(exc).__name__}
    if not rows:
        return {"status": "error", "error": "표 비어 있음"}
    return {"status": "ok", "latest_date": rows[0][0], "latest_rate": rows[0][1],
            "prev_rate": rows[1][1] if len(rows) > 1 else None}


def meeting_published(html: str, meeting_date: str) -> bool | None:
    mo, d = meeting_date[5:7], meeting_date[8:10]
    m = re.search(rf'<th scope="row">\s*{mo}월\s*{d}일', html)
    if not m:
        return None
    nxt = html.find('<th scope="row">', m.end())
    row = html[m.end(): nxt if nxt > 0 else m.end() + 6000]
    return bool(re.search(r"국문보도자료|fileDown\.do|viewer\.html", row))


def bok_meeting(cfg: dict) -> dict:
    try:
        pub = meeting_published(fetch(cfg["sources"]["bok_meeting_page"], 25), cfg["meeting_date"])
    except Exception as exc:
        return {"status": "error", "error": type(exc).__name__}
    return {"status": "ok", "published": pub}


NEWS_SKIP = re.compile(r"전망|예상|할 듯|유력|주목|여부|앞두고|D-\d|내일|촉각|시나리오|관측|올리나|내리나|하나\b|\?|점검|Comments|기자간담회 예정|이번 주")
NEWS_HOLD = re.compile(r"동결|유지")
NEWS_HIKE = re.compile(r"인상|올려|올렸|올린|상향|↑|긴축 돌입")
NEWS_CUT = re.compile(r"인하|내려|내렸|내린|하향|↓")
NEWS_PAIR = re.compile(r"(\d\.\d{1,2})\s*%?\s*(?:→|->|에서)\s*(\d\.\d{1,2})\s*%")
NEWS_RATE = re.compile(r"(?:연\s*)?(\d\.\d{1,2})\s*%")


def classify_headline(title: str, ref_rate: float, max_move: float) -> dict | None:
    t = re.sub(r"\s+-\s+[^-]+$", "", title).strip()  # drop " - 언론사"
    if NEWS_SKIP.search(t) or not re.search(r"기준금리|금리|금통위|한은|한국은행", t):
        return None
    rate = None
    pm = NEWS_PAIR.search(t)
    if pm:
        a, b = float(pm.group(1)), float(pm.group(2))
        if a != b and on_grid(a) and on_grid(b) and abs(a - b) <= max_move:
            rate = b
            dec = "hike" if b > a else "cut"
            return {"decision": dec, "rate": rate, "title": t}
    hold = bool(NEWS_HOLD.search(t))
    hike = bool(NEWS_HIKE.search(t))
    cut = bool(NEWS_CUT.search(t))
    if hold and not re.search(r"동결\s*(?:전망|예상)?\s*(?:깨|깬|뒤집)", t):
        dec = "hold"
    elif hike and not cut:
        dec = "hike"
    elif cut and not hike:
        dec = "cut"
    else:
        return None
    for r in NEWS_RATE.findall(t):
        r = float(r)
        if on_grid(r) and abs(r - ref_rate) <= max_move:
            rate = r
            break
    return {"decision": dec, "rate": rate, "title": t}


def parse_rss(xml_text: str) -> list[dict]:
    out = []
    root = ET.fromstring(xml_text)
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        pub = (it.findtext("pubDate") or "").strip()
        try:
            day = datetime.strptime(pub[5:16], "%d %b %Y").strftime("%Y-%m-%d")
        except ValueError:
            day = ""
        out.append({"title": unescape(title), "day": day})
    return out


def news_vote(items: list[dict], meeting_date: str, ref_rate: float, max_move: float) -> dict:
    md = datetime.strptime(meeting_date, "%Y-%m-%d")
    ok_days = {(md + timedelta(days=k)).strftime("%Y-%m-%d") for k in (0, 1)}  # pubDate is GMT and coarse
    votes = []
    for it in items:
        if it["day"] not in ok_days:
            continue
        c = classify_headline(it["title"], ref_rate, max_move)
        if c:
            votes.append(c)
    if not votes:
        return {"status": "none", "n": 0, "votes": []}
    tally = {}
    for v in votes:
        tally[v["decision"]] = tally.get(v["decision"], 0) + 1
    top = max(tally, key=tally.get)
    share = tally[top] / len(votes)
    rates = [v["rate"] for v in votes if v["decision"] == top and v["rate"] is not None]
    rate = max(set(rates), key=rates.count) if rates else None
    return {"status": "ok", "decision": top, "rate": rate, "n": len(votes), "agree": share,
            "tally": tally, "examples": [v["title"] for v in votes if v["decision"] == top][:3],
            "dissent": news_dissent([v["title"] for v in votes])}


def news_dissent(titles: list[str]) -> str | None:
    for t in titles:
        if "만장일치" in t:
            return "만장일치(기사)"
        m = re.search(r"(\d)\s*명\s*중\s*(\d)\s*명\s*찬성", t)
        if m:
            return f"{m.group(1)}명 중 {m.group(2)}명 찬성(기사)"
        m = re.search(r"(?:금통위원\s*)?(\d)\s*명\s*(?:은|이)?\s*[\"“']?\s*(올려야|내려야|인상\s*주장|인하\s*주장|동결\s*주장)", t)
        if m:
            return f"소수의견 {m.group(1)}명 {m.group(2)}(기사)"
    return None


def news_source(cfg: dict, ref_rate: float) -> dict:
    q = urllib.parse.quote(cfg["sources"]["news_rss_query"])
    url = f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
    try:
        items = parse_rss(fetch(url, 20))
    except Exception as exc:
        return {"status": "error", "error": type(exc).__name__}
    return news_vote(items, cfg["meeting_date"], ref_rate, float(cfg["rules"]["max_rate_move_pct"]))


def resolve_decision(cfg: dict, rel: dict, table: dict, mtg: dict, news: dict) -> dict:
    """Combine sources. status: ok | not_yet | unknown. Never returns a previous meeting."""
    rules = cfg["rules"]
    md = cfg["meeting_date"]
    notes = []
    table_change = table.get("status") == "ok" and table.get("latest_date") == md
    table_rate = table.get("latest_rate") if table.get("status") == "ok" else None

    def news_agrees(dec, rate):
        if news.get("status") != "ok":
            return None
        same = news["decision"] == dec and (news["rate"] is None or rate is None or abs(news["rate"] - rate) < 0.001)
        return same

    if rel.get("status") == "found":
        d = {"status": "ok", "decision": rel["decision"], "rate": rel["rate"], "prev": rel["prev"],
             "source": "한은 통화정책방향 보도자료", "dissent": rel["dissent"]["text"],
             "minority": rel["dissent"]["minority"], "url": rel.get("url")}
        if table.get("status") == "ok":
            if rel["decision"] != "hold" and not table_change:
                notes.append("기준금리 표는 아직 미반영")
            if rel["decision"] == "hold" and table_change:
                notes.append("기준금리 표와 다름, 원문 확인 필요")
        ag = news_agrees(rel["decision"], rel["rate"])
        notes.append("뉴스 헤드라인 일치" if ag else ("뉴스 헤드라인과 다름" if ag is False else "뉴스 교차확인 안 됨"))
        d["notes"] = notes
        return d

    if table_change:
        prev = table.get("prev_rate")
        dec = "hike" if prev is not None and table_rate > prev else "cut"
        d = {"status": "ok", "decision": dec, "rate": table_rate, "prev": prev,
             "source": "한은 기준금리 추이 표", "dissent": news.get("dissent") or "소수의견 확인 안 됨",
             "minority": None}
        ag = news_agrees(dec, table_rate)
        notes.append("뉴스 헤드라인 일치" if ag else ("뉴스 헤드라인과 다름" if ag is False else "뉴스 교차확인 안 됨"))
        d["notes"] = notes
        return d

    published = mtg.get("status") == "ok" and mtg.get("published")
    if news.get("status") == "ok" and news["n"] >= int(rules["news_min_headlines"]) and news["agree"] >= float(rules["news_min_agree"]):
        if news["decision"] == "hold":
            if table_rate is not None and (news["rate"] is None or abs(news["rate"] - table_rate) < 0.001):
                src = "뉴스 헤드라인 + 한은 기준금리 표(변경 없음)"
                if published:
                    src += " + 한은 회의자료 게시"
                return {"status": "ok", "decision": "hold", "rate": table_rate, "prev": table_rate,
                        "source": src, "dissent": news.get("dissent") or "소수의견 확인 안 됨",
                        "minority": None, "notes": [f"헤드라인 {news['n']}건 중 {news['agree']:.0%} 동결"]}
            return {"status": "unknown", "why": "뉴스는 동결인데 금리 숫자가 한은 표와 다름"}
        if news["n"] >= int(rules["news_only_min_headlines"]) and news["agree"] >= float(rules["news_only_min_agree"]) and news["rate"] is not None:
            ref = table_rate if table_rate is not None else float(cfg["current_base_rate"])
            ok_dir = (news["decision"] == "hike" and news["rate"] > ref) or (news["decision"] == "cut" and news["rate"] < ref)
            if ok_dir:
                return {"status": "ok", "decision": news["decision"], "rate": news["rate"], "prev": ref,
                        "source": "뉴스 헤드라인 기준(한은 공식 자료 아직 미확인)",
                        "dissent": news.get("dissent") or "소수의견 확인 안 됨", "minority": None,
                        "notes": [f"헤드라인 {news['n']}건 중 {news['agree']:.0%} 일치"]}
        return {"status": "unknown", "why": "뉴스 헤드라인만 있고 한은 공식 자료와 맞춰 볼 수 없음"}

    if rel.get("status") == "absent" and not published and not table_change and news.get("status") in ("none", "ok"):
        return {"status": "not_yet", "why": f"{md} 결정문, 기준금리 표 변경, 결정 기사 모두 아직 없음"}
    errs = [f"{k} {v.get('error') or v.get('status')}" for k, v in
            (("보도자료", rel), ("기준금리표", table), ("회의자료", mtg), ("뉴스", news))
            if v.get("status") in ("error", "unparsed")]
    return {"status": "unknown", "why": ", ".join(errs) or "결정을 판단할 근거 부족"}


# ---------- 2. yields ----------

def naver_series(code: str, pages: int = 2) -> list[tuple[str, float]]:
    out = {}
    for page in range(1, pages + 1):
        raw = get_json("https://m.stock.naver.com/front-api/marketIndex/prices?category=bond&page="
                       f"{page}&reutersCode={urllib.parse.quote(code)}")
        for r in (raw or {}).get("result") or []:
            v, d = num(r.get("closePrice")), str(r.get("localTradedAt") or "")[:10]
            if v is not None and d:
                out[d] = v
    return sorted(out.items())


def naver_now(code: str) -> tuple[float | None, str | None]:
    raw = get_json(f"https://m.stock.naver.com/front-api/marketIndex/productDetail?category=bond&reutersCode={urllib.parse.quote(code)}")
    r = (raw or {}).get("result") or {}
    return num(r.get("closePrice")), str(r.get("localTradedAt") or "")[:16].replace("T", " ") or None


def yield_view(series: list[tuple[str, float]], now_val, now_at, day: str) -> dict:
    """Value for `day` (realtime if dated today, else that day's close) and 1d / 1w changes."""
    closes = dict(series)
    val, at, kind = None, None, None
    if now_val is not None and now_at and now_at[:10] == day:
        val, at, kind = now_val, now_at, "실시간"
    elif day in closes:
        val, at, kind = closes[day], day, "종가"
    elif series:
        at, val = series[-1]
        kind = "최근 종가"
    prior = [(d, v) for d, v in series if d < (at or day)[:10]]
    d1 = (val - prior[-1][1]) * 100 if val is not None and prior else None
    wk = (datetime.strptime((at or day)[:10], "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
    wrows = [(d, v) for d, v in series if d <= wk]
    w1 = (val - wrows[-1][1]) * 100 if val is not None and wrows else None
    return {"value": val, "at": at, "kind": kind, "d1_bp": d1, "w1_bp": w1,
            "prev": prior[-1] if prior else None, "week_ago": wrows[-1] if wrows else None,
            "is_today": bool(at and at[:10] == day)}


def ecos_last(cfg: dict, item: str) -> tuple[str, float] | None:
    key = os.environ.get("ECOS_API_KEY")
    if not key:
        return None
    end = datetime.now(KST)
    url = (f"https://ecos.bok.or.kr/api/StatisticSearch/{urllib.parse.quote(key)}/json/kr/1/30/"
           f"{cfg['sources']['ecos_stat']}/D/{(end - timedelta(days=14)):%Y%m%d}/{end:%Y%m%d}/{item}")
    try:
        rows = (get_json(url, 12).get("StatisticSearch") or {}).get("row") or []
        vals = sorted((str(r["TIME"]), float(r["DATA_VALUE"])) for r in rows if r.get("DATA_VALUE") not in (None, ""))
        if vals:
            t, v = vals[-1]
            return f"{t[:4]}-{t[4:6]}-{t[6:8]}", v
    except Exception as exc:
        print(f"ecos skipped {item}: {type(exc).__name__}", file=sys.stderr)
    return None


def yields(cfg: dict, day: str) -> dict:
    src = cfg["sources"]
    out = {}
    for tag, code in (("y10", src["naver_bond_10y"]), ("y3", src["naver_bond_3y"])):
        try:
            series = naver_series(code)
            try:
                nv, nat = naver_now(code)
            except Exception as exc:
                print(f"naver realtime skipped {code}: {type(exc).__name__}", file=sys.stderr)
                nv, nat = None, None
            out[tag] = yield_view(series, nv, nat, day)
        except Exception as exc:
            out[tag] = {"value": None, "error": type(exc).__name__}
    e10 = ecos_last(cfg, src["ecos_ktb10_item"])
    if e10:
        out["ecos10"] = e10
    return out


# ---------- 3. ETF ----------

def parse_issuer_table(text: str) -> dict | None:
    t = re.sub(r"\s+", " ", text or "")
    asof = re.search(r"(20\d\d)\.(\d{2})\.(\d{2})\s*기준", t)
    m = re.search(r"KIWOOM 국고채10년(?!레버리지)\s*(?:\(\s*148070\s*\))?\s*\|?\s*(\d{1,2}\.\d{2,3})\s*%\s*\|?\s*(\d{1,2}\.\d{1,3})", t)
    if not m:
        return None
    ytm, dur = float(m.group(1)), float(m.group(2))
    if not (0.5 <= ytm <= 10 and 3 <= dur <= 12):
        return None
    return {"ytm": ytm, "duration": dur, "asof": f"{asof.group(1)}-{asof.group(2)}-{asof.group(3)}" if asof else None}


def parse_issuer_product(text: str) -> dict | None:
    """Product page layout: "Duration & YTM (2025.10.22 기준) Duration(년)8.111 YTM(%)2.953%"."""
    m = re.search(r"\(\s*(20\d\d)\.(\d{2})\.(\d{2})\s*기준\s*\)\s*Duration\s*\(년\)\s*(\d+\.\d+)\s*YTM\s*\(%\)\s*(\d+\.\d+)", text or "")
    if not m:
        return None
    return {"ytm": float(m.group(5)), "duration": float(m.group(4)), "asof": f"{m.group(1)}-{m.group(2)}-{m.group(3)}"}


def etf(cfg: dict) -> dict:
    code = cfg["sources"]["etf_code"]
    out = {}
    try:
        basic = get_json(f"https://m.stock.naver.com/api/stock/{code}/basic")
        integ = get_json(f"https://m.stock.naver.com/api/stock/{code}/integration")
        ind = (integ or {}).get("etfKeyIndicator") or {}
        rate = num(ind.get("deviationRate"))
        prem = None
        if rate is not None:
            prem = -abs(rate) if str(ind.get("deviationSign") or "").strip() == "-" else abs(rate)
        out.update({"price": num(basic.get("closePrice")), "chg_pct": num(basic.get("fluctuationsRatio")),
                    "at": str(basic.get("localTradedAt") or "")[:16].replace("T", " "),
                    "nav": num(ind.get("nav")), "premium": prem})
        if str((basic.get("compareToPreviousPrice") or {}).get("name") or "") == "FALLING" and out["chg_pct"]:
            out["chg_pct"] = -abs(out["chg_pct"])
    except Exception as exc:
        out["error"] = type(exc).__name__
    issuer = None
    for url in (cfg["sources"]["issuer_bond_table"], cfg["sources"]["issuer_product_page"]):
        try:
            page = text_of(fetch(url, 15))
        except Exception as exc:
            print(f"issuer skipped {url}: {type(exc).__name__}", file=sys.stderr)
            continue
        issuer = parse_issuer_table(page) or parse_issuer_product(page)
        stale = (datetime.strptime(cfg["meeting_date"], "%Y-%m-%d") - timedelta(days=14)).strftime("%Y-%m-%d")
        if issuer and issuer.get("asof") and issuer["asof"] < stale:
            print(f"issuer value too old ({issuer['asof']}); ignored", file=sys.stderr)
            issuer = None
        if issuer:
            issuer["source"] = "키움 발행사"
            break
    if not issuer:
        issuer = {"ytm": cfg.get("ytm"), "duration": cfg.get("duration"), "asof": cfg.get("ytm_asof"),
                  "source": "설정값, 발행사 조회 실패"}
    out["issuer"] = issuer
    return out


# ---------- 4. verdict ----------

def judge(cfg: dict, dec: dict, y: dict) -> dict:
    r = cfg["rules"]
    y10 = y["y10"]["value"]
    d1, w1 = y["y10"].get("d1_bp"), y["y10"].get("w1_bp")
    hi = y10 is not None and y10 >= float(r["expand_min_y10"])
    sharp = (d1 is not None and d1 <= -float(r["sharp_drop_1d_bp"])) or (w1 is not None and w1 <= -float(r["sharp_drop_1w_bp"]))
    if dec["decision"] == "hike":
        return {"verdict": "wait", "reason": "기준금리 인상. 금리가 더 오를 수 있어 채권 가격 하락 위험이 큼"}
    if sharp:
        return {"verdict": "wait", "reason": f"10년 금리가 이미 크게 하락(하루 {bp(d1)}, 1주 {bp(w1)}). 기대가 가격에 먼저 반영됨"}
    if dec["decision"] == "cut":
        if hi:
            return {"verdict": "expand", "reason": f"기준금리 인하 + 10년 {pct2(y10)}로 기준 {float(r['expand_min_y10']):.2f}% 이상"}
        return {"verdict": "keep", "reason": f"인하지만 10년 {pct2(y10)}로 기준 {float(r['expand_min_y10']):.2f}% 아래, 이미 반영된 편"}
    dovish = dec.get("minority") == "cut" or (d1 is not None and d1 <= -float(r["dovish_1d_bp"]))
    hawkish = dec.get("minority") == "hike" or (d1 is not None and d1 >= float(r["hawkish_jump_1d_bp"]))
    if dovish and hi:
        why = "소수의견 인하" if dec.get("minority") == "cut" else f"10년 하루 {bp(d1)} 하락"
        return {"verdict": "expand", "reason": f"동결이지만 비둘기 신호({why}) + 10년 {pct2(y10)}로 기준 {float(r['expand_min_y10']):.2f}% 이상"}
    if hawkish:
        why = "소수의견 인상" if dec.get("minority") == "hike" else f"10년 하루 {bp(d1)} 상승"
        return {"verdict": "keep", "reason": f"동결이지만 매파 신호({why}). 늘리지 말고 간격을 넓혀 소액만", "hawkish": True}
    return {"verdict": "keep", "reason": f"동결, 10년 하루 {bp(d1)}, 1주 {bp(w1)}로 큰 변화 없음"}


VERDICT_KO = {"expand": "확대 검토", "keep": "소액 분할 유지", "wait": "대기"}


# ---------- copy ----------

INTRO = ("이게 뭐 알림: 10/22 금통위(한국은행 금융통화위원회) 실제 결정과 당일 국고채 금리로 "
         "KIWOOM 국고채10년(148070) 분할매수를 늘릴지(확대 검토), 그대로 갈지(소액 분할 유지), 쉴지(대기) 판정하는 일회 알림입니다.")
DONT = ("하지 말 것: 원금 보장이나 연 4% 확정 이자 상품으로 보지 마세요(가격은 매일 움직입니다). "
        "한 번에 몰아 사기, 레버리지 ETF로 바꾸기, 신용이나 미수 매수 금지. 투자 권유가 아니며 최종 판단은 본인 몫입니다.")


def yline(y: dict) -> str:
    a, b = y["y10"], y["y3"]
    spread = (a["value"] - b["value"]) * 100 if a.get("value") is not None and b.get("value") is not None else None
    s = (f"국고채 10년 {pct3(a.get('value'))}({a.get('kind') or '조회 실패'} {a.get('at') or ''}, 전일 대비 {bp(a.get('d1_bp'))}, "
         f"1주 전 대비 {bp(a.get('w1_bp'))}), 3년 {pct3(b.get('value'))}(전일 대비 {bp(b.get('d1_bp'))}), "
         f"10년-3년 차이 {bp(spread)}.")
    if y.get("ecos10"):
        s += f" 한은 ECOS 10년 {y['ecos10'][0]} {y['ecos10'][1]:.3f}%."
    return s.replace("( ", "(").replace(" ,", ",")


def eline(e: dict) -> str:
    iss = e["issuer"]
    prem = "확인 불가" if e.get("premium") is None else f"{e['premium']:+.2f}%"
    chg = "" if e.get("chg_pct") is None else f", 전일 대비 {e['chg_pct']:+.2f}%"
    return (f"148070 {won(e.get('price'))}({e.get('at') or '시각 미상'}{chg}), NAV {won(e.get('nav'))}, 괴리 {prem}. "
            f"YTM {pct3(iss.get('ytm'))}, 듀레이션 {iss.get('duration') or '확인 불가'}년({iss.get('source')}, {iss.get('asof') or '기준일 미상'}).")


def build(cfg: dict, dec: dict, y: dict, e: dict, v: dict, now: datetime) -> tuple[str, list[str], int]:
    dko = DEC_KO[dec["decision"]]
    y10 = y["y10"]["value"]
    vko = VERDICT_KO[v["verdict"]]
    title = f"[KTB148070] 금통위 {dko} {dec['rate']:.2f}% | 10년 {y10:.2f}% | {vko}"
    if dec["decision"] == "hold":
        what = f"기준금리 {dec['rate']:.2f}% {dko}"
    else:
        what = f"기준금리 {dec['prev']:.2f}%에서 {dec['rate']:.2f}%로 {dko}"
    notes = ", ".join(dec.get("notes") or [])
    dur = e["issuer"].get("duration")
    sens = f"10년 금리 0.1%p 오르면 148070은 약 {dur * 0.1:.1f}% 떨어집니다(듀레이션 기준 추정)." if dur else ""
    expand_min = float(cfg["rules"]["expand_min_y10"])
    status = (f"지금 결과/상태: 결론 {vko}. 금통위 {what}"
              f"(출처 {dec['source']}{', ' + notes if notes else ''}). {dec.get('dissent') or ''}. "
              f"{yline(y)} {eline(e)} 판정 이유: {v['reason']}. 조회 {now:%m/%d %H:%M} KST.")
    status = status.replace(". .", ".").replace("..", ".")
    if v["verdict"] == "expand":
        todo = ("지금 할 일: 미리 정한 분할 계획 안에서 이번 회차만 조금 늘리는 것을 검토하세요(평소 1회분의 1.5배 이내). "
                "지정가로 2번 이상 나눠 사고, 괴리가 +0.3% 넘게 붙으면 그날은 넘기세요.")
        later = (f"나중에: 11/26 다음 금통위 전까지 10년 금리가 {expand_min:.2f}% 아래로 크게 내려가면 확대를 멈추고 소액 분할로 돌아갑니다.")
    elif v["verdict"] == "keep":
        gap = "간격을 평소보다 넓혀(예: 2주에 1회) " if v.get("hawkish") else ""
        todo = f"지금 할 일: 평소 계획대로 {gap}소액 분할만 이어가세요. 이번 결과로 금액을 늘리지 마세요."
        later = (f"나중에: 인하 신호(소수의견 인하, 물가 둔화)가 나오고 10년이 {expand_min:.2f}% 이상이면 확대를 다시 봅니다. "
                 "11/26 다음 금통위 전에 다시 점검하세요.")
    else:
        todo = "지금 할 일: 이번 회차 추가 매수는 쉬세요. 보유분은 급히 팔 필요는 없습니다."
        later = ("나중에: 10년 금리가 다시 오르거나(되돌림) 11/26 금통위에서 방향이 확인되면 소액 분할부터 다시 시작합니다.")
    cut = f"익절/줄일 때: 이 돈을 1년 안에 써야 하거나, 추가 인상 신호가 강해지면 비중을 줄이세요. {sens}".strip()
    pri = 4 if v["verdict"] in ("expand", "wait") else 3
    return title, [INTRO, status, todo, later, cut, DONT], pri


def problem_copy(cfg: dict, kind: str, detail: str, dec: dict | None, now: datetime) -> tuple[str, list[str]]:
    if dec and dec.get("status") == "ok":
        title = f"[KTB148070] 금통위 {DEC_KO[dec['decision']]} {dec['rate']:.2f}% | {kind}, 판단 보류"
    else:
        title = f"[KTB148070] 금통위 결과 {kind} | 판단 보류"
    lines = [
        INTRO,
        f"지금 결과/상태: {kind}. {detail}. 조회 {now:%m/%d %H:%M} KST. 한은: {BOK}/portal/singl/baseRate/list.do?dataSeCd=01&menuNo=200643",
        "지금 할 일: 한은 홈페이지 통화정책방향 원문과 HTS 국고채 10년 금리를 직접 확인하기 전에는 매수 금액을 바꾸지 마세요.",
        "나중에: 원인을 고친 뒤 force로 다시 돌리면 정상 판정을 한 번 보냅니다.",
        "익절/줄일 때: 이 알림은 진입 강도만 봅니다. 보유분은 기존 계획을 따르세요.",
        "하지 말 것: 조회 실패를 매수나 매도 신호로 읽지 마세요. 투자 권유 아님.",
    ]
    return title, lines


def send(cfg: dict, title: str, lines: list[str], priority: int, dry: bool) -> str | None:
    """Publish via ntfy_util.publish (route_topic + append_ends inside). Returns the ntfy id."""
    if dry:
        print("DRY RUN (would send)", title, *lines, "dry-run; no ntfy, no state write", sep="\n")
        return None
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        publish(cfg["ntfy_topic"], title, "\n".join(lines), priority,
                ["chart_with_downwards_trend" if "대기" in title else "chart_with_upwards_trend"],
                resolve_server(cfg.get("ntfy_server")))
    raw = buf.getvalue()
    print(raw.strip())
    try:
        return json.loads(raw.strip().splitlines()[-1]).get("id")
    except Exception:
        return None


# ---------- simulation ----------

def sim_data(name: str, cfg: dict) -> tuple[dict, dict, dict]:
    def yv(v, d1, w1, kind="실시간"):
        return {"value": v, "at": "2026-10-22 15:30", "kind": kind, "d1_bp": d1, "w1_bp": w1, "is_today": True}
    y = {"y10": yv(4.18, -1.0, -3.0), "y3": yv(3.86, -1.5, -2.0)}
    e = {"price": 106300.0, "chg_pct": 0.08, "at": "2026-10-22 15:30", "nav": 106280.0, "premium": 0.02,
         "issuer": {"ytm": 4.21, "duration": 7.74, "asof": "2026-10-21", "source": "키움 발행사"}}
    off_hold = {"status": "found", "decision": "hold", "rate": 3.0, "prev": 3.0,
                "dissent": {"text": "만장일치", "minority": None, "unanimous": True}, "url": "sim"}
    table = {"status": "ok", "latest_date": "2026-08-27", "latest_rate": 3.0, "prev_rate": 2.75}
    mtg = {"status": "ok", "published": True}
    news_hold = {"status": "ok", "decision": "hold", "rate": 3.0, "n": 12, "agree": 0.92, "dissent": None}
    rel, nw = off_hold, news_hold
    if name == "hold_dovish":
        rel = {**off_hold, "dissent": {"text": "소수의견: 홍길동 위원 2.75% 주장", "minority": "cut", "unanimous": False}}
        y["y10"] = yv(4.12, -6.0, -8.0)
    elif name == "hold_hawkish":
        rel = {**off_hold, "dissent": {"text": "소수의견: 홍길동 위원 3.25% 주장", "minority": "hike", "unanimous": False}}
        y["y10"] = yv(4.27, 9.0, 6.0)
    elif name == "cut":
        rel = {**off_hold, "decision": "cut", "rate": 2.75, "prev": 3.0}
        table = {**table, "latest_date": cfg["meeting_date"], "latest_rate": 2.75, "prev_rate": 3.0}
        nw = {**news_hold, "decision": "cut", "rate": 2.75}
        y["y10"] = yv(4.06, -7.0, -12.0)
    elif name == "cut_low":
        rel = {**off_hold, "decision": "cut", "rate": 2.75, "prev": 3.0}
        nw = {**news_hold, "decision": "cut", "rate": 2.75}
        y["y10"] = yv(3.92, -6.0, -14.0)
    elif name == "hike":
        rel = {**off_hold, "decision": "hike", "rate": 3.25, "prev": 3.0,
               "dissent": {"text": "소수의견: 홍길동 위원 3.00% 주장", "minority": "cut", "unanimous": False}}
        nw = {**news_hold, "decision": "hike", "rate": 3.25}
        y["y10"] = yv(4.31, 12.0, 15.0)
    elif name == "rally":
        y["y10"] = yv(3.98, -12.0, -28.0)
    elif name == "fail_decision":
        rel = {"status": "error", "error": "목록 URLError"}
        table = {"status": "error", "error": "URLError"}
        mtg = {"status": "error", "error": "URLError"}
        nw = {"status": "error", "error": "URLError"}
    elif name == "fail_yield":
        y = {"y10": {"value": None, "error": "URLError"}, "y3": {"value": None, "error": "URLError"}}
    elif name == "news_only_hike":
        rel = {"status": "absent"}
        nw = {"status": "ok", "decision": "hike", "rate": 3.25, "n": 9, "agree": 0.89, "dissent": "7명 중 6명 찬성(기사)"}
        y["y10"] = yv(4.29, 10.0, 12.0)
    elif name == "conflict":
        rel = {"status": "absent"}
        nw = {"status": "ok", "decision": "hike", "rate": 3.25, "n": 3, "agree": 0.67, "dissent": None}
    elif name != "hold":
        raise SystemExit(f"unknown scenario: {name}")
    dec = resolve_decision(cfg, rel, table, mtg, nw)
    return dec, y, e


# ---------- main ----------

def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode not in {"auto", "dry", "force", "sim"}:
        raise SystemExit(f"unknown mode: {mode}")
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    md = cfg["meeting_date"]

    if mode == "sim":
        name = sys.argv[2] if len(sys.argv) > 2 else "hold"
        print(f"SIM {name}: injected data; no network, no ntfy, no state write")
        dec, y, e = sim_data(name, cfg)
        fake_now = datetime.strptime("2026-10-22 15:30", "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        return finish(cfg, dec, y, e, fake_now, dry=True, state=None)

    dry = mode == "dry"
    if dry:
        print("DRY RUN: no ntfy, no state write")
    if now.year != int(cfg["event_year"]):
        print(f"year guard: {now.year} != {cfg['event_year']}; silent")
        return 0
    start = datetime.strptime(cfg["send_after_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    end = datetime.strptime(cfg["ends_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST) + timedelta(minutes=1)
    in_window = start <= now < end
    if not in_window:
        if not dry:
            print(f"outside window {cfg['send_after_kst']} ~ {cfg['ends_kst']} KST; silent")
            return 0
        print(f"outside window {cfg['send_after_kst']} ~ {cfg['ends_kst']} KST; dry run continues for wiring check")
    state = read_state()
    if state.get("sent") and mode == "auto":
        print(f"already sent ({state.get('ntfy_id')}); silent")
        return 0

    rel = bok_release(cfg)
    table = bok_base_table(cfg)
    mtg = bok_meeting(cfg)
    ref = table.get("latest_rate") if table.get("status") == "ok" else float(cfg["current_base_rate"])
    news = news_source(cfg, ref)
    print(f"[decision] meeting_date={md}")
    print(f"  bok_release : {json.dumps(rel, ensure_ascii=False)}")
    print(f"  bok_table   : {json.dumps(table, ensure_ascii=False)}")
    print(f"  bok_meeting : {json.dumps(mtg, ensure_ascii=False)}")
    print(f"  news        : {json.dumps({k: v for k, v in news.items() if k != 'votes'}, ensure_ascii=False)}")
    dec = resolve_decision(cfg, rel, table, mtg, news)
    print(f"  resolved    : {json.dumps(dec, ensure_ascii=False)}")
    day = now.strftime("%Y-%m-%d")
    y = yields(cfg, day)
    print(f"[yields] {json.dumps(y, ensure_ascii=False)}")
    e = etf(cfg)
    print(f"[etf] {json.dumps(e, ensure_ascii=False)}")
    if dry and dec["status"] == "not_yet":
        print(f"MPC decision for {md}: not yet ({dec['why']}). On {md} this would stay silent before "
              f"{cfg['send_after_kst']} and report 확인 불가 if still missing after it.")
        if y["y10"].get("value") is not None and not in_window:
            # wiring check: show what the copy would look like using the current base rate as a stand-in
            stand = {"status": "ok", "decision": "hold", "rate": ref, "prev": ref, "source": "dry 예시(실제 결정 아님)",
                     "dissent": "소수의견 확인 안 됨", "minority": None, "notes": []}
            print("--- dry wiring example only (NOT the 10/22 decision) ---")
            return finish(cfg, stand, y, e, now, dry=True, state=None)
        return 0
    return finish(cfg, dec, y, e, now, dry=dry, state=state)


def finish(cfg: dict, dec: dict, y: dict, e: dict, now: datetime, dry: bool, state: dict | None) -> int:
    if dec["status"] != "ok":
        why = dec.get("why") or "결정 근거 없음"
        title, lines = problem_copy(cfg, "확인 불가", f"{cfg['meeting_date']} 금통위 결정을 확인하지 못했습니다({why})", None, now)
        return send_problem(cfg, title, lines, f"decision:{why}", dry, state, now)
    y10 = y["y10"]
    if y10.get("value") is None or (not dry and not y10.get("is_today")):
        detail = f"10년 금리 {y10.get('error') or '당일 값 없음'} (마지막 {y10.get('at') or '없음'})"
        title, lines = problem_copy(cfg, "금리 확인 불가", detail, dec, now)
        return send_problem(cfg, title, lines, f"yield:{detail}", dry, state, now)
    v = judge(cfg, dec, y)
    title, lines, pri = build(cfg, dec, y, e, v, now)
    for s in [title, *lines]:
        assert "\u00b7" not in s and "\u2014" not in s, "forbidden punctuation"
    nid = send(cfg, title, lines, pri, dry)
    if not dry and state is not None:
        state.update({"sent": True, "sent_at": now.isoformat(timespec="seconds"), "ntfy_id": nid, "title": title,
                      "verdict": v["verdict"], "decision": dec["decision"], "rate": dec["rate"],
                      "y10": y10["value"], "source": dec["source"]})
        save_state(state, now)
        print(f"sent ntfy id={nid}")
    return 0


def send_problem(cfg, title, lines, key, dry, state, now) -> int:
    if not dry and state is not None and state.get("problem_key") == key:
        print("problem already reported; silent")
        return 0
    nid = send(cfg, title, lines, 3, dry)
    if not dry and state is not None:
        state.update({"problem_key": key, "problem_ntfy_id": nid})
        save_state(state, now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
