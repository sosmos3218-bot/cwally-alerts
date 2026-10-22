#!/usr/bin/env python3
"""SPCX 2026 Q3 10-Q: customer concentration + non-cancelable commitments vs Q2. Sends once.

Runs in the weekday 09:00 KST morning slot. Silent until EDGAR lists a 10-Q for
period 2026-09-30, then compares the concentration table, the cloud-contract
termination clause and the unconditional-obligations total with the Q2 10-Q
baseline in configs/spcx10q.json and sends the implications once. Year-guarded.

modes: auto (default) | dry (print only: no ntfy, no state write) | force (resend)
       dry <file.htm> [period label]  parses a local filing instead of EDGAR (wiring test)
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "spcx10q.json"
STATE_PATH = ROOT / "state" / "spcx10q.json"
UA = {"User-Agent": "cwally-alerts research@example.com"}
PCT = re.compile(r"[0-9]+(?:\.[0-9]+)?\s*%|\*")
CUSTOMER = re.compile(r"Customer ([A-Z])((?:\s+(?:[0-9]+(?:\.[0-9]+)?\s*%|\*)){2,4})")
TERMINATION = re.compile(r"terminated by either party upon (\d+) days")


def read_state() -> dict:
    base = {"result_notified": False, "last_problem_key": None, "accession": None}
    if STATE_PATH.exists():
        try:
            base.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return base


def save_state(state: dict, now: datetime) -> None:
    state["updated_at"] = now.isoformat(timespec="seconds")
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=40) as resp:
        return resp.read().decode("utf-8", "replace")


def plain(raw: str) -> str:
    text = re.sub(r"(?is)<script.*?>.*?</script>", " ", raw)
    text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text).replace("’", "'").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text)


def find_10q(cfg: dict) -> dict | None:
    """Newest 10-Q (or 10-Q/A) whose reportDate is the configured period end."""
    cik = cfg["cik"]
    recent = json.loads(fetch(f"https://data.sec.gov/submissions/CIK{cik}.json"))["filings"]["recent"]
    for i, form in enumerate(recent.get("form") or []):
        if form not in ("10-Q", "10-Q/A") or recent["reportDate"][i] != cfg["period_end"]:
            continue
        acc = recent["accessionNumber"][i]
        url = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
               f"{acc.replace('-', '')}/{recent['primaryDocument'][i]}")
        return {"form": form, "accession": acc, "filed": recent["filingDate"][i], "url": url}
    return None


def num(s: str) -> float | None:
    s = s.replace(",", "").replace("$", "").strip()
    return float(s) if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", s) else None


def parse_concentration(text: str) -> dict:
    """{'q': {'A': 18.3, ...}, 'ytd': {...}} from the 'Concentration of risk' table."""
    at = text.find("Concentration of risk")
    if at < 0:
        return {"q": {}, "ytd": {}}
    q, ytd = {}, {}
    for letter, cells in CUSTOMER.findall(text[at:at + 2500]):
        vals = [None if c == "*" else float(c.replace("%", "").strip()) for c in PCT.findall(cells)]
        if letter in q:
            continue
        q[letter] = vals[0]
        if len(vals) >= 3:
            ytd[letter] = vals[2]
    return {"q": q, "ytd": ytd}


def parse_commitments(text: str, label: str) -> dict:
    """Total and next-year amounts (USD millions) of the non-cancelable commitments table."""
    hit = re.search(
        r"non-cancelable contractual commitments as of " + re.escape(label) + r":?(.{0,600}?)Total \$ ?([0-9,]+)",
        text,
    )
    if not hit:
        return {"total": None, "rows": {}}
    rows = {}
    for year, amount in re.findall(r"(20\d\d|Thereafter)(?: \([^)]*\))? \$? ?([0-9,]+|—)", hit.group(1)):
        rows.setdefault(year, num(amount) or 0.0)
    return {"total": num(hit.group(2)), "rows": rows}


def capex_sentence(text: str) -> str | None:
    hit = re.search(r"[^.]{0,160}\bcapital expenditures\b[^.]{0,200}\$ ?[0-9][^.]{0,160}\.", text)
    return hit.group(0).strip()[:220] if hit else None


def concentration_view(cfg: dict, conc: dict, days: int | None) -> tuple[str, list[str], str]:
    r, base = cfg["rules"], cfg["q2"]
    q = {k: v for k, v in conc["q"].items() if v is not None}
    notes = []
    if not q:
        return "미확인", ["고객 집중도 표를 읽지 못했습니다. 10-Q 원문의 'Concentration of risk' 표를 직접 보세요."], "표 미확인"
    top_letter = max(q, key=q.get)
    top, prev_top = q[top_letter], max(base["customer_q"].values())
    step = top - prev_top
    if top >= r["conc_warn_pct"]:
        word = "심화"
        notes.append(f"한 고객(고객 {top_letter})이 분기 매출의 {top:.1f}%입니다. 계약이 90일 통지로 끝날 수 있어, 고객 1곳 이탈만으로 분기 매출의 4분의 1 안팎이 흔들릴 수 있습니다.")
    elif step >= r["conc_step_pp"]:
        word = "심화"
        notes.append(f"최대 고객 비중이 2분기 {prev_top:.1f}%에서 {top:.1f}%로 {step:+.1f}%p 올랐습니다. 성장이 소수 고객에 더 기대고 있습니다.")
    elif -step >= r["conc_step_pp"]:
        word = "완화"
        notes.append(f"최대 고객 비중이 2분기 {prev_top:.1f}%에서 {top:.1f}%로 {step:+.1f}%p 내려 분산이 진전됐습니다.")
    else:
        word = "유지"
        notes.append(f"최대 고객 비중 {top:.1f}%로 2분기({prev_top:.1f}%)와 비슷합니다. 집중 위험은 줄지도 늘지도 않았습니다.")
    new = sorted(set(q) - set(base["customer_q"]))
    gone = sorted(set(base["customer_q"]) - set(q))
    if new:
        notes.append(f"매출 10% 넘는 고객이 새로 생겼습니다(고객 {', '.join(new)}). 분산 신호지만 대형 고객 몇 곳에 기대는 구조는 그대로입니다.")
    if gone:
        notes.append(f"고객 {', '.join(gone)}가 10% 아래로 내려갔습니다. 계약 축소인지 다른 매출이 커진 결과인지 원문에서 확인하세요.")
    if days is None:
        notes.append("클라우드 계약 해지 조항 문구를 찾지 못했습니다. 위험요인 문구가 바뀌었을 수 있습니다.")
    elif days != base["termination_days"]:
        better = days > base["termination_days"]
        notes.append(f"계약 해지 통지 기간이 {base['termination_days']}일에서 {days}일로 바뀌었습니다({'고객 이탈 위험 완화' if better else '고객 이탈 위험 확대'}).")
        if not better and word == "유지":
            word = "심화"
    shown = ", ".join(f"{k} {v:.1f}%" for k, v in sorted(q.items()))
    return word, notes, shown


def commitment_view(cfg: dict, com: dict) -> tuple[str, list[str], str]:
    r, base = cfg["rules"], cfg["q2"]
    total = com["total"]
    if total is None:
        return "미확인", ["취소 불가 약정 표를 읽지 못했습니다. 10-Q 원문의 Commitments and Contingencies 주석을 직접 보세요."], "표 미확인"
    ratio = total / base["commit_total_musd"]
    cash_pct = total / cfg["q3_cash_and_securities_musd"] * 100.0
    if ratio >= r["commit_surge_ratio"]:
        word = "급증"
    elif ratio >= r["commit_up_ratio"]:
        word = "증가"
    elif ratio <= r["commit_down_ratio"]:
        word = "감소"
    else:
        word = "유지"
    notes = []
    if word in ("급증", "증가"):
        notes.append(f"취소 불가 약정이 2분기의 {ratio:.2f}배입니다. {cfg['q4_capex_guide']}와 같은 방향으로 투자가 더 커지고 있습니다.")
    elif word == "감소":
        notes.append(f"취소 불가 약정이 2분기의 {ratio:.2f}배로 줄었습니다. 이미 대금을 내서 줄어든 것일 수 있어, 투자 속도 조절로 바로 읽지 마세요.")
    else:
        notes.append(f"취소 불가 약정이 2분기의 {ratio:.2f}배로 비슷합니다.")
    if cash_pct >= r["commit_cash_warn_pct"]:
        notes.append(f"약정 합계가 현금과 유가증권의 {cash_pct:.0f}%입니다. 영업현금흐름만으로 메우기 어려우면 증자나 차입이 필요하고, 증자라면 주식 가치가 희석됩니다.")
    next_year = com["rows"].get("2027")
    shown = f"${total / 1000:,.1f}B (2분기 ${base['commit_total_musd'] / 1000:,.1f}B)"
    if next_year is not None:
        shown += f", 이 중 2027년 ${next_year / 1000:,.1f}B (2분기 ${base['commit_next_year_musd'] / 1000:,.1f}B)"
    return word, notes, shown + f", 현금+유가증권 대비 {cash_pct:.0f}%"


def send(cfg: dict, title: str, lines: list[str], priority: int) -> None:
    publish(resolve_topic("port", cfg.get("ntfy_topic")), title, "\n".join(lines), priority,
            ["rocket", "chart_with_upwards_trend"], resolve_server(cfg.get("ntfy_server")))


def problem(cfg: dict, state: dict, now: datetime, kind: str, detail: str, dry: bool) -> None:
    key = f"{kind}:{detail}"
    if state.get("last_problem_key") == key:
        print(f"problem already reported: {kind}")
        return
    title = f"[SPCX10Q] SPCX 3Q 10-Q {kind} | 점검 유보"
    lines = [
        "이게 뭐 알림: SPCX 3분기 10-Q에서 고객 집중도와 투자 약정만 2분기와 비교하는 일회 알림입니다.",
        f"지금 결과/상태: {kind}. {detail}. 조회 {now:%Y-%m-%d %H:%M} KST. EDGAR: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cfg['cik']}&type=10-Q",
        "지금 할 일: 원문을 직접 확인하기 전에는 판단을 바꾸지 마세요.",
        f"나중에: 평일 09:00에 다시 확인합니다. 마지막 확인은 {cfg['window_end'][5:].replace('-', '/')} 09:00 KST입니다.",
        "익절/줄일 때: 가격 기준은 [SPCX] 알림을 따르세요.",
        "하지 말 것: 자료 누락이나 조회 실패를 매매 신호로 읽지 마세요.",
    ]
    if dry:
        print("DRY RUN (problem)", title, *lines, sep="\n")
        return
    send(cfg, title, lines, 3)
    state["last_problem_key"] = key
    save_state(state, now)


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode not in {"auto", "dry", "force"}:
        raise SystemExit(f"unknown mode: {mode}")
    dry = mode == "dry"
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = read_state()
    now = datetime.now(KST)
    day = now.strftime("%Y-%m-%d")
    local = sys.argv[2] if dry and len(sys.argv) > 2 else None
    if dry:
        print("DRY RUN: no ntfy, no state write")
    if not local and (now.year != int(cfg["year_guard"]) or not (cfg["window_start"] <= day <= cfg["window_end"])):
        if not dry:
            print("outside 2026 window; silent")
            return 0
        print("outside window; dry run continues for wiring check")
    if state.get("result_notified") and mode == "auto":
        print("result already notified; silent")
        return 0

    if local:
        filing = {"form": "local", "accession": "local", "filed": "-", "url": local}
        label = sys.argv[3] if len(sys.argv) > 3 else cfg["period_label"]
        raw = Path(local).read_text(encoding="utf-8", errors="replace")
    else:
        label = cfg["period_label"]
        try:
            filing = find_10q(cfg)
        except Exception as exc:
            problem(cfg, state, now, "조회 실패", f"EDGAR 제출 목록 {type(exc).__name__}", dry)
            return 0
        if not filing:
            final = now >= datetime.strptime(cfg["final_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
            if final:
                problem(cfg, state, now, "미게시", f"{cfg['period_end']} 분기 10-Q가 EDGAR에 아직 없습니다", dry)
            else:
                print("10-Q not filed yet; silent")
            return 0
        try:
            raw = fetch(filing["url"])
        except Exception as exc:
            problem(cfg, state, now, "조회 실패", f"10-Q 원문 {type(exc).__name__}", dry)
            return 0
    text = plain(raw)
    conc = parse_concentration(text)
    term = TERMINATION.search(text)
    days = int(term.group(1)) if term else None
    com = parse_commitments(text, label)
    print(f"filing={filing['accession']} conc={conc} termination_days={days} commitments={com}")
    if not conc["q"] and com["total"] is None:
        problem(cfg, state, now, "숫자 미확인", f"10-Q {filing['accession']}에서 고객 표와 약정 표를 둘 다 읽지 못했습니다", dry)
        return 0

    cw, c_notes, c_shown = concentration_view(cfg, conc, days)
    mw, m_notes, m_shown = commitment_view(cfg, com)
    worse = cw == "심화" or mw in ("급증", "증가")
    act = "위험 재점검" if worse else "변화 작음"
    title = f"[SPCX10Q] 고객 집중 {cw}, 투자 약정 {mw} | {act}"
    ytd = ", ".join(f"{k} {v:.1f}%" for k, v in sorted(conc["ytd"].items()) if v is not None)
    base = cfg["q2"]
    capex = capex_sentence(text)
    lines = [
        "이게 뭐 알림: SPCX 3분기 10-Q에서 고객 집중도와 취소 불가 투자 약정만 2분기 10-Q와 비교한 결과입니다.",
        (f"지금 결과/상태: 매출 10% 넘는 고객 분기 비중 {c_shown} (2분기 A {base['customer_q']['A']}%, B {base['customer_q']['B']}%)"
         + (f", 9개월 누적 {ytd}" if ytd else "")
         + f". 계약 해지 통지 {str(days) + '일' if days else '문구 미확인'} (2분기 {base['termination_days']}일)."
         + f" 취소 불가 약정 {m_shown}."
         + f" {filing['form']} 제출 {filing['filed']}, 원문 {filing['url']}"),
        "지금 할 일: " + " ".join(c_notes + m_notes),
        "나중에: 4분기 실적과 10-K에서 같은 두 항목, 그리고 대규모 자금조달이 증자인지 차입인지 확인하세요."
        + (f" 10-Q 캡엑스 문구: {capex}" if capex else ""),
        "익절/줄일 때: 가격 기준은 [SPCX] 알림을 따르세요. 이 알림은 위험 점검용입니다.",
        "하지 말 것: 이 숫자 하나로 매매하지 마세요. 고객 이름을 추정하거나 ARR 발언을 계약 잔고로 읽지 마세요.",
    ]
    if dry:
        print("DRY RUN (result)", title, *lines, "dry-run; no ntfy", sep="\n")
        return 0
    send(cfg, title, lines, 4 if worse else 3)
    state.update({
        "result_notified": True,
        "last_problem_key": None,
        "accession": filing["accession"],
        "filed": filing["filed"],
        "customer_q": conc["q"],
        "customer_ytd": conc["ytd"],
        "termination_days": days,
        "commit_total_musd": com["total"],
        "concentration": cw,
        "commitments": mw,
    })
    save_state(state, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
