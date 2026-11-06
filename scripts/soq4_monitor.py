#!/usr/bin/env python3
"""[SO] Southern Company earnings engine, quarter-parameterized (configs/soq4.json `profile`).

Reuses the soq3 engine (retired 2026-11-06, commit 0a562e0): SEC 8-K EX-99 numbers parse, boilerplate
filter, guidance wording ('higher than the top' = 상향), large-load GW, equity amount vs baseline
(ef52c0a). New for Q4 (pt66u):
  1. Call verdict only from a call TRANSCRIPT (Motley Fool), tried from the morning after the call.
     Guidance raised but equity need or large-load GW not confirmed -> '콜 핵심 미확인' (no optimism).
     No transcript by filing day + 3 days 09:00 KST -> one '콜 핵심 미확인' notice.
  2. Equity need up vs baseline but attributed to growth capex (to fund incremental/additional
     investment, >=30% equity share of incremental capex, large-load/data center) -> the equity part
     is softened ONE level ('주의 | 추가 보류' -> '보유 유지'); unclear attribution -> no softening.
  3. Long-term EPS growth range ('7% to 8%') parsed and compared with the baseline range.
Verdicts feed state/soq4.json so_verdict (실행 / 분할 일부만 / 보류), the shape cash30 read from soq3.

Modes: auto | dry (print only, no ntfy, no state, date gate ignored) | selftest (offline).
  dry env: SOQ4_PHASE=call (force call stage), SOQ4_REGRESS=q3 (run the live Q3 2026 transcript
  through the engine with the Q3 profile), SOQ4_TEST_TEXT=<file> (local transcript/release text).
Not investment advice.
"""
from __future__ import annotations

import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
EST = timezone(timedelta(hours=-5))  # Feb = US standard time
CFG = ROOT / "configs" / "soq4.json"
STATE = ROOT / "state" / "soq4.json"
UA_SEC = {"User-Agent": "cwally-alerts sosmos3218@gmail.com", "Accept-Encoding": "identity"}
UA_WEB = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
          "Accept-Encoding": "identity", "Accept-Language": "en-US,en;q=0.8"}
CIK_INT = 92122
Q3_URL = "https://www.fool.com/earnings/call-transcripts/2026/11/05/southern-co-so-q3-2026-earnings-call-transcript/"
Q3_SA_URL = "https://stockanalysis.com/stocks/so/transcripts/720161-q3-2026/"

# Verdict ladder, best to worst. No '매수' in titles (pt66u).
LEVELS = ["추가 검토", "분할 일부만", "보유 유지", "주의 | 추가 보류"]
UNCONFIRMED = "콜 핵심 미확인"
CASH30 = {0: "실행", 1: "분할 일부만", 2: "보류", 3: "보류"}

# Q3 2026 profile = the retired soq3 baselines; used by the regression test only.
Q3_PROFILE = {
    "quarter": "Q3", "quarter_word": "third", "company_q_eps": 1.65, "prior_q_adj_eps": 1.60,
    "fy_year": 2026, "fy_estimate": None, "guide_year": 2026, "guidance_low": 4.50, "guidance_high": 4.60,
    "contracted_gw": 17, "pipeline_gw": 75, "equity_need_bn": 1.1, "lt_low": 7.0, "lt_high": 8.0,
    "entry_ref": 89.30, "chase_limit_pct": 5.0, "stop_ref": 84.0,
}


# ---------------------------------------------------------------- fetch

class Net:
    """Network access, swapped out in selftest."""

    def get(self, url: str, sec: bool = False) -> str:
        with urlopen(Request(url, headers=UA_SEC if sec else UA_WEB), timeout=25) as resp:
            return resp.read().decode("utf-8", errors="ignore")


def textify(html: str) -> str:
    t = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    t = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = unescape(t).replace("\xa0", " ").replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    t = t.replace("\u2013", "-").replace("\u2014", "-")
    return re.sub(r"\s+", " ", t).strip()


def sec_release(net: Net, cfg: dict, pf: dict, since: str) -> tuple[str, str, str]:
    """Return (text, source, filing_date) of the first 8-K EX-99 on/after `since` for the quarter."""
    data = json.loads(net.get(cfg["sources"]["sec"], sec=True))
    recent = data.get("filings", {}).get("recent", {})
    qw = pf["quarter_word"]
    rows = sorted(zip(recent.get("form", []), recent.get("filingDate", []), recent.get("accessionNumber", [])), key=lambda r: r[1])
    for form, date, acc in rows:
        if form != "8-K" or date < since:
            continue
        base = f"https://www.sec.gov/Archives/edgar/data/{CIK_INT}/{acc.replace('-', '')}"
        try:
            idx = json.loads(net.get(base + "/index.json", sec=True))
            names = [i.get("name", "") for i in idx.get("directory", {}).get("item", [])]
        except Exception:
            names = []
        for name in [n for n in names if re.search(r"ex-?99", n, re.I) and n.lower().endswith((".htm", ".html"))]:
            try:
                body = textify(net.get(f"{base}/{name}", sec=True))
            except Exception:
                continue
            if re.search(qw + r".quarter", body, re.I) and re.search(r"per share", body, re.I):
                return body, f"SEC 8-K {date} {name}", date
    return "", "", ""


def get_price(net: Net, cfg: dict, release_date: str) -> tuple[float | None, float | None, str]:
    try:
        data = json.loads(net.get(cfg["sources"]["quote"], sec=True))  # Yahoo 429s the browser UA; soq3 used this one
        res = ((data.get("chart") or {}).get("result") or [None])[0]
        if not res:
            return None, None, "시세 없음"
        last = (res.get("meta") or {}).get("regularMarketPrice")
        ref = None
        closes = ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        for t, c in zip(res.get("timestamp") or [], closes):
            if c is not None and datetime.fromtimestamp(t, EST).strftime("%Y-%m-%d") < release_date:
                ref = c
        return (float(last) if last else None), ref, ""
    except Exception as exc:
        return None, None, f"시세 실패: {exc}"


def is_transcript(text: str, pf: dict) -> bool:
    return (len(text) > 15000 and bool(re.search(pf["quarter_word"] + r" quarter", text, re.I))
            and bool(re.search(r"\bOperator\b", text)) and bool(re.search(r"Poroch|Womack", text)))


def trim_transcript(text: str) -> str:
    """Drop site navigation around a Motley Fool transcript (nav words once matched the guidance rule)."""
    i = -1
    for key in ("TAKEAWAYS", "Full Conference Call Transcript", "Operator:", "Operator"):
        i = text.find(key)
        if i >= 0:
            break
    if i > 0:
        text = text[i:]
    j = re.search(r"The Motley Fool has no position|This article is a transcript of this conference call", text)
    return text[:j.start()] if j else text


def find_fool_urls(html: str, slug: str) -> list[str]:
    raw = html.replace("\\/", "/")
    found = re.findall(r"/earnings/call-transcripts/\d{4}/\d{2}/\d{2}/[a-z0-9-]*" + re.escape(slug) + r"[a-z0-9-]*/", raw)
    out = []
    for p in found:
        if re.search(r"southern|-so-", p) and p not in out:
            out.append(p)
    return ["https://www.fool.com" + p for p in out]


def find_sa_urls(html: str, slug: str) -> list[str]:
    out = []
    for p in re.findall(r"/stocks/so/transcripts/[0-9]+-" + re.escape(slug) + r"/", html.replace("\\/", "/")):
        if p not in out:
            out.append(p)
    return ["https://stockanalysis.com" + p for p in out]


def transcript(net: Net, cfg: dict, pf: dict, filing_date: str, log: list[str]) -> tuple[str, str]:
    """Motley Fool quote page -> transcript link, date-guessed Fool slugs, then StockAnalysis. Returns (text, url)."""
    slug = cfg.get("transcript_slug", "q4-2026")
    urls: list[str] = []
    try:
        urls += find_fool_urls(net.get(cfg["sources"]["fool_quote"]), slug)
        log.append(f"fool quote ok, links {len(urls)}")
    except Exception as exc:
        log.append(f"fool quote 실패: {exc}")
    if filing_date:
        d0 = datetime.fromisoformat(filing_date)
        for k in (0, 1):
            d = d0 + timedelta(days=k)
            u = cfg["sources"]["fool_guess"].format(y=d.year, m=f"{d.month:02d}", d=f"{d.day:02d}")
            if u not in urls:
                urls.append(u)
    try:
        sa = find_sa_urls(net.get(cfg["sources"]["stockanalysis"]), slug)
        log.append(f"stockanalysis ok, links {len(sa)}")
        urls += [u for u in sa if u not in urls]
    except Exception as exc:
        log.append(f"stockanalysis 실패: {str(exc)[:40]}")
    for u in urls:
        try:
            text = textify(net.get(u))
        except Exception as exc:
            log.append(f"{u.rsplit('/', 2)[-2]}: {str(exc)[:40]}")
            continue
        if is_transcript(text, pf):
            return trim_transcript(text), u
        log.append(f"{u.rsplit('/', 2)[-2]}: 녹취 형식 아님")
    return "", ""


def article_bodies(net: Net, cfg: dict, log: list[str]) -> list[tuple[str, str]]:
    """Fallback: news article bodies (never a transcript; used only as evidence in the missing notice)."""
    out = []
    for q in cfg.get("news_queries", []):
        try:
            root = ET.fromstring(net.get(cfg["sources"]["news_rss"].format(q=quote_plus(q))))
        except Exception as exc:
            log.append(f"news rss 실패: {str(exc)[:40]}")
            continue
        for item in list(root.iter("item"))[:6]:
            link = item.findtext("link") or ""
            title = item.findtext("title") or ""
            if not re.search(r"southern", title, re.I):
                continue
            try:
                body = textify(net.get(link))
            except Exception:
                continue
            if len(body) > 3000 and re.search(r"Poroch|Womack", body):
                out.append((body, title[:80]))
    return out


# ---------------------------------------------------------------- parse

def sentences(text: str) -> list[str]:
    return re.split(r"(?<=[A-Za-z0-9%)\]])[.?!]\s+(?=[A-Z(\"$])", text)


BOILER_CUT = r"(Cautionary Note Regarding Forward.Looking|Cautionary Statements? (and|Regarding)|forward-looking statements? (are|include|involve|within the meaning))"
BOILER_SENT = r"(forward.looking|risks? and uncertaint|could cause|may cause|cautionary|risk factors|no assurance|could give rise|subject to conditions that are not anticipated)"


def drop_boilerplate(text: str) -> str:
    m = re.search(BOILER_CUT, text, re.I)
    if m and m.start() > len(text) * 0.3:
        text = text[:m.start()]
    return " ".join(s + "." for s in sentences(text) if not re.search(BOILER_SENT, s, re.I))


def num(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text, re.I)
    return float(m.group(1).replace(",", "")) if m else None


def parse_numbers(text: str, pf: dict) -> dict:
    qw = pf["quarter_word"]
    reported = num(r"reported " + qw + r".quarter earnings of \$[\d.,]+ (?:billion|million),? or \$(\d+\.\d+) per share", text)
    adj = num(r"Excluding the items[^$]{0,200}?earned \$[\d.,]+ (?:billion|million),? or \$(\d+\.\d+) per share", text)
    if adj is None:
        adj = num(r"adjusted (?:earnings|EPS)[^$.]{0,80}\$(\d+\.\d+) per share", text)
    fy_adj = num(r"full.year \d{4}, excluding[^$]{0,120}?earned \$[\d.,]+ (?:billion|million),? or \$(\d+\.\d+) per share", text)
    return {"published": bool(reported or adj) and bool(re.search(qw + r".quarter", text, re.I)),
            "reported_eps": reported, "adj_eps": adj, "fy_adj_eps": fy_adj}


RANGE_RE = re.compile(r"(\d{1,2}(?:\.\d)?)\s?%?\s?(?:to|-|through)\s?(\d{1,2}(?:\.\d)?)\s?(?:%|percent)", re.I)
LT_KEY = re.compile(r"long.?term|longer term|into the next decade|path starting in 20\d\d|beyond 20\d\d|from (?:our |the |a )?(?:higher )?(?:'?\d\d|20\d\d) (?:guidance|base)", re.I)


def parse_lt_range(text: str) -> tuple[tuple[float, float] | None, str]:
    """Long-term EPS growth range. Prefer a range followed by 'from ... base/guidance', else one near a long-term word."""
    best = None
    for s in sentences(text):
        if not LT_KEY.search(s) or not re.search(r"grow|growth|path|CAGR", s, re.I):
            continue
        if re.search(r"sales|rate base|dividend", s, re.I) and not re.search(r"earnings|EPS", s, re.I):
            continue
        for m in RANGE_RE.finditer(s):
            lo, hi = float(m.group(1)), float(m.group(2))
            if not (0 < lo < hi <= 20):
                continue
            after, before = s[m.end():m.end() + 40], s[max(0, m.start() - 60):m.start()]
            score = 2 if re.match(r"\W*(?:annually |a year |per year )?(?:from (?:our |the |a )?(?:higher )?(?:'?\d\d|20\d\d)|path starting|long.?term|longer term)", after, re.I) \
                else (1 if re.search(r"long.?term|longer term", before, re.I) else 0)
            if score and (best is None or score > best[0]):
                best = (score, (lo, hi), s.strip()[:160])
    return (best[1], best[2]) if best else (None, "")


def compare_range(new: tuple[float, float] | None, lo: float | None, hi: float | None) -> str:
    if new is None or lo is None or hi is None:
        return "미확인"
    up = new[0] > lo + 1e-9 or new[1] > hi + 1e-9
    down = new[0] < lo - 1e-9 or new[1] < hi - 1e-9
    if up and not down:
        return "상향"
    if down and not up:
        return "하향"
    return "유지" if not up else "혼재"


def parse_guide_range(text: str, year: int) -> tuple[tuple[float, float] | None, str]:
    for s in sentences(text):
        i = s.find(str(year))
        if i < 0 or not re.search(r"guidance|range|expect|project", s, re.I):
            continue
        m = re.search(r"\$\s?(\d\.\d{2})\s?(?:to|-)\s?\$?\s?(\d\.\d{2})", s[i:i + 160])
        if m and float(m.group(1)) < float(m.group(2)):
            return (float(m.group(1)), float(m.group(2))), s.strip()[:160]
    return None, ""


STRONG_ATTR = re.compile(r"to fund (?:the )?(?:incremental|additional|growth)\w* (?:capital|investment|capex|plan)|large.load|data cent(?:er|re)s?|growth (?:capital|investment|capex)", re.I)
GENERIC_ATTR = re.compile(r"incremental[^.]{0,40}(?:investment|capital|capex|projects)|above our[^.]{0,30}base capital plan|additional investment", re.I)
PCT_ATTR = re.compile(r"(\d{1,2})% (?:equity|of (?:the )?(?:incremental|additional))", re.I)


def equity_attribution(sent: str, ctx: str) -> tuple[bool, str]:
    """Explicit growth wording must sit in the equity sentence; the % share and generic wording may be in the next one."""
    pct = PCT_ATTR.search(ctx)
    strong = STRONG_ATTR.search(sent)
    generic = GENERIC_ATTR.search(ctx)
    bits = []
    if pct:
        bits.append(f"증가분 자기자본 비중 {pct.group(1)}%")
    if strong:
        bits.append(f"'{strong.group(0)}'")
    if generic and not strong:
        bits.append(f"'{generic.group(0)[:60]}'")
    ok = bool(strong) or (bool(generic) and pct is not None and int(pct.group(1)) >= 30)
    return ok, ", ".join(bits)


def call_signals(text: str, pf: dict) -> dict:
    sents = sentences(drop_boilerplate(text))
    sig = {"guide": "미확인", "guide_range": None, "gw": None, "equity": "미확인", "equity_bn": None,
           "equity_attr": False, "equity_attr_ev": "", "lt": None, "lt_cmp": "미확인", "evidence": []}

    def ev(line: str) -> None:
        line = line.strip()[:110]
        if len(sig["evidence"]) < 4 and line not in sig["evidence"]:
            sig["evidence"].append(line)

    rng, rs = parse_guide_range(text, int(pf["guide_year"]))
    if rng:
        sig["guide_range"] = rng
        c = compare_range(rng, pf.get("guidance_low"), pf.get("guidance_high"))
        sig["guide"] = {"상향": "상향", "하향": "하향/하단", "유지": "유지", "혼재": "유지"}.get(c, "미확인")
        ev(rs)
    for i, s in enumerate(sents):
        if not rng:
            if re.search(r"(lower(s|ed)?|cut(s)?|trim(s|med)?|reduc(es|ed))[^.]{0,40}(guidance|outlook|forecast)|lower end|bottom end|low end", s, re.I):
                if sig["guide"] != "하향/하단":
                    sig["guide"] = "하향/하단"
                    ev(s)
            elif sig["guide"] in {"미확인", "상단 재확인", "유지"} and re.search(
                    r"(rais(es|ed|ing)|lift(s|ed)|boost(s|ed)|increas(es|ed))[^.]{0,40}(guidance|outlook|forecast)"
                    r"|(higher than|above) the (top|upper end|high end)", s, re.I):
                sig["guide"] = "상향"  # soq3: "$4.70 or $0.10 higher than the top of ... guidance range"
                ev(s)
            elif sig["guide"] == "미확인" and re.search(r"top end|upper end|high end|top of", s, re.I) and re.search(r"guidance|range", s, re.I):
                sig["guide"] = "상단 재확인"
                ev(s)
            elif sig["guide"] == "미확인" and re.search(r"(reaffirm|maintain|on track|on target)[^.]{0,40}(guidance|outlook|20\d\d)", s, re.I):
                sig["guide"] = "유지"
                ev(s)
        for gw in re.findall(r"(\d{1,3}(?:\.\d)?)\s?(?:GW|gigawatts?)\b", s, re.I):
            g = float(gw)
            if re.search(r"contract|signed|agreement|commit", s, re.I) and g < pf["pipeline_gw"] and (sig["gw"] is None or g > sig["gw"]):
                sig["gw"] = g
                ev(s)
        amt = num(r"equity (?:need|needs|requirement)s?[^.$]{0,40}\$(\d+(?:\.\d+)?) ?(?:billion|B)\b", s) \
            or num(r"\$(\d+(?:\.\d+)?) ?(?:billion|B)\b[^.$]{0,30}(?:of )?(?:remaining |additional |new |projected )*equity (?:need|needs|requirement)", s)
        if amt is not None and pf.get("equity_need_bn") is not None:
            sig["equity_bn"] = amt
            sig["equity"] = "증가" if amt > pf["equity_need_bn"] + 0.1 else "유지/감소"
            sig["equity_num"] = True
            ctx = " ".join(sents[i:i + 2])
            sig["equity_attr"], sig["equity_attr_ev"] = equity_attribution(s, ctx)
            if sig["equity_attr"]:
                sig["equity_attr_ev"] += f" (\"{s.strip()[:150]}\")"
            ev(s)
        elif sig.get("equity_num"):
            pass
        elif re.search(r"\bequity\b", s, re.I):
            if re.search(r"(more|additional|increas\w*|rais\w*|higher|new)[^.]{0,30}equity (need|issu|offering)|equity[^.]{0,30}(increas|higher|offering)", s, re.I):
                sig["equity"] = "증가"
                ev(s)
            elif sig["equity"] == "미확인" and re.search(r"no (new|additional) equity|equity[^.]{0,30}(unchanged|reduc|lower|complete)", s, re.I):
                sig["equity"] = "유지/감소"
                ev(s)
    lt, lts = parse_lt_range(text)
    if lt:
        sig["lt"] = lt
        sig["lt_cmp"] = compare_range(lt, pf.get("lt_low"), pf.get("lt_high"))
        ev(lts)
    return sig


# ---------------------------------------------------------------- decide

def judge_numbers(pf: dict, p: dict, last: float | None, ref: float | None) -> tuple[str, str, int]:
    reasons, bad = [], False
    eps = p["adj_eps"]
    if eps is not None:
        diff = eps - pf["company_q_eps"]
        tag = "상회" if diff > 0.02 else ("미달" if diff < -0.05 else "부합")
        reasons.append(f"{pf['quarter']} 조정 EPS {eps} {tag}(회사 추정 {pf['company_q_eps']}, 전년 {pf['prior_q_adj_eps']})")
        bad |= tag == "미달"
    if p.get("fy_adj_eps") is not None and pf.get("fy_estimate"):
        d = p["fy_adj_eps"] - pf["fy_estimate"]
        reasons.append(f"{pf['fy_year']} 연간 조정 EPS {p['fy_adj_eps']} (추정 {pf['fy_estimate']}, {d:+.2f})")
        bad |= d < -0.05
    if last is not None:
        if ref:
            reasons.append(f"현재 {last:.2f}, 전일 대비 {(last / ref - 1) * 100:+.1f}%")
        if last < pf["stop_ref"]:
            reasons.append(f"{pf['stop_ref']} 아래")
            bad = True
    action = "주의 | 추가 보류" if bad else "보유 유지 | 추가는 콜 판정 뒤"
    return action, ". ".join(reasons) or "판정 근거 부족", 3 if bad else 2


def judge_call(pf: dict, p: dict, sig: dict, last: float | None, ref: float | None) -> dict:
    """Return {'label', 'level', 'reasons', 'softened', 'attr'}; level indexes LEVELS (UNCONFIRMED uses 2)."""
    reasons, other = [], 2
    eps = p.get("adj_eps")
    if eps is not None and eps < pf["company_q_eps"] - 0.05:
        reasons.append(f"{pf['quarter']} EPS {eps} 미달")
        other = 3
    g = sig["guide"]
    gr = f" {sig['guide_range'][0]:.2f}-{sig['guide_range'][1]:.2f}" if sig.get("guide_range") else ""
    base_g = f" (기준 {pf['guidance_low']:.2f}-{pf['guidance_high']:.2f})" if pf.get("guidance_low") else ""
    reasons.append(f"{pf['guide_year']} 가이던스 {g}{gr}{base_g}")
    if g == "하향/하단":
        other = 3
    if sig["lt"]:
        reasons.append(f"장기 성장률 {sig['lt'][0]:g}-{sig['lt'][1]:g}% {sig['lt_cmp']} (기준 {pf['lt_low']:g}-{pf['lt_high']:g}%)")
        if sig["lt_cmp"] == "하향":
            other = 3
    if sig["gw"] is not None:
        reasons.append(f"대형부하 계약 {sig['gw']:g}GW (기준 {pf['contracted_gw']:g}GW)")
        if sig["gw"] < pf["contracted_gw"]:
            other = max(other, 2)
    eq_level, softened, attr = 0, False, ""
    if sig["equity"] == "증가":
        amt = f" 약 ${sig['equity_bn']:g}B, 기준 ${pf['equity_need_bn']:g}B" if sig.get("equity_bn") is not None else ""
        eq_level = 3
        if sig.get("equity_attr"):
            eq_level, softened, attr = 2, True, sig["equity_attr_ev"]
            reasons.append(f"증자 필요 증가{amt}, 성장 투자 몫이라 한 단계 완화(근거: {attr})")
        else:
            reasons.append(f"증자 필요 증가{amt}, 원인 불명확이라 완화 없음")
    elif sig["equity"] == "유지/감소":
        reasons.append("증자 필요 유지/감소" + (f" 약 ${sig['equity_bn']:g}B" if sig.get("equity_bn") is not None else ""))
    level = max(other, eq_level)
    optimistic = g in {"상단 재확인", "상향"} or sig["lt_cmp"] == "상향"
    if level < 3 and optimistic:
        if sig["equity"] == "미확인" or sig["gw"] is None:
            miss = [x for x, ok in (("증자 필요", sig["equity"] != "미확인"), ("대형부하 GW", sig["gw"] is not None)) if not ok]
            reasons.append(f"가이던스는 좋지만 {', '.join(miss)} 콜에서 확인 못 함")
            return {"label": UNCONFIRMED + " | 추가 보류", "level": 2, "reasons": reasons, "softened": softened, "attr": attr}
        if sig["equity"] != "증가" and sig["gw"] >= pf["contracted_gw"]:
            level = 0
    if last is not None:
        chase = pf["entry_ref"] * (1 + pf["chase_limit_pct"] / 100)
        if ref:
            reasons.append(f"종가 {last:.2f}, 전일 대비 {(last / ref - 1) * 100:+.1f}%")
        if level == 0 and last > chase:
            reasons.append(f"1차 기준가 {pf['entry_ref']}의 +{pf['chase_limit_pct']:.0f}%({chase:.2f}) 초과라 추격 금지")
            level = 1
        if last < pf["stop_ref"]:
            reasons.append(f"{pf['stop_ref']} 아래")
            level = 3
    return {"label": LEVELS[level], "level": level, "reasons": reasons, "softened": softened, "attr": attr}


TODO = {
    0: "추가 비중을 장중 분할로 검토. 갭 상승 출발이면 첫 30분은 기다림.",
    1: "추가 비중은 일부만, 1차 기준가 +5% 이하 가격에서만.",
    2: "1차 보유 유지. 추가는 보류.",
    3: "추가 보류. 1차 물량은 손절 참고선과 가이던스 변화로 재평가.",
}


def body(cfg: dict, now: datetime, head: str, status: str, todo: str, later: str) -> str:
    pf = cfg["profile"]
    return "\n".join([
        now.strftime("%Y-%m-%d %H:%M KST") + f" | SO | {head}",
        "이게 뭐 알림: " + cfg["explain"],
        "지금 상태: " + status,
        "지금 할 일: " + todo,
        "나중에: " + later,
        f"익절/줄일 때: {pf['guide_year']} 가이던스 하향, 장기 성장률 하향, 원인 불명 증자 확대, 또는 종가 {pf['stop_ref']} 이탈 시 1차 물량 재평가.",
        "하지 말 것: 실적 당일 갭 추격, 계획 비중 초과, 헤드라인 1건만 보고 단정, 신용/미수. 투자 권유 아님.",
    ])


# ---------------------------------------------------------------- main

def load_state() -> dict:
    st = {"numbers_sent": False, "call_sent": False, "missing_sent": False, "fail_notified": False, "attempts": []}
    if STATE.exists():
        try:
            st.update(json.loads(STATE.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return st


def save_state(st: dict, now: datetime, dry: bool) -> None:
    if dry:
        return
    st["updated_at"] = now.isoformat()
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class Sender:
    def __init__(self, dry: bool):
        self.dry, self.sent = dry, []

    def __call__(self, title: str, msg: str, prio: int) -> None:
        self.sent.append((title, msg, prio))
        if self.dry:
            print(f"DRY RUN would send: {title}\n{msg}\n")
            return
        publish(resolve_topic("port"), title, msg, prio)


def call_times(cfg: dict, filing_date: str) -> tuple[datetime, datetime]:
    d = datetime.fromisoformat(filing_date).replace(tzinfo=KST)
    return d + timedelta(days=1, hours=7), d + timedelta(days=int(cfg.get("missing_after_days", 3)), hours=9)


def set_verdict(st: dict, label: str, level: int, note: str, now: datetime) -> None:
    st.update({"score": label, "so_verdict": CASH30.get(level, "보류") if not label.startswith(UNCONFIRMED) else "보류",
               "so_verdict_note": note[:600], "so_verdict_updated": now.strftime("%Y-%m-%d")})


def run(cfg: dict, st: dict, now: datetime, mode: str, net: Net, send: Sender, test_text: str | None = None) -> dict:
    pf = cfg["profile"]
    start = datetime.fromisoformat(cfg["window_start_kst"]).replace(tzinfo=KST)
    scan_end = datetime.fromisoformat(cfg["release_scan_end_kst"]).replace(tzinfo=KST)
    end = datetime.fromisoformat(cfg["window_end_kst"]).replace(tzinfo=KST)
    in_win = start <= now <= end and now.year == int(cfg["event_year"])
    print(f"{now.isoformat()} mode={mode} window={in_win}")
    if mode == "auto" and not in_win:
        print("outside window; silent")
        return st
    if mode == "auto" and (st.get("call_sent") or st.get("missing_sent")):
        print("call stage done; skip")
        return st

    rel = st.get("release") or {}
    release_text, src, fdate, err = "", rel.get("src", ""), rel.get("date", ""), []
    if test_text is not None:
        release_text, src, fdate = test_text, "테스트 텍스트", now.strftime("%Y-%m-%d")
    elif not st.get("numbers_sent") or not fdate:
        if mode == "auto" and now > scan_end and not fdate:
            print("release scan window over, no 8-K; silent")
            return st
        try:
            release_text, src, fdate = sec_release(net, cfg, pf, (start - timedelta(days=1)).strftime("%Y-%m-%d"))
        except Exception as exc:
            err.append(f"sec: {exc}")
    p = parse_numbers(release_text, pf) if release_text else {"published": False, "reported_eps": None, "adj_eps": rel.get("adj_eps"), "fy_adj_eps": rel.get("fy_adj_eps")}
    print("numbers:", json.dumps(p, ensure_ascii=False), "| src:", src or "-", "| err:", err)
    if err and not st.get("fail_notified"):
        msg = body(cfg, now, "SEC 조회", "SEC 8-K 조회 실패. " + "; ".join(err)[:200], "IR 페이지 직접 확인. 이 알림으로 매매 금지.", "다음 실행에서 자동 재확인.")
        send("[SO] SO 조회 실패 | 판정 보류", msg, 3)  # route_topic -> cwally-ops
        st["fail_notified"] = True
    if not fdate or not (p["published"] or rel):
        print("8-K not found yet")
        return st

    last = ref = None
    pnote = ""
    if test_text is None:
        last, ref, pnote = get_price(net, cfg, fdate)
    if p["published"] and not st.get("numbers_sent"):
        action, why, level = judge_numbers(pf, p, last, ref)
        status = f"{action}. {why}. 보고 EPS {p['reported_eps'] if p['reported_eps'] is not None else '미기재'}. 출처 {src}."
        todo = "추가 보류. 콜 판정 확인." if level == 3 else "1차 보유 유지. 추가는 녹취 기반 콜 판정 뒤 결정."
        send(f"[SO] SO 4분기 숫자 | {action}", body(cfg, now, f"{pf['quarter']} 숫자 판정", status, todo,
             "다음 날 08:00 KST 이후 콜 녹취로 가이던스, 장기 성장률, 대형부하, 증자 판정이 한 번 더 옵니다."), 4 if level == 3 else 3)
        st.update({"numbers_sent": True, "release": {"date": fdate, "src": src, "adj_eps": p["adj_eps"], "fy_adj_eps": p["fy_adj_eps"]}})
        set_verdict(st, action, 2 if level != 3 else 3, why, now)

    first, deadline = call_times(cfg, fdate)
    force_call = mode == "dry" and os.environ.get("SOQ4_PHASE") == "call"
    if now < first and not force_call:
        print(f"call stage from {first:%m-%d %H:%M} KST")
        return st
    log: list[str] = []
    text, url = (test_text, "테스트 텍스트") if test_text is not None and is_transcript(test_text, pf) else ("", "")
    if test_text is None:
        text, url = transcript(net, cfg, pf, fdate, log)
    print("transcript:", url or "-", "|", "; ".join(log))
    if text:
        sig = call_signals(text, pf)
        res = judge_call(pf, p, sig, last, ref)
        ev = " / ".join(sig["evidence"]) or "근거 문장 없음"
        status = f"{res['label']}. {'. '.join(res['reasons'])}. 근거: {ev}. 출처: 콜 녹취 {url}."
        todo = TODO[res["level"]] if not res["label"].startswith(UNCONFIRMED) else "1차 보유 유지. 추가는 증자와 대형부하 숫자를 콜 요약에서 직접 확인한 뒤."
        send(f"[SO] SO 콜 판정 | {res['label']}", body(cfg, now, "콜 녹취 판정", status, todo, "일회 알림 종료. 다음 분기 실적 전 재평가."), 4 if res["level"] != 2 else 3)
        st.update({"call_sent": True, "guide": sig["guide"], "gw": sig["gw"], "equity": sig["equity"], "equity_bn": sig["equity_bn"],
                   "equity_attr": sig["equity_attr_ev"], "lt": sig["lt"], "lt_cmp": sig["lt_cmp"], "transcript": url})
        set_verdict(st, res["label"], res["level"], "; ".join(res["reasons"]), now)
        return st
    st.setdefault("attempts", []).append(now.strftime("%m-%d %H:%M"))
    st["attempts"] = st["attempts"][-12:]
    if now < deadline and not force_call:
        print(f"no transcript yet; missing notice at {deadline:%m-%d %H:%M} KST")
        return st
    arts = [] if test_text is not None else article_bodies(net, cfg, log)
    extra = ""
    if arts:
        sig = call_signals(" ".join(a[0] for a in arts), pf)
        extra = f" 기사 본문 참고(판정 아님): 가이던스 {sig['guide']}, 증자 {sig['equity']}, GW {sig['gw'] if sig['gw'] is not None else '미확인'}."
    status = (f"{UNCONFIRMED}. {fdate} 8-K 뒤 콜 녹취를 {len(st['attempts'])}번 찾았지만 없음(Motley Fool)."
              f"{extra} 녹취 없이는 낙관 판정을 내지 않음.")
    send(f"[SO] SO {UNCONFIRMED} | 녹취 없음, 추가 보류", body(cfg, now, "콜 녹취 없음", status,
         "1차 보유 유지. 추가는 보류. 회사 IR 슬라이드에서 증자, 대형부하 GW, 장기 성장률을 직접 확인.", "일회 알림 종료."), 3)
    st["missing_sent"] = True
    set_verdict(st, UNCONFIRMED, 2, "콜 녹취 없음", now)
    return st


def regress_q3(net: Net, url: str = Q3_URL) -> int:
    """dry: live Q3 2026 transcript through the engine with the Q3 profile (expect 보유 유지 after softening)."""
    print("Q3 REGRESSION source:", url)
    text = trim_transcript(textify(net.get(url)))
    print("Q3 transcript chars:", len(text), "is_transcript:", is_transcript(text, Q3_PROFILE))
    sig = call_signals(text, Q3_PROFILE)
    res = judge_call(Q3_PROFILE, {"adj_eps": 1.94}, sig, None, None)
    print("Q3 signals:", json.dumps({k: v for k, v in sig.items() if k != "evidence"}, ensure_ascii=False))
    for e in sig["evidence"]:
        print("  evidence:", e)
    print("Q3 REGRESSION verdict:", res["label"], "| softened:", res["softened"])
    print("Q3 reasoning:", " / ".join(res["reasons"]))
    return 0 if res["label"] == "보유 유지" and res["softened"] else 1


def reach(net: Net, cfg: dict) -> None:
    for name, url, sec in (("sec", cfg["sources"]["sec"], True), ("fool_quote", cfg["sources"]["fool_quote"], False),
                           ("fool_q3", Q3_URL, False), ("stockanalysis", cfg["sources"]["stockanalysis"], False), ("sa_q3", Q3_SA_URL, False),
                           ("news_rss", cfg["sources"]["news_rss"].format(q="Southern+Company+earnings"), False),
                           ("yahoo", cfg["sources"]["quote"], True)):
        try:
            t = net.get(url, sec=sec)
            note = "challenge" if re.search(r"Enable JavaScript and cookies|Just a moment|cf-chl", t) else "ok"
            print(f"REACH {name}: {note} {len(t)} bytes")
        except Exception as exc:
            print(f"REACH {name}: FAIL {str(exc)[:80]}")


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    ok = fail = 0

    def check(name: str, cond: bool, info: str = "") -> None:
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"FAIL {name} {info}")

    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    pf = cfg["profile"]
    filler = " The weather was warm and customers grew in the quarter." * 20

    # 1. downgrade: guidance raised but equity / GW unconfirmed
    t = "Our initial guidance range for 2027 is adjusted earnings per share of $4.95 to $5.05." + filler
    sig = call_signals(t, pf)
    check("guide range parsed", sig["guide_range"] == (4.95, 5.05) and sig["guide"] == "상향", str(sig["guide_range"]))
    r = judge_call(pf, {}, sig, None, None)
    check("downgrade equity+gw unconfirmed", r["label"].startswith(UNCONFIRMED) and r["level"] == 2, r["label"])
    sig2 = call_signals(t + " We project a remaining equity need of approximately $2 billion through 2031.", pf)
    r = judge_call(pf, {}, sig2, None, None)
    check("downgrade gw unconfirmed", r["label"].startswith(UNCONFIRMED) and "대형부하 GW" in " ".join(r["reasons"]), r["label"])
    sig3 = call_signals(t + " Our signed large load contracts have grown to 24 gigawatts.", pf)
    check("downgrade equity unconfirmed", judge_call(pf, {}, sig3, None, None)["label"].startswith(UNCONFIRMED))
    good = t + " Our signed large load contracts have grown to 24 gigawatts. We project a remaining equity need of approximately $2 billion through 2031."
    r = judge_call(pf, {}, call_signals(good, pf), None, None)
    check("all confirmed -> 추가 검토", r["label"] == "추가 검토", r["label"])
    r = judge_call(pf, {}, call_signals(good, pf), 95.0, 93.0)
    check("chase limit -> 분할 일부만", r["label"] == "분할 일부만", r["label"])
    r = judge_call(pf, {}, call_signals(good, pf), 83.0, 86.0)
    check("stop ref -> 주의", r["level"] == 3)
    check("no optimism without guide raise", judge_call(pf, {}, call_signals(filler, pf), None, None)["label"] == "보유 유지")

    # 2. growth-driven equity softening
    attributed = (t + " Our signed large load contracts have grown to 26 gigawatts. Including $6 billion of incremental projected investment above our base"
                  " capital plan, we project a remaining equity need of approximately $3.5 billion through 2031, consistent with our plan to fund"
                  " additional investment with approximately 40% equity.")
    sig = call_signals(attributed, pf)
    r = judge_call(pf, {}, sig, None, None)
    check("attributed equity -> 보유 유지", r["label"] == "보유 유지" and r["softened"], r["label"])
    check("attribution evidence string", "40%" in r["attr"] and "to fund additional investment" in r["attr"] and "approximately $3.5 billion" in r["attr"], r["attr"])
    check("attribution in reasons", any("성장 투자 몫" in x for x in r["reasons"]))
    unclear = t + " Our signed large load contracts have grown to 26 gigawatts. We now project a remaining equity need of approximately $3.5 billion through 2031."
    r = judge_call(pf, {}, call_signals(unclear, pf), None, None)
    check("unclear equity -> 주의", r["label"] == "주의 | 추가 보류" and not r["softened"], r["label"])
    check("unclear reason text", any("원인 불명확" in x for x in r["reasons"]))
    low_pct = (t + " Our signed large load contracts have grown to 26 gigawatts. With the incremental projects above our base capital plan,"
               " we project a remaining equity need of approximately $3.5 billion, about 20% of the incremental amount.")
    r = judge_call(pf, {}, call_signals(low_pct, pf), None, None)
    check("generic + 20% -> no softening", r["label"] == "주의 | 추가 보류", r["label"])
    lowered = attributed.replace("$4.95 to $5.05", "$4.70 to $4.80")
    r = judge_call(pf, {}, call_signals(lowered, pf), None, None)
    check("softening cannot beat guidance cut", r["label"] == "주의 | 추가 보류", r["label"])
    ok_amt = good.replace("$2 billion", "$2.05 billion")
    check("equity within +0.1 = 유지", call_signals(ok_amt, pf)["equity"] == "유지/감소")

    # 3. long-term range parse/compare
    cases = [("Longer term, we expect adjusted earnings to grow approximately 7%-8% from our 2028 guidance range.", (7.0, 8.0), "유지"),
             ("Longer term, we expect adjusted earnings to grow approximately 7% to 9% from our 2029 guidance range.", (7.0, 9.0), "상향"),
             ("We now expect long-term EPS growth of 6 to 7 percent.", (6.0, 7.0), "하향"),
             ("That puts us at a higher starting point for the 7% to 8% path starting in 2028 and well into the next decade.", (7.0, 8.0), "유지"),
             ("2026 and Long-term EPS Guidance 8% long-term adjusted EPS growth through 2030 '26 to '28: 8% - 9% and 7% - 8% from higher '28 base.", (7.0, 8.0), "유지")]
    for s, want, cmpw in cases:
        got, _ = parse_lt_range(s)
        check(f"lt parse {s[:30]}", got == want and compare_range(got, pf["lt_low"], pf["lt_high"]) == cmpw, f"{got}")
    got, _ = parse_lt_range("Retail electric sales growth of 9% to 10% through 2030 over the long term.")
    check("lt ignores sales growth", got is None, str(got))
    r = judge_call(pf, {}, call_signals(good.replace("$4.95 to $5.05", "$4.85 to $4.95") + " Longer term, we expect adjusted earnings to grow 8% to 9% from our 2029 guidance range.", pf), None, None)
    check("lt raised alone positive", r["label"] == "추가 검토", r["label"])
    r = judge_call(pf, {}, call_signals(good + " Longer term, we expect adjusted earnings to grow 6% to 7% from our 2029 guidance range.", pf), None, None)
    check("lt lowered -> 주의", r["level"] == 3, r["label"])

    # 4. transcript-missing path (offline fake net)
    class FakeNet(Net):
        def __init__(self, tx: str = ""):
            self.tx = tx

        def get(self, url: str, sec: bool = False) -> str:
            if "submissions" in url:
                return json.dumps({"filings": {"recent": {"form": ["8-K"], "filingDate": ["2027-02-18"], "accessionNumber": ["0000092122-27-000010"]}}})
            if url.endswith("index.json"):
                return json.dumps({"directory": {"item": [{"name": "ex99-pressreleaseq42026.htm"}]}})
            if "ex99" in url:
                return Q4_RELEASE
            if "chart" in url:
                return json.dumps({"chart": {"result": []}})
            if "quote/nyse/so" in url:
                return self.tx and '{"u":"\\/earnings\\/call-transcripts\\/2027\\/02\\/18\\/southern-co-so-q4-2026-earnings-call-transcript\\/"}'
            if "q4-2026-earnings-call-transcript" in url and self.tx:
                return "<p>" + self.tx + "</p>"
            if "news.google" in url:
                return "<rss><channel></channel></rss>"
            raise OSError("404")

    def at(s: str) -> datetime:
        return datetime.fromisoformat(s).replace(tzinfo=KST)

    snd = Sender(dry=True)
    st = {"numbers_sent": False, "call_sent": False, "missing_sent": False, "fail_notified": False, "attempts": []}
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):
        st = run(cfg, st, at("2027-02-09 09:00"), "auto", FakeNet(), snd)
        check("before window silent", not snd.sent)
        st = run(cfg, st, at("2027-02-18 21:40"), "auto", FakeNet(), snd)
        check("numbers sent", len(snd.sent) == 1 and "4분기 숫자" in snd.sent[0][0] and st["release"]["date"] == "2027-02-18", str(snd.sent[:1]))
        check("numbers parsed q4 adj", st["release"]["adj_eps"] == 0.55 and st["release"]["fy_adj_eps"] == 4.30, str(st["release"]))
        st = run(cfg, st, at("2027-02-18 23:40"), "auto", FakeNet(), snd)
        check("no call before first attempt", len(snd.sent) == 1)
        for when in ("2027-02-19 08:00", "2027-02-19 10:00", "2027-02-19 13:00", "2027-02-20 09:10"):
            st = run(cfg, st, at(when), "auto", FakeNet(), snd)
        check("no notice before deadline", len(snd.sent) == 1 and len(st["attempts"]) == 4, str(st["attempts"]))
        st = run(cfg, st, at("2027-02-21 09:10"), "auto", FakeNet(), snd)
        check("missing notice once", len(snd.sent) == 2 and UNCONFIRMED in snd.sent[1][0] and st["missing_sent"], str(snd.sent[-1][0]))
        check("missing -> so_verdict 보류", st["so_verdict"] == "보류" and not st["call_sent"])
        st = run(cfg, st, at("2027-02-22 09:00"), "auto", FakeNet(), snd)
        check("missing not repeated", len(snd.sent) == 2)
        # transcript arrives on time
        tx = ("Operator: Welcome to the Southern Company fourth quarter 2026 earnings call. David Poroch: " + attributed + filler * 40)
        snd2 = Sender(dry=True)
        st2 = run(cfg, {"attempts": []}, at("2027-02-18 21:40"), "auto", FakeNet(tx), snd2)
        st2 = run(cfg, st2, at("2027-02-19 08:00"), "auto", FakeNet(tx), snd2)
        check("call verdict from transcript", len(snd2.sent) == 2 and "콜 판정 | 보유 유지" in snd2.sent[1][0], str([s[0] for s in snd2.sent]))
        check("call body has attribution", "성장 투자 몫" in snd2.sent[1][1] and "40%" in snd2.sent[1][1])
        check("so_verdict link", st2["so_verdict"] == "보류" and st2["so_verdict_updated"] == "2027-02-19" and st2["call_sent"])
        snd3 = Sender(dry=True)
        run(cfg, {"attempts": []}, at("2027-02-19 08:00"), "auto", FakeNet(tx.replace("Our signed large load contracts have grown to 26 gigawatts. ", "")), snd3)
        check("late 8-K: numbers + call same run", len(snd3.sent) == 2 and UNCONFIRMED in snd3.sent[1][0], str([s[0] for s in snd3.sent]))
        snd4 = Sender(dry=True)
        run(cfg, {"attempts": []}, at("2028-02-18 21:40"), "auto", FakeNet(), snd4)
        check("year guard", not snd4.sent)

    # 5. Q3 regression (excerpts of the Motley Fool Q3 2026 transcript, Q3 profile)
    q3 = ("Our adjusted EPS estimate for the fourth quarter is $0.30 per share, which, combined with our strong year-to-date performance, would represent full"
          " year adjusted earnings of $4.70 or $0.10 higher than the top of our original 2026 adjusted earnings guidance range, representing 9% year-over-year"
          " adjusted earnings growth. Our portfolio of signed large load contracts has now grown to 21 gigawatts with an additional 7 gigawatts in late stages"
          " across Alabama, Georgia and Mississippi. Together, these projects represent approximately $4 billion of projected investment above our $81 billion"
          " base capital plan that we originally shared in February. Considering the equity addressed to date, along with the incremental $4 billion of"
          " projected investment above our base capital plan, we project a remaining equity need of approximately $2 billion through 2030, consistent with"
          " our communication of to fund additional investment with approximately 40% equity or equity equivalents. And we think that, that's going to put"
          " us in a, if you will, a higher starting point for the 7% to 8% path starting in 2028 and well into the next decade.")
    sig = call_signals(q3, Q3_PROFILE)
    r = judge_call(Q3_PROFILE, {"adj_eps": 1.94}, sig, None, None)
    check("q3 guide 상향", sig["guide"] == "상향", sig["guide"])
    check("q3 gw 21", sig["gw"] == 21, str(sig["gw"]))
    check("q3 equity 2.0 증가", sig["equity_bn"] == 2.0 and sig["equity"] == "증가")
    check("q3 lt 7-8 유지", sig["lt"] == (7.0, 8.0) and sig["lt_cmp"] == "유지", str(sig["lt"]))
    check("q3 regression 보유 유지", r["label"] == "보유 유지" and r["softened"], r["label"])
    check("q3 reasoning 40%", "40%" in r["attr"] and ("incremental" in r["attr"] or "additional" in r["attr"]), r["attr"])
    check("q3 no softening -> 주의", judge_call(Q3_PROFILE, {"adj_eps": 1.94}, {**sig, "equity_attr": False}, None, None)["label"] == "주의 | 추가 보류")

    # 6. numbers parse (Q4 2025 release wording) + fool link discovery + title rules
    n = parse_numbers(Q4_RELEASE, pf)
    check("q4 release parse", n["published"] and n["reported_eps"] == 0.38 and n["adj_eps"] == 0.55 and n["fy_adj_eps"] == 4.30, str(n))
    a, _, lvl = judge_numbers({**pf, "fy_estimate": 4.30}, n, None, None)
    check("numbers judge beat", "보유 유지" in a and lvl == 2, a)
    a, _, lvl = judge_numbers(pf, n, None, None)
    check("numbers judge fy miss -> 주의", a == "주의 | 추가 보류" and lvl == 3, a)
    urls = find_fool_urls('x "\\/earnings\\/call-transcripts\\/2027\\/02\\/18\\/southern-co-so-q4-2026-earnings-call-transcript\\/" '
                          '"/earnings/call-transcripts/2026/11/05/southern-co-so-q3-2026-earnings-call-transcript/"', "q4-2026")
    check("fool link discovery", urls == ["https://www.fool.com/earnings/call-transcripts/2027/02/18/southern-co-so-q4-2026-earnings-call-transcript/"], str(urls))
    check("sa link discovery", find_sa_urls('<a href="/stocks/so/transcripts/730001-q4-2026/">x</a> <a href="/stocks/so/transcripts/720161-q3-2026/">', "q4-2026")
          == ["https://stockanalysis.com/stocks/so/transcripts/730001-q4-2026/"])
    titles = [f"[SO] SO 4분기 숫자 | {x}" for x in ("보유 유지 | 추가는 콜 판정 뒤", "주의 | 추가 보류")]
    titles += [f"[SO] SO 콜 판정 | {x}" for x in LEVELS + [UNCONFIRMED + " | 추가 보류"]]
    titles += [f"[SO] SO {UNCONFIRMED} | 녹취 없음, 추가 보류", "[SO] SO 조회 실패 | 판정 보류"]
    check("titles rule", all(" SO " in x and "매수" not in x and "\u00b7" not in x and "\u2014" not in x for x in titles))
    src = Path(__file__).read_text(encoding="utf-8")
    check("no middle dot/em-dash in source", "\u00b7" not in src and "\u2014" not in src)
    print(f"selftest: {ok} passed, {fail} failed")
    return 1 if fail else 0


Q4_RELEASE = ("Southern Company today reported fourth-quarter earnings of $416 million, or $0.38 per share, in 2025 compared with earnings of $534 million,"
              " or $0.49 per share, in the fourth quarter of 2024. Southern Company also reported full-year 2025 earnings of $4.3 billion, or $3.94 per share,"
              " compared with $4.4 billion, or $4.02 per share, in 2024. Excluding the items described under \"Net Income - Excluding Items\" in the table"
              " below, Southern Company earned $612 million, or $0.55 per share, during the fourth quarter of 2025, compared with $544 million, or $0.50 per"
              " share, during the fourth quarter of 2024. For the full-year 2025, excluding these items, Southern Company earned $4.7 billion, or $4.30 per"
              " share, compared with $4.4 billion, or $4.05 per share, for 2024.")


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry"}:
        print(f"unknown mode {mode}; use auto | dry | selftest")
        return 2
    dry = mode == "dry"
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    net = Net()
    if dry:
        print("DRY RUN (no ntfy, no state write, date gate ignored)")
        reach(net, cfg)
        if os.environ.get("SOQ4_REGRESS") == "q3":
            for u in (Q3_URL, Q3_SA_URL):
                try:
                    print("Q3 regression exit:", regress_q3(net, u))
                except Exception as exc:
                    print("Q3 regression fetch failed:", u, exc)
    test = os.environ.get("SOQ4_TEST_TEXT") if dry else None
    st = load_state()
    run(cfg, st, datetime.now(KST), mode, net, Sender(dry), Path(test).read_text(encoding="utf-8") if test else None)
    save_state(st, datetime.now(KST), dry)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
