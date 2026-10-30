#!/usr/bin/env python3
"""Shared earnings-call transcript helpers (no alert of its own, no config, no state).

Used by:
  - scripts/secq3_monitor.py   [SECQ3] call add-on (Q3 2026 only). Its behaviour is unchanged: the functions
    below were moved here from secq3_monitor.py verbatim (transcript_body, find_transcript, sentences, amount,
    direction, asp_pick, judge_check1, judge_check2) and secq3 imports them back under the same names.
  - scripts/005930_monitor.py  [005930] quarterly earnings auto-check (configs/005930.json "earn_auto",
    2026-10-30): finds the 디일렉(thelec.kr) "삼성전자 YYYY년 N분기 실적발표 컨퍼런스콜 전문" every quarter and
    judges the three plan checks (item1 LTA/HBM, item2 CAPEX + price-decline hint, item3 ASP -10% + China
    YMTC/CXMT news keywords). Results are 문제없음 / 경계 / 확인 불가; 확인 불가 never counts as 문제없음.

The transcript is a press transcription (not an official document). Every judgment quotes one sentence.
"""
from __future__ import annotations

import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path

KST = timezone(timedelta(hours=9))
# same browser UA as secq3_monitor.py (news.samsung.com / thelec answer it quickly)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}

OK, WARN, UNK = "문제없음", "경계", "확인 불가"


# ---------- fetch ----------

def fetch(url: str, timeout: int = 30) -> bytes | None:
    """Body bytes, or None on 404/410 (not published yet). Other errors raise. (same as secq3_monitor.fetch)"""
    parts = urllib.parse.urlsplit(url)
    url = urllib.parse.urlunsplit(parts._replace(path=urllib.parse.quote(parts.path)))
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 410):
            return None
        raise


def read_src(src: str) -> bytes | None:
    if re.match(r"https?://", src):
        return fetch(src)
    return Path(src).read_bytes()


# ---------- moved from secq3_monitor.py (call add-on), unchanged ----------

ASP_WORDS = ("평균 판매 단가", "평균판매단가", "평균 판매가", "평균판매가", "판매 단가", "ASP", "판가")
UP_WORDS = ("상승", "인상", "증가", "올랐", "개선")
DOWN_WORDS = ("하락", "인하", "감소", "떨어", "내렸")
POS_ADJ = {"초반": 2.0, "중반": 5.0, "후반": 8.0}


def transcript_body(raw: bytes) -> str:
    s = raw.decode("utf-8", errors="ignore")
    i = s.find('id="article-view-content-div"')
    if i >= 0:
        j = s.find("</article>", i)
        s = s[i: j if j > 0 else None]
    s = re.sub(r"<(script|style|figure)[^>]*>.*?</\1>", "", s, flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</(p|div|h\d|li)>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = unescape(s).replace("\xa0", " ")
    return "\n".join(x.strip() for x in s.splitlines() if x.strip())


def transcript_sources(ca: dict) -> list[str]:
    """secq3 call_addon: rss_url + list_url pages 1-3 (unchanged). 005930 earn_auto: its own "sources" list."""
    if ca.get("sources"):
        return list(ca["sources"])
    return [ca["rss_url"]] + [ca["list_url"] + f"&page={n}" for n in (1, 2, 3)]


def find_transcript(ca: dict, list_src: str | None = None, reader=None) -> tuple[str | None, str | None]:
    """(body text, url) of the Samsung call transcript from the RSS feed or the list pages.
    reader(src) -> bytes | None; secq3 passes its own read_src so its tests and behaviour stay the same.
    Google-news-sitemap entries (<loc> + <news:title>, thelec.kr/sitemap.xml) are read too; the RSS and
    list pages secq3 uses have no <news:title>, so secq3 sees the same pairs as before."""
    reader = reader or read_src
    title_re = re.compile(ca["title_re"])
    srcs = [list_src] if list_src else transcript_sources(ca)
    for src in srcs:
        try:
            raw = reader(src)
        except Exception as exc:
            print(f"transcript list skipped ({src}): {exc}", file=sys.stderr)
            continue
        if not raw:
            continue
        s = raw.decode("utf-8", errors="ignore")
        pairs = re.findall(r"<title>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</title>\s*<link>\s*(.*?)\s*</link>", s, re.S)
        pairs += [(t, u) for u, t in re.findall(r'href="([^"]*articleView\.html\?idxno=\d+)"[^>]*>\s*(?:<[^>]+>\s*)*([^<]{6,160})', s)]
        if "<news:title>" in s:
            pairs += [(t, u) for u, t in re.findall(r"<loc>\s*(.*?)\s*</loc>.*?<news:title>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</news:title>", s, re.S)]
        for title, url in pairs:
            if title_re.search(unescape(title).strip()):
                url = unescape(url).strip()
                if not url.startswith("http"):
                    url = "https://www.thelec.kr" + url
                body = reader(url)
                text = transcript_body(body) if body else ""
                if len(text) > 2000:
                    return text, url
    return None, None


def sentences(text: str) -> list[str]:
    return [x.strip() for x in re.split(r"(?<=[.?!다])\s+|\n", text) if x.strip()]


def amount(seg: str) -> float | None:
    """'40% 중반' -> 45, '90%대 초반' -> 92, '한 자릿수 후반' -> 8, '두 자릿수' -> 10, '소폭' -> 2."""
    m = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%\s*대?\s*(초반|중반|후반)?", seg)
    if m:
        return float(m.group(1)) + POS_ADJ.get(m.group(2) or "", 0.0)
    m = re.search(r"(한|두)\s*자리\s*수\s*(초반|중반|후반)?|(한|두)\s*자릿수\s*(초반|중반|후반)?", seg)
    if m:
        tens = m.group(1) or m.group(3)
        adj = m.group(2) or m.group(4)
        if tens == "한":
            return POS_ADJ.get(adj or "", 5.0) if adj else 5.0
        return 10.0 + POS_ADJ.get(adj or "", 0.0)
    if "소폭" in seg:
        return 2.0
    if re.search(r"보합|비슷|유사|변동\s*없", seg):
        return 0.0
    return None


def direction(seg: str, sent: str) -> int | None:
    for part in (seg, sent):
        up = any(w in part for w in UP_WORDS)
        down = any(w in part for w in DOWN_WORDS)
        if up != down:
            return 1 if up else -1
    return None


def asp_pick(text: str) -> dict:
    """First actual (not outlook) ASP sentence for DRAM and NAND. Values signed in %."""
    sents = [x for x in sentences(text) if any(w in x for w in ASP_WORDS)
             and not re.search(r"스마트폰|세트|모바일 기기|부품 원가|MLCC", x)]
    actual = [x for x in sents if not re.search(r"전망|예상|계획|예정|것으로 보", x)]
    out: dict = {"outlook": next((x for x in sents if re.search(r"전망|예상", x) and re.search(r"D램|낸드|DRAM|NAND", x)), None)}
    for key, pat, other in (("dram", r"D램|DRAM", r"낸드|NAND"), ("nand", r"낸드|NAND", r"D램|DRAM")):
        for x in actual:
            m = re.search(pat, x)
            if not m:
                continue
            seg = x[m.end():]
            o = re.search(other, seg)
            seg = seg[: o.start()] if o else seg
            a, dsign = amount(seg), direction(seg, x)
            if a is None or dsign is None:
                continue
            raw = re.search(r"(\d{1,3}(?:\.\d+)?\s*%\s*대?\s*(?:초반|중반|후반)?|(?:한|두)\s*(?:자리\s*수|자릿수)\s*(?:초반|중반|후반)?|소폭|보합)", seg)
            out[key] = {"pct": a * dsign, "raw": (raw.group(0).strip() if raw else seg.strip()[:12]) + (" 상승" if dsign > 0 else " 하락"),
                        "sent": x}
            break
    return out


def judge_check1(asp: dict, ca: dict) -> tuple[str, str]:
    d, n = asp.get("dram"), asp.get("nand")
    if not d:
        return "확인 불가", "전문에 D램 ASP 문장 없음"
    if d["pct"] < 0 or (n and n["pct"] < 0):
        return "경계", "ASP 하락 전환"
    if d["pct"] >= ca["dram_keep_min_pct"] and (n is None or n["pct"] >= ca["nand_keep_min_pct"]):
        return "유지", f"D램 +{ca['dram_keep_min_pct']:g}% 이상"
    return "관찰", f"D램 상승폭 +{ca['dram_keep_min_pct']:g}% 미만"


NEG_AFTER = r"[^.]{0,20}(?:없|않|아니)"


def warn_hits(sents: list[str], warn_words: list[str]) -> list[str]:
    """Sentences with a warn word that is not negated right after it ('축소는 없다')."""
    return [x for x in sents if any(re.search(re.escape(w), x) and not re.search(re.escape(w) + NEG_AFTER, x)
                                    for w in warn_words)]


def judge_check2(text: str, ca: dict) -> tuple[str, str, str | None]:
    sents = sentences(text)
    lta = [x for x in sents if any(w in x for w in ca["lta_words"])]
    warn = warn_hits(lta, ca["lta_warn_words"])
    keep = [x for x in sents if any(w in x for w in ca["lta_keep_words"]) and re.search(r"가격|floor", x, re.I)]
    share = next((m.group(0) for x in lta for m in [re.search(r"\d{2}\s*[~∼-]\s*\d{2}\s*%", x)] if m), None)
    if warn:
        return "경계", "축소/재협상/취소 언급", warn[0]
    if keep:
        return "유지", "하한가격 언급 유지" + (f", 계약 비중 {share}" if share else ""), keep[0]
    if lta:
        return "관찰", "장기계약 언급은 있으나 하한가 언급 없음", lta[0]
    return "확인 불가", "전문에 장기계약 언급 없음", None


# ---------- 005930 earn_auto (2026-10-30) ----------

QUESTION_RE = re.compile(r"^\s*Q\s*[.:]|(?:느냐|는가|나요|할까|일까|달라|궁금하다|부탁드린다|부탁한다)\s*[.?]?\s*$|\?\s*$")


def is_question(s: str) -> bool:
    """Analyst questions (Q. ..., ...부탁드린다.) never count as company statements."""
    return bool(QUESTION_RE.search(s))


def quarter_of_filing(d: date) -> tuple[int, int]:
    """Quarter reported by a results filing made on day d: Jan-Mar -> previous year Q4, Apr-Jun Q1, Jul-Sep Q2, Oct-Dec Q3."""
    if d.month <= 3:
        return d.year - 1, 4
    return d.year, (d.month - 1) // 3


def qkey(y: int, q: int) -> str:
    return f"{y}Q{q}"


def parse_qkey(k: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{4})Q([1-4])", k)
    if not m:
        raise ValueError(f"bad quarter key: {k}")
    return int(m.group(1)), int(m.group(2))


def qlabel(y: int, q: int) -> str:
    return f"{y}년 {q}분기"


def next_quarter(y: int, q: int) -> tuple[int, int]:
    return (y + 1, 1) if q == 4 else (y, q + 1)


def title_re_for(template: str, y: int, q: int) -> str:
    return template.replace("{year}", str(y)).replace("{q}", str(q))


def cut(s: str | None, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _any(words, s: str) -> bool:
    return any(w in s for w in words)


def judge_item1(text: str | None, conf: dict) -> dict:
    """2027년 HBM 장기공급계약(LTA) 물량 축소 여부. LTA or HBM-supply sentences (answers, not questions);
    경계 on reduction / renegotiation / cancellation / delay wording (not negated)."""
    if not text:
        return {"status": UNK, "why": "컨콜 전문 없음", "evidence": None}
    sents = [x for x in sentences(text) if not is_question(x)]
    rel = [x for x in sents if _any(conf["lta_words"], x)
           or ("HBM" in x and re.search(conf["hbm_supply_re"], x))]
    if not rel:
        return {"status": UNK, "why": "전문에 LTA, HBM 공급 문장 없음", "evidence": None}
    warn = warn_hits(rel, conf["warn_words"])
    if warn:
        return {"status": WARN, "why": "물량 축소, 재협상, 취소, 지연 표현", "evidence": warn[0]}
    pref = [x for x in rel if "HBM" in x and re.search(r"2027|내년", x)]
    pref += [x for x in rel if _any(conf["lta_words"], x) and re.search(conf["keep_re"], x)]
    return {"status": OK, "why": "축소, 재협상, 취소, 지연 표현 없음", "evidence": (pref or rel)[0]}


def judge_item2(text: str | None, conf: dict) -> dict:
    """CAPEX(설비투자) 강행 및 판가 하락 암시. 경계 only when a CAPEX-expansion sentence AND a price-decline hint both appear."""
    if not text:
        return {"status": UNK, "why": "컨콜 전문 없음", "evidence": None}
    sents = [x for x in sentences(text) if not is_question(x)]
    capex_any = [x for x in sents if re.search(conf["capex_re"], x, re.I) and not re.search(conf["capex_exclude_re"], x)]
    capex = [x for x in capex_any if re.search(conf["expand_re"], x)]
    price = [x for x in sents if re.search(conf["price_re"], x, re.I) and not re.search(conf["price_exclude_re"], x)]
    down = warn_hits([x for x in price if re.search(conf["down_re"], x)], conf["down_words"])
    if not capex_any:
        return {"status": UNK, "why": "전문에 설비 투자 문장 없음" + (", 판가 하락 언급은 있음" if down else ""),
                "evidence": down[0] if down else None}
    if capex and down:
        return {"status": WARN, "why": "설비 투자 확대와 판가 하락 암시가 같이 나옴", "evidence": down[0], "capex": capex[0]}
    if capex:
        return {"status": OK, "why": "설비 투자 확대 문장은 있으나 판가 하락 암시 없음", "evidence": capex[0]}
    if down:
        return {"status": OK, "why": "판가 하락 언급은 있으나 설비 투자 확대 문장 없음", "evidence": down[0]}
    return {"status": OK, "why": "설비 투자 확대와 판가 하락 암시 모두 없음", "evidence": capex_any[0]}


def judge_asp(text: str | None, drop_pct: float) -> dict:
    """분기 ASP 10% 이상 하락 (DRAM or NAND QoQ <= drop_pct). Uses asp_pick (same parser as [SECQ3])."""
    if not text:
        return {"status": UNK, "why": "컨콜 전문 없음", "dram": None, "nand": None, "evidence": None}
    a = asp_pick(text)
    d, n = a.get("dram"), a.get("nand")
    hit = [x for x in (d, n) if x and x["pct"] <= drop_pct]
    if hit:
        return {"status": WARN, "why": f"분기 ASP {drop_pct:g}% 이하", "dram": d, "nand": n, "evidence": hit[0]["sent"]}
    if d and n:
        return {"status": OK, "why": f"D램, 낸드 모두 {drop_pct:g}% 하락보다 양호", "dram": d, "nand": n, "evidence": d["sent"]}
    miss = "D램" if not d else "낸드"
    return {"status": UNK, "why": f"전문에 {miss} ASP 문장 없음", "dram": d, "nand": n, "evidence": (d or n or {}).get("sent")}


def china_news(conf: dict, now: datetime, reader=None) -> dict:
    """Google News RSS (free, no key) titles in the last window_days that name YMTC/CXMT and carry a shipment /
    price-pressure keyword. Keyword-based only; the message says the final judgment is manual."""
    reader = reader or (lambda u: fetch(u, timeout=20))
    try:
        raw = reader(conf["rss"])
    except Exception as exc:
        return {"status": UNK, "fail": str(exc)[:80], "hits": [], "n": 0}
    if raw is None:
        return {"status": UNK, "fail": "RSS 없음", "hits": [], "n": 0}
    s = raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else raw
    since = now - timedelta(days=int(conf.get("window_days", 30)))
    hits, seen = [], set()
    for it in re.findall(r"<item>(.*?)</item>", s, re.S):
        tm = re.search(r"<title>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</title>", it, re.S)
        pm = re.search(r"<pubDate>(.*?)</pubDate>", it)
        if not tm:
            continue
        title = unescape(unescape(tm.group(1))).strip()
        try:
            pub = parsedate_to_datetime(pm.group(1)).astimezone(KST) if pm else None
        except (TypeError, ValueError):
            pub = None
        if pub is None or pub < since or pub > now + timedelta(hours=1):
            continue
        if not re.search(conf["names_re"], title, re.I):
            continue
        kw = next((w for w in conf["warn_words"] if w in title), None)
        if not kw:
            continue
        key = re.sub(r"\s+-\s+[^-]+$", "", title)
        if key in seen:
            continue
        seen.add(key)
        hits.append({"title": title, "day": pub.strftime("%m-%d"), "kw": kw, "pub": pub.isoformat()})
    hits.sort(key=lambda h: h["pub"], reverse=True)
    return {"status": WARN if hits else OK, "fail": None, "hits": hits, "n": len(hits)}


def judge_item3(asp: dict, china: dict) -> dict:
    """Worst of the ASP part (automatic) and the China part (keyword-based): 경계 > 확인 불가 > 문제없음."""
    parts = [asp["status"], china["status"]]
    status = WARN if WARN in parts else UNK if UNK in parts else OK
    return {"status": status, "asp": asp, "china": china, "why": f"ASP {asp['status']}, 중국 {china['status']}(키워드)",
            "evidence": asp.get("evidence"), "china_only": status == WARN and asp["status"] != WARN}


def conclusion(items: list[dict], trims: str, china_counts: bool = False) -> tuple[str, str]:
    """(title tail, 지금 할 일 line) from the plan rule. items: [{'no','status', ...}].
    A 경계 that comes only from the China news keywords (item 3 china_only) is a keyword warning that needs a manual
    read, so by default it holds new buying (like 확인 불가) instead of the full plan-rule action. configs
    earn_auto.item3.china.counts_as_negative=true makes it a full 경계."""
    soft = [str(i["no"]) for i in items if i["status"] == WARN and i.get("china_only") and not china_counts]
    warn = [str(i["no"]) for i in items if i["status"] == WARN and str(i["no"]) not in soft]
    unk = [str(i["no"]) for i in items if i["status"] == UNK]
    if warn:
        line = (f"자동 결론: 경계 {', '.join(warn)}번. 계획대로 추가 매수 중단, 축소선({trims})을 기다리지 말고 비중 축소 전환을 검토하세요.")
        if unk:
            line += f" 확인 불가 {', '.join(unk)}번도 직접 확인하세요."
        return "추가 매수 중단, 비중 축소 검토", line
    if soft or unk:
        parts = ([f"{', '.join(soft)}번 중국 뉴스 키워드 경계"] if soft else []) + ([f"확인 불가 {', '.join(unk)}번"] if unk else [])
        return ("추가 매수 보류, 중국 기사 확인" if soft and not unk else "추가 매수 보류"), (
            f"자동 결론: {', '.join(parts)}. 직접 확인할 때까지 추가 매수 보류. "
            f"확인해서 부정적이면 추가 매수 중단, 축소선({trims})을 기다리지 말고 비중 축소 전환 검토. 문제없으면 기존 계획 유지.")
    return "기존 계획 유지", "자동 결론: 3개 모두 문제없음. 기존 매수선, 축소선 계획 유지."
