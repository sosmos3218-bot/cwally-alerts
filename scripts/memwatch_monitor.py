#!/usr/bin/env python3
"""[MEMWATCH] D램/낸드 분기 계약가 -10% 와 CXMT HBM 해외 고객 인증 감시. 평일 09:00 KST morning 묶음 (hbmchk, hbmasp 옆).

삼성전자(005930) 계획 규칙: 분기 D램 또는 낸드 계약가가 전분기 대비 10% 이상 하락하거나, CXMT HBM3E 가 중국 밖 고객
인증을 통과하거나, 수율 50% 이상 보도가 여러 곳이면 '추가 매수 보류' 재적용 (SK하이닉스 000660 도 같은 영향).

A) 계약가 (모두 무료, 키 없음)
  A1. 트렌드포스 프레스센터 Semiconductors 목록 1~2쪽. 제목이 메모리 가격 기사면 본문 앞부분에서 범용(conventional/overall)
      D램, 낸드의 분기(QoQ) 수치를 읽음. 전망(projected/expected ...) 하락 범위 큰 쪽이 10% 이상이면 '경계' p4
      (범위 전체가 10% 이상이면 본문에 그렇게 적음). 지난 분기 집계(plunged/fell ...) 10% 이상 하락이면 '확정' p5.
      PC/서버/모바일 D램, SSD, 웨이퍼, 현물(spot), HBM 같은 세부 품목 숫자는 판정에 안 씀.
  A2. 트렌드포스 무료 계약가 표 (D램 DDR4 8Gb 1Gx8, 낸드 128Gb 16Gx8 MLC). 월별 평균값을 state 에 쌓아 최근 달 대비
      3개월 전 달 변화율이 -10% 이하이면 '확정' p5 (에피소드당 1번, -5% 위로 돌아오면 재무장). 무료 표는 1~2달 늦게 갱신.
  A3. 구글 뉴스 RSS (ko, en) 제목: D램/낸드 + 가격 단어 바로 뒤 10% 이상 숫자 + 하락 단어면 기사마다 1번 p4 (키워드).
B) CXMT (구글 뉴스 RSS ko, en 제목 키워드, 기사마다 1번 p4)
  B1. 인증: CXMT 이름 + HBM + 인증 단어 + 중국 밖 고객 이름. 부정/부인 단어, 삼성/SK하이닉스/마이크론 이름이 같이 있으면 뺌.
  B2. 수율: CXMT + HBM + 수율 단어 옆 50~99%. 목표/계획/전망 표현과 부정 단어는 뺌. 최근 30일 서로 다른 출처 수 표시.
첫 실행(state 에 seeded_at 없음)은 지금 보이는 기사/수치를 조용히 기록만 함 (무료 표 과거값은 Wayback 스냅샷으로 채움).
조회/파싱 실패는 cwally-ops 하루 1번. 매매 지시가 아님.
modes: auto | dry(발송 없음, state 안 씀) | seed(발송 없음, 기록만, state 씀) | selftest(가짜 값, 네트워크 없음)
"""
from __future__ import annotations

import gzip
import json
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "memwatch.json"
STATE_PATH = ROOT / "state" / "memwatch.json"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      "Accept-Language": "en-US,en;q=0.9,ko;q=0.8"}
KEEP_DAYS = 150

# ---------- 공통 ----------
BANNED = {"\u00b7": ", ", "\u2027": ", ", "\u30fb": ", ", "\u318d": ", ", "\u2022": ", ", "\u2014": "-", "\u2015": "-"}


def clean(s: str) -> str:
    """Korean text rule: no middle dot, no em-dash (also inside quoted headlines)."""
    for k, v in BANNED.items():
        s = s.replace(k, v)
    return re.sub(r" {2,}", " ", s)


def fetch(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", errors="ignore")


def gnews_url(q: str, lang: str) -> str:
    p = {"ko": {"hl": "ko", "gl": "KR", "ceid": "KR:ko"}, "en": {"hl": "en-US", "gl": "US", "ceid": "US:en"}}[lang]
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": q, **p})


def html_text(s: str) -> str:
    s = re.sub(r"<script.*?</script>|<style.*?</style>", " ", s, flags=re.S | re.I)
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def norm_key(title: str) -> str:
    t = re.sub(r"\s+-\s+[^-]{1,40}$", "", title or "")
    return re.sub(r"[^0-9a-z\uac00-\ud7a3\u4e00-\u9fff]+", "", t.lower())[:120]


def parse_rss(xml: str) -> list[dict]:
    out = []
    for it in re.findall(r"<item>(.*?)</item>", xml, re.S):
        tm = re.search(r"<title>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</title>", it, re.S)
        if not tm:
            continue
        title = unescape(unescape(tm.group(1))).strip()
        lm = re.search(r"<link>(.*?)</link>", it, re.S)
        pm = re.search(r"<pubDate>(.*?)</pubDate>", it)
        sm = re.search(r"<source[^>]*>(.*?)</source>", it, re.S)
        try:
            pub = parsedate_to_datetime(pm.group(1)).astimezone(KST) if pm else None
        except (TypeError, ValueError):
            pub = None
        src = unescape(sm.group(1)).strip() if sm else (title.rsplit(" - ", 1)[1].strip() if " - " in title else "")
        out.append({"title": title, "link": (lm.group(1).strip() if lm else ""), "pub": pub, "source": src})
    return out


def month_add(ym: str, n: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    k = y * 12 + (m - 1) + n
    return f"{k // 12:04d}-{k % 12 + 1:02d}"


def fmt_rng(lo: float, hi: float, d: int) -> str:
    sign = "-" if d < 0 else ("+" if d > 0 else "")
    if lo == hi:
        return f"{sign}{lo:g}%"
    return f"{sign}{lo:g}~{hi:g}%"


PROD_KO = {"dram": "D램", "nand": "낸드"}

# ---------- A1 트렌드포스 본문 수치 ----------
PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%?\s*(?:~|\u2013|\u2014|-|to)\s*(\d+(?:\.\d+)?)\s*%|(\d+(?:\.\d+)?)\s*%")
DOWN_RE = re.compile(r"declin|\bfall|\bfell\b|\bdrop|decreas|plung|slump|\blower\b|\breduc|\bslid|\bdip|\bdown\b|\bcuts?\b|\bslash", re.I)
UP_RE = re.compile(r"increas|\brise|\brising\b|\brose\b|\bgrow|growth|\bhike|climb|surg|\bgain|rebound|\bhigher\b|\bsoar|\bjump|\bup\b|uptick", re.I)
FORECAST_RE = re.compile(r"project|expect|forecast|predict|\bwill\b|estimat|likely|anticipat|\bcould\b|\bmay\b|outlook|\bto (?:fall|rise|grow|increase|decline|drop|climb)|\bseen\b|\bset to\b", re.I)
PAST_RE = re.compile(r"\b(?:fell|dropped|declined|plunged|plummeted|decreased|slid|rose|increased|grew|jumped|surged|climbed|registered|recorded|posted)\b", re.I)
PRICE_RE = re.compile(r"price|ASP", re.I)
QTR_RE = re.compile(r"\b([1-4])Q(\d{2})\b")
QOQ_RE = re.compile(r"QoQ|quarter|\b[1-4]Q\d{2}\b", re.I)
SEG_PRE_RE = re.compile(r"(?:PC|server|mobile|graphics|consumer|specialty|HBM\d?E?|DDR\d|LPDDR\d*X?|automotive|enterprise|client|embedded|legacy)[\s-]*$", re.I)
SEG_POST_RE = re.compile(r"^(?:\s+Flash)?\s+(?:wafer|module|card|SSD|spot)", re.I)
PROD_RE = re.compile(r"\b(DRAM|NAND)\b")


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z\"\u201c])", text) if s.strip()]


def split_clauses(sent: str) -> list[str]:
    return [c for c in re.split(r",\s*(?:while|whereas|and|but)\s+|;\s*|\s+while\s+|\s+whereas\s+|,\s+with\s+", sent) if c and c.strip()]


def prod_mentions(clause: str) -> list[tuple[int, str, bool]]:
    out = []
    for m in PROD_RE.finditer(clause):
        pre = clause[max(0, m.start() - 22):m.start()]
        post = clause[m.end():m.end() + 25]
        seg = bool(SEG_PRE_RE.search(pre)) and not re.search(r"(?:conventional|overall|general)[\s-]*$", pre, re.I)
        seg = seg or bool(SEG_POST_RE.search(post))
        out.append((m.start(), m.group(1).lower(), not seg))
    return out


def parse_tf_figures(text: str, title_q: str = "") -> list[dict]:
    """Quarterly DRAM/NAND contract price figures in TrendForce text (title + lead). overall=False for segment items."""
    figs: list[dict] = []
    seen = set()
    for sent in split_sentences(text):
        if not PRICE_RE.search(sent) or "spot" in sent.lower():
            continue
        if re.search(r"\bYoY\b|year-over-year|annual", sent, re.I) and not QOQ_RE.search(sent):
            continue
        sq = QTR_RE.search(sent)
        carry = None
        for clause in split_clauses(sent):
            mentions = prod_mentions(clause)
            for pm in PCT_RE.finditer(clause):
                prev = [x for x in mentions if x[0] < pm.start()]
                prod = prev[-1] if prev else carry
                if not prod:
                    continue
                if pm.group(3):
                    lo = hi = float(pm.group(3))
                else:
                    lo, hi = sorted((float(pm.group(1)), float(pm.group(2))))
                if hi > 100:
                    continue
                win_lo = 0
                window = clause[win_lo:pm.end() + 25]
                best, bd = 0, 10 ** 9
                for rx, d in ((DOWN_RE, -1), (UP_RE, 1)):
                    for dm in rx.finditer(window):
                        dist = min(abs(dm.start() - pm.start()), abs(dm.start() - pm.end()))
                        if dist < bd:
                            best, bd = d, dist
                if best == 0:
                    continue
                if not (QOQ_RE.search(clause) or QOQ_RE.search(sent) or title_q):
                    continue
                kind = "forecast" if FORECAST_RE.search(clause) else ("actual" if PAST_RE.search(clause) else "forecast")
                qm = QTR_RE.search(clause) or sq
                q = f"{qm.group(1)}Q{qm.group(2)}" if qm else title_q
                key = (prod[1], lo, hi, best, kind, prod[2])
                if key in seen:
                    continue
                seen.add(key)
                figs.append({"prod": prod[1], "overall": prod[2], "lo": lo, "hi": hi, "dir": best, "kind": kind,
                             "q": q, "sent": clean(sent[:260])})
            if mentions:
                carry = mentions[-1]
    return figs


def tf_hits(figs: list[dict], drop: float) -> list[dict]:
    """Overall DRAM/NAND figures that meet the rule. forecast: larger decline end >= |drop| (경계 p4).
    actual (reported past quarter): decline >= |drop| (확정 p5)."""
    lim = abs(drop)
    out = []
    for f in figs:
        if not f["overall"] or f["dir"] >= 0:
            continue
        if f["kind"] == "actual" and f["lo"] >= lim:
            out.append({**f, "level": "확정", "priority": 5})
        elif f["kind"] == "forecast" and f["hi"] >= lim:
            out.append({**f, "level": "경계", "priority": 4, "full": f["lo"] >= lim})
    return out


def tf_list(html: str, base: str) -> list[dict]:
    out, seen = [], set()
    for m in re.finditer(r'href="(/presscenter/news/(\d{8})-(\d+)\.html)"', html):
        if m.group(3) in seen:
            continue
        seen.add(m.group(3))
        seg = html[m.end():m.end() + 900]
        t = html_text(seg)
        t = re.split(r"\s\d{1,2} (?:January|February|March|April|May|June|July|August|September|October|November|December) \d{4}\b", t)[0]
        t = t.lstrip("> ").strip()
        d = m.group(2)
        out.append({"id": m.group(3), "date": f"{d[:4]}-{d[4:6]}-{d[6:]}", "url": base + m.group(1), "title": t[:220]})
    return out


def tf_lead(html: str, title: str, n: int) -> str:
    t = html_text(html)
    i = t.find("Home Press Center")
    if i < 0:
        i = t.find(title[:40]) if title else -1
    if i < 0:
        return ""
    return t[i + len("Home Press Center"):i + n]


# ---------- A2 트렌드포스 무료 계약가 표 ----------
TABLE_HEAD = {
    "dram": r"DRAM Contract Price \((\w+ \w+)\)",
    "nand": r"NAND Flash Contract Price in Memory Card/UFD \((\w+ \w+)\)",
}


MONTHS = {k: i for i, k in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def parse_table(html: str, prod: str, item: str) -> dict | None:
    t = html_text(html)
    m = re.search(TABLE_HEAD[prod] + r".{0,400}?Last Update (\d{4})-(\d{2})-(\d{2})", t)
    if not m:
        return None
    seg = t[m.end():m.end() + 1500]
    r = re.search(re.escape(item) + r"\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*([\u25b2\u25bc\u2014-]?)\s*(-?[\d.]+)\s*%", seg)
    if not r:
        return None
    avg, chg = float(r.group(3)), float(r.group(5))
    if r.group(4) == "\u25bc" and chg > 0:
        chg = -chg
    y, um = int(m.group(2)), int(m.group(3))
    mon = MONTHS.get(m.group(1).split()[-1][:3].lower(), um)  # period label month ("2H Aug"), year from Last Update
    if mon > um + 1:
        y -= 1
    return {"label": m.group(1), "month": f"{y:04d}-{mon:02d}", "avg": avg, "chg": chg}


def add_point(series: dict, p: dict, src: str) -> None:
    cur = series.get(p["month"])
    if not cur or cur.get("src") == "derived" or src == "live":
        series[p["month"]] = {"v": p["avg"], "src": src, "label": p["label"]}
    prev = month_add(p["month"], -1)
    if prev not in series and p["chg"] > -100:
        series[prev] = {"v": round(p["avg"] / (1 + p["chg"] / 100), 4), "src": "derived", "label": f"{p['label']} 전기 대비 역산"}


def rolling(series: dict, months: int) -> dict | None:
    if not series:
        return None
    last = max(series)
    base = month_add(last, -months)
    if base not in series:
        return {"last": last, "base": base, "chg": None, "v": series[last]["v"]}
    v, b = series[last]["v"], series[base]["v"]
    return {"last": last, "base": base, "chg": round((v / b - 1) * 100, 2), "v": v, "b": b}


def wayback_points(url: str, prod: str, item: str, since: str) -> list[dict]:
    cdx = ("https://web.archive.org/cdx/search/cdx?" + urllib.parse.urlencode(
        {"url": url.split("://", 1)[1], "from": since, "output": "json", "filter": "statuscode:200", "collapse": "timestamp:6"}))
    rows = json.loads(fetch(cdx, timeout=40) or "[]")[1:]
    out = []
    for r in rows[-8:]:
        p = None
        for attempt in range(2):  # web.archive.org rate-limits (429); one slow retry, then skip that month
            time.sleep(3 + 12 * attempt)
            try:
                p = parse_table(fetch(f"https://web.archive.org/web/{r[1]}id_/{r[2]}", timeout=40), prod, item)
                break
            except Exception as exc:  # noqa: BLE001
                print(f"wayback {prod} {r[1]} try {attempt + 1} fail: {exc}")
        if p:
            out.append(p)
    return out


# ---------- A3 뉴스 제목 (계약가 하락) ----------
KO_DOWN = re.compile(r"^\s*(?:대\s*)?(?:이상\s*)?(?:하락|급락|떨어|내려|내린|↓|감소|인하|뚝|추락|폭락)")
EN_DOWN = re.compile(r"(?:fall|fell|drop|declin|plung|slid|slump|down|cut|lower|decreas)\w*\s+(?:by\s+|of\s+|as much as\s+|more than\s+|over\s+|up to\s+|around\s+)?$", re.I)
NEWS_PRICE = re.compile(r"가격|고정거래|계약가|단가|ASP|price", re.I)
NEWS_MEM = re.compile(r"D램|DRAM|낸드|NAND", re.I)
NEWS_SKIP = re.compile(r"주가|주식|증시|코스피|시총|시가총액|\bshares?\b|\bstocks?\b|\bADR\b|현물|\bspot\b|HBM|스마트폰|노트북|출하", re.I)
NEG_RE = re.compile(r"실패|탈락|무산|불발|과도|어렵|아니|부인|반박|루머|\bfail|\bdeni|\bdeny|\bnot\b|n't\b|unlikely|struggl|reject|\brumou?r", re.I)


def price_news_hit(title: str, drop: float) -> dict | None:
    t = re.sub(r"\s+-\s+[^-]{1,40}$", "", title)
    if not NEWS_MEM.search(t) or NEWS_SKIP.search(t) or NEG_RE.search(t):
        return None
    lim = abs(drop)
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(?:%\s*)?(?:~|-|\u2013)?\s*(\d+(?:\.\d+)?)?\s*%(\s*대)?|두\s*자릿수|두자릿수|double[- ]digit", t, re.I):
        if m.group(0)[0] in "두dD":
            hi = 10.0
        else:
            nums = [float(x) for x in (m.group(1), m.group(2)) if x]
            hi = max(nums)
        before, after = t[max(0, m.start() - 22):m.start()], t[m.end():m.end() + 8]
        if not NEWS_PRICE.search(before):
            continue
        if re.search(r"상승|급등|올라|오른|↑|인상|증가", after) or UP_RE.search(before[-12:]):
            continue
        if not (KO_DOWN.search(after) or EN_DOWN.search(before)):
            continue
        if hi < lim:
            continue
        pre = t[:m.start()]
        dpos = max(pre.rfind("D램"), pre.upper().rfind("DRAM"))
        npos = max(pre.rfind("낸드"), pre.upper().rfind("NAND"))
        prod = "dram" if dpos >= npos and dpos >= 0 else ("nand" if npos >= 0 else "dram")
        return {"prod": prod, "pct": hi, "match": clean(m.group(0).strip())}
    return None


# ---------- B CXMT ----------
CX_NAME = re.compile(r"CXMT|ChangXin|Changxin|창신|长鑫", re.I)
CX_HBM = re.compile(r"HBM|고대역폭", re.I)
CX_QUAL = re.compile(r"인증|퀄|qualif|certif|공급\s?승인|납품|승인|공급\s?계약|passe[sd]|clear(?:s|ed)\b|approv|supply (?:deal|agreement)|수주|wins? order|도입|채택|adopt|select(?:s|ed)\b", re.I)
CX_CUST = re.compile(r"엔비디아|Nvidia|\bAMD\b|구글|Google|브로드컴|Broadcom|애플|Apple|마이크로소프트|Microsoft|아마존|Amazon|\bAWS\b|메타|\bMeta\b|인텔|Intel|퀄컴|Qualcomm|테슬라|Tesla|오픈AI|OpenAI|마벨|Marvell|시스코|Cisco|오라클|Oracle", re.I)
CX_COMP = re.compile(r"삼성|하이닉스|\bSK\b|마이크론|Samsung|Hynix|Micron", re.I)
CX_YIELD = re.compile(r"수율|yields?\b", re.I)
CX_YIELD_NEG = re.compile(r"목표|계획|추진|전망|예상|기대|노린|노려|불량|target|\baims?\b|goal|\bplans?\b|expect|hopes?|\bcould\b|\bwould\b|\bmay\b|forecast|defect|\bonly\b|below|under|미만|그쳐|불과|뿐", re.I)


def cx_qual_hit(title: str) -> dict | None:
    t = re.sub(r"\s+-\s+[^-]{1,40}$", "", title)
    if not (CX_NAME.search(t) and CX_HBM.search(t)):
        return None
    q, c = CX_QUAL.findall(t), CX_CUST.findall(t)
    if not q or not c or NEG_RE.search(t) or CX_COMP.search(t):
        return None
    words = sorted({w.strip() for w in [CX_NAME.search(t).group(0), CX_HBM.search(t).group(0), *q, *c]})
    return {"words": words}


def cx_yield_hit(title: str, min_pct: float) -> dict | None:
    t = re.sub(r"\s+-\s+[^-]{1,40}$", "", title)
    nm = CX_NAME.search(t)
    if not (nm and CX_HBM.search(t)) or NEG_RE.search(t) or CX_YIELD_NEG.search(t):
        return None
    comp = CX_COMP.search(t)
    if comp:  # numbers after a competitor name belong to that competitor
        t = t[:comp.start()] if nm.start() < comp.start() else t[nm.start():]
    for ym in CX_YIELD.finditer(t):
        zone = t[max(0, ym.start() - 18):ym.end() + 18]
        for pm in re.finditer(r"(\d{2})(?:\s*%?\s*(?:~|-|\u2013)\s*(\d{2}))?\s*%", zone):
            lo = float(pm.group(1))
            if min_pct <= lo < 100:
                return {"pct": lo, "words": sorted({nm.group(0), ym.group(0), pm.group(0).strip()})}
    return None


# ---------- 본문 ----------
ACT_CONFIRMED = ("삼성전자, SK하이닉스 추가 매수 보류(매수선 알림 따르지 말 것). 원문 숫자가 맞고 흐름이 부정적이면 축소선을 기다리지 말고 "
                 "비중 축소 전환 검토. 매매 지시 아님.")
ACT_KEYWORD = ("기사 원문을 직접 읽고 사실인지 확인. 확인되면 삼성전자, SK하이닉스 추가 매수 보류(매수선 알림 따르지 말 것), 부정적이면 "
               "축소선을 기다리지 말고 비중 축소 전환 검토. 매매 지시 아님.")
LATER = "같은 기사, 같은 수치는 다시 안 보냅니다. 평일 09:00 에 새 트렌드포스 전망, 계약가 표, 새 기사만 봅니다."
TRIM = "이 알림만으로 바로 팔지 말고, 삼성전자/SK하이닉스 계획의 축소선과 같이 보기. 확인되면 축소 전환 검토가 먼저입니다."
AVOID = "제목 키워드만 보고 매매, 추격 매수, 신용/미수, 부정 기사(실패, 무산, 부인)를 확정으로 읽기."


def body(stamp: str, head: str, what: str, status: str, action: str, ref: str) -> str:
    return clean("\n".join([f"{stamp} | MEMWATCH | {head}", f"이게 뭐 알림: {what}", f"지금 결과/상태: {status}", f"지금 할 일: {action}",
                            f"나중에: {LATER}", f"익절/줄일 때: {TRIM}", f"하지 말 것: {AVOID}", f"참고: {ref}"]))


def latest_line(st: dict) -> str:
    lt = st.get("tf_latest") or {}
    parts = []
    for p in ("dram", "nand"):
        f = lt.get(p)
        if f:
            parts.append(f"{PROD_KO[p]} {f.get('q') or '분기 미표기'} {fmt_rng(f['lo'], f['hi'], f['dir'])} ({'집계' if f['kind'] == 'actual' else '전망'}, {f['date']})")
    return "트렌드포스 최근 분기 수치: " + (", ".join(parts) if parts else "기록 없음")


def ref_line(st: dict) -> str:
    rl = st.get("rolling") or {}
    px = []
    for p in ("dram", "nand"):
        r = rl.get(p)
        if r:
            px.append(f"{PROD_KO[p]} {r['last']} {r['v']:g}달러" + (f", 3개월 {r['chg']:+.1f}%" if r.get("chg") is not None else ", 3개월 비교 기록 부족"))
    return (latest_line(st) + ". 무료 계약가 표: " + (", ".join(px) if px else "기록 없음")
            + ". 2026-10 기준선: 4Q26 범용 D램 +10~15%, 낸드 +15~20% 전망, CXMT HBM3E 수율 20~30%, 고객 중국 내 설계사뿐.")


def tf_msg(stamp: str, art: dict, hit: dict, st: dict) -> tuple[str, str, int]:
    p, q = PROD_KO[hit["prod"]], hit.get("q") or "분기"
    rng = fmt_rng(hit["lo"], hit["hi"], -1)
    if hit["level"] == "확정":
        title = f"[MEMWATCH] 005930/000660 트렌드포스 {q} {p} 계약가 {rng} 집계 | 확정, 추가 매수 보류"
        what = f"트렌드포스가 {p} 분기 계약가(전분기 대비) 10% 이상 하락을 집계했다는 알림. 삼성전자 계획 규칙의 추가 매수 보류 조건입니다."
        action = ACT_CONFIRMED
    else:
        title = f"[MEMWATCH] 005930/000660 트렌드포스 {q} {p} 계약가 {rng} 전망 | 경계, 추가 매수 보류 검토"
        what = (f"트렌드포스 분기 전망에서 {p} 계약가 하락 범위 큰 쪽이 10% 이상이라는 알림. 전망이라 '경계'입니다"
                + (" (범위 전체가 -10% 이하)." if hit.get("full") else " (범위 작은 쪽은 10% 미만)."))
        action = "원문 전망 숫자를 직접 확인. " + ACT_CONFIRMED
    status = f"{art['date']} 트렌드포스 '{art['title'][:120]}'. 문장: \"{hit['sent'][:220]}\". 링크: {art['url']}"
    return title, body(stamp, f"{p} 분기 계약가 {hit['level']}", what, status, action, ref_line(st)), hit["priority"]


def table_msg(stamp: str, prod: str, r: dict, cfg: dict, st: dict) -> tuple[str, str, int]:
    p = PROD_KO[prod]
    label = cfg["price"]["tables"][prod]["label"]
    title = f"[MEMWATCH] 005930/000660 {p} 고정거래가 3개월 {r['chg']:+.1f}% | 확정, 추가 매수 보류"
    status = (f"{label}: {r['last']} 평균 {r['v']:g}달러, {r['base']} {r['b']:g}달러 대비 {r['chg']:+.1f}% (기준 -10% 이하). "
              f"링크: {cfg['price']['tables'][prod]['url']}")
    what = f"{p} 계약(고정거래) 가격이 3개월 누적 10% 이상 내렸다는 알림. 분기 계약가 -10% 규칙에 해당합니다. 무료 표는 1~2달 늦게 갱신됩니다."
    return title, body(stamp, f"{p} 계약가 3개월 누적 하락 확정", what, status, ACT_CONFIRMED, ref_line(st)), 5


def items_lines(items: list[dict], extra) -> str:
    rows = []
    for i, it in enumerate(items[:6], 1):
        day = it["pub"].strftime("%Y-%m-%d") if it.get("pub") else "날짜 없음"
        rows.append(f"{i}) {it['title'][:150]} ({it.get('source') or '출처 미상'}, {day}) 매칭: {extra(it)} 링크: {it['link']}")
    if len(items) > 6:
        rows.append(f"외 {len(items) - 6}건")
    return " ".join(rows)


def news_msg(stamp: str, items: list[dict], st: dict) -> tuple[str, str, int]:
    title = f"[MEMWATCH] 005930/000660 메모리 계약가 10% 이상 하락 보도 {len(items)}건 | 원문 확인, 추가 매수 보류 검토"
    what = "D램/낸드 가격 10% 이상 하락이 기사 제목에 나왔다는 알림. 제목 키워드 매치라 원문을 직접 읽어야 합니다."
    status = items_lines(items, lambda it: f"{PROD_KO[it['hit']['prod']]} {it['hit']['match']}")
    return title, body(stamp, "계약가 하락 보도 (키워드)", what, status, ACT_KEYWORD, ref_line(st)), 4


def cx_msg(stamp: str, items: list[dict], st: dict) -> tuple[str, str, int]:
    title = f"[MEMWATCH] 005930/000660 CXMT HBM 해외 고객 인증 보도 {len(items)}건 | 원문 확인, 추가 매수 보류 검토"
    what = "CXMT(창신메모리) HBM 이 중국 밖 고객 인증이나 납품을 받았다는 제목이 나왔다는 알림. 제목 키워드 매치라 원문을 직접 읽어야 합니다."
    status = items_lines(items, lambda it: ", ".join(it["hit"]["words"]))
    return title, body(stamp, "CXMT 해외 고객 인증 (키워드)", what, status, ACT_KEYWORD, ref_line(st)), 4


def cy_msg(stamp: str, items: list[dict], nsrc: int, need: int, days: int, st: dict) -> tuple[str, str, int]:
    title = f"[MEMWATCH] 005930/000660 CXMT HBM 수율 50% 이상 보도 {len(items)}건 (출처 {nsrc}곳) | 원문 확인"
    what = "CXMT HBM 수율이 50% 이상이라는 제목이 나왔다는 알림. 제목 키워드 매치라 원문을 직접 읽어야 합니다."
    multi = f"최근 {days}일 서로 다른 출처 {nsrc}곳, 여러 보도 조건({need}곳 이상) " + ("충족" if nsrc >= need else "아직 미충족")
    status = multi + ". " + items_lines(items, lambda it: ", ".join(it["hit"]["words"]))
    return title, body(stamp, "CXMT HBM 수율 (키워드)", what, status, ACT_KEYWORD, ref_line(st)), 4


def ops_msg(stamp: str, fails: list[str]) -> tuple[str, str, int]:
    title = "[MEMWATCH] 005930/000660 데이터 수집 실패 | 일부 판정 보류"
    b = clean("\n".join([f"{stamp} | MEMWATCH | 데이터 수집 안내", "이게 뭐 알림: MEMWATCH 데이터 조회/파싱 실패 안내. 매매 신호 아님.",
                         "지금 결과/상태: " + " / ".join(fails), "지금 할 일: 필요하면 트렌드포스 프레스센터와 기사 검색을 직접 보세요.",
                         "나중에: 다음 평일 09:00 에 다시 봅니다. 같은 날 같은 안내는 1번만.", "익절/줄일 때: 이 안내로 비중을 바꾸지 않기.",
                         "하지 말 것: 조회 실패를 조건 미충족(문제없음)으로 읽기."]))
    return title, b, 3


# ---------- state ----------
def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def prune(st: dict, now: datetime) -> None:
    cut = (now - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    for k in ("news_seen", "cx_seen"):
        st[k] = {a: b for a, b in (st.get(k) or {}).items() if (b.get("day") or "9999") >= cut}
    tf = st.get("tf_seen") or {}
    if len(tf) > 200:
        st["tf_seen"] = dict(sorted(tf.items(), key=lambda kv: kv[1].get("date", ""))[-200:])
    st["yield_hits"] = [h for h in st.get("yield_hits") or [] if h.get("day", "9999") >= cut]


# ---------- run ----------
def gather_rss(queries: list[dict], reader) -> tuple[list[dict], list[str]]:
    items, errs, seen = [], [], set()
    for q in queries:
        url = gnews_url(q["q"], q["lang"])
        try:
            got = parse_rss(reader(url))
        except Exception as exc:  # noqa: BLE001
            errs.append(f"{q['lang']} '{q['q'][:40]}': {str(exc)[:80]}")
            continue
        for it in got:
            k = norm_key(it["title"])
            if k and k not in seen:
                seen.add(k)
                it["key"] = k
                items.append(it)
    if len(errs) < len(queries):
        errs = []  # 일부 실패는 다른 검색으로 채움, 전부 실패만 ops
    return items, errs


def run(mode: str, now: datetime, cfg: dict, reader=None) -> dict:
    reader = reader or fetch
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    today = now.strftime("%Y-%m-%d")
    st = load_state()
    seeding = mode == "seed" or not st.get("seeded_at")
    pc = cfg["price"]
    drop = float(pc["drop_pct"])
    out: list[tuple[str, str, int]] = []
    fails: list[tuple[str, str]] = []
    seed_triggers: list[str] = []
    rep: dict = {"tf_articles": 0, "tf_price_articles": [], "news_matches": [], "cx_qual": [], "cx_yield": []}

    # A1 TrendForce
    arts: list[dict] = []
    for u in pc["tf_list_urls"]:
        try:
            arts.extend(tf_list(reader(u), pc["tf_article_base"]))
        except Exception as exc:  # noqa: BLE001
            fails.append(("tf_list", f"트렌드포스 목록 조회 실패 ({str(exc)[:80]})"))
    uniq = {a["id"]: a for a in arts}
    arts = sorted(uniq.values(), key=lambda a: a["id"])
    rep["tf_articles"] = len(arts)
    if not arts and not any(f[0] == "tf_list" for f in fails):
        fails.append(("tf_list", "트렌드포스 목록 파싱 실패 (기사 0건, 페이지 구조 변경 가능)"))
    tf_seen = st.setdefault("tf_seen", {})
    title_re = re.compile(pc["tf_title_re"], re.I)
    for a in arts:
        is_price = bool(title_re.search(a["title"]))
        if a["id"] in tf_seen and not (seeding and is_price and "figs" not in tf_seen[a["id"]]):
            continue
        rec = {"date": a["date"], "title": a["title"][:160]}
        if is_price:
            try:
                lead = tf_lead(reader(a["url"]), a["title"], int(pc["tf_lead_chars"]))
            except Exception as exc:  # noqa: BLE001
                fails.append(("tf_body", f"트렌드포스 본문 조회 실패 {a['id']} ({str(exc)[:60]})"))
                continue  # not marked seen: retried next run
            tq = QTR_RE.search(a["title"])
            figs = parse_tf_figures(a["title"] + ". " + lead, f"{tq.group(1)}Q{tq.group(2)}" if tq else "")
            overall = [f for f in figs if f["overall"]]
            rec["figs"] = [{k: f[k] for k in ("prod", "lo", "hi", "dir", "kind", "q")} for f in overall][:6]
            rep["tf_price_articles"].append({"id": a["id"], "date": a["date"], "title": a["title"][:120],
                                             "overall": [f"{PROD_KO[f['prod']]} {f['q']} {fmt_rng(f['lo'], f['hi'], f['dir'])} {f['kind']}" for f in overall],
                                             "segment": len(figs) - len(overall)})
            looks_forecast = bool(re.search(r"contract|ASP", a["title"], re.I) and QTR_RE.search(a["title"]))
            if not overall and looks_forecast:
                fails.append(("tf_parse", f"트렌드포스 수치 파싱 실패: '{a['title'][:70]}' {a['url']}"))
            for p in ("dram", "nand"):
                fp = [f for f in overall if f["prod"] == p]
                if fp:
                    f = fp[0]
                    old = (st.get("tf_latest") or {}).get(p)
                    if not old or a["date"] >= old.get("date", ""):
                        st.setdefault("tf_latest", {})[p] = {**{k: f[k] for k in ("lo", "hi", "dir", "kind", "q")}, "date": a["date"], "id": a["id"]}
            for h in tf_hits(figs, drop):
                if seeding:
                    seed_triggers.append(f"TF {a['date']} {PROD_KO[h['prod']]} {fmt_rng(h['lo'], h['hi'], -1)} {h['level']}: {a['title'][:80]}")
                else:
                    out.append(tf_msg(stamp, a, h, st))
        tf_seen[a["id"]] = rec

    # A2 contract price tables
    series_all = st.setdefault("px", {})
    st.setdefault("px_armed", {})
    for prod, tc in pc["tables"].items():
        series = series_all.setdefault(prod, {})
        try:
            p = parse_table(reader(tc["url"]), prod, tc["item"])
        except Exception as exc:  # noqa: BLE001
            fails.append((f"table_{prod}", f"{PROD_KO[prod]} 계약가 표 조회 실패 ({str(exc)[:60]})"))
            p = None
        else:
            if not p:
                fails.append((f"table_{prod}", f"{PROD_KO[prod]} 계약가 표 파싱 실패 ({tc['item']} 행 없음)"))
        if seeding and mode != "selftest" and len(series) < 3:
            since = month_add(now.strftime("%Y-%m"), -7).replace("-", "")
            try:
                for wp in wayback_points(tc["url"], prod, tc["item"], since):
                    add_point(series, wp, "wayback")
            except Exception as exc:  # noqa: BLE001
                print(f"wayback backfill {prod} skipped: {exc}")
        if p:
            add_point(series, p, "live")
        r = rolling(series, int(pc["rolling_months"]))
        if r:
            st.setdefault("rolling", {})[prod] = r
            chg = r.get("chg")
            armed = st["px_armed"].get(prod, True)
            if chg is not None and chg <= drop:
                if seeding:
                    seed_triggers.append(f"표 {PROD_KO[prod]} {r['base']}->{r['last']} {chg:+.1f}%")
                    st["px_armed"][prod] = False
                elif armed:
                    out.append(table_msg(stamp, prod, r, cfg, st))
                    st["px_armed"][prod] = False
            elif chg is not None and chg > float(pc["rearm_pct"]):
                st["px_armed"][prod] = True

    # A3 price news
    window = now - timedelta(days=int(pc["news"]["window_days"]))
    items, errs = gather_rss(pc["news"]["queries"], reader)
    if errs:
        fails.append(("news", "가격 기사 검색 조회 실패 (" + "; ".join(errs)[:120] + ")"))
    news_seen = st.setdefault("news_seen", {})
    new_news = []
    for it in items:
        h = price_news_hit(it["title"], drop)
        if not h or (it["pub"] and it["pub"] < window):
            continue
        it["hit"] = h
        rep["news_matches"].append(f"{it['pub'].strftime('%m-%d') if it['pub'] else '?'} {clean(it['title'][:100])} [{h['match']}]")
        if it["key"] in news_seen or it["link"] in {v.get("link") for v in news_seen.values()}:
            continue
        news_seen[it["key"]] = {"day": (it["pub"] or now).strftime("%Y-%m-%d"), "link": it["link"]}
        if seeding:
            seed_triggers.append(f"가격 기사 {clean(it['title'][:90])}")
        else:
            new_news.append(it)
    if new_news:
        out.append(news_msg(stamp, new_news, st))

    # B CXMT
    cc = cfg["cxmt"]
    cwin = now - timedelta(days=int(cc["window_days"]))
    items, errs = gather_rss(cc["queries"], reader)
    if errs:
        fails.append(("cxmt", "CXMT 기사 검색 조회 실패 (" + "; ".join(errs)[:120] + ")"))
    cx_seen = st.setdefault("cx_seen", {})
    yhits = st.setdefault("yield_hits", [])
    new_q, new_y = [], []
    for it in items:
        if it["pub"] and it["pub"] < cwin:
            continue
        qh = cx_qual_hit(it["title"])
        yh = cx_yield_hit(it["title"], float(cc["yield_min_pct"]))
        for kind, h, bucket, rkey in (("q", qh, new_q, "cx_qual"), ("y", yh, new_y, "cx_yield")):
            if not h:
                continue
            rep[rkey].append(f"{it['pub'].strftime('%m-%d') if it['pub'] else '?'} {clean(it['title'][:100])} [{', '.join(h['words'])}]")
            key = f"{kind}:{it['key']}"
            if key in cx_seen or it["link"] in {v.get("link") for v in cx_seen.values() if v.get("kind") == kind}:
                continue
            cx_seen[key] = {"day": (it["pub"] or now).strftime("%Y-%m-%d"), "link": it["link"], "kind": kind}
            if kind == "y":
                yhits.append({"day": (it["pub"] or now).strftime("%Y-%m-%d"), "source": it.get("source") or "?", "key": it["key"]})
            it2 = {**it, "hit": h}
            if seeding:
                seed_triggers.append(f"CXMT {'인증' if kind == 'q' else '수율'} {clean(it['title'][:90])}")
            else:
                bucket.append(it2)
    if new_q:
        out.append(cx_msg(stamp, new_q, st))
    if new_y:
        since = (now - timedelta(days=int(cc["yield_count_days"]))).strftime("%Y-%m-%d")
        nsrc = len({h["source"] for h in yhits if h["day"] >= since})
        out.append(cy_msg(stamp, new_y, nsrc, int(cc["yield_multi_sources"]), int(cc["yield_count_days"]), st))

    # ops: once per day per failure group
    od = st.setdefault("ops_day", {})
    new_f = [(g, m) for g, m in fails if od.get(g) != today]
    if new_f:
        out.append(ops_msg(stamp, [m for _, m in new_f]))
        for g, _ in new_f:
            od[g] = today

    # report
    print(f"MEMWATCH {stamp} mode={mode} seeding={seeding}")
    print(f"TrendForce list: {rep['tf_articles']} articles; price articles parsed this run: {len(rep['tf_price_articles'])}")
    for a in rep["tf_price_articles"]:
        print(f"  TF {a['date']} {a['id']} {a['title']} -> overall {a['overall'] or '없음'} (segment figures {a['segment']})")
    print("  " + latest_line(st))
    for prod in ("dram", "nand"):
        s = (st.get("px") or {}).get(prod) or {}
        print(f"  table {prod}: " + ", ".join(f"{k}={v['v']:g}({v['src'][0]})" for k, v in sorted(s.items())) + f" | rolling {(st.get('rolling') or {}).get(prod)}")
    print(f"price-news matches in window: {len(rep['news_matches'])}")
    for x in rep["news_matches"][:10]:
        print("  " + x)
    print(f"CXMT qualification matches in window: {len(rep['cx_qual'])}")
    for x in rep["cx_qual"][:10]:
        print("  " + x)
    print(f"CXMT yield>=50% matches in window: {len(rep['cx_yield'])}")
    for x in rep["cx_yield"][:10]:
        print("  " + x)
    if fails:
        print("FAILS: " + " / ".join(m for _, m in fails))
    if seeding:
        print(f"SEED: recorded tf={len(tf_seen)} news={len(news_seen)} cxmt={len(cx_seen)}; current items meeting a trigger (not sent): {seed_triggers or '없음'}")

    topic, server = resolve_topic("port", cfg.get("ntfy_topic")), resolve_server(cfg.get("ntfy_server"))
    sent_titles = []
    for t, m, p in out:
        t, m = clean(t), clean(m)
        assert "\u00b7" not in t + m and "\u2014" not in t + m, t
        to = route_topic(topic, t)
        print(f"TITLE: {t}\nPRIORITY: {p} TOPIC: {to}\n{m}\n")
        sent_titles.append((t, p, to))
        if not dry and mode != "selftest" and mode != "seed":
            publish(resolve_topic("ops") if to == "cwally-ops" else topic, t, m, p, ["warning"] if to == "cwally-ops" else ["mag"], server)
    if not out:
        print("no alert")
    res = {"out": out, "titles": sent_titles, "state": st, "seed_triggers": seed_triggers, "rep": rep, "fails": fails}
    if dry or mode == "selftest":
        if dry:
            print("DRY RUN: no publish, state not written")
        return res
    if seeding:
        st["seeded_at"] = now.isoformat()
        st["seed_count"] = {"tf": len(tf_seen), "news": len(news_seen), "cxmt": len(cx_seen)}
    if sent_titles:
        st["last_sent"] = {"at": stamp, "titles": [x[0] for x in sent_titles]}
    prune(st, now)
    st["updated_at"] = now.isoformat()
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return res


# ---------- selftest ----------
TF_LIST_FIX = ('<a href="/presscenter/news/20270105-13400.html"> QoQ Decline in DRAM ASP Will Moderate to Around 13~18% for 1Q27, Says TrendForce 5 January 2027 x</a>'
               '<a href="/presscenter/news/20270106-13401.html"> AI Server Demand Sustains Memory Contract Price Increases in 1Q27, Says TrendForce 6 January 2027</a>'
               '<a href="/presscenter/news/20270107-13402.html"> DRAM Contract Price Outlook for 1Q27 Revised, Says TrendForce 7 January 2027</a>'
               '<a href="/presscenter/news/20270108-13403.html"> Top 10 Foundries Revenue Rises 8% QoQ, Says TrendForce 8 January 2027</a>')
TF_BODY_FIX = {
    "13400": ("Home Press Center QoQ Decline in DRAM ASP Will Moderate to Around 13~18% for 1Q27, Says TrendForce 5 January 2027 Semiconductors "
              "TrendForce's latest analysis finds inventory pressure remains. Therefore, the QoQ decline in DRAM prices are projected to shrink to around "
              "13~18% for 1Q27. PC DRAM and server DRAM are projected to again register a drop that is near 20%. DDR4 8 GB module is still likely to fall by more than 10% in 1Q27."),
    "13401": ("Home Press Center AI Server Demand Sustains Memory Contract Price Increases in 1Q27, Says TrendForce 6 January 2027 Semiconductors TrendForce "
              "Conventional DRAM contract prices are projected to grow 10\u201315% QoQ in 1Q27, while NAND Flash contract prices are expected to increase 15\u201320%. "
              "NAND Flash wafer contract prices are expected to fall 12% QoQ in 1Q27. Enterprise SSD prices will rise."),
    "13402": "Home Press Center DRAM Contract Price Outlook for 1Q27 Revised, Says TrendForce 7 January 2027 Semiconductors No numbers here, see the report.",
}
TF_ACTUAL_FIX = "The ASP of DRAM plunged 20% in 1Q27, and this price decline is predicted to slow down to 10~15% next quarter."
TF_NAND_FIX = "ASP of NAND Flash Products Will Continue to Fall 5~10% in 2Q27. TrendForce projects the ASP for DRAM converging towards a 0~5% decline in 3Q27."
DRAM_TABLE_FIX = ("<div>DRAM Contract Price (2H Mar) Mar. DRAM contract price updated on 3/31/2027. Last Update 2027-03-31 14:00 (GMT+8) Item Session High "
                  "Session Low Session Average Average Change Low Change History DDR4 16Gb 2Gx8 50.00 40.00 44.00 \u25bc -6.00 % \u25bc -5.0 % "
                  "DDR4 8Gb 1Gx8 25.00 19.00 21.00 \u25bc -8.70 % \u25bc -4.65 % Specialty</div>")
NAND_TABLE_FIX = ("<div>NAND Flash Contract Price in Memory Card/UFD (2H Mar) Last Update 2027-03-31 10:00 (GMT+8) Item High Low Average Average Change Low "
                  "Change History NAND 128Gb 16Gx8 MLC 30.80 30.30 30.608 \u25b2 0.44 % \u25b2 0.17 % Wafer Spot</div>")


def rss_fix(titles: list[tuple[str, str, str]]) -> str:
    items = "".join(f"<item><title>{t}</title><link>https://news.google.com/rss/articles/{abs(hash(t)) % 10 ** 8}</link>"
                    f"<pubDate>{d}</pubDate><source url=\"https://x\">{s}</source></item>" for t, s, d in titles)
    return f"<rss><channel>{items}</channel></rss>"


def selftest(cfg: dict) -> int:
    drop = float(cfg["price"]["drop_pct"])
    # A1 parse: trigger (forecast range), non-trigger (up, segment), actual
    f = parse_tf_figures(TF_BODY_FIX["13400"][len("Home Press Center "):], "1Q27")
    ov = [x for x in f if x["overall"]]
    assert ov and ov[0]["prod"] == "dram" and (ov[0]["lo"], ov[0]["hi"], ov[0]["dir"], ov[0]["kind"]) == (13, 18, -1, "forecast"), f
    assert all(not x["overall"] for x in f if x["hi"] in (20.0,) or x["lo"] == 10.0 and x["hi"] == 10.0), f
    h = tf_hits(f, drop)
    assert len(h) == 1 and h[0]["level"] == "경계" and h[0]["full"] and h[0]["priority"] == 4, h
    f2 = parse_tf_figures(TF_BODY_FIX["13401"][len("Home Press Center "):], "1Q27")
    got = sorted((x["prod"], x["lo"], x["hi"], x["dir"], x["overall"]) for x in f2)
    assert ("dram", 10.0, 15.0, 1, True) in got and ("nand", 15.0, 20.0, 1, True) in got and ("nand", 12.0, 12.0, -1, False) in got, got
    assert tf_hits(f2, drop) == [], "up forecasts and wafer segment must not trigger"
    f3 = parse_tf_figures(TF_ACTUAL_FIX)
    h3 = tf_hits(f3, drop)
    assert any(x["level"] == "확정" and x["lo"] == 20 and x["priority"] == 5 for x in h3), (f3, h3)
    assert any(x["level"] == "경계" and (x["lo"], x["hi"]) == (10, 15) for x in h3), h3
    f4 = parse_tf_figures(TF_NAND_FIX, "2Q27")
    h4 = tf_hits(f4, drop)
    assert [(x["prod"], x["lo"], x["hi"], x["full"]) for x in h4] == [("nand", 5.0, 10.0, False)], (f4, h4)
    print("A1 TrendForce parse ok (경계/확정/상승/세부 품목)")
    # A2 tables + rolling
    pdm = parse_table(DRAM_TABLE_FIX, "dram", "DDR4 8Gb 1Gx8")
    assert pdm == {"label": "2H Mar", "month": "2027-03", "avg": 21.0, "chg": -8.7}, pdm
    assert parse_table(NAND_TABLE_FIX, "nand", "NAND 128Gb 16Gx8 MLC")["avg"] == 30.608
    assert parse_table("<div>layout changed</div>", "dram", "DDR4 8Gb 1Gx8") is None
    assert parse_table(DRAM_TABLE_FIX.replace("(2H Mar)", "(2H Dec)").replace("2027-03-31", "2027-01-02"), "dram", "DDR4 8Gb 1Gx8")["month"] == "2026-12"
    ser = {"2026-12": {"v": 25.0, "src": "live"}, "2027-01": {"v": 24.0, "src": "live"}}
    add_point(ser, pdm, "live")
    assert round(ser["2027-02"]["v"], 2) == 23.0 and ser["2027-02"]["src"] == "derived", ser
    r = rolling(ser, 3)
    assert r["chg"] == -16.0 and r["base"] == "2026-12" and r["b"] == 25.0, r
    assert rolling({"2027-01": {"v": 24.0}, "2027-03": {"v": 21.0}}, 3)["chg"] is None  # base month missing -> no verdict
    assert rolling({"2026-12": {"v": 25.0}, "2027-03": {"v": 24.0}}, 3)["chg"] == -4.0
    print("A2 table parse + 3-month rolling ok")
    # A3 news titles
    assert price_news_hit("1분기 D램 고정거래가격 12% 하락 - 연합뉴스", drop)["pct"] == 12
    assert price_news_hit("TrendForce: DRAM contract prices to fall 10-15% in 1Q27 - TrendForce", drop)["prod"] == "dram"
    assert price_news_hit("낸드 가격 두 자릿수 하락 전망 - 뉴스1", drop)["prod"] == "nand"
    assert price_news_hit("10월 D램 가격 5.8%↑…낸드는 21개월 연속 상승세 멈춰 - 연합뉴스", drop) is None
    assert price_news_hit("D램 가격 4% 하락 - 연합뉴스", drop) is None
    assert price_news_hit("SK하이닉스 3주간 40% 급락에도 D램 가격, 여전히 '고공행진' - 월요신문", drop) is None
    assert price_news_hit("D램 현물가격 15% 하락 - 뉴스", drop) is None
    assert price_news_hit("D램 가격 10% 하락 루머 아니다 - 뉴스", drop) is None
    print("A3 price headline rules ok")
    # B CXMT
    assert cx_qual_hit("CXMT HBM3E passes Nvidia qualification - Reuters")["words"]
    assert cx_qual_hit("창신메모리 HBM3E, 엔비디아 퀄 테스트 통과 - 연합뉴스")
    assert cx_qual_hit("구글, TPU에 CXMT HBM3E 채택 - 전자신문")
    assert cx_qual_hit("[김대호 인물] 엔비디아 젠슨황 \"CXMT 메모리 도입\" - 초이스스탁US") is None  # no HBM
    assert cx_qual_hit("CXMT fails Nvidia HBM3E qualification - Reuters") is None  # negation
    assert cx_qual_hit("中 창신메모리 HBM 엔비디아 납품 무산 - 뉴스1") is None  # negation
    assert cx_qual_hit("CXMT Ships HBM3E AI Memory to Alibaba and Cambricon - Tech Times") is None  # China customers
    assert cx_qual_hit("삼성 엔비디아 HBM 퀄 통과, CXMT 추격 - 뉴스") is None  # competitor subject
    assert cx_yield_hit("CXMT HBM3E yield reaches 60% - DigiTimes", 50)["pct"] == 60
    assert cx_yield_hit("中 CXMT HBM 수율 55% 돌파 - 서울경제", 50)
    assert cx_yield_hit("CXMT Pushes Ahead With HBM3E Despite 20% Yield - Seoul Economic Daily", 50) is None
    assert cx_yield_hit("CXMT HBM 수율 50% 목표 - 뉴스", 50) is None
    assert cx_yield_hit("CXMT HBM yield 30%, SK hynix at 80% - X", 50) is None
    assert cx_yield_hit("CXMT HBM 수율 60% 주장은 과도 - 뉴스", 50) is None
    print("B CXMT rules ok (trigger, China-only, competitor, negation, target)")
    # full run with fixtures (no network, no publish, no state)
    now = datetime(2027, 1, 11, 9, 0, tzinfo=KST)
    d = "Mon, 11 Jan 2027 00:30:00 GMT"
    rss_by = {
        "price": rss_fix([("1분기 D램 고정거래가격 12% 하락 전망 - 연합뉴스", "연합뉴스", d), ("10월 D램 가격 5.8%↑ - 연합뉴스", "연합뉴스", d)]),
        "cx": rss_fix([("CXMT HBM3E passes Nvidia qualification - Reuters", "Reuters", d), ("CXMT fails Nvidia HBM3E qualification - Bloomberg", "Bloomberg", d),
                       ("CXMT HBM3E yield reaches 60% - DigiTimes", "DigiTimes", d), ("中 CXMT HBM 수율 55% 돌파 - 서울경제", "서울경제", d),
                       ("Old CXMT HBM3E passes AMD qualification - Old", "Old", "Mon, 02 Nov 2026 00:00:00 GMT")]),
    }

    def reader(url: str) -> str:
        if "presscenter/news/Semiconductors" in url:
            return TF_LIST_FIX
        m = re.search(r"presscenter/news/\d{8}-(\d+)\.html", url)
        if m:
            return TF_BODY_FIX.get(m.group(1), "")
        if "dram_contract" in url:
            return DRAM_TABLE_FIX
        if "flash_contract" in url:
            return "<div>layout changed</div>"  # parse failure fixture
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["q"][0]
        return rss_by["cx" if re.search("CXMT|창신", q) else "price"]

    global load_state
    real_load = load_state
    base_state = {"seeded_at": "2026-10-30T17:00:00+09:00", "px": {"dram": {"2026-12": {"v": 25.0, "src": "live"}}}, "tf_seen": {"13403": {"date": "2027-01-08"}}}
    load_state = lambda: json.loads(json.dumps(base_state))  # noqa: E731
    try:
        res = run("selftest", now, cfg, reader)
    finally:
        load_state = real_load
    titles = [t for t, _, _ in res["titles"]]
    pri = {t: p for t, p, _ in res["titles"]}
    print("\n".join(titles))
    assert any("트렌드포스 1Q27 D램 계약가 -13~18% 전망 | 경계" in t for t in titles), titles
    assert any(t.startswith("[MEMWATCH] 005930/000660 D램 고정거래가 3개월 -16.0% | 확정") and pri[t] == 5 for t in titles), titles
    assert any("메모리 계약가 10% 이상 하락 보도 1건" in t for t in titles), titles
    assert any("CXMT HBM 해외 고객 인증 보도 1건" in t for t in titles), titles
    assert any("CXMT HBM 수율 50% 이상 보도 2건 (출처 2곳)" in t for t in titles), titles
    ops = [x for x in res["titles"] if x[2] == "cwally-ops"]
    assert len(ops) == 1 and "파싱 실패" in res["out"][-1][1] and "트렌드포스 수치 파싱 실패" in res["out"][-1][1], res["titles"]
    assert all(x[2] == "cwally-port" for x in res["titles"] if x not in ops)
    for t, m, p in res["out"]:
        assert "\u00b7" not in t + m and "\u2014" not in t + m
        for sec in ("이게 뭐 알림:", "지금 결과/상태:", "지금 할 일:", "나중에:", "익절/줄일 때:", "하지 말 것:"):
            assert sec in m, (sec, t)
        if "cwally-ops" != route_topic("cwally-port", t):
            assert "참고:" in m and "추가 매수 보류" in m
    # second run with the resulting state: nothing new (dedupe), ops not repeated the same day
    st2 = res["state"]
    load_state = lambda: json.loads(json.dumps(st2))  # noqa: E731
    try:
        res2 = run("selftest", now + timedelta(hours=1), cfg, reader)
    finally:
        load_state = real_load
    assert res2["titles"] == [], res2["titles"]
    # first run (no seeded_at): records silently, reports genuine triggers
    load_state = lambda: {}  # noqa: E731
    try:
        res3 = run("selftest", now, cfg, reader)
    finally:
        load_state = real_load
    assert [x for x in res3["titles"] if x[2] == "cwally-port"] == [] and res3["seed_triggers"], res3["titles"]
    print("SELFTEST OK: fixtures only, no network, no publish, no state")
    return 0


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    if mode == "selftest":
        return selftest(cfg)
    if mode not in {"auto", "dry", "seed"}:
        print(f"unknown mode {mode}")
        return 2
    now = datetime.now(KST)
    try:
        run(mode, now, cfg)
        return 0
    except Exception as exc:  # noqa: BLE001
        print(traceback.format_exc(), file=sys.stderr)
        if mode != "auto":
            return 1
        try:
            t, m, p = ops_msg(now.strftime("%Y-%m-%d %H:%M KST"), [f"스크립트 오류: {type(exc).__name__}: {str(exc)[:200]}"])
            publish(resolve_topic("ops"), t, m, p, ["warning"], resolve_server(cfg.get("ntfy_server")))
        except Exception:  # noqa: BLE001
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
