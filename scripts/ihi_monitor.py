#!/usr/bin/env python3
"""[IHI] iShares U.S. Medical Devices ETF watch on the weekday 09:00 KST morning bundle (approved in chat 2026-11-02).

Same US-monitor pattern as lly_monitor.py (self-contained, no other alert files read):
1. Relative-strength turn signal from Yahoo chart v8 daily regular-session closes (close, not adjclose):
   (1) IHI 63-trading-day price return > XLV 63-trading-day price return AND (2) IHI close > IHI 200-day SMA,
   on the latest US close. Fires once (p4) when both become true; re-arms on a close where either is false.
   A bar of a session still in progress is dropped. Missing/short/mismatched data = 판단 보류: no fire, no
   re-arm, one cwally-ops notice per KST day (title has 조회 실패, routed by ntfy_util.route_topic).
2. One-time 2027 guidance verdict (Jan to Feb 2027 filings) for ABT, ISRG, MDT from SEC EDGAR 8-K Item 2.02
   EX-99.1 only. Per company: ABT 2027 organic/comparable sales growth midpoint vs 7.0% (2026 initial) and
   adjusted EPS midpoint growth vs 10%; ISRG 2027 da Vinci procedure growth midpoint vs 14.0% (2026 initial);
   MDT FY27 organic revenue growth and non-GAAP EPS midpoints vs the prior FY27 guidance ("prior guidance"
   wording in the same release first, else the last pre-window MDT earnings 8-K, else the config seed).
   Any metric down = 하향, else any up = 상향, else 유지; nothing readable = 확인 필요.
   Verdict: 2+ 하향 or manual estimate downgrade = 약세, 관망 연장; 2+ 상향 and RS signal on = 강세, 신규 분할 매수
   전환; an unreadable company that could flip the result = 확인 필요; else 중립. Sent when all three have
   reported (확인 필요 at most once, final once), or on the first run after the deadline (2027-02-28) with the
   companies that reported. A company that never reported by then is left out (MDT FQ3 can slip).
3. Ends 2027-03-31: after that day the script returns before any network call; publish() also blocks.
holding is null (보유 없음, ISRG 2026-11 정리): no price-trim or sell line.
modes: auto | dry (no ntfy, no state) | force (ignore end date guard, still state) | confirm (install
confirmation) | selftest (fixtures, no network) | replay (live SEC parse of the 2026 guidance releases with
2026 labels, print only)
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "ihi.json"
STATE_PATH = ROOT / "state" / "ihi.json"
SEC_UA = {"User-Agent": "cwally-alerts ihi monitor sosmos3218@gmail.com", "Accept-Encoding": "identity"}
WEB_UA = {"User-Agent": "Mozilla/5.0 (cwally-alerts ihi monitor)"}
PCT = r"(-?\d+(?:\.\d+)?)"
USD = r"(\d+\.\d+)"
FOOTER_LABEL = "참고:"
AVOID = "한 번에 다 사기, 신용이나 미수, 못 읽은 항목이나 판단 보류를 통과로 읽기. 참고용이며 투자 권유가 아닙니다."
NO_TRIM = "없음. 보유가 확인되지 않아 매도나 비중 축소 기준을 두지 않습니다."


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return json.loads(json.dumps(fallback))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return json.loads(json.dumps(fallback))
    out = json.loads(json.dumps(fallback))
    out.update(data)
    return out


def fetch(url: str, headers: dict, tries: int = 2) -> str:
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=25) as resp:
                return resp.read().decode("utf-8", errors="ignore")
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2 * (i + 1))
    raise last  # type: ignore[misc]


# ---------- Yahoo daily closes ----------
def daily_closes(chart: dict, now_ts: float) -> list[tuple[str, float | None]]:
    """[(YYYY-MM-DD exchange date, close or None)] oldest first; a session still in progress is dropped."""
    result = (chart.get("chart") or {}).get("result") or []
    if not result:
        return []
    r = result[0]
    meta = r.get("meta") or {}
    ts = r.get("timestamp") or []
    closes = (((r.get("indicators") or {}).get("quote") or [{}])[0] or {}).get("close") or []
    reg = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    out = []
    for i, t in enumerate(ts):
        off = int(meta.get("gmtoffset") or 0)
        # date of the bar in exchange time; bars are stamped at the session open, so any offset gives the same day
        day = datetime.fromtimestamp(int(t) + off, timezone.utc).strftime("%Y-%m-%d")
        c = closes[i] if i < len(closes) else None
        out.append((day, float(c) if c is not None else None))
    try:
        start, end = int(reg.get("start")), int(reg.get("end"))
        if ts and int(ts[-1]) >= start and now_ts < end:
            out = out[:-1]  # today's session not closed yet
    except (TypeError, ValueError):
        pass
    return out


def rs_compute(ihi: list[tuple[str, float | None]], bench: list[tuple[str, float | None]], lookback: int = 63, sma_n: int = 200) -> dict:
    """-> {ok, reason, date, close, sma, ret_ihi, ret_bench, gap_pp, cond_rs, cond_ma, on}. Returns in %."""
    if not ihi or not bench:
        return {"ok": False, "reason": "일봉 없음"}
    if ihi[-1][0] != bench[-1][0]:
        return {"ok": False, "reason": f"최신 거래일 불일치 (IHI {ihi[-1][0]}, 비교 {bench[-1][0]})"}
    need = max(lookback + 1, sma_n)
    if len(ihi) < need or len(bench) < lookback + 1:
        return {"ok": False, "reason": f"이력 부족 (IHI {len(ihi)}개, 비교 {len(bench)}개, 필요 {need}개)"}
    bmap = dict(bench)
    i_now, i_then = ihi[-1], ihi[-1 - lookback]
    b_now, b_then = bench[-1], bench[-1 - lookback]
    win = [c for _, c in ihi[-sma_n:]]
    if None in (i_now[1], i_then[1], b_now[1], b_then[1]) or None in win:
        return {"ok": False, "reason": "종가 빈 값 (판단 보류)"}
    if i_then[0] != b_then[0] or bmap.get(i_then[0]) is None:
        return {"ok": False, "reason": f"63거래일 전 날짜 불일치 (IHI {i_then[0]}, 비교 {b_then[0]})"}
    ret_i = (i_now[1] / i_then[1] - 1) * 100
    ret_b = (b_now[1] / b_then[1] - 1) * 100
    sma = sum(win) / sma_n
    cond_rs, cond_ma = ret_i > ret_b, i_now[1] > sma
    return {"ok": True, "reason": "", "date": i_now[0], "base_date": i_then[0], "close": round(i_now[1], 4), "sma": round(sma, 4),
            "ret_ihi": round(ret_i, 2), "ret_bench": round(ret_b, 2), "gap_pp": round(ret_i - ret_b, 2),
            "cond_rs": cond_rs, "cond_ma": cond_ma, "on": cond_rs and cond_ma}


def fetch_rs(cfg: dict) -> dict:
    now_ts = time.time()
    rc = cfg["rs_signal"]
    try:
        ihi = daily_closes(json.loads(fetch(cfg["sources"]["ihi"], WEB_UA)), now_ts)
        xlv = daily_closes(json.loads(fetch(cfg["sources"]["xlv"], WEB_UA)), now_ts)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"Yahoo 조회 실패: {str(exc)[:120]}"}
    return rs_compute(ihi, xlv, rc["lookback"], rc["sma"])


# ---------- copy ----------
def footer(cfg: dict) -> str:
    return f"{FOOTER_LABEL} {cfg['footer']}"


def body(cfg: dict, stamp: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([
        f"{stamp} | IHI | 상대강도, 2027 가이던스 점검",
        "이게 뭐 알림: 미국 의료기기 ETF IHI 가 헬스케어 전체(XLV)보다 강해지고 200일선 위에 있는지, 그리고 ABT, ISRG, MDT 의 2027 가이던스가 올랐는지 봅니다. "
        + cfg["context"]["note"],
        f"지금 결과/상태: {status}",
        f"지금 할 일: {action}",
        f"나중에: {later}",
        f"익절/줄일 때: {trim}",
        f"하지 말 것: {avoid}",
        footer(cfg),
    ])


def rs_line(rs: dict) -> str:
    return (f"{rs['date']} 미국 종가 기준 63거래일 수익률 IHI {rs['ret_ihi']:+.2f}%, XLV {rs['ret_bench']:+.2f}%, 차이 {rs['gap_pp']:+.2f}%p "
            f"({'IHI 우위' if rs['cond_rs'] else 'IHI 열위'}). IHI 종가 {rs['close']:.2f}달러, 200일선 {rs['sma']:.2f}달러 "
            f"({'위' if rs['cond_ma'] else '아래'}).")


def rs_alert(cfg: dict, stamp: str, rs: dict) -> tuple[str, str, int]:
    meaning = cfg["rs_signal"]["meaning"]
    return (f"[IHI] 상대강도 전환 신호 | {meaning}",
            body(cfg, stamp, f"두 조건 모두 충족. {rs_line(rs)} 의미: {meaning}.",
                 "신규 분할 매수를 검토하세요. 들어간다면 한 번에 다 말고 나눠서, 지정가로. 해외계좌로만 살 수 있습니다.",
                 "조건 중 하나라도 깨진 종가가 나오면 재무장되고, 다시 둘 다 맞으면 한 번 더 옵니다. 2027년 1, 2월 ABT, ISRG, MDT 가이던스 판정은 따로 1번 옵니다.",
                 NO_TRIM, "신호 하루만 보고 큰 비중으로 추격 매수. " + AVOID),
            4)


def rs_fail_alert(cfg: dict, stamp: str, reason: str) -> tuple[str, str, int]:
    return ("[IHI] 시세 조회 실패 | 판단 보류",
            body(cfg, stamp, f"IHI, XLV 일봉 종가 조회 실패 또는 데이터 부족: {reason}. 판단 보류: 신호 발송도 재무장도 하지 않았습니다.",
                 "할 일 없음. 증권 앱에서 IHI 종가를 직접 볼 수 있습니다.", "다음 평일 09:00 KST에 다시 읽습니다.",
                 NO_TRIM, "조회 실패를 신호 충족이나 해제로 읽기. " + AVOID),
            3)


def rs_check(cfg: dict, state: dict, rs: dict, today: str, stamp: str, mode: str) -> list[tuple[str, str, int]]:
    s = state.setdefault("rs", {"armed": True, "on": None, "last_date": None, "fired": None, "fail_day": None, "last": None})
    if not rs.get("ok"):
        print(f"rs 판단 보류: {rs.get('reason')}")
        if s.get("fail_day") == today and mode != "force":
            return []
        s["fail_day"] = today
        return [rs_fail_alert(cfg, stamp, rs.get("reason") or "빈 응답")]
    s["last"] = {k: rs[k] for k in ("date", "close", "sma", "ret_ihi", "ret_bench", "gap_pp", "cond_rs", "cond_ma")}
    s["on"], s["last_date"] = rs["on"], rs["date"]
    if rs["on"] and s.get("armed", True):
        s.update(armed=False, fired={"at": stamp, "date": rs["date"], "gap_pp": rs["gap_pp"], "close": rs["close"], "sma": rs["sma"]})
        return [rs_alert(cfg, stamp, rs)]
    if not rs["on"] and not s.get("armed", True):
        s["armed"] = True
        print(f"rs re-armed on {rs['date']}")
    else:
        print(f"rs on={rs['on']} armed={s.get('armed', True)}; silent")
    return []


# ---------- SEC EDGAR ----------
def earnings_8ks(data: dict, months: tuple[str, ...] | None = None, after: str = "", before: str = "9999") -> list[dict]:
    recent = ((data.get("filings") or {}).get("recent") or {})
    out = []
    for i, form in enumerate(recent.get("form") or []):
        items = (recent.get("items") or [""] * (i + 1))[i] or ""
        day = (recent.get("filingDate") or [""] * (i + 1))[i] or ""
        if form == "8-K" and "2.02" in items and after < day < before and (months is None or day[:7] in months):
            out.append({"acc": recent["accessionNumber"][i], "date": day})
    return sorted(out, key=lambda x: x["date"])


def html_to_text(raw: str) -> str:
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t).replace("\xa0", " ").replace("\u2019", "'").replace("\u2013", "-").replace("\u2014", "-")
    return re.sub(r"\s+", " ", t).strip()


EX991_ROW_RE = re.compile(r'<a href="([^"]+)">[^<]*</a></td>\s*<td[^>]*>\s*EX-99\.1\s*</td>', re.I)


def pick_ex991(index_html: str, names: list[str]) -> str | None:
    """exhibit TYPE column of <acc>-index.html first, then an ex99.1-looking file name (abt-...xexhibitx991.htm too)."""
    m = EX991_ROW_RE.search(index_html or "")
    if m:
        return m.group(1).rsplit("/", 1)[-1]
    for n in sorted(names):
        low = n.lower()
        if low.endswith((".htm", ".html", ".txt")) and re.search(r"ex", low) and re.search(r"99[._-]?0?1(?!\d)", low):
            return n
    return None


def ex991_text(cik: str, acc: str) -> tuple[str, str]:
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/"
    idx = ""
    try:
        idx = fetch(base + f"{acc}-index.html", SEC_UA)
    except Exception as exc:  # noqa: BLE001
        print(f"index.html fail {acc}: {exc}")
    names: list[str] = []
    if not EX991_ROW_RE.search(idx):
        names = [x.get("name", "") for x in json.loads(fetch(base + "index.json", SEC_UA)).get("directory", {}).get("item", [])]
    name = pick_ex991(idx, names)
    if not name:
        raise RuntimeError("EX-99.1 파일 없음")
    return html_to_text(fetch(base + name, SEC_UA)), base + name


# ---------- guidance parsers (label = 2027 for ABT/ISRG, FY27 for MDT) ----------
def _range(lo: str, hi: str | None) -> dict:
    a = float(lo)
    b = float(hi) if hi is not None else a
    return {"low": a, "high": b, "mid": round((a + b) / 2, 4)}


def parse_abt(text: str, label: str = "2027") -> dict:
    out: dict = {}
    m = re.search(rf"Abbott projects full-year {label} (organic|comparable) sales growth to be (?:in the range of )?(?:approximately )?{PCT} ?%(?: to {PCT} ?%)?", text, re.I)
    if m:
        out["sales_growth"] = dict(_range(m.group(2), m.group(3)), basis=m.group(1).lower(), src=m.group(0)[:200])
    m = re.search(rf"full-year {label} adjusted diluted (?:EPS|earnings per share) of \${USD} to \${USD}", text, re.I)
    if m:
        eps = _range(m.group(1), m.group(2))
        g = re.search(rf"full-year {label} adjusted diluted (?:EPS|earnings per share) of \$[\d.]+ to \$[\d.]+,? which reflects (?:growth of )?{PCT} percent(?: growth)? at the midpoint", text, re.I)
        out["eps"] = dict(eps, src=m.group(0)[:200])
        if g:
            out["eps_growth"] = {"mid": float(g.group(1)), "src": g.group(0)[:220]}
    return out


def parse_isrg(text: str, label: str = "2027") -> dict:
    pats = [rf"Worldwide da Vinci procedure growth of (?:approximately )?{PCT}%(?: to {PCT}%)? in {label}",
            rf"worldwide da Vinci procedures to (?:increase|grow) (?:by )?(?:approximately )?{PCT}%(?: to {PCT}%)? in {label}"]
    for p in pats:
        m = re.search(p, text, re.I)
        if m:
            return {"proc_growth": dict(_range(m.group(1), m.group(2)), src=m.group(0)[:200])}
    return {}


def parse_mdt(text: str, label: str = "FY27") -> dict:
    out: dict = {}
    m = re.search(rf"{label} organic revenue growth (?:guidance )?(?:to |of )(?:the new range of )?(?:approximately )?{PCT}%(?: to {PCT}%)?", text, re.I)
    if not m:
        return out
    seg = text[m.start(): m.start() + 900]
    out["sales_growth"] = dict(_range(m.group(1), m.group(2)), src=m.group(0)[:200])
    p = re.search(rf"^.{{0,80}}?prior guidance of (?:approximately )?{PCT}%(?: to {PCT}%)?", text[m.end(): m.end() + 200], re.I)
    if p:
        out["sales_prior"] = _range(p.group(1), p.group(2))
    e = re.search(rf"non-GAAP EPS guidance (?:to the new range of |of |to |in the range of )\${USD} to \${USD}(?:,? (?:versus|compared (?:with|to)|from) the prior (?:guidance of )?\${USD} to \${USD})?", seg, re.I)
    if e:
        out["eps"] = dict(_range(e.group(1), e.group(2)), src=e.group(0)[:200])
        if e.group(3):
            out["eps_prior"] = _range(e.group(3), e.group(4))
    return out


PARSERS = {"abt": parse_abt, "isrg": parse_isrg, "mdt": parse_mdt}


def _status(delta: float, tol: float) -> str:
    if delta >= tol - 1e-9:
        return "상향"
    if delta <= -tol + 1e-9:
        return "하향"
    return "유지"


def rng(d: dict, unit: str = "%", fmt: str = "g") -> str:
    lo, hi = format(d["low"], fmt), format(d["high"], fmt)
    return f"약 {lo}{unit}" if d["low"] == d["high"] else f"{lo}{unit}~{hi}{unit}"


def judge_company(tick: str, ccfg: dict, found: dict, tol: dict, mdt_base: dict | None = None) -> dict:
    """-> {status: 상향|유지|하향|확인, lines: [...]}"""
    lines, sts = [], []
    b = ccfg["baseline"]
    if tick == "ABT":
        sg = found.get("sales_growth")
        if sg:
            st = _status(sg["mid"] - b["sales_growth_mid"], tol["growth_pp"])
            sts.append(st)
            lines.append(f"매출 성장({sg['basis']}) 가이던스 {rng(sg)}, 중간값 {sg['mid']:g}% vs 2026 첫 가이던스 {b['sales_growth_mid']:g}% -> {st}")
        else:
            lines.append("매출 성장 가이던스 못 읽음")
        eg, eps = found.get("eps_growth"), found.get("eps")
        if eg:
            st = _status(eg["mid"] - b["eps_growth_mid"], tol["abt_eps_growth_pp"])
            sts.append(st)
            erng = f"{eps['low']:.2f}~{eps['high']:.2f}달러, " if eps else ""
            lines.append(f"조정 EPS 가이던스 {erng}중간값 성장 {eg['mid']:g}% vs 2026 첫 가이던스 {b['eps_growth_mid']:g}% -> {st}")
        elif eps:
            lines.append(f"조정 EPS 가이던스 {eps['low']:.2f}~{eps['high']:.2f}달러, 중간값 성장률 문구 못 읽음(판정에서 뺌)")
        else:
            lines.append("조정 EPS 가이던스 못 읽음")
    elif tick == "ISRG":
        pg = found.get("proc_growth")
        if pg:
            st = _status(pg["mid"] - b["proc_growth_mid"], tol["growth_pp"])
            sts.append(st)
            lines.append(f"da Vinci 시술 성장 가이던스 {rng(pg)}, 중간값 {pg['mid']:g}% vs 2026 첫 가이던스 {b['proc_growth_mid']:g}% -> {st} (EPS 가이던스는 안 줌)")
        else:
            lines.append("da Vinci 시술 성장 가이던스 못 읽음")
    else:  # MDT
        base = dict(b)
        base.update(mdt_base or {})
        sg = found.get("sales_growth")
        if sg:
            prior = (found.get("sales_prior") or {}).get("mid", base.get("sales_growth_mid"))
            src = "같은 보도자료의 직전 가이던스" if found.get("sales_prior") else f"직전 가이던스({base.get('date', '설정값')})"
            st = _status(sg["mid"] - prior, tol["growth_pp"])
            sts.append(st)
            lines.append(f"FY27 organic 매출 성장 가이던스 {rng(sg)}, 중간값 {sg['mid']:g}% vs {src} {prior:g}% -> {st}")
        else:
            lines.append("FY27 매출 성장 가이던스 못 읽음")
        eps = found.get("eps")
        if eps:
            prior = (found.get("eps_prior") or {}).get("mid", base.get("eps_mid"))
            src = "같은 보도자료의 직전 가이던스" if found.get("eps_prior") else f"직전 가이던스({base.get('date', '설정값')})"
            st = _status(eps["mid"] - prior, tol["mdt_eps_usd"])
            sts.append(st)
            lines.append(f"FY27 non-GAAP EPS 가이던스 {eps['low']:.2f}~{eps['high']:.2f}달러, 중간값 {eps['mid']:.3f}달러 vs {src} {prior:.3f}달러 -> {st}")
        else:
            lines.append("FY27 EPS 가이던스 못 읽음")
    if not sts:
        status = "확인"
    elif "하향" in sts:
        status = "하향"
    elif "상향" in sts:
        status = "상향"
    else:
        status = "유지"
    return {"status": status, "lines": lines}


def combine(statuses: dict[str, str], rs_on: bool | None, est_down: bool) -> str:
    """statuses: reported companies only (상향|유지|하향|확인). -> 강세|약세|중립|확인|보류(RS unknown)."""
    up = sum(1 for v in statuses.values() if v == "상향")
    down = sum(1 for v in statuses.values() if v == "하향")
    unk = sum(1 for v in statuses.values() if v == "확인")
    if down >= 2 or est_down:
        return "약세"
    if up >= 2:
        if rs_on is None:
            return "보류"
        return "강세" if rs_on else "중립"
    if unk and (down + unk >= 2 or up + unk >= 2):
        return "확인"
    return "중립"


VERDICT_TEXT = {"강세": "강세, 신규 분할 매수 전환", "약세": "약세, 관망 연장", "중립": "중립", "확인": "확인 필요"}


def verdict_alert(cfg: dict, stamp: str, results: dict, verdict: str, rs: dict | None, est_down: bool, final_note: str, interim: bool = False) -> tuple[str, str, int]:
    gc = cfg["guidance_2027"]
    parts = []
    for t in gc["companies"]:
        r = results.get(t)
        if not r:
            parts.append(f"{t}: 기한({gc['deadline']})까지 실적 8-K 없음, 판정에서 뺌")
            continue
        parts.append(f"{t} ({r['date']} 8-K {r['acc']}): {VERDICT_STATUS.get(r['status'], r['status'])}. " + ". ".join(r["lines"]) + f". 원문 {r['link']}")
    if rs and rs.get("ok"):
        rs_txt = f"상대강도 신호 {'켜짐' if rs['on'] else '꺼짐'}: {rs_line(rs)}"
    else:
        rs_txt = "상대강도 신호 판단 보류(시세 없음)"
    est = " 애널리스트 추정치 하향(수동 입력) 있음." if est_down else ""
    status = " / ".join(parts) + f". {rs_txt}{est} 종합: {VERDICT_TEXT[verdict]}. {final_note}"
    if verdict == "강세":
        action, pri = "신규 분할 매수로 전환을 검토하세요. 한 번에 다 말고 나눠서, 지정가로, 해외계좌로.", 4
    elif verdict == "약세":
        action, pri = "관망을 연장하세요. 신규 매수는 보류합니다.", 4
    elif verdict == "확인":
        action, pri = "못 읽은 회사의 보도자료(EX-99.1) 가이던스 문단을 직접 보세요. 못 읽은 항목은 통과로 세지 않습니다. 그 전까지 신규 매수 판단 보류.", 3
    else:
        action, pri = "중립: 강세나 약세 조건이 아닙니다. 상대강도 신호를 계속 기다리세요.", 3
    later = (f"못 읽은 회사의 새 보도자료가 {gc['deadline']}까지 나오면 최종 판정이 1번 더 옵니다." if interim
             else "이 판정은 다시 오지 않습니다. 상대강도 신호는 2027-03-31 까지 계속 확인합니다.")
    title = f"[IHI] 2027 가이던스 판정 (ABT, ISRG, MDT) | {VERDICT_TEXT[verdict]}"
    return title, body(cfg, stamp, status, action, later, NO_TRIM, "가이던스 숫자를 실적콜 발언이나 기사 숫자로 바꿔 읽기. " + AVOID), pri


VERDICT_STATUS = {"상향": "상향", "유지": "유지", "하향": "하향", "확인": "확인 필요"}


def mdt_baseline_refresh(cfg: dict, state: dict, sub: dict, getter=None) -> None:
    """Nov to Dec 2026: record the FY27 guidance of a new pre-window MDT earnings 8-K as the MDT baseline (silent)."""
    getter = getter or ex991_text
    c = cfg["guidance_2027"]["companies"]["MDT"]
    g = state.setdefault("guidance", {})
    base = g.get("mdt_base") or {}
    after = base.get("date") or c["baseline"]["date"]
    for f in earnings_8ks(sub, None, after, "2027-01-01"):
        try:
            text, _ = getter(c["cik"], f["acc"])
        except Exception as exc:  # noqa: BLE001
            print(f"mdt baseline {f['acc']} fetch fail: {exc}")
            return
        found = parse_mdt(text, c["label"])
        new = {"date": f["date"], "acc": f["acc"]}
        if found.get("sales_growth"):
            new["sales_growth_mid"] = found["sales_growth"]["mid"]
        if found.get("eps"):
            new["eps_mid"] = found["eps"]["mid"]
        if len(new) > 2:
            g["mdt_base"] = new
            print(f"mdt baseline updated: {new}")
        else:
            print(f"mdt baseline {f['acc']}: FY27 guidance not readable, kept {base or c['baseline']}")


def guidance_check(cfg: dict, state: dict, subs: dict, today: str, stamp: str, rs: dict | None, getter=None) -> list[tuple[str, str, int]]:
    gc = cfg["guidance_2027"]
    getter = getter or ex991_text
    g = state.setdefault("guidance", {})
    g.setdefault("seen", {})
    g.setdefault("results", {})
    g.setdefault("fail", {})
    if g.get("verdict"):
        return []
    out = []
    for t, c in gc["companies"].items():
        sub = subs.get(t)
        if sub is None:
            continue
        for f in earnings_8ks(sub, tuple(gc["months"]), gc["seed_after"]):
            if f["acc"] in g["seen"].get(t, []):
                continue
            try:
                text, link = getter(c["cik"], f["acc"])
            except Exception as exc:  # noqa: BLE001
                print(f"guidance {t} {f['acc']} fetch fail: {exc}")
                if not g["fail"].get(f["acc"]):
                    g["fail"][f["acc"]] = stamp
                    out.append((f"[IHI] {t} 실적 원문 조회 실패 | 다음 실행 재시도",
                                body(cfg, stamp, f"{t} {f['date']} 8-K {f['acc']} EX-99.1 조회 실패: {str(exc)[:150]}. 가이던스 판정 보류.",
                                     "할 일 없음. 다음 평일 09:00에 다시 읽습니다.", "계속 실패하면 SEC 원문을 직접 보세요.", NO_TRIM,
                                     "조회 실패를 가이던스 판정으로 읽기. " + AVOID), 3))
                continue
            found = PARSERS[c["kind"]](text, c["label"])
            print(f"guidance {t} {f['acc']} parsed: " + json.dumps({k: {kk: vv for kk, vv in v.items() if kk != 'src'} for k, v in found.items()}, ensure_ascii=False))
            g["seen"].setdefault(t, []).append(f["acc"])
            prev = g["results"].get(t)
            if found or not prev:  # a readable later filing overrides; an unreadable one never hides a readable one
                j = judge_company(t, c, found, gc["tolerance"], g.get("mdt_base"))
                g["results"][t] = {"acc": f["acc"], "date": f["date"], "link": link, "status": j["status"], "lines": j["lines"]}
    results = g["results"]
    statuses = {t: r["status"] for t, r in results.items()}
    all_in = all(t in results for t in gc["companies"])
    past = today > gc["deadline"]
    rs_on = rs.get("on") if rs and rs.get("ok") else None
    est = bool(gc.get("estimate_downgrade_manual"))
    print(f"guidance statuses={statuses} all_in={all_in} past_deadline={past} rs_on={rs_on}")
    if not (all_in or past):
        return out
    if past and not results:
        g["verdict"] = {"verdict": "확인", "at": stamp, "note": "no filings"}
        out.append(("[IHI] 2027 가이던스 판정 (ABT, ISRG, MDT) | 확인 필요",
                    body(cfg, stamp, f"{gc['deadline']}까지 ABT, ISRG, MDT 실적 8-K(Item 2.02) 를 하나도 찾지 못했습니다. 판정 없이 끝냅니다.",
                         "각 회사 IR 이나 SEC 에서 2027 가이던스를 직접 보세요.", "이 판정 알림은 다시 오지 않습니다. 상대강도 신호는 2027-03-31 까지 계속 확인합니다.",
                         NO_TRIM, "판정 없음을 중립이나 통과로 읽기. " + AVOID), 3))
        return out
    v = combine(statuses, rs_on, est)
    if v == "보류":
        print("2+ 상향 but RS signal unknown today; verdict waits for the next run")
        return out
    if v == "확인" and not past:
        if g.get("check_sent"):
            return out
        g["check_sent"] = stamp
        out.append(verdict_alert(cfg, stamp, results, v, rs, est, f"기한 {gc['deadline']}까지 다시 읽은 뒤 최종 판정.", interim=True))
        return out
    if v == "확인" and g.get("check_sent"):
        g["verdict"] = {"verdict": v, "at": stamp, "note": "still unreadable at deadline; 확인 필요 already sent"}
        print("deadline passed, still 확인 필요 and already sent; closing silently")
        return out
    note = "최종 판정." if all_in else f"기한 {gc['deadline']}까지 나온 회사만으로 낸 최종 판정."
    out.append(verdict_alert(cfg, stamp, results, v, rs, est, note))
    g["verdict"] = {"verdict": v, "at": stamp, "statuses": statuses, "rs_on": rs_on}
    return out


def install_message(cfg: dict, stamp: str, rs: dict | None) -> tuple[str, str]:
    now = rs_line(rs) + f" 신호 {'켜짐' if rs['on'] else '꺼짐'}." if rs and rs.get("ok") else "지금 값은 판단 보류(시세 없음)."
    msg = body(
        cfg, stamp,
        "설치 확인. 1) 상대강도 전환: 최신 미국 종가에서 IHI 63거래일 수익률이 XLV 보다 높고 IHI 종가가 200일선 위면 1번(p4), 둘 중 하나가 깨지면 재무장. "
        f"{now} 2) 2027년 1, 2월 ABT, ISRG, MDT 실적 보도자료(8-K EX-99.1)로 2027 가이던스 상향, 유지, 하향 판정 1번: 2곳 이상 상향 + 상대강도 켜짐 = 강세, 신규 분할 매수 전환, "
        "2곳 이상 하향 또는 추정치 하향 = 약세, 관망 연장, 나머지 중립, 못 읽으면 확인 필요. MDT 는 2월 중하순, 2027-02-28 까지 안 나오면 나머지 두 곳으로 판정.",
        "평일 09:00 KST morning 번들에서만 실행됩니다. 새 cron 없음. 할 일 없음.",
        "ABT, ISRG 실적은 보통 1월 하순, MDT 는 2월 중하순입니다.",
        NO_TRIM,
        "이 설치 확인을 매수 신호로 읽기. " + AVOID,
    )
    return "[IHI] 설치 확인 | 매매 신호 아님", msg


DEFAULT_STATE = {
    "updated_at": None,
    "rs": {"armed": True, "on": None, "last_date": None, "fired": None, "fail_day": None, "last": None},
    "sec_fail_day": None,
    "guidance": {"seen": {}, "results": {}, "fail": {}, "check_sent": None, "verdict": None, "mdt_base": None},
}


def emit(alerts: list[tuple[str, str, int]], topic: str, server: str, dry: bool) -> None:
    for title, msg, pri in alerts:
        print(title)
        print(msg)
        print(f"topic={topic} send={not dry}")
        if not dry:
            publish(topic, title, msg, pri, ["stethoscope"], server)
    if not alerts:
        print("no alert")


def replay(cfg: dict) -> int:
    """Live SEC parse of the 2026 guidance releases with 2026 labels. Print only, no ntfy, no state."""
    gc = cfg["guidance_2027"]
    cases = [("ABT", "0001628280-26-002982", "2026"), ("ISRG", "0001035267-26-000003", "2026"), ("ISRG", "0001035267-26-000006", "2026"),
             ("MDT", "0001628280-26-059697", "FY27"), ("MDT", "0001628280-26-008300", "FY26")]
    for t, acc, label in cases:
        c = gc["companies"][t]
        try:
            text, link = ex991_text(c["cik"], acc)
        except Exception as exc:  # noqa: BLE001
            print(f"REPLAY {t} {acc}: fetch fail {exc}")
            continue
        found = PARSERS[c["kind"]](text, label)
        j = judge_company(t, c, found, gc["tolerance"])
        print(f"REPLAY {t} {acc} label={label}: {json.dumps({k: {kk: vv for kk, vv in v.items() if kk != 'src'} for k, v in found.items()}, ensure_ascii=False)}")
        print(f"  -> {j['status']} | " + " | ".join(j["lines"]) + f" | {link}")
    print("DRY RUN: no ntfy, no state write")
    return 0


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    dry = mode == "dry" or os.environ.get("IHI_DRY") == "1"
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    if mode == "selftest":
        return selftest(cfg)
    if mode == "replay":
        return replay(cfg)
    now = datetime.now(KST)
    today = now.date().isoformat()
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    if today > cfg["ends_kst"][:10] and mode not in {"force", "dry"}:
        print(f"after {cfg['ends_kst']} KST; silent (no fetch, no send)")
        return 0

    if mode == "confirm":
        rs = fetch_rs(cfg)
        title, msg = install_message(cfg, stamp, rs)
        print(title)
        print(msg)
        if not dry:
            publish(topic, title, msg, 3, ["white_check_mark"], server)
        else:
            print("DRY RUN: no ntfy, no state write")
        return 0

    state = load_json(STATE_PATH, DEFAULT_STATE)
    alerts: list[tuple[str, str, int]] = []
    rs = fetch_rs(cfg)
    print(f"rs={json.dumps(rs, ensure_ascii=False)}")
    alerts.extend(rs_check(cfg, state, rs, today, stamp, mode))

    gc = cfg["guidance_2027"]
    in_window = "2027-01-01" <= today and not (state.get("guidance") or {}).get("verdict")
    in_refresh = "2026-11-01" <= today < "2027-01-01"
    if in_window or in_refresh:
        subs, err = {}, None
        names = list(gc["companies"]) if in_window else ["MDT"]
        for t in names:
            try:
                subs[t] = json.loads(fetch(cfg["sources"]["sec"].format(cik=gc["companies"][t]["cik"]), SEC_UA))
            except Exception as exc:  # noqa: BLE001
                err = f"{t}: {str(exc)[:120]}"
                print(f"sec fail {err}")
        if in_refresh and "MDT" in subs:
            mdt_baseline_refresh(cfg, state, subs["MDT"])
        if in_window:
            if err and state.get("sec_fail_day") != today:
                state["sec_fail_day"] = today
                alerts.append(("[IHI] SEC 공시 조회 실패 | 판정 보류",
                               body(cfg, stamp, f"SEC 공시 목록 조회 실패({err}). 이 회사는 다음 실행에 다시 읽습니다. 가이던스 판정 보류.",
                                    "할 일 없음.", "다음 평일 09:00에 다시 읽습니다.", NO_TRIM, "조회 실패를 가이던스 판정으로 읽기. " + AVOID), 3))
            # with a failed company list, read what we can but never close on the deadline that run
            alerts.extend(guidance_check(cfg, state, subs, today if not err else min(today, gc["deadline"]), stamp, rs))
    emit(alerts, topic, server, dry)
    state["updated_at"] = now.isoformat()
    if not dry:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        print("DRY RUN: no ntfy, no state write")
    return 0


# ---------- selftest ----------
ABT_FIX = ("ABBOTT'S FINANCIAL GUIDANCE Abbott projects full-year {y} {basis} sales growth to be in the range of {slo}% to {shi}%. "
           "Abbott projects full-year {y} adjusted diluted EPS of ${elo} to ${ehi}, which reflects growth of {eg} percent at the midpoint. "
           "Abbott projects full-year {y} adjusted diluted earnings per share of ${elo} to ${ehi} and first-quarter {y} adjusted diluted earnings per share of $1.20 to $1.26.")
ISRG_FIX = ("{y} Financial Outlook The Company expects the following results for the full year of {y}: \u2022 Worldwide da Vinci procedure growth of approximately "
            "{lo}% to {hi}% in {y}, compared to 16% in 2026. \u2022 Non-GAAP* gross profit margin to be within a range of 67% and 68%.")
MDT_FIX = ("Raising FY27 organic revenue growth guidance 25 basis points to {slo}% to {shi}% Guidance The company today {verb} its FY27 organic revenue growth "
           "guidance to {slo}% to {shi}%, an {chg} from the prior guidance of {plo}% to {phi}%. The company also {verb} its FY27 diluted non-GAAP EPS guidance "
           "to the new range of ${elo} to ${ehi} versus the prior ${pelo} to ${pehi}. This guidance includes an estimated neutral impact.")
MDT_REIT = ("Guidance The company reiterates its FY27 organic revenue growth of approximately {s}% and diluted non-GAAP EPS guidance of ${elo} to ${ehi}. "
            "This includes a potential impact from tariffs.")


def _bars(start: str, closes: list[float | None]) -> list[tuple[str, float | None]]:
    d = datetime.strptime(start, "%Y-%m-%d")
    out = []
    for c in closes:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append((d.strftime("%Y-%m-%d"), c))
        d += timedelta(days=1)
    return out


def selftest(cfg: dict) -> int:
    n = {"ok": 0, "fail": 0}

    def check(label, got, want):
        ok = got == want
        n["ok" if ok else "fail"] += 1
        print(("ok  " if ok else "FAIL"), label, got, "" if ok else f"want {want}")

    stamp = "2027-02-20 09:00 KST"
    gc = cfg["guidance_2027"]
    samples = []
    from ntfy_util import route_topic

    # --- Yahoo bars: in-progress session dropped, exchange dates
    chart = {"chart": {"result": [{"meta": {"gmtoffset": -18000, "currentTradingPeriod": {"regular": {"start": 1793629800, "end": 1793653200}}},
                                   "timestamp": [1793367000, 1793629800], "indicators": {"quote": [{"close": [54.59, 55.0]}]}}]}}
    check("in-progress bar dropped", daily_closes(chart, 1793633400), [("2026-10-30", 54.59)])
    check("closed bar kept", [d for d, _ in daily_closes(chart, 1793653260)], ["2026-10-30", "2026-11-02"])

    # --- RS compute and state machine
    flat = [50.0] * 199
    ihi_up = _bars("2026-01-02", flat + [55.0])  # 200 bars: enough for the 200DMA and the 63-day return
    xlv = _bars("2026-01-02", [100.0] * 199 + [103.0])
    r = rs_compute(ihi_up, xlv)
    check("RS on: IHI +10% vs XLV +3%, close 55 > SMA 50.025", (r["ok"], r["on"], r["ret_ihi"], r["ret_bench"], r["gap_pp"]), (True, True, 10.0, 3.0, 7.0))
    r2 = rs_compute(_bars("2026-01-02", flat + [51.0]), _bars("2026-01-02", [100.0] * 199 + [104.0]))
    check("RS off: IHI +2% < XLV +4%", (r2["on"], r2["cond_rs"], r2["cond_ma"]), (False, False, True))
    r3 = rs_compute(_bars("2026-01-02", [60.0] * 199 + [55.0]), _bars("2026-01-02", [100.0] * 199 + [90.0]))
    check("RS off: IHI beats XLV but under 200DMA", (r3["on"], r3["cond_rs"], r3["cond_ma"]), (False, True, False))
    check("short history -> 판단 보류", rs_compute(ihi_up[-100:], xlv[-100:])["ok"], False)
    check("last close None -> 판단 보류", rs_compute(ihi_up[:-1] + [(ihi_up[-1][0], None)], xlv)["ok"], False)
    check("date mismatch -> 판단 보류", rs_compute(ihi_up, xlv[:-1])["ok"], False)
    check("empty -> 판단 보류", rs_compute([], xlv)["ok"], False)
    st = {}
    seq = [r2, r, r, r3, r, r2]
    got = [len(rs_check(cfg, st, x, "2026-11-0x", stamp, "auto")) for x in seq]
    check("fire once, re-arm on fail, fire again", got, [0, 1, 0, 0, 1, 0])
    fail = {"ok": False, "reason": "HTTP 429"}
    st2 = {"rs": {"armed": True, "on": True}}
    f1 = rs_check(cfg, st2, fail, "2026-11-03", stamp, "auto")
    check("data fail -> no fire, no re-arm, ops once a day", (len(f1), st2["rs"]["armed"], rs_check(cfg, st2, fail, "2026-11-03", stamp, "auto")), (1, True, []))
    check("data fail routes to cwally-ops", route_topic("cwally-port", f1[0][0]), "cwally-ops")
    st3 = {"rs": {"armed": False, "on": True}}
    rs_check(cfg, st3, fail, "2026-11-04", stamp, "auto")
    check("data fail never re-arms", st3["rs"]["armed"], False)
    a = rs_check(cfg, {}, r, "2026-11-05", stamp, "auto")
    t, m, p = a[0]
    check("RS title has [IHI] and meaning", (t.startswith("[IHI]") and "IHI" in t, "추세 전환 확인, 신규 분할 매수 검토 시점" in m, p), (True, True, 4))
    check("RS body has both returns, gap, close, 200DMA", all(k in m for k in ("IHI +10.00%", "XLV +3.00%", "+7.00%p", "종가 55.00달러", "200일선 50.02달러")), True)
    check("RS alert goes to cwally-port", route_topic("cwally-port", t), "cwally-port")
    samples += [f"TITLE: {t} (p{p})\n{m}", f"TITLE: {f1[0][0]} (p{f1[0][2]})\n{f1[0][1]}"]

    # --- parsers on real wording (2026 releases) and 2027 fixtures
    abt26 = ("Abbott projects full-year 2026 organic sales growth to be in the range of 6.5% to 7.5%. \u2022 Abbott projects full-year 2026 adjusted diluted EPS of $5.55 to $5.80, "
             "which reflects growth of 10 percent at the midpoint. \u2022")
    p = parse_abt(abt26, "2026")
    check("ABT 2026 real wording", (p["sales_growth"]["mid"], p["eps"]["mid"], p["eps_growth"]["mid"]), (7.0, 5.675, 10.0))
    p = parse_abt("Abbott projects full-year 2026 comparable sales growth to be approximately 6.0% 1 . Abbott projects full-year 2026 adjusted diluted earnings per share of $5.50 to $5.55.", "2026")
    check("ABT approximately + no growth phrase", (p["sales_growth"]["mid"], p["sales_growth"]["basis"], "eps_growth" in p, p["eps"]["mid"]), (6.0, "comparable", False, 5.525))
    check("ABT 2026 text is not read as 2027", parse_abt(abt26, "2027"), {})
    isrg26 = "The Company expects worldwide da Vinci procedures to increase approximately 13% to 15% in 2026 as compared with 2025."
    check("ISRG prelim wording", parse_isrg(isrg26, "2026")["proc_growth"]["mid"], 14.0)
    check("ISRG outlook wording", parse_isrg(ISRG_FIX.format(y="2027", lo="14", hi="16"), "2027")["proc_growth"]["mid"], 15.0)
    check("ISRG 2026 text not read as 2027", parse_isrg(isrg26, "2027"), {})
    mdt_q1 = ("Guidance The company today raised its FY27 organic revenue growth and EPS guidance. The company raised its FY27 organic revenue growth guidance to 7.25% to 7.75%, "
              "an increase from the prior guidance of 6.75% to 7.25%. The company also raised its FY27 diluted non-GAAP EPS guidance to the new range of $5.94 to $6.00 versus the prior $5.90 to $6.00.")
    p = parse_mdt(mdt_q1, "FY27")
    check("MDT real FQ1 wording + prior", (p["sales_growth"]["mid"], p["sales_prior"]["mid"], p["eps"]["mid"], p["eps_prior"]["mid"]), (7.5, 7.0, 5.97, 5.95))
    p = parse_mdt(MDT_REIT.format(s="7.5", elo="5.94", ehi="6.00"), "FY27")
    check("MDT reiterate wording", (p["sales_growth"]["mid"], p["eps"]["mid"], "sales_prior" in p), (7.5, 5.97, False))
    check("MDT FY26 text not read as FY27", parse_mdt(MDT_REIT.format(s="5.5", elo="5.62", ehi="5.66").replace("FY27", "FY26"), "FY27"), {})

    # --- company judgments
    tol = gc["tolerance"]
    cs = gc["companies"]
    def jabt(slo, shi, eg):
        return judge_company("ABT", cs["ABT"], parse_abt(ABT_FIX.format(y="2027", basis="organic", slo=slo, shi=shi, elo="6.10", ehi="6.30", eg=eg)), tol)["status"]
    check("ABT 7.0->7.5 sales, EPS 10->10: 상향", jabt("7.0", "8.0", "10"), "상향")
    check("ABT sales same, EPS 10->9: 하향 (any cut wins)", jabt("6.5", "7.5", "9"), "하향")
    check("ABT sales 7.0->7.5 but EPS 10->9: 하향", jabt("7.0", "8.0", "9"), "하향")
    check("ABT sales 7.0->7.1 (<0.25), EPS 10: 유지", jabt("6.6", "7.6", "10"), "유지")
    check("ABT sales +0.25 edge counts", jabt("6.75", "7.75", "10"), "상향")
    check("ABT nothing readable: 확인", judge_company("ABT", cs["ABT"], {}, tol)["status"], "확인")
    def jisrg(lo, hi):
        return judge_company("ISRG", cs["ISRG"], parse_isrg(ISRG_FIX.format(y="2027", lo=lo, hi=hi)), tol)["status"]
    check("ISRG 14->15: 상향", jisrg("14", "16"), "상향")
    check("ISRG 14->12: 하향", jisrg("11", "13"), "하향")
    check("ISRG 14->14: 유지", jisrg("13", "15"), "유지")
    def jmdt(slo, shi, plo, phi, elo, ehi, pelo, pehi, verb="raised", chg="increase"):
        return judge_company("MDT", cs["MDT"], parse_mdt(MDT_FIX.format(slo=slo, shi=shi, plo=plo, phi=phi, elo=elo, ehi=ehi, pelo=pelo, pehi=pehi, verb=verb, chg=chg)), tol)["status"]
    check("MDT raise vs same-release prior: 상향", jmdt("7.5", "8.0", "7.25", "7.75", "6.00", "6.05", "5.94", "6.00"), "상향")
    check("MDT lowered: 하향", jmdt("6.75", "7.25", "7.25", "7.75", "5.85", "5.95", "5.94", "6.00", "lowered", "decrease"), "하향")
    check("MDT sales up, EPS down: 하향", jmdt("7.5", "8.0", "7.25", "7.75", "5.85", "5.95", "5.94", "6.00"), "하향")
    reit = parse_mdt(MDT_REIT.format(s="7.5", elo="5.94", ehi="6.00"), "FY27")
    check("MDT reiterate vs config seed 7.5 / 5.97: 유지", judge_company("MDT", cs["MDT"], reit, tol)["status"], "유지")
    check("MDT reiterate vs refreshed baseline 7.75 / 6.00: 하향", judge_company("MDT", cs["MDT"], reit, tol, {"sales_growth_mid": 7.75, "eps_mid": 6.0, "date": "2026-11-17"})["status"], "하향")

    # --- combination
    cases = [
        ({"ABT": "상향", "ISRG": "상향", "MDT": "유지"}, True, False, "강세"),
        ({"ABT": "상향", "ISRG": "상향", "MDT": "유지"}, False, False, "중립"),
        ({"ABT": "상향", "ISRG": "상향", "MDT": "유지"}, None, False, "보류"),
        ({"ABT": "하향", "ISRG": "하향", "MDT": "상향"}, True, False, "약세"),
        ({"ABT": "상향", "ISRG": "상향", "MDT": "상향"}, True, True, "약세"),
        ({"ABT": "상향", "ISRG": "유지", "MDT": "하향"}, True, False, "중립"),
        ({"ABT": "상향", "ISRG": "확인", "MDT": "유지"}, True, False, "확인"),
        ({"ABT": "유지", "ISRG": "확인", "MDT": "유지"}, True, False, "중립"),
        ({"ABT": "상향", "ISRG": "상향", "MDT": "확인"}, True, False, "강세"),
        ({"ABT": "상향", "ISRG": "상향"}, True, False, "강세"),
        ({"ABT": "상향", "ISRG": "유지"}, True, False, "중립"),
    ]
    for sts, on, est, want in cases:
        check(f"combine {sts} rs={on} est={est}", combine(sts, on, est), want)

    # --- guidance flow over filings
    def sub(rows):
        return {"filings": {"recent": {"form": [r[0] for r in rows], "items": [r[1] for r in rows], "filingDate": [r[2] for r in rows],
                                       "accessionNumber": [r[3] for r in rows]}}}
    subs_jan = {"ABT": sub([("8-K", "2.02,9.01", "2027-01-21", "ABT-1"), ("8-K", "2.02", "2026-10-21", "ABT-0")]),
                "ISRG": sub([("8-K", "2.02,9.01", "2027-01-13", "ISRG-P"), ("8-K", "2.02,9.01", "2027-01-21", "ISRG-1")]),
                "MDT": sub([("8-K", "2.02,9.01", "2026-11-17", "MDT-Q2")])}
    subs_feb = dict(subs_jan, MDT=sub([("8-K", "2.02,9.01", "2027-02-17", "MDT-Q3"), ("8-K", "2.02,9.01", "2026-11-17", "MDT-Q2")]))
    texts = {"ABT-1": ABT_FIX.format(y="2027", basis="organic", slo="7.0", shi="8.0", elo="6.10", ehi="6.30", eg="10"),
             "ISRG-P": "Intuitive announces preliminary fourth quarter 2026 results. " + ISRG_FIX.format(y="2027", lo="14", hi="16").replace("Worldwide", "Worldwide"),
             "ISRG-1": ISRG_FIX.format(y="2027", lo="14", hi="16"),
             "MDT-Q3": MDT_FIX.format(slo="7.5", shi="8.0", plo="7.25", phi="7.75", elo="6.00", ehi="6.05", pelo="5.94", pehi="6.00", verb="raised", chg="increase")}
    calls = []
    def getter(cik, acc):
        calls.append(acc)
        return texts[acc], f"https://sec.example/{acc}"
    rs_on = dict(r, ok=True)
    st = {}
    out = guidance_check(cfg, st, subs_jan, "2027-01-22", stamp, rs_on, getter)
    check("Jan: ABT and ISRG in, MDT not yet -> silent", out, [])
    check("pre-window filings skipped", "ABT-0" in calls or "MDT-Q2" in calls, False)
    out = guidance_check(cfg, st, subs_jan, "2027-01-23", stamp, rs_on, getter)
    check("same filings again -> no refetch", (out, calls.count("ABT-1")), ([], 1))
    out = guidance_check(cfg, st, subs_feb, "2027-02-18", stamp, rs_on, getter)
    check("MDT FQ3 in -> 강세 once", [o[0] for o in out], ["[IHI] 2027 가이던스 판정 (ABT, ISRG, MDT) | 강세, 신규 분할 매수 전환"])
    samples.append(f"TITLE: {out[0][0]} (p{out[0][2]})\n{out[0][1]}")
    check("verdict body has per-company lines and RS", all(k in out[0][1] for k in ("ABT (2027-01-21", "ISRG (2027-01-21", "MDT (2027-02-17", "상대강도 신호 켜짐", "최종 판정")), True)
    check("after verdict silent", guidance_check(cfg, st, subs_feb, "2027-02-19", stamp, rs_on, getter), [])
    st = {}
    guidance_check(cfg, st, subs_jan, "2027-02-27", stamp, rs_on, getter)
    out = guidance_check(cfg, st, subs_jan, "2027-03-01", stamp, rs_on, getter)
    check("MDT missing at deadline -> judged on ABT + ISRG", [o[0][-15:] for o in out], ["강세, 신규 분할 매수 전환"[-15:]])
    check("deadline message says MDT left out", "MDT: 기한(2027-02-28)까지 실적 8-K 없음" in out[0][1], True)
    st = {}
    out = guidance_check(cfg, st, subs_feb, "2027-02-18", stamp, dict(r2, ok=True), getter)
    check("2 raised but RS off -> 중립", out[0][0].endswith("| 중립"), True)
    samples.append(f"TITLE: {out[0][0]} (p{out[0][2]})\n{out[0][1]}")
    st = {}
    check("2 raised, RS unknown -> wait", guidance_check(cfg, st, subs_feb, "2027-02-18", stamp, {"ok": False}, getter), [])
    texts_bad = dict(texts, **{"ISRG-P": "no guidance here", "ISRG-1": "still nothing"})
    def getter_bad(cik, acc):
        return texts_bad[acc], f"https://sec.example/{acc}"
    st = {}
    out = guidance_check(cfg, st, subs_feb, "2027-02-18", stamp, rs_on, getter_bad)
    check("ISRG unreadable, ABT up, MDT up -> still 강세 (2 raised)", out[0][0].endswith("강세, 신규 분할 매수 전환"), True)
    texts_bad2 = dict(texts_bad, **{"MDT-Q3": MDT_REIT.format(s="7.5", elo="5.94", ehi="6.00")})
    def getter_bad2(cik, acc):
        return texts_bad2[acc], f"https://sec.example/{acc}"
    st = {}
    out = guidance_check(cfg, st, subs_feb, "2027-02-18", stamp, rs_on, getter_bad2)
    check("ISRG unreadable could flip -> 확인 필요 once", [o[0] for o in out], ["[IHI] 2027 가이던스 판정 (ABT, ISRG, MDT) | 확인 필요"])
    samples.append(f"TITLE: {out[0][0]} (p{out[0][2]})\n{out[0][1]}")
    check("확인 필요 not repeated", guidance_check(cfg, st, subs_feb, "2027-02-19", stamp, rs_on, getter_bad2), [])
    check("still unreadable at deadline -> closed silently", (guidance_check(cfg, st, subs_feb, "2027-03-01", stamp, rs_on, getter_bad2), bool(st["guidance"].get("verdict"))), ([], True))
    st = {}
    out = guidance_check(cfg, st, {"ABT": sub([]), "ISRG": sub([]), "MDT": sub([])}, "2027-03-01", stamp, rs_on, getter)
    check("nothing filed by deadline -> one 확인 필요", (len(out), out[0][0].endswith("확인 필요")), (1, True))
    cfg_est = json.loads(json.dumps(cfg))
    cfg_est["guidance_2027"]["estimate_downgrade_manual"] = True
    st = {}
    out = guidance_check(cfg_est, st, subs_feb, "2027-02-18", stamp, rs_on, getter)
    check("manual estimate downgrade -> 약세, 관망 연장", out[0][0].endswith("약세, 관망 연장"), True)
    samples.append(f"TITLE: {out[0][0]} (p{out[0][2]})\n{out[0][1]}")
    def boom(cik, acc):
        raise RuntimeError("HTTP 503")
    st = {}
    out = guidance_check(cfg, st, subs_feb, "2027-02-18", stamp, rs_on, boom)
    check("EX-99.1 fetch fail -> ops notice per accession, not seen", (len(out), route_topic("cwally-port", out[0][0]), st["guidance"]["seen"]), (4, "cwally-ops", {}))
    check("fetch fail not repeated", guidance_check(cfg, st, subs_feb, "2027-02-18", stamp, rs_on, boom), [])

    # --- MDT baseline refresh (Nov to Dec 2026)
    st = {}
    mdt_q2 = MDT_FIX.format(slo="7.5", shi="8.0", plo="7.25", phi="7.75", elo="5.98", ehi="6.04", pelo="5.94", pehi="6.00", verb="raised", chg="increase")
    mdt_baseline_refresh(cfg, st, sub([("8-K", "2.02,9.01", "2026-11-17", "MDT-Q2"), ("8-K", "2.02", "2026-09-01", "MDT-Q1")]), lambda c, a: (mdt_q2, "x"))
    check("MDT baseline refreshed from FQ2", st["guidance"]["mdt_base"], {"date": "2026-11-17", "acc": "MDT-Q2", "sales_growth_mid": 7.75, "eps_mid": 6.01})

    # --- install, copy rules
    ti, mi = install_message(cfg, "2026-11-02 13:30 KST", r)
    check("install title", ti, "[IHI] 설치 확인 | 매매 신호 아님")
    samples.append(f"TITLE: {ti} (p3)\n{mi}")
    allt = "\n".join(samples)
    check("footer on every sample", allt.count(cfg["footer"]) == len(samples), True)
    check("no sell / trim instruction", any(k in allt for k in ("매도 검토", "비중 축소 검토", "익절 검토", "정리 검토")), False)
    check("no middle dot / em-dash in samples", any(ch in allt for ch in "\u00b7\u2014"), False)
    check("six-item order", all(s.find("이게 뭐 알림") < s.find("지금 결과/상태") < s.find("지금 할 일") < s.find("나중에") < s.find("익절/줄일 때") < s.find("하지 말 것") for s in samples), True)
    titles = [s.split("\n", 1)[0] for s in samples]
    check("all titles start with [IHI]", all(x.startswith("TITLE: [IHI]") for x in titles), True)
    src = Path(__file__).read_text(encoding="utf-8")
    check("no middle dot / em-dash in script", any(ch in re.sub(r"\\u2014|\\u00b7", "", src) for ch in "\u00b7\u2014"), False)
    check("no middle dot / em-dash in config", any(ch in CFG_PATH.read_text(encoding="utf-8") for ch in "\u00b7\u2014"), False)
    check("ends 2027-03-31", cfg["ends_kst"][:10], "2027-03-31")
    print("\n--- samples ---\n" + "\n\n".join(samples))
    print(f"SELFTEST {'OK' if not n['fail'] else 'FAIL'}: {n['ok']} passed, {n['fail']} failed (fixtures only, no network, no publish, no state)")
    return 1 if n["fail"] else 0


def guarded_main() -> int:
    """The morning bundle runs scripts back to back under bash -e: an IHI crash must not stop the next scripts."""
    try:
        return main()
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
        if mode in {"auto", "force"} and os.environ.get("IHI_DRY") != "1":
            try:
                publish("cwally-ops", "[IHI] 스크립트 오류 | 판단 보류", f"ihi_monitor.py 오류: {str(exc)[:300]}. 이번 실행은 판단 보류(발송, 상태 변경 없음).", 3, ["warning"])
            except Exception as exc2:  # noqa: BLE001
                print(f"ops notice failed: {exc2}")
        return 0 if mode in {"auto", "force"} else 1


if __name__ == "__main__":
    raise SystemExit(guarded_main())
