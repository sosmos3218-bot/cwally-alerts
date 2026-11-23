#!/usr/bin/env python3
"""[WBTN] 3Q26 실적 원문 vs 2026-10-08 투자 판단 조건 대조 (일회 알림).

modes: auto (oneshot 기본) | dry (발송/상태 저장 없이 출력) | force (창/중복 무시하고 채점 발송)
       install (설치 확인 1회 발송, 상태 파일을 건드리지 않음)
       fixture <path.txt> (로컬 검증: 텍스트 파일을 보도자료로 보고 채점만 출력, 발송 없음)
       selftest (네트워크 없이 파서/본문 검사, 발송/상태 저장 없음)

원문: SEC 8-K Item 2.02 보도자료(EX-99.1)와 10-Q. IR 뉴스 페이지는 발표일 공지 확인용(실패해도 계속).
원문이 없으면 침묵. 실패는 cwally-ops(제목 '조회 실패'), 매매 신호로 쓰지 않는다.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG = ROOT / "configs" / "wbtnq3.json"
STATE = ROOT / "state" / "wbtnq3.json"
CIK_NUM = "1997859"
# SEC requires a contact UA (https://www.sec.gov/os/accessing-edgar-data)
SEC_UA = {"User-Agent": "cwally-alerts sosmos3218@gmail.com", "Accept-Encoding": "identity"}
WEB_UA = {"User-Agent": "Mozilla/5.0 (cwally-alerts)", "Accept-Encoding": "identity"}
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
MONTH_NUM = {m: i + 1 for i, m in enumerate(MONTHS.split("|"))}
FILED_FROM = "2026-10-15"


# ---------- fetch ----------

def fetch(url: str, ua: dict) -> str:
    with urlopen(Request(url, headers=ua), timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def textify(html: str) -> str:
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", unescape(t))


def sec_docs() -> tuple[str, str, list[str], str]:
    """Return (release_text, tenq_text, links, error)."""
    errors, links = [], []
    release, tenq = "", ""
    try:
        data = json.loads(fetch(f"https://data.sec.gov/submissions/CIK000{CIK_NUM}.json", SEC_UA))
    except Exception as exc:
        return "", "", [], f"sec submissions: {exc}"
    rec = data.get("filings", {}).get("recent", {})
    rows = zip(rec.get("form", []), rec.get("filingDate", []), rec.get("accessionNumber", []),
               rec.get("primaryDocument", []), rec.get("items", []))
    for form, date, acc, doc, items in rows:
        if date < FILED_FROM:
            break
        base = f"https://www.sec.gov/Archives/edgar/data/{CIK_NUM}/{acc.replace('-', '')}"
        if form == "8-K" and "2.02" in (items or "") and not release:
            try:
                idx = json.loads(fetch(base + "/index.json", SEC_UA))
                names = [i.get("name", "") for i in idx.get("directory", {}).get("item", [])]
                ex = [n for n in names if re.search(r"(ex-?99|exx?99|991)", n, re.I) and n.endswith(".htm")]
                url = base + "/" + (ex[0] if ex else doc)
                release = textify(fetch(url, SEC_UA))
                links.append(url)
            except Exception as exc:
                errors.append(f"8-K: {exc}")
        if form == "10-Q" and not tenq:
            try:
                url = base + "/" + doc
                tenq = textify(fetch(url, SEC_UA))
                links.append(url)
            except Exception as exc:
                errors.append(f"10-Q: {exc}")
    return release, tenq, links, "; ".join(errors)


def ir_news(cfg: dict) -> tuple[str, str]:
    try:
        raw = fetch(cfg["sources"]["ir_news"], WEB_UA)
    except Exception as exc:
        return "", f"ir: {exc}"
    blobs = [textify(raw)]
    for href in re.findall(r'href="([^"]+)"', raw, re.I)[:400]:
        if re.search(r"third-quarter-2026", href, re.I):
            url = href if href.startswith("http") else "https://ir.webtoon.com" + ("" if href.startswith("/") else "/") + href
            try:
                blobs.append(textify(fetch(url, WEB_UA)))
            except Exception:
                pass
            if len(blobs) >= 4:
                break
    return " ".join(blobs), ""


def quote(cfg: dict) -> tuple[float | None, str]:
    try:
        data = json.loads(fetch(cfg["sources"]["quote"], WEB_UA))
        res = data["chart"]["result"][0]
        closes = [c for c in (res.get("indicators", {}).get("quote", [{}])[0].get("close") or []) if c]
        ts = res.get("timestamp") or []
        last = closes[-1] if closes else res["meta"].get("regularMarketPrice")
        when = ""
        if ts:
            when = datetime.fromtimestamp(ts[-1], KST).strftime("%m-%d %H:%M KST")
        return (float(last) if last else None), when
    except Exception as exc:
        print(f"quote fail: {exc}", file=sys.stderr)
        return None, ""


# ---------- parse ----------

def num(s: str | None) -> float | None:
    if s is None:
        return None
    s = s.strip()
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace(",", "")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def pair(pattern: str, text: str) -> tuple[float | None, float | None]:
    m = re.search(pattern, text, re.I)
    if not m:
        return None, None
    return num(m.group(1)), num(m.group(2))


def pct(cur: float | None, prev: float | None) -> float | None:
    if cur is None or prev in (None, 0):
        return None
    return (cur / prev - 1) * 100


def printed_pct(s: str | None) -> float | None:
    """괄호 또는 선행 마이너스는 음수. '0.2', '(11.4)', '(11.4%)'."""
    if not s:
        return None
    s = s.strip()
    neg = "(" in s or s.lstrip().startswith("-")
    s = s.replace("(", "").replace(")", "").replace("%", "").replace(",", "").strip()
    try:
        v = float(s)
    except ValueError:
        return None
    return -abs(v) if neg else v


def cc_growth_printed(text: str) -> float | None:
    """회사가 인쇄한 전사 고정환율 매출 성장률(%)을 읽는다.

    달러 두 칸의 비율은 쓰지 않는다. 3Q26 조정표는 $378,631 / $378,041 = 0.156%인데
    같은 칸과 헤드라인은 0.2%로 적혀 있다. 비율을 저장하면 0.156이 되고, 본문 반올림은
    +0.2%가 되어 저장된 숫자와 알림이 어긋난다. 가이던스 구간(9.0%-12.0%)과
    지역/사업부 행(Korea, Japan, Paid Content 등)은 전사 성장률이 아니다.
    """
    m = re.search(
        r"Revenue Growth on a Constant Currency Basis of (\(?-?[\d.]+\)?)\s*%",
        text,
        re.I,
    )
    if m:
        return printed_pct(m.group(1))
    skip = ("korea", "japan", "world", "content", "advertising", "adaptations")
    row = re.compile(
        r"Revenue on a Constant Currency Basis\s*\$?\s*[\d,]+\s*\$?\s*[\d,]+\s*(\(?-?[\d.]+\)?)\s*%",
        re.I,
    )
    for m in row.finditer(text):
        prev = text[max(0, m.start() - 16):m.start()].lower().rstrip()
        if any(prev.endswith(w) for w in skip):
            continue
        return printed_pct(m.group(1))
    return None


def japan_total_cc_disclosed(text: str) -> float | None:
    """보도자료 조정표의 일본 전체 매출 고정환율 성장률(%). 채점에는 쓰지 않는다.

    scripts/wbtn4q_monitor.py parse_results 의 일본 행과 같은 식이다.
    달러 두 칸 다음의 첫 퍼센트(분기 칸)만 읽고, 같은 행의 9개월 칸은 버린다.
    예: '(11.4%)' 뒤에 '(7.3%)'가 있으면 -11.4. 행이 없으면 None.
    """
    m = re.search(
        r"Japan Revenue on a Constant Currency Basis\s*\$?\s*[\d,]+\s*\$?\s*[\d,]+\s*(\(?-?[\d.]+\)?)\s*%",
        text or "",
        re.I,
    )
    if not m:
        return None
    return printed_pct(m.group(1))


def parse_release(text: str, quarter: str = "third", period: str = "September 30, 2026") -> dict:
    is_q = bool(re.search(rf"{quarter} quarter ended {period}", text, re.I))
    rev, rev_prev = pair(r"Total Revenue \$ ?([\d,]+) \$ ?([\d,]+)", text)
    if rev is None:
        m = re.search(r"Total revenue of \$([\d.]+) million", text, re.I)
        rev = float(m.group(1)) * 1000 if m else None
    cc_growth = cc_growth_printed(text)
    ebitda, _ = pair(r"Adjusted EBITDA\s*(?:\(\d\))?\s*\$\s*(\(?[\d,]+\)?)\s*\$\s*(\(?[\d,]+\)?)", text)
    margin = None
    m = re.search(r"Adjusted EBITDA Margin\s*(\(?-?[\d.]+\)?)\s*%", text, re.I)
    if m:
        margin = num(m.group(1))
    if margin is None and ebitda is not None and rev:
        margin = ebitda / rev * 100

    regions = {}
    for reg in ("Korea", "Japan", "Rest of World"):
        r_cur, r_prev = pair(rf"{reg} Paid Content Revenue \$ ?([\d,]+) \$ ?([\d,]+)", text)
        a_cur, a_prev = pair(rf"{reg} ARPPU ([\d.]+) ([\d.]+)", text)
        c_cur, _ = pair(rf"{reg} ARPPU on a Constant Currency Basis \$ ?([\d.]+) \$ ?([\d.]+)", text)
        regions[reg] = {"rev": r_cur, "rev_prev": r_prev, "arppu": a_cur, "arppu_prev": a_prev, "arppu_cc": c_cur}

    def mpu(reg, cur=True):
        d = regions[reg]
        r, a = (d["rev"], d["arppu"]) if cur else (d["rev_prev"], d["arppu_prev"])
        if not r or not a:
            return None
        return r / a / 3  # thousands of users (ARPPU = 월 유료매출 / MPU, 3개월 평균)

    jp = regions["Japan"]
    jp_mpu_g = pct(mpu("Japan"), mpu("Japan", False))
    jp_cc_g = None
    if jp["rev"] and jp["arppu"] and jp["arppu_cc"] and jp["rev_prev"]:
        jp_cc_g = (jp["rev"] * jp["arppu_cc"] / jp["arppu"] / jp["rev_prev"] - 1) * 100
    tot_cur = [mpu(r) for r in regions]
    tot_prev = [mpu(r, False) for r in regions]
    g_mpu_g = pct(sum(tot_cur), sum(tot_prev)) if all(tot_cur) and all(tot_prev) else None
    return {
        "is_quarter": is_q,
        "published": bool(is_q and rev is not None),
        "rev_m": rev / 1000 if rev else None,
        "rev_prev_m": rev_prev / 1000 if rev_prev else None,
        "cc_growth": cc_growth,
        "ebitda_m": ebitda / 1000 if ebitda is not None else None,
        "margin": margin,
        "jp_cc_growth": jp_cc_g,
        "jp_total_cc_growth_disclosed": japan_total_cc_disclosed(text),
        "jp_mpu_growth": jp_mpu_g,
        "jp_rev_growth": pct(jp["rev"], jp["rev_prev"]),
        "global_mpu_growth": g_mpu_g,
    }


def parse_10q(text: str, period: str = "September 30, 2026") -> dict:
    out = {"mau_m": None, "mau_growth": None, "mau_is_period": False}
    if not text:
        return out
    m = re.search(r"global MAU was approximately ([\d.]+) million\. The global MAU (increased|decreased) by approximately ([\d.]+)%", text, re.I)
    if m:
        out["mau_m"] = float(m.group(1))
        g = float(m.group(3))
        out["mau_growth"] = g if m.group(2).lower() == "increased" else -g
    out["mau_is_period"] = bool(re.search(rf"quarter ended {period}, our global MAU", text, re.I))
    if not out["mau_is_period"]:
        out["mau_m"] = out["mau_growth"] = None
    return out


def parse_date_notice(text: str) -> str | None:
    m = re.search(rf"third quarter 2026 (?:financial )?results[^.]{{0,160}}?({MONTHS}) (\d{{1,2}}), 2026", text, re.I)
    if not m:
        return None
    month = MONTH_NUM[m.group(1).capitalize()]
    return f"2026-{month:02d}-{int(m.group(2)):02d}"


# ---------- score ----------

def f1(v, unit="%"):
    return "없음" if v is None else (f"{v:+.1f}{unit}" if unit == "%" else f"{v:.1f}{unit}")


def pm(v):
    return "없음" if v is None else f"{v:.1f}%"


def judge(p: dict, q: dict, price: float | None, cfg: dict) -> dict:
    r = cfg["rules"]
    band = float(r["derived_band_pp"])
    items = []  # (label, status, fact) status: 충족 | 미충족 | 경계 | 확인불가

    def plain(label, v, ok, fact):
        if v is None:
            items.append((label, "확인불가", fact))
        else:
            items.append((label, "충족" if ok else "미충족", fact))

    def derived(label, v, line, fact):
        if v is None:
            items.append((label, "확인불가", fact))
        elif abs(v - line) < band:
            items.append((label, "경계", fact + f" (기준선 ±{band}%p 안, 세지 않음)"))
        else:
            items.append((label, "충족" if v >= line else "미충족", fact))

    rev, eb = p.get("rev_m"), p.get("ebitda_m")
    plain("매출", rev, rev is not None and rev >= r["buy_rev_m"], f"매출 {f1(rev, 'M달러')} (기준 {r['buy_rev_m']:g}M 이상)")
    plain("조정 EBITDA", eb, eb is not None and eb >= r["buy_ebitda_m"], f"조정 EBITDA {f1(eb, 'M달러')} (기준 {r['buy_ebitda_m']:g}M 이상)")
    derived("일본 고정환율 성장", p.get("jp_cc_growth"), r["buy_japan_cc_growth"],
            f"일본 유료콘텐츠 고정환율 성장 추정 {f1(p.get('jp_cc_growth'))} (기준 플러스)")
    derived("일본 MPU", p.get("jp_mpu_growth"), r["buy_japan_mpu_growth"],
            f"일본 유료 사용자(MPU) 증감 추정 {f1(p.get('jp_mpu_growth'))} (기준 -3% 이내)")
    mau, gmpu = q.get("mau_growth"), p.get("global_mpu_growth")
    if (mau is not None and mau >= r["buy_mau_growth"]) or (gmpu is not None and gmpu >= r["buy_mpu_growth"] + band):
        st = "충족"
    elif gmpu is not None and abs(gmpu - r["buy_mpu_growth"]) < band:
        st = "경계"
    elif mau is None and gmpu is None:
        st = "확인불가"
    elif mau is None:
        st = "미충족(MAU 미확인)"
    else:
        st = "미충족"
    items.append(("사용자 성장", st, f"글로벌 MAU {f1(mau)} (기준 3%), 글로벌 MPU 추정 {f1(gmpu)} (기준 5%)"))
    m = p.get("margin")
    items.append(("2분기 연속 마진 3%", "미충족", f"3분기 마진 {pm(m)}, 2분기 {cfg['baseline']['q2_adj_ebitda_margin']}%라 연속 충족 불가"))

    met = sum(1 for _, s, _ in items if s == "충족")
    price_ok = price is not None and price <= r["buy_price_max"]
    jp_mpu = p.get("jp_mpu_growth")
    sell_a = (rev is not None and rev < r["sell_rev_m"]) or (eb is not None and eb < r["sell_ebitda_m"])
    sell_c = jp_mpu is not None and jp_mpu <= r["sell_japan_mpu_growth"] - band
    core_missing = rev is None or eb is None
    if core_missing:
        verdict, why = "보유", "매출 또는 조정 EBITDA 확인불가. 판단 보류, 신규 매수 보류 유지."
    elif sell_a and sell_c:
        verdict, why = "매도", "가이던스 하단 미달 또는 적자 전환 + 일본 MPU 두 자릿수 감소. 매도/회피 조건 충족."
    elif met >= r["buy_min_count"] and price_ok:
        verdict, why = "매수 검토", f"조건 {met}/6 충족, 주가 {price:.2f}달러로 11달러 이하. 분할 매수 검토."
    elif met >= r["buy_min_count"]:
        verdict, why = "보유", f"조건 {met}/6 충족이나 주가 {('%.2f' % price) if price else '확인불가'}달러로 11달러 조건 미충족. 추격 금지."
    else:
        extra = " 매도 조건 일부(매출 하단 미달 또는 적자)만 충족, 일본 MPU 두 자릿수 감소는 아님." if sell_a else ""
        verdict, why = "보유", f"조건 {met}/6 충족, 매수 기준 3개 미달. 신규 매수 보류 유지.{extra}"
    return {"verdict": verdict, "why": why, "items": items, "met": met, "price": price,
            "sell_a": sell_a, "sell_c": sell_c, "mau_missing": mau is None}


# ---------- io ----------

def load_state() -> dict:
    st = {"final_notified": False, "verdict": None, "mau_missing": None, "supplement_notified": False,
          "date_notified": False, "release_date": None, "date_missing_notified": False,
          "fail_notified": False, "empty_final_notified": False}
    if STATE.exists():
        try:
            st.update(json.loads(STATE.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return st


def save_state(st: dict, now: datetime) -> None:
    st["updated_at"] = now.isoformat()
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def emit(cfg: dict, title: str, lines: list[str], pri: int = 3) -> None:
    publish(resolve_topic("port", cfg.get("ntfy_topic")), title[:180], "\n".join(lines), pri,
            ["chart_with_upwards_trend"], resolve_server(cfg.get("ntfy_server")))


def head(now: datetime, what: str) -> str:
    return now.strftime("%Y-%m-%d %H:%M KST") + " | WBTN | " + what


def score_lines(cfg, now, p, q, j, price_when, links, supplement=False):
    facts = ". ".join(f"{label} {s}: {fact}" for label, s, fact in j["items"])
    price_txt = f"{j['price']:.2f}달러({price_when}, 시간외 포함 최근가)" if j["price"] else "주가 확인불가"
    act = {
        "매수 검토": "분할 매수 검토. 첫 매수는 계획 비중의 1/3 이하.",
        "보유": "보유 중이면 유지, 없으면 신규 매수 보류.",
        "매도": "보유 중이면 비중 축소 또는 정리. 신규 매수 금지.",
    }[j["verdict"]]
    lines = [
        head(now, "3분기 실적 대조" + (" 보강" if supplement else "")),
        "이게 뭐 알림: " + cfg["explain"],
        f"지금 결과: 결론 {j['verdict']}. {j['why']} 주가 {price_txt}. 고정환율 매출 성장 {f1(p.get('cc_growth'))}, 조정 EBITDA 마진 {pm(p.get('margin'))}. {facts}.",
    ]
    # 유료콘텐츠 추정치(jp_cc_growth) 바로 다음. 공시 전체 매출은 표시만 하고 채점하지 않는다.
    disclosed = p.get("jp_total_cc_growth_disclosed")
    if disclosed is not None:
        lines.append(f"일본 전체 매출 고정환율 성장(공시) {f1(disclosed)} (채점 미반영)")
    lines.extend([
        "지금 할 일: " + act + " 확인불가와 경계는 충족으로 채우지 말 것.",
        "나중에: 실적 콜과 주주서한에서 일본 MPU, 4분기 가이던스, RI Games 투자 집행만 보완. 10-Q가 늦게 나오면 MAU는 다음 실행에서 판단이 바뀔 때만 보강.",
        "익절/줄일 때: 매도 조건이면 축소. 매수 검토로 샀는데 다음 분기 조정 EBITDA 적자 또는 일본 MPU 두 자릿수 감소면 정리.",
        "하지 말 것: 실적 직후 시간외 급등 추격, 일본 수치 확인 전 몰빵, 신용, 미수.",
        "출처: " + (", ".join(links) if links else cfg["sources"]["sec"]),
    ])
    return lines


# 2026-11-10 채점 때 state 에 남은 스냅샷. cc_growth 만 이 버그의 대상이고 판정 입력은 아니다.
NOV10_PARSED = {
    "rev_m": 362.775,
    "rev_prev_m": 378.041,
    "ebitda_m": 2.206,
    "margin": 0.6,
    "jp_cc_growth": -11.621098565243704,
    "jp_mpu_growth": -13.09408025582296,
    "jp_rev_growth": -17.8812707502056,
    "global_mpu_growth": -3.225039154547682,
}
NOV10_TENQ = {"mau_m": 159.6, "mau_growth": 2.7, "mau_is_period": True}
NOV10_CC_DOLLARS = (378631, 378041)  # 조정표 분기 칸. 비율은 0.156%, 인쇄는 0.2%.
Q3_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "wbtnq3" / "q3_2026_ex991.txt"


def selftest() -> int:
    fails = []

    def check(name, got, want):
        if got != want:
            fails.append(f"{name}: got {got!r} want {want!r}")

    text = Q3_FIXTURE.read_text(encoding="utf-8")
    check("fixture has printed 0.2", "Basis of 0.2%" in text, True)
    check("fixture keeps dollar trap", "$ 378,631 $ 378,041 0.2%" in text, True)
    check("fixture keeps fx 15856", "15,856" in text, True)
    check("fixture keeps outlook trap", "9.0%-12.0%" in text, True)

    p = parse_release(text)
    ratio = pct(*NOV10_CC_DOLLARS)
    check("printed cc is 0.2", p["cc_growth"], 0.2)
    check("not the dollar ratio", p["cc_growth"] == ratio, False)
    check("ratio is the old stored 0.156", round(ratio, 6) if ratio is not None else None, round(0.1560677281035705, 6))
    check("display of printed rate", f1(p["cc_growth"]), "+0.2%")
    check("old ratio also rounded to +0.2 in the alert", f1(ratio), "+0.2%")

    table_only = text[text.find("Total Revenue"):]
    check("table path still 0.2", cc_growth_printed(table_only), 0.2)
    check("table path is not 1.8 or 13.5", cc_growth_printed(table_only) not in (1.8, 13.5, -11.4, -7.3, 1.0, 30.1), True)

    # 인쇄된 %와 달러 비율이 반올림 뒤에도 다른 경우. 비율을 쓰면 +11.1%가 된다.
    diverged = "Revenue on a Constant Currency Basis $ 100,000 $ 90,000 (4.5%)"
    check("printed negative wins over dollar ratio", cc_growth_printed(diverged), -4.5)
    check("dollar ratio of that row is not -4.5", round(pct(100000, 90000), 1), 11.1)

    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    fresh = dict(NOV10_PARSED, cc_growth=p["cc_growth"])
    stale = dict(NOV10_PARSED, cc_growth=ratio)
    j_new = judge(fresh, NOV10_TENQ, 9.93, cfg)
    j_old = judge(stale, NOV10_TENQ, 9.93, cfg)
    check("11/10 verdict stays 보유", j_new["verdict"], "보유")
    check("verdict ignores cc_growth", j_new["verdict"], j_old["verdict"])
    check("met stays 0", j_new["met"], 0)
    check("met unchanged", j_new["met"], j_old["met"])

    now = datetime(2026, 11, 10, 12, 10, tzinfo=KST)
    blob = "\n".join(score_lines(cfg, now, fresh, NOV10_TENQ, j_new, "11-10 06:01 KST", ["fixture"]))
    shown = f"고정환율 매출 성장 {f1(fresh['cc_growth'])}"
    check("body uses parsed cc", shown in blob, True)
    check("body shows +0.2%", "고정환율 매출 성장 +0.2%" in blob, True)
    check("body has no 15.6", "+15.6%" not in blob, True)
    check("body keeps ticker context", "WBTN" in blob, True)
    check("no disclosed line when value absent", "일본 전체 매출 고정환율 성장(공시)" in blob, False)

    # Ex 99.1 일본 전체 매출 고정환율: 분기 (11.4%) 와 9개월 (7.3%). % 는 괄호 안.
    # 채점은 ARPPU 추정치를 유지하고, 파서는 첫 칸만 읽는다.
    row_m = re.search(r"Japan Revenue on a Constant Currency Basis.*?(?=Paid Content)", text)
    row = row_m.group(0) if row_m else ""
    row_pcts = [printed_pct(x) for x in re.findall(r"\([^)]*%\)", row)]
    check("fixture quarter then nine-month", row_pcts, [-11.4, -7.3])
    check("disclosed japan total cc is quarter -11.4", p["jp_total_cc_growth_disclosed"], -11.4)
    check("disclosed is not nine-month -7.3", p["jp_total_cc_growth_disclosed"] == (row_pcts[-1] if row_pcts else None), False)
    check("fixture parse does not replace scoring field", p["jp_cc_growth"] == -11.4, False)
    import wbtn4q_monitor
    check(
        "mirrors wbtn4q japan regex",
        p["jp_total_cc_growth_disclosed"],
        wbtn4q_monitor.parse_results(text)["jp_cc_growth"],
    )

    scored = dict(fresh, jp_total_cc_growth_disclosed=p["jp_total_cc_growth_disclosed"])
    j_scored = judge(scored, NOV10_TENQ, 9.93, cfg)
    check("verdict unchanged by disclosed", j_scored["verdict"], j_new["verdict"])
    check("met unchanged by disclosed", j_scored["met"], j_new["met"])
    check("why unchanged by disclosed", j_scored["why"], j_new["why"])
    check("items unchanged by disclosed", j_scored["items"], j_new["items"])
    check("verdict still 보유", j_scored["verdict"], "보유")
    check("met still 0", j_scored["met"], 0)
    check("record keeps arppu estimate", scored["jp_cc_growth"], NOV10_PARSED["jp_cc_growth"])
    check("record stores disclosed", scored["jp_total_cc_growth_disclosed"], -11.4)
    jp_item = next(it for it in j_scored["items"] if it[0] == "일본 고정환율 성장")
    check("criterion 3 still estimate", "일본 유료콘텐츠 고정환율 성장 추정 -11.6%" in jp_item[2], True)
    check("criterion 3 status", jp_item[1], "미충족")
    check("criterion 3 ignores disclosed percent", "-11.4" not in jp_item[2], True)

    blob2 = "\n".join(score_lines(cfg, now, scored, NOV10_TENQ, j_scored, "11-10 06:01 KST", ["fixture"]))
    exact = "일본 전체 매출 고정환율 성장(공시) -11.4% (채점 미반영)"
    lines = blob2.splitlines()
    check("body disclosed line", exact in lines, True)
    check("body still has paid content estimate", "일본 유료콘텐츠 고정환율 성장 추정 -11.6%" in blob2, True)
    est_i = next(i for i, line in enumerate(lines) if "일본 유료콘텐츠 고정환율 성장 추정 -11.6%" in line)
    check("disclosed line follows estimate line", lines.index(exact), est_i + 1)
    check("disclosed line has no middle dot", "·" in exact, False)
    check("disclosed line has no em dash", "—" in exact, False)
    check("title unchanged", f"[WBTN] {j_scored['verdict']} | 3분기 조건 대조", "[WBTN] 보유 | 3분기 조건 대조")

    withheld = text.replace("Japan Revenue on a Constant Currency Basis", "Japan total CC row withheld")
    p_miss = parse_release(withheld)
    check("absent row disclosed is none", p_miss["jp_total_cc_growth_disclosed"], None)
    miss_rec = dict(fresh, jp_total_cc_growth_disclosed=None)
    j_miss = judge(miss_rec, NOV10_TENQ, 9.93, cfg)
    blob_miss = "\n".join(score_lines(cfg, now, miss_rec, NOV10_TENQ, j_miss, "11-10 06:01 KST", ["fixture"]))
    check("absent omits disclosed line", "일본 전체 매출 고정환율 성장(공시)" in blob_miss, False)
    check("absent verdict unchanged", j_miss["verdict"], j_new["verdict"])
    check("absent met unchanged", j_miss["met"], j_new["met"])
    check("absent items unchanged", j_miss["items"], j_new["items"])
    check("rules note display only", "채점에 넣지 않는다" in cfg["rules_note"], True)

    if fails:
        print("SELFTEST FAIL")
        for f in fails:
            print(" ", f)
        return 1
    print("selftest ok")
    return 0


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode == "selftest":
        return selftest()
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    now = datetime.now(KST)

    if mode == "fixture":
        text = Path(sys.argv[2]).read_text(encoding="utf-8")
        quarter = sys.argv[3] if len(sys.argv) > 3 else "third"
        period = sys.argv[4] if len(sys.argv) > 4 else "September 30, 2026"
        tenq = Path(sys.argv[5]).read_text(encoding="utf-8") if len(sys.argv) > 5 else ""
        p = parse_release(text, quarter, period)
        q = parse_10q(tenq, period)
        price, when = quote(cfg)
        j = judge(p, q, price, cfg)
        print(json.dumps({"parsed": p, "tenq": q}, ensure_ascii=False, indent=2))
        print(f"[WBTN] {j['verdict']} | 3분기 조건 대조")
        print("\n".join(score_lines(cfg, now, p, q, j, when, ["fixture"])))
        return 0

    if mode == "install":
        lines = [
            head(now, "설치 확인"),
            "이게 뭐 알림: " + cfg["explain"],
            "지금 상태: 설치 확인용 발송입니다. 매매 신호가 아닙니다. 회사는 아직 3분기 발표일을 공지하지 않았고 시장 추정은 미국 11/11 장 마감 후입니다.",
            "지금 할 일: 없음. 기존 판단(신규 매수 보류) 유지.",
            "나중에: 10/29, 11/5 09:23 KST에 발표일 공지를 확인하고, 11/6~11/14 12:10 KST에 원문이 나오면 한 번 채점합니다. 원문 전에는 조용합니다.",
            "익절/줄일 때: 이 설치 확인으로 비중을 바꾸지 마세요.",
            "하지 말 것: 이 알림을 매수 또는 매도 신호로 읽기.",
        ]
        emit(cfg, "[WBTN] 설치 확인 | 매매 신호 아님", lines, 2)
        return 0  # state untouched by design (AGENTS.md install-confirmation rule)

    st = load_state()
    start = datetime.fromisoformat(cfg["window_start_kst"]).replace(tzinfo=KST)
    expire = datetime.fromisoformat(cfg["expire_date"] + "T23:59:59").replace(tzinfo=KST)
    in_win = start <= now <= expire and now.year == int(cfg["event_year"])
    print(f"{now.isoformat()} mode={mode} window={in_win}")
    if mode not in {"force", "dry"} and not in_win:
        print("outside 2026 window; silent")
        return 0

    release, tenq, links, sec_err = sec_docs()
    ir_text, ir_err = ir_news(cfg)
    p = parse_release(release) if release else {"published": False}
    if not p.get("published") and ir_text:
        p_ir = parse_release(ir_text)
        if p_ir.get("published") and p_ir.get("ebitda_m") is not None:
            p = p_ir
            links = links + [cfg["sources"]["ir_news"]]
    q = parse_10q(tenq)
    st["parsed"] = p
    st["tenq"] = q
    last_run = now >= datetime.fromisoformat(cfg["last_run_kst"]).replace(tzinfo=KST) - timedelta(minutes=15)

    if p.get("published"):
        price, when = quote(cfg)
        j = judge(p, q, price, cfg)
        title = f"[WBTN] {j['verdict']} | 3분기 조건 대조"
        if mode == "dry":
            print(title)
            print("\n".join(score_lines(cfg, now, p, q, j, when, links)))
            return 0
        if not st.get("final_notified") or mode == "force":
            emit(cfg, title, score_lines(cfg, now, p, q, j, when, links), 4 if j["verdict"] != "보유" else 3)
            st.update(final_notified=True, verdict=j["verdict"], mau_missing=j["mau_missing"], met=j["met"])
        elif st.get("mau_missing") and not j["mau_missing"] and not st.get("supplement_notified"):
            st["supplement_notified"] = True
            st["mau_missing"] = False
            if j["verdict"] != st.get("verdict"):
                emit(cfg, f"[WBTN] {j['verdict']}로 변경 | MAU 보강", score_lines(cfg, now, p, q, j, when, links, True), 4)
                st["verdict"] = j["verdict"]
            else:
                print("MAU filled; verdict unchanged; silent")
        else:
            print("already scored; silent")
        save_state(st, now)
        return 0

    # not published yet
    if mode == "dry":
        print("not published", sec_err, ir_err, parse_date_notice(ir_text) if ir_text else None)
        return 0
    if sec_err and not ir_text and not st.get("fail_notified"):
        lines = [
            head(now, "3분기 실적"),
            "이게 뭐 알림: " + cfg["explain"],
            "지금 상태: 조회 실패. " + sec_err + ". 실패를 실적 결과로 채점하지 않음.",
            "지금 할 일: SEC 또는 IR 원문을 직접 확인. 이 실패로 매매하지 마세요.",
            "나중에: 남은 일정에서 자동 재확인. 마지막 실행 11/14 12:10 KST.",
            "익절/줄일 때: 조회 실패로 비중 조정 금지.",
            "하지 말 것: 조회 실패를 실적 미스로 읽기.",
        ]
        emit(cfg, "[WBTN] 조회 실패 | 판단 보류", lines, 3)
        st["fail_notified"] = True
        save_state(st, now)
        return 0

    rdate = parse_date_notice(ir_text) if ir_text else None
    if rdate and not st.get("date_notified"):
        covered = "2026-11-05" <= rdate <= "2026-11-13"
        kst_run = (datetime.fromisoformat(rdate) + timedelta(days=1)).strftime("%m/%d")
        plan = (f"{kst_run} 12:10 KST 실행에서 채점합니다." if covered
                else "이 알림의 채점 창(미국 11/5~11/13 발표) 밖입니다. 일정 재설치가 필요합니다.")
        lines = [
            head(now, "발표일 공지"),
            "이게 뭐 알림: " + cfg["explain"],
            f"지금 상태: 회사 공지 3분기 발표일 {rdate}(미국). {plan}",
            "지금 할 일: 없음. 발표 전 신규 매수 보류 유지.",
            "나중에: 원문이 나오면 매수 검토, 보유, 매도 중 하나로 한 번 알립니다.",
            "익절/줄일 때: 발표일 공지만으로 비중 조정 금지.",
            "하지 말 것: 발표 전 기대감 추격매수, 신용, 미수.",
        ]
        emit(cfg, f"[WBTN] 발표일 {rdate[5:]} 공지 | 매매 신호 아님", lines, 2 if covered else 4)
        st.update(date_notified=True, release_date=rdate)
        save_state(st, now)
        return 0

    missing_after = datetime.fromisoformat(cfg["date_missing_after_kst"]).replace(tzinfo=KST)
    if not rdate and not st.get("date_notified") and now >= missing_after and not st.get("date_missing_notified") and not last_run:
        if ir_err:
            title, state_txt = "[WBTN] 발표일 조회 실패 | 판단 보류", "IR 페이지 조회 실패(" + ir_err + "). 발표일 공지 여부를 모름."
        else:
            title, state_txt = "[WBTN] 발표일 미공지 | 신규 매수 보류", "IR에 3분기 발표일 공지가 아직 없음."
        lines = [
            head(now, "발표일 확인"),
            "이게 뭐 알림: " + cfg["explain"],
            "지금 상태: " + state_txt + " 채점 실행은 11/6~11/14 12:10 KST에 그대로 돕니다.",
            "지금 할 일: 없음. 신규 매수 보류 유지.",
            "나중에: 미국 11/13 이후 발표면 이 알림이 놓칩니다. 그때는 재설치가 필요합니다.",
            "익절/줄일 때: 미공지로 비중 조정 금지.",
            "하지 말 것: 미공지를 실적 부진 신호로 읽기.",
        ]
        emit(cfg, title, lines, 2)
        st["date_missing_notified"] = True
        save_state(st, now)
        return 0

    if last_run and not st.get("empty_final_notified"):
        lines = [
            head(now, "3분기 실적"),
            "이게 뭐 알림: " + cfg["explain"],
            "지금 상태: 마지막 실행까지 SEC 3분기 원문 없음. 2분기 수치로 채점하지 않음."
            + (f" 공지 발표일 {st.get('release_date')}." if st.get("release_date") else ""),
            "지금 할 일: 신규 매수 보류 유지. 발표 후 원문을 직접 대조하세요.",
            "나중에: 이 일회 알림은 여기서 끝납니다. 발표가 늦으면 재설치가 필요합니다.",
            "익절/줄일 때: 미발표로 비중 조정 금지.",
            "하지 말 것: 미발표를 가이던스 하향으로 읽기, 추격매수.",
        ]
        emit(cfg, "[WBTN] 원문 없음 | 신규 매수 보류", lines, 3)
        st["empty_final_notified"] = True
        save_state(st, now)
        return 0

    print("not published; silent", sec_err, ir_err)
    save_state(st, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
