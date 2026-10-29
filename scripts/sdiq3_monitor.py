#!/usr/bin/env python3
"""Samsung SDI 006400 earnings watcher.

Q3 2026 (part q3, the original one-shot, unchanged): (operating profit - AMPC) vs 1,600억,
compensation shown separately. The 1,600억 bar is Mirae Asset's Q3 OP estimate 3,023억 minus AMPC
1,420억, so the actual is compared the same way (OP - AMPC). Compensation is not in the bar; it is
shown on its own line and, when its amount is disclosed, it can only block an upgrade.
Peer 373220 print 2026-10-08 is context only. Sungrow 10GWh is not a confirmed order.

Q4 2026 (part q4, attached to the morning 09:00 / afternoon 15:20 / k203780 19:00 slots): reports the
disclosed OP, AMPC and OP - AMPC, and separately any GM / compensation / settlement amount the company
actually discloses for 2026-10-01 ~ 12-31. OP - AMPC - compensation is shown only when the disclosure
says explicitly that the amount is in operating profit. Absence is never read as zero, no broker
estimate is used, and the Q3 1,600억 bar and Q3 peer are not reused. The window 2027-01-04 ~ 03-31 is
an observation window, not a company-announced release date. Annual-only filings fail closed.
State lives under state["q4"]; the Q3 keys (result_notified, ...) are never touched by part q4.

Failures and missing numbers go to cwally-ops, never as a trading line.

CLI: sdiq3_monitor.py [auto|dry|force|selftest] [--part q3|q4|all] [--as-of "YYYY-MM-DD HH:MM"]
  default part is q3 (the existing one-shot step). --as-of is accepted only with dry.
  dry and selftest never publish and never write state/.
"""
from __future__ import annotations

import copy
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from naver_api import notices  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "sdiq3.json"
STATE_PATH = ROOT / "state" / "sdiq3.json"
DISCLOSURES = "https://m.stock.naver.com/domestic/stock/006400/notice"
AMOUNT = r"(-?\d[\d,]*)\s*(억원|백만원)"


def read_state() -> dict:
    if not STATE_PATH.exists():
        return {"result_notified": False, "empty_notified": False, "last_problem_key": None}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict, now: datetime) -> None:
    state["updated_at"] = now.isoformat(timespec="seconds")
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def is_q3_disclosure(item: dict) -> bool:
    title = item.get("title", "")
    body = item.get("text", "")
    return (
        "잠정" in title
        and "영업" in title
        and bool(re.search(r"삼성SDI|삼성에스디아이", title + body))
        and bool(re.search(r"2026-07-01\s*[~∼-]\s*2026-09-30|'26\.3Q|2026년\s*3분기", body))
    )


def num(raw: str) -> int:
    return int(raw.replace(",", ""))


def to_eok(value: int, unit: str) -> int:
    return round(value / 100) if unit == "백만원" else value


def table_unit(body: str) -> str:
    hit = re.search(r"단위\s*[:：]\s*(억원|백만원|천원|원)", body)
    return hit.group(1) if hit else "억원"


def table_value(body: str, label: str, unit: str) -> int | None:
    """First 당해실적 number after the row label, e.g. '영업이익 당해실적 2,038 -1,556 ...'."""
    hit = re.search(label + r"\s*당해실적\s*(-?\d[\d,]*)", body)
    if not hit or unit not in {"억원", "백만원"}:
        return None
    return to_eok(num(hit.group(1)), unit)


def note_amount(body: str, label: str, block: str | None = None) -> tuple[bool, int | None]:
    """(mentioned, amount in 억원) for a footnote like 'AMPC ... 세액 공제 금액(1,077억원)을 포함'."""
    mentioned = False
    for m in re.finditer(label, body):
        mentioned = True
        tail = body[m.end(): m.end() + 200]
        hit = re.search(AMOUNT, tail)
        if not hit:
            continue
        span = tail[: hit.start()]
        if block and re.search(block, span):
            continue  # the amount belongs to another item (e.g. AMPC inside a compensation sentence)
        return True, to_eok(num(hit.group(1)), hit.group(2))
    return mentioned, None


def parse_result(item: dict) -> dict:
    body = item["text"]
    unit = table_unit(body)
    sales = table_value(body, r"매출액", unit)
    op = table_value(body, r"영업이익", unit)
    ampc_seen, ampc = note_amount(body, r"AMPC|첨단\s*제조\s*생산\s*세액\s*공제", block=r"보상")
    comp_seen, comp = note_amount(body, r"보상금|보상\s*이익|일회성\s*보상", block=r"AMPC|세액\s*공제")
    if sales is not None and not 0 < sales < 200000:
        sales = None
    if op is not None and not -50000 < op < 50000:
        op = None
    if ampc is not None and not 0 <= ampc < 20000:
        ampc = None
    if comp is not None and not -20000 < comp < 20000:
        comp = None
    return {"sales": sales, "op": op, "ampc": ampc, "ampc_seen": ampc_seen, "comp": comp, "comp_seen": comp_seen}


def judge(p: dict, bar: int) -> tuple[str, int, str]:
    """(verdict, core = OP - AMPC, reason)."""
    core = p["op"] - p["ampc"]
    if core < bar:
        return "관망 유지", core, f"AMPC 제외 이익 {core:,}억원이 기준 {bar:,}억원 미만"
    if p["comp"] is not None and core - p["comp"] < bar:
        return "관망 유지", core, (
            f"AMPC 제외 {core:,}억원은 기준 이상이지만 보상금 {p['comp']:,}억원까지 빼면 "
            f"{core - p['comp']:,}억원으로 미만. 일회성 이익으로 넘은 것"
        )
    if p["comp_seen"] and p["comp"] is None:
        return "관망 유지", core, f"AMPC 제외 {core:,}억원은 기준 이상이지만 공시에 보상금이 언급되고 금액이 없어 상향 보류"
    return "관망 상향", core, f"AMPC 제외 이익 {core:,}억원이 기준 {bar:,}억원 이상"


def problem(cfg: dict, state: dict, now: datetime, kind: str, detail: str, dry: bool) -> None:
    key = f"{kind}:{detail}"
    if state.get("last_problem_key") == key:
        print(f"problem already reported: {kind}")
        return
    title = f"[SDI] 삼성SDI 3Q {kind} | 관망 유지"
    lines = [
        "이게 뭐 알림: 삼성SDI 006400 2026년 3분기 잠정 영업이익에서 AMPC를 뺀 값을 1,600억원과 비교하는 알림의 운영 안내.",
        f"지금 결과/상태: {kind}. {detail}. {now:%Y-%m-%d %H:%M} KST. 공시: {DISCLOSURES}",
        "지금 할 일: 관망 유지. 공시 원문을 직접 볼 때까지 비중을 바꾸지 마세요.",
        f"나중에: 다음 원샷 시각에 다시 확인합니다. 창은 {cfg['window_end']}까지입니다.",
        "익절/줄일 때: 숫자 미확인만으로 보유 비중을 줄이지 마세요.",
        "하지 말 것: AMPC나 보상금을 추정으로 채워 1,600억원과 비교하지 마세요.",
    ]
    topic = route_topic(resolve_topic("ops"), title)  # data/lookup problems are never trading signals
    if dry:
        print(f"[dry] topic={topic}", title, *lines, sep="\n")
        return
    publish(topic, title, "\n".join(lines), 3, ["warning"], resolve_server(cfg.get("ntfy_server")))
    state["last_problem_key"] = key
    save_state(state, now)


def result_message(cfg: dict, item: dict, p: dict, now: datetime) -> tuple[str, list[str]]:
    bar = int(cfg["bar_ex_eok"])
    verdict, core, reason = judge(p, bar)
    if p["comp"] is not None:
        comp_text = f"보상금 {p['comp']:,}억원(별도, 빼면 {core - p['comp']:,}억원)"
    elif p["comp_seen"]:
        comp_text = "보상금 언급 있음, 금액 미기재"
    else:
        comp_text = "보상금 공시 없음"
    sales = f"매출 {p['sales']:,}억원, " if p["sales"] is not None else ""
    title = f"[SDI] 삼성SDI 3Q {verdict} | AMPC 제외 {core:,}억원"
    if verdict == "관망 상향":
        action = "관망 상향. 추가 매수 신호는 아닙니다. 컨퍼런스콜에서 4분기 가이던스를 확인하세요."
    else:
        action = "관망 유지. 비중을 바꾸지 마세요."
    lines = [
        "이게 뭐 알림: 삼성SDI 006400 2026년 3분기 잠정. 영업이익에서 AMPC를 뺀 이익을 1,600억원과 비교.",
        (
            f"지금 결과/상태: {sales}영업이익 {p['op']:,}억원, AMPC {p['ampc']:,}억원, AMPC 제외 {core:,}억원 "
            f"vs 기준 {bar:,}억원. {comp_text}. 한 줄: {verdict} ({reason}). "
            f"공시 {item['id']}, 조회 {now:%Y-%m-%d %H:%M} KST. {DISCLOSURES}"
        ),
        f"지금 할 일: {action}",
        (
            "나중에: 기준 1,600억원은 미래에셋 추정이고 확정 컨센서스가 아닙니다. "
            "피어 373220은 10/8 잠정 매출 96,434억, 영업이익 7,560억, AMPC 4,169억, AMPC 제외 3,391억. "
            "보상금 금액은 그 공시에 없습니다. 선그로우 10GWh는 확정 수주가 아닙니다."
        ),
        "익절/줄일 때: AMPC 제외 이익이 적자로 정정되면 보유 비중을 재평가하세요.",
        "하지 말 것: 우선주 괴리, 목표주가, 미확정 수주만으로 이 판정을 바꾸지 마세요.",
    ]
    return title, lines



def run_q3(cfg: dict, mode: str, now: datetime) -> int:
    """Original Q3 one-shot flow, unchanged."""
    dry = mode == "dry"
    state = read_state()
    day = now.strftime("%Y-%m-%d")
    if now.year != int(cfg["year_guard"]) or not cfg["window_start"] <= day <= cfg["window_end"]:
        print("outside 2026 one-shot window; silent")
        return 0
    if state.get("result_notified") and mode != "force":
        print("result already notified; silent")
        return 0
    try:
        rows = notices(cfg["ticker"], 30)
    except Exception as exc:
        problem(cfg, state, now, "조회 실패", type(exc).__name__, dry)
        return 0
    if not rows:
        problem(cfg, state, now, "조회 실패", "공시 목록이 비었습니다", dry)
        return 0
    matches = [r for r in rows if is_q3_disclosure(r)]
    if not matches:
        if day >= cfg["window_end"] and not state.get("empty_notified"):
            problem(cfg, state, now, "미게시", "2026년 3분기 잠정 공시를 찾지 못했습니다", dry)
            if not dry:
                state["empty_notified"] = True
                save_state(state, now)
        else:
            print("Q3 disclosure not posted; silent")
        return 0
    item = matches[0]
    try:
        p = parse_result(item)
    except Exception as exc:
        problem(cfg, state, now, "숫자 미확인", f"공시 {item['id']} 파싱 오류 {type(exc).__name__}", dry)
        return 0
    if p["op"] is None or p["ampc"] is None:
        missing = [name for name, key in (("영업이익", "op"), ("AMPC", "ampc")) if p[key] is None]
        problem(cfg, state, now, "숫자 미확인", f"공시 {item['id']}에서 {', '.join(missing)} 금액 없음", dry)
        return 0
    title, lines = result_message(cfg, item, p, now)
    if dry:
        print(f"[dry] topic={resolve_topic('port')}", title, *lines, "dry-run; no ntfy", sep="\n")
        return 0
    publish(resolve_topic("port", cfg.get("ntfy_topic")), title, "\n".join(lines), 4,
            ["chart_with_upwards_trend"], resolve_server(cfg.get("ntfy_server")))
    state["result_notified"] = True
    state["last_key"] = item["id"]
    state["last_problem_key"] = None
    save_state(state, now)
    return 0


# ---------------------------------------------------------------------------------------------
# Q4 2026 (part q4). Everything below reads/writes only state["q4"].
# ---------------------------------------------------------------------------------------------
Q4_START, Q4_END = "2026-10-01", "2026-12-31"
Y26_START = "2026-01-01"
PERIOD_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\s*[~∼]\s*(\d{4}-\d{2}-\d{2})")
LABEL_RE = re.compile(r"\(\s*'?(\d{2})\.\s*(\d)\s*Q\s*\)|\(\s*'?(\d{2}|\d{4})\s*년\s*\)")
UNIT_RE = re.compile(r"단위\s*[:：]\s*(억\s*원|백만\s*원|천\s*원|원)")
UNIT_DIV = {"억원": 1, "백만원": 100, "천원": 100_000, "원": 100_000_000}
# Amount with a currency unit. Table cells have no unit, so this only reads note text.
AMT_RE = re.compile(
    r"(?P<neg>[-△▲](?=\d))?"
    r"(?:(?P<jo>\d[\d,]*(?:\.\d+)?)\s*조\s*(?:(?P<jeok>\d[\d,]*)\s*억\s*)?원"
    r"|(?P<n>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>억\s*원|백만\s*원|천\s*원|원))"
)
RANGE_BEFORE = re.compile(r"\d[\d,.]*\s*(?:조|억|백만|천)?\s*(?:원)?\s*[~∼]\s*$|\d\s*$")
# Keyword classes for note sentences. An amount belongs to the closest keyword before it.
KW_RE = re.compile(
    r"(?P<ampc>AMPC|45X|첨단\s*제조\s*생산\s*세액\s*공제|생산\s*세액\s*공제|세액\s*공제)"
    r"|(?P<comp>보상|정산|합의금|위약금|배상)"
    r"|(?P<other>매출액|매출|영업이익|영업손실|당기순|순이익|법인세|지분법|손상|충당|투자)"
)
# Period markers. Prior-year / other-quarter patterns come first so '25.4Q is not read as Q4.
MARK_RE = re.compile(
    r"(?P<other>'?25\.\s*\d\s*Q|2025\s*년(?:\s*\d\s*분기)?|2025\.\s*\dQ|'?26\.\s*[1-3]\s*Q|2026\s*년\s*[1-3]\s*분기"
    r"|(?<![\d.'])[1-3]\s*Q(?![A-Za-z])|(?<!\d)[1-3]\s*분기|전기\s*실적|전기\s*대비|직전\s*분기|전년\s*동기|전년도|전년"
    r"|당기\s*누계|당기\s*누적|누계|누적|연간|상반기|하반기|2026\s*년\s*연간)"
    r"|(?P<q4>'?26\.\s*4\s*Q|2026\s*년\s*4\s*분기|2026\.\s*4Q|2026-10-01\s*[~∼]\s*2026-12-31"
    r"|(?<![\d.'])4\s*Q(?![A-Za-z])|(?<!\d)4\s*분기|당기\s*실적|당분기|이번\s*분기|금번\s*분기|당기(?!순))"
)
NOT_ACTUAL_RE = re.compile(r"추정|예상|전망|컨센서스|증권사|목표")
NOTE_HEAD_RE = re.compile(r"기타\s*투자\s*판단과?\s*관련한?\s*중요\s*사항")


def _sentences(text: str) -> list[str]:
    """Note text split into bullets/sentences. No fixed-width window: an amount is only ever read
    from the same sentence as its keyword."""
    parts = re.split(r"\s+-\s+(?=\S)|※|(?<=[임음함다])\.\s+|\n", text)
    return [p.strip() for p in parts if p and p.strip()]


def _to_eok(raw: str, div: int) -> int:
    val = float(raw.replace(",", ""))
    return int(round(val / div))


def _amount_eok(m: re.Match) -> int:
    if m.group("jo"):
        val = float(m.group("jo").replace(",", "")) * 10000 + (float(m.group("jeok").replace(",", "")) if m.group("jeok") else 0)
        val = int(round(val))
    else:
        val = _to_eok(m.group("n"), UNIT_DIV[re.sub(r"\s", "", m.group("unit"))])
    return -val if m.group("neg") else val


def note_items(text: str, default_q4: bool) -> dict:
    """Read AMPC and compensation amounts for 2026 Q4 from note sentences.

    Returns {"ampc": [..], "comp": [{"amount","label","in_op","sentence"}], "ampc_seen", "comp_seen",
    "ampc_ambiguous": [reasons], "comp_ambiguous": [reasons]}. Amounts tied to another period
    (3Q, 전기, 누계, 연간, 2025 ...) or to another item (매출, 영업이익, AMPC inside a compensation
    sentence) are excluded. Ranges, '각각' lists, estimates and mixed periods fail closed.
    default_q4: a sentence without any period marker counts as Q4 (True for the Q4 results filing,
    False for other filings, which must name the quarter).
    """
    head = NOTE_HEAD_RE.search(text)
    notes = text[head.end():] if head else text
    out = {"ampc": [], "comp": [], "ampc_seen": False, "comp_seen": False,
           "ampc_ambiguous": [], "comp_ambiguous": []}
    for s in _sentences(notes):
        kws = [(m.start(), m.end(), m.lastgroup) for m in KW_RE.finditer(s)]
        kinds = {k for _, _, k in kws}
        if not kinds & {"ampc", "comp"}:
            continue
        marks = [(m.start(), m.lastgroup) for m in MARK_RE.finditer(s)]
        mark_kinds = {k for _, k in marks}
        if marks and mark_kinds == {"other"}:
            continue  # sentence is only about another period (prior quarter, 누계, 연간, 2025)
        if not marks and not default_q4:
            continue
        if "ampc" in kinds:
            out["ampc_seen"] = True
        if "comp" in kinds:
            out["comp_seen"] = True
        amts = list(AMT_RE.finditer(s))
        if not amts:
            continue
        estimate = bool(NOT_ACTUAL_RE.search(s))
        each = "각각" in s
        if each and len(amts) > 1:
            for k in kinds & {"ampc", "comp"}:
                out[f"{k}_ambiguous"].append("'각각' 나열: 어느 금액이 어느 항목인지 단정 불가")
            continue
        for a in amts:
            before = [k for k in kws if k[1] <= a.start()]
            owner = before[-1][2] if before else None
            prior = [k for p, k in marks if p < a.start()]
            if prior:
                period = prior[-1]
            elif mark_kinds == {"q4"} or (not marks and default_q4):
                period = "q4"
            else:
                period = "mixed"
            for k in ("ampc", "comp"):
                first = next((x for x in kws if x[2] == k), None)
                if first and a.start() > first[1] and owner == "other" and period != "other":
                    out[f"{k}_ambiguous"].append(f"{k} 문장에 다른 항목 금액 {a.group(0)}")
            if owner not in {"ampc", "comp"}:
                continue
            if period == "other":
                continue
            reasons = []
            if period == "mixed":
                reasons.append("기간 표시가 섞임")
            if each and len(amts) > 1:
                reasons.append("'각각' 나열")
            if estimate:
                reasons.append("추정/전망 표현")
            if RANGE_BEFORE.search(s[: a.start()]) or re.match(r"\s*[~∼]", s[a.end():]):
                reasons.append("범위 표기")
            if reasons:
                out[f"{owner}_ambiguous"].append(f"{a.group(0)}: {', '.join(reasons)}")
                continue
            val = _amount_eok(a)
            if owner == "ampc":
                out["ampc"].append(val)
            else:
                label = "GM 보상" if re.search(r"GM|제너럴\s*모터스|General\s*Motors", s) and "보상" in s else (
                    "정산" if "정산" in s else ("합의금" if "합의금" in s else "보상"))
                in_op = bool(re.search(r"영업이익", s) and re.search(r"포함|반영", s))
                out["comp"].append({"amount": val, "label": label, "in_op": in_op, "sentence": s[:160]})
    return out


def q4_period_status(body: str) -> tuple[str, str]:
    """('q4' | 'annual_q4col' | 'annual_only' | 'mismatch' | 'unknown' | 'other', detail)."""
    m = re.search(r"당기\s*실적\s*" + PERIOD_RE.pattern, body) or PERIOD_RE.search(body)
    unit_at = UNIT_RE.search(body)
    lab = LABEL_RE.search(body, unit_at.end() if unit_at else 0)
    label = None
    if lab:
        label = f"{lab.group(1)}.{lab.group(2)}Q" if lab.group(1) else f"{lab.group(3)[-2:]}Y"
    if not m:
        if label == "26.4Q" or re.search(r"2026\s*년\s*4\s*분기", body):
            return "unknown", "4분기로 보이지만 실적기간을 읽지 못함"
        return "other", "실적기간 없음"
    start, end = m.group(1), m.group(2)
    if (start, end) == (Q4_START, Q4_END):
        if label and label != "26.4Q":
            return "mismatch", f"실적기간 {start}~{end}인데 표 열 표시가 {label}"
        return "q4", f"실적기간 {start}~{end}"
    if (start, end) == (Y26_START, Q4_END):
        if label == "26.4Q":
            return "annual_q4col", f"실적기간 {start}~{end}, 당해실적 표 열이 '26.4Q로 명시"
        return "annual_only", f"실적기간 {start}~{end} 연간 숫자만 있고 4분기 숫자 없음"
    return "other", f"실적기간 {start}~{end}"


def table_cell(body: str, label: str) -> tuple[int | None, str | None, str | None]:
    """(raw value, unit, error) of the first 당해실적 cell of a row. Fails on unknown unit or on
    two different values for the same row."""
    u = UNIT_RE.search(body)
    if not u:
        return None, None, "표 단위 없음"
    unit = re.sub(r"\s", "", u.group(1))
    vals = set()
    for h in re.finditer(r"(?<![가-힣])" + label + r"\s*당해실적\s*(?P<v>[-△]?\d[\d,]*(?:\.\d+)?|-)(?=\s|$)", body):
        vals.add(h.group("v"))
    if not vals:
        return None, unit, f"{label} 행 없음"
    if len(vals) > 1:
        return None, unit, f"{label} 행 값이 여러 개 {sorted(vals)}"
    v = vals.pop()
    if v == "-":
        return None, unit, f"{label} 칸 비어 있음"
    neg = v[0] in "-△"
    eok = _to_eok(v.lstrip("-△"), UNIT_DIV[unit])
    return (-eok if neg else eok), unit, None


def parse_q4(item: dict) -> dict:
    body = item["text"]
    errs: list[str] = []
    sales, _, e1 = table_cell(body, "매출액")
    op, unit, e2 = table_cell(body, "영업이익")
    if e2:
        errs.append(e2)
    notes = note_items(body, default_q4=True)
    ampc = None
    distinct = sorted(set(notes["ampc"]))
    if notes["ampc_ambiguous"]:
        errs.append("AMPC 해석 불가: " + "; ".join(notes["ampc_ambiguous"][:3]))
    elif len(distinct) > 1:
        errs.append(f"AMPC 4분기 금액이 여러 개 {distinct}")
    elif distinct:
        ampc = distinct[0]
    if op is not None and not -50000 < op < 50000:
        errs.append(f"영업이익 {op}억원 범위 밖")
        op = None
    if sales is not None and not 0 < sales < 300000:
        sales = None
    if ampc is not None and not 0 <= ampc < 20000:
        errs.append(f"AMPC {ampc}억원 범위 밖")
        ampc = None
    comp = [c for c in notes["comp"] if -20000 < c["amount"] < 20000]
    return {"sales": sales, "op": op, "ampc": ampc, "unit": unit, "errors": errs,
            "ampc_seen": notes["ampc_seen"], "comp": comp, "comp_seen": notes["comp_seen"],
            "comp_ambiguous": notes["comp_ambiguous"]}


def is_q4_candidate(item: dict) -> bool:
    title = item.get("title", "")
    return bool(
        (("잠정" in title and "영업" in title) or "손익구조" in title)
        and re.search(r"삼성SDI|삼성에스디아이", title + item.get("text", ""))
    )


def extra_comp(rows: list[dict], skip_ids: set[str]) -> list[dict]:
    """Compensation amounts from other filings that name 2026 Q4 explicitly (e.g. a later
    기타 경영사항). A sentence without a Q4 marker is ignored there."""
    found = []
    for r in rows:
        if r.get("id") in skip_ids or not re.search(r"보상|정산|합의금|위약금|배상", r.get("text", "")):
            continue
        for c in note_items(r.get("text", ""), default_q4=False)["comp"]:
            found.append(dict(c, source=r.get("id")))
    return found


def comp_status(p: dict) -> str:
    if p["comp"]:
        return "amount"
    if p["comp_ambiguous"]:
        return "ambiguous"
    return "mentioned" if p["comp_seen"] else "none"


def q4_signature(item: dict, p: dict) -> str:
    comp = sorted((c["amount"], c["label"], c["in_op"]) for c in p["comp"])
    return json.dumps([p["op"], p["ampc"], p["sales"], comp, comp_status(p)], ensure_ascii=False)


def comp_lines(p: dict, core: int) -> tuple[str, str]:
    """(comp text, OP-AMPC-comp text or '')."""
    st = comp_status(p)
    if st == "amount":
        parts = [f"{c['label']} {c['amount']:,}억원" + ("(영업이익에 포함 명시)" if c["in_op"] else "(영업이익 포함 여부 명시 없음)")
                 for c in p["comp"]]
        text = "공시된 보상/정산 금액: " + ", ".join(parts)
        if all(c["in_op"] for c in p["comp"]):
            tot = sum(c["amount"] for c in p["comp"])
            return text, f"영업이익-AMPC-보상/정산 {core - tot:,}억원(공시 명시분만 차감)"
        return text, "영업이익-AMPC-보상/정산은 계산 안 함(영업이익 포함이 공시에 명시되지 않음)"
    if st == "ambiguous":
        return "보상/정산 관련 금액이 있으나 해석 불가(원문 확인 필요, 0으로 보지 않음)", ""
    if st == "mentioned":
        return "보상/정산은 언급됐지만 금액 미기재(0으로 보지 않음). 금액이 공시되면 후속 알림", ""
    return "보상/정산 금액은 이번 공시에 없음(0으로 보지 않음). 나중에 공시되거나 정정되면 후속 알림", ""


def q4_bar(q: dict) -> int | None:
    bar = q.get("bar_ex_eok")
    return int(bar) if isinstance(bar, int) and q.get("bar_as_of") and q.get("bar_source") else None


def q4_result_message(q: dict, item: dict, p: dict, status: str, now: datetime, follow: str | None = None) -> tuple[str, list[str]]:
    core = p["op"] - p["ampc"]
    ctext, minus = comp_lines(p, core)
    bar = q4_bar(q)
    if bar is None:
        verdict = "비중 유지"
        judge_txt = "4분기 기준선은 아직 정하지 않아 판정 없이 숫자만 전달(3분기 기준선과 3분기 피어 숫자는 쓰지 않음)"
        action = "관망 유지. 숫자만 확인하고 비중은 바꾸지 마세요. 컨퍼런스콜에서 2027년 1분기 가이던스와 보상/정산 설명을 확인하세요."
    else:
        verdict = "관망 상향" if core >= bar else "관망 유지"
        judge_txt = f"AMPC 제외 {core:,}억원 vs 4분기 기준 {bar:,}억원({q.get('bar_source')}, {q.get('bar_as_of')})"
        action = ("관망 상향. 추가 매수 신호는 아닙니다." if verdict == "관망 상향" else "관망 유지. 비중을 바꾸지 마세요.")
    sales = f"매출 {p['sales']:,}억원, " if p["sales"] is not None else ""
    basis = "4분기 분기 표" if status == "q4" else "연간 공시 안 '26.4Q 당해실적 열"
    head = f"[SDI] 삼성SDI 4Q {'후속 ' + follow if follow else '잠정'} | AMPC 제외 {core:,}억원, {verdict}"
    lines = [
        "이게 뭐 알림: 삼성SDI 006400 2026년 4분기(10/1~12/31) 잠정 실적. 공시된 영업이익, AMPC, 영업이익-AMPC와 보상/정산 금액을 따로 보여줍니다."
        + (f" 후속: {follow}." if follow else ""),
        (
            f"지금 결과/상태: {sales}영업이익 {p['op']:,}억원, AMPC {p['ampc']:,}억원, 영업이익-AMPC {core:,}억원. "
            f"{ctext}." + (f" {minus}." if minus else "") + f" {judge_txt}. "
            f"근거 {basis}, 공시 {item['id']}, 조회 {now:%Y-%m-%d %H:%M} KST. {DISCLOSURES}"
        ),
        f"지금 할 일: {action}",
        (
            f"나중에: 관찰 창 {q['window_start']}~{q['window_end']}은 회사가 발표한 일정이 아닙니다. "
            "보상/정산 금액이 나중에 공시되거나 숫자가 정정되면 한 번 더 알립니다."
        ),
        "익절/줄일 때: AMPC 제외 이익이 적자로 정정되면 보유 비중을 재평가하세요.",
        "하지 말 것: 증권사 추정 보상금이나 3분기 기준선으로 이 숫자를 판정하지 마세요. 우선주 006405 알림이 아닙니다.",
    ]
    return head, lines


def q4_problem_lines(q: dict, kind: str, detail: str, now: datetime) -> tuple[str, list[str]]:
    title = f"[SDI] 삼성SDI 4Q {kind} | 관망 유지"
    lines = [
        "이게 뭐 알림: 삼성SDI 006400 2026년 4분기 잠정 실적 확인 알림의 운영 안내. 매매 신호가 아닙니다.",
        f"지금 결과/상태: {kind}. {detail}. {now:%Y-%m-%d %H:%M} KST. 공시: {DISCLOSURES}",
        "지금 할 일: 관망 유지. 공시 원문을 직접 볼 때까지 비중을 바꾸지 마세요.",
        f"나중에: 다음 슬롯(09:00, 15:20, 19:00)에 다시 확인합니다. 관찰 창 {q['window_start']}~{q['window_end']}은 회사 발표 일정이 아닙니다.",
        "익절/줄일 때: 숫자 미확인만으로 보유 비중을 줄이지 마세요.",
        "하지 말 것: AMPC나 보상금을 추정으로 채우거나 연간 숫자로 4분기를 계산하지 마세요.",
    ]
    return title, lines


def run_q4(cfg: dict, state: dict, now: datetime, mode: str, fetch, send, save) -> list[str]:
    """One Q4 check. send(kind, title, lines) publishes (or prints in dry); save() persists state.
    Returns a list of event names for tests. In dry, send prints and save is a no-op."""
    q = cfg["q4"]
    st = state.setdefault("q4", {})
    events: list[str] = []
    day = now.strftime("%Y-%m-%d")
    if now.year != int(q["year_guard"]) or not q["window_start"] <= day <= q["window_end"]:
        print("q4: outside observation window; silent")
        return ["outside"]

    def problem_q4(kind: str, detail: str) -> None:
        key = f"{kind}:{detail}"
        if st.get("last_problem_key") == key:
            print(f"q4: problem already reported: {kind}")
            events.append("problem_dup")
            return
        title, lines = q4_problem_lines(q, kind, detail, now)
        send("ops", title, lines)
        st["last_problem_key"] = key
        save()
        events.append(f"problem:{kind}")

    try:
        rows = fetch()
    except Exception as exc:
        problem_q4("조회 실패", type(exc).__name__)
        return events
    if not rows:
        problem_q4("조회 실패", "공시 목록이 비었습니다")
        return events
    accepted, blockers = [], []
    for r in rows:
        if not is_q4_candidate(r):
            continue
        status, detail = q4_period_status(r.get("text", ""))
        if status in {"q4", "annual_q4col"}:
            accepted.append((r, status))
        elif status in {"annual_only", "mismatch", "unknown"}:
            blockers.append(f"공시 {r.get('id')} {detail}")
        else:
            print(f"q4: skip {r.get('id')} ({detail})")
    if not accepted:
        if blockers:
            problem_q4("해석 불가", blockers[0] + ". 4분기 숫자를 연간에서 계산하지 않음")
        elif now.strftime("%Y-%m-%d %H:%M") >= q["empty_after_kst"] and not st.get("empty_notified"):
            problem_q4("미게시", f"관찰 창 {q['window_start']}~{q['window_end']}에 2026년 4분기 잠정 공시를 찾지 못했습니다")
            st["empty_notified"] = True
            save()
        else:
            print("q4: disclosure not posted; silent")
            events.append("silent")
        return events
    item, status = accepted[0]  # newest first, so a 정정 supersedes the original
    try:
        p = parse_q4(item)
    except Exception as exc:
        problem_q4("숫자 미확인", f"공시 {item['id']} 파싱 오류 {type(exc).__name__}")
        return events
    missing = [n for n, k in (("영업이익", "op"), ("AMPC", "ampc")) if p[k] is None]
    if p["errors"] or missing:
        why = "; ".join(p["errors"] + ([f"{', '.join(missing)} 금액 없음"] if missing else []))
        problem_q4("숫자 미확인", f"공시 {item['id']}: {why}")
        return events
    ids = {r.get("id") for r, _ in accepted}
    for c in extra_comp(rows, ids):
        if (c["amount"], c["label"]) not in {(x["amount"], x["label"]) for x in p["comp"]}:
            p["comp"].append(c)
    sig = q4_signature(item, p)
    if not st.get("result_notified") or mode == "force":
        title, lines = q4_result_message(q, item, p, status, now)
        send("port", title, lines)
        st.update({"result_notified": True, "result_id": item["id"], "last_sig": sig,
                   "comp_status": comp_status(p), "last_problem_key": None})
        save()
        events.append("result")
        return events
    if sig == st.get("last_sig"):
        print("q4: result already notified, no change; silent")
        events.append("silent")
        return events
    reasons = []
    if item["id"] != st.get("result_id"):
        reasons.append(f"새 공시 {item['id']}(정정 가능)")
    if comp_status(p) == "amount" and st.get("comp_status") != "amount":
        reasons.append("보상/정산 금액 공개")
    if not reasons:
        reasons.append("숫자 변경")
    title, lines = q4_result_message(q, item, p, status, now, follow=", ".join(reasons))
    send("port", title, lines)
    st.setdefault("followups", []).append({"id": item["id"], "sig": sig, "at": now.strftime("%Y-%m-%d %H:%M")})
    st.update({"last_sig": sig, "result_id": item["id"], "comp_status": comp_status(p), "last_problem_key": None})
    save()
    events.append("followup")
    return events


def save_q4_state(state: dict, now: datetime) -> None:
    """Write state with only the q4 subkey changed against what is on disk now."""
    disk = read_state()
    disk["q4"] = dict(state.get("q4", {}), updated_at=now.isoformat(timespec="seconds"))
    STATE_PATH.write_text(json.dumps(disk, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main_q4(cfg: dict, mode: str, now: datetime) -> int:
    dry = mode == "dry"
    state = read_state()

    def send(kind: str, title: str, lines: list[str]) -> None:
        if kind == "ops":
            topic, prio, tags = route_topic(resolve_topic("ops"), title), 3, ["warning"]
        else:
            topic, prio, tags = resolve_topic("port", cfg.get("ntfy_topic")), 4, ["chart_with_upwards_trend"]
        if dry:
            print(f"[dry] topic={topic}", title, *lines, "dry-run; no ntfy", sep="\n")
            return
        publish(topic, title, "\n".join(lines), prio, tags, resolve_server(cfg.get("ntfy_server")))

    def save() -> None:
        if not dry:
            save_q4_state(state, now)

    run_q4(cfg, state, now, mode, lambda: notices(cfg["ticker"], 30), send, save)
    if dry:
        print("DRY RUN q4: no ntfy, no state write")
    return 0


# ---------------------------------------------------------------------------------------------
# selftest (fixtures only: no network, no ntfy, no state write)
# ---------------------------------------------------------------------------------------------
def _fx(period: str, label: str, unit: str, op: str, notes: str, sales: str = "52,000", title_extra: str = "") -> dict:
    text = (
        f"연결재무제표 기준 영업(잠정)실적(공정공시) 실적기간 당기실적 {period} 전기실적 2026-07-01 ~ 2026-09-30 "
        f"※ 동 정보는 잠정치로서 향후 확정치와는 다를 수 있음. 1. 연결실적내용 단위 : {unit}, % 구분 당기실적 전기실적 "
        f"({label}) ('26.3Q) 매출액 당해실적 {sales} 46,856 10.9 - 누계실적 - - - 영업이익 당해실적 {op} 4,078 -10.0 - "
        f"누계실적 - - - - 2. 정보제공내역 정보제공자 IR 4. 기타 투자판단과 관련한 중요사항 {notes} ※ 관련공시 -"
    )
    return {"id": f"fx{abs(hash(text)) % 10**6}", "title": f"삼성SDI(주) {title_extra}연결재무제표기준영업(잠정)실적(공정공시)",
            "datetime": "2027-01-28T08:00:00", "text": text}


def selftest() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    q = cfg["q4"]
    before_state = STATE_PATH.read_bytes() if STATE_PATH.exists() else None
    fails = []

    def check(name, got, want):
        ok = got == want
        print(("ok  " if ok else "FAIL"), name, "->", got, "" if ok else f"(want {want})")
        if not ok:
            fails.append(name)

    Q4P = "2026-10-01 ~ 2026-12-31"
    AMPC_NOTE = "- 상기 '26.4Q 매출액 52,000억원 및 영업이익 3,000억원은 미국 IRA AMPC 조항(45X)에 따른 세액 공제 금액(1,100억원)을 포함한 수치임."
    base = _fx(Q4P, "'26.4Q", "억원", "3,000", AMPC_NOTE)
    p = parse_q4(base)
    check("q4 period", q4_period_status(base["text"])[0], "q4")
    check("q4 op/ampc", (p["op"], p["ampc"], p["errors"]), (3000, 1100, []))
    check("q4 comp none", comp_status(p), "none")
    t, lines = q4_result_message(q, base, p, "q4", datetime(2027, 1, 28, 9, 0, tzinfo=KST))
    body = "\n".join(lines)
    check("msg core", "영업이익-AMPC 1,900억원" in body, True)
    check("msg absence not zero + follow-up", "0으로 보지 않음" in body and "후속" in body, True)
    check("msg no Q3 bar/peer", ("1,600" in t + body) or ("373220" in body), False)
    check("msg observation window wording", "회사가 발표한 일정이 아닙니다" in body, True)
    check("config q4 bar not reused", q.get("bar_ex_eok") != cfg.get("bar_ex_eok") and q4_bar(q) is None, True)

    annual = _fx("2026-01-01 ~ 2026-12-31", "'26년", "억원", "9,000", AMPC_NOTE.replace("'26.4Q", "'26년"))
    check("annual-only fails closed", q4_period_status(annual["text"])[0], "annual_only")
    annual4q = _fx("2026-01-01 ~ 2026-12-31", "'26.4Q", "억원", "3,000", AMPC_NOTE)
    check("annual with '26.4Q column accepted", q4_period_status(annual4q["text"])[0], "annual_q4col")
    q3real = _fx("2026-07-01 ~ 2026-09-30", "'26.3Q", "억원", "4,078", AMPC_NOTE.replace("4Q", "3Q"))
    check("wrong quarter ignored", q4_period_status(q3real["text"])[0], "other")
    check("Q4 period with 3Q label mismatch", q4_period_status(_fx(Q4P, "'26.3Q", "억원", "1", "")["text"])[0], "mismatch")
    # the Q3 matcher also hits a Q4 filing (전기실적 07-01~09-30), so the Q3 window must end before Q4 starts
    check("Q3 matcher unchanged, windows disjoint", (is_q3_disclosure(q3real), cfg["window_end"] < q["window_start"]), (True, True))

    neg = _fx(Q4P, "'26.4Q", "백만원", "-123,456", "- '26.4Q 영업손실은 AMPC 세액 공제 금액(112,340백만원)을 포함함.", sales="5,200,000")
    pn = parse_q4(neg)
    check("negative + 백만원", (pn["op"], pn["ampc"], pn["sales"]), (-1235, 1123, 52000))
    check("empty dash cell not read as negative next column",
          table_cell("단위 : 억원 영업이익 당해실적 - 4,078", "영업이익")[0], None)
    check("no unit fails closed", table_cell("영업이익 당해실적 3,000", "영업이익")[2], "표 단위 없음")

    comp_notes = (AMPC_NOTE + " - '26.4Q 영업이익에는 GM과의 정산이익 500억원이 포함되어 있으며, 이는 AMPC 1,100억원과 별개임."
                  " - '26.3Q 영업이익에는 고객사 보상금 800억원이 포함됨. - 연간 AMPC 세액 공제 금액은 4,500억원임.")
    pc = parse_q4(_fx(Q4P, "'26.4Q", "억원", "3,000", comp_notes))
    check("comp excludes AMPC/prior quarter/annual", ([c["amount"] for c in pc["comp"]], pc["ampc"], pc["errors"]), ([500], 1100, []))
    check("comp explicit in OP", pc["comp"][0]["in_op"], True)
    check("OP-AMPC-comp line", "1,400억원" in comp_lines(pc, 1900)[1], True)
    pm = parse_q4(_fx(Q4P, "'26.4Q", "억원", "3,000", AMPC_NOTE + " - '26.4Q 실적에는 일회성 보상금이 반영됨."))
    check("comp mentioned no amount", comp_status(pm), "mentioned")
    pr = parse_q4(_fx(Q4P, "'26.4Q", "억원", "3,000", AMPC_NOTE + " - 보상금은 증권사 추정 1,000억~1,500억원 수준."))
    check("range/estimate comp not used", (pr["comp"], comp_status(pr)), ([], "ambiguous"))
    pe = parse_q4(_fx(Q4P, "'26.4Q", "억원", "3,000", "- '26.4Q AMPC 및 보상금은 각각 1,100억원, 500억원임."))
    check("'각각' fails closed", (pe["ampc"], bool(pe["errors"])), (None, True))
    far = "- 상기 실적은 AMPC 세액 공제를 포함함. - 당사는 " + "가" * 150 + " 시설투자 2,000억원을 결정함."
    pf = parse_q4(_fx(Q4P, "'26.4Q", "억원", "3,000", far))
    check("no catch-all: far amount not AMPC", (pf["ampc"], pf["ampc_seen"]), (None, True))
    pmix = parse_q4(_fx(Q4P, "'26.4Q", "억원", "3,000", "- AMPC 세액 공제 금액은 4Q 1,100억원, 연간 4,500억원임."))
    check("AMPC with annual in same sentence", (pmix["ampc"], pmix["errors"]), (1100, []))

    # runner: dedupe under q4, Q3 keys untouched, follow-up on later explicit comp, dry no state
    sent: list[tuple] = []
    st = {"result_notified": True, "empty_notified": False, "last_problem_key": None, "last_key": "171079648"}
    q3_keys = copy.deepcopy(st)
    rows = [base, q3real]
    send = lambda k, t, l: sent.append((k, t))  # noqa: E731
    at = datetime(2027, 1, 28, 9, 0, tzinfo=KST)
    check("before window silent", run_q4(cfg, st, datetime(2026, 12, 15, 9, 0, tzinfo=KST), "auto", lambda: rows, send, lambda: None), ["outside"])
    check("first result", run_q4(cfg, st, at, "auto", lambda: rows, send, lambda: None), ["result"])
    check("dedupe", run_q4(cfg, st, at, "auto", lambda: rows, send, lambda: None), ["silent"])
    later = _fx(Q4P, "'26.4Q", "억원", "3,000", comp_notes, title_extra="[기재정정]")
    check("follow-up on explicit comp/correction", run_q4(cfg, st, at, "auto", lambda: [later] + rows, send, lambda: None), ["followup"])
    check("follow-up once", run_q4(cfg, st, at, "auto", lambda: [later] + rows, send, lambda: None), ["silent"])
    check("Q3 keys untouched", {k: st[k] for k in q3_keys}, q3_keys)
    check("q4 result sent to port", [k for k, _ in sent], ["port", "port"])
    st2: dict = {}
    run_q4(cfg, st2, at, "auto", lambda: [annual], send, lambda: None)
    check("annual-only -> ops 해석 불가", sent[-1][0] == "ops" and "해석 불가" in sent[-1][1], True)
    st3: dict = {}
    run_q4(cfg, st3, at, "auto", lambda: [_fx(Q4P, "'26.4Q", "억원", "3,000", "- 특이사항 없음.")], send, lambda: None)
    check("missing AMPC -> ops 숫자 미확인", sent[-1][0] == "ops" and "숫자 미확인" in sent[-1][1], True)
    st4: dict = {}
    check("미게시 at end", run_q4(cfg, st4, datetime(2027, 3, 31, 15, 20, tzinfo=KST), "auto", lambda: [q3real], send, lambda: None), ["problem:미게시"])
    check("after window silent", run_q4(cfg, {}, datetime(2027, 4, 1, 9, 0, tzinfo=KST), "auto", lambda: rows, send, lambda: None), ["outside"])
    check("route ops titles", route_topic("cwally-port", "[SDI] 삼성SDI 4Q 숫자 미확인 | 관망 유지"), "cwally-ops")
    check("--as-of rejected outside dry", _parse_args(["auto", "--as-of", "2027-01-28 09:00"])[0], "error")
    check("--part q4 parse", _parse_args(["auto", "--part", "q4"])[:2], ("auto", "q4"))
    check("default part q3", _parse_args(["auto"])[:2], ("auto", "q3"))
    after_state = STATE_PATH.read_bytes() if STATE_PATH.exists() else None
    check("state file unchanged", after_state == before_state, True)
    print(f"selftest: {'FAIL ' + ', '.join(fails) if fails else 'all ok'}")
    return 1 if fails else 0


def _parse_args(argv: list[str]) -> tuple[str, str, datetime | None]:
    mode, part, as_of = "auto", "q3", None
    pos = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--part", "--as-of") and i + 1 < len(argv):
            val = argv[i + 1]
            i += 2
            if a == "--part":
                part = val.lower()
            else:
                as_of = datetime.strptime(val, "%Y-%m-%d %H:%M").replace(tzinfo=KST)
            continue
        if a.startswith("--part="):
            part = a.split("=", 1)[1].lower()
        elif a.startswith("--as-of="):
            as_of = datetime.strptime(a.split("=", 1)[1], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        else:
            pos.append(a.lower())
        i += 1
    if pos:
        mode = pos[0]
    if mode not in {"auto", "dry", "force", "selftest"} or part not in {"q3", "q4", "all"}:
        return "error", part, as_of
    if as_of is not None and mode != "dry":
        return "error", part, as_of
    return mode, part, as_of


def main() -> int:
    mode, part, as_of = _parse_args(sys.argv[1:])
    if mode == "error":
        raise SystemExit(f"bad args {sys.argv[1:]}: mode auto|dry|force|selftest, --part q3|q4|all, --as-of only with dry")
    if mode == "selftest":
        return selftest()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = as_of or datetime.now(KST)
    rc = 0
    if part in {"q3", "all"}:
        rc |= run_q3(cfg, mode, now)
    if part in {"q4", "all"}:
        rc |= main_q4(cfg, mode, now)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
