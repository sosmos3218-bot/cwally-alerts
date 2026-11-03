#!/usr/bin/env python3
"""[BKE] The Buckle (BKE, NYSE) watch (approved in chat 2026-11-03 t38u). Ends 2027-02-26 23:59 KST.

Two parts, both on the weekday 09:00 KST morning bundle (no new repeating cron):

  earn   Q3 FY2026 results. From 2026-11-20 00:00 KST, poll SEC EDGAR submissions (CIK 0000885245) for a new
         8-K with Item 2.02 filed on/after 2026-11-15. Found -> ONE p4 summary parsed from Exhibit 99.1
         (net sales and YoY, Q3 comps, gross margin, EPS vs consensus 0.91 and prior year 0.96, inventory,
         cash and investments, dividend 8-K (Item 8.01) if any, October/November sales if the release
         mentions them, price reaction from the Yahoo daily bar of the filing day), compared with the
         baseline (TTM EPS 4.34, Sept comps +0.5%, YTD comps +2.8%, regular dividend 0.35, Hold).
         Figures not parsed -> the same message with the 8-K links and a '원문 확인' line.
         Not found by 2026-11-27 09:00 KST -> ONE p3 '미발견' notice and this part stops.
         Morning does not run on Saturday, so oneshot_kst 2026-11-21 09:00 (job=oneshot) runs `auto`;
         Monday 11/23 09:00 morning is the fallback. Dedupe: state earn.done / earn.seen.
  px     US regular close < 40.00 -> ONE p4 message (52-week low 39.44 and regular-dividend yield in the
         body). Re-arm only when a close is >= 41.00 (1.00 buffer). First run seeds from the latest
         completed close, armed, no send. Dedupe by bar date.

  sdiv   Special dividend (approved in chat 2026-11-03 t39u). 8-Ks filed 2026-12-01 .. 2027-01-31 with Item 8.01
         (primary) or Item 7.01 (robustness) whose Exhibit 99 text has 'special' + 'dividend' -> ONE p4 message per
         accession: special per share, record/payment/ex-date, regular dividend in the same release, special yield
         and (1.40 + special) yield at the latest close, vs prior year 3.00, 8-K link. Amount not parsed -> link +
         '원문 확인'. Regular-only releases are recorded as seen and not sent. None by 2027-01-31 -> ONE p3
         '특별배당 공시 없음(1/31까지)' notice (first run from 2027-02-01 00:00 KST) and this part stops.

Modes: auto | dry (print only, no ntfy, never writes state) | selftest. Not investment advice.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
NY = ZoneInfo("America/New_York")
CFG = ROOT / "configs" / "bke.json"
STATE = ROOT / "state" / "bke.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}
NAME = "[BKE]"
BANNED_CHARS = ("\u00b7", "\u2014", "\u318d", "\u3186")
TITLE_BANNED = ("매수", "신호")
DRY = False


def load_cfg() -> dict:
    return json.loads(CFG.read_text(encoding="utf-8"))


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    except json.JSONDecodeError:
        return {}


def save_state(st: dict, dry: bool) -> None:
    if dry or DRY:
        print("dry: state not saved")
        return
    st["updated_at"] = datetime.now(KST).isoformat(timespec="seconds")
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def clean(text: str) -> str:
    """Korean output rule: no middle dot, no hangul middle dot, no em-dash (also inside quoted English)."""
    return text.replace("\u2014", "-").replace("\u2013", "-").replace("\u00b7", " ").replace("\u318d", " ").replace("\u3186", " ")


def send(kind: str, title: str, body: str, prio: int, tags: list[str], dry: bool) -> None:
    title, body = clean(title), clean(body)
    if dry or DRY:
        print(f"DRY RUN would send ({kind}, p{prio}): {title}\n{body}")
        return
    publish(resolve_topic(kind), title, body, prio, tags=tags, config_name="bke")


def kst(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=KST)


def ends_at(cfg: dict) -> datetime:
    return kst(cfg["ends_kst"])


# ---------- data ----------

def fetch_bars(cfg: dict, now_ny: datetime) -> list[tuple[date, float]]:
    """Completed regular-session daily bars (US/Eastern date, close), ascending. Today's live bar is excluded."""
    req = urllib.request.Request(cfg["sources"]["bars"], headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        res = json.loads(resp.read().decode())["chart"]["result"][0]
    out: dict[date, float] = {}
    for t, c in zip(res.get("timestamp") or [], res["indicators"]["quote"][0].get("close") or []):
        if c is not None:
            out[datetime.fromtimestamp(t, NY).date()] = float(c)
    meta = res.get("meta") or {}
    rmt, rmp = meta.get("regularMarketTime"), meta.get("regularMarketPrice")
    if rmt and rmp:
        mt = datetime.fromtimestamp(rmt, NY)
        if mt.date() not in out and mt.hour * 60 + mt.minute >= 16 * 60:
            out[mt.date()] = float(rmp)
    today = now_ny.date()
    after_close = now_ny.hour * 60 + now_ny.minute >= 16 * 60 + 10
    return sorted((d, c) for d, c in out.items() if d < today or (d == today and after_close))


def edgar_get(cfg: dict, url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": cfg["edgar"]["user_agent"], "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="replace")


def html_text(h: str) -> str:
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", h or "", flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", unescape(t)).strip()


def recent_filings(sub: dict) -> list[dict]:
    r = sub["filings"]["recent"]
    return [{"form": r["form"][i], "date": r["filingDate"][i], "acc": r["accessionNumber"][i],
             "items": r.get("items", [""] * len(r["form"]))[i] or "", "doc": r["primaryDocument"][i]}
            for i in range(len(r["form"]))]


def _items(f: dict) -> list[str]:
    return [x.strip() for x in (f.get("items") or "").split(",")]


def pick_earnings(cfg: dict, filings: list[dict], seen: set[str]) -> dict | None:
    e = cfg["edgar"]
    for f in sorted(filings, key=lambda x: x["date"]):
        if f["form"] == e["form"] and e["item"] in _items(f) and f["date"] >= e["min_filing_date"] and f["acc"] not in seen:
            return f
    return None


def dividend_filings(cfg: dict, filings: list[dict]) -> list[dict]:
    e = cfg["edgar"]
    return [f for f in sorted(filings, key=lambda x: x["date"])
            if f["form"] == e["form"] and e["dividend_item"] in _items(f) and f["date"] >= e["min_filing_date"]]


def filing_urls(cfg: dict, f: dict) -> tuple[str, str, str]:
    base = f"{cfg['edgar']['archive_base']}/{f['acc'].replace('-', '')}"
    return base, f"{base}/{f['acc']}-index.html", f"{base}/{f['doc']}"


def exhibit_name(index_json: dict) -> str | None:
    names = [i["name"] for i in index_json["directory"]["item"]]
    for n in names:
        low = n.lower()
        if low.endswith((".htm", ".html", ".txt")) and (re.search(r"(ex|exhibit)[-_]?99", low) or "8kexhibit" in low
                                                        or "kdivex" in low or "exhibit" in low):
            if not low.startswith("r") or not re.fullmatch(r"r\d+\.htm", low):
                return n
    return None


def exhibit_text(cfg: dict, f: dict, get) -> tuple[str | None, str | None, str]:
    """(exhibit url, exhibit text, note). Falls back to the 8-K main document text."""
    base, _, main_url = filing_urls(cfg, f)
    note = ""
    try:
        ex = exhibit_name(json.loads(get(f"{base}/index.json")))
        if ex:
            url = f"{base}/{ex}"
            return url, html_text(get(url)), note
        note = "Exhibit 99.1 파일 못 찾음, 8-K 본문으로 대체. "
    except Exception as exc:  # noqa: BLE001
        note = f"Exhibit 99.1 조회 실패({type(exc).__name__}), 8-K 본문으로 대체. "
    try:
        return None, html_text(get(main_url)), note
    except Exception as exc:  # noqa: BLE001
        return None, None, note + f"8-K 본문 조회 실패({type(exc).__name__}). "


# ---------- parsing (pure) ----------

def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _signed(word: str, val: str) -> float:
    return -float(val) if word.lower().startswith("decrease") else float(val)


def parse_release(text: str) -> dict:
    t = text or ""
    out: dict = {"third_quarter": bool(re.search(r"THIRD QUARTER", t))}
    m = re.search(r"Net sales for the 13-week fiscal quarter ended [^$]*?(increased|decreased) ([\d.]+) percent to \$([\d.,]+) million"
                  r" from net sales of \$([\d.,]+) million", t)
    if m:
        out.update(sales_yoy=_signed(m.group(1), m.group(2)), sales=_num(m.group(3)), sales_prior=_num(m.group(4)))
    m = re.search(r"Comparable store net sales for the 13-week fiscal quarter ended [^$%]*?(increased|decreased) ([\d.]+) percent", t)
    if m:
        out["comps"] = _signed(m.group(1), m.group(2))
    elif re.search(r"Comparable store net sales for the 13-week fiscal quarter ended [^.%]*?(were|was) flat", t):
        out["comps"] = 0.0
    m = re.search(r"Comparable store net sales for the (?:39|40)-week (?:fiscal )?period ended [^$%]*?(increased|decreased) ([\d.]+) percent", t)
    if m:
        out["comps_ytd"] = _signed(m.group(1), m.group(2))
    m = re.search(r"net income for the fiscal quarter ended [^$]*?was \$([\d.,]+) million, or \$([\d.]+) per share "
                  r"\(\$([\d.]+) per share on a diluted basis\)", t)
    if m:
        out.update(net_income=_num(m.group(1)), eps_basic=float(m.group(2)), eps=float(m.group(3)))
    else:
        m = re.search(r"EARNINGS PER SHARE: Basic \$ ?([\d.]+) \$ ?([\d.]+) .*?Diluted \$ ?([\d.]+) \$ ?([\d.]+)", t)
        if m:
            out.update(eps=float(m.group(3)), eps_prior_tbl=float(m.group(4)))
    m = re.search(r"SALES, Net of returns and allowances \$ ?([\d,]+) \$ ?([\d,]+)", t)
    g = re.search(r"Gross profit \$? ?([\d,]+) \$? ?([\d,]+)", t)
    if m and g:
        s0, s1, g0, g1 = _num(m.group(1)), _num(m.group(2)), _num(g.group(1)), _num(g.group(2))
        if s0 and s1:
            out.update(gm=round(g0 / s0 * 100, 2), gm_prior=round(g1 / s1 * 100, 2))
    m = re.search(r"Inventory \$? ?([\d,]+) \$? ?([\d,]+) \$? ?([\d,]+)", t)
    if m:
        out.update(inventory=_num(m.group(1)) / 1000, inventory_prior=_num(m.group(3)) / 1000)
    c = re.search(r"Cash and cash equivalents \$ ?([\d,]+) \$ ?([\d,]+) \$ ?([\d,]+)", t)
    s = re.search(r"Short-term investments \$? ?([\d,]+) \$? ?([\d,]+) \$? ?([\d,]+)", t)
    lt = re.search(r"LONG-TERM INVESTMENTS \$? ?([\d,]+) \$? ?([\d,]+) \$? ?([\d,]+)", t, flags=re.I)
    if c and s and lt:
        out.update(cash_inv=sum(_num(x.group(1)) for x in (c, s, lt)) / 1000,
                   cash_inv_prior=sum(_num(x.group(3)) for x in (c, s, lt)) / 1000)
    elif c:
        out.update(cash_only=_num(c.group(1)) / 1000)
    m = re.search(r"[^.]*\b(?:fiscal October|fiscal November|November[- ]to[- ]date|quarter[- ]to[- ]date|month[- ]to[- ]date)\b[^.]*\.", t)
    if m:
        out["recent_sales_note"] = m.group(0).strip()[:220]
    out.update(parse_dividend(t))
    return out


def parse_dividend(text: str) -> dict:
    out = {}
    m = re.search(r"\$([\d.]+) per share special cash dividend", text or "")
    if m:
        out["special_div"] = float(m.group(1))
    m = re.search(r"\$([\d.]+) per share quarterly dividend", text or "")
    if m:
        out["regular_div"] = float(m.group(1))
    return out


def parsed_ok(p: dict) -> bool:
    return all(k in p for k in ("sales", "sales_yoy", "eps"))


# ---------- messages ----------

def pct(v: float | None, digits: int = 1) -> str:
    return "확인 안 됨" if v is None else f"{v:+.{digits}f}%"


def header(now: datetime, what: str) -> str:
    return f"{now:%Y-%m-%d %H:%M} KST | BKE | 더 버클 {what}"


def reaction_text(bars: list[tuple[date, float]] | None, filed: str) -> str:
    if not bars:
        return "주가 반응: 시세 조회 안 됨."
    d = date.fromisoformat(filed)
    idx = [i for i, (bd, _) in enumerate(bars) if bd == d]
    if not idx or idx[0] == 0:
        last_d, last_c = bars[-1]
        return f"주가 반응: 발표일({filed}) 정규장 종가 아직 없음. 최근 종가 {last_c:.2f}달러({last_d})."
    i = idx[0]
    c, pc = bars[i][1], bars[i - 1][1]
    return f"주가 반응: 발표일 {filed} 종가 {c:.2f}달러, 전 거래일 {pc:.2f}달러 대비 {(c / pc - 1) * 100:+.1f}%."


def earn_verdict(cfg: dict, p: dict) -> tuple[str, str]:
    """(short title action, todo sentence). Simple fixed rules, reference only."""
    e = cfg["earn"]
    eps, comps = p.get("eps"), p.get("comps")
    if eps is None:
        return "원문 확인", "EPS 를 자동으로 못 읽었습니다. 원문 확인 후 Hold(보통) 판단 유지 여부를 직접 정하세요."
    beat = eps >= e["consensus_eps"]
    if beat and (comps is None or comps >= 0):
        return "Hold 유지 점검", ("EPS 가 컨센서스 이상이고 동일점 매출이 줄지 않았습니다. 기존 Hold(보통) 유지 근거. "
                               "서둘러 비중을 늘리지 말고 12월 배당 발표와 11월 매출을 확인.")
    if not beat and comps is not None and comps < 0:
        return "Hold 약화 점검", ("EPS 가 컨센서스 아래이고 동일점 매출도 줄었습니다. Hold(보통) 판단 약화. "
                               "비중 확대 보류, 종가 40달러선과 52주 저점 39.44달러 확인.")
    return "Hold 재점검", ("EPS 와 동일점 매출 중 하나만 기준에 못 미쳤습니다. Hold(보통) 유지하되 비중 확대 보류, "
                          "원문에서 매출총이익률과 재고 추세 확인.")


def earnings_message(cfg: dict, f: dict, info: dict, p: dict, div: dict, bars, now: datetime) -> tuple[str, str, int]:
    e, b = cfg["earn"], cfg["earn"]["baseline"]
    ok = parsed_ok(p)
    links = f"8-K 목록 {info['index_url']}" + (f", 보도자료(Exhibit 99.1) {info['ex_url']}" if info.get("ex_url") else f", 8-K 본문 {info['main_url']}")
    if not ok:
        title = f"{NAME} 3분기 실적 8-K 나옴 | 원문 확인"
        state = (f"{f['date']} 8-K 제출(접수번호 {f['acc']}). 숫자를 자동으로 다 읽지 못했습니다(원문 확인). "
                 f"{links}. {info.get('note', '').strip()}")
        todo = ("원문 확인: 매출과 전년 대비, 3분기 동일점, 매출총이익률, 희석 EPS(컨센서스 0.91달러, 전년 0.96달러), "
                "재고, 현금과 투자자산을 직접 보고 Hold(보통) 유지 여부 판단.")
    else:
        act, todo = earn_verdict(cfg, p)
        eps = p["eps"]
        vs = "상회" if eps > e["consensus_eps"] else ("부합" if eps == e["consensus_eps"] else "하회")
        title = f"{NAME} 3분기 실적 EPS {eps:.2f}달러, 예상 {e['consensus_eps']:.2f} {vs} | {act}"
        parts = [f"{f['date']} 8-K 제출(접수번호 {f['acc']}).",
                 f"매출 {p['sales']:.1f}백만달러, 전년 대비 {pct(p['sales_yoy'])} (전년 {p['sales_prior']:.1f}백만달러).",
                 f"3분기 동일점 {pct(p.get('comps'))} (전년 3분기 +{e['prior_comps_pct']}%, 9월 {pct(b['sept_comps_pct'])}, 35주 누계 {pct(b['ytd_comps_pct'])})"
                 + (f", 39주 누계 동일점 {pct(p['comps_ytd'])}." if p.get("comps_ytd") is not None else ".")]
        if p.get("gm") is not None:
            parts.append(f"매출총이익률 {p['gm']:.1f}% (전년 {p['gm_prior']:.1f}%, {(p['gm'] - p['gm_prior']) * 100:+.0f}bp).")
        else:
            parts.append("매출총이익률: 표를 못 읽음(원문 확인).")
        ttm = b["ttm_eps"] - e["prior_eps"] + eps
        parts.append(f"희석 EPS {eps:.2f}달러 (컨센서스 {e['consensus_eps']:.2f}, 전년 {e['prior_eps']:.2f}, 전년 대비 {(eps / e['prior_eps'] - 1) * 100:+.1f}%). "
                     f"TTM EPS {b['ttm_eps']:.2f} -> {ttm:.2f}달러.")
        if p.get("inventory") is not None:
            parts.append(f"재고 {p['inventory']:.1f}백만달러 (전년 같은 때 {p['inventory_prior']:.1f}, {(p['inventory'] / p['inventory_prior'] - 1) * 100:+.1f}%).")
        else:
            parts.append("재고: 표를 못 읽음(원문 확인).")
        if p.get("cash_inv") is not None:
            parts.append(f"현금과 투자자산 {p['cash_inv']:.1f}백만달러 (전년 같은 때 {p['cash_inv_prior']:.1f}).")
        elif p.get("cash_only") is not None:
            parts.append(f"현금 {p['cash_only']:.1f}백만달러(투자자산은 원문 확인).")
        else:
            parts.append("현금과 투자자산: 표를 못 읽음(원문 확인).")
        state = " ".join(parts)
        state += " " + links + "."
        if info.get("note"):
            state += " " + info["note"].strip()
    if div.get("special_div") or div.get("regular_div"):
        dv = []
        if div.get("regular_div"):
            dv.append(f"정기배당 {div['regular_div']:.2f}달러" + (" (기준 0.35 그대로)" if abs(div["regular_div"] - b["regular_div"]) < 1e-9 else f" (기준 {b['regular_div']:.2f}에서 변경)"))
        if div.get("special_div"):
            dv.append(f"특별배당 {div['special_div']:.2f}달러")
        divtxt = "배당 발표: " + ", ".join(dv) + (f" ({div['src']})." if div.get("src") else ".")
    else:
        divtxt = "배당 발표: 아직 없음(작년은 12/9 이사회 뒤 특별배당 3.00달러 + 정기배당 0.35달러 발표)."
    recent = ("10월/11월 매출: " + p["recent_sales_note"]) if p.get("recent_sales_note") else \
        "10월/11월 매출: 실적 보도자료에 언급 없음(월간 매출은 회사 별도 보도자료, SEC 미제출)."
    state += f" {divtxt} {recent} {reaction_text(bars, f['date'])}"
    if ok and not p.get("third_quarter"):
        state += " (보도자료에 'THIRD QUARTER' 문구가 없어 분기 확인 필요.)"
    lines = [
        header(now, "3분기 실적"),
        "이게 뭐 알림: 버클(BKE) 3분기 실적 공시(SEC 8-K Item 2.02)를 자동으로 요약해 1번 보내는 알림. " + cfg["source_note"],
        "지금 결과/상태: " + state,
        "지금 할 일: " + todo + " 기준: " + b["note"],
        "나중에: 12월 초 배당 발표(작년 특별배당 3.00달러), 11월 월간 매출(12월 초 회사 보도자료). 종가 40달러선은 평일 09:00 에 계속 봅니다.",
        "익절/줄일 때: 동일점 매출 감소가 이어지고 EPS 가 2분기 연속 컨센서스를 밑돌면 비중 축소 검토.",
        "하지 말 것: 헤드라인 숫자만 보고 장전/시간외 추격, 특별배당 기대만으로 비중 확대, 신용/미수, 이 알림만 보고 주문. 투자 권유 아님.",
    ]
    return title, "\n".join(lines), int(e["priority"])


def missing_message(cfg: dict, now: datetime) -> tuple[str, str, int]:
    title = f"{NAME} 3분기 실적 8-K 미발견 | 발표일 직접 확인"
    lines = [
        header(now, "3분기 실적"),
        "이게 뭐 알림: 11/27 09:00 KST 까지 SEC 에 버클 실적 8-K(Item 2.02)가 안 보여 1번 오는 안내. 실적 요약은 여기서 멈춥니다. " + cfg["source_note"],
        "지금 결과/상태: EDGAR(CIK 0000885245)에 2026-11-15 이후 Item 2.02 8-K 없음. 예상일은 " + cfg["earn"]["expected"],
        "지금 할 일: 버클 IR(corporate.buckle.com) 보도자료에서 3분기 실적 발표 여부를 직접 확인. 그 전까지 Hold(보통) 유지, 비중 확대 보류.",
        "나중에: 자동 요약은 더 보내지 않습니다. 종가 40달러선 점검은 평일 09:00 에 계속.",
        "익절/줄일 때: 해당 없음.",
        "하지 말 것: 공시 확인 전 추측으로 주문, 신용/미수. 투자 권유 아님.",
    ]
    return title, "\n".join(lines), int(cfg["earn"]["missing_priority"])


def price_message(cfg: dict, close: float, day: str, prev_close: float | None, earn_state: dict, now: datetime) -> tuple[str, str, int]:
    px = cfg["price"]
    yld = px["annual_regular_div"] / close * 100
    below_low = close < px["low_52w"]
    chg = f", 전 거래일 대비 {(close / prev_close - 1) * 100:+.1f}%" if prev_close else ""
    title = f"{NAME} 종가 {close:.2f}달러, 40달러 아래 | " + ("52주 저점도 이탈, 판단 재점검" if below_low else "Hold 판단 재점검")
    if earn_state.get("done") == "found":
        earn = f"3분기 실적 8-K 요약 받음({earn_state.get('found_date')})"
    elif earn_state.get("done") == "missing":
        earn = "3분기 실적 8-K 는 11/27 까지 미발견"
    else:
        earn = "3분기 실적(11/20 예상) 확인 전"
    low = (f"52주 저점 {px['low_52w']:.2f}달러({px['low_52w_date']} 장중) 아래, 추세 약세 확인." if below_low
           else f"52주 저점 {px['low_52w']:.2f}달러({px['low_52w_date']} 장중)보다 {(close / px['low_52w'] - 1) * 100:.1f}% 위.")
    lines = [
        header(now, "종가 40달러선"),
        "이게 뭐 알림: " + cfg["explain"] + " " + cfg["source_note"],
        f"지금 결과/상태: 미국 정규장 종가 {close:.2f}달러({day} 미 동부{chg}), 40.00달러 아래로 새로 내려감. {low} "
        f"정기배당(분기 0.35달러, 연 1.40달러) 기준 배당수익률 {yld:.2f}%. {earn}.",
        "지금 할 일: Hold(보통) 판단을 다시 점검. 3분기 실적, 월간 동일점 매출 추세, 배당 발표를 원문으로 확인하고, 그 전까지 비중 확대 보류.",
        f"나중에: 같은 알림은 종가가 {px['rearm']:.2f}달러 이상(완충 1달러)으로 회복한 뒤 다시 40달러 아래로 내려갈 때만 또 옵니다(평일 09:00 점검).",
        "익절/줄일 때: 52주 저점 39.44달러 아래 종가가 이어지고 실적도 기준에 못 미치면 비중 축소 검토.",
        "하지 말 것: 하루 종가만 보고 급히 팔거나 물타기, 장중 급락 추격, 신용/미수, 이 알림만 보고 주문. 투자 권유 아님.",
    ]
    return title, "\n".join(lines), int(px["priority"])


# ---------- core (pure) ----------

def px_step(cfg: dict, armed: bool | None, close: float) -> tuple[bool, bool]:
    """(new armed, fired). armed None = first run: seed armed (close >= line), never fire."""
    px = cfg["price"]
    if armed is None:
        return close >= px["line"], False
    if armed and close < px["line"]:
        return False, True
    if not armed and close >= px["rearm"]:
        return True, False
    return armed, False


# ---------- run ----------

def run_px(cfg: dict, st: dict, bars, now: datetime, dry: bool) -> bool:
    ps = st.setdefault("px", {})
    if bars is None:
        return False
    if not bars:
        print("px: no completed bars")
        return False
    last_d, last_c = bars[-1]
    if (now.date() - last_d).days > int(cfg["price"].get("stale_days", 6)):
        print(f"px: stale bar {last_d}; skip")
        return False
    if not ps.get("initialized"):
        armed, _ = px_step(cfg, None, last_c)
        ps.update({"initialized": True, "armed": armed, "last_date": str(last_d), "last_close": round(last_c, 4),
                   "init_note": f"initialized from {last_d} close {last_c:.2f}, no send"})
        print(f"px: init from {last_d} close {last_c:.2f} armed={armed} (no send)")
        return True
    prev_date = ps.get("last_date")
    new_bars = [(d, c) for d, c in bars if prev_date is None or str(d) > prev_date]
    if not new_bars:
        print(f"px: no new bar after {prev_date}; armed={ps.get('armed')}")
        return False
    armed, prev_close = bool(ps.get("armed", True)), ps.get("last_close")
    fire = None
    for i, (d, c) in enumerate(new_bars):
        armed, fired = px_step(cfg, armed, c)
        print(f"px: bar {d} close {c:.2f} -> fired={fired} armed={armed}")
        if fired and fire is None:
            fire = (d, c, prev_close if i == 0 else new_bars[i - 1][1])
    if fire:
        d, c, pc = fire
        t, b, p = price_message(cfg, c, str(d), pc, st.get("earn") or {}, now)
        send("port", t, b, p, ["warning"], dry)
        ps["last_sent"] = {"day": str(d), "close": round(c, 4), "title": clean(t)}
    else:
        print("px: no new close below 40.00; silent")
    d, c = new_bars[-1]
    ps.update({"armed": armed, "last_date": str(d), "last_close": round(c, 4)})
    return True


def run_earn(cfg: dict, st: dict, bars, now: datetime, dry: bool, fetch_sub, get) -> bool:
    e = cfg["earn"]
    es = st.setdefault("earn", {})
    if es.get("done"):
        print(f"earn: done ({es['done']})")
        return False
    if now < kst(e["poll_start_kst"]):
        print("earn: before poll window")
        return False
    try:
        filings = recent_filings(fetch_sub())
    except Exception as exc:  # noqa: BLE001
        print(f"earn: EDGAR fetch failed: {exc}")
        day = now.strftime("%Y-%m-%d")
        if es.get("fail_day") == day:
            return False
        send("ops", f"{NAME} EDGAR 조회 실패 | 다음 09:00 재시도",
             f"SEC EDGAR submissions 조회 실패: {type(exc).__name__} {exc}"[:300] + ". 다음 실행에서 다시 봅니다.", 3, ["warning"], dry)
        es["fail_day"] = day
        return True
    seen = set(es.get("seen") or [])
    f = pick_earnings(cfg, filings, seen)
    if f:
        base, index_url, main_url = filing_urls(cfg, f)
        ex_url, text, note = exhibit_text(cfg, f, get)
        p = parse_release(text or "")
        div = {k: v for k, v in p.items() if k in ("special_div", "regular_div")}
        if div:
            div["src"] = "실적 보도자료"
        for df in reversed(dividend_filings(cfg, filings)):
            if div:
                break
            _, dt, _ = exhibit_text(cfg, df, get)
            d2 = parse_dividend(dt or "")
            if d2:
                div = {**d2, "src": f"{df['date']} 8-K Item 8.01"}
        info = {"index_url": index_url, "main_url": main_url, "ex_url": ex_url, "note": note}
        t, b, pr = earnings_message(cfg, f, info, p, div, bars, now)
        send("port", t, b, pr, ["bar_chart"], dry)
        es.update({"done": "found", "found_accession": f["acc"], "found_date": f["date"], "found_at": now.strftime("%Y-%m-%d %H:%M KST"),
                   "found_index": index_url, "parsed": parsed_ok(p),
                   "figures": {k: p.get(k) for k in ("sales", "sales_yoy", "comps", "gm", "eps", "inventory", "cash_inv")}})
        es["seen"] = sorted(seen | {f["acc"]})
        return True
    print("earn: no new Item 2.02 8-K")
    if now >= kst(e["deadline_kst"]):
        t, b, pr = missing_message(cfg, now)
        send("port", t, b, pr, ["hourglass"], dry)
        es.update({"done": "missing", "missing_sent": now.strftime("%Y-%m-%d %H:%M KST")})
        return True
    return False


# ---------- special dividend (pure parse + message) ----------

MONTHS = {m: i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August",
                                       "September", "October", "November", "December"], 1)}
_DATE = r"((?:January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}, \d{4})"


def iso_date(text: str | None) -> str | None:
    if not text:
        return None
    m = re.fullmatch(r"([A-Za-z]+) (\d{1,2}), (\d{4})", text.strip())
    if not m or m.group(1) not in MONTHS:
        return None
    return f"{int(m.group(3)):04d}-{MONTHS[m.group(1)]:02d}-{int(m.group(2)):02d}"


def mentions_special(text: str) -> bool:
    """'special' as a word (not 'specialty retailer' in the boilerplate) within one sentence of 'dividend'."""
    t = text or ""
    return bool(re.search(r"\bspecial\b[^.]{0,120}\bdividends?\b|\bdividends?\b[^.]{0,120}\bspecial\b", t, flags=re.I))


def parse_special(text: str) -> dict:
    t = text or ""
    out: dict = {"mentions": mentions_special(t)}
    m = re.search(r"\$([\d.]+) per share special (?:cash )?dividend", t, flags=re.I)
    if m:
        out["special"] = float(m.group(1))
    m = re.search(r"\$([\d.]+) per share (?:regular )?quarterly (?:cash )?dividend", t, flags=re.I)
    if m:
        out["regular"] = float(m.group(1))
    m = re.search(r"special (?:cash )?dividend[^.]*?of record at the close of business on " + _DATE, t, flags=re.I) \
        or re.search(r"of record (?:at the close of business )?on " + _DATE, t, flags=re.I)
    if m:
        out["record"] = iso_date(m.group(1))
    m = re.search(r"(?:are|is|will be) payable on " + _DATE, t, flags=re.I) or re.search(r"payment date of " + _DATE, t, flags=re.I) \
        or re.search(r"to be paid on " + _DATE, t, flags=re.I)
    if m:
        out["payment"] = iso_date(m.group(1))
    m = re.search(r"ex-dividend date (?:is|of|will be|for [^.]*? (?:is|will be)) " + _DATE, t, flags=re.I)
    if m:
        out["ex"] = iso_date(m.group(1))
    return out


def sdiv_message(cfg: dict, f: dict, links: str, p: dict, close: tuple[str, float] | None, now: datetime) -> tuple[str, str, int]:
    sd = cfg["sdiv"]
    prior = float(sd["prior_special"])
    reg_ann = float(cfg["price"]["annual_regular_div"])
    if p.get("special") is None:
        title = f"{NAME} 특별배당 공시 나옴 | 원문 확인"
        state = (f"{f['date']} 8-K(Item {f['items']}, 접수번호 {f['acc']})에 특별배당 문구가 있지만 금액을 자동으로 못 읽었습니다(원문 확인). {links}.")
        todo = "원문 확인: 주당 특별배당, 기록일, 지급일, 배당락일, 같은 공시의 정기배당(기준 0.35달러)을 직접 확인."
    else:
        sp = p["special"]
        diff = sp - prior
        cmp_ = "작년과 같음" if abs(diff) < 1e-9 else f"작년 {prior:.2f}달러보다 {diff:+.2f}달러({(sp / prior - 1) * 100:+.1f}%)"
        title = f"{NAME} 특별배당 {sp:.2f}달러 공시 | " + ("작년과 같음, 일정 확인" if abs(diff) < 1e-9 else ("작년보다 많음, 일정 확인" if diff > 0 else "작년보다 적음, 일정 확인"))
        reg = p.get("regular")
        regtxt = (f"같은 공시 정기배당 {reg:.2f}달러" + (" (기준 0.35 그대로)." if abs(reg - 0.35) < 1e-9 else f" (기준 0.35에서 변경).")) if reg is not None \
            else "같은 공시에 정기배당 언급 없음."
        ex = p.get("ex") or "보도자료에 명시 없음(보통 기록일과 같은 날, 브로커에서 확인)"
        if close:
            cd, cc = close
            yl = (f"최근 종가 {cc:.2f}달러({cd}) 기준 특별배당 수익률 {sp / cc * 100:.2f}%, "
                  f"정기 연 {reg_ann:.2f} + 특별 {sp:.2f} = {reg_ann + sp:.2f}달러 기준 {(reg_ann + sp) / cc * 100:.2f}%.")
        else:
            yl = "수익률: 최근 종가 조회 안 됨."
        state = (f"{f['date']} 8-K(Item {f['items']}, 접수번호 {f['acc']}). 주당 특별배당 {sp:.2f}달러, {cmp_}. "
                 f"기록일 {p.get('record') or '못 읽음'}, 지급일 {p.get('payment') or '못 읽음'}, 배당락일 {ex}. {regtxt} {yl} {links}.")
        todo = "보유 중이면 기록일까지 보유해야 받습니다. 배당락일에 주가가 배당만큼 내려가는 것은 정상. 특별배당만 보고 비중을 늘리지 않기."
    lines = [
        header(now, "특별배당"),
        "이게 뭐 알림: 버클(BKE) 특별배당 공시(SEC 8-K, Item 8.01 또는 보도자료에 특별배당 문구가 있는 7.01)를 공시마다 1번 보내는 알림. " + cfg["source_note"],
        "지금 결과/상태: " + state,
        "지금 할 일: " + todo + " 작년 기준: " + sd["prior_note"],
        "나중에: 2027-01-31 까지 평일 09:00 에 다른 특별배당 공시가 있는지 계속 봅니다. 종가 40달러선 점검도 계속.",
        "익절/줄일 때: 특별배당 지급 뒤 주가가 배당락 이상으로 밀리고 실적도 약하면 비중 축소 검토.",
        "하지 말 것: 배당만 노리고 기록일 직전 추격, 신용/미수, 배당락 하락을 악재로 오해해 급히 정리, 이 알림만 보고 주문. 투자 권유 아님.",
    ]
    return title, "\n".join(lines), int(sd["priority"])


def sdiv_missing_message(cfg: dict, now: datetime) -> tuple[str, str, int]:
    sd = cfg["sdiv"]
    title = f"{NAME} 특별배당 공시 없음(1/31까지) | 정기배당만 기준"
    lines = [
        header(now, "특별배당"),
        "이게 뭐 알림: 2026-12-01 ~ 2027-01-31 에 SEC 에 버클 특별배당 8-K 가 안 보여 1번 오는 안내. 특별배당 감시는 여기서 멈춥니다. " + cfg["source_note"],
        "지금 결과/상태: EDGAR(CIK 0000885245)에 그 기간 특별배당 문구가 있는 8-K(Item 8.01/7.01) 없음. 작년 기준: " + sd["prior_note"],
        "지금 할 일: 배당수익률은 정기배당(분기 0.35달러, 연 1.40달러)만으로 보세요. 버클 IR 보도자료에서 배당 공지를 한 번 직접 확인.",
        "나중에: 특별배당 자동 감시는 더 하지 않습니다. 종가 40달러선 점검은 2027-02-26 까지 평일 09:00 에 계속.",
        "익절/줄일 때: 해당 없음.",
        "하지 말 것: 특별배당을 기대한 추가 비중, 신용/미수. 투자 권유 아님.",
    ]
    return title, "\n".join(lines), int(sd["missing_priority"])


def sdiv_candidates(cfg: dict, filings: list[dict], seen: set[str]) -> list[dict]:
    sd = cfg["sdiv"]
    return [f for f in sorted(filings, key=lambda x: x["date"])
            if f["form"] == "8-K" and sd["window_from"] <= f["date"] <= sd["window_to"]
            and any(i in _items(f) for i in sd["items"]) and f["acc"] not in seen]


def run_sdiv(cfg: dict, st: dict, bars, now: datetime, dry: bool, fetch_sub, get) -> bool:
    sd = cfg["sdiv"]
    ss = st.setdefault("sdiv", {})
    if ss.get("done"):
        print(f"sdiv: done ({ss['done']})")
        return False
    if now < kst(sd["poll_start_kst"]):
        print("sdiv: before window")
        return False
    day = now.strftime("%Y-%m-%d")
    try:
        filings = recent_filings(fetch_sub())
    except Exception as exc:  # noqa: BLE001
        print(f"sdiv: EDGAR fetch failed: {exc}")
        if ss.get("fail_day") == day:
            return False
        send("ops", f"{NAME} EDGAR 조회 실패 | 다음 09:00 재시도",
             f"SEC EDGAR submissions 조회 실패(특별배당 감시): {type(exc).__name__} {exc}"[:300] + ". 다음 실행에서 다시 봅니다.", 3, ["warning"], dry)
        ss["fail_day"] = day
        return True
    changed = False
    seen = set(ss.get("seen") or [])
    close = (str(bars[-1][0]), bars[-1][1]) if bars else None
    for f in sdiv_candidates(cfg, filings, seen):
        _, index_url, main_url = filing_urls(cfg, f)
        ex_url, text, note = exhibit_text(cfg, f, get)
        if text is None:
            print(f"sdiv: {f['acc']} text fetch failed; retry next run")
            if ss.get("fail_day") != day:
                send("ops", f"{NAME} 8-K 본문 조회 실패 | 다음 09:00 재시도", f"{f['date']} 8-K {f['acc']} 본문 조회 실패. {note}"[:300], 3, ["warning"], dry)
                ss["fail_day"] = day
                changed = True
            continue
        p = parse_special(text)
        if not p["mentions"]:
            print(f"sdiv: {f['acc']} (Item {f['items']}) no special dividend text; recorded, not sent")
            ss.setdefault("skipped", []).append({"acc": f["acc"], "date": f["date"], "items": f["items"], "regular": p.get("regular")})
            seen.add(f["acc"])
            changed = True
            continue
        links = f"8-K 목록 {index_url}" + (f", 보도자료(Exhibit 99) {ex_url}" if ex_url else f", 8-K 본문 {main_url}")
        if note:
            links += f" ({note.strip()})"
        t, b, pr = sdiv_message(cfg, f, links, p, close, now)
        send("port", t, b, pr, ["moneybag"], dry)
        ss.setdefault("found", []).append({"acc": f["acc"], "date": f["date"], "items": f["items"], "special": p.get("special"),
                                           "regular": p.get("regular"), "record": p.get("record"), "payment": p.get("payment"),
                                           "sent_at": now.strftime("%Y-%m-%d %H:%M KST")})
        seen.add(f["acc"])
        changed = True
    ss["seen"] = sorted(seen)
    if now >= kst(sd["deadline_kst"]):
        if not ss.get("found"):
            t, b, pr = sdiv_missing_message(cfg, now)
            send("port", t, b, pr, ["hourglass"], dry)
            ss["done"] = "missing"
            ss["missing_sent"] = now.strftime("%Y-%m-%d %H:%M KST")
        else:
            ss["done"] = "found"
        changed = True
    return changed


def memo(fn):
    box: dict = {}

    def wrapped():
        if "v" not in box:
            try:
                box["v"] = (fn(), None)
            except Exception as exc:  # noqa: BLE001
                box["v"] = (None, exc)
        v, e = box["v"]
        if e is not None:
            raise e
        return v
    return wrapped


def run(cfg: dict, dry: bool, now: datetime, bars_fn=fetch_bars, fetch_sub=None, get=None) -> int:
    if now > ends_at(cfg):
        print(f"ended (ends_kst {cfg['ends_kst']}); silent")
        return 0
    st = load_state()
    get = get or (lambda u: edgar_get(cfg, u))
    fetch_sub = memo(fetch_sub or (lambda: json.loads(edgar_get(cfg, cfg["edgar"]["submissions"]))))
    changed = False
    try:
        bars = bars_fn(cfg, now.astimezone(NY))
    except Exception as exc:  # noqa: BLE001
        print(f"bars fetch failed: {exc}")
        bars = None
        day = now.strftime("%Y-%m-%d")
        if st.get("px_fail_day") != day:
            send("ops", f"{NAME} 시세 조회 실패 | 다음 09:00 재시도", f"BKE 일봉(Yahoo chart v8) 조회 실패: {exc}"[:300] + ". 다음 09:00 실행에서 다시 봅니다.", 3, ["warning"], dry)
            st["px_fail_day"] = day
            changed = True
    changed |= run_earn(cfg, st, bars, now, dry, fetch_sub, get)
    changed |= run_sdiv(cfg, st, bars, now, dry, fetch_sub, get)
    changed |= run_px(cfg, st, bars, now, dry)
    if changed:
        save_state(st, dry)
    else:
        print("nothing changed; silent")
    return 0


def main(argv: list[str]) -> int:
    global DRY
    mode = (argv[1] if len(argv) > 1 else "auto").lower()
    if mode == "selftest":
        return selftest()
    dry = mode != "auto"
    DRY = dry
    now = datetime.now(KST)
    print(f"{now.isoformat(timespec='seconds')} bke mode={mode}")
    if dry:
        print("DRY RUN (no ntfy, no state write)")
    return run(load_cfg(), dry, now)


# ---------- selftest ----------

FIX_Q3 = (
    "THE BUCKLE, INC. REPORTS THIRD QUARTER NET INCOME KEARNEY, NE -- (BUSINESS WIRE) -- The Buckle, Inc. (NYSE: BKE) announced today "
    "that net income for the fiscal quarter ended October 31, 2026 was $47.5 million, or $0.94 per share ($0.93 per share on a diluted basis). "
    "Net sales for the 13-week fiscal quarter ended October 31, 2026 increased 2.5 percent to $328.9 million from net sales of $320.8 million "
    "for the prior year 13-week fiscal quarter ended November 1, 2025. Comparable store net sales for the 13-week fiscal quarter ended "
    "October 31, 2026 increased 0.8 percent from comparable store net sales for the prior year 13-week period ended November 1, 2025. "
    "Comparable store net sales for the 39-week period ended October 31, 2026 increased 2.6 percent from comparable store net sales. "
    "Financial Tables to Follow SALES, Net of returns and allowances $ 328,900 $ 320,837 $ 937,451 $ 898,695 COST OF SALES 171,000 166,931 "
    "Gross profit 157,900 153,906 437,000 425,891 EARNINGS PER SHARE: Basic $ 0.94 $ 0.97 $ 2.74 $ 2.57 Diluted $ 0.93 $ 0.96 $ 2.72 $ 2.55 "
    "CURRENT ASSETS: Cash and cash equivalents $ 300,100 $ 249,461 $ 316,152 Short-term investments 22,000 24,698 23,993 Receivables 9,000 "
    "Inventory 170,200 139,504 165,779 Prepaid expenses LONG-TERM INVESTMENTS 36,000 32,400 31,157 OTHER ASSETS"
)
FIX_Q3_2025 = (  # trimmed verbatim from the real 2025-11-21 Exhibit 99.1 (accession 0000885245-25-000109)
    "THE BUCKLE, INC. REPORTS THIRD QUARTER NET INCOME KEARNEY, NE -- (BUSINESS WIRE) -- The Buckle, Inc. (NYSE: BKE) announced today that "
    "net income for the fiscal quarter ended November 1, 2025 was $48.7 million, or $0.97 per share ($0.96 per share on a diluted basis). "
    "Net sales for the 13-week fiscal quarter ended November 1, 2025 increased 9.3 percent to $320.8 million from net sales of $293.6 million "
    "for the prior year 13-week fiscal quarter ended November 2, 2024. Comparable store net sales for the 13-week fiscal quarter ended "
    "November 1, 2025 increased 8.3 percent from comparable store net sales for the prior year 13-week period ended November 2, 2024. "
    "SALES, Net of returns and allowances $ 320,837 $ 293,618 $ 898,695 $ 838,490 COST OF SALES (Including buying, distribution, and occupancy "
    "costs) 166,931 153,547 472,804 445,188 Gross profit 153,906 140,071 425,891 393,302 "
    "CURRENT ASSETS: Cash and cash equivalents $ 316,152 $ 266,929 $ 301,958 Short-term investments 23,993 23,801 23,482 Receivables 8,222 "
    "6,758 7,901 Inventory 165,779 120,789 149,351 Prepaid expenses and other assets 24,977 LONG-TERM INVESTMENTS 31,157 28,116 27,261 OTHER"
)
FIX_SDIV_2025 = (  # trimmed verbatim from the real 2025-12-09 Exhibit 99.1 (accession 0000885245-25-000111, Items 8.01, 9.01)
    "THE BUCKLE, INC. ANNOUNCES A $3.00 PER SHARE SPECIAL CASH DIVIDEND, A $0.35 PER SHARE QUARTERLY DIVIDEND, AND THE APPOINTMENT OF "
    "ASHLEY L. ANDREAS AS VICE PRESIDENT OF HUMAN RESOURCES KEARNEY, NE -- (BUSINESS WIRE) -- The Buckle, Inc. (NYSE: BKE) announced that "
    "at its quarterly meeting of the Board of Directors, held on December 8, 2025, the Board authorized a $3.00 per share special cash "
    "dividend to be paid to shareholders of record at the close of business on January 15, 2026. The Board also authorized a $0.35 per "
    "share quarterly dividend to be paid to shareholders of record at the close of business on January 15, 2026. Both the $3.00 per share "
    "special cash dividend and the $0.35 per share quarterly dividend ($3.35 per share in total) are payable on January 29, 2026 and will "
    "be paid together. About Buckle Buckle is a specialty retailer focused on delivering exceptional service and style."
)
FIX_REG_ONLY = (  # trimmed verbatim from the real 2026-09-15 Exhibit 99.1 (Items 8.01, 9.01): regular dividend only
    "THE BUCKLE, INC. REPORTS QUARTERLY DIVIDEND KEARNEY, NE -- (BUSINESS WIRE) -- The Buckle, Inc. (NYSE: BKE) announced that at its "
    "quarterly meeting of the Board of Directors, held on September 14, 2026, the Board authorized a $0.35 per share quarterly dividend to "
    "be paid to shareholders of record at the close of business on October 15, 2026, with a payment date of October 29, 2026. About Buckle "
    "Buckle is a specialty retailer focused on delivering exceptional service and style through unforgettable experiences."
)
FIX_DIV = ("THE BUCKLE, INC. ANNOUNCES A $3.00 PER SHARE SPECIAL CASH DIVIDEND ... the Board authorized a $3.00 per share special cash "
           "dividend to be paid ... The Board also authorized a $0.35 per share quarterly dividend to be paid")


def selftest() -> int:
    import io
    import tempfile
    from contextlib import redirect_stdout
    from unittest import mock

    global STATE, DRY
    cfg = load_cfg()
    fails = 0
    count = 0

    def check(label, got, want):
        nonlocal fails, count
        count += 1
        ok = got == want
        fails += not ok
        print(("ok  " if ok else "FAIL"), label, "" if ok else f"got {got!r} want {want!r}")

    def rules(name, t, b):
        check(f"{name}: no middle dot / hangul dot / em-dash", any(c in t + b for c in BANNED_CHARS), False)
        check(f"{name}: title [BKE], no 매수/신호", t.startswith("[BKE] ") and not any(w in t for w in TITLE_BANNED), True)
        check(f"{name}: six-part order", [x.split(":")[0] for x in b.split("\n")[1:7]],
              ["이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"])
        check(f"{name}: disclaimer", "참고용, 투자 권유 아님" in b and b.rstrip().endswith("투자 권유 아님."), True)

    # --- parsing ---
    p = parse_release(FIX_Q3)
    check("parse sales/yoy/prior", (p.get("sales"), p.get("sales_yoy"), p.get("sales_prior")), (328.9, 2.5, 320.8))
    check("parse comps q3 / ytd", (p.get("comps"), p.get("comps_ytd")), (0.8, 2.6))
    check("parse diluted EPS", p.get("eps"), 0.93)
    check("parse gross margin", (p.get("gm"), p.get("gm_prior")), (48.01, 47.97))
    check("parse inventory", (p.get("inventory"), p.get("inventory_prior")), (170.2, 165.779))
    check("parse cash+investments", (round(p.get("cash_inv"), 3), round(p.get("cash_inv_prior"), 3)), (358.1, 371.302))
    check("parse third quarter flag", p.get("third_quarter"), True)
    r = parse_release(FIX_Q3_2025)
    check("real 2025 Q3 release: sales, comps, eps", (r.get("sales"), r.get("sales_yoy"), r.get("comps"), r.get("eps")), (320.8, 9.3, 8.3, 0.96))
    check("real 2025 Q3 release: gm, inventory, cash+inv", (r.get("gm"), r.get("inventory"), round(r.get("cash_inv"), 3)), (47.97, 165.779, 371.302))
    neg = parse_release(FIX_Q3.replace("increased 0.8 percent", "decreased 1.2 percent").replace("increased 2.5 percent", "decreased 0.4 percent"))
    check("parse decreased -> negative", (neg.get("comps"), neg.get("sales_yoy")), (-1.2, -0.4))
    check("parse dividend 8-K", parse_dividend(FIX_DIV), {"special_div": 3.0, "regular_div": 0.35})
    bad = parse_release("<p>Item 2.02 see exhibit</p>")
    check("parse fail -> not ok", parsed_ok(bad), False)

    # --- price core ---
    check("seed 47.31 armed, no fire", px_step(cfg, None, 47.31), (True, False))
    check("seed 39.5 disarmed, no fire", px_step(cfg, None, 39.5), (False, False))
    check("close 40.00 does not fire", px_step(cfg, True, 40.0), (True, False))
    check("close 39.99 fires", px_step(cfg, True, 39.99), (False, True))
    check("disarmed 39.0 silent", px_step(cfg, False, 39.0), (False, False))
    check("disarmed 40.99 stays disarmed", px_step(cfg, False, 40.99), (False, False))
    check("rearm at 41.00", px_step(cfg, False, 41.0), (True, False))
    walk = [(39.9, True), (40.5, False), (39.5, False), (41.0, False), (39.8, True)]
    a = True
    for i, (c, want) in enumerate(walk):
        a, fired = px_step(cfg, a, c)
        check(f"walk{i} {c}", fired, want)

    # --- messages ---
    now = datetime(2026, 11, 21, 9, 0, tzinfo=KST)
    f8 = {"form": "8-K", "date": "2026-11-20", "acc": "0000885245-26-000050", "items": "2.02,9.01", "doc": "bke-20261120.htm"}
    info = {"index_url": "https://x/index.html", "main_url": "https://x/main.htm", "ex_url": "https://x/ex.htm", "note": ""}
    bars = [(date(2026, 11, 19), 46.0), (date(2026, 11, 20), 47.38)]
    msgs = {
        "earn_ok": earnings_message(cfg, f8, info, p, {}, bars, now),
        "earn_miss": earnings_message(cfg, f8, info, neg | {"eps": 0.85}, {"special_div": 3.0, "regular_div": 0.35, "src": "x"}, bars, now),
        "earn_fail": earnings_message(cfg, f8, {**info, "ex_url": None}, bad, {}, None, now),
        "missing": missing_message(cfg, now),
        "px": price_message(cfg, 39.99, "2026-11-24", 41.2, {}, now),
        "px_low": price_message(cfg, 39.0, "2026-11-24", 39.99, {"done": "found", "found_date": "2026-11-20"}, now),
    }
    for n, (t, b, pr) in msgs.items():
        rules(n, t, b)
    t, b, pr = msgs["earn_ok"]
    check("earn_ok p4 + title", (pr, t), (4, "[BKE] 3분기 실적 EPS 0.93달러, 예상 0.91 상회 | Hold 유지 점검"))
    check("earn_ok has all figures", all(x in b for x in ("매출 328.9백만달러", "+2.5%", "3분기 동일점 +0.8%", "9월 +0.5%", "35주 누계 +2.8%",
                                                          "매출총이익률 48.0% (전년 48.0%", "희석 EPS 0.93달러 (컨센서스 0.91, 전년 0.96",
                                                          "TTM EPS 4.34 -> 4.31달러", "재고 170.2백만달러", "현금과 투자자산 358.1백만달러",
                                                          "배당 발표: 아직 없음", "주가 반응: 발표일 2026-11-20 종가 47.38달러", "+3.0%", "Hold(보통)")), True)
    t, b, pr = msgs["earn_miss"]
    check("earn_miss verdict + dividend", ("하회 | Hold 약화 점검" in t, "특별배당 3.00달러" in b, "정기배당 0.35달러 (기준 0.35 그대로)" in b), (True, True, True))
    t, b, pr = msgs["earn_fail"]
    check("parse fail: 원문 확인 title + link + p4", (t, "원문 확인" in b, "https://x/index.html" in b, pr),
          ("[BKE] 3분기 실적 8-K 나옴 | 원문 확인", True, True, 4))
    check("missing p3 title", (msgs["missing"][2], msgs["missing"][0]), (3, "[BKE] 3분기 실적 8-K 미발견 | 발표일 직접 확인"))
    t, b, pr = msgs["px"]
    check("px p4 title", (pr, t), (4, "[BKE] 종가 39.99달러, 40달러 아래 | Hold 판단 재점검"))
    check("px body: 52w low, yield, buffer", ("52주 저점 39.44달러" in b, "배당수익률 3.50%" in b, "41.00달러 이상(완충 1달러)" in b), (True, True, True))
    check("px under 52w low title", msgs["px_low"][0], "[BKE] 종가 39.00달러, 40달러 아래 | 52주 저점도 이탈, 판단 재점검")
    check("clean() strips em-dash/dots", clean("a\u2014b\u00b7c\u318dd\u3186e"), "a-b c d e")

    # --- runs with a temp state ---
    def sub(extra=()):
        rows = [("8-K", "2026-09-15", "0000885245-26-000039", "8.01,9.01", "bke-20260915.htm"),
                ("8-K", "2026-08-21", "0000885245-26-000033", "2.02,9.01", "bke-20260821.htm")] + list(extra)
        rows = sorted(rows, key=lambda r: r[1], reverse=True)
        return {"filings": {"recent": {"form": [r[0] for r in rows], "filingDate": [r[1] for r in rows],
                                       "accessionNumber": [r[2] for r in rows], "items": [r[3] for r in rows],
                                       "primaryDocument": [r[4] for r in rows]}}}

    q3 = ("8-K", "2026-11-20", "0000885245-26-000050", "2.02,9.01", "bke-20261120.htm")
    pages = {"index.json": json.dumps({"directory": {"item": [{"name": "bke-20261120.htm"}, {"name": "bke20261031-8kexhibit.htm"}, {"name": "R1.htm"}]}}),
             "bke20261031-8kexhibit.htm": "<p>" + FIX_Q3 + "</p>", "bke-20261120.htm": "<p>Item 2.02</p>"}
    pages_bad = {**pages, "bke20261031-8kexhibit.htm": "<p>see attached</p>"}
    old = STATE
    sent: list[tuple[str, str, int]] = []
    try:
        with tempfile.TemporaryDirectory() as td:
            STATE = Path(td) / "bke.json"
            seed = {"px": {"initialized": True, "armed": True, "last_date": "2026-11-02", "last_close": 47.31}, "earn": {}}

            def reset(st=None):
                STATE.write_text(json.dumps(st or seed), encoding="utf-8")
                sent.clear()

            def go(ts, s=lambda: sub(), pg=pages, barz=None, dry=False):
                global DRY
                barz = barz if barz is not None else [(date(2026, 11, 2), 47.31)]
                DRY = dry
                with mock.patch(f"{__name__}.publish", side_effect=lambda topic, t, b, pr, **k: sent.append((topic, t, pr))), \
                        redirect_stdout(io.StringIO()):
                    run(cfg, dry, kst(ts), bars_fn=lambda c, n: barz, fetch_sub=s,
                        get=lambda u: pg[u.rsplit("/", 1)[1]])
                DRY = False

            S2 = lambda: sub([q3])  # noqa: E731
            reset()
            before = STATE.read_bytes()
            go("2026-11-21 09:00", S2, dry=True)
            go("2026-11-27 09:00", dry=True)
            go("2026-11-24 09:00", barz=[(date(2026, 11, 2), 47.31), (date(2026, 11, 23), 39.0)], dry=True)
            check("dry: state bytes unchanged", STATE.read_bytes() == before, True)
            check("dry: nothing published", sent, [])
            go("2026-11-19 09:00", S2)
            check("before 11/20 poll window: silent", sent, [])
            go("2026-11-20 09:00")
            check("8-K not found yet: silent", sent, [])
            go("2026-11-21 09:00", S2)
            go("2026-11-23 09:00", S2)
            check("8-K found: one p4 summary, dedupe on Monday", [(x[1].split(" | ")[0][:20], x[2]) for x in sent], [("[BKE] 3분기 실적 EPS 0.9", 4)])
            stj = json.loads(STATE.read_text())
            check("state earn done + accession", (stj["earn"]["done"], stj["earn"]["found_accession"], stj["earn"]["parsed"]), ("found", q3[2], True))
            go("2026-11-27 09:00")
            check("found -> no missing notice later", len(sent), 1)
            reset()
            go("2026-11-21 09:00", S2, pg=pages_bad)
            check("parse fail -> 원문 확인 send", [x[1] for x in sent], ["[BKE] 3분기 실적 8-K 나옴 | 원문 확인"])
            reset()
            go("2026-11-26 09:00")
            check("not found before deadline: silent", sent, [])
            go("2026-11-27 09:00")
            go("2026-11-30 09:00", S2)
            check("deadline: one p3 미발견, then that part stops", [(x[1], x[2]) for x in sent], [("[BKE] 3분기 실적 8-K 미발견 | 발표일 직접 확인", 3)])
            reset()
            boom = lambda: (_ for _ in ()).throw(OSError("edgar down"))  # noqa: E731
            go("2026-11-21 09:00", boom)
            go("2026-11-21 09:05", boom)
            check("EDGAR fail: one ops notice per day", [(x[0], x[1]) for x in sent], [("cwally-ops", "[BKE] EDGAR 조회 실패 | 다음 09:00 재시도")])
            # price runs
            reset({"px": {}, "earn": {}})
            go("2026-11-04 09:00", barz=[(date(2026, 11, 3), 47.31)])
            stj = json.loads(STATE.read_text())
            check("first run seeds armed, no send", (sent, stj["px"]["armed"], stj["px"]["last_close"]), ([], True, 47.31))
            go("2026-11-05 09:00", barz=[(date(2026, 11, 3), 47.31), (date(2026, 11, 4), 40.0)])
            check("close 40.00: no send", sent, [])
            go("2026-11-06 09:00", barz=[(date(2026, 11, 4), 40.0), (date(2026, 11, 5), 39.99)])
            go("2026-11-06 09:00", barz=[(date(2026, 11, 4), 40.0), (date(2026, 11, 5), 39.99)])
            check("close 39.99: one p4, dedupe by bar date", [(x[1], x[2]) for x in sent], [("[BKE] 종가 39.99달러, 40달러 아래 | Hold 판단 재점검", 4)])
            go("2026-11-09 09:00", barz=[(date(2026, 11, 5), 39.99), (date(2026, 11, 6), 40.8)])
            go("2026-11-10 09:00", barz=[(date(2026, 11, 6), 40.8), (date(2026, 11, 9), 39.5)])
            check("no rearm under 41: silent", len(sent), 1)
            go("2026-11-11 09:00", barz=[(date(2026, 11, 9), 39.5), (date(2026, 11, 10), 41.0)])
            go("2026-11-12 09:00", barz=[(date(2026, 11, 10), 41.0), (date(2026, 11, 11), 39.7)])
            check("rearm at 41 then fires again", len(sent), 2)
            sent.clear()
            go("2026-11-14 09:00", barz=[(date(2026, 11, 12), 41.5), (date(2026, 11, 13), 39.0)])
            go("2026-11-16 09:00", barz=[(date(2026, 11, 13), 39.0), (date(2026, 11, 14), 41.2), (date(2026, 11, 15), 38.0)])
            check("gap run: rearm + fire inside one run counts once", len(sent), 2)
            reset()
            go("2027-02-27 00:00", S2, barz=[(date(2027, 2, 26), 30.0)])
            check("after 2027-02-26 23:59: silent, state untouched", (sent, STATE.read_bytes() == json.dumps(seed).encode()), ([], True))
            go("2027-02-26 23:59", barz=[(date(2027, 2, 25), 39.0)])
            check("end minute still active", len(sent) >= 1, True)
    finally:
        STATE, DRY = old, False
    # --- special dividend (sdiv) ---
    ps = parse_special(FIX_SDIV_2025)
    check("real 2025-12-09: special/regular/record/payment", (ps.get("special"), ps.get("regular"), ps.get("record"), ps.get("payment"), ps["mentions"]),
          (3.0, 0.35, "2026-01-15", "2026-01-29", True))
    pr_ = parse_special(FIX_REG_ONLY)
    check("real 2026-09-15 regular only: no special ('specialty retailer' ignored)", (pr_["mentions"], pr_.get("special"), pr_.get("regular")), (False, None, 0.35))
    check("ex-date parsed when stated", parse_special("a $4.00 per share special cash dividend. The ex-dividend date is January 14, 2027.").get("ex"), "2027-01-14")
    fs = {"form": "8-K", "date": "2026-12-08", "acc": "0000885245-26-000060", "items": "8.01,9.01", "doc": "bke-20261208.htm"}
    m_ok = sdiv_message(cfg, fs, "8-K 목록 https://x/i.html", ps, ("2026-12-07", 47.31), now)
    m_fail = sdiv_message(cfg, fs, "8-K 목록 https://x/i.html", parse_special("The Board declared a special dividend, amount to follow."), None, now)
    m_miss = sdiv_missing_message(cfg, now)
    for n, (t, b, pr) in {"sdiv_ok": m_ok, "sdiv_fail": m_fail, "sdiv_missing": m_miss}.items():
        rules(n, t, b)
    t, b, pr = m_ok
    check("sdiv_ok p4 title", (pr, t), (4, "[BKE] 특별배당 3.00달러 공시 | 작년과 같음, 일정 확인"))
    check("sdiv_ok body figures", all(x in b for x in ("주당 특별배당 3.00달러, 작년과 같음", "기록일 2026-01-15", "지급일 2026-01-29", "배당락일 보도자료에 명시 없음",
                                                     "정기배당 0.35달러 (기준 0.35 그대로)", "특별배당 수익률 6.34%", "= 4.40달러 기준 9.30%", "https://x/i.html")), True)
    check("sdiv vs prior: more", sdiv_message(cfg, fs, "", {"special": 3.5, "mentions": True}, None, now)[0], "[BKE] 특별배당 3.50달러 공시 | 작년보다 많음, 일정 확인")
    check("sdiv parse fail -> 원문 확인 p4", (m_fail[0], "원문 확인" in m_fail[1], m_fail[2]), ("[BKE] 특별배당 공시 나옴 | 원문 확인", True, 4))
    check("sdiv missing p3 title", (m_miss[0], m_miss[2]), ("[BKE] 특별배당 공시 없음(1/31까지) | 정기배당만 기준", 3))

    def subd(rows):
        rows = sorted([("8-K", "2026-09-15", "0000885245-26-000039", "8.01,9.01", "bke-20260915.htm")] + list(rows), key=lambda r: r[1], reverse=True)
        return {"filings": {"recent": {"form": [r[0] for r in rows], "filingDate": [r[1] for r in rows],
                                       "accessionNumber": [r[2] for r in rows], "items": [r[3] for r in rows],
                                       "primaryDocument": [r[4] for r in rows]}}}

    def acc(n):
        return f"0000885245-26-0000{n}"
    texts = {"61": FIX_SDIV_2025, "62": FIX_REG_ONLY, "63": FIX_SDIV_2025.replace("$3.00", "$3.50"),
             "64": "<p>The Board declared a special dividend; the amount will be announced later.</p>", "65": FIX_SDIV_2025, "66": FIX_SDIV_2025,
             "39": FIX_REG_ONLY}

    def getd(u):
        folder, name = u.split("/")[-2], u.split("/")[-1]
        n = folder[-2:]
        if name == "index.json":
            return json.dumps({"directory": {"item": [{"name": f"bke-{n}.htm"}, {"name": f"bke{n}8-kdivex.htm"}, {"name": "R1.htm"}]}})
        return "<p>" + texts[n] + "</p>"
    r801 = ("8-K", "2026-12-08", acc(61), "8.01,9.01", "bke-61.htm")
    rreg = ("8-K", "2026-12-08", acc(62), "8.01,9.01", "bke-62.htm")
    r701 = ("8-K", "2026-12-09", acc(63), "7.01,9.01", "bke-63.htm")
    rbad = ("8-K", "2026-12-10", acc(64), "8.01,9.01", "bke-64.htm")
    rold = ("8-K", "2026-11-30", acc(65), "8.01,9.01", "bke-65.htm")
    rlate = ("8-K", "2027-02-01", acc(66), "8.01,9.01", "bke-66.htm")
    old = STATE
    sent2: list[tuple[str, str, int]] = []
    try:
        with tempfile.TemporaryDirectory() as td:
            STATE = Path(td) / "bke.json"
            seed2 = {"px": {"initialized": True, "armed": True, "last_date": "2026-12-07", "last_close": 47.31}, "earn": {"done": "found"}, "sdiv": {}}

            def reset2():
                STATE.write_text(json.dumps(seed2), encoding="utf-8")
                sent2.clear()

            def go2(ts, rows, dry=False):
                global DRY
                DRY = dry
                with mock.patch(f"{__name__}.publish", side_effect=lambda topic, t, b, pr, **k: sent2.append((topic, t, pr))), \
                        redirect_stdout(io.StringIO()):
                    run(cfg, dry, kst(ts), bars_fn=lambda c, n: [(date(2026, 12, 7), 47.31)], fetch_sub=lambda: subd(rows), get=getd)
                DRY = False
            reset2()
            before = STATE.read_bytes()
            go2("2026-12-09 09:00", [r801, r701, rbad], dry=True)
            go2("2027-02-01 09:00", [], dry=True)
            check("sdiv dry: state unchanged, nothing sent", (STATE.read_bytes() == before, sent2), (True, []))
            go2("2026-11-30 09:00", [r801])
            check("sdiv before 12/01 window: silent", sent2, [])
            reset2()
            go2("2026-12-09 09:00", [r801])
            go2("2026-12-10 09:00", [r801])
            check("8.01 special: one p4, dedupe by accession", [(x[1], x[2]) for x in sent2], [("[BKE] 특별배당 3.00달러 공시 | 작년과 같음, 일정 확인", 4)])
            reset2()
            go2("2026-12-09 09:00", [rreg])
            stj = json.loads(STATE.read_text())
            check("8.01 regular only: not sent, recorded as seen", (sent2, acc(62) in stj["sdiv"]["seen"], stj["sdiv"]["skipped"][0]["regular"]), ([], True, 0.35))
            reset2()
            go2("2026-12-10 09:00", [r701])
            check("7.01 with special text: sent", [x[1] for x in sent2], ["[BKE] 특별배당 3.50달러 공시 | 작년보다 많음, 일정 확인"])
            reset2()
            go2("2026-12-11 09:00", [rbad])
            check("special text, amount not parsed: 원문 확인", [x[1] for x in sent2], ["[BKE] 특별배당 공시 나옴 | 원문 확인"])
            reset2()
            go2("2027-01-29 09:00", [rold, rreg])
            check("outside window filings (11/30) ignored", sent2, [])
            go2("2027-02-01 09:00", [rold, rreg, rlate])
            go2("2027-02-02 09:00", [rold, rreg, rlate, r801])
            check("none by 1/31: one p3 notice, then stops (2/1 filing ignored)", [(x[1], x[2]) for x in sent2], [("[BKE] 특별배당 공시 없음(1/31까지) | 정기배당만 기준", 3)])
            reset2()
            go2("2026-12-09 09:00", [r801])
            go2("2027-02-01 09:00", [r801])
            check("found before deadline: no missing notice", len(sent2), 1)
            reset2()
            boom = lambda: (_ for _ in ()).throw(OSError("edgar down"))  # noqa: E731
            DRY = False
            with mock.patch(f"{__name__}.publish", side_effect=lambda topic, t, b, pr, **k: sent2.append((topic, t, pr))), redirect_stdout(io.StringIO()):
                run(cfg, False, kst("2026-12-09 09:00"), bars_fn=lambda c, n: [(date(2026, 12, 7), 47.31)], fetch_sub=boom, get=getd)
                run(cfg, False, kst("2026-12-09 09:10"), bars_fn=lambda c, n: [(date(2026, 12, 7), 47.31)], fetch_sub=boom, get=getd)
            check("sdiv EDGAR fail: one ops notice per day", [(x[0], x[1]) for x in sent2], [("cwally-ops", "[BKE] EDGAR 조회 실패 | 다음 09:00 재시도")])
            reset2()
            go2("2027-02-27 00:00", [r801])
            check("after ends_kst: sdiv silent", sent2, [])
    finally:
        STATE, DRY = old, False
    print("\n=== sample special dividend ===")
    print(m_ok[2], m_ok[0])
    print(m_ok[1])
    print("\n=== sample summary ===")
    print(msgs["earn_ok"][2], msgs["earn_ok"][0])
    print(msgs["earn_ok"][1])
    print("\n=== sample price ===")
    print(msgs["px"][2], msgs["px"][0])
    print(msgs["px"][1])
    print(f"selftest {count - fails}/{count}", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
