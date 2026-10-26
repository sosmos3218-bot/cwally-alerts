#!/usr/bin/env python3
"""[NFLX-Q4] Netflix Q4 2026 earnings verdict (requested in chat 2026-10-26). One push only.

Source: the Q4'26 shareholder letter, 8-K EX-99.1 on SEC EDGAR (US close, ~06:01 KST next day).
Parsed: 2027 revenue guidance growth (range low/mid), 2027 operating margin target, 2026 actual operating
margin, 2027 free cash flow outlook, 2026 actual FCF (reference), and whether the 2027 ad tier expansion
(15 new countries, first launches March 2027) is on track or delayed.

Verdict (configs/nflxq4.json rules, from the 2026-10-22 NFLX analysis):
  강화       2027 growth low end >= 10% AND 2027 OM target > 2026 actual (31.5% if unread) AND ads on track
  중단       2027 growth midpoint < 10% AND 2027 FCF outlook < $11.0B (recurring ~$10.5B stalls)
  중간       neither (3rd tranche on hold, keep holding)
  확인 필요  growth or 2027 OM not parseable, ads unknown while growth/OM pass, or FCF unknown with growth < 10%
Separate 축소 검토 line: last close >= $85 while 2027 FCF outlook < $11.0B or missing.

Forward-looking boilerplate is dropped before any wording check, so risk-factor text never fires.
Runs on the existing morning 09:00 KST slot inside 2027-01-12 ~ 2027-02-05 (year guard 2027). No new cron.
Modes: auto | force (ignore window/state) | dry (print only, no ntfy, no state) | selftest.
Local parser test: NFLXQ4_TEST_TEXT=<file> python scripts/nflxq4_monitor.py dry
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
CFG = ROOT / "configs" / "nflxq4.json"
STATE = ROOT / "state" / "nflxq4.json"
UA = {"User-Agent": "cwally-alerts sosmos3218@gmail.com", "Accept-Encoding": "identity"}
CIK_INT = 1065280
NUM = r"(\d+(?:\.\d+)?)"


# ---------- fetch ----------

def fetch(url: str) -> str:
    with urlopen(Request(url, headers=UA), timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def textify(html: str) -> str:
    t = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    t = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = unescape(t).replace("\xa0", " ").replace("’", "'").replace("“", '"').replace("”", '"')
    t = t.replace("–", "-").replace("—", "-")
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
    """8-Ks filed on/after the window's US-Eastern start date. Item 2.02 (results) first."""
    c = [f for f in filings if f.get("form") == "8-K" and f.get("filingDate", "") >= day]
    return sorted(c, key=lambda f: "2.02" not in (f.get("items") or ""))


def is_q4_letter(body: str) -> bool:
    """Q4'26 actuals plus a Q1'27 forecast column. The Q3'26 letter has Q4'26 (forecast) and 2027 but no Q1'27."""
    return bool(re.search(r"Q4'26", body) and re.search(r"Q1'27", body) and re.search(r"Revenue", body))


def sec_release(cfg: dict, filings: list[dict]) -> tuple[str, str]:
    for f in candidate_8ks(filings, cfg["release_filing_from_et"]):
        base, names = exhibit_names(f["accessionNumber"])
        ex = [n for n in names if re.search(r"ex-?99|exx99", n, re.I) and n.lower().endswith((".htm", ".html"))]
        for name in ex:
            try:
                body = textify(fetch(f"{base}/{name}"))
            except Exception:
                continue
            if is_q4_letter(body):
                return body, f"{base}/{name}"
    return "", ""


def last_close(cfg: dict) -> tuple[float | None, str]:
    try:
        res = json.loads(fetch(cfg["sources"]["quote"]))["chart"]["result"][0]
        ts, cl = res.get("timestamp") or [], res["indicators"]["quote"][0].get("close") or []
        pts = [(t, c) for t, c in zip(ts, cl) if c is not None]
        meta = res.get("meta") or {}
        if meta.get("regularMarketPrice") and meta.get("regularMarketTime") and (not pts or meta["regularMarketTime"] > pts[-1][0]):
            pts.append((meta["regularMarketTime"], meta["regularMarketPrice"]))  # Yahoo leaves the last close null for hours
        if not pts:
            return None, ""
        t, c = pts[-1]
        return float(c), datetime.fromtimestamp(t, timezone(timedelta(hours=-5))).strftime("%m/%d")
    except Exception:
        return None, ""


# ---------- parse ----------

def sentences(text: str) -> list[str]:
    return re.split(r"(?<=[A-Za-z0-9%)\]])\.\s+(?=[A-Z(\"])", text)


BOILER_CUT = r"(Forward-Looking Statements? This|Cautionary (Note|Statements?)|forward-looking statements? (are|include|involve|within the meaning))"
BOILER_SENT = r"(forward.looking|risks? and uncertaint|could cause|may cause|cautionary|risk factors|no assurance|actual results)"


def drop_boilerplate(text: str) -> str:
    m = re.search(BOILER_CUT, text, re.I)
    if m and m.start() > len(text) * 0.3:
        text = text[:m.start()]
    return " ".join(s + "." for s in sentences(text) if not re.search(BOILER_SENT, s, re.I))


def chunks_2027(clean: str, n: int = 450) -> list[str]:
    """Text windows that start at a 2027 full-year guidance cue. Q1'27 quarter forecasts are not cues."""
    cues = r"(For (full year |the full year of )?2027|for 2027|2027 (revenue|operating margin|free cash flow)|full year 2027)"
    return [clean[m.start():m.start() + n] for m in re.finditer(cues, clean)]


def pct_range(s: str) -> tuple[float | None, float | None]:
    m = re.search(r"\+?" + NUM + r"%(?:\s*(?:-|to)\s*\+?" + NUM + r"%)?\s*(?:year over year|y/y|YoY)", s, re.I)
    if not m:
        return None, None
    lo = float(m.group(1))
    hi = float(m.group(2)) if m.group(2) else lo
    return lo, hi


def money_range_b(s: str) -> tuple[float | None, float | None]:
    m = re.search(r"\$" + NUM + r"\s*(B|billion)(?:\s*(?:-|to)\s*\$?" + NUM + r"\s*(?:B|billion))?", s, re.I)
    if not m:
        return None, None
    lo = float(m.group(1))
    hi = float(m.group(3)) if m.group(3) else lo
    return lo, hi


def parse(raw: str, cfg: dict) -> dict:
    clean = drop_boilerplate(raw)
    p: dict = {"published": is_q4_letter(raw)}
    # 2027 revenue guidance
    p["g_lo"] = p["g_hi"] = p["rev27_lo"] = p["rev27_hi"] = None
    p["g_calc"] = False
    for ch in chunks_2027(clean):
        rm = re.search(r"revenue (?:of|to be|guidance of)\s*(?:approximately |roughly |about |~)?(\$.{0,200})", ch, re.I)
        if not rm:
            continue
        lo, hi = money_range_b(rm.group(1))
        glo, ghi = pct_range(rm.group(1))
        if lo is not None and p["rev27_lo"] is None:
            p["rev27_lo"], p["rev27_hi"] = lo, hi
        if glo is not None:
            p["g_lo"], p["g_hi"] = glo, ghi
            break
    p["rev26"] = None
    m = re.search(r"For the year, we delivered \$" + NUM + r"\s*B of revenue", clean, re.I) or re.search(r"2026 revenue (?:of|was|totaled) \$" + NUM + r"\s*B", clean, re.I)
    if m:
        p["rev26"] = float(m.group(1))
    if p["g_lo"] is None and p["rev27_lo"] and p["rev26"]:
        p["g_lo"] = round((p["rev27_lo"] / p["rev26"] - 1) * 100, 1)
        p["g_hi"] = round((p["rev27_hi"] / p["rev26"] - 1) * 100, 1)
        p["g_calc"] = True
    p["g_mid"] = round((p["g_lo"] + p["g_hi"]) / 2, 2) if p["g_lo"] is not None else None
    # 2027 operating margin target
    m = re.search(r"2027 operating margin (?:target )?of (?:approximately |roughly |about |~)?" + NUM + "%", clean, re.I)
    if not m:
        for ch in chunks_2027(clean):
            m = re.search(r"operating margin (?:of|to be|target of) (?:approximately |roughly |about |~)?" + NUM + "%", ch, re.I)
            if m:
                break
    p["om27"] = float(m.group(1)) if m else None
    # 2026 actual operating margin
    m = (re.search(r"For the year, we delivered \$[\d.]+\s*B of revenue[^.]{0,160}?operating margin of " + NUM + "%", clean, re.I)
         or re.search(r"(?:2026|full year) operating margin (?:was|of) " + NUM + "%", clean, re.I))
    p["om26"] = float(m.group(1)) if m else None
    # 2027 FCF outlook and 2026 actual FCF
    p["fcf27_lo"] = p["fcf27_hi"] = None
    for ch in [x for x in sentences(clean) if "2027" in x and not re.search(r"Q[1-4]'27", x)]:
        fm = re.search(r"free cash flow (?:of|to be|guidance of)\s*(?:approximately |roughly |about |~)?(\$[\d.]+\s*(?:B|billion)(?:\s*(?:-|to)\s*\$?[\d.]+\s*(?:B|billion))?)", ch, re.I)
        if fm:
            p["fcf27_lo"], p["fcf27_hi"] = money_range_b(fm.group(1))
            break
    p["fcf27"] = round((p["fcf27_lo"] + p["fcf27_hi"]) / 2, 2) if p["fcf27_lo"] is not None else None
    m = (re.search(r"For the full year 2026, we produced.{0,160}?\$" + NUM + r"\s*B of (?:FCF|free cash flow)", clean, re.I)
         or re.search(r"(?:For the (?:full )?year|In 2026|For 2026)[^.]{0,60}?(?:generated|delivered) free cash flow of (?:approximately |roughly |~)?\$" + NUM + r"\s*B", clean, re.I)
         or re.search(r"(?:2026|full year) free cash flow (?:was|of|totaled) (?:approximately |roughly |~)?\$" + NUM + r"\s*B", clean, re.I))
    p["fcf26"] = float(m.group(1)) if m else None
    # ads
    p["ad27"] = None
    m = re.search(r"2027 ad(?:vertising)? revenue (?:of |to )?(?:approximately |roughly |about |~)?\$" + NUM + r"\s*B", clean, re.I)
    if m:
        p["ad27"] = float(m.group(1))
    p["ads"], p["ads_text"] = ads_status(clean)
    return p


AD_TOPIC = r"(ad(?:vertising)?[- ]?(?:supported )?(?:tier|plan)|new (?:ad )?(?:countries|markets)|ads? (?:in|to) \d+ (?:new )?(?:countries|markets)|15 new countries|ad (?:product|tech|capabilit)\w*|market launches)"
AD_DELAY = r"(delay\w*|postpon\w*|push(?:ed|ing)? (?:out|back)|later than (?:planned|expected)|slower than (?:expected|planned)|behind schedule|pause\w*)"
AD_OK = r"(launched|went live|on track|as planned|on schedule|will (?:introduce|launch)|plan to (?:introduce|launch)|in March 2027|in March)"


def ads_status(clean: str) -> tuple[str, str]:
    """정상 / 지연 / 미확인 for the 2027 ad tier expansion. A delay sentence must also be about 2027
    (the Q3'26 ad revenue cut wording is about 2026 capabilities and is ignored unless tied to 2027)."""
    hits = [s.strip() for s in sentences(clean) if re.search(AD_TOPIC, s, re.I)]
    for s in hits:
        if re.search(AD_DELAY, s, re.I) and re.search(r"2027|March|countries|markets|expansion", s, re.I) and not re.search(r"\b2026 ad revenue\b", s, re.I):
            return "지연", s[:180]
    for s in hits:
        if re.search(AD_OK, s, re.I) and re.search(r"2027|March|countries|markets", s, re.I):
            return "정상", s[:180]
    return "미확인", ""


# ---------- decide ----------

def fmt(v, unit="%") -> str:
    return "미확인" if v is None else f"{v:g}{unit}"


def judge(p: dict, cfg: dict) -> tuple[str, list[str]]:
    r = cfg["rules"]
    glo, gmid, om27, fcf = p.get("g_lo"), p.get("g_mid"), p.get("om27"), p.get("fcf27")
    om_base = p.get("om26") if p.get("om26") is not None else r["om_base"]
    base_txt = f"2026 실제 {om_base:g}%" if p.get("om26") is not None else f"2026 가이던스 {om_base:g}%(실제 못 읽음)"
    if gmid is None:
        return "확인 필요", ["2027 매출 가이던스 성장률 못 읽음"]
    if gmid < r["grow_min"]:
        if fcf is None:
            return "확인 필요", [f"2027 매출 성장 중간값 {gmid:g}% < {r['grow_min']:g}%", "2027 FCF 전망 못 읽음(중단 조건 둘째 항목)"]
        if fcf < r["fcf_stall_below_b"]:
            return "중단", [f"2027 매출 성장 중간값 {gmid:g}% < {r['grow_min']:g}%", f"2027 FCF 전망 {fcf:g}B < {r['fcf_stall_below_b']:g}B(정체)"]
        return "중간", [f"2027 매출 성장 중간값 {gmid:g}% 한 자릿수(경고)", f"2027 FCF 전망 {fcf:g}B 로 정체는 아님"]
    if om27 is None:
        return "확인 필요", [f"2027 매출 성장 하단 {fmt(glo)}", "2027 영업이익률 목표 못 읽음"]
    grow_ok = glo is not None and glo >= r["grow_min"]
    om_ok = om27 > om_base
    if grow_ok and om_ok:
        if p.get("ads") == "정상":
            return "강화", [f"2027 매출 성장 하단 {glo:g}% >= {r['grow_min']:g}%", f"2027 영업이익률 {om27:g}% > {base_txt}", "광고 확대 정상"]
        if p.get("ads") == "미확인":
            return "확인 필요", [f"2027 매출 성장 하단 {glo:g}%, 영업이익률 {om27:g}% > {base_txt} 로 두 조건 충족", "광고 확대 진행 문구 못 찾음(셋째 조건)"]
        return "중간", [f"2027 매출 성장 하단 {glo:g}%, 영업이익률 {om27:g}% 충족", "광고 확대 지연 언급"]
    why = []
    if not grow_ok:
        why.append(f"2027 매출 성장 하단 {fmt(glo)} < {r['grow_min']:g}%(중간값 {gmid:g}%)")
    if not om_ok:
        why.append(f"2027 영업이익률 {om27:g}% 가 {base_txt} 보다 높지 않음")
    if p.get("ads") == "지연":
        why.append("광고 확대 지연 언급")
    return "중간", why


def trim_flag(p: dict, px: float | None, cfg: dict) -> str | None:
    r = cfg["rules"]
    if px is None or px < r["trim_px"]:
        return None
    fcf = p.get("fcf27")
    if fcf is None or fcf < r["fcf_stall_below_b"]:
        return f"종가 {px:.2f}달러 >= {r['trim_px']:g}달러인데 2027 FCF 전망 {fmt(fcf, 'B')}(개선 없음): 비중 축소 검토 조건"
    return None


def compose(cfg: dict, verdict: str, why: list[str], p: dict, src: str, px: float | None, pxd: str) -> tuple[str, str, int]:
    b = cfg["baseline"]
    head = {"강화": "강화 조건 충족 | 3차 40% 검토", "중단": "중단 조건 충족 | 추가 매수 중단",
            "중간": "강화 조건 미달 | 3차 보류, 보유 유지", "확인 필요": "확인 필요 | 원문 숫자 직접 확인"}[verdict]
    trim = trim_flag(p, px, cfg)
    title = f"[NFLX-Q4] {head}" + (" | 축소 검토" if trim else "")
    todo = {
        "강화": "계획한 3차 40%를 분할로 검토. 실적 직후 갭 상승 첫 30분 추격은 하지 않기.",
        "중단": "추가 매수 중단. 보유분은 다음 분기 FCF 확인 전까지 늘리지 않기.",
        "중간": "3차 40%는 보류하고 1, 2차 보유 유지. 다음 분기(4월) 실적에서 같은 조건 다시 확인.",
        "확인 필요": "위 원문 숫자를 직접 보고 강화/중단/중간 중 고르기. 자동 판정 없음.",
    }[verdict]
    if trim:
        todo += " " + trim + "."
    g = f"{fmt(p.get('g_lo'))}" + (f"~{p['g_hi']:g}%" if p.get("g_hi") is not None and p.get("g_hi") != p.get("g_lo") else "")
    if p.get("g_calc"):
        g += "(2026 실제 매출 대비 계산)"
    rev = f"{p['rev27_lo']:g}~{p['rev27_hi']:g}B" if p.get("rev27_lo") is not None and p.get("rev27_hi") != p.get("rev27_lo") else fmt(p.get("rev27_lo"), "B")
    nums = (f"2027 매출 가이던스 {rev}, 성장 {g}. 2027 영업이익률 목표 {fmt(p.get('om27'))}(2026 실제 {fmt(p.get('om26'))}, 가이던스 {b['fy26_om_guide']:g}%)."
            f" 2027 FCF 전망 {fmt(p.get('fcf27'), 'B')}, 2026 실제 FCF {fmt(p.get('fcf26'), 'B')}(위약금 포함, 반복 기준 약 {b['fy26_fcf_recurring_b']:g}B)."
            f" 광고 확대 {p.get('ads')}" + (f": {p['ads_text']}" if p.get("ads_text") else "") + "."
            + (f" 2027 광고 매출 전망 {p['ad27']:g}B." if p.get("ad27") is not None else ""))
    pxt = f"현재 종가 {px:.2f}달러({pxd}), 10/21 기준 {b['ref_close']:g}달러." if px else "종가 조회 안 됨."
    lines = [
        f"{datetime.now(KST):%Y-%m-%d %H:%M} KST | NFLX | 4분기 조건 판정",
        "이게 뭐 알림: " + cfg["explain"],
        f"지금 결과/상태: {verdict}. 근거: {'; '.join(why)}. {nums} {pxt} 원문 {src}",
        "지금 할 일: " + todo,
        "나중에: 실적 인터뷰 요약에서 시청시간, 광고 기능 출시 일정, 2027 FCF 가정을 다시 확인. 일회 알림이라 다시 오지 않음.",
        "익절/줄일 때: 반복 FCF 개선 없이 85~90달러 이상 급등하면 비중 축소 검토.",
        "하지 말 것: 실적 직후 갭 추격, 계획(전체 주식자산 4~5%) 초과 매수, 위약금 포함 FCF로 싸다고 판단, 신용/미수. 투자 권유 아님.",
    ]
    prio = {"강화": 4, "중단": 5, "중간": 3, "확인 필요": 4}[verdict]
    return title, "\n".join(lines), prio


# ---------- state / send ----------

def load_state() -> dict:
    st = {"final_notified": False, "missing_notified": False, "fail_notified": False, "verdict": None}
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

def synth(rev: str = "we forecast revenue of $56.2B-$57.2B. This represents 10%-12% year over year growth (or 10%-12% F/X neutral growth)",
          om: str = "We're targeting a 2027 operating margin of 33.0% (based on 1/1/27 F/X rates), up from 31.6% in 2026.",
          fcf: str = "For 2027, assuming no material swings in F/X, we expect to generate free cash flow of roughly $12.5B.",
          ads: str = "We will introduce our advertising tier to 15 new countries in 2027, with the first market launches on track for March 2027.",
          extra: str = "") -> str:
    return (
        "EX-99.1 Exhibit 99.1 January 19, 2027 Fellow shareholders, • In 2026, we met our financial objectives. "
        "◦ For the year, we delivered $51.1B of revenue (+13% year over year) and operating margin of 31.6% (+2 points). "
        "Our summary results, and forecast for Q1, are below. Q4'25 Q1'26 Q2'26 Q3'26 Q4'26 Q1'27 Forecast Revenue $ 12,051 $ 12,250 "
        "In Q1'27, we expect revenue of $13.9B, which represents year over year growth of 13%. "
        f"For 2027, based on F/X rates as of 1/1/2027, {rev}. {om} {ads} {extra} "
        f"For the full year 2026, we generated free cash flow of $12.1B. {fcf} "
        "Forward-Looking Statements This shareholder letter contains forward-looking statements, including the timing of our ad tier launches, "
        "which could be delayed or pushed out to later than planned in new countries; actual results may differ."
    )


def selftest() -> int:
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    fails = 0

    def case(label, text, want, px=None):
        nonlocal fails
        p = parse(text, cfg)
        v, why = judge(p, cfg)
        ok = v == want
        fails += not ok
        print(("ok  " if ok else "FAIL"), f"{label}: {v} ({'; '.join(why)})" + ("" if ok else f" want {want} | {json.dumps(p, ensure_ascii=False)[:500]}"))
        return p

    p = case("10-12% + 33% > 31.6% + ads on track -> 강화", synth(), "강화")
    want = {"g_lo": 10.0, "g_hi": 12.0, "rev27_lo": 56.2, "om27": 33.0, "om26": 31.6, "fcf27": 12.5, "fcf26": 12.1, "ads": "정상", "published": True}
    got = {k: p.get(k) for k in want}
    ok = got == want
    fails += not ok
    print("ok  " if ok else "FAIL", "numbers", got)
    case("single point (+11% y/y) -> 강화", synth(rev="we expect revenue of $56.7B (+11% year over year)"), "강화")
    case("range only, computed vs 51.1 -> 11.4% -> 강화", synth(rev="we forecast revenue of $56.9B-$57.9B"), "강화")
    case("9%-11% (low 9) -> 중간", synth(rev="we forecast revenue of $55.7B-$56.7B. This represents 9%-11% year over year growth"), "중간")
    case("om 31.5 <= 31.6 actual -> 중간", synth(om="We're targeting a 2027 operating margin of 31.5%."), "중간")
    case("ads delay -> 중간", synth(ads="Due to slower than expected ad capabilities, the March 2027 launch in new countries is delayed to later in 2027."), "중간")
    case("ads not mentioned -> 확인 필요", synth(ads=""), "확인 필요")
    case("boilerplate ad delay ignored (ads on track)", synth(), "강화")
    case("8%-9% + FCF 10.8B -> 중단", synth(rev="we forecast revenue of $55.2B-$55.7B. This represents 8%-9% year over year growth",
                                          fcf="For 2027, we expect to generate free cash flow of approximately $10.8B."), "중단")
    case("8%-9% + FCF 12.5B -> 중간 (one of two)", synth(rev="we forecast revenue of $55.2B-$55.7B. This represents 8%-9% year over year growth"), "중간")
    case("8%-9% + no FCF -> 확인 필요", synth(rev="we forecast revenue of $55.2B-$55.7B. This represents 8%-9% year over year growth", fcf=""), "확인 필요")
    case("no 2027 revenue -> 확인 필요", synth(rev="we remain focused on growth"), "확인 필요")
    case("no 2027 OM -> 확인 필요", synth(om=""), "확인 필요")
    p = case("Q1'27 quarter growth 13% is not the annual number", synth(rev="we forecast revenue of $55.2B-$55.7B. This represents 8%-9% year over year growth"), "중간")
    ok = p.get("g_lo") == 8.0
    fails += not ok
    print("ok  " if ok else "FAIL", "annual growth taken, not Q1:", p.get("g_lo"))
    # trim overlay
    p = parse(synth(fcf="For 2027, we expect to generate free cash flow of roughly $10.9B."), cfg)
    ok = trim_flag(p, 88.0, cfg) is not None and trim_flag(p, 80.0, cfg) is None and trim_flag(parse(synth(), cfg), 90.0, cfg) is None
    fails += not ok
    print("ok  " if ok else "FAIL", "trim flag: 88 + FCF 10.9 -> on; 80 -> off; 90 + FCF 12.5 -> off")
    # filing window: the Q4 8-K is dated in US/Eastern
    fl = [{"form": "8-K", "filingDate": "2027-01-19", "accessionNumber": "a-q4", "items": "2.02,9.01"},
          {"form": "4", "filingDate": "2027-01-19", "accessionNumber": "a-f4", "items": ""},
          {"form": "8-K", "filingDate": "2027-01-15", "accessionNumber": "a-other", "items": "5.02"},
          {"form": "8-K", "filingDate": "2026-10-20", "accessionNumber": "a-q3", "items": "2.02,9.01"}]
    got = [f["accessionNumber"] for f in candidate_8ks(fl, cfg["release_filing_from_et"])]
    ok = got == ["a-q4", "a-other"]
    fails += not ok
    print("ok  " if ok else "FAIL", "8-K candidates (Q3 2026 letter ignored):", got)
    ok = is_q4_letter(synth()) and not is_q4_letter("Q3'26 Q4'26 Forecast Revenue. Ad tier to 15 new countries in 2027")
    fails += not ok
    print("ok  " if ok else "FAIL", "Q4 letter check: needs Q4'26 and Q1'27 (Q3 letter rejected)")
    for v in ["강화", "중단", "중간", "확인 필요"]:
        t, body, _ = compose(cfg, v, ["x"], parse(synth(), cfg), "test", 88.0, "01/19")
        bad = any(ch in t + body for ch in "·—")
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
    print(f"{now.isoformat(timespec='seconds')} nflxq4 mode={mode} window={in_win}")
    if dry:
        print("DRY RUN (no ntfy, no state write)")
    if mode == "auto" and not in_win:
        print("outside window; silent")
        return 0
    if mode == "auto" and st.get("final_notified"):
        print("verdict already sent; silent")
        return 0
    test = os.environ.get("NFLXQ4_TEST_TEXT") if dry else None
    err: list[str] = []
    if test:
        raw, src = Path(test).read_text(encoding="utf-8"), "테스트 텍스트"
    else:
        try:
            raw, src = sec_release(cfg, recent_filings(cfg))
        except Exception as exc:
            raw, src = "", ""
            err.append(f"sec: {exc}")
    print("src:", src or "-", "| err:", err)
    if not raw and err and mode == "auto" and not st.get("fail_notified"):
        send(dry, "[NFLX-Q4] 조회 실패 | 판정 보류", f"SEC EDGAR 조회 실패. {'; '.join(err)}. 다음 09:00 실행에서 다시 봅니다. 이 알림으로 매매 금지.", 3, "ops")
        st["fail_notified"] = True
        save_state(st, now, dry)
        return 0
    if not raw:
        if mode == "auto" and now.date() >= end.date() and not st.get("missing_notified"):
            msg = f"Netflix 4분기 서한(8-K EX-99.1)을 {now:%m/%d %H:%M} KST 까지 못 찾음. {'; '.join(err)} IR 직접 확인. 이 알림으로 매매 금지."
            send(dry, "[NFLX-Q4] 원문 미게시 | 판정 보류", msg, 3)  # route_topic -> cwally-ops
            st["missing_notified"] = True
        else:
            print("Q4 letter not found yet; retry at next 09:00 run")
        save_state(st, now, dry)
        return 0
    p = parse(raw, cfg)
    print("parse:", json.dumps(p, ensure_ascii=False))
    verdict, why = judge(p, cfg)
    px, pxd = (None, "") if test else last_close(cfg)
    title, msg, prio = compose(cfg, verdict, why, p, src, px, pxd)
    send(dry, title, msg, prio)
    st.update({"final_notified": True, "verdict": verdict, "why": why, "src": src,
               "g_lo": p.get("g_lo"), "om27": p.get("om27"), "fcf27": p.get("fcf27"), "ads": p.get("ads")})
    save_state(st, now, dry)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
