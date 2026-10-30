#!/usr/bin/env python3
"""[LLY] Eli Lilly watch on the weekday 09:00 KST morning bundle (approved in chat 2026-10-30).

Modeled on scripts/isrg_monitor.py (code copied, ISRG itself untouched):
1. Price line: US regular-session close (Yahoo regularMarketPrice of the last session) <= $1,200 -> one
   alert (p4, 강세 전환 조건(조정) 도달, 신규 진입 재검토); re-arm after a close > $1,212 (+1%). Seeded with
   the 2026-10-29 close $1,373.70, so nothing fires at install. Close lookup failure = 판단 보류 and one
   cwally-ops notice per KST day (title has 조회 실패, routed by ntfy_util.route_topic).
2. One-time Q4 2026 / 2027 guidance verdict from SEC EDGAR only (data.sec.gov submissions + Archives,
   never investor.lilly.com): 8-K with Item 2.02 filed in Jan or Feb 2027 after guidance.seed_after ->
   EX-99.1 found by exhibit TYPE in the filing index (Lilly names it q4XXlillysalesandearningsp.htm, not
   ex99) -> (a) Q4 2026 revenue, (b) 2027 revenue guidance midpoint growth vs FY2026 actual (and vs the
   base minus ~$2.0B 2026 one-time items), (c) Foundayo Q4 revenue. 약세 전환 if (a) or (b) bear (wins),
   강세 전환 if (b) and (c) bull candidates, else 중립 유지. An unreadable item is 확인 필요 and never a pass.
   Items merge across Jan/Feb filings oldest first (a later filing overrides); 확인 필요 is sent once,
   the final verdict once; dedupe by accession. Nothing by 2027-03-06 -> one expiry notice.
3. One reminder in the first run after 2027-03-31: 레타트루타이드 FDA 신청 여부 확인.
holding is null in the config (position unknown): no holding info in any message.
modes: auto | dry (no ntfy, no state) | force | confirm (install confirmation) | selftest (fixtures, no
network) | replay <accession> [fy] (live SEC parse of one past filing, print only, no ntfy, no state)
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "lly.json"
STATE_PATH = ROOT / "state" / "lly.json"
UA = {"User-Agent": "cwally-alerts lly monitor sosmos3218@gmail.com", "Accept-Encoding": "identity"}
NUM = r"(\d{1,3}(?:,\d{3})*(?:\.\d+)?)"


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return dict(fallback)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return dict(fallback)
    out = dict(fallback)
    out.update(data)
    return out


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def quote_meta(url: str) -> tuple[float | None, str | None]:
    """(regularMarketPrice, market date YYYY-MM-DD in the exchange timezone). Same as isrg_monitor."""
    data = json.loads(fetch(url))
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        return None, None
    meta = result[0].get("meta") or {}
    price = meta.get("regularMarketPrice")
    day = None
    try:
        ts = int(meta.get("regularMarketTime")) + int(meta.get("gmtoffset") or 0)
        day = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        pass
    try:
        return (float(price) if price is not None else None), day
    except (TypeError, ValueError):
        return None, day


def eok(b: float) -> str:
    """billions of USD -> '243.3억 달러' (1 billion = 10억)."""
    v = round(b * 10, 1)
    return f"{v:,.1f}억 달러" if v % 1 else f"{int(v):,}억 달러"


def _f(x: str) -> float:
    return float(x.replace(",", ""))


# ---------- SEC EDGAR ----------
def earnings_8ks(data: dict, months: tuple[str, ...]) -> list[dict]:
    """8-K Item 2.02 whose filingDate YYYY-MM is in months, oldest first (copied from isrg_monitor)."""
    recent = ((data.get("filings") or {}).get("recent") or {})
    out = []
    for i, form in enumerate(recent.get("form") or []):
        items = (recent.get("items") or [""] * (i + 1))[i] or ""
        day = (recent.get("filingDate") or [""] * (i + 1))[i] or ""
        if form == "8-K" and "2.02" in items and day[:7] in months:
            out.append({"acc": recent["accessionNumber"][i], "date": day, "doc": (recent.get("primaryDocument") or [""] * (i + 1))[i]})
    return sorted(out, key=lambda x: x["date"])


def filing_base(cik: str, acc: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/"


def html_to_text(raw: str) -> str:
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", html.unescape(t).replace("\xa0", " ")).strip()


EX991_ROW_RE = re.compile(r'<a href="([^"]+)">[^<]*</a></td>\s*<td[^>]*>\s*EX-99\.1\s*</td>', re.I)


def pick_ex991(index_html: str, names: list[str]) -> str | None:
    """EX-99.1 file name: exhibit TYPE column of <acc>-index.html first, then an ex99.1-looking name."""
    m = EX991_ROW_RE.search(index_html or "")
    if m:
        return m.group(1).rsplit("/", 1)[-1]
    for n in sorted(names):
        if re.search(r"ex-?99(?:[._-]?0?1)?\b", n, re.I) and n.lower().endswith((".htm", ".html", ".txt")):
            return n
    return None


def ex991_text(cik: str, acc: str) -> tuple[str, str]:
    base = filing_base(cik, acc)
    idx = ""
    try:
        idx = fetch(base + f"{acc}-index.html")
    except Exception as exc:  # noqa: BLE001
        print(f"index.html fail {acc}: {exc}")
    names: list[str] = []
    if not EX991_ROW_RE.search(idx):
        names = [x.get("name", "") for x in json.loads(fetch(base + "index.json")).get("directory", {}).get("item", [])]
    name = pick_ex991(idx, names)
    if not name:
        raise RuntimeError("EX-99.1 파일 없음")
    return html_to_text(fetch(base + name)), base + name


# ---------- 원문 파싱 (fy = 실적 연도, fy+1 = 가이던스 연도) ----------
def _is_q4_release(text: str, fy: int) -> bool:
    return bool(re.search(rf"(fourth[- ]quarter(?: of)? {fy}|Q4 {fy}|full[- ]year {fy})", text, re.I))


def find_q4_revenue(text: str, fy: int = 2026) -> dict | None:
    if not _is_q4_release(text, fy):
        return None
    m = re.search(rf"Fourth[- ]Quarter {fy} {fy - 1} % Change Revenue \$ ?{NUM} \$ ?{NUM}", text, re.I)
    if m:
        return {"b": round(_f(m.group(1)) / 1000, 3), "basis": f"보도자료 실적표 {_f(m.group(1)):,.0f}백만 달러", "src": m.group(0)[:160]}
    m = re.search(rf"Three Months Ended Twelve Months Ended December 31, December 31, {fy} {fy - 1} % Chg\. {fy} {fy - 1} % Chg\. Revenue \$ ?{NUM}", text, re.I)
    if m:
        return {"b": round(_f(m.group(1)) / 1000, 3), "basis": f"손익계산서 3개월 {_f(m.group(1)):,.0f}백만 달러", "src": m.group(0)[-120:]}
    m = re.search(rf"Revenue in Q4 {fy} (?:increased|decreased|grew|rose|fell)(?: by)? \d+(?:\.\d+)?% to \$(\d+(?:\.\d+)?) billion", text, re.I)
    if m:
        return {"b": float(m.group(1)), "basis": "보도자료 문장(십억 달러 단위 반올림)", "src": m.group(0)[:160]}
    return None


def find_fy_revenue(text: str, fy: int = 2026) -> dict | None:
    if not _is_q4_release(text, fy):
        return None
    m = re.search(rf"Three Months Ended Twelve Months Ended December 31, December 31, {fy} {fy - 1} % Chg\. {fy} {fy - 1} % Chg\. Revenue \$ ?{NUM} \$ ?{NUM} \(?-?\d+\)? ?% \$ ?{NUM}", text, re.I)
    if m:
        return {"b": round(_f(m.group(3)) / 1000, 3), "basis": f"손익계산서 12개월 {_f(m.group(3)):,.0f}백만 달러", "src": m.group(0)[-120:]}
    m = re.search(rf"Fourth[- ]Quarter Full[- ]Year .{{0,80}}?{fy} {fy - 1} % Change {fy} {fy - 1} % Change .{{0,4000}}?Total Revenue {NUM} {NUM} \(?-?\d+\)? ?% {NUM}", text, re.I)
    if m:
        return {"b": round(_f(m.group(3)) / 1000, 3), "basis": f"제품별 매출표 연간 {_f(m.group(3)):,.0f}백만 달러", "src": m.group(0)[-120:]}
    m = re.search(rf"(?:full[- ]year {fy} revenue|revenue (?:for|in) (?:full[- ]year )?{fy}) (?:was|of|increased[^$]{{0,30}}to|grew[^$]{{0,30}}to) \$(\d+(?:\.\d+)?) billion", text, re.I)
    if m:
        return {"b": float(m.group(1)), "basis": "보도자료 문장(십억 달러 단위 반올림)", "src": m.group(0)[:160]}
    return None


def find_guidance(text: str, year: int = 2027) -> dict | None:
    pats = [
        rf"{year} Guidance Revenue \$(\d+(?:\.\d+)?)(?: billion)? to \$(\d+(?:\.\d+)?) billion",
        rf"{year} guidance (?:issued|provided)? ?with revenue in the range of \$(\d+(?:\.\d+)?)(?: billion)? to \$(\d+(?:\.\d+)?) billion",
    ]
    for p in pats:
        m = re.search(p, text, re.I)
        if m:
            lo, hi = float(m.group(1)), float(m.group(2))
            if 30 <= lo <= hi <= 400:
                return {"low": lo, "high": hi, "mid": round((lo + hi) / 2, 3), "src": m.group(0)[:200]}
    for s in re.split(r"(?<=[.;])\s+|\u2022", text):
        low = s.lower()
        if str(year) not in s or "revenue" not in low or "earnings per share" in low or "eps" in low:
            continue
        m = re.search(r"revenue[^$]{0,80}\$(\d+(?:\.\d+)?)(?: billion)? to \$(\d+(?:\.\d+)?) billion", s, re.I)
        if m:
            lo, hi = float(m.group(1)), float(m.group(2))
            if 30 <= lo <= hi <= 400:
                return {"low": lo, "high": hi, "mid": round((lo + hi) / 2, 3), "src": s.strip()[:200]}
    return None


def find_foundayo_q4(text: str, fy: int = 2026) -> dict | None:
    if not _is_q4_release(text, fy):
        return None
    m = re.search(rf"Fourth[- ]Quarter Full[- ]Year .{{0,80}}?{fy} {fy - 1} % Change .{{0,4000}}?Foundayo(?: \(\d\))? {NUM} (?:\u2014|\u2013|-|{NUM})", text, re.I)
    if m:
        v = _f(m.group(1))
        if 0 <= v < 20000:
            return {"m": v, "basis": "제품별 매출표 4분기(백만 달러)", "src": m.group(0)[-80:]}
    m = re.search(rf"For Q4 {fy}, (?:worldwide |U\.S\. )?Foundayo revenue (?:was|of|increased[^$]{{0,40}}to|grew[^$]{{0,40}}to) \$(\d+(?:\.\d+)?) (million|billion)", text, re.I)
    if m:
        v = float(m.group(1)) * (1000 if m.group(2).lower() == "billion" else 1)
        return {"m": v, "basis": "보도자료 문장", "src": m.group(0)[:160]}
    return None


def parse_release(text: str, fy: int = 2026) -> dict:
    return {"q4": find_q4_revenue(text, fy), "fy": find_fy_revenue(text, fy), "guide": find_guidance(text, fy + 1), "foundayo": find_foundayo_q4(text, fy)}


# ---------- 판정 ----------
def evaluate(gc: dict, items: dict) -> dict:
    """-> {verdict: 약세|강세|중립|확인, a, b, c: status or None, growth..., base..., missing[], lines[]}"""
    q4, fyr, g, fo = items.get("q4"), items.get("fy"), items.get("guide"), items.get("foundayo")
    base = base_note = None
    if fyr:
        base, base_note = fyr["b"], f"2026 실제 매출 {eok(fyr['b'])}({fyr['basis']})"
    elif q4:
        base = round(gc["nine_month_rev_b"] + q4["b"], 3)
        base_note = f"2026 실제 매출 {eok(base)}(계산: 9개월 {eok(gc['nine_month_rev_b'])} + 4분기 {eok(q4['b'])})"
    a = None if q4 is None else ("bear" if q4["b"] < gc["q4_bear_below_b"] else "ok")
    b = g_rep = g_adj = None
    if g and base:
        adj_base = base - gc["one_time_2026_b"]
        g_rep = round((g["mid"] / base - 1) * 100, 1)
        g_adj = round((g["mid"] / adj_base - 1) * 100, 1)
        b = "bear" if g_rep < gc["growth_bear_below_pct"] else ("bull" if g_adj >= gc["growth_bull_min_pct"] else "mid")
    c = None if fo is None else ("bull" if fo["m"] >= gc["foundayo_bull_min_m"] else "no")
    missing = [n for n, v in (("(a) 4분기 매출", a), ("(b) 2027 가이던스 성장률", b), ("(c) Foundayo 4분기 매출", c)) if v is None]
    if a == "bear" or b == "bear":
        verdict = "약세"
    elif a is None or b is None:
        verdict = "확인"
    elif b == "bull" and c == "bull":
        verdict = "강세"
    elif b == "bull" and c is None:
        verdict = "확인"
    else:
        verdict = "중립"
    lines = []
    if q4:
        lines.append(f"(a) 4분기 2026 매출 {eok(q4['b'])}({q4['basis']}). 기준 {eok(gc['q4_bear_below_b'])} 미만이면 약세 -> {'약세' if a == 'bear' else '약세 아님'}")
    else:
        lines.append("(a) 4분기 2026 매출 확인 필요(못 읽음, 통과로 세지 않음)")
    if g and base:
        lines.append(
            f"(b) 2027 매출 가이던스 {eok(g['low'])}~{eok(g['high'])}, 중간값 {eok(g['mid'])}. 기준 {base_note}: 성장 {g_rep:g}% (10% 미만이면 약세). "
            f"2026 일회성 약 {eok(gc['one_time_2026_b'])} 뺀 기준 {eok(base - gc['one_time_2026_b'])}: 성장 {g_adj:g}% (15% 이상이면 강세 후보) -> "
            + {"bear": "약세", "bull": "강세 후보", "mid": "중간"}[b])
    elif g:
        lines.append(f"(b) 2027 매출 가이던스 {eok(g['low'])}~{eok(g['high'])}, 2026 실제 매출 확인 필요(못 읽음), 성장률 판정 못 함")
    else:
        lines.append("(b) 2027 매출 가이던스 확인 필요(못 읽음, 통과로 세지 않음)")
    if fo:
        lines.append(f"(c) Foundayo 4분기 매출 {fo['m']:,.0f}백만 달러({fo['basis']}). 300백만 달러 이상이면 강세 후보 -> {'강세 후보' if c == 'bull' else '미달'}")
    else:
        lines.append("(c) Foundayo 4분기 매출 확인 필요(못 읽음, 통과로 세지 않음)")
    return {"verdict": verdict, "a": a, "b": b, "c": c, "growth_rep": g_rep, "growth_adj": g_adj, "base": base, "missing": missing, "lines": lines}


def context_line(cfg: dict) -> str:
    c = cfg["context"]
    return (f"{c['judgment_date'][5:].replace('-', '/')} 판단: {c['judgment']}. 밸류: 2026 EPS 가이던스 중간값 {c['eps_guide_mid_2026']:.2f}달러 기준 약 {c['pe_ref']:g}배"
            f"(10/29 종가 {c['close_ref']:,.2f}달러). 2026 일회성 약 {eok(c['one_time_2026_b'])}(마일스톤, 리베이트 추정 변경)는 반복되지 않음. "
            f"3분기 미국 가격 {c['us_price_q3_reported_pct']}%(보고), 조정 제외 약 {c['us_price_q3_ex_adj_pct']}%(실적콜 발언).")


def body(cfg: dict, stamp: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join(
        [
            f"{stamp} | LLY | 변경조건 점검",
            "이게 뭐 알림: 일라이 릴리(LLY)의 1,200달러 종가 선과 4분기 실적, 2027 가이던스 판정을 봅니다. 보유 정보는 넣지 않습니다(보유 여부 미입력). "
            + context_line(cfg),
            f"지금 결과/상태: {status}",
            f"지금 할 일: {action}",
            f"나중에: {later}",
            f"익절/줄일 때: {trim}",
            f"하지 말 것: {avoid}",
        ]
    )


AVOID = "한 번에 다 사기, 신용이나 미수, 못 읽은 항목을 통과로 읽기. 참고용이며 투자 권유가 아닙니다."


def guidance_alert(cfg: dict, stamp: str, f: dict, items: dict, link: str) -> tuple[str, str, int, str]:
    gc = cfg["guidance_2027"]
    ev = evaluate(gc, items)
    v = ev["verdict"]
    head = f"{f['date']} 8-K(Item 2.02, {f['acc']}) EX-99.1 기준. " + ". ".join(ev["lines"]) + f". 원문 {link}"
    pre = "[LLY] 4분기, 2027 가이던스 판정 | "
    if v == "약세":
        why = [x for x, s in (("(a) 4분기 매출 기준 미달", ev["a"]), ("(b) 2027 가이던스 성장 10% 미만", ev["b"])) if s == "bear"]
        title, pri = pre + "약세 전환", 4
        action = f"약세 전환: {', '.join(why)}. 이 판정이 다른 항목보다 우선입니다. 신규 진입은 보류하고, 보유 중이면 투자 논리를 다시 보고 비중 축소를 검토하세요."
        trim = "보유 중이면 한 번에 다 팔지 말고 나눠서 줄이는 것을 검토하세요."
    elif v == "강세":
        title, pri = pre + "강세 전환", 4
        action = "강세 전환: (b) 일회성 뺀 2027 성장 15% 이상과 (c) Foundayo 4분기 300백만 달러 이상이 함께 나왔습니다. 신규 진입 검토 가능. 한 번에 다 사지 말고 나눠서."
        trim = "강세 판정이어도 비중 상한을 넘겨 사지 마세요."
    elif v == "확인":
        title, pri = pre + "확인 필요", 3
        action = f"못 읽은 항목: {', '.join(ev['missing'])}. 못 읽은 항목은 통과로 세지 않습니다. 판단은 보유 유지, 신규 진입 중립 그대로 두고 원문 EX-99.1에서 직접 확인하세요."
        trim = "확인 필요 상태로 비중을 바꾸지 마세요."
    else:
        title, pri = pre + "중립 유지", 3
        action = "중립 유지: 약세 조건도, 강세 조건(두 항목 모두)도 아닙니다. 보유 유지, 신규 진입 중립 그대로."
        trim = "이 판정으로 비중을 바꿀 이유는 없습니다."
    later = ("다음 1, 2월 실적 공시에서 빠진 항목이 채워지면 최종 판정이 한 번 더 옵니다. 2027-03-06까지 없으면 만료 안내가 1번 옵니다." if v == "확인"
             else "이 판정은 한 번만 옵니다. 1,200달러 종가 선 알림은 계속 돕니다.")
    return title, body(cfg, stamp, head, action, later, trim, "보도자료 숫자를 콜 발언으로 바꿔 읽기. " + AVOID), pri, v


def guidance_check(cfg: dict, state: dict, data: dict, stamp: str, getter=None) -> list[tuple[str, str, int]]:
    gc = cfg["guidance_2027"]
    getter = getter or ex991_text
    gs = state.setdefault("guidance", {"seed_after": gc["seed_after"], "seen": [], "verdict": None, "fail": {}})
    gs.setdefault("fail", {})
    gs.setdefault("items", {})
    if gs.get("verdict"):
        return []
    out = []
    for f in earnings_8ks(data, tuple(gc["months"])):
        if f["date"] <= gs["seed_after"] or f["acc"] in gs["seen"]:
            continue
        try:
            text, link = getter(cfg["cik"], f["acc"])
        except Exception as exc:  # noqa: BLE001
            print(f"guidance {f['acc']} fetch fail: {exc}")
            if not gs["fail"].get(f["acc"]):
                gs["fail"][f["acc"]] = stamp
                out.append(("[LLY] 4분기 실적 원문 조회 실패 | 다음 실행 재시도",
                            body(cfg, stamp, f"{f['date']} 8-K {f['acc']} EX-99.1 조회 실패: {str(exc)[:150]}. 판단 보류.", "할 일 없음. 다음 평일 09:00에 다시 읽습니다.",
                                 "계속 실패하면 SEC 원문을 직접 보세요.", "이 안내로 비중을 바꾸지 마세요.", "조회 실패를 실적 판정으로 읽기. " + AVOID), 3))
            continue
        found = parse_release(text, 2026)
        print(f"guidance {f['acc']} parsed: " + json.dumps({k: (v and {kk: vv for kk, vv in v.items() if kk != 'src'}) for k, v in found.items()}, ensure_ascii=False))
        for k, v in found.items():
            if v is not None:
                gs["items"][k] = dict(v, acc=f["acc"])  # a later filing overrides an earlier one
        gs["seen"].append(f["acc"])
        title, msg, pri, verdict = guidance_alert(cfg, stamp, f, gs["items"], link or filing_base(cfg["cik"], f["acc"]))
        if verdict == "확인":
            if gs.get("check_sent"):
                print(f"guidance {f['acc']}: still missing items; 확인 필요 already sent")
                continue
            gs["check_sent"] = {"acc": f["acc"], "at": stamp}
            out.append((title, msg, pri))
            continue
        out.append((title, msg, pri))
        gs["verdict"] = {"acc": f["acc"], "date": f["date"], "verdict": verdict, "title": title, "at": stamp}
        break
    return out


def expiry_check(cfg: dict, state: dict, today: str, stamp: str) -> list[tuple[str, str, int]]:
    gc = cfg["guidance_2027"]
    gs = state.setdefault("guidance", {"seed_after": gc["seed_after"], "seen": [], "verdict": None, "fail": {}})
    if gs.get("verdict") or state.get("expiry_sent") or today <= gc["valid_until"]:
        return []
    state["expiry_sent"] = stamp
    partial = "확인 필요 안내 뒤 빠진 항목이 끝내 채워지지 않았습니다" if gs.get("check_sent") else "2027년 1, 2월 8-K Item 2.02(4분기 실적) 원문을 찾지 못했습니다"
    return [("[LLY] 4분기, 2027 가이던스 판정 | 기한 만료, 판정 없음",
             body(cfg, stamp, f"{gc['valid_until']}까지 {partial}. 판정 없이 끝냅니다. 본 accession: {', '.join(gs.get('seen') or []) or '없음'}.",
                  "판단은 보유 유지, 신규 진입 중립 그대로. 4분기 실적과 2027 가이던스는 SEC 원문에서 직접 보세요.",
                  "이 판정 알림은 더 오지 않습니다. 1,200달러 종가 선 알림은 꺼지는 때까지 돕니다.",
                  "판정 없음으로 비중을 바꾸지 마세요.", "판정 없음을 중립이나 통과로 읽기. " + AVOID), 3)]


def reminder_check(cfg: dict, state: dict, today: str, stamp: str) -> list[tuple[str, str, int]]:
    out = []
    sent = state.setdefault("reminders", {})
    for r in cfg.get("reminders") or []:
        if today > r["after"] and not sent.get(r["id"]):
            sent[r["id"]] = stamp
            out.append((f"[LLY] {r['text']} | 매매 신호 아님",
                        body(cfg, stamp, f"{r['after']} 이후 첫 실행 알림(1번만). {r['text']}.",
                             "릴리 보도자료나 SEC 공시에서 레타트루타이드 FDA 신청(제출) 여부를 확인하세요.",
                             "신청이 늦어지면 2027 성장 가정을 다시 보세요. 이 알림은 다시 오지 않습니다.",
                             "이 알림으로 비중을 바꾸지 마세요.", "신청 뉴스만 보고 추격 매수. " + AVOID), 3))
    return out


def price_check(cfg: dict, state: dict, price: float, price_day: str | None, stamp: str) -> list[tuple[str, str, int]]:
    pl = cfg["price_line"]
    ps = state.setdefault("price", {"armed": True, "fired": None, "last_close": None, "last_date": None, "fail_day": None})
    out = []
    ps["last_close"], ps["last_date"] = price, price_day
    pe = price / cfg["context"]["eps_guide_mid_2026"]
    if ps.get("armed", True) and price <= pl["line"]:
        out.append((
            f"[LLY] 종가 {pl['line']:,}달러 이하 | 강세 전환 조건(조정) 도달, 신규 진입 재검토",
            body(cfg, stamp,
                 f"LLY 미국 정규장 종가 {price:,.2f}달러({price_day or '날짜 미상'}), {pl['line']:,}달러 이하. 강세 전환 조건(조정) 도달, 신규 진입 재검토. "
                 f"이 종가면 2026 EPS 가이던스 중간값 기준 약 {pe:.1f}배.",
                 "신규 진입을 다시 검토하세요. 들어간다면 한 번에 다 말고 나눠서, 지정가로. 하락 이유(가격 인하, 가이던스, 경쟁)부터 확인하세요.",
                 f"종가가 {pl['rearm_above']:,}달러를 넘었다가 다시 {pl['line']:,}달러 이하로 오면 한 번 더 옵니다. 4분기 실적 판정(2027년 1, 2월)은 따로 옵니다.",
                 "보유 중이면 이 알림은 매도 신호가 아닙니다.",
                 "가격만 보고 추격 매수. " + AVOID),
            4))
        ps.update(armed=False, fired={"at": stamp, "close": price, "date": price_day})
    elif not ps.get("armed", True) and price > pl["rearm_above"]:
        ps["armed"] = True
        print(f"price line re-armed at {price}")
    else:
        print(f"price line: close {price} armed={ps.get('armed', True)}; silent")
    return out


def price_fail(cfg: dict, state: dict, today: str, stamp: str, err: str | None, mode: str) -> list[tuple[str, str, int]]:
    ps = state.setdefault("price", {"armed": True, "fired": None, "last_close": None, "last_date": None, "fail_day": None})
    if ps.get("fail_day") == today and mode != "force":
        print("price fetch fail; ops notice already sent today")
        return []
    ps["fail_day"] = today
    return [("[LLY] 종가 조회 실패 | 판단 보류",
             body(cfg, stamp, f"LLY 종가 조회 실패. {err or '빈 응답'}. 판단 보류: {cfg['price_line']['line']:,}달러 이탈이 아닙니다.",
                  "증권 앱에서 LLY 종가를 직접 보세요. 이 알림으로 판정하지 마세요.", "다음 평일 09:00 KST에 재시도합니다.",
                  "이번 알림으로 사지 말고 줄이지 마세요.", "조회 실패를 1,200달러 이탈이나 정상으로 취급. " + AVOID), 3)]


def install_message(cfg: dict, stamp: str) -> tuple[str, str]:
    pl, gc = cfg["price_line"], cfg["guidance_2027"]
    msg = body(
        cfg, stamp,
        f"설치 확인. 1) 종가 선: 미국 정규장 종가 {pl['line']:,}달러 이하면 1번(강세 전환 조건(조정) 도달, 신규 진입 재검토), {pl['rearm_above']:,}달러 초과 종가에서 재무장. "
        f"기준 {pl['seed_date']} 종가 {pl['seed_close']:,.2f}달러라 지금은 안 울립니다. 조회 실패면 판단 보류, 운영 채널에 하루 1번. "
        f"2) 2027년 1, 2월 8-K(Item 2.02) EX-99.1로 한 번 판정: (a) 4분기 매출 {eok(gc['q4_bear_below_b'])} 미만 약세, (b) 2027 가이던스 중간값 성장 10% 미만 약세, "
        f"일회성 {eok(gc['one_time_2026_b'])} 뺀 기준 15% 이상 강세 후보, (c) Foundayo 4분기 300백만 달러 이상 강세 후보. 약세 우선, (b)(c) 모두면 강세, 아니면 중립. "
        f"못 읽은 항목은 확인 필요. {gc['valid_until']}까지 없으면 만료 안내 1번. 3) 2027-03-31 이후 첫 실행에 레타트루타이드 FDA 신청 확인 1번.",
        "평일 09:00 KST morning 번들(isrg 뒤)에서만 돕니다. 새 cron 없음. 할 일 없음.",
        "4분기 실적은 보통 2월 초(날짜 미확정)입니다. 판정 알림을 기다리세요.",
        "보유 정보가 없어 비중 알림은 없습니다. 보유 여부를 config holding 에 채우면 됩니다.",
        "이 설치 확인을 매수나 매도 신호로 읽기. 조회 실패를 조건 충족으로 세기. " + AVOID,
    )
    return "[LLY] 설치 확인 | 매매 신호 아님", msg


DEFAULT_STATE = {
    "updated_at": None,
    "price": {"armed": True, "fired": None, "last_close": None, "last_date": None, "fail_day": None},
    "sec_fail_day": None,
    "expiry_sent": None,
    "reminders": {},
}


def emit(alerts: list[tuple[str, str, int]], topic: str, server: str, dry: bool) -> None:
    for title, msg, pri in alerts:
        print(title)
        print(msg)
        print(f"topic={topic} send={not dry}")
        if not dry:
            publish(topic, title, msg, pri, ["pill"], server)
    if not alerts:
        print("no alert")


def replay(cfg: dict, acc: str, fy: int) -> int:
    """Live SEC parse of one past filing (e.g. the Feb 2026 Q4 2025 release with fy=2025). Print only."""
    text, link = ex991_text(cfg["cik"], acc)
    found = parse_release(text, fy)
    for k, v in found.items():
        print(k, json.dumps(v, ensure_ascii=False))
    gc = dict(cfg["guidance_2027"])
    stamp = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    title, msg, pri, verdict = guidance_alert(dict(cfg, guidance_2027=gc), stamp, {"acc": acc, "date": "replay"}, found, link)
    print(f"REPLAY (fy={fy}, thresholds are the 2026 ones, print only): {title} (p{pri}) verdict={verdict}")
    print(msg)
    print("DRY RUN: no ntfy, no state write")
    return 0


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    dry = mode == "dry" or os.environ.get("LLY_DRY") == "1"
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    if mode == "selftest":
        return selftest(cfg)
    if mode == "replay":
        return replay(cfg, sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 2025)
    now = datetime.now(KST)
    today = now.date().isoformat()
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))

    if mode == "confirm":
        title, msg = install_message(cfg, stamp)
        print(title)
        print(msg)
        if not dry:
            publish(topic, title, msg, 3, ["white_check_mark"], server)
        else:
            print("DRY RUN: no ntfy, no state write")
        return 0

    state = load_json(STATE_PATH, DEFAULT_STATE)
    if today > cfg["ends_kst"][:10] and mode not in {"force", "dry"}:
        print(f"after {cfg['ends_kst']}; silent")
        return 0

    alerts: list[tuple[str, str, int]] = []
    price = price_day = qerr = None
    try:
        price, price_day = quote_meta(cfg["sources"]["quote"])
    except Exception as exc:  # noqa: BLE001
        qerr = str(exc)
    print(f"price={price} ({price_day}) qerr={qerr}")
    if price is None:
        alerts.extend(price_fail(cfg, state, today, stamp, qerr, mode))
    else:
        alerts.extend(price_check(cfg, state, price, price_day, stamp))
    print(f"price state={state.get('price')}")

    sub = None
    try:
        sub = json.loads(fetch(cfg["sources"]["sec"]))
    except Exception as exc:  # noqa: BLE001
        print(f"sec fail: {exc}")
        gc = cfg["guidance_2027"]
        in_window = "2027-01-01" <= today <= gc["valid_until"] and not (state.get("guidance") or {}).get("verdict")
        if in_window and state.get("sec_fail_day") != today:
            state["sec_fail_day"] = today
            alerts.append(("[LLY] SEC 공시 조회 실패 | 판정 보류",
                           body(cfg, stamp, f"SEC 공시 목록 조회 실패: {str(exc)[:150]}. 4분기 판정 보류.", "할 일 없음. 다음 평일 09:00에 다시 읽습니다.",
                                "계속 실패하면 SEC 원문을 직접 보세요.", "이 안내로 비중을 바꾸지 마세요.", "조회 실패를 실적 판정으로 읽기. " + AVOID), 3))
    if sub is not None:
        alerts.extend(guidance_check(cfg, state, sub, stamp))
        g = state.get("guidance") or {}
        print(f"guidance seen={g.get('seen')} verdict={g.get('verdict')} items={sorted((g.get('items') or {}).keys())}")
    alerts.extend(expiry_check(cfg, state, today, stamp))
    alerts.extend(reminder_check(cfg, state, today, stamp))

    emit(alerts, topic, server, dry)
    state["updated_at"] = now.isoformat()
    if mode != "dry" and not dry:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        print("DRY RUN: no ntfy, no state write")
    return 0


# Fixture follows the real Lilly EX-99.1 layout (Feb 2026 Q4 2025 release, 8-K 0000059478-26-000008) with fake
# Q4 2026 / 2027 numbers.
FIX_991 = ("EX-99.1 2 q426lillysalesandearningsp.htm EX-99.1 Document February 4, 2027 Lilly reports fourth-quarter 2026 financial results "
           "and provides 2027 guidance \u2022 Revenue in Q4 2026 increased {q4pct}% to ${q4b} billion driven by Mounjaro and Zepbound. "
           "\u2022 2027 guidance issued with revenue in the range of ${glo} billion to ${ghi} billion and non-GAAP EPS in the range of $44.00 to $46.00. "
           "Financial Results $ in millions, except per share data Fourth-Quarter 2026 2025 % Change Revenue $ {q4m} $ 19,292 {q4pct}% "
           "Net income \u2013 Reported 7,100 6,636 7% Fourth-Quarter Full Year Selected Products 2026 2025 % Change 2026 2025 % Change "
           "Mounjaro $ 11,000 $ 7,409 48% $ 40,000 $ 22,965 74% Zepbound (1) 6,000 4,261 41% 22,000 13,542 62% "
           "Foundayo {fo} \u2014 NM {fofy} \u2014 NM Inluriyo 90 \u2014 NM 273 \u2014 NM Total Revenue {q4m} 19,292 {q4pct}% {fym} 65,179 41% "
           "2027 Financial Guidance The following table summarizes the company's full-year 2027 financial guidance: 2027 Guidance Revenue ${glo} to ${ghi} billion "
           "Performance Margin 50.0% to 52.0% Earnings per Share $44.00 to $46.00 Cautionary Statement Regarding Forward-Looking Statements x "
           "Eli Lilly and Company Operating Results (Unaudited) \u2013 REPORTED Three Months Ended Twelve Months Ended December 31, December 31, "
           "2026 2025 % Chg. 2026 2025 % Chg. Revenue $ {q4m} $ 19,292 {q4pct}% $ {fym} $ 65,179 41% Cost of sales 4,000 3,372 19%")


def fix(q4m="24,700", fym="91,870", glo="100", ghi="104", fo="320", drop=()):
    q4 = _f(q4m)
    t = FIX_991.format(q4m=q4m, q4b=f"{q4 / 1000:.1f}", q4pct=round((q4 / 19292 - 1) * 100), fym=fym, glo=glo, ghi=ghi, fo=fo, fofy=f"{_f(fo) + 281:,.0f}")
    if "guide" in drop:
        t = re.sub(r"2027 guidance issued[^\u2022]*?\. ", "", t).replace("2027 Guidance Revenue", "2027 Outlook Items")
    if "foundayo" in drop:
        t = re.sub(r"Foundayo \S+ \u2014 NM \S+ \u2014 NM ", "", t)
    if "q4" in drop:
        t = (t.replace("Fourth-Quarter 2026 2025 % Change Revenue", "Fourth-Quarter 2026 2025 % Change Sales")
              .replace("Revenue in Q4 2026", "Sales in Q4 2026").replace("Chg. Revenue", "Chg. Sales").replace("Total Revenue", "Total Sales"))
    if "fy" in drop:
        t = t.replace("Total Revenue", "Total Sales").replace("Chg. Revenue", "Chg. Sales")
    return t


INDEX_HTML = ('<tr class="evenRow"> <td scope="row">2</td> <td scope="row">EX-99.1</td> <td scope="row">'
              '<a href="/Archives/edgar/data/59478/000005947827000009/q426lillysalesandearningsp.htm">q426lillysalesandearningsp.htm</a></td> '
              '<td scope="row">EX-99.1</td> <td scope="row">199153</td> </tr>')
Q3_SNIPPET = ("Lilly reports third-quarter 2026 financial results. Financial Results $ in millions Third-Quarter 2026 2025 % Change Revenue $ 24,397 $ 17,601 39% "
              "Foundayo 184 \u2014 NM 281 \u2014 NM Total Revenue 24,397 17,601 39% 67,170 45,887 46% Prior Updated Revenue $85 to $87 billion $91 to $92 billion")


def selftest(cfg: dict) -> int:
    import copy
    n = {"ok": 0, "fail": 0}

    def check(label, got, want):
        ok = got == want
        n["ok" if ok else "fail"] += 1
        print(("ok  " if ok else "FAIL"), label, got, "" if ok else f"want {want}")

    stamp = "2027-02-05 09:00 KST"
    gc = cfg["guidance_2027"]
    f = {"acc": "0000059478-27-000009", "date": "2027-02-04", "doc": "lly-20270204.htm"}
    link = "https://www.sec.gov/Archives/edgar/data/59478/000005947827000009/q426lillysalesandearningsp.htm"

    # EX-99.1 picking by exhibit type (Lilly file names have no 'ex99')
    check("EX-99.1 from index.html type column", pick_ex991(INDEX_HTML, []), "q426lillysalesandearningsp.htm")
    check("EX-99.1 fallback by name", pick_ex991("", ["lly-20270204.htm", "ex99-1.htm", "ex99-2.htm"]), "ex99-1.htm")
    check("no EX-99.1 -> None", pick_ex991("", ["lly-20270204.htm"]), None)

    # parsers on the real layout
    p = parse_release(fix())
    check("Q4 revenue from results table", (p["q4"] or {}).get("b"), 24.7)
    check("FY2026 revenue from 12-month table", (p["fy"] or {}).get("b"), 91.87)
    check("2027 guidance 100~104 mid 102", (p["guide"] or {}).get("low"), 100.0)
    check("2027 guidance mid", (p["guide"] or {}).get("mid"), 102.0)
    check("Foundayo Q4 from product table", (p["foundayo"] or {}).get("m"), 320.0)
    check("Q4 from sentence when tables missing", (find_q4_revenue("Revenue in Q4 2026 increased 28% to $24.7 billion driven by x.") or {}).get("b"), 24.7)
    check("Foundayo from sentence", (find_foundayo_q4("For Q4 2026, worldwide Foundayo revenue was $410 million.") or {}).get("m"), 410.0)
    check("Q3 release is not read as Q4", (find_q4_revenue(Q3_SNIPPET), find_fy_revenue(Q3_SNIPPET), find_foundayo_q4(Q3_SNIPPET), find_guidance(Q3_SNIPPET)), (None, None, None, None))
    check("missing guidance -> None", find_guidance(fix(drop=("guide",))), None)
    check("missing Foundayo -> None", find_foundayo_q4(fix(drop=("foundayo",))), None)
    check("missing Q4 -> None", find_q4_revenue(fix(drop=("q4",))), None)
    check("missing FY -> None", find_fy_revenue(fix(drop=("fy",))), None)

    # verdicts
    def items(q4=24.7, fy=91.87, lo=100.0, hi=104.0, fo=320.0):
        out = {}
        if q4 is not None:
            out["q4"] = {"b": q4, "basis": "b"}
        if fy is not None:
            out["fy"] = {"b": fy, "basis": "b"}
        if lo is not None:
            out["guide"] = {"low": lo, "high": hi, "mid": (lo + hi) / 2}
        if fo is not None:
            out["foundayo"] = {"m": fo, "basis": "b"}
        return out
    # FY 91.87: bear if mid < 101.057; bull if mid >= 1.15 x 89.87 = 103.35
    cases = [
        ("mid 102 (11.0%, adj 13.5%) -> 중립", items(), "중립", 3),
        ("mid 104 adj 15.7% + Foundayo 320 -> 강세", items(lo=102, hi=106), "강세", 4),
        ("mid 104 adj bull but Foundayo 299 -> 중립", items(lo=102, hi=106, fo=299), "중립", 3),
        ("Foundayo edge 300 counts", items(lo=102, hi=106, fo=300), "강세", 4),
        ("mid 100 (8.8%) -> 약세", items(lo=98, hi=102), "약세", 4),
        ("Q4 24.32 < 24.33 -> 약세", items(q4=24.32), "약세", 4),
        ("Q4 24.33 edge is not bear", items(q4=24.33), "중립", 3),
        ("Q4 bear wins over bull (b)+(c)", items(q4=24.0, lo=102, hi=106), "약세", 4),
        ("(b) bear wins over missing (a) and (c)", items(q4=None, lo=96, hi=100, fo=None), "약세", 4),
        ("missing guidance -> 확인", items(lo=None), "확인", 3),
        ("missing Q4 and FY -> 확인", items(q4=None, fy=None), "확인", 3),
        ("missing Foundayo with (b) bull -> 확인, never a pass", items(lo=102, hi=106, fo=None), "확인", 3),
        ("missing Foundayo with (b) not bull -> 중립", items(fo=None), "중립", 3),
    ]
    samples = []
    for label, it, want, pri in cases:
        title, msg, p_, v = guidance_alert(cfg, stamp, f, it, link)
        check(label, (v, p_, title.endswith({"강세": "강세 전환", "약세": "약세 전환", "중립": "중립 유지", "확인": "확인 필요"}[want])), (want, pri, True))
        samples.append(f"TITLE: {title} (p{p_})\n{msg}")
    ev = evaluate(gc, items(q4=24.7, fy=None))
    check("FY missing -> base computed 67.17 + Q4", ev["base"], round(67.17 + 24.7, 3))
    _, msg, _, _ = guidance_alert(cfg, stamp, f, items(q4=24.7, fy=None), link)
    check("computed base is stated as 계산", "계산: 9개월" in msg, True)
    _, msg, _, _ = guidance_alert(cfg, stamp, f, items(), link)
    check("message states both bases", ("기준 2026 실제 매출 918.7억 달러" in msg, "뺀 기준 898.7억 달러" in msg), (True, True))
    _, msg, _, _ = guidance_alert(cfg, stamp, f, items(fo=None), link)
    check("unreadable item says 확인 필요", "(c) Foundayo 4분기 매출 확인 필요" in msg, True)
    for k in ("보유 유지, 신규 진입 중립", "약 38.7배", "35.50달러", "반복되지 않음", "-7%(보고)", "약 -10%(실적콜 발언)", "투자 권유가 아닙니다"):
        check(f"context in message: {k}", k in msg, True)

    # filings: seed filter, accession dedupe, 확인 필요 once, final once
    sub = {"filings": {"recent": {"form": ["8-K", "8-K", "8-K", "10-Q", "8-K"], "items": ["2.02,9.01", "2.02", "2.02,9.01", "", "5.02"],
                                  "filingDate": ["2027-02-04", "2027-01-13", "2026-10-29", "2026-10-29", "2027-01-20"],
                                  "accessionNumber": ["A-27-2", "A-27-1", "0000059478-26-000104", "Q", "X"], "primaryDocument": ["a", "b", "c", "d", "e"]}}}
    check("earnings_8ks Jan/Feb only, oldest first", [x["acc"] for x in earnings_8ks(sub, tuple(gc["months"]))], ["A-27-1", "A-27-2"])
    prelim = "Lilly announces preliminary fourth-quarter 2026 revenue. Revenue in Q4 2026 increased 28% to $24.7 billion."
    texts = {"A-27-1": prelim, "A-27-2": fix(glo="102", ghi="106")}
    getter = lambda cik, acc: (texts[acc], link)  # noqa: E731
    sub1 = copy.deepcopy(sub)
    for k in ("form", "items", "filingDate", "accessionNumber", "primaryDocument"):
        sub1["filings"]["recent"][k] = sub["filings"]["recent"][k][1:]
    st = {"guidance": {"seed_after": "2026-10-30", "seen": [], "verdict": None, "fail": {}}}
    out1 = guidance_check(cfg, st, sub1, stamp, getter)
    check("prelim without guidance -> one 확인 필요", [o[0] for o in out1], ["[LLY] 4분기, 2027 가이던스 판정 | 확인 필요"])
    check("same filing again -> silent (accession dedupe)", guidance_check(cfg, st, sub1, stamp, getter), [])
    out2 = guidance_check(cfg, st, sub, stamp, getter)
    check("full release -> final 강세 once", [o[0] for o in out2], ["[LLY] 4분기, 2027 가이던스 판정 | 강세 전환"])
    check("after verdict, silent", guidance_check(cfg, st, sub, stamp, getter), [])
    st2 = {"guidance": {"seed_after": "2027-02-10", "seen": [], "verdict": None, "fail": {}}}
    check("seed_after skips older filings", guidance_check(cfg, st2, sub, stamp, getter), [])
    st3 = {"guidance": {"seed_after": "2026-10-30", "seen": [], "verdict": None, "fail": {}}}
    def bad(cik, acc):
        raise RuntimeError("HTTP 503")
    o3 = guidance_check(cfg, st3, sub, stamp, bad)
    check("EX-99.1 fetch fail -> one ops notice per accession, not seen", ([o[0] for o in o3], st3["guidance"]["seen"]),
          (["[LLY] 4분기 실적 원문 조회 실패 | 다음 실행 재시도"] * 2, []))
    check("fetch fail notice not repeated", guidance_check(cfg, st3, sub, stamp, bad), [])
    from ntfy_util import route_topic
    check("fetch fail routes to cwally-ops", route_topic("cwally-port", o3[0][0]), "cwally-ops")

    # expiry and reminder
    st4 = {"guidance": {"seed_after": "2026-10-30", "seen": [], "verdict": None}}
    check("no expiry on 2027-03-06", expiry_check(cfg, st4, "2027-03-06", stamp), [])
    e = expiry_check(cfg, st4, "2027-03-08", stamp)
    check("expiry once after 2027-03-06", (len(e), expiry_check(cfg, st4, "2027-03-09", stamp)), (1, []))
    samples.append(f"TITLE: {e[0][0]} (p{e[0][2]})\n{e[0][1]}")
    check("no expiry when verdict exists", expiry_check(cfg, {"guidance": {"verdict": {"x": 1}}}, "2027-03-08", stamp), [])
    st5 = {}
    check("no reminder on 2027-03-31", reminder_check(cfg, st5, "2027-03-31", stamp), [])
    r = reminder_check(cfg, st5, "2027-04-01", stamp)
    check("reminder once after 2027-03-31", (len(r), "레타트루타이드 FDA 신청 여부 확인 (2027년 1분기 예정)" in r[0][0], reminder_check(cfg, st5, "2027-04-02", stamp)), (1, True, []))
    samples.append(f"TITLE: {r[0][0]} (p{r[0][2]})\n{r[0][1]}")

    # price line: seed 1,373.70 armed, fire <= 1,200 once, re-arm > 1,212
    st = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    check("seed state armed at 1373.70 on 2026-10-29", ((st.get("price") or {}).get("armed"), (st.get("price") or {}).get("last_close"), (st.get("price") or {}).get("last_date")), (True, 1373.7, "2026-10-29"))
    st = {"price": {"armed": True, "last_close": 1373.7, "last_date": "2026-10-29"}}
    seq = [(1373.7, 0), (1250.0, 0), (1200.0, 1), (1150.0, 0), (1212.0, 0), (1190.0, 0), (1212.5, 0), (1199.99, 1), (1300.0, 0)]
    got = [len(price_check(cfg, st, px, "2026-11-0x", stamp)) for px, _ in seq]
    check("hysteresis 1373.7, 1250, 1200*, 1150, 1212(no rearm), 1190, 1212.5(rearm), 1199.99*, 1300", got, [x for _, x in seq])
    pa = price_check(cfg, {"price": {"armed": True}}, 1195.5, "2026-11-20", stamp)
    check("price title", pa[0][0], "[LLY] 종가 1,200달러 이하 | 강세 전환 조건(조정) 도달, 신규 진입 재검토")
    check("price alert p4 to cwally-port", (pa[0][2], route_topic("cwally-port", pa[0][0])), (4, "cwally-port"))
    samples.insert(0, f"TITLE: {pa[0][0]} (p{pa[0][2]})\n{pa[0][1]}")
    stf = {}
    pf = price_fail(cfg, stf, "2026-11-02", stamp, "HTTP 429", "auto")
    check("close fail -> 판단 보류 to cwally-ops once a day", (len(pf), route_topic("cwally-port", pf[0][0]), "판단 보류" in pf[0][1], price_fail(cfg, stf, "2026-11-02", stamp, "x", "auto")), (1, "cwally-ops", True, []))
    check("close fail next day sends again", len(price_fail(cfg, stf, "2026-11-03", stamp, "x", "auto")), 1)
    samples.append(f"TITLE: {pf[0][0]} (p{pf[0][2]})\n{pf[0][1]}")
    ti, mi = install_message(cfg, stamp)
    check("install title", ti, "[LLY] 설치 확인 | 매매 신호 아님")
    samples.append(f"TITLE: {ti} (p3)\n{mi}")

    allt = "\n".join(samples)
    check("no holding info in messages", any(k in allt for k in ("보유 6주", "평단", "평가손익")), False)
    check("no middle dot / em-dash in samples", any(ch in allt for ch in "\u00b7\u2014"), False)
    src = Path(__file__).read_text(encoding="utf-8")
    src_ko = re.sub(r"\\u2014", "", src)
    check("no middle dot / em-dash in script", any(ch in src_ko for ch in "\u00b7\u2014"), False)
    cfg_txt = CFG_PATH.read_text(encoding="utf-8")
    check("no middle dot / em-dash in config", any(ch in cfg_txt for ch in "\u00b7\u2014"), False)
    print("\n--- samples ---\n" + "\n\n".join(samples))
    print(f"SELFTEST {'OK' if not n['fail'] else 'FAIL'}: {n['ok']} passed, {n['fail']} failed (fixtures only, no network, no publish, no state)")
    return 1 if n["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
