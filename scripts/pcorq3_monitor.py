#!/usr/bin/env python3
"""[PCOR-Q3] Procore Q3 2026 earnings verdict (approved in chat 2026-10-26). One push only.

Source: the Q3 8-K EX-99.1 on SEC EDGAR (filed 2026-11-03 after the US close = 11/4 ~06:05 KST).
Parsed: Q3 revenue + YoY, Q3 non-GAAP operating margin, FY27 revenue guidance growth, whether the
25% FY27 non-GAAP operating margin target is reaffirmed, and NEW financing news.

Verdict (configs/pcorq3.json rules):
  증액     FY27 revenue growth >= 13% AND 25% target reaffirmed
  보류     11% <= growth < 13% AND target kept (add only below $46)
  청산     growth < 10% OR 25% target cut OR NEW large convertible/dilutive financing.
           The $950M 0.00% notes due 2031 issued 2026-08-06 are existing, never a trigger.
  확인 필요 10% <= growth < 11%, or a number not parseable (raw numbers in the body).

Forward-looking/cautionary boilerplate is dropped before any stance or financing parse
(same boilerplate filter as the retired soq3/neeq3 parsers, commit 454c322), so risk-factor text never fires.

Runs on the existing morning 09:00 KST slot inside the window (11/4-11/6, year 2026) plus the one-shot
times 11/5 08:00 and 18:00 KST taken over from the retired `pcor` alert (same cron-job.org jobs
8589357/8589366 and GitHub fallback crons). No new cron.

Absorbed from `pcor` (2026-10-26): procore.com/press fallback when EDGAR has no exhibit yet, one
fetch-failure notice (cwally-ops), and reference-only lines: free cash flow, $100K customers YoY, gross
revenue retention, DroneDeploy contribution / organic growth, 2027 guidance wording, a data center
sentence with a number, and pcor's old 통과/보류/축소 checklist bands. None of these change the verdict.
Modes: auto | force (ignore window/state) | dry (print only, no ntfy, no state) | selftest.
Local parser test: PCORQ3_TEST_TEXT=<file> python scripts/pcorq3_monitor.py dry
Not investment advice.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG = ROOT / "configs" / "pcorq3.json"
STATE = ROOT / "state" / "pcorq3.json"
UA = {"User-Agent": "cwally-alerts sosmos3218@gmail.com", "Accept-Encoding": "identity"}
CIK_INT = 1611052
NUM = r"(\d+(?:\.\d+)?)"


# ---------- fetch ----------

def fetch(url: str) -> str:
    with urlopen(Request(url, headers=UA), timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def textify(html: str) -> str:
    t = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    t = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = unescape(t).replace("\xa0", " ").replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    return re.sub(r"\s+", " ", t)


def recent_filings(cfg: dict) -> list[dict]:
    data = json.loads(fetch(cfg["sources"]["sec"]))
    r = data.get("filings", {}).get("recent", {})
    keys = ["form", "filingDate", "accessionNumber", "primaryDocument", "items"]
    return [dict(zip(keys, row)) for row in zip(*(r.get(k, []) for k in keys))]


def exhibit_names(acc: str) -> tuple[str, list[str]]:
    base = f"https://www.sec.gov/Archives/edgar/data/{CIK_INT}/{acc.replace('-', '')}"
    try:
        idx = json.loads(fetch(base + "/index.json"))
        return base, [i.get("name", "") for i in idx.get("directory", {}).get("item", [])]
    except Exception:
        return base, []


def candidate_8ks(filings: list[dict], day: str) -> list[dict]:
    """8-Ks filed on/after the US-Eastern release date (11/3 after the close = 11/4 ~06:05 KST).
    Item 2.02 (results) first; others kept as a fallback in case items are blank."""
    c = [f for f in filings if f.get("form") == "8-K" and f.get("filingDate", "") >= day]
    return sorted(c, key=lambda f: "2.02" not in (f.get("items") or ""))


def sec_release(cfg: dict, filings: list[dict]) -> tuple[str, str]:
    day = cfg["release_filing_date_et"]
    for f in candidate_8ks(filings, day):
        base, names = exhibit_names(f["accessionNumber"])
        ex = [n for n in names if re.search(r"ex-?99|xexx99|exx99", n, re.I) and n.lower().endswith((".htm", ".html"))]
        for name in ex:
            try:
                body = textify(fetch(f"{base}/{name}"))
            except Exception:
                continue
            if re.search(r"third quarter 2026", body, re.I) and re.search(r"Revenue", body):
                return body, f"{base}/{name}"
    return "", ""


def press_release(cfg: dict) -> tuple[str, str]:
    """procore.com/press fallback (from pcor). Only a Q3 2026 results page counts."""
    raw = fetch(cfg["sources"]["press"])
    for href in re.findall(r'href="([^"]+)"', raw, re.I):
        if re.search(r"third-quarter-(fiscal-year-)?2026.*result|q3-2026.*result", href, re.I):
            link = href if href.startswith("http") else "https://www.procore.com" + href
            body = textify(fetch(link))
            if re.search(r"third quarter 2026", body, re.I) and re.search(r"Revenue was", body):
                return body, link
    return "", ""


def new_financing_filings(cfg: dict, filings: list[dict]) -> list[str]:
    """Financing 8-K / prospectus filings after the seed cursor. Existing 2031 notes excluded."""
    ex = cfg["existing_financing"]
    seen_to, since = ex["seen_accessions_through"], ex["filings_since"]
    out = []
    for f in filings:  # newest first
        if f["accessionNumber"] == seen_to or f["filingDate"] < since:
            break
        form, items = f["form"], f.get("items") or ""
        prospectus = form.startswith("424B") or form in {"S-3", "S-3ASR", "S-1"}
        fin8k = form == "8-K" and re.search(r"\b(1\.01|2\.03|3\.02)\b", items)
        if not (prospectus or fin8k):
            continue
        base = f"https://www.sec.gov/Archives/edgar/data/{CIK_INT}/{f['accessionNumber'].replace('-', '')}"
        try:
            text = drop_boilerplate(textify(fetch(f"{base}/{f['primaryDocument']}")))
        except Exception as exc:
            out.append(f"{f['filingDate']} {form} {f['accessionNumber']} (본문 조회 실패: {exc})")
            continue
        for s in financing_sentences(text, ex["exclude_regex"]):
            out.append(f"{f['filingDate']} {form}: {s[:140]}")
            break
    return out


def last_close(cfg: dict) -> tuple[float | None, str]:
    try:
        res = json.loads(fetch(cfg["sources"]["quote"]))["chart"]["result"][0]
        ts, cl = res.get("timestamp") or [], res["indicators"]["quote"][0].get("close") or []
        pts = [(t, c) for t, c in zip(ts, cl) if c is not None]
        if not pts:
            return None, ""
        t, c = pts[-1]
        return float(c), datetime.fromtimestamp(t, timezone(timedelta(hours=-5))).strftime("%m/%d")
    except Exception:
        return None, ""


# ---------- parse ----------

def sentences(text: str) -> list[str]:
    return re.split(r"(?<=[A-Za-z0-9%)\]])\.\s+(?=[A-Z(\"])", text)


BOILER_CUT = r"(Cautionary Note Regarding Forward.Looking|Forward-Looking Statements This|Cautionary Statements? (and|Regarding)|forward-looking statements? (are|include|involve|within the meaning))"
BOILER_SENT = r"(forward.looking|risks? and uncertaint|could cause|may cause|cautionary|risk factors|no assurance|could give rise|subject to conditions that are not anticipated|actual results)"


def drop_boilerplate(text: str) -> str:
    m = re.search(BOILER_CUT, text, re.I)
    if m and m.start() > len(text) * 0.3:
        text = text[:m.start()]
    return " ".join(s + "." for s in sentences(text) if not re.search(BOILER_SENT, s, re.I))


FIN_RE = (r"\b(announc\w*|pric(ed|ing)|complet\w*|closed|clos(es|ing) of|entered into|launch\w*|commenc\w*|issu(ed|es|ance of))\b"
          r"[^.]{0,140}\b(convertible|offering of[^.]{0,60}(common stock|shares)|private placement|at-the-market|equity offering|registered direct)")


def financing_sentences(clean: str, exclude: str) -> list[str]:
    return [s.strip() for s in sentences(clean) if re.search(FIN_RE, s, re.I) and not re.search(exclude, s, re.I)]


def money_m(s: str) -> float | None:
    vals = []
    for v, unit in re.findall(r"\$([\d,]+(?:\.\d+)?)\s*(million|billion)", s, re.I):
        x = float(v.replace(",", ""))
        vals.append(x * 1000 if unit.lower() == "billion" else x)
    return max(vals) if vals else None


def section(clean: str, heads: list[str], n: int = 700) -> str:
    """Text after the first heading match whose body actually holds an 'expected to be' figure.
    The combined banner ("Fourth Quarter, Full Year, and FY '27 Outlook:") is skipped that way."""
    for head in heads:
        for m in re.finditer(head, clean, re.I):
            s = clean[m.end():m.end() + n]
            stop = re.search(r"(Full Year 20\d\d|(First|Second|Third|Fourth) Quarter 20\d\d|FY ?'? ?\d\d) Outlook|A reconciliation|Conference Call|About Procore", s, re.I)
            s = s[:stop.start()] if stop else s
            if re.search(r"expected to be", s, re.I):
                return s
    return ""


FY27_HEADS = [r"Full Year 2027( Preliminary)? Outlook:?", r"Fiscal (Year )?2027( Preliminary)? Outlook:?", r"FY ?'? ?27( Preliminary)? Outlook:?"]


def parse(raw: str, cfg: dict) -> dict:
    clean = drop_boilerplate(raw)
    p: dict = {"published": bool(re.search(r"third quarter 2026", raw, re.I))}
    m = re.search(r"Revenue was \$([\d.,]+) (million|billion), an increase of " + NUM + r"% year-over-year", clean, re.I)
    p["rev_m"] = float(m.group(1).replace(",", "")) * (1000 if m and m.group(2).lower() == "billion" else 1) if m else None
    p["rev_yoy"] = float(m.group(3)) if m else None
    t = re.search(r"Revenue \$ ?([\d,]{6,}) \$ ?([\d,]{6,})", raw)
    if t:
        a, b = float(t.group(1).replace(",", "")), float(t.group(2).replace(",", ""))
        if b > 0 and 0.5 < a / b < 2:
            p["rev_yoy_exact"] = round((a / b - 1) * 100, 2)
            p["rev_k"] = a
            p["rev_prior_k"] = b
            p["rev_m"] = p["rev_m"] or round(a / 1000, 1)
            p["rev_yoy"] = p["rev_yoy"] if p["rev_yoy"] is not None else p["rev_yoy_exact"]
    m = re.search(r"non-GAAP operating margin was " + NUM + "%", clean, re.I)
    p["q3_om"] = float(m.group(1)) if m else None
    t = re.search(r"Non-GAAP income from operations \$ ?([\d,]{5,})", raw)
    rk = p.get("rev_k") or (p["rev_m"] * 1000 if p.get("rev_m") else None)
    if t and rk:
        p["q3_om_exact"] = round(float(t.group(1).replace(",", "")) / rk * 100, 2)
    # FY27 outlook
    fy27 = section(clean, FY27_HEADS)
    p["fy27_text"] = fy27[:300]
    g = re.search(r"Revenue[^.]{0,160}?growth of (?:approximately |about |at least )?" + NUM + r"%(?: to " + NUM + "%)?", fy27, re.I)
    p["fy27_growth"] = float(g.group(1)) if g else None
    p["fy27_growth_hi"] = float(g.group(2)) if g and g.group(2) else None
    p["fy27_growth_calc"] = False
    rng = re.search(r"Revenue is expected to be (?:in the range of )?\$([\d,]+(?:\.\d+)?) (million|billion)(?: to \$([\d,]+(?:\.\d+)?) (million|billion))?", fy27, re.I)
    p["fy27_rev"] = rng.group(0)[len("Revenue is expected to be "):] if rng else None
    if p["fy27_growth"] is None and rng:
        fy26 = section(clean, [r"Full Year 2026 Outlook:?"])
        r26 = re.search(r"Revenue is expected to be (?:in the range of )?\$([\d,]+(?:\.\d+)?) (million|billion)(?: to \$([\d,]+(?:\.\d+)?))?", fy26, re.I)
        if r26:
            lo27 = float(rng.group(1).replace(",", ""))
            hi26 = float((r26.group(3) or r26.group(1)).replace(",", ""))
            if hi26 > 0:
                p["fy27_growth"] = round((lo27 / hi26 - 1) * 100, 1)
                p["fy27_growth_calc"] = True
    o = re.search(r"Non-GAAP operating margin is expected to be (?:at least |approximately |about )?" + NUM + r"%(?: to " + NUM + "%)?", fy27, re.I)
    p["fy27_om"] = float(o.group(1)) if o else None
    target = float(cfg["rules"]["om_target"])
    cut_re = r"(lower\w*|reduc\w*|cut|withdr\w*|no longer|revis\w* (down|lower)|below)[^.]{0,80}(2027|FY ?'? ?27)[^.]{0,80}25%|25%[^.]{0,80}(2027|FY ?'? ?27)[^.]{0,80}(lower\w*|reduc\w*|withdr\w*|no longer|delay\w*|push\w* out)"
    keep_re = r"(reaffirm\w*|reiterat\w*|on track|remain\w* committed|confiden\w*)[^.]{0,120}25%|25%[^.]{0,80}(2027|FY ?'? ?27)[^.]{0,60}(reaffirm\w*|on track|unchanged)"
    if p["fy27_om"] is not None:
        p["om_target"] = "유지" if p["fy27_om"] >= target else "하향"
    elif re.search(cut_re, clean, re.I):
        p["om_target"] = "하향"
    elif re.search(keep_re, clean, re.I):
        p["om_target"] = "유지"
    else:
        p["om_target"] = "미확인"
    fin = financing_sentences(clean, cfg["existing_financing"]["exclude_regex"])
    p["financing"] = [s[:160] for s in fin]
    p.update(reference_metrics(raw, clean))
    return p


def first(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text, re.I)
    return float(m.group(1).replace(",", "")) if m else None


def reference_metrics(raw: str, clean: str) -> dict:
    """Reference-only figures ported from pcor. Wording checks use the boilerplate-free text."""
    r = {
        "fcf_m": first(r"free cash (?:in)?flow(?: for the (?:first|second|third|fourth) quarter)? (?:was|of) \$?\s*" + NUM + r"\s*million", clean),
        "cust_100k": first(r"customers contributing more than \$100,000 of annual recurring revenue totaled ([0-9,]{3,})", clean),
        "cust_yoy": first(r"100,000 of annual recurring revenue totaled [0-9,]+[^.]{0,60}?increase of " + NUM + r"\s*(?:%|percent)", clean),
        "grr": first(r"gross revenue retention rate of " + NUM + r"\s*(?:%|percent)", clean),
        "dd_rev": first(r"DroneDeploy[^.]{0,80}?(?:contributed|contribution of|revenue of|added)\s*(?:approximately\s*)?\$\s*" + NUM + r"\s*million", clean),
        "organic_yoy": first(r"organic revenue (?:growth|grew|increased)[^.]{0,40}?" + NUM + r"\s*(?:%|percent)", clean)
        or first(r"excluding (?:the (?:impact|contribution) of )?(?:the )?DroneDeploy[^.]{0,80}?(?:grew|growth of|increased|increase of)\s*" + NUM + r"\s*(?:%|percent)", clean),
        "g27_organic": first(r"2027[^.]{0,160}?(?:organic|excluding (?:the (?:impact|contribution) of )?(?:the )?DroneDeploy)[^.]{0,100}?" + NUM + r"\s*(?:%|percent)", clean),
        "dd_mentioned": "DroneDeploy" in clean,
    }
    if r["dd_rev"] is None:
        r["dd_rev"] = first(r"\$\s*" + NUM + r"\s*million[^.]{0,60}?(?:from|attributable to|contributed by)\s*(?:the\s*)?DroneDeploy", clean)
    if r["dd_rev"] is not None and r["dd_rev"] >= 100:
        r["dd_rev"] = None  # a quarterly contribution of $100M+ is not plausible (purchase price, ARR...); not a disclosure
    w = "미확인"
    if re.search(r"(lower\w*|reduc\w*|cut|below prior)[^.]{0,60}(2027|FY ?'? ?27)|(2027|FY ?'? ?27)[^.]{0,60}(lower\w*|reduc\w*|below prior)", clean, re.I):
        w = "하향 표현"
    elif re.search(r"(rais\w*|increas\w*)[^.]{0,60}(2027|FY ?'? ?27)[^.]{0,40}(outlook|guidance|target)", clean, re.I):
        w = "상향 표현"
    elif re.search(r"(reaffirm\w*|maintain\w*|reiterat\w*)[^.]{0,80}(2027|FY ?'? ?27)", clean, re.I):
        w = "유지 표현"
    r["g27_wording"] = w
    r["dc_sentence"] = None
    for sent in sentences(clean):
        if len(sent) <= 400 and re.search(r"data cent(?:er|re)s?", sent, re.I) and re.search(r"\$\s?[0-9]|[0-9]+(?:\.[0-9]+)?\s*(?:%|percent)", sent):
            r["dc_sentence"] = sent.strip()[:200]
            break
    return r


def ref_band(v, pass_at: float, hold_at: float) -> str:
    if v is None:
        return "미확인"
    return "통과" if v >= pass_at else ("보류" if v >= hold_at else "축소")


def reference_line(p: dict, cfg: dict) -> str:
    t = cfg.get("reference_thresholds") or {}
    parts = []
    if t:
        parts.append("옛 pcor 채점(판정 미반영): 매출 " + ref_band(p.get("organic_yoy") if p.get("organic_yoy") is not None else p.get("rev_yoy"), t["rev_pass"], t["rev_hold"])
                     + ", 영업이익률 " + ref_band(p.get("q3_om"), t["om_pass"], t["om_hold"])
                     + (f", FCF {p['fcf_m']:g}백만달러 " + ref_band(p["fcf_m"], t["fcf_pass"], t["fcf_hold"]) if p.get("fcf_m") is not None else ", FCF 미확인")
                     + f", 10만달러 고객 전년비 {fmt(p.get('cust_yoy'))} " + ref_band(p.get("cust_yoy"), t["cust_pass"], t["cust_hold"])
                     + f", 총매출 유지율 {fmt(p.get('grr'))} " + ref_band(p.get("grr"), t["grr_pass"], t["grr_hold"]))
    if p.get("dd_rev") is not None:
        parts.append(f"DroneDeploy 기여 매출 {p['dd_rev']:g}백만달러")
    elif p.get("organic_yoy") is not None:
        parts.append(f"DroneDeploy 제외 증가율 {p['organic_yoy']:g}%")
    else:
        parts.append("DroneDeploy 기여분 미공시(3분기와 FY27 성장률에 인수 효과 포함 가능)")
    if p.get("g27_organic") is not None:
        parts.append(f"FY27 DroneDeploy 제외 성장 {p['g27_organic']:g}%")
    parts.append(f"2027 문구 {p.get('g27_wording', '미확인')}")
    if p.get("dc_sentence"):
        parts.append("데이터센터 원문: " + p["dc_sentence"])
    return "참고: " + ". ".join(parts) + "."


# ---------- decide ----------

def dd_basis(p: dict) -> tuple[str | None, float | None]:
    """Growth basis for the 13% test once DroneDeploy is disclosed (cap rule, approved 2026-10-26).
    Priority: FY27 ex-DroneDeploy growth > stated Q3 ex-DroneDeploy/organic growth > Q3 organic computed
    from the disclosed DroneDeploy revenue. (None, None) = not disclosed. Only boilerplate-free text is
    parsed (reference_metrics uses `clean`), so a risk-factor mention of DroneDeploy never counts."""
    if p.get("g27_organic") is not None:
        return "FY27 DroneDeploy 제외 성장", p["g27_organic"]
    if p.get("organic_yoy") is not None:
        return "3분기 DroneDeploy 제외 성장", p["organic_yoy"]
    if p.get("dd_rev") is not None and p.get("rev_k") and p.get("rev_prior_k"):
        return f"3분기 DroneDeploy 제외 계산(기여 {p['dd_rev']:g}백만달러 차감)", round(((p["rev_k"] / 1000 - p["dd_rev"]) / (p["rev_prior_k"] / 1000) - 1) * 100, 1)
    if p.get("dd_rev") is not None:
        return "보고 기준(DroneDeploy 기여 매출 공시, 전년 매출 표 없음)", p.get("fy27_growth")
    return None, None


def judge(p: dict, fin_filings: list[str], cfg: dict) -> tuple[str, list[str]]:
    """Base verdict, then the DroneDeploy cap: only 증액 can move (to 보류); 청산/확인 필요/보류 untouched."""
    v, why = judge_base(p, fin_filings, cfg)
    p["dd_cap"] = None
    if v != "증액":
        return v, why
    r = cfg["rules"]
    label, val = dd_basis(p)
    g = p.get("fy27_growth")
    if label is None:
        p["dd_cap"] = "capped"
        return "보류", [f"DroneDeploy 기여분 미공시라 최대 보류(보고 기준 FY27 성장 {g:g}%, 인수 효과 포함 가능)", "25% 목표 유지"]
    p["dd_cap"] = f"{label} {fmt(val)}"
    if val is not None and val >= r["grow_up"]:
        return "증액", why + [f"판정 기준 {label} {val:g}% >= {r['grow_up']:g}%"]
    return "보류", [f"보고 기준 FY27 성장 {g:g}% 이지만 {label} {fmt(val)} < {r['grow_up']:g}%라 보류", "25% 목표 유지"]


def judge_base(p: dict, fin_filings: list[str], cfg: dict) -> tuple[str, list[str]]:
    r = cfg["rules"]
    why = []
    g, om = p.get("fy27_growth"), p.get("om_target")
    fin_all = p.get("financing", []) + fin_filings
    big = [s for s in fin_all if (money_m(s) or 0) >= r["financing_min_m"]]
    unknown_size = [s for s in fin_all if money_m(s) is None]
    if g is not None and g < r["cut_below"]:
        why.append(f"FY27 매출 성장 {g:g}% < {r['cut_below']:g}%")
    if om == "하향":
        why.append("25% 영업이익률 목표 하향" + (f"(FY27 {p['fy27_om']:g}%)" if p.get("fy27_om") is not None else "(문구 기준)"))
    if big:
        why.append("새 대규모 조달: " + big[0][:100])
    if why:
        return "청산", why
    if g is None or om != "유지" or unknown_size:
        if g is None:
            why.append("FY27 매출 가이던스 성장률 못 읽음")
        if om != "유지":
            why.append(f"25% 목표 {om}")
        if unknown_size:
            why.append("규모 미상 새 조달 문구: " + unknown_size[0][:100])
        return "확인 필요", why
    if g < r["hold_low"]:
        return "확인 필요", [f"FY27 매출 성장 {g:g}% (10~11% 경계 구간)"]
    if g >= r["grow_up"]:
        return "증액", [f"FY27 매출 성장 {g:g}% >= {r['grow_up']:g}%", "25% 목표 유지"]
    return "보류", [f"FY27 매출 성장 {g:g}% (11~13%)", "25% 목표 유지"]


def fmt(v, unit="%") -> str:
    return "미확인" if v is None else f"{v:g}{unit}"


def compose(cfg: dict, verdict: str, why: list[str], p: dict, fin_filings: list[str], src: str, px: float | None, pxd: str) -> tuple[str, str, int]:
    r, b = cfg["rules"], cfg["baseline"]
    g = p.get("fy27_growth")
    gtxt = fmt(g) + (" 계산값(FY27 하단/FY26 가이던스 상단)" if p.get("fy27_growth_calc") else "")
    if p.get("fy27_growth_hi") is not None:
        gtxt += f"~{p['fy27_growth_hi']:g}%(하단 기준 판정)"
    head = {"증액": "계획대로 증액 검토", "보류": f"보유 유지 | 추가는 {r['hold_add_below']:g}달러 아래만",
            "청산": "청산 검토 | 추가 매수 금지", "확인 필요": "원문 숫자 직접 확인"}[verdict]
    title = f"[PCOR-Q3] {verdict} | {head}"
    todo = {
        "증액": "증액은 콜(11/4 22:30 KST) 뒤 분할로. 갭 상승 첫 30분 추격은 하지 않기.",
        "보류": f"보유 유지. 추가 매수는 종가 {r['hold_add_below']:g}달러 아래에서만.",
        "청산": "청산 계획 실행 검토(분할 매도). 추가 매수 중단.",
        "확인 필요": "위 원문 숫자를 직접 보고 증액/보류/청산 중 고르기. 자동 판정 없음.",
    }[verdict]
    fin_all = p.get("financing", []) + fin_filings
    nums = (f"3분기 매출 {fmt(p.get('rev_m'), '백만달러')}, 전년비 {fmt(p.get('rev_yoy'))}"
            + (f"(표 계산 {p['rev_yoy_exact']:g}%)" if p.get("rev_yoy_exact") is not None else "")
            + f", non-GAAP 영업이익률 {fmt(p.get('q3_om'))}"
            + (f"(표 계산 {p['q3_om_exact']:g}%)" if p.get("q3_om_exact") is not None else "")
            + f" [가이던스 {b['q3_guide_low_m']}~{b['q3_guide_high_m']}백만달러, {b['q3_guide_om']}]."
            + f" FY27 매출 가이던스 {p.get('fy27_rev') or '미기재'}, 성장 {gtxt}. FY27 non-GAAP 영업이익률 {fmt(p.get('fy27_om'))}, 25% 목표 {p.get('om_target')}."
            + (f" 판정 기준: {p['dd_cap']}." if p.get("dd_cap") and p["dd_cap"] != "capped" else (" 판정 기준: 보고 기준(DroneDeploy 기여분 미공시로 최대 보류)." if p.get("dd_cap") == "capped" else ""))
            + f" 새 조달: {' / '.join(s[:90] for s in fin_all) if fin_all else '없음(기존 2031 만기 9.5억 달러 전환사채는 제외)'}.")
    pxt = f"현재 종가 {px:.2f}달러({pxd})." if px else "종가 조회 안 됨."
    lines = [
        f"{datetime.now(KST):%Y-%m-%d %H:%M} KST | PCOR | 3분기 판정",
        "이게 뭐 알림: " + cfg["explain"],
        f"지금 결과/상태: {verdict}. 근거: {'; '.join(why)}. {nums} {pxt} 원문 {src}",
        "지금 할 일: " + todo,
        "나중에: 콜 요약에서 DroneDeploy 기여 매출과 2027 가이던스 문구 다시 확인. 일회 알림이라 다시 오지 않음.",
        "익절/줄일 때: FY27 성장 10% 미만, 25% 목표 하향, 새 대규모 전환사채/증자 중 하나라도 확인되면 비중 축소.",
        "하지 말 것: 실적 직후 갭 추격, 계획 초과 매수, 위험요인 문구만 보고 단정, 신용/미수. 투자 권유 아님.",
        reference_line(p, cfg),
    ]
    prio = {"증액": 4, "보류": 3, "청산": 5, "확인 필요": 4}[verdict]
    return title, "\n".join(lines), prio


# ---------- state / send ----------

def load_state() -> dict:
    st = {"final_notified": False, "missing_notified": False, "verdict": None}
    if STATE.exists():
        try:
            st.update(json.loads(STATE.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return st


def save_state(st: dict, now: datetime, dry: bool) -> None:
    if dry:
        return
    st["updated_at"] = now.isoformat(timespec="seconds")
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def send(dry: bool, title: str, msg: str, prio: int, kind: str = "port") -> None:
    if dry:
        print(f"DRY RUN would send (p{prio}): {title}\n{msg}\n")
        return
    publish(resolve_topic(kind), title, msg, prio)


# ---------- selftest ----------

DD_ORG14 = "Excluding DroneDeploy, revenue grew 14% year-over-year. "


def synth(growth: str | None = "growth of 14%", om27: str | None = "25%", extra: str = "", fy27_rev: bool = True, dd: str = DD_ORG14) -> str:
    fy27 = ""
    if growth is not None or om27 is not None:
        fy27 = "• Full Year 2027 Outlook: "
        if growth is not None:
            fy27 += ("◦ Revenue is expected to be in the range of $1,720 million to $1,730 million, representing year-over-year " + growth + ". ") if fy27_rev else ""
        if om27 is not None:
            fy27 += f"◦ Non-GAAP operating margin is expected to be {om27}. "
    return (
        "Procore Announces Third Quarter 2026 Financial Results CARPINTERIA, CA - November 3, 2026 - Procore today announced results. "
        "Third Quarter 2026 Financial Highlights: • Revenue was $386 million, an increase of 14% year-over-year. "
        "• GAAP operating margin was 2% and non-GAAP operating margin was 20%. " + dd +
        "Fourth Quarter, Full Year, and FY '27 Outlook: • Fourth Quarter 2026 Outlook: ◦ Revenue is expected to be in the range of $400 million to $402 million. "
        "• Full Year 2026 Outlook: ◦ Revenue is expected to be in the range of $1,520 million to $1,522 million, representing year-over-year growth of 15%. "
        + fy27 + "A reconciliation of non-GAAP guidance measures is not available. " + extra +
        " Forward-Looking Statements This press release contains forward-looking statements within the meaning of the Securities Act, including our ability to"
        " service our convertible senior notes, and we announced that we may issue additional convertible notes or equity offerings of common stock which could dilute stockholders and"
        " may cause actual results to differ; our 25% FY27 target may be lowered. Revenue $ 386,100 $ 338,900 Non-GAAP income from operations $ 77,500"
    )


def selftest() -> int:
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    fails = 0

    def case(label, text, want, fin_filings=None):
        nonlocal fails
        p = parse(text, cfg)
        v, why = judge(p, fin_filings or [], cfg)
        ok = v == want
        fails += not ok
        print(("ok  " if ok else "FAIL"), f"{label}: {v} ({'; '.join(why)})" + ("" if ok else f" want {want} | {json.dumps(p, ensure_ascii=False)[:400]}"))
        return p

    p = case("growth 14 + 25% -> 증액", synth(), "증액")
    ok = p["rev_yoy"] == 14 and p["q3_om"] == 20 and p.get("rev_yoy_exact") == 13.93 and p.get("q3_om_exact") == 20.07
    fails += not ok
    print("ok  " if ok else "FAIL", "q3 numbers", p["rev_yoy"], p["q3_om"], p.get("rev_yoy_exact"), p.get("q3_om_exact"))
    case("growth 13 exactly -> 증액", synth("growth of 13%"), "증액")
    case("growth 12 -> 보류", synth("growth of 12%"), "보류")
    case("growth 11 -> 보류", synth("growth of 11%"), "보류")
    case("growth 10.5 -> 확인 필요", synth("growth of 10.5%"), "확인 필요")
    case("growth 9 -> 청산", synth("growth of 9%"), "청산")
    case("om 23% -> 청산", synth("growth of 14%", "23%"), "청산")
    case("om at least 25% -> 증액", synth("growth of 14%", "at least 25%"), "증액")
    case("range 12% to 14% uses low -> 보류", synth("growth of 12% to 14%"), "보류")
    case("no FY27 section -> 확인 필요", synth(None, None), "확인 필요")
    case("no FY27 section but growth<10 impossible; om only -> 확인 필요", synth(None, "25%"), "확인 필요")
    case("FY27 range w/o growth -> computed 13.0 -> 증액", synth("", "25%").replace("representing year-over-year . ", ""), "증액")
    case("boilerplate dilution/convertible text never fires", synth(), "증액")
    case("existing 2031 notes mentioned -> not new", synth(extra="In August, we completed the offering of $950 million of 0.00% Convertible Senior Notes due 2031 and closed the DroneDeploy acquisition."), "증액")
    case("new $600M convertible in release -> 청산", synth(extra="On November 3, 2026, Procore announced a proposed private offering of $600 million of Convertible Senior Notes due 2032."), "청산")
    case("new financing filing -> 청산", synth(), "청산", ["2026-11-05 8-K: Procore priced $500 million of convertible senior notes due 2032"])
    case("new financing unknown size -> 확인 필요", synth(), "확인 필요", ["2026-11-05 424B5: Procore commenced an at-the-market offering of common stock"])
    case("explicit cut sentence w/o outlook -> 청산", synth(None, None, extra="We are lowering our FY27 non-GAAP operating margin target of 25% to reflect DroneDeploy."), "청산")
    case("reaffirm sentence w/o outlook om, growth 14 -> 증액", synth("growth of 14%", None, extra="We reaffirm our FY27 non-GAAP operating margin target of 25%."), "증액")
    # filing window: the Q3 8-K is dated 11/3 (US/Eastern) although it lands ~11/4 06:05 KST
    fl = [{"form": "8-K", "filingDate": "2026-11-03", "accessionNumber": "a-q3", "items": "2.02,9.01"},
          {"form": "4", "filingDate": "2026-11-03", "accessionNumber": "a-f4", "items": ""},
          {"form": "8-K", "filingDate": "2026-11-03", "accessionNumber": "a-other", "items": "5.02"},
          {"form": "8-K", "filingDate": "2026-11-02", "accessionNumber": "a-old", "items": "2.02"},
          {"form": "8-K", "filingDate": "2026-09-09", "accessionNumber": "a-dd", "items": "8.01"}]
    got = [f["accessionNumber"] for f in candidate_8ks(fl, cfg["release_filing_date_et"])]
    ok = got == ["a-q3", "a-other"]
    fails += not ok
    print("ok  " if ok else "FAIL", "8-K dated 11/3 is a candidate, 2.02 first, 11/2 and older ignored:", got)
    # reference metrics ported from pcor (never change the verdict)
    extra = ("Free cash inflow for the third quarter was $70 million, an increase of 20% year-over-year. "
             "Achieved a gross revenue retention rate of 94% in the third quarter. "
             "Number of organic customers contributing more than $100,000 of annual recurring revenue totaled 2,950 as of September 30, 2026, an increase of 13% year-over-year. "
             "DroneDeploy contributed approximately $9 million of revenue in the quarter. "
             "Data center projects on the platform grew 40% year-over-year.")
    p = case("reference metrics do not change verdict", synth(extra=extra), "증액")
    want = {"fcf_m": 70.0, "grr": 94.0, "cust_100k": 2950.0, "cust_yoy": 13.0, "dd_rev": 9.0}
    got = {k: p.get(k) for k in want}
    ok = got == want and bool(p.get("dc_sentence"))
    fails += not ok
    print("ok  " if ok else "FAIL", "reference metrics", got, "| dc:", (p.get("dc_sentence") or "")[:50])
    line = reference_line(p, cfg)
    ok = "총매출 유지율 94% 보류" in line and "DroneDeploy 기여 매출 9백만달러" in line and "FCF 70백만달러 통과" in line
    fails += not ok
    print("ok  " if ok else "FAIL", line[:160])
    p = parse(synth(), cfg)
    ok = p.get("dc_sentence") is None and p.get("g27_wording") == "미확인"
    fails += not ok
    print("ok  " if ok else "FAIL", "boilerplate '25% FY27 target may be lowered' is not a 2027 wording signal:", p.get("g27_wording"))
    ok = bool(re.search(r"third-quarter-(fiscal-year-)?2026.*result|q3-2026.*result", "/press/procore-announces-third-quarter-2026-financial-results", re.I)) and not re.search(r"third-quarter-(fiscal-year-)?2026.*result|q3-2026.*result", "/press/procore-announces-timing-of-third-quarter-fiscal-year-2026-earnings-call", re.I)
    fails += not ok
    print("ok  " if ok else "FAIL", "press fallback link: results page yes, timing page no")
    # DroneDeploy cap (approved 2026-10-26): undisclosed -> 증액 capped to 보류; disclosed -> 13% test on ex-DroneDeploy basis
    p = case("cap: no DroneDeploy disclosure, FY27 14% -> 보류", synth(dd=""), "보류")
    ok = p.get("dd_cap") == "capped"
    fails += not ok
    print("ok  " if ok else "FAIL", "cap flag", p.get("dd_cap"))
    t, body, _ = compose(cfg, "보류", judge(p, [], cfg)[1], p, [], "test", 60.0, "11/03")
    ok = "DroneDeploy 기여분 미공시라 최대 보류(보고 기준 FY27 성장 14%" in body
    fails += not ok
    print("ok  " if ok else "FAIL", "cap text in push:", body.split("근거: ")[1][:90])
    p = case("disclosed FY27 ex-DroneDeploy 13.5% -> 증액", synth(dd="", extra="The FY27 outlook implies 2027 revenue growth excluding DroneDeploy of 13.5%."), "증액")
    print("     basis:", p.get("dd_cap"))
    case("disclosed Q3 organic 12% (FY27 reported 14%) -> 보류", synth(dd="Organic revenue growth was 12% year-over-year. "), "보류")
    p = case("disclosed DroneDeploy revenue $9M -> computed organic 11.3% -> 보류", synth(dd="DroneDeploy contributed approximately $9 million of revenue in the quarter. "), "보류")
    print("     basis:", p.get("dd_cap"))
    case("disclosed DroneDeploy revenue $2M -> computed organic 13.3% -> 증액", synth(dd="DroneDeploy contributed approximately $2 million of revenue in the quarter. "), "증액")
    case("청산 unaffected by cap (FY27 9%, no disclosure)", synth("growth of 9%", dd=""), "청산")
    case("청산 unaffected by cap (new $600M convertible, organic 14%)", synth(extra="On November 3, 2026, Procore announced a proposed private offering of $600 million of Convertible Senior Notes due 2032."), "청산")
    case("확인 필요 unaffected by cap (10.5%, no disclosure)", synth("growth of 10.5%", dd=""), "확인 필요")
    case("확인 필요 unaffected by cap (no FY27 section)", synth(None, None, dd=""), "확인 필요")
    case("보류 stays 보류 (12%, no disclosure)", synth("growth of 12%", dd=""), "보류")
    case("purchase price sentence is not a contribution -> capped 보류", synth(dd="", extra="DroneDeploy added $845 million of goodwill and intangibles."), "보류")
    bp = synth(dd="", extra="DroneDeploy is now part of Procore.")
    bp = bp.replace("may cause actual results to differ;", "may cause actual results to differ; DroneDeploy contributed approximately $40 million of revenue and organic revenue growth was 20%, which could cause results to differ;")
    p = case("boilerplate DroneDeploy figures are not disclosure -> capped 보류", bp, "보류")
    ok = p.get("dd_rev") is None and p.get("organic_yoy") is None and p.get("dd_mentioned")
    fails += not ok
    print("ok  " if ok else "FAIL", "boilerplate: dd_rev", p.get("dd_rev"), "organic", p.get("organic_yoy"), "mentioned", p.get("dd_mentioned"))
    t, body, pr = compose(cfg, "보류", ["x"], parse(synth("growth of 12%"), cfg), [], "test", 55.0, "11/03")
    bad = any(ch in t + body for ch in "\u00b7\u2014")
    fails += bad
    print("ok  " if not bad else "FAIL", "no middle dot / em-dash;", t)
    print("selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


# ---------- main ----------

def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode == "selftest":
        return selftest()
    dry = mode == "dry"
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    st = load_state()
    start = datetime.fromisoformat(cfg["window_start_kst"]).replace(tzinfo=KST)
    end = datetime.fromisoformat(cfg["window_end_kst"]).replace(tzinfo=KST)
    in_win = start <= now <= end and now.year == int(cfg["event_year"])
    print(f"{now.isoformat(timespec='seconds')} pcorq3 mode={mode} window={in_win}")
    if dry:
        print("DRY RUN (no ntfy, no state write)")
    if mode == "auto" and not in_win:
        print("outside window; silent")
        return 0
    if mode == "auto" and st.get("final_notified"):
        print("verdict already sent; silent")
        return 0
    test = os.environ.get("PCORQ3_TEST_TEXT") if dry else None
    err: list[str] = []
    filings: list[dict] = []
    if test:
        raw, src = Path(test).read_text(encoding="utf-8"), "테스트 텍스트"
    else:
        try:
            filings = recent_filings(cfg)
            raw, src = sec_release(cfg, filings)
        except Exception as exc:
            raw, src = "", ""
            err.append(f"sec: {exc}")
        if not raw:
            try:
                raw, src = press_release(cfg)
            except Exception as exc:
                err.append(f"press: {exc}")
    fin_filings: list[str] = []
    if filings:
        try:
            fin_filings = new_financing_filings(cfg, filings)
        except Exception as exc:
            err.append(f"financing scan: {exc}")
    print("src:", src or "-", "| new financing filings:", fin_filings or "none", "| err:", err)
    if not raw and len(err) >= 2 and mode == "auto" and not st.get("fail_notified"):
        # both EDGAR and procore.com failed (pcor's 조회 실패 notice; routed to cwally-ops, never a trade signal)
        send(dry, "[PCOR-Q3] 조회 실패 | 판정 보류", f"SEC EDGAR 와 procore.com 조회가 모두 실패. {'; '.join(err)}. 다음 실행에서 다시 봅니다. 이 알림으로 매매 금지.", 3, "ops")
        st["fail_notified"] = True
        save_state(st, now, dry)
        return 0
    if not raw:
        last_try = now.date() >= end.date()
        if mode == "auto" and last_try and not st.get("missing_notified"):
            msg = f"Procore 3분기 8-K EX-99.1 을 {now:%m/%d %H:%M} KST 까지 못 찾음. {'; '.join(err)} 회사 IR 직접 확인. 이 알림으로 매매 금지."
            send(dry, "[PCOR-Q3] 원문 미게시 | 판정 보류", msg, 3)  # route_topic -> cwally-ops
            st["missing_notified"] = True
        else:
            print("Q3 release not found yet; retry at next 09:00 run" if not dry else "no Q3 release yet (expected 2026-11-03 after US close)")
        save_state(st, now, dry)
        return 0
    p = parse(raw, cfg)
    print("parse:", json.dumps({k: v for k, v in p.items() if k != "fy27_text"}, ensure_ascii=False))
    verdict, why = judge(p, fin_filings, cfg)
    px, pxd = (None, "") if test else last_close(cfg)
    title, msg, prio = compose(cfg, verdict, why, p, fin_filings, src, px, pxd)
    send(dry, title, msg, prio)
    st.update({"final_notified": True, "verdict": verdict, "why": why, "src": src,
               "fy27_growth": p.get("fy27_growth"), "om_target": p.get("om_target")})
    save_state(st, now, dry)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
