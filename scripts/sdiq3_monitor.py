#!/usr/bin/env python3
"""Samsung SDI 2026 Q3 preliminary: OP minus compensation and AMPC vs 1,600억."""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from naver_api import notices  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "sdiq3.json"
STATE_PATH = ROOT / "state" / "sdiq3.json"
DISCLOSURES = "https://m.stock.naver.com/domestic/stock/006400/notice"


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
        bool(re.search(r"잠정", title))
        and bool(re.search(r"영업", title))
        and bool(re.search(r"삼성SDI|삼성에스디아이", title + body))
        and bool(re.search(r"2026-07-01\s*[~∼-]\s*2026-09-30|'?26\.3Q|2026년\s*3분기", body))
    )


def to_eok(raw: int, unit_million: bool) -> int:
    return raw // 100 if unit_million else raw


def amount_after(text: str, label: str) -> int | None:
    hit = re.search(label + r".{0,40}?(-?[\d,]+)\s*(백만원|억원)", text)
    if not hit:
        return None
    value = int(hit.group(1).replace(",", ""))
    return to_eok(value, hit.group(2) == "백만원")


def parse_result(item: dict) -> dict:
    body = item["text"]
    unit_million = "백만원" in body and "억원" not in body[:200]
    sales = amount_after(body, r"매출액")
    op = amount_after(body, r"영업이익")
    if sales is None:
        hit = re.search(r"매출액\s+당해실적\s+(-?[\d,]+)", body)
        if hit:
            sales = to_eok(int(hit.group(1).replace(",", "")), unit_million)
    if op is None:
        hit = re.search(r"영업이익\s+당해실적\s+(-?[\d,]+)", body)
        if hit:
            op = to_eok(int(hit.group(1).replace(",", "")), unit_million)
    comp = amount_after(body, r"(?:완성차\s*)?보상금|일회성\s*보상")
    ampc = amount_after(body, r"AMPC|첨단제조생산세액공제|세액공제")
    if sales is not None and not 0 < sales < 200000:
        sales = None
    if op is not None and not -50000 < op < 50000:
        op = None
    for name, value in (("comp", comp), ("ampc", ampc)):
        if value is not None and not -20000 < value < 20000:
            if name == "comp":
                comp = None
            else:
                ampc = None
    return {"sales": sales, "op": op, "comp": comp, "ampc": ampc}


def send(cfg: dict, title: str, lines: list[str]) -> None:
    publish(
        resolve_topic("port", cfg.get("ntfy_topic")), title, "\n".join(lines), 4,
        ["chart_with_upwards_trend"], resolve_server(cfg.get("ntfy_server")),
    )


def problem(cfg: dict, state: dict, now: datetime, kind: str, detail: str, dry: bool) -> None:
    key = f"{kind}:{detail}"
    if state.get("last_problem_key") == key:
        print(f"problem already reported: {kind}")
        return
    title = f"[SDI] 삼성SDI 3Q {kind} | 관망 유지"
    lines = [
        "이게 뭐 알림: 삼성SDI 006400 2026년 3분기 잠정에서 보상금과 AMPC를 뺀 이익 확인.",
        f"지금 결과/상태: {kind}. {detail}. {now:%Y-%m-%d %H:%M} KST. 공시: {DISCLOSURES}",
        "지금 할 일: 관망 유지. 공시 원문을 직접 볼 때까지 비중을 바꾸지 마세요.",
        "나중에: 다음 원샷 시각에 다시 확인합니다. 창은 2026-11-05까지입니다.",
        "익절/줄일 때: 숫자 미확인만으로 보유 비중을 줄이지 마세요.",
        "하지 말 것: 보상금이나 AMPC를 추정으로 채워 1,600억원과 비교하지 마세요.",
    ]
    if dry:
        print(title, *lines, sep="\n")
        return
    send(cfg, title, lines)
    state["last_problem_key"] = key
    save_state(state, now)


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode not in {"auto", "dry", "force"}:
        raise SystemExit(f"unknown mode: {mode}")
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
        problem(cfg, state, now, "조회 실패", type(exc).__name__, mode == "dry")
        return 0
    if not rows:
        problem(cfg, state, now, "조회 실패", "공시 목록이 비었습니다", mode == "dry")
        return 0
    matches = [r for r in rows if is_q3_disclosure(r)]
    if not matches:
        if day >= cfg["window_end"] and not state.get("empty_notified"):
            problem(cfg, state, now, "미게시", "2026년 3분기 잠정 공시를 찾지 못했습니다", mode == "dry")
            if mode != "dry":
                state["empty_notified"] = True
                save_state(state, now)
        else:
            print("Q3 disclosure not posted; silent")
        return 0
    item = matches[0]
    p = parse_result(item)
    bar = int(cfg["bar_ex_eok"])
    if p["op"] is None or p["comp"] is None or p["ampc"] is None:
        missing = [k for k in ("op", "comp", "ampc") if p[k] is None]
        problem(cfg, state, now, "숫자 미확인", f"공시 {item['id']}에서 {', '.join(missing)} 없음", mode == "dry")
        return 0
    ex = p["op"] - p["comp"] - p["ampc"]
    if ex >= bar:
        verdict = "관망 상향"
        action = f"보상금과 AMPC를 뺀 이익이 {ex:,}억원으로 기준 {bar:,}억원 이상입니다. 관망 상향. 추가 매수 신호는 아닙니다."
    else:
        verdict = "관망 유지"
        action = f"보상금과 AMPC를 뺀 이익이 {ex:,}억원으로 기준 {bar:,}억원 미만입니다. 관망 유지."
    sales = f"매출 {p['sales']:,}억원, " if p["sales"] is not None else ""
    title = f"[SDI] 삼성SDI 3Q {verdict} | 본업 {ex:,}억원"
    lines = [
        "이게 뭐 알림: 삼성SDI 006400 2026년 3분기 잠정에서 보상금과 AMPC를 뺀 이익 판정.",
        (
            f"지금 결과/상태: {sales}영업이익 {p['op']:,}억원, 보상금 {p['comp']:,}억원, "
            f"AMPC {p['ampc']:,}억원, 차감 후 {ex:,}억원. 기준 {bar:,}억원. 한 줄: {verdict}. "
            f"공시 번호 {item['id']}. 조회 {now:%Y-%m-%d %H:%M} KST. {DISCLOSURES}"
        ),
        f"지금 할 일: {action}",
        "나중에: 4분기 미국 LFP 가동과 2028년 배당 정책은 별도 공시로 확인하세요.",
        "익절/줄일 때: 차감 후 이익이 적자로 정정되면 보유 비중을 재평가하세요.",
        "하지 말 것: 우선주 괴리나 목표주가만으로 이 판정을 바꾸지 마세요.",
    ]
    if mode == "dry":
        print(title, *lines, "dry-run; no ntfy", sep="\n")
        return 0
    send(cfg, title, lines)
    state["result_notified"] = True
    state["last_key"] = item["id"]
    state["last_problem_key"] = None
    save_state(state, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
