#!/usr/bin/env python3
"""[ASML] price-zone + weekly check + Q3 2026 earnings verdict.

Modes
  slot          morning bundle (weekdays 09:00 KST). Previous Amsterdam close (ASML.AS, EUR) ->
                zone. Sends only when the zone changes into an action zone, plus one weekly
                check per ISO week. Data failure -> one ops notice per day.
  auto | earn   one-shot (configs/asml.json oneshot_kst). Reads the ASML 6-K press release on
                SEC EDGAR and grades it against the 2026-10-07 analysis conditions. Silent until
                the 6-K exists; at the last occurrence a missing release goes to ops.
  dry-slot | dry-earn   same logic, prints only (no ntfy, no state write).
  selftest      offline parser check on a built-in sample (no network).
  force-slot    send the current status even without a zone change (manual use).

Exit 0 = normal (sent or silent). An uncaught error exits 1 (workflow ops notice).
State is written only after a successful publish, so a failed send retries next run.
"""
from __future__ import annotations

import html
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "asml.json"
STATE_PATH = ROOT / "state" / "asml.json"
BROWSER_UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}
ACTION_BANDS = {"ZONE1", "ZONE2", "BEAR", "RICH", "BULL"}
NEVER = (
    "하지 말 것: 한 번에 전액 매수, 신용/미수, 급등일 추격, "
    "조회 실패나 가격만 보고 자동 매수하기."
)


# ---------- io ----------
def load_cfg() -> dict:
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


def load_state() -> dict:
    base = {
        "updated_at": None,
        "price_eur": None,
        "price_date": None,
        "band": None,
        "weekly_sent": None,
        "fail_sent_date": None,
        "earn_final": False,
        "earn_verdict": None,
        "earn_summary": None,
    }
    try:
        base.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return base


def save_state(st: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def get(url: str, headers: dict, timeout: int = 25) -> bytes:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def emit(cfg: dict, title: str, lines: list[str], pri: int, dry: bool, tags: list[str] | None = None) -> None:
    msg = "\n".join(lines)
    print(title)
    print(msg)
    if dry:
        print("dry-run; no ntfy")
        return
    publish(
        resolve_topic("port", cfg.get("ntfy_topic")),
        title,
        msg,
        pri,
        tags or ["chart_with_upwards_trend"],
        resolve_server(cfg.get("ntfy_server")),
    )


# ---------- price ----------
def yahoo_quote(ticker: str) -> dict | None:
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(ticker)
        + "?interval=1d&range=10d"
    )
    try:
        data = json.loads(get(url, BROWSER_UA).decode())
        res = data["chart"]["result"][0]
        meta = res.get("meta", {})
        ts = res.get("timestamp") or []
        closes = (res.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
        pairs = [(t, c) for t, c in zip(ts, closes) if c is not None]
        price = meta.get("regularMarketPrice")
        mtime = meta.get("regularMarketTime")
        if price is None and pairs:
            mtime, price = pairs[-1]
        if price is None or mtime is None:
            return None
        prev = None
        mday = datetime.fromtimestamp(mtime, KST).date()
        older = [c for t, c in pairs if datetime.fromtimestamp(t, KST).date() < mday]
        if older:
            prev = older[-1]
        return {
            "price": float(price),
            "date": datetime.fromtimestamp(mtime, timezone.utc).strftime("%Y-%m-%d"),
            "prev": float(prev) if prev is not None else None,
        }
    except Exception as exc:  # network/shape -> data failure, never a signal
        print(f"yahoo fail {ticker}: {exc}", file=sys.stderr)
        return None


def band_for(price: float | None, lv: dict) -> str:
    if price is None:
        return "DATA_FAIL"
    if price >= lv["bull"]:
        return "BULL"
    if price >= lv["fair"]:
        return "RICH"
    if price <= lv["bear"]:
        return "BEAR"
    if price <= lv["zone_low"]:
        return "ZONE2"
    if price <= lv["zone_high"]:
        return "ZONE1"
    return "HOLD"


def eur(v: float | None) -> str:
    return f"€{v:,.1f}" if v is not None else "없음"


def pct(a: float | None, b: float | None) -> str:
    if a is None or not b:
        return ""
    return f" ({(a / b - 1) * 100:+.1f}%)"


# ---------- slot copy ----------
def slot_copy(cfg: dict, st: dict, band: str, q: dict, usd: dict | None, weekly: bool, stamp: str):
    lv = cfg["levels_eur"]
    eps27 = cfg["eps_eur"]["y2027e"]
    price = q["price"]
    per = price / eps27
    tr = cfg["tranche_pct"]
    verdict = st.get("earn_verdict")
    down = verdict == "DOWN"
    usd_txt = f", 미국 ${usd['price']:,.2f}" if usd else ""
    status = (
        f"지금 결과/상태: 암스테르담 {q['date']} 종가 {eus(price)}{pct(price, q.get('prev'))}{usd_txt}. "
        f"2027 예상 PER {per:.1f}배 (EPS €{eps27}, {cfg['eps_eur']['as_of']} 기준). "
        f"선호 구간 €{lv['zone_low']:,}~{lv['zone_high']:,}, 적정가 참고 €{lv['fair']:,}, 강세 €{lv['bull']:,}, 약세 €{lv['bear']:,}."
    )
    if verdict:
        status += f" 최근 실적 판정: {st.get('earn_summary') or verdict}."
    if band == "ZONE1":
        title = f"[ASML] €{lv['zone_high']:,} 이하 | 1차 {tr[0]}%만" if not down else "[ASML] 선호 구간 | 실적 하향이라 보류"
        now_do = (
            f"예정 총매수액의 {tr[0]}%만 분할 매수하세요. 2027 EPS 전망이 내려갔으면 보류하세요."
            if not down
            else "실적 판정이 하향이라 이 가격도 싸다고 보지 마세요. 진입 구간을 다시 계산할 때까지 보류하세요."
        )
        pri = 4
    elif band == "ZONE2":
        title = f"[ASML] €{lv['zone_low']:,} 이하 | 2차 {tr[1]}%만" if not down else "[ASML] €1,400 이하 | 실적 하향이라 보류"
        now_do = (
            f"하락 이유(수출규제, 고객 투자 축소)를 먼저 확인하고, 이유가 일시적이면 {tr[1]}%만 추가하세요."
            if not down
            else "실적 판정이 하향입니다. 추가 매수하지 말고 전망 재계산을 기다리세요."
        )
        pri = 4
    elif band == "BEAR":
        title = f"[ASML] 약세 시나리오 €{lv['bear']:,} | 자동 추가 금지"
        now_do = (
            "가격만 보고 추가하지 마세요. 2027 EPS 전망과 중국/수출규제 뉴스를 먼저 확인하고, "
            f"전망이 유지될 때만 남은 {tr[2]}%를 쓰세요."
        )
        pri = 5
    elif band == "RICH":
        title = f"[ASML] €{lv['fair']:,} 이상 | 신규 보류, 비중 점검"
        now_do = "새로 사지 마세요. 보유 비중이 계획보다 크면 초과분만 줄이는 것을 검토하세요."
        pri = 4
    elif band == "BULL":
        title = f"[ASML] 강세 시나리오 €{lv['bull']:,} 도달 | 일부 차익 검토"
        now_do = "강세 시나리오를 미리 반영한 가격입니다. 보유분 일부 차익 실현을 검토하세요."
        pri = 5
    else:
        title = "[ASML] 주간 점검 | 관망, 분할 원칙 유지"
        now_do = f"관망하세요. 신규는 €{lv['zone_high']:,} 이하에서 {tr[0]}%씩 나눠 사는 원칙만 지키세요. 보유분은 유지."
        pri = 3
    if weekly and band != "HOLD":
        title = title.replace("[ASML]", "[ASML] 주간", 1)
    later = (
        "나중에: 3분기 실적은 2026-10-14 14:00~15:00 KST 발표, 판정 알림은 SEC 사본 게시 후(보통 19시대) 옵니다. "
        "2027-06-10 자본시장의 날에 장기 목표가 바뀌면 기준값을 다시 잡아야 합니다."
    )
    trim = (
        f"익절/줄일 때: €{lv['fair']:,} 이상은 신규 중단과 비중 점검, €{lv['bull']:,} 이상은 일부 차익. "
        "2026 매출 가이던스(430억~450억 유로)가 내려가면 가격과 무관하게 비중을 다시 보세요."
    )
    lines = [
        f"{stamp} | ASML | 암스테르담 유로 기준",
        f"이게 뭐 알림: {cfg['explain']}",
        status,
        f"지금 할 일: {now_do}",
        later,
        trim,
        NEVER,
    ]
    return title, lines, pri


def eus(v: float) -> str:  # keep one formatter name for the status line
    return eur(v)


def run_slot(cfg: dict, mode: str) -> int:
    dry = mode.startswith("dry")
    force = mode == "force-slot"
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    st = load_state()
    years = cfg.get("year_guard") or [now.year]
    if not (dry or force) and (now.year not in years or today > cfg.get("valid_until", "9999-12-31")):
        print("asml outside window; silent")
        return 0
    q = yahoo_quote(cfg["ticker_eur"])
    band = band_for(q["price"] if q else None, cfg["levels_eur"])
    print(f"quote={q} band={band} prev_band={st.get('band')}")
    if band == "DATA_FAIL":
        if st.get("fail_sent_date") == today and not dry:
            print("data fail already notified today; silent")
            return 0
        emit(
            cfg,
            "[ASML] 시세 조회 실패 | 직접 확인",
            [
                f"{stamp} | ASML",
                "이게 뭐 알림: ASML 가격 구간 알림의 데이터 조회가 실패했습니다.",
                "지금 결과/상태: 암스테르담(ASML.AS) 종가를 받지 못했습니다. 실패를 정상으로 보지 마세요.",
                "지금 할 일: 증권앱에서 ASML 종가를 직접 확인하세요.",
                "나중에: 다음 평일 09:00 KST에 다시 조회합니다.",
                "익절/줄일 때: 이번 알림은 매매 신호가 아닙니다.",
                NEVER,
            ],
            4,
            dry,
            ["warning"],
        )
        if not dry:
            st["fail_sent_date"] = today
            save_state(st)
        return 0

    usd = yahoo_quote(cfg["ticker_usd"])
    iso = now.isocalendar()
    week_key = f"{iso[0]}-W{iso[1]:02d}"
    zone_change = band in ACTION_BANDS and band != st.get("band")
    weekly = st.get("weekly_sent") != week_key
    send = zone_change or weekly or force
    st_new = dict(st)
    st_new.update(
        {
            "updated_at": stamp,
            "price_eur": q["price"],
            "price_date": q["date"],
            "band": band,
            "fail_sent_date": None,
        }
    )
    if not send:
        print("asml no zone change, weekly already sent; silent")
        if not dry:
            save_state(st_new)
        return 0
    title, lines, pri = slot_copy(cfg, st, band, q, usd, weekly and not force, stamp)
    emit(cfg, title, lines, pri, dry)
    if not dry:
        if weekly:
            st_new["weekly_sent"] = week_key
        save_state(st_new)
    return 0


# ---------- earnings ----------
def textify(raw: str) -> str:
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t).replace("\xa0", " ").replace("\u2019", "'")
    t = re.sub(r"\s+", " ", t)
    # the 6-K html splits words across spans ("AS ML"); glue the company name back
    return re.sub(r"A\s?S\s?M\s?L", "ASML", t)


def num(s: str | None) -> float | None:
    if s is None:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def parse_release(t: str, q: str, nq: str, year: int) -> dict:
    out: dict = {}
    m = re.search(rf"{q} total net sales of €\s?([\d.,]+) billion, gross margin of ([\d.]+)\s?%(?:, net income of €\s?([\d.,]+) billion)?", t)
    if m:
        out["q_sales"], out["q_gm"], out["q_ni"] = num(m.group(1)), num(m.group(2)), num(m.group(3))
    # precise table value (millions): "Total net sales <prev> <this>"
    m = re.search(r"Total net sales ([\d,]{4,}) \.?\s?([\d,]{4,})", t)
    if m:
        out["q_sales_m"] = num(m.group(2))
        out["q_sales"] = round(out["q_sales_m"] / 1000, 3)
    m = re.search(r"Gross margin \(%\) ([\d.]+) ([\d.]+)", t)
    if m:
        out["q_gm"] = num(m.group(2))
    m = re.search(r"EPS \(basic; in euros\) ([\d.]+) ([\d.]+)", t)
    if m:
        out["eps"] = num(m.group(2))
    m = re.search(
        rf"expects? {nq} {year} total net sales (?:to be )?between €\s?([\d.]+) billion and €\s?([\d.]+) billion,? and a gross margin between ([\d.]+)\s?% and ([\d.]+)\s?%",
        t,
    )
    if m:
        out["nq_low"], out["nq_high"], out["nq_gm_low"], out["nq_gm_high"] = (num(m.group(i)) for i in range(1, 5))
    m = re.search(rf"expects? {year} total net sales (?:to be )?between €\s?([\d.]+) billion and €\s?([\d.]+) billion", t)
    if m:
        out["fy_low"], out["fy_high"] = num(m.group(1)), num(m.group(2))
    else:
        m = re.search(rf"expects? {year} total net sales (?:to be |of )?(?:around|approximately|about) €\s?([\d.]+) billion", t)
        if m:
            out["fy_low"] = out["fy_high"] = num(m.group(1))
    m = re.search(r"[^.]*\b" + str(year + 1) + r"\b[^.]*\.", t)
    if m:
        s = m.group(0).strip()
        out["next_year_note"] = s[:220] + ("..." if len(s) > 220 else "")
    m = re.search(r"Net bookings[^0-9]{0,40}([\d,]{3,}) ([\d,]{3,})", t)
    if m:
        out["bookings_m"] = num(m.group(2))
    return out


def grade(p: dict, e: dict) -> dict:
    flags = []
    h1 = e["h1_sales_bn"]
    if p.get("fy_low") is not None:
        fy_mid = (p["fy_low"] + p["fy_high"]) / 2
        fy_src = f"회사 가이던스 €{p['fy_low']:g}~{p['fy_high']:g}십억"
    elif p.get("q_sales") is not None and p.get("nq_low") is not None:
        fy_mid = h1 + p["q_sales"] + (p["nq_low"] + p["nq_high"]) / 2
        fy_src = f"상반기+3분기 실적+4분기 가이던스 중간값으로 계산 €{fy_mid:.1f}십억"
    else:
        return {"verdict": "UNKNOWN", "flags": ["연간 전망 숫자 미확인"], "fy_mid": None, "fy_src": ""}
    if p.get("q_sales") is not None and p["q_sales"] < e["q_guide_low"]:
        flags.append(f"3분기 매출 €{p['q_sales']:g}십억, 가이던스 하단 €{e['q_guide_low']:g}십억 미달")
    if p.get("q_gm") is not None and p["q_gm"] < e["q_gm_low"]:
        flags.append(f"3분기 매출총이익률 {p['q_gm']:g}%, 가이던스 하단 {e['q_gm_low']:g}% 미달")
    if fy_mid < e["fy_guide_low"]:
        verdict = "DOWN"
    elif fy_mid > e["fy_guide_high"]:
        verdict = "UP"
    else:
        verdict = "KEEP"
    if verdict == "UP" and flags:
        verdict = "KEEP"  # a raise with a miss is not an upgrade
    return {"verdict": verdict, "flags": flags, "fy_mid": fy_mid, "fy_src": fy_src}


def sec_release(cfg: dict) -> tuple[str | None, str | None]:
    e = cfg["earnings"]
    ua = {"User-Agent": cfg["sec_user_agent"], "Accept-Encoding": "identity"}
    sub = json.loads(get(f"https://data.sec.gov/submissions/CIK{e['sec_cik']}.json", ua).decode())
    r = sub["filings"]["recent"]
    cik = str(int(e["sec_cik"]))
    for i, form in enumerate(r["form"]):
        if form != "6-K" or r["filingDate"][i] < e["window_start"]:
            continue
        acc = r["accessionNumber"][i].replace("-", "")
        base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/"
        idx = json.loads(get(base + "index.json", ua).decode())
        names = [it["name"] for it in idx["directory"]["item"]]
        pr = [n for n in names if n.lower().startswith("pressrelease") and n.lower().endswith(".htm")]
        if not pr:
            continue
        raw = get(base + pr[0], ua).decode("utf-8", "ignore")
        txt = textify(raw)
        if re.search(rf"{e['quarter']} total net sales", txt):
            return txt, base + pr[0]
    return None, None


VERDICT_KO = {"UP": "상향", "KEEP": "유지", "DOWN": "하향", "UNKNOWN": "판정 불가"}


def earn_copy(cfg: dict, p: dict, g: dict, url: str, q: dict | None, stamp: str):
    e = cfg["earnings"]
    lv = cfg["levels_eur"]
    v = g["verdict"]
    qs = f"€{p['q_sales']:g}십억" if p.get("q_sales") is not None else "미확인"
    gm = f"{p['q_gm']:g}%" if p.get("q_gm") is not None else "미확인"
    nq = (
        f"€{p['nq_low']:g}~{p['nq_high']:g}십억, 이익률 {p['nq_gm_low']:g}~{p['nq_gm_high']:g}%"
        if p.get("nq_low") is not None
        else "미확인"
    )
    px = f" 주가 {eur(q['price'])}{pct(q['price'], q.get('prev'))} (암스테르담 {q['date']})." if q else ""
    status = (
        f"지금 결과/상태: 3분기 매출 {qs}(가이던스 €{e['q_guide_low']:g}~{e['q_guide_high']:g}십억), "
        f"매출총이익률 {gm}(가이던스 {e['q_gm_low']:g}~{e['q_gm_high']:g}%), EPS €{p.get('eps', '미확인')}. "
        f"4분기 가이던스 {nq}. 2026 연간: {g['fy_src']} (기존 €{e['fy_guide_low']:g}~{e['fy_guide_high']:g}십억).{px}"
    )
    if g["flags"]:
        status += " 경고: " + "; ".join(g["flags"]) + "."
    if v == "UP":
        title = "[ASML] 실적 판정 상향 | 보유 유지, 추격 금지"
        now_do = (
            "기존 보유는 유지하세요. 2027 이익 전망이 오를 가능성이 큽니다. "
            f"다만 신규는 여전히 €{lv['zone_high']:,} 이하 분할 원칙을 지키고, 컨센서스 갱신(1~2주) 뒤 진입 구간을 다시 계산하세요."
        )
        pri = 4
    elif v == "KEEP":
        title = "[ASML] 실적 판정 유지 | 기존 계획 그대로"
        now_do = f"보유는 유지, 신규는 €{lv['zone_low']:,}~{lv['zone_high']:,}에서 나눠 사는 기존 계획을 그대로 쓰세요."
        pri = 4
    else:
        title = "[ASML] 실적 판정 하향 | 신규 매수 보류"
        now_do = (
            "신규 매수를 멈추세요. 진입 구간을 다시 계산하기 전까지 가격 알림을 매수 신호로 쓰지 마세요. "
            "보유 비중이 계획보다 크면 줄이는 것을 검토하세요."
        )
        pri = 5
    later_bits = ["나중에: 원문에서 2027 수요, 중국 비중, 수출규제 언급을 직접 읽으세요."]
    if p.get("next_year_note"):
        later_bits.append(f"원문 2027 언급: \"{p['next_year_note']}\"")
    later_bits.append(f"원문: {url}")
    lines = [
        f"{stamp} | ASML | 3분기 실적 판정({VERDICT_KO[v]})",
        "이게 뭐 알림: ASML 3분기 실적을 2026-10-07 분석의 판단 변경 조건(연간 매출 가이던스 430억~450억 유로 유지 여부, 3분기 가이던스 달성)으로 채점한 일회 알림입니다.",
        status,
        f"지금 할 일: {now_do}",
        " ".join(later_bits),
        f"익절/줄일 때: €{lv['fair']:,} 이상이면 신규 중단과 비중 점검, €{lv['bull']:,} 이상이면 일부 차익. 하향 판정이면 가격과 무관하게 비중 재점검.",
        NEVER + " 실적 당일 변동만 보고 판단 바꾸기.",
    ]
    return title, lines, pri


def run_earn(cfg: dict, mode: str) -> int:
    dry = mode.startswith("dry")
    e = cfg["earnings"]
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    st = load_state()
    if not dry:
        if now.year != e["event_year"] or not (e["window_start"] <= today <= e["window_end"]):
            print("asml earnings outside window/year; silent")
            return 0
        if st.get("earn_final"):
            print("asml earnings already graded; silent")
            return 0
    is_final = now.strftime("%Y-%m-%d %H:%M") >= e["final_kst"]
    try:
        txt, url = sec_release(cfg)
    except Exception as exc:
        print(f"sec fail: {exc}", file=sys.stderr)
        txt, url = None, None
        if not is_final:
            print("sec error before final occurrence; retry at next one-shot")
            return 0
    if txt is None:
        if not is_final:
            print("ASML Q3 6-K not on SEC yet; silent until next one-shot")
            return 0
        emit(
            cfg,
            "[ASML] 3분기 실적 원문 미게시 | 직접 확인",
            [
                f"{stamp} | ASML",
                "이게 뭐 알림: ASML 3분기 실적 판정 알림입니다.",
                "지금 결과/상태: SEC에 3분기 보도자료 사본(6-K)을 찾지 못했습니다. 실적이 없다는 뜻이 아닙니다.",
                "지금 할 일: investor.asml.com에서 3분기 보도자료를 직접 열어 2026 연간 매출 전망(기존 430억~450억 유로)을 확인하세요.",
                "나중에: 자동 재시도는 없습니다.",
                "익절/줄일 때: 이번 알림은 매매 신호가 아닙니다.",
                NEVER,
            ],
            4,
            dry,
            ["warning"],
        )
        if not dry:
            st["earn_final"] = True
            st["earn_verdict"] = None
            save_state(st)
        return 0
    p = parse_release(txt, e["quarter"], e["next_quarter"], e["event_year"])
    g = grade(p, e)
    print(f"parsed={p} grade={g}")
    if g["verdict"] == "UNKNOWN" or p.get("q_sales") is None:
        emit(
            cfg,
            "[ASML] 3분기 실적 숫자 미확인 | 원문 확인",
            [
                f"{stamp} | ASML",
                "이게 뭐 알림: ASML 3분기 실적 판정 알림입니다.",
                "지금 결과/상태: 보도자료는 찾았지만 매출이나 연간 전망 숫자를 읽지 못했습니다.",
                f"지금 할 일: 원문을 직접 확인하세요: {url}",
                "나중에: 문구가 바뀌었으면 파서를 고쳐야 합니다.",
                "익절/줄일 때: 이번 알림은 매매 신호가 아닙니다.",
                NEVER,
            ],
            4,
            dry,
            ["warning"],
        )
        if not dry:
            st["earn_final"] = True
            save_state(st)
        return 0
    qq = yahoo_quote(cfg["ticker_eur"])
    title, lines, pri = earn_copy(cfg, p, g, url, qq, stamp)
    emit(cfg, title, lines, pri, dry)
    if not dry:
        st["earn_final"] = True
        st["earn_verdict"] = g["verdict"]
        st["earn_summary"] = f"{e['quarter']} {VERDICT_KO[g['verdict']]} ({today})"
        save_state(st)
    return 0


# ---------- selftest ----------
SAMPLE = (
    "ASML reports €11.6 billion total net sales and €3.9 billion net income in Q3 2026 "
    "• Q3 total net sales of €11.6 billion, gross margin of 56.1%, net income of €3.9 billion "
    "• ASML expects Q4 2026 total net sales between €13.5 billion and €14.5 billion, and a gross margin between 55% and 57% "
    "• ASML now expects 2026 total net sales to be between €44.5 billion and €45.5 billion, with a gross margin between 55% and 56% "
    "(Figures in millions of euros unless otherwise indicated) Q2 2026 Q3 2026 Total net sales 9,326 11,612 "
    "Gross margin (%) 54.0 56.1 EPS (basic; in euros) 7.59 10.12 "
    "We see 2027 as another growth year, with customer commitments covering our added capacity."
)


def selftest() -> int:
    cfg = load_cfg()
    e = cfg["earnings"]
    t = textify("<p>AS</p>ML" + SAMPLE)
    p = parse_release(t, "Q3", "Q4", 2026)
    g = grade(p, e)
    assert p["q_sales"] == 11.612 and p["q_gm"] == 56.1 and p["eps"] == 10.12, p
    assert (p["nq_low"], p["nq_high"]) == (13.5, 14.5), p
    assert (p["fy_low"], p["fy_high"]) == (44.5, 45.5) and g["verdict"] == "KEEP", g
    p2 = dict(p, fy_low=45.5, fy_high=46.5)
    assert grade(p2, e)["verdict"] == "UP"
    p3 = dict(p, fy_low=41.0, fy_high=42.0)
    assert grade(p3, e)["verdict"] == "DOWN"
    p4 = {k: v for k, v in p.items() if not k.startswith("fy_")}
    g4 = grade(p4, e)  # implied: 18.093 + 11.612 + 14.0 = 43.7 -> KEEP
    assert g4["verdict"] == "KEEP" and abs(g4["fy_mid"] - 43.705) < 1e-6, g4
    p5 = dict(p, q_sales=10.5, fy_low=45.5, fy_high=46.5)
    assert grade(p5, e)["verdict"] == "KEEP"  # raise + miss is not an upgrade
    lv = cfg["levels_eur"]
    for price, want in [(1636.4, "HOLD"), (1500, "ZONE1"), (1400, "ZONE2"), (1080, "BEAR"), (1800, "RICH"), (2124, "BULL"), (None, "DATA_FAIL")]:
        assert band_for(price, lv) == want, (price, band_for(price, lv))
    title, lines, _ = earn_copy(cfg, p, g, "https://example", {"price": 1700.0, "prev": 1636.4, "date": "2026-10-14"}, "2026-10-14 19:40 KST")
    assert title.startswith("[ASML]") and len(lines) == 7
    for b in ["HOLD", "ZONE1", "ZONE2", "BEAR", "RICH", "BULL"]:
        st = {"earn_verdict": "DOWN", "earn_summary": "Q3 하향"}
        ti, li, _ = slot_copy(cfg, st, b, {"price": 1450.0, "prev": 1500.0, "date": "2026-10-13"}, None, True, "x")
        assert ti.startswith("[ASML]") and len(li) == 7, (b, ti)
    print("selftest ok")
    return 0


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    print(f"{datetime.now(KST):%Y-%m-%d %H:%M KST} asml mode={mode}")
    if mode == "selftest":
        return selftest()
    cfg = load_cfg()
    if mode in {"slot", "dry-slot", "force-slot"}:
        return run_slot(cfg, mode)
    if mode in {"auto", "earn", "event", "dry-earn"}:
        return run_earn(cfg, mode)
    print(f"unknown mode {mode}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
