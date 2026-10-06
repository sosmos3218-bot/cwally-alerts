#!/usr/bin/env python3
"""LG Energy Solution 2026 Q3 preliminary earnings, using a single disclosure."""
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
CFG_PATH = ROOT / "configs" / "lgesq3.json"
STATE_PATH = ROOT / "state" / "lgesq3.json"
DISCLOSURES = "https://www.lgensol.com/kr/investors/electronic-disclosure"


def read_state() -> dict:
    if not STATE_PATH.exists():
        return {"result_notified": False, "empty_notified": False, "last_problem_key": None}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict, now: datetime) -> None:
    state["updated_at"] = now.isoformat(timespec="seconds")
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def amount(text: str, pattern: str) -> int | None:
    hit = re.search(pattern, text, re.I)
    return int(hit.group(1).replace(",", "")) if hit else None


def is_q3_disclosure(item: dict) -> bool:
    title = item.get("title", "")
    body = item.get("text", "")
    return (
        bool(re.search(r"연결재무제표\s*기준\s*영업\s*\(잠정\)\s*실적", title))
        and bool(re.search(r"당기실적\s*2026-07-01\s*[~∼-]\s*2026-09-30|당기실적\s*\('?26\.3Q", body))
        and bool(re.search(r"엘지에너지솔루션|LG에너지솔루션", title))
    )


def parse_result(item: dict) -> dict:
    body = item["text"]
    sales = amount(body, r"매출액\s+당해실적\s+(-?[\d,]+)")
    op = amount(body, r"영업이익\s+당해실적\s+(-?[\d,]+)")
    ex = amount(body, r"제외한.{0,180}?영업이익(?:은|이)?\s*(-?[\d,]+)\s*억원")
    if sales is not None and not 0 < sales < 200000:
        sales = None
    if op is not None and not -50000 < op < 50000:
        op = None
    if ex is not None and not -50000 < ex < 50000:
        ex = None
    ampc = op - ex if op is not None and ex is not None else None
    if ampc is not None and not 0 <= ampc < 30000:
        ex = ampc = None
    return {"sales": sales, "op": op, "ex": ex, "ampc": ampc}


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
    title = f"[LG] LG엔솔 3Q {kind} | 판정 유보"
    lines = [
        "이게 뭐 알림: LG에너지솔루션 2026년 3분기 잠정실적 확인.",
        f"지금 결과/상태: {kind}. {detail}. {now:%Y-%m-%d %H:%M} KST. 공시: {DISCLOSURES}",
        "지금 할 일: 공시 원문을 직접 확인할 때까지 매매 판단을 보류하세요.",
        "나중에: 다음 기존 원샷 시각에 다시 확인합니다.",
        "익절/줄일 때: 자료 누락만으로 보유 비중을 바꾸지 마세요.",
        "하지 말 것: 추정치를 확정 실적으로 쓰거나 조회 실패를 매매 신호로 읽지 마세요.",
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
    item = matches[0]  # Newest first, including corrections.
    p = parse_result(item)
    if any(p[k] is None for k in ("sales", "op", "ex", "ampc")):
        problem(cfg, state, now, "숫자 미확인", f"공시 {item['id']}에서 필수 수치를 확인하지 못했습니다", mode == "dry")
        return 0
    c = cfg["consensus"]
    op, ex = p["op"], p["ex"]
    if ex >= 0:
        status = "본업 흑자 확인, 매수 재검토 1조건"
        action = "본업 흑자 조건 1개만 충족했습니다. ESS 수주 등 독립 조건 확인 전 추가 매수는 보류하세요."
    elif ex < c["q2_ex_ampc_eok"]:
        status = "본업 적자 확대, 위험 점검"
        action = "2분기보다 본업 손실이 커졌습니다. 신규 매수를 멈추고 공시 원문을 확인하세요."
    else:
        status = "본업 적자 축소, 중립 유지"
        action = "손실은 줄었지만 본업 흑자가 아닙니다. 신규 매수 판단을 보류하세요."
    miss = " 영업이익은 컨센서스보다 15% 이상 낮음." if op < c["op_eok"] * 0.85 else ""
    title = f"[LG] LG엔솔 3Q {status}"
    lines = [
        "이게 뭐 알림: 373220 2026년 3분기 잠정실적을 단일 공시로 확인했습니다.",
        (f"지금 결과/상태: 매출 {p['sales']:,}억원, 영업이익 {op:,}억원"
         f"(10월 6일 컨센서스 {c['op_eok']:,}억원 대비 {op-c['op_eok']:+,}억원), "
         f"AMPC {p['ampc']:,}억원(공시 수치로 계산), AMPC 제외 영업이익 {ex:+,}억원"
         f"(2분기 -1,277억원).{miss} 공시 번호 {item['id']}. 조회 {now:%Y-%m-%d %H:%M} KST. {DISCLOSURES}"),
        f"지금 할 일: {action}",
        "나중에: 연간 ESS 신규 수주 90GWh 달성 여부, GM 생산/매출 회복, 2027년 현금흐름을 공식 자료에서 따로 확인하세요.",
        "익절/줄일 때: AMPC 제외 손실 확대나 수주 지연이 확인되면 보유 비중을 재평가하세요.",
        "하지 말 것: 공시되지 않은 ESS 수주량을 채워 넣거나 영업이익만으로 매수 신호를 만들지 마세요.",
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
