#!/usr/bin/env python3
"""BOK MPC (금통위) decision helpers shared by alert scripts (read-only fetch/parse, no ntfy, no state).

Moved here 2026-10-26 from scripts/ktb148070_monitor.py (retired one-shot) unchanged, so that
scripts/krrate_monitor.py keeps working: bok_release, bok_base_table, bok_meeting, news_source,
resolve_decision, parse_base_table, parse_rss, fetch, text_of.
cfg keys read: meeting_date, current_base_rate, rules, sources.{bok_news_list, bok_release_title_re,
bok_base_rate_table, bok_meeting_page, news_rss_query}.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from html import unescape

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


# ---------- MPC decision ----------

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
