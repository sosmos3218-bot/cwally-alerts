#!/usr/bin/env python3
"""FIG (Figma) Q3 2026 post-earnings one-shot. 2026-11-05 09:00 KST, once.

Reads the Q3 press release (SEC 8-K EX-99.1, IR page fallback) and scores:
revenue vs the 373-375M guidance, NDR vs 136%, AI credits (usage line + non-GAAP gross margin
as the AI cost proxy vs 85%), FCF margin vs 14%, FY2026 revenue guidance change.
Re-rating conditions: revenue meets/beats + NDR holds + AI cost offset + FCF improves -> positive,
otherwise caution. Year guard 2026 and date guard 2026-11-05 (GitHub cron has no year).

Modes: auto (scheduled) | force (ignore date/state, sends) | dry (print only, no ntfy, no state).
Local parser test: FIGQ3_TEST_TEXT=<file> python scripts/figq3_monitor.py dry
Not investment advice.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "figq3.json"
STATE_PATH = ROOT / "state" / "figq3.json"
UA = {"User-Agent": "cwally-alerts figq3 research monitor contact@example.com", "Accept-Encoding": "identity"}
RESULT_RE = re.compile(r"third quarter 2026 financial (results|highlights)", re.I)
NUM = r"\$?\(?([0-9][0-9,]*(?:\.[0-9]+)?)\)?"


# ---------- fetch ----------

def fetch(url: str) -> str:
    req = Request(url, headers=UA)
    with urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def textify(html: str) -> str:
    t = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    t = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = unescape(t).replace("\xa0", " ").replace("\u2019", "'")
    return re.sub(r"\s+", " ", t)


def sec_release(cfg: dict) -> tuple[str, str]:
    """Press release exhibit of the first 8-K filed on/after the earnings day."""
    data = json.loads(fetch(cfg["sources"]["sec"]))
    recent = data.get("filings", {}).get("recent", {})
    floor = cfg["filing_not_before"]
    cik_int = int(cfg["cik"])
    for form, day, acc in zip(recent.get("form", []), recent.get("filingDate", []), recent.get("accessionNumber", [])):
        if form not in {"8-K", "8-K/A"} or day < floor:
            continue
        base = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc.replace('-', '')}"
        names: list[str] = []
        try:
            idx = json.loads(fetch(base + "/index.json"))
            names = [i.get("name", "") for i in idx.get("directory", {}).get("item", [])]
        except Exception as exc:
            print(f"index fail {base}: {exc}")
        ex = [n for n in names if re.search(r"ex-?99|press", n, re.I) and n.lower().endswith((".htm", ".html"))]
        ex = ex or [cfg.get("sec_exhibit_guess", "q326pressrelease.htm")]
        for name in ex:
            try:
                body = textify(fetch(f"{base}/{name}"))
            except Exception as exc:
                print(f"exhibit fail {name}: {exc}")
                continue
            if RESULT_RE.search(body):
                return body, f"SEC 8-K {day} {base}/{name}"
    return "", ""


# ---------- parse ----------

def to_m(val: str, unit: str | None) -> float:
    v = float(val.replace(",", ""))
    return round(v * 1000, 1) if (unit or "").lower().startswith("b") else round(v, 1)


def pct(text: str, pattern: str) -> float | None:
    m = re.search(pattern, text, re.I)
    return float(m.group(1)) if m else None


def parse(text: str) -> dict:
    out: dict = {}
    m = re.search(r"\bRevenue was \$([0-9][0-9,]*(?:\.[0-9]+)?) (million|billion)(?:, up ([0-9]+(?:\.[0-9]+)?)% year-over-year)?", text, re.I)
    out["revenue_m"] = to_m(m.group(1), m.group(2)) if m else None
    out["revenue_yoy"] = float(m.group(3)) if m and m.group(3) else None
    out["ndr"] = pct(text, r"Net Dollar Retention Rate (?:was|of)\s*([0-9]+(?:\.[0-9]+)?)%")
    if out["ndr"] is None:
        out["ndr"] = pct(text, r"Net Dollar Retention Rate [a-z ]{0,30}?([0-9]+(?:\.[0-9]+)?)%")
    out["nongaap_gm"] = pct(text, r"non-GAAP gross margin was ([0-9]+(?:\.[0-9]+)?)%")
    out["fcf_margin"] = pct(text, r"Free Cash Flow Margin was \(?(-?[0-9]+(?:\.[0-9]+)?)\)?%")
    m = re.search(r"Free Cash Flow was \$\(?([0-9][0-9,]*(?:\.[0-9]+)?)\)? (million|billion)", text, re.I)
    out["fcf_m"] = to_m(m.group(1), m.group(2)) if m else None
    out["ai_weekly_pct"] = pct(text, r"([0-9]+(?:\.[0-9]+)?)% of Paid Customers with more than \$10,000 in ARR (?:were )?consuming AI credits")
    ai_lines = []
    for s in re.split(r"(?<=[.!?\u201d])\s+|\s\u2022\s", text):
        if re.search(r"AI credit", s, re.I) and not re.search(r"calculat|defined|forward-looking|Quarterly Report", s, re.I):
            s = s.strip(" \u2022\u201c\u201d\"")
            if s and s not in ai_lines:
                ai_lines.append(s[:180])
    out["ai_lines"] = ai_lines[:2]
    fy = re.search(r"Full Year 2026 Outlook:.{0,40}?Revenue between \$([0-9][0-9,]*(?:\.[0-9]+)?) (million|billion) and \$([0-9][0-9,]*(?:\.[0-9]+)?) (million|billion)", text, re.I)
    out["fy_low_m"] = to_m(fy.group(1), fy.group(2)) if fy else None
    out["fy_high_m"] = to_m(fy.group(3), fy.group(4)) if fy else None
    op = re.search(r"Full Year 2026 Outlook:.{0,400}?Non-GAAP operating income between \$([0-9][0-9,]*(?:\.[0-9]+)?) (million|billion) and \$([0-9][0-9,]*(?:\.[0-9]+)?) (million|billion)", text, re.I)
    out["fy_op_low_m"] = to_m(op.group(1), op.group(2)) if op else None
    out["fy_op_high_m"] = to_m(op.group(3), op.group(4)) if op else None
    return out


# ---------- judge ----------

def judge(p: dict, cfg: dict) -> dict:
    b, ln = cfg["baseline"], cfg["lines"]
    rev = p.get("revenue_m")
    if rev is None:
        rev_s = None
    elif rev > b["q3_guidance_high_m"]:
        rev_s = "상회"
    elif rev >= b["q3_guidance_low_m"]:
        rev_s = "부합"
    else:
        rev_s = "미달"
    ndr = p.get("ndr")
    ndr_s = None if ndr is None else ("유지" if ndr >= ln["ndr_hold"] else "하락")
    gm = p.get("nongaap_gm")
    ai_s = None if gm is None else ("상쇄" if gm >= ln["ai_cost_gross_margin"] else "부담")
    fcf = p.get("fcf_margin")
    fcf_s = None if fcf is None else ("개선" if fcf > ln["fcf_margin_improve_over"] else "개선 없음")
    lo, hi = p.get("fy_low_m"), p.get("fy_high_m")
    if lo is None or hi is None:
        fy_s = None
    else:
        mid_new = (lo + hi) / 2
        mid_old = (b["fy_guidance_low_m"] + b["fy_guidance_high_m"]) / 2
        fy_s = "상향" if mid_new > mid_old + 0.5 else ("하향" if mid_new < mid_old - 0.5 else "유지")
    core = [rev_s, ndr_s, ai_s, fcf_s]
    if all(core) and rev_s in {"상회", "부합"} and ndr_s == "유지" and ai_s == "상쇄" and fcf_s == "개선":
        verdict = "긍정"
    elif any(s is None for s in core) and not any(s in {"미달", "하락", "부담", "개선 없음"} for s in core):
        verdict = "보류"
    else:
        verdict = "신중"
    return {"rev": rev_s, "ndr": ndr_s, "ai": ai_s, "fcf": fcf_s, "fy": fy_s, "verdict": verdict}


def f(v, suffix: str = "") -> str:
    return "원문 확인" if v is None else f"{v:g}{suffix}"


def body(cfg: dict, now: datetime, p: dict, j: dict, source: str) -> str:
    b, lv = cfg["baseline"], cfg["levels"]
    lab = lambda s: s or "원문 확인"  # noqa: E731
    fy_txt = "원문 확인"
    if p.get("fy_low_m") is not None:
        fy_txt = f"{p['fy_low_m']:g}-{p['fy_high_m']:g}백만달러, 기존 {b['fy_guidance_low_m']:g}-{b['fy_guidance_high_m']:g}. {lab(j['fy'])}"
    op_txt = ""
    if p.get("fy_op_low_m") is not None:
        op_txt = f" 비GAAP 영업이익 {p['fy_op_low_m']:g}-{p['fy_op_high_m']:g}백만달러 (기존 {b['fy_op_income_low_m']:g}-{b['fy_op_income_high_m']:g})."
    ai_use = f"주간 AI 크레딧 사용 고객 {f(p.get('ai_weekly_pct'), '%')} (2분기 {b['q2_ai_weekly_pct']}% 이상)"
    ai_quote = f" 원문: \"{p['ai_lines'][0]}\"" if p.get("ai_lines") else ""
    verdict = j["verdict"]
    if verdict == "긍정":
        status_head = "밸류 재평가(리레이팅) 조건 4개 모두 충족."
        action = f"긍정 신호입니다. 새 돈은 나눠서만, 장 초반 갭 상승 추격은 하지 마세요. {lv['chase']}달러 이상이면 신규 추격 금지."
        trim = f"{lv['chase']}달러 이상 급등이면 일부 차익을 고려. 다음 분기 NDR이 꺾이면 비중 축소."
    elif verdict == "보류":
        status_head = "일부 숫자를 원문에서 못 읽었습니다. 읽은 항목에는 미달이 없습니다."
        action = "판단 보류. 출처 링크에서 빈 칸(원문 확인)을 직접 보고 결정하세요."
        trim = "이번 알림만으로 줄이지 마세요."
    else:
        status_head = "재평가 조건 4개 중 하나 이상 미충족."
        action = "추가매수 보류. 이미 가진 분할 계획만 유지하고 실적 직후 반등에 따라 사지 마세요."
        trim = f"매출 미달이나 NDR 하락이면 반등 때 비중 축소를 고려. {lv['split']}달러 이하는 장기 분할 관심 구간입니다."
    if j.get("fy") == "하향":
        action += " 연간 가이던스 하향이 함께 나왔습니다. 더 보수적으로 보세요."
    lines = [
        f"{now.strftime('%Y-%m-%d %H:%M KST')} | FIG | 3분기 실적 요약",
        "이게 뭐 알림: 피그마 3분기 실적(11/04 미국 장 마감 후) 다음 날 1회 요약입니다. 매수 지시가 아닙니다.",
        f"지금 결과/상태: {verdict}. {status_head}",
        f"매출 {f(p.get('revenue_m'), '백만달러')} (가이던스 {b['q3_guidance_low_m']:g}-{b['q3_guidance_high_m']:g}, 전년비 {f(p.get('revenue_yoy'), '%')}): {lab(j['rev'])}.",
        f"NDR(순달러유지율) {f(p.get('ndr'), '%')} (2분기 {b['q2_ndr']}%): {lab(j['ndr'])}.",
        f"AI 크레딧: {ai_use}. AI 비용 상쇄는 비GAAP 매출총이익률로 봅니다 {f(p.get('nongaap_gm'), '%')} (2분기 {b['q2_nongaap_gross_margin']}%): {lab(j['ai'])}.{ai_quote}",
        f"FCF(잉여현금흐름) 마진 {f(p.get('fcf_margin'), '%')}, FCF {f(p.get('fcf_m'), '백만달러')} (2분기 {b['q2_fcf_margin']}%): {lab(j['fcf'])}.",
        f"연간 매출 가이던스: {fy_txt}.{op_txt}",
        f"지금 할 일: {action}",
        "나중에: 이 알림은 다시 오지 않습니다. 평일 09:00 [FIG-PX] 가격 구간 알림은 계속됩니다. 다음 분기 NDR과 AI 크레딧 매출 공개 여부를 보세요.",
        f"익절/줄일 때: {trim}",
        "하지 말 것: 원문 확인 칸을 충족이나 미달로 읽기, 신용 및 미수, 실적 직후 시초가 일괄 매수.",
        f"출처: {source or '없음'}",
    ]
    return "\n".join(lines)


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    dry = mode == "dry"
    if dry:
        print("DRY RUN")
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    today = now.date()
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    state = {"updated_at": None, "sent": False, "sent_at": None, "verdict": None, "source": None, "parsed": None, "fail_notified": False}
    if STATE_PATH.exists():
        try:
            state.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass

    test_file = os.environ.get("FIGQ3_TEST_TEXT")
    start = date.fromisoformat(cfg["window_start"])
    end = date.fromisoformat(cfg["window_end"])
    in_window = today.year == int(cfg["oneshot_year"]) and start <= today <= end
    if mode not in {"force", "dry"} and not in_window:
        print(f"outside {start}~{end} (year {cfg['oneshot_year']}); silent")
        return 0
    if state.get("sent") and mode not in {"force", "dry"}:
        print("already sent; skip")
        return 0

    text, source = "", ""
    if test_file:
        text, source = textify(Path(test_file).read_text(encoding="utf-8", errors="ignore")), f"test {test_file}"
    else:
        try:
            text, source = sec_release(cfg)
        except Exception as exc:
            print(f"sec fail: {exc}")
        if not text:
            try:
                raw = textify(fetch(cfg["sources"]["ir"]))
                if RESULT_RE.search(raw):
                    text, source = raw, cfg["sources"]["ir"]
            except Exception as exc:
                print(f"ir fail: {exc}")

    parsed: dict = {}
    if text:
        parsed = parse(text)
        j = judge(parsed, cfg)
        titles = {
            "긍정": "[FIG-Q3] 재평가 조건 충족 | 분할만, 추격 금지",
            "신중": "[FIG-Q3] 재평가 조건 미충족 | 신중, 추가매수 보류",
            "보류": "[FIG-Q3] 일부 항목 원문 확인 | 판단 보류",
        }
        title = titles[j["verdict"]]
        msg = body(cfg, now, parsed, j, source)
        pri = 4
        state.update({"sent": True, "sent_at": now.isoformat(), "verdict": j["verdict"], "source": source})
    else:
        if state.get("fail_notified") and mode not in {"force", "dry"}:
            print("release not found; fail notice already sent")
            return 0
        title = "[FIG-Q3] 실적 원문 조회 실패 | 직접 확인"
        msg = "\n".join([
            f"{now.strftime('%Y-%m-%d %H:%M KST')} | FIG | 3분기 실적 요약",
            "이게 뭐 알림: 피그마 3분기 실적 원문(SEC 8-K, IR)을 찾지 못했습니다.",
            "지금 결과/상태: 조회 실패. 실적 미달이나 충족이 아닙니다.",
            "지금 할 일: investor.figma.com 또는 SEC EDGAR(FIG)에서 3분기 보도자료를 직접 보세요.",
            "나중에: 자동 재시도는 없습니다. 필요하면 figq3 스크립트를 force로 한 번만 다시 돌리세요.",
            "익절/줄일 때: 이번 알림은 매매 신호가 아닙니다.",
            "하지 말 것: 조회 실패를 실적 미달로 보기.",
        ])
        pri = 3
        state["fail_notified"] = True

    print(title)
    print(msg)
    print(f"topic={topic} send={not dry}")
    if not dry:
        publish(topic, title, msg, pri, ["chart_with_upwards_trend"], server)
        state.update({"updated_at": now.isoformat(), "parsed": parsed or state.get("parsed")})
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
