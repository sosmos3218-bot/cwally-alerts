#!/usr/bin/env python3
"""Samsung SDI 2026 Q3 preliminary: (operating profit - AMPC) vs 1,600억, compensation shown separately.

Basis: the 1,600억 bar is Mirae Asset's Q3 OP estimate 3,023억 minus AMPC 1,420억, so the actual is
compared the same way (OP - AMPC). Compensation is not in the bar; it is shown on its own line and,
when its amount is disclosed, it can only block an upgrade (one-off income never lifts the verdict).
Failures and missing numbers go to cwally-ops, never as a trading line.
"""
from __future__ import annotations

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
        "나중에: 기준 1,600억원은 미래에셋 추정(영업이익 3,023억원 - AMPC 1,420억원)이고 확정 컨센서스가 아닙니다. 확정 실적에서 다시 보세요.",
        "익절/줄일 때: AMPC 제외 이익이 적자로 정정되면 보유 비중을 재평가하세요.",
        "하지 말 것: 우선주 괴리나 목표주가만으로 이 판정을 바꾸지 마세요.",
    ]
    return title, lines


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode not in {"auto", "dry", "force"}:
        raise SystemExit(f"unknown mode: {mode}")
    dry = mode == "dry"
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = read_state()
    now = datetime.now(KST)
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


if __name__ == "__main__":
    raise SystemExit(main())
