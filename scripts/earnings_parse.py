#!/usr/bin/env python3
"""Shared earnings-text parser for DART 공정공시 (잠정/확정 실적) and IR scripts.

Used by lges1103_monitor.py; meant for later one-shots such as a 4분기 잠정실적 alert.
All amounts are 억원 ints. Losses are negative. Nothing here sends ntfy or writes state by itself:
safe_parse() only calls the notify callback you pass and sets one state key.

usage: python scripts/earnings_parse.py selftest
"""
from __future__ import annotations

import re
import sys
from typing import Callable

# A number must start with a digit (a bare "," used to reach int() and raise ValueError).
NUM_RE = r"([-△]?\d[\d,]*)"  # △ = 음수 (LG 보도자료 "영업이익 △1220억 원")
# The gap between label and number may not cross another metric word, so
# "보상금 반영으로 영업이익은 7,560억 원" is not read as a 7,560억원 compensation.
GAP_RE = r"(?:(?!영업이익|매출|순이익|순손실|제외|포함)[^0-9\-△]){0,24}?"
MONEY_RE = r"(\d[\d,]*(?:\.\d+)?)\s*(조|억|백만)\s*원?(?:\s*(\d[\d,]*)\s*억\s*원?)?"
RANGE = (-200000, 300000)


def eok(text: str, pattern: str) -> int | None:
    """pattern groups: (number, unit 억원|백만원)."""
    hit = re.search(pattern, text, re.I)
    if not hit or hit.group(1) is None:
        return None
    raw = int(hit.group(1).replace(",", "").replace("△", "-"))
    if (hit.group(2) or "").startswith("백만"):
        raw = raw // 100
    return raw if RANGE[0] < raw < RANGE[1] else None


def grab(text: str, label: str) -> int | None:
    return eok(text, r"(?:" + label + r")" + GAP_RE + NUM_RE + r"\s*(억\s*원|백만\s*원)")


def money(hit: re.Match | None, sign: int = 1, g: int = 1) -> int | None:
    """MONEY_RE groups (g, g+1, g+2) -> 억원: 4,169억원 / 4,169억 원 / 0.4조원 / 1조 2,000억원."""
    if not hit:
        return None
    num = float(hit.group(g).replace(",", ""))
    unit, extra = hit.group(g + 1), hit.group(g + 2)
    v = num * 10000 if unit == "조" else num / 100 if unit == "백만" else num
    v = int(round(v)) + (int(extra.replace(",", "")) if extra else 0)
    return sign * v if RANGE[0] < v < RANGE[1] else None


def table_row(text: str, label: str) -> int | None:
    """DART 공정공시 표 행: "당기순이익 당해실적 -2,969" (단위 억원, 숫자 뒤 단위 없음)."""
    hit = re.search(r"(?:" + label + r")\s+당해실적\s+" + NUM_RE + r"(?![\d.])", text)
    if not hit:
        return None
    v = int(hit.group(1).replace(",", "").replace("△", "-"))
    if re.search(r"단위\s*:\s*백만원", text):
        v //= 100
    return v if RANGE[0] < v < RANGE[1] else None


def signed(text: str, gain: str, loss: str) -> int | None:
    """문장형 손익: 손실은 음수. "당기순손실은 2,969억 원" -> -2969."""
    for pat, sign in ((loss, -1), (gain, 1)):
        v = money(re.search(r"(?:" + pat + r")(?:은|는|이|가)?\s*(?:약\s*)?" + MONEY_RE, text), sign)
        if v is not None:
            return v
    return None


SUBSIDY_LABELS = r"Tax\s*Credit|AMPC|북미\s*생산\s*보조금|첨단\s*제조\s*생산\s*세액공제"


def parse_results(text: str, subsidy: str = SUBSIDY_LABELS, extra: dict[str, str] | None = None) -> dict:
    """Generic 실적 parse. Period-agnostic: works for 3분기 확정 and 4분기 잠정 공시 alike.

    subsidy: labels for the directly stated subsidy amount (AMPC). extra: {key: grab label} for
    company-specific items (보상금, 사업부, 현금흐름). ex = 보조금 제외 영업이익.
    """
    op = grab(text, r"영업이익")
    if op is None:
        op = table_row(text, r"영업이익")
    if op is None:
        op = money(re.search(r"영업이익(?:은|는|이)?\s*" + MONEY_RE, text))
    sales = grab(text, r"매출액")
    if sales is None:
        sales = table_row(text, r"매출액")
    if sales is None:
        sales = money(re.search(r"매출액?(?:은|는|이)?\s*" + MONEY_RE, text))
    net = table_row(text, r"당기순이익")
    if net is None:
        net = signed(text, r"당기\s*순이익", r"당기\s*순손실")
    pretax = table_row(text, r"법인세비용차감전계속사업이익|법인세비용차감전순이익")
    if pretax is None:
        pretax = signed(text, r"법인세\s*(?:비용\s*)?차감\s*전\s*(?:순)?이익", r"법인세\s*(?:비용\s*)?차감\s*전\s*(?:순)?손실")
    # "보조금 효과를 포함한 수치이며, 이를 제외한 매출액은 92,265억원"처럼 포함/제외 문구를 건너뛰지 않는다.
    sub = money(re.search(r"(?:" + subsidy + r")(?:(?!제외|포함|영업이익)[^.\n]){0,60}?" + MONEY_RE, text, re.I))
    # 공시 기타 항목: "이를 제외한 ... 영업이익은 3,391억원, 영업이익률은 +3.7%"
    hit = re.search(r"제외한[^.]{0,80}?영업이익(?:은|는|이)?\s*([-△]?)" + MONEY_RE, text)
    ex = money(hit, -1 if hit and hit.group(1) else 1, 2)
    mm = re.search(r"제외한[^.]{0,140}?영업이익률(?:은|는|이)?\s*([+\-△]?\d+(?:\.\d+)?)\s*%", text)
    if sub is None and op is not None and ex is not None:
        sub = op - ex
    if ex is None and op is not None and sub is not None:
        ex = op - sub
    out = {"sales": sales, "op": op, "net": net, "pretax": pretax, "ampc": sub, "ex": ex,
           "ex_margin": float(mm.group(1).replace("△", "-")) if mm else None}
    for key, label in (extra or {}).items():
        out[key] = grab(text, label)
    return out


def unwrap_pdf(text: str) -> str:
    """pdftotext breaks Korean lines mid-word ("수익\n성이"): join hangul-hangul wraps without a space."""
    return re.sub(r"(?<=[가-힣])\n(?=[가-힣])", "", text)


def quote(snt: str | None, pat: re.Pattern | None = None, cap: int = 160) -> str:
    """Quote the clause that carries the hit verbatim, instead of the first N chars of the sentence."""
    if not snt:
        return ""
    m = pat.search(snt) if pat else None
    if m is None or len(snt) <= cap:
        return snt if len(snt) <= cap else snt[:cap] + "..."
    cuts = [c.end() for c in re.finditer(r",\s|에\s*더해\s|그리고\s|며\s|고\s", snt[:m.start()])]
    start = cuts[-1] if cuts and m.start() - cuts[-1] <= 60 else max(0, m.start() - 40)
    out = snt[start:]
    return ("..." if start else "") + (out if len(out) <= cap else out[:cap] + "...")


# ---------------- guidance classification ----------------

def period_re(quarter: int) -> re.Pattern:
    """Regex for the guided quarter: period_re(4) for a 3분기 call, period_re(1) for a 4분기 call."""
    return re.compile(rf"{quarter}\s*분기|{quarter}Q|다음\s*분기" + (r"|fourth quarter|연말" if quarter == 4 else r"|내년\s*초|연초"), re.I)


Q4 = period_re(4)
EXC = re.compile(
    r"(보조금|IRA|AMPC|세액\s*공제|tax credit|production (tax )?credit|incentive|45X)\s*(효과|금액)?\s*(를|을|은|는|도)?\s*(제외|빼|뺀|없이)|"
    r"(excluding|ex-|without)\s*(the\s*)?(IRA|AMPC|tax credit|production)|진짜\s*흑자|실질\s*흑자|본업\s*(흑자|이익|수익성)", re.I)
PROFIT = re.compile(r"흑자|적자|손실|이익|손익|수익성|profit|loss|margin|마진", re.I)
NEG = re.compile(
    r"적자|손실|\bloss|unprofitable|흑자\s*(를\s*)?(유지|지속)\s*(는|가|이|하기)?\s*(가\s*)?(어렵|쉽지\s*않|불투명|힘들)|"
    r"이익\s*(감소|축소|둔화)\s*(는|가|이)?\s*불가피", re.I)
NEG_NEGATED = re.compile(r"(적자|손실)\s*(로\s*)?(전환|돌아설|를\s*낼|날)\s*(가능성|우려|일)\s*(은|는|이)?\s*(낮|크지\s*않|없)", re.I)
POS = re.compile(
    r"흑자|수익성\s*(을|도|이)?\s*(확보|개선|높|유지|강화)|이익\s*(이|은|도)?\s*(증가|개선|확대|늘)|"
    r"profit\w*\s*(to\s*)?(continue|remain|sustain|improve|expand|grow)|remain\s*profitable|stay\s*profitable", re.I)
ASPIRE = re.compile(r"최선|노력하|목표로|하도록\s*하겠|할\s*수\s*있도록|aim|strive", re.I)
SLOW_WHAT = re.compile(r"수익성|손익|이익률|영업이익")
SLOW_HOW = re.compile(r"둔화|감소|하락|축소|악화|부담")

GUIDANCE_WEAK = {"not_stated": 0, "overall_slowdown": 1, "aspirational": 2}


def classify_guidance(sents: list[str], period: re.Pattern = Q4) -> tuple[str, str | None]:
    """-> positive | negative | mixed | aspirational | overall_slowdown | not_stated, evidence sentence.

    overall_slowdown: 해당 분기 전사 수익성 둔화는 말했지만 보조금 제외 손익은 말하지 않음.
    """
    pos = neg = asp = None
    for i, snt in enumerate(sents):
        prev = sents[i - 1] if i else ""
        if not (EXC.search(snt) and PROFIT.search(snt) and (period.search(snt) or period.search(prev))):
            continue
        if NEG.search(snt) and not NEG_NEGATED.search(snt):
            neg = neg or snt
        elif POS.search(snt) or NEG_NEGATED.search(snt):
            if ASPIRE.search(snt):
                asp = asp or snt
            else:
                pos = pos or snt
    if neg and pos:
        return "mixed", neg
    if neg:
        return "negative", neg
    if pos:
        return "positive", pos
    if asp:
        return "aspirational", asp
    for i, snt in enumerate(sents):
        prev = sents[i - 1] if i else ""
        if EXC.search(snt) or not (period.search(snt) or period.search(prev)):
            continue
        if SLOW_WHAT.search(snt) and SLOW_HOW.search(snt):
            return "overall_slowdown", snt
    return "not_stated", None


# ---------------- safe parse with once-per-day ops notice ----------------

def safe_parse(parse_fn: Callable[[], dict], *, state: dict, day: str, ops_notify: Callable[[str], None],
               key: str = "ops_parse_day", dry: bool = False, label: str = "parse") -> dict | None:
    """Run parse_fn(); on any exception print ::error::, call ops_notify(detail) once per `day`, return None.

    The caller owns the title/body (ticker in the title) and persisting `state`. In dry mode ops_notify is
    still called (the caller should print only) but the state key is not set.
    """
    try:
        return parse_fn()
    except Exception as exc:  # noqa: BLE001
        detail = f"{type(exc).__name__}: {str(exc)[:80]}"
        print(f"::error::{label} failed: {detail}", file=sys.stderr)
        if state.get(key) == day:
            print(f"{label} failure already reported to ops today")
            return None
        try:
            ops_notify(detail)
        except Exception as e2:  # noqa: BLE001
            print(f"ops notify failed: {e2}", file=sys.stderr)
            return None
        if not dry:
            state[key] = day
        return None


# ---------------- selftest (real 2026-11-03 LG에너지솔루션 3분기 texts) ----------------

Q3_NOTICE = (
    "1. 연결실적내용(단위: 억원, %) - - 당기실적('26.3Q) - - - 법인세비용차감전 계속사업이익(당해실적) - -2,312 - "
    "당기순이익(당해실적) - -2,969 - 1. 연결실적내용 단위 : 억원, % 구분 당기실적 전기실적 "
    "매출액 당해실적 96,434 75,602 27.6 - 60,654 59.0 - 누계실적 237,586 - - - 188,442 26.1 - "
    "영업이익 당해실적 7,560 1,133 567.3 - 6,013 25.7 - 누계실적 6,616 - - - 14,681 -54.9 - "
    "법인세비용차감전계속사업이익 당해실적 -2,312 -1,278 - - 5,528 - 적자전환 누계실적 -12,176 - - - 8,902 - 적자전환 "
    "당기순이익 당해실적 -2,969 -3,286 - - 5,361 - 적자전환 누계실적 -15,696 - - - 8,533 - 적자전환 "
    "지배기업 소유주지분 순이익 당해실적 - - - - - - - 4. 기타 투자판단과 관련한 중요사항 "
    "나. 상기 '1. 연결실적내용' 중, '26.3Q 매출액 96,434억원, 영업이익 7,560억원은 미국 IRA Tax Credit(Inflation Reduction Act) "
    "첨단 제조 생산 세액공제(Advanced Manufacturing Production Credit) 제도(45X) 등에 따른 2026년 3분기 예상 북미 생산 보조금 "
    "효과를 포함한 수치이며, 이를 제외한 2026년 3분기 매출액은 92,265억원, 영업이익은 3,391억원, 영업이익률은 +3.7%임."
)
Q3_SCRIPT_RAW = (
    "CFO: 3분기 손익은 견조한 유럽 고객 수요를 기반으로 한 폴란드 Site의 가동률 개선, 상대적으로 수익\n"
    "성이 견조한 원통형 제품 공급 물량 지속 효과, 그리고 ESS 신규 양산 Site들의 생산성 개선에 더해 고객으로부터 일회성 물량 보상금 반영으로 "
    "영업이익은 7,560억 원을, 영업이익률은 7.8%를 기록했습니다. 한편 매출액과 영업이익에 반영된 북미 생산 보조금은 전력망 중심의 "
    "ESS 제품 판매 확대에 힘입어 전분기 대비 73% 늘어난 4,169억 원을 기록했습니다. "
    "법인세 차감 전 순손실은 2,312억 원, 당기순손실은 2,969억 원이었습니다. "
    "3분기에 인식했던 여러 가지 일회성 요인이 좀 소멸되는 기저 효과가 있고, 또 4분기에 예정되어 있는 혼다 JV에 ESS Capa 추가 가동 "
    "준비함에 따라서 초기 고정비 부담이 발생할 것 같습니다. 이로 인해서 4분기 수익성은 일부 둔화가 불가피할 것으로 보고 있습니다."
)
# Real 2026-01-09 4분기 잠정 공시 (DART 20260109800185, financialfilings.com 사본) and LG 보도자료 29769.
Q4_2025_NOTICE = (
    "연결재무제표 기준 영업(잠정)실적(공정공시) 실적기간 당기실적 2025-10-01 ~ 2025-12-31 전기실적 2025-07-01 ~ 2025-09-30 "
    "※ 동 정보는 잠정치로서 향후 확정치와는 다를 수 있음. 1. 연결실적내용 단위 : 억원, % 구분 당기실적 전기실적 전기대비 "
    "('25.4Q) ('25.3Q) 증감율(%) 흑자적자전환여부 ('24.4Q) 증감율(%) 흑자적자전환여부 "
    "매출액 당해실적 61,415 56,999 7.7 - 64,512 -4.8 - 누계실적 236,718 - - - 256,196 -7.6 - "
    "영업이익 당해실적 -1,220 6,013 -120.3 적자전환 -2,255 45.9 - 누계실적 13,461 - - - 5,754 133.9 - "
    "법인세비용차감전계속사업이익 당해실적 - - - - - - - 누계실적 - - - - - - - 당기순이익 당해실적 - - - - - - - "
    "4. 기타 투자판단과 관련한 중요사항 가. 상기 '1. 연결실적내용'은 K-IFRS 연결 재무제표 기준에 따라 작성된 영업(잠정) 실적임 "
    "나. 상기 1. 연결실적내용 중, '25.4Q 영업이익 당기실적' 금액인 -1,220억원은 미국 IRA Tax Credit (Inflation Reduction Act) "
    "첨단 제조 생산 세액공제(Advanced Manufacturing Production Credit) 제도(45X) 등에 따른 2025년 4분기 예상 세액 공제 금액을 포함한 수치이며, "
    "이를 제외한 2025년 4분기 영업이익은 -4,548억원, 영업이익률은 -7.4%임"
)
Q4_2025_RELEASE = (
    "LG에너지솔루션이 2025년 4분기 잠정 실적을 발표했다. 매출 6조1415억 원, 영업이익 △1220억 원이다. "
    "미국 IRA(Inflation Reduction Act) 첨단 제조 생산 세액공제(Advanced Manufacturing Production Credit) 제도(45X) 등에 따른 "
    "Tax Credit은 3328억 원으로 동 금액을 제외한 영업이익은 △4548억 원이다."
)
COMP_QUOTE = "고객으로부터 일회성 물량 보상금 반영으로 영업이익은 7,560억 원을, 영업이익률은 7.8%를 기록했습니다."


def selftest() -> int:
    fails = 0

    def check(name, got, want):
        nonlocal fails
        ok = got == want
        print(("ok  " if ok else "FAIL"), name, got, "" if ok else f"want {want}")
        fails += not ok

    extra = {"comp": r"보상금|위약금|최소구매"}
    pn = parse_results(Q3_NOTICE, extra=extra)
    script = unwrap_pdf(Q3_SCRIPT_RAW)
    ps = parse_results(script, extra=extra)
    for k, want in (("ampc", 4169), ("ex", 3391), ("ex_margin", 3.7), ("op", 7560), ("sales", 96434),
                    ("net", -2969), ("pretax", -2312), ("comp", None)):
        check(f"notice {k}", pn[k], want)
    for k, want in (("ampc", 4169), ("op", 7560), ("ex", 3391), ("net", -2969), ("pretax", -2312), ("comp", None)):
        check(f"script {k}", ps[k], want)
    q4 = parse_results(Q4_2025_NOTICE)
    for k, want in (("sales", 61415), ("op", -1220), ("ex", -4548), ("ampc", 3328), ("ex_margin", -7.4), ("net", None)):
        check(f"q4'25 notice {k}", q4[k], want)
    q4r = parse_results(Q4_2025_RELEASE)
    for k, want in (("sales", 61415), ("op", -1220), ("ampc", 3328), ("ex", -4548)):
        check(f"q4'25 release {k}", q4r[k], want)
    check("△ table row", table_row("영업이익 당해실적 △2,255 4,483", "영업이익"), -2255)
    check("unwrap 수익성", "수익성이" in script, True)
    check("조원", parse_results("AMPC 0.4조원 반영")["ampc"], 4000)
    check("조+억", parse_results("영업이익 1조 2,000억원")["op"], 12000)
    check("백만원 table", parse_results("단위 : 백만원 영업이익 당해실적 756,000")["op"], 7560)
    check("comma alone", parse_results("순이익 , 억원 영업이익 , 억원")["op"], None)
    check("loss sentence", parse_results("당기순손실은 1조 2,300억 원")["net"], -12300)
    check("ex negative", parse_results("이를 제외한 영업이익은 -1,200억원, 영업이익률은 -1.3%임")["ex"], -1200)
    check("grab gap guard", grab("보상금 반영으로 영업이익은 7,560억 원", r"보상금"), None)
    check("grab plain", grab("보상금 1,500억원", r"보상금"), 1500)
    sents = [s for s in re.split(r"(?<=[다.])\s+", re.sub(r"\s+", " ", script)) if s]
    g = classify_guidance(sents)
    check("guidance overall_slowdown", g[0], "overall_slowdown")
    check("guidance not_stated", classify_guidance(["4분기에도 수주는 이어질 것입니다."])[0], "not_stated")
    check("guidance positive", classify_guidance(["4분기에도 보조금 제외 흑자를 유지할 것으로 봅니다."])[0], "positive")
    check("guidance negative", classify_guidance(["4분기 보조금을 제외하면 적자가 예상됩니다."])[0], "negative")
    check("guidance Q1 period", classify_guidance(["1분기 수익성은 둔화될 것입니다."], period_re(1))[0], "overall_slowdown")
    comp_sent = next(s for s in sents if "보상금" in s)
    check("quote exact", quote(comp_sent, re.compile("일회성")).lstrip("."), COMP_QUOTE)
    st: dict = {}
    sent: list[str] = []

    def boom() -> dict:
        raise ValueError("invalid literal for int() with base 10: ''")
    for _ in range(2):
        r = safe_parse(boom, state=st, day="2026-11-03", ops_notify=sent.append)
    check("safe_parse once per day", (r, len(sent), st.get("ops_parse_day")), (None, 1, "2026-11-03"))
    safe_parse(boom, state=st, day="2026-11-04", ops_notify=sent.append)
    check("safe_parse next day", len(sent), 2)
    d: dict = {}
    safe_parse(boom, state=d, day="2026-11-03", ops_notify=sent.append, dry=True)
    check("safe_parse dry no state", d, {})
    check("safe_parse ok", safe_parse(lambda: {"op": 1}, state={}, day="x", ops_notify=sent.append), {"op": 1})
    print("earnings_parse selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(selftest() if sys.argv[1:2] == ["selftest"] else 2)
