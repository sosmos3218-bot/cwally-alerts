#!/usr/bin/env python3
"""ISRG watch for a held position (6 shares, overseas account): price zones, tranche 1 at $425,
weight 8% trim check, Monday status, 8-K watch and a one-time 2027 guidance verdict.

2026-10-28 (user approved): holding true; config position (shares, avg cost, total assets KRW,
target 5%, cap 6%) and tranches (t1 3 shares $420~425 before Q4, t2 4 shares after Q4, t3 note only).
New alerts with re-arm: close <= t1.high -> tranche 1 once, re-arm after close > t1.rearm_above,
done once shares >= t1.done_at_shares; weight (shares x close x USDKRW / total assets) > 8% -> trim
to 6% with the share count, re-arm below 7.5%. 2027 guidance verdict now uses three items
(guidance midpoint, US da Vinci growth, da Vinci I&A per procedure) merged across Jan/Feb 2027
Item 2.02 8-Ks (EX-99.1 + EX-99.2); stop (<10% midpoint) wins; a missing item is never a pass.

2026-10-26 (user approved): end moved to 2027-02-06, baseline refreshed to Q3 2026, and a one-time
2027 guidance verdict: a new 8-K with Item 2.02 filed in Jan or Feb 2027 (after guidance.seed_after) ->
EX-99.1 press release -> worldwide da Vinci procedure growth range for 2027 -> verdict by midpoint
(config guidance_add / guidance_cut). Dedupe by accession number in state["guidance"].
modes: auto | dry (no ntfy, no state) | force | confirm | selftest (fixtures, no network)
"""
from __future__ import annotations

import html
import json
import math
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
CFG_PATH = ROOT / "configs" / "isrg.json"
STATE_PATH = ROOT / "state" / "isrg.json"
UA = {"User-Agent": "cwally-alerts isrg research monitor research@example.com"}


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


def quote(url: str) -> float | None:
    return quote_meta(url)[0]


def quote_meta(url: str) -> tuple[float | None, str | None]:
    """(regularMarketPrice, market date YYYY-MM-DD in the exchange timezone)."""
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


def submissions(url: str) -> dict:
    return json.loads(fetch(url))


def latest_sec_date_from(data: dict) -> str | None:
    recent = ((data.get("filings") or {}).get("recent") or {})
    for form, day in zip(recent.get("form") or [], recent.get("filingDate") or []):
        if form in {"8-K", "10-Q", "10-K"}:
            return day
    return None


# ---------- 2027 가이던스 판정 ----------
RANGE_RE = re.compile(r"(\d{1,2}(?:\.\d+)?)\s*%\s*(?:to|-|\u2013)\s*(\d{1,2}(?:\.\d+)?)\s*%")
CUT_RE = re.compile(r"(forward-looking statements|safe harbor|about intuitive|non-gaap measures|risk factors|this report contains forward)", re.I)
NUM = r"(\d{1,3}(?:,\d{3})*(?:\.\d+)?)"


def earnings_8ks(data: dict, months: tuple[str, ...]) -> list[dict]:
    """8-K Item 2.02, filingDate 의 YYYY-MM 이 months 안. 오래된 것부터."""
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


def ex99_texts(cik: str, acc: str) -> tuple[str, str]:
    """All EX-99.x exhibits (99.1 press release + 99.2 metrics table in the January preliminary 8-K), joined."""
    base = filing_base(cik, acc)
    names = [x.get("name", "") for x in json.loads(fetch(base + "index.json")).get("directory", {}).get("item", [])]
    pick = sorted(n for n in names if re.search(r"ex-?99", n, re.I) and n.lower().endswith((".htm", ".html", ".txt")))
    if not pick:
        raise RuntimeError("EX-99 파일 없음")
    return " \n ".join(html_to_text(fetch(base + n)) for n in pick), base + pick[0]


def ex991_text(cik: str, acc: str) -> tuple[str, str]:  # kept for callers of the old name
    return ex99_texts(cik, acc)


def strip_boilerplate(text: str) -> str:
    """각 EX-99 조각마다 미래예측 진술, 위험요인, 회사 소개 같은 상용 문구 이후를 잘라냄."""
    parts = []
    for chunk in text.split(" \n "):
        m = CUT_RE.search(chunk)
        parts.append(chunk[: m.start()] if m and m.start() > 500 else chunk)
    return " \n ".join(parts)


def find_guidance(text: str, year: int) -> dict | None:
    """'{year}' 전 세계 da Vinci 시술 성장 범위. 없으면 None."""
    body = strip_boilerplate(text)
    sents = re.split(r"(?<=[.;])(?<!U\.S\.)\s+|\u2022", body)
    cands = []
    for s in sents:
        low = s.lower()
        if str(year) not in s or "da vinci procedure" not in low:
            continue
        m = RANGE_RE.search(s)
        if not m:
            continue
        lo, hi = float(m.group(1)), float(m.group(2))
        if not (0 <= lo <= hi <= 60):
            continue
        us_only = re.search(r"\b(u\.s\.|ous|outside of the u)", low) and "worldwide" not in low
        cands.append((0 if "worldwide" in low else (2 if us_only else 1), lo, hi, s.strip()[:300]))
    if not cands:
        return None
    rank, lo, hi, sent = sorted(cands, key=lambda c: c[0])[0]
    if rank == 2:
        return None
    return {"low": lo, "high": hi, "mid": round((lo + hi) / 2, 2), "sentence": sent}


def _f(x: str) -> float:
    return float(x.replace(",", ""))


def find_us(text: str, fy: int = 2026) -> dict | None:
    """US da Vinci procedure growth. Q4 sentence first, then the trended annual table (EX-99.2)."""
    body = strip_boilerplate(text)
    for s in re.split(r"(?<=[.;])(?<!U\.S\.)\s+|\u2022", body):
        if "fourth quarter" not in s.lower():
            continue
        m = re.search(r"(?:U\.S\.|United States)\s+(?:da Vinci\s+)?procedures?\s+(?:grew|increased|growth(?: of)?)\s+(?:by\s+)?(?:approximately\s+)?(\d{1,2}(?:\.\d+)?)\s*%", s)
        if not m:
            m = re.search(r"(?:U\.S\.|United States),?\s+da Vinci procedure growth (?:was|of)\s+(?:approximately\s+)?(\d{1,2}(?:\.\d+)?)\s*%", s)
        if m:
            return {"pct": float(m.group(1)), "basis": f"{fy}년 4분기 미국 다빈치 시술 성장(보도자료 문장)", "src": s.strip()[:200]}
    m = re.search(r"Total United States\s+" + NUM + r"\s+" + NUM + r"\s+" + NUM + r"\s+(\(?\d{1,2}(?:\.\d+)?\)?)\s*%", body)
    if m:
        cur, prev = _f(m.group(1)), _f(m.group(2))
        raw = m.group(4)
        pct = -float(raw.strip("()")) if raw.startswith("(") else float(raw)
        calc = round((cur / prev - 1) * 100, 1) if prev else None
        return {"pct": pct, "basis": f"{fy}년 연간 미국 다빈치 시술 성장(EX-99.2 표, {cur:,.0f}천 건 vs {prev:,.0f}천 건, 계산 {calc}%)", "src": m.group(0)[:160]}
    return None


def find_ia_per_proc(text: str) -> dict | None:
    """da Vinci I&A revenue per procedure in USD. Stated table/sentence first, else computed."""
    body = strip_boilerplate(text)
    m = re.search(r"I&A revenue per procedure \(thousands\)\s*\$?\s*(\d+(?:\.\d+)?)", body)
    if m:
        return {"usd": round(float(m.group(1)) * 1000), "basis": "4분기 다빈치 시술당 I&A 매출(회사 표 값, 천 달러 단위 반올림)", "src": m.group(0)[:160]}
    m = re.search(r"I&A revenue per procedure (?:was|of) approximately \$" + NUM, body)
    if m:
        return {"usd": round(_f(m.group(1))), "basis": "다빈치 시술당 I&A 매출(회사 문장)", "src": m.group(0)[:160]}
    ia = re.search(r"Instruments and accessories \(\W?I&A\W?\)\s*\$?\s*" + NUM, body)
    pr = re.search(r"Procedures \(thousands\)\s+" + NUM, body)
    if ia and pr and _f(pr.group(1)) > 0:
        usd = round(_f(ia.group(1)) * 1e6 / (_f(pr.group(1)) * 1e3))
        return {"usd": usd, "basis": f"계산: 4분기 I&A 매출 {_f(ia.group(1)):,.1f}백만 달러 / 다빈치 시술 {_f(pr.group(1)):,.0f}천 건 (Ion I&A 포함이라 회사 지표보다 높게 나옴)", "src": ia.group(0)[:80]}
    ia = re.search(r"(?:fourth quarter|full year) 20\d\d instruments and accessories revenue increased by \d+% to (?:approximately )?\$(\d+(?:\.\d+)?) billion", body, re.I)
    pr = re.search(r"approximately " + NUM + r" surgical procedures were performed with da Vinci", body)
    if ia and pr and "full year" in ia.group(0).lower():
        usd = round(float(ia.group(1)) * 1e9 / _f(pr.group(1)))
        return {"usd": usd, "basis": f"계산: 연간 I&A 매출 {ia.group(1)}십억 달러 / 연간 다빈치 시술 {pr.group(1)}건 (Ion I&A 포함이라 회사 지표보다 높게 나옴)", "src": ia.group(0)[:120]}
    return None


def find_us_growth(text: str) -> str | None:  # old helper, kept
    u = find_us(text)
    return f"{u['pct']:g}%" if u else None


def evaluate_guidance(gc: dict, items: dict) -> tuple[str, list[str], list[str]]:
    """-> (verdict, failed, missing). verdict in 중단 | 매수 | 보류 | 확인."""
    g, us, ia = items.get("guidance"), items.get("us"), items.get("ia")
    if g and g["mid"] < gc["mid_stop"]:
        return "중단", [f"가이던스 중간값 {g['mid']:g}% < {gc['mid_stop']:g}%"], []
    missing = [name for name, v in (("2027 가이던스 중간값", g), ("미국 다빈치 시술 성장", us), ("시술당 I&A 매출", ia)) if v is None]
    if missing:
        return "확인", [], missing
    failed = []
    if g["mid"] < gc["mid_min"]:
        failed.append(f"가이던스 중간값 {g['mid']:g}% < {gc['mid_min']:g}%")
    if us["pct"] < gc["us_min"]:
        failed.append(f"미국 다빈치 시술 {us['pct']:g}% < {gc['us_min']:g}%")
    if ia["usd"] < gc["ia_min_usd"]:
        failed.append(f"시술당 I&A {ia['usd']:,}달러 < {gc['ia_min_usd']:,}달러")
    return ("보류" if failed else "매수"), failed, []


def items_text(gc: dict, items: dict) -> str:
    g, us, ia = items.get("guidance"), items.get("us"), items.get("ia")
    a = (f"(a) 2027 가이던스 {g['low']:g}~{g['high']:g}%, 중간값 {g['mid']:g}% (기준 {gc['mid_min']:g}% 이상, {gc['mid_stop']:g}% 미만이면 중단)"
         if g else "(a) 2027 가이던스 못 읽음")
    b = (f"(b) 미국 다빈치 시술 {us['pct']:g}% (기준 {gc['us_min']:g}% 이상, 근거: {us['basis']})" if us else "(b) 미국 다빈치 시술 성장 못 읽음")
    c = (f"(c) 시술당 I&A {ia['usd']:,}달러 (기준 {gc['ia_min_usd']:,}달러 이상, 근거: {ia['basis']})" if ia else "(c) 시술당 I&A 매출 못 읽음")
    return f"{a}. {b}. {c}."


def guidance_alert(cfg: dict, stamp: str, f: dict, items: dict, link: str, pos: str) -> tuple[str, str, int, str]:
    gc = cfg["guidance_2027"]
    t2 = cfg["tranches"]["t2"]
    verdict, failed, missing = evaluate_guidance(gc, items)
    head = f"{f['date']} 8-K(Item 2.02, {f['acc']}) 기준. {items_text(gc, items)} {pos} 원문 {link}"
    if verdict == "중단":
        title, pri = f"[ISRG] 2027 가이던스 중간값 {gc['mid_stop']:g}% 미만 | 추가 매수 중단", 5
        action = f"2회차, 3회차 추가 매수를 멈추세요. {failed[0]}. 다른 항목과 상관없이 이 판정이 우선입니다. 보유 {cfg['position']['shares']}주는 투자 논리부터 다시 보세요."
    elif verdict == "확인":
        title, pri = "[ISRG] 2027 가이던스 3조건 | 확인 필요", 3
        action = f"못 읽은 항목: {', '.join(missing)}. 못 읽은 항목은 통과로 세지 않습니다. 2회차는 아직 사지 말고 원문이나 IR 보충 자료에서 직접 확인하세요."
    elif verdict == "매수":
        title, pri = "[ISRG] 2027 가이던스 3조건 통과 | 2회차 매수", 4
        action = (f"2회차 {t2['shares']}주 분할 매수 조건 충족. 지정가 기본 {t2['limit_base']}달러 이하, 세 조건 모두 통과라 {t2['limit_pass']}달러까지 허용. "
                  f"{t2['limit_pass']}달러 위면 기다리세요. 한 번에 다 사지 말고 지정가로.")
    else:
        title, pri = "[ISRG] 2027 가이던스 조건 미달 | 2회차 보류", 4
        action = f"2회차 보류. 미달: {', '.join(failed)}. {t2['limit_base']}달러 아래여도 이번에는 사지 마세요."
    later = ("다음 1, 2월 실적 공시에서 빠진 항목이 채워지면 최종 판정이 한 번 더 옵니다." if verdict == "확인"
             else "이 판정은 한 번만 옵니다. 3회차(2027년 2분기 실적 뒤, 380~420달러)는 아직 알림이 없습니다. 가격 구간과 월요일 상태는 계속 옵니다.")
    return title, body(stamp, head, action, later,
                       f"비중이 {cfg['position']['trim_alert_pct']:g}%를 넘으면 {cfg['position']['cap_weight_pct']:g}%로 줄이는 알림이 따로 옵니다.",
                       "보도자료 숫자를 콜 발언으로 바꿔 읽기. 못 읽은 항목을 통과로 읽기. 참고용이며 투자 권유가 아닙니다."), pri, verdict


def guidance_check(cfg: dict, state: dict, data: dict, stamp: str, pos: str = "", getter=None) -> list[tuple[str, str, int]]:
    gc = cfg["guidance_2027"]
    getter = getter or ex99_texts
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
                out.append(("[ISRG] 2027 가이던스 원문 조회 실패 | 다음 실행 재시도",
                            body(stamp, f"{f['date']} 8-K {f['acc']} EX-99 조회 실패: {str(exc)[:150]}.", "할 일 없음. 다음 평일 09:00에 다시 읽습니다.",
                                 "계속 실패하면 원문을 직접 보세요.", "이 안내로 비중을 바꾸지 마세요.", "조회 실패를 가이던스 판정으로 읽기."), 3))
            continue
        found = {"guidance": find_guidance(text, 2027), "us": find_us(text), "ia": find_ia_per_proc(text)}
        for k, v in found.items():
            if v is not None:
                gs["items"][k] = dict(v, acc=f["acc"])  # a later filing overrides an earlier one
        gs["seen"].append(f["acc"])
        title, msg, pri, verdict = guidance_alert(cfg, stamp, f, gs["items"], link or filing_base(cfg["cik"], f["acc"]), pos)
        if verdict == "확인":
            if gs.get("check_sent"):
                print(f"guidance {f['acc']}: still missing items; 확인 필요 already sent")
                continue
            gs["check_sent"] = {"acc": f["acc"], "at": stamp}
            out.append((title, msg, pri))
            continue
        out.append((title, msg, pri))
        gs["verdict"] = {"acc": f["acc"], "date": f["date"], "verdict": verdict, "title": title, "at": stamp,
                         "mid": (gs["items"].get("guidance") or {}).get("mid")}
        break
    return out


# ---------- 보유, 비중, 회차 ----------
def weight_pct(cfg: dict, price: float, fx: float | None) -> float | None:
    if fx is None or price is None:
        return None
    pos = cfg["position"]
    return pos["shares"] * price * fx / pos["total_assets_krw"] * 100


def shares_to_cap(cfg: dict, price: float, fx: float) -> tuple[int, float]:
    """Shares to sell so the weight is at or below cap_weight_pct, and the weight after."""
    pos = cfg["position"]
    keep = math.floor(pos["cap_weight_pct"] / 100 * pos["total_assets_krw"] / (price * fx))
    sell = max(0, pos["shares"] - keep)
    return sell, (pos["shares"] - sell) * price * fx / pos["total_assets_krw"] * 100


def pos_line(cfg: dict, price: float | None, fx: float | None) -> str:
    pos = cfg["position"]
    w = weight_pct(cfg, price, fx) if price else None
    pl = f", 평가손익 {(price / pos['avg_cost_usd'] - 1) * 100:+.1f}%" if price else ""
    wt = (f"비중 {w:.1f}% (원/달러 {fx:,.2f}, 총자산 {pos['total_assets_krw'] / 1e8:g}억원 기준, 목표 {pos['target_weight_pct']:g}%, 상한 {pos['cap_weight_pct']:g}%)"
          if w is not None else "비중 계산 못 함(환율 조회 실패)")
    return f"보유 {pos['shares']}주, 평단 {pos['avg_cost_usd']}달러{pl}, {wt}."


def tranche_alerts(cfg: dict, state: dict, price: float, fx: float | None, stamp: str, mode: str) -> list[tuple[str, str, int]]:
    """1회차 $425 (re-arm > $435, done at >= 9 shares) and weight > 8% (re-arm < 7.5%)."""
    out = []
    t1c = cfg["tranches"]["t1"]
    pos = cfg["position"]
    t1 = state.setdefault("t1", {"armed": True, "fired": None, "done": False})
    if pos["shares"] >= t1c["done_at_shares"]:
        if not t1.get("done"):
            print(f"t1 done: shares {pos['shares']} >= {t1c['done_at_shares']}")
        t1.update(done=True, armed=False)
    else:
        t1["done"] = False
        if t1.get("armed") and price <= t1c["high"]:
            out.append((
                f"[ISRG] 1회차 매수 구간 (${t1c['low']}~{t1c['high']}, {t1c['shares']}주)",
                body(stamp,
                     f"ISRG 종가 {price:.2f}달러, 1회차 상단 {t1c['high']}달러 이하. {pos_line(cfg, price, fx)}",
                     f"1회차 {t1c['shares']}주를 {t1c['low']}~{t1c['high']}달러 지정가로 나눠 거세요. {t1c['when']}이 기한입니다. 체결 안 되면 2회차로 넘깁니다. 4분기 전 가이던스 하향 같은 악재가 나오면 취소.",
                     f"체결 뒤 configs/isrg.json position.shares 를 {pos['shares'] + t1c['shares']}로 고치면 이 알림은 완료로 꺼집니다. 종가가 {t1c['rearm_above']}달러를 넘었다가 다시 내려오면 한 번 더 옵니다.",
                     f"비중 {pos['cap_weight_pct']:g}%를 넘는 추가 매수는 하지 마세요.",
                     "가격만 보고 한 번에 다 사기, 신용이나 미수. 참고용이며 투자 권유가 아닙니다."),
                4))
            t1.update(armed=False, fired=stamp)
        elif not t1.get("armed") and price > t1c["rearm_above"]:
            t1["armed"] = True
            print(f"t1 re-armed at {price}")
    w8 = state.setdefault("w8", {"armed": True, "fired": None})
    w = weight_pct(cfg, price, fx)
    if w is None:
        print("weight: no fx; skip")
    elif w8.get("armed") and w > pos["trim_alert_pct"]:
        sell, after = shares_to_cap(cfg, price, fx)
        out.append((
            f"[ISRG] 비중 {pos['trim_alert_pct']:g}% 초과, {pos['cap_weight_pct']:g}%로 축소 검토",
            body(stamp,
                 f"ISRG 종가 {price:.2f}달러. {pos_line(cfg, price, fx)} 계산: {pos['shares']}주 x {price:.2f}달러 x {fx:,.2f}원 / {pos['total_assets_krw']:,}원 = {w:.2f}%.",
                 f"{sell}주를 팔면 {pos['shares'] - sell}주, 비중 {after:.2f}%로 {pos['cap_weight_pct']:g}% 이하가 됩니다. 한 번에 다 말고 나눠서 검토하세요.",
                 f"비중이 {pos['trim_rearm_pct']:g}% 아래로 내려왔다가 다시 {pos['trim_alert_pct']:g}%를 넘으면 한 번 더 옵니다. 매도 뒤 position.shares 를 고치세요.",
                 f"{pos['cap_weight_pct']:g}%까지만 줄이고 그 아래로 서둘러 팔 필요는 없습니다.",
                 f"총자산 숫자({pos['total_assets_krw']:,}원)가 낡았는데 그대로 믿기. 참고용이며 투자 권유가 아닙니다."),
            4))
        w8.update(armed=False, fired=stamp)
    elif not w8.get("armed") and w < pos["trim_rearm_pct"]:
        w8["armed"] = True
        print(f"w8 re-armed at {w:.2f}%")
    return out


def zone_of(price: float, split: float, chase: float) -> str:
    if price <= split:
        return "split"
    if price >= chase:
        return "chase"
    return "fair"


def body(stamp: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join(
        [
            f"{stamp} | ISRG | 변경조건 점검",
            "이게 뭐 알림: 인튜이티브 서지컬 보유분(해외 계좌)의 가격 구간, 분할 매수 회차, 비중, 2027 가이던스 판정을 봅니다. 참고용이며 매수 지시가 아닙니다.",
            f"지금 결과/상태: {status}",
            f"지금 할 일: {action}",
            f"나중에: {later}",
            f"익절/줄일 때: {trim}",
            f"하지 말 것: {avoid}",
        ]
    )


def zone_alert(cfg: dict, state: dict, price: float, fx: float | None, today, stamp: str, mode: str, price_day: str | None = None) -> list[tuple[str, str, int]]:
    base, levels = cfg["baseline"], cfg["levels"]
    zone = zone_of(price, levels["split"], levels["chase"])
    prev = state.get("zone") or "fair"
    changed = zone != prev
    weekly = today.weekday() == 0 and state.get("last_weekly") != today.isoformat()
    state["zone"] = zone
    state["last_price"] = price
    if not (changed or weekly or mode == "force"):
        print(f"price zone unchanged ({zone}); silent")
        return []
    if zone == "split":
        title = f"[ISRG] {levels['split']}달러 이하 | 분할만, 일괄 금지"
        action = (f"급락 예외: 투자 조건이 깨지지 않았다면 다음 회차를 1회 앞당깁니다(한 번만). 새 돈은 나누고 한 번에 넣지 마세요."
                  f"\n경영진 3분기 자사주 평균 매입가 약 {base['buyback_avg_price_q3']:.0f}달러 근처")
    elif zone == "chase":
        title = f"[ISRG] {levels['chase']}달러 이상 | 추격 금지"
        action = "연초 프리미엄 구간에 가깝습니다. 추가 매수 추격 금지. 비중이 8%를 넘으면 축소 알림이 따로 옵니다."
    else:
        title = f"[ISRG] {levels['split']}~{levels['chase']}달러 | 프리미엄 유지, 일괄 금지"
        action = "저평가가 아닙니다. 계획한 회차 조건이 올 때만 나눠서. 일괄 매수 금지."
    why = "구간 변경" if changed else "월요일 상태"
    if changed and weekly:
        why = "구간 변경, 월요일 상태"
    t1c, t1 = cfg["tranches"]["t1"], state.get("t1") or {}
    t1s = ("1회차 완료" if t1.get("done") else
           (f"1회차 대기({t1c['high']}달러 이하 종가 시 알림)" if t1.get("armed", True) else f"1회차 알림 보냄, {t1c['rearm_above']}달러 초과 시 재무장"))
    day = f"({price_day} 종가) " if price_day else ""
    state["last_weekly"] = today.isoformat() if (weekly or mode == "force") else state.get("last_weekly")
    return [(
        title,
        body(
            stamp,
            f"ISRG {day}{price:.2f}달러. {pos_line(cfg, price, fx)} 52주 {base['low_52w']}~{base['high_52w']}. 구간 {prev} -> {zone}. {why}. {t1s}. 2회차는 4분기 실적 판정 뒤.",
            action,
            "2회차는 1, 2월 실적 공시의 세 조건(가이던스 중간값 12%, 미국 시술 10%, 시술당 I&A 1,800달러)으로 한 번 판정합니다. 3회차는 아직 알림 없음.",
            f"비중 {cfg['position']['trim_alert_pct']:g}% 초과면 {cfg['position']['cap_weight_pct']:g}%로 축소 검토. {levels['split']}달러 복귀는 손절 신호가 아닙니다.",
            f"목표가 {base['analyst_target']:.0f}달러를 안전마진으로 쓰기. 신용, 미수, 실적 전 일괄 매수. 참고용이며 투자 권유가 아닙니다.",
        ),
        4 if zone != "fair" else 3,
    )]


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    dry = mode == "dry" or os.environ.get("ISRG_DRY") == "1"
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    if mode == "selftest":
        return selftest(cfg)
    now = datetime.now(KST)
    today = now.date()
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    base = cfg["baseline"]
    levels = cfg["levels"]

    if mode == "confirm":
        msg = body(
            stamp,
            f"설치 확인. {pos_line(cfg, None, None)} 분할 관심 {levels['split']}달러 이하, 추격 금지 {levels['chase']}달러 이상, 1회차 {cfg['tranches']['t1']['high']}달러 이하.",
            "평일 09:00 KST morning에서만 돕니다. 구간 변경, 1회차, 비중 8%, 월요일, 실적 공시 때만 보냅니다.",
            "2027 가이던스 3조건은 1, 2월 실적 공시로 한 번 판정합니다.",
            "비중 8% 초과면 6%로 축소 검토.",
            "이 설치 확인을 매수나 매도 신호로 읽기. 조회 실패를 조건 충족으로 세기.",
        )
        title = "[ISRG] 설치 확인 | 매매 신호 아님"
        print(title)
        print(msg)
        if not dry:
            publish(topic, title, msg, 3, ["white_check_mark"], server)
        return 0

    state = load_json(
        STATE_PATH,
        {
            "updated_at": None,
            "last_price": None,
            "zone": "fair",
            "last_weekly": None,
            "last_filing": base.get("last_known_filing"),
            "filing_alerted": None,
            "fetch_fail_date": None,
        },
    )
    if today.isoformat() > cfg["valid_until"] and mode not in {"force", "dry"}:
        print(f"after {cfg['valid_until']}; silent")
        return 0

    price = price_day = None
    qerr = None
    try:
        price, price_day = quote_meta(cfg["sources"]["quote"])
    except Exception as exc:
        qerr = str(exc)
    fx = None
    try:
        fx = quote(cfg["sources"]["fx"])
    except Exception as exc:
        print(f"fx fail: {exc}")
    w = weight_pct(cfg, price, fx) if price else None
    print(f"price={price} ({price_day}) fx={fx} weight={'%.2f%%' % w if w is not None else None} qerr={qerr}")

    alerts: list[tuple[str, str, int]] = []
    if price is None:
        if state.get("fetch_fail_date") != today.isoformat() or mode == "force":
            alerts.append(
                (
                    "[ISRG] 주가 조회 실패 | 구간 미확인",
                    body(
                        stamp,
                        f"주가 조회 실패. {qerr or '빈 응답'}. 380달러 이탈이 아닙니다. 보유 {cfg['position']['shares']}주.",
                        "증권 앱에서 ISRG 종가를 직접 보세요. 이 알림으로 구간을 판정하지 마세요.",
                        "다음 평일 09:00 KST에 재시도합니다.",
                        "이번 알림으로 사지 말고 줄이지 마세요.",
                        "조회 실패를 380달러 이탈이나 정상으로 취급.",
                    ),
                    4,
                )
            )
            state["fetch_fail_date"] = today.isoformat()
    else:
        alerts.extend(tranche_alerts(cfg, state, price, fx, stamp, mode))
        alerts.extend(zone_alert(cfg, state, price, fx, today, stamp, mode, price_day))
        print(f"t1={state.get('t1')} w8={state.get('w8')} zone={state.get('zone')}")

    filing = None
    ferr = None
    sub = None
    try:
        sub = submissions(cfg["sources"]["sec"])
        filing = latest_sec_date_from(sub)
    except Exception as exc:
        ferr = str(exc)
        print(f"sec fail: {exc}")
    last_filing = state.get("last_filing") or base.get("last_known_filing")
    if filing and last_filing and filing > last_filing and state.get("filing_alerted") != filing:
        alerts.append(
            (
                "[ISRG] 새 공시 | 절차 점수는 원문",
                body(
                    stamp,
                    f"SEC 8-K/10-Q/10-K {filing}. 이전 {last_filing}. 1, 2월 실적 공시면 2027 가이던스 3조건 판정이 따로 옵니다. {pos_line(cfg, price, fx)}",
                    "IR 원문에서 미국 다빈치 시술 성장, 시술당 I&A 매출, 2027 가이던스를 보세요.",
                    "2회차는 세 조건 판정 알림을 기다리세요.",
                    "공시 제목만 보고 비중을 바꾸지 마세요.",
                    "이 알림의 공시 감지를 절차 성장 확정으로 읽기.",
                ),
                4,
            )
        )
        state["last_filing"] = filing
        state["filing_alerted"] = filing
    elif ferr and state.get("fetch_fail_date") != today.isoformat() and today.weekday() == 0:
        alerts.append(
            (
                "[ISRG] 공시 조회 실패 | 변경조건 미확인",
                body(
                    stamp,
                    f"SEC 조회 실패. {ferr}. 변경 조건은 미확인입니다.",
                    "investor.intuitivesurgical.com 최신 실적을 직접 보세요.",
                    "다음 월요일에 재시도합니다.",
                    "이번 알림으로 비중을 바꾸지 마세요.",
                    "조회 실패를 가이던스 유지로 읽기.",
                ),
                4,
            )
        )
        state["fetch_fail_date"] = today.isoformat()

    if sub is not None:
        alerts.extend(guidance_check(cfg, state, sub, stamp, pos_line(cfg, price, fx)))

    for title, msg, pri in alerts:
        print(title)
        print(msg)
        print(f"topic={topic} send={not dry}")
        if not dry:
            publish(topic, title, msg, pri, ["chart_with_upwards_trend"], server)
    if not alerts:
        print("no alert")

    state["updated_at"] = now.isoformat()
    if mode != "dry" and not dry:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        print("DRY RUN: no ntfy, no state write")
    return 0

# Fixtures copy the real formats: 2026-01-14 preliminary 8-K (EX-99.1 + EX-99.2 metrics table) and
# the 2026-01-22 full release (outlook bullets), with 2027 numbers.
FIX_Q4_2025 = ("Fourth quarter 2025 revenue was $2.87 billion. 2026 Financial Outlook The Company expects the following results "
               "for the full year of 2026: \u2022 Worldwide da Vinci procedure growth of approximately 13% to 15% in 2026, compared to 18% in 2025. "
               "\u2022 Non-GAAP* gross profit margin to be within a range of 67% and 68%. " + "x " * 300 +
               "Forward-Looking Statements This press release contains forward-looking statements, including expected procedure growth "
               "in 2027 of 9% to 10% and risk factors.")
PRE_991 = ("EX-99.1 INTUITIVE ANNOUNCES PRELIMINARY FOURTH QUARTER AND FULL YEAR 2026 RESULTS. Fourth quarter 2026 worldwide procedures grew approximately 15%. "
           "Da Vinci procedures grew approximately 14%. Full year 2026 worldwide procedures grew approximately 15%. The Company expects worldwide da Vinci "
           "procedures to increase approximately {lo}% to {hi}% in 2027 as compared with 2026. Preliminary fourth quarter 2026 instruments and accessories "
           "revenue increased by 15% to approximately $1.91 billion. " + "x " * 300 + "This press release contains forward-looking statements. 2027 of 30% to 40%.")
PRE_992 = ("EX-99.2 Revenue ($millions) Instruments and accessories (\u201cI&A\u201d) $ 1,910.0 $ 1,658.3 15 % % $ 7,128.7 $ 6,018.9 18 % % "
           "Other da Vinci Surgical System Metrics Procedures (thousands) 975 849 15 % % 3,620 3,153 15 % % Average Selling Price (millions) $ 1.62 $ 1.68 "
           "I&A revenue per procedure (thousands) $ {ia} $ 1.85 (1) % % $ 1.84 $ 1.81 2 % % Installed base 12,600 11,106 13 % % "
           "United States General Surgery 1,420 1,250 1,063 14 % % 18 % % Total United States {us_cur} 2,012 1,757 {us} % % 15 % % "
           "Total Worldwide 3,620 3,153 2,683 15 % % 18 % % This report contains forward-looking statements. Total United States 9 9 9 99 % %")


def pre(lo="11.5", hi="13.5", ia="1.83", us="11", us_cur="2,233", drop=()):
    a = PRE_991.format(lo=lo, hi=hi)
    b = PRE_992.format(ia=ia, us=us, us_cur=us_cur)
    if "guidance" in drop:
        a = a.replace("The Company expects worldwide da Vinci procedures to increase approximately", "The Company will discuss its outlook")
    if "ia" in drop:
        b = re.sub(r"I&A revenue per procedure \(thousands\) \$ \S+", "", b)
        b = b.replace("Instruments and accessories", "Stuff").replace("Procedures (thousands)", "Cases")
        a = a.replace("instruments and accessories", "stuff")
    if "us" in drop:
        b = b.replace("Total United States", "Total US region")
    return a + " \n " + b


def selftest(cfg: dict) -> int:
    import copy
    n = {"ok": 0, "fail": 0}

    def check(label, got, want):
        ok = got == want
        n["ok" if ok else "fail"] += 1
        print(("ok  " if ok else "FAIL"), label, got, "" if ok else f"want {want}")

    stamp = "2027-01-27 09:00 KST"
    f = {"acc": "0001035267-27-000009", "date": "2027-01-26", "doc": "x.htm"}
    link = "https://www.sec.gov/Archives/edgar/data/1035267/000103526727000009/"
    gc = cfg["guidance_2027"]
    # parsers on the real formats
    g = find_guidance(FIX_Q4_2025, 2026)
    check("guidance 2026 bullet 13~15 mid 14", (g["low"], g["high"], g["mid"]) if g else None, (13.0, 15.0, 14.0))
    check("2027 numbers inside boilerplate ignored", find_guidance(FIX_Q4_2025, 2027), None)
    t = pre()
    check("prelim guidance mid", (find_guidance(t, 2027) or {}).get("mid"), 12.5)
    check("I&A per procedure from table (Q4 value)", (find_ia_per_proc(t) or {}).get("usd"), 1830)
    check("US growth from EX-99.2 annual table", (find_us(t) or {}).get("pct"), 11.0)
    check("US Q4 sentence wins over table", (find_us("In the fourth quarter of 2026, U.S. da Vinci procedures grew approximately 9%. \n " + t) or {}).get("pct"), 9.0)
    check("US 'In the U.S., da Vinci procedure growth was' form", (find_us("In the fourth quarter, in the U.S., da Vinci procedure growth was 10%.") or {}).get("pct"), 10.0)
    check("US negative table pct", (find_us(pre(us="(3)")) or {}).get("pct"), -3.0)
    ia_calc = find_ia_per_proc(t.replace("I&A revenue per procedure (thousands) $ 1.83", ""))
    check("I&A computed from I&A revenue / procedures", (ia_calc or {}).get("usd"), round(1910.0e6 / 975e3))
    check("computed I&A basis says 계산", "계산" in (ia_calc or {}).get("basis", ""), True)
    check("missing I&A -> None", find_ia_per_proc(pre(drop=("ia",))), None)
    check("missing US -> None", find_us(pre(drop=("us",))), None)
    check("missing guidance -> None", find_guidance(pre(drop=("guidance",)), 2027), None)

    # verdict branches
    def items(mid=12.5, us=11.0, ia=1830):
        out = {}
        if mid is not None:
            out["guidance"] = {"low": mid - 1, "high": mid + 1, "mid": mid, "sentence": "s"}
        if us is not None:
            out["us"] = {"pct": us, "basis": "b"}
        if ia is not None:
            out["ia"] = {"usd": ia, "basis": "b"}
        return out
    pos = pos_line(cfg, 445.88, 1337.98)
    cases = [
        ("all pass -> 2회차 매수 p4", items(), ("매수", 4, "2회차 매수")),
        ("edge 12 / 10 / 1800 pass", items(12.0, 10.0, 1800), ("매수", 4, "2회차 매수")),
        ("mid 11.9 fail -> 보류", items(11.9), ("보류", 4, "2회차 보류")),
        ("US 9.9 fail -> 보류", items(us=9.9), ("보류", 4, "2회차 보류")),
        ("I&A 1799 fail -> 보류", items(ia=1799), ("보류", 4, "2회차 보류")),
        ("mid 9.5 -> 추가 매수 중단 p5", items(9.5), ("중단", 5, "추가 매수 중단")),
        ("mid 9.5 wins over missing items", items(9.5, None, None), ("중단", 5, "추가 매수 중단")),
        ("mid 9.5 wins over other fails", items(9.5, 5.0, 1500), ("중단", 5, "추가 매수 중단")),
        ("missing US -> 확인 필요 p3", items(us=None), ("확인", 3, "확인 필요")),
        ("missing I&A never a pass", items(ia=None), ("확인", 3, "확인 필요")),
        ("missing guidance -> 확인 필요", items(mid=None), ("확인", 3, "확인 필요")),
    ]
    samples = []
    for label, it, (v, pri, word) in cases:
        title, msg, p_, verdict = guidance_alert(cfg, stamp, f, it, link, pos)
        check(label, (verdict, p_, word in title), (v, pri, True))
        samples.append(f"TITLE: {title} (p{p_})\n{msg}")
    _, msg, _, _ = guidance_alert(cfg, stamp, f, items(us=9.9, ia=1700), link, pos)
    check("보류 lists both failed items", ("미국 다빈치 시술 9.9%" in msg, "시술당 I&A 1,700달러" in msg), (True, True))
    _, msg, _, _ = guidance_alert(cfg, stamp, f, items(), link, pos)
    check("2회차 매수 says 460 limit and 430 base", ("460달러까지" in msg, "430달러 이하" in msg), (True, True))
    _, msg, _, _ = guidance_alert(cfg, stamp, f, items(ia=None), link, pos)
    check("확인 필요 names the missing item", "못 읽은 항목: 시술당 I&A 매출" in msg, True)

    # filings: seed filter, merge across 8-Ks, 확인 필요 once, final verdict once (idempotent)
    sub = {"filings": {"recent": {"form": ["8-K", "8-K", "8-K", "8-K"], "items": ["2.02,9.01", "2.02,9.01", "2.02", "5.02"],
                                  "filingDate": ["2027-01-26", "2027-01-13", "2026-10-20", "2027-01-20"],
                                  "accessionNumber": ["A-27-2", "A-27-1", "0001035267-26-000067", "X"], "primaryDocument": ["a", "b", "c", "d"]}}}
    check("earnings_8ks order and filters", [x["acc"] for x in earnings_8ks(sub, tuple(gc["months"]))], ["A-27-1", "A-27-2"])
    texts = {"A-27-1": pre(drop=("guidance",)), "A-27-2": FIX_Q4_2025.replace("2026", "2027").replace("13% to 15%", "12% to 14%")}
    getter = lambda cik, acc: (texts[acc], link)  # noqa: E731
    st = {"guidance": {"seed_after": "2026-10-26", "seen": [], "verdict": None, "fail": {}}}
    sub1 = copy.deepcopy(sub)
    for k in ("form", "items", "filingDate", "accessionNumber", "primaryDocument"):
        sub1["filings"]["recent"][k] = sub["filings"]["recent"][k][1:]
    out1 = guidance_check(cfg, st, sub1, stamp, pos, getter)
    check("prelim without guidance -> one 확인 필요", [o[0] for o in out1], ["[ISRG] 2027 가이던스 3조건 | 확인 필요"])
    out1b = guidance_check(cfg, st, sub1, stamp, pos, getter)
    check("same filing again -> silent", out1b, [])
    out2 = guidance_check(cfg, st, sub, stamp, pos, getter)
    check("full release adds guidance -> final 2회차 매수 once", [o[0] for o in out2], ["[ISRG] 2027 가이던스 3조건 통과 | 2회차 매수"])
    check("merged items keep prelim US and I&A", (st["guidance"]["items"]["us"]["acc"], st["guidance"]["items"]["guidance"]["acc"]), ("A-27-1", "A-27-2"))
    sub3 = copy.deepcopy(sub)
    sub3["filings"]["recent"]["form"].insert(0, "8-K"); sub3["filings"]["recent"]["items"].insert(0, "2.02")
    sub3["filings"]["recent"]["filingDate"].insert(0, "2027-02-02"); sub3["filings"]["recent"]["accessionNumber"].insert(0, "A-27-3")
    sub3["filings"]["recent"]["primaryDocument"].insert(0, "e")
    check("after verdict, a new 8-K is silent", guidance_check(cfg, st, sub3, stamp, pos, getter), [])
    st2 = {"guidance": {"seed_after": "2026-10-26", "seen": [], "verdict": None, "fail": {}}}
    texts2 = {"A-27-1": pre(drop=("guidance",)), "A-27-2": pre(drop=("ia",)).replace("drop", "")}
    out = guidance_check(cfg, st2, sub, stamp, pos, lambda c, a: (texts2[a], link))
    check("prelim missing guidance, full missing I&A -> 확인 필요 then merged final", [o[0] for o in out], ["[ISRG] 2027 가이던스 3조건 | 확인 필요", "[ISRG] 2027 가이던스 3조건 통과 | 2회차 매수"])
    st3 = {"guidance": {"seed_after": "2026-10-26", "seen": [], "verdict": None, "fail": {}}}
    texts3 = {"A-27-1": pre(drop=("guidance", "ia")), "A-27-2": pre(drop=("guidance", "ia"))}
    out = guidance_check(cfg, st3, sub, stamp, pos, lambda c, a: (texts3[a], link))
    check("still missing after 2 filings -> 확인 필요 only once, no verdict", ([o[0] for o in out], st3["guidance"]["verdict"]), (["[ISRG] 2027 가이던스 3조건 | 확인 필요"], None))

    # tranche 1 at $425 with re-arm above $435
    c6 = copy.deepcopy(cfg); c6["position"]["shares"] = 6
    st = {}
    seq = [(445.88, 0), (424.0, 1), (423.0, 0), (430.0, 0), (436.0, 0), (425.0, 1), (420.0, 0)]
    got = [len([a for a in tranche_alerts(c6, st, px, 1337.98, stamp, "auto") if "1회차" in a[0]]) for px, _ in seq]
    check("t1 hysteresis 445,424*,423,430,436(rearm),425*,420", got, [x for _, x in seq])
    st = {}
    check("t1 seeded armed at 445.88, not fired", (tranche_alerts(c6, st, 445.88, 1337.98, stamp, "auto"), st["t1"]["armed"]), ([], True))
    c9 = copy.deepcopy(cfg); c9["position"]["shares"] = 9
    st = {}
    check("t1 done at 9 shares, no alert at 400", ([a for a in tranche_alerts(c9, st, 400.0, 1337.98, stamp, "auto") if "1회차" in a[0]], st["t1"]["done"]), ([], True))
    t1a = tranche_alerts(c6, {}, 424.0, 1337.98, stamp, "auto")[0]
    check("t1 title", t1a[0], "[ISRG] 1회차 매수 구간 ($420~425, 3주)")
    samples.append(f"TITLE: {t1a[0]} (p{t1a[2]})\n{t1a[1]}")

    # weight math and re-arm
    check("weight 6 sh x 445.88 x 1337.98 / 2억", round(weight_pct(cfg, 445.88, 1337.98), 2), 1.79)
    c40 = copy.deepcopy(cfg); c40["position"]["shares"] = 40
    w = weight_pct(c40, 450.0, 1400.0)
    sell, after = shares_to_cap(c40, 450.0, 1400.0)
    check("weight 40 sh = 12.6%", round(w, 2), 12.6)
    check("sell 21 -> 19 sh 5.99%", (sell, round(after, 2)), (21, 5.99))
    st = {}
    wa = [a for a in tranche_alerts(c40, st, 450.0, 1400.0, stamp, "auto") if "비중" in a[0]]
    check("w8 fires once", (len(wa), "21주를 팔면" in wa[0][1] if wa else None), (1, True))
    samples.append(f"TITLE: {wa[0][0]} (p{wa[0][2]})\n{wa[0][1]}")
    check("w8 silent next day", [a for a in tranche_alerts(c40, st, 455.0, 1400.0, stamp, "auto") if "비중" in a[0]], [])
    c12 = copy.deepcopy(cfg); c12["position"]["shares"] = 12
    tranche_alerts(c12, st, 450.0, 1300.0, stamp, "auto")  # 12 x 450 x 1300 / 2e8 = 3.51% -> re-arm
    check("w8 re-armed below 7.5%", st["w8"]["armed"], True)
    c25 = copy.deepcopy(cfg); c25["position"]["shares"] = 25
    check("w8 between 7.5 and 8 stays silent", ([a for a in tranche_alerts(c25, {"w8": {"armed": False}}, 450.0, 1380.0, stamp, "auto") if "비중" in a[0]]), [])
    check("no fx -> no weight alert", [a for a in tranche_alerts(c40, {}, 450.0, None, stamp, "auto") if "비중" in a[0]], [])

    # zones keep working; split adds 급락 예외
    from datetime import date
    zs = {"zone": "fair"}
    za = zone_alert(cfg, zs, 379.0, 1337.98, date(2027, 1, 5), stamp, "auto")
    check("split zone alert with 급락 예외", (len(za), "급락 예외: 투자 조건이 깨지지 않았다면 다음 회차를 1회 앞당깁니다" in za[0][1], za[0][2]), (1, True, 4))
    samples.append(f"TITLE: {za[0][0]} (p{za[0][2]})\n{za[0][1]}")
    check("same zone next day silent", zone_alert(cfg, zs, 378.0, 1337.98, date(2027, 1, 6), stamp, "auto"), [])
    zm = zone_alert(cfg, {"zone": "fair"}, 445.88, 1337.98, date(2026, 11, 2), stamp, "auto", "2026-10-30")
    check("Monday status states shares and weight", ("보유 6주" in zm[0][1], "비중 1.8%" in zm[0][1]), (True, True))
    samples.append(f"TITLE: {zm[0][0]} (p{zm[0][2]})\n{zm[0][1]}")
    check("chase zone", zone_alert(cfg, {"zone": "fair"}, 501.0, 1337.98, date(2027, 1, 5), stamp, "auto")[0][0], "[ISRG] 500달러 이상 | 추격 금지")

    allt = "\n".join(samples)
    check("no middle dot / em-dash in samples", any(ch in allt for ch in "\u00b7\u2014"), False)
    src = Path(__file__).read_text(encoding="utf-8")
    check("no middle dot / em-dash in script", any(ch in src for ch in "\u00b7\u2014"), False)
    print("\n--- samples ---\n" + "\n\n".join(samples))
    print(f"SELFTEST {'OK' if not n['fail'] else 'FAIL'}: {n['ok']} passed, {n['fail']} failed (fixtures only, no network, no publish, no state)")
    return 1 if n["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
