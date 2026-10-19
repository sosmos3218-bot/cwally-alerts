#!/usr/bin/env python3
"""Hyundai Motor 2026 Q3: preliminary earnings + Q3 quarterly dividend (2우B), one-shot verdict.

Sends once, when both the KRX 'consolidated preliminary results' notice and the
'cash dividend decision' notice for Q3 2026 are visible on Naver's notice API.
Verdict line: keep / keep but hold adds / review the ISA long-term hold of 현대차2우B.

modes: auto (default) | dry (print only: no ntfy, no state write) | force (resend)
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from naver_api import get_json, notices, num  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "hmcq3.json"
STATE_PATH = ROOT / "state" / "hmcq3.json"
NOTICE_PAGE = "https://m.stock.naver.com/domestic/stock/005380/notice"

EARN_TITLE = re.compile(r"연결재무제표\s*기준\s*영업\s*\(잠정\)\s*실적")
DIV_TITLE = re.compile(r"현금\s*[ㆍ·.]?\s*현물\s*배당\s*결정")
Q3_SPAN = re.compile(
    r"2026[-.]\s*0?7[-.]\s*0?1\s*[~∼-]\s*2026[-.]\s*0?9[-.]\s*30"
    r"|'?\s*26\s*\.?\s*3\s*Q|2026\s*\.?\s*3\s*Q|3\s*Q\s*'?\s*26|2026\s*년\s*3\s*분기"
)
UNIT_EOK = {"조원": 10000.0, "억원": 1.0, "백만원": 0.01, "천원": 0.00001, "원": 0.00000001}


def read_state() -> dict:
    base = {"result_notified": False, "last_problem_key": None, "earn_seen_at": None}
    if STATE_PATH.exists():
        try:
            base.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return base


def save_state(state: dict, now: datetime) -> None:
    state["updated_at"] = now.isoformat(timespec="seconds")
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def day_of(item: dict) -> str:
    hit = re.search(r"(\d{4})[-.](\d{2})[-.](\d{2})", item.get("datetime", ""))
    return f"{hit.group(1)}-{hit.group(2)}-{hit.group(3)}" if hit else ""


def in_window(item: dict, cfg: dict) -> bool:
    d = day_of(item)
    return bool(d) and cfg["window_start"] <= d <= cfg["window_end"]


def find_earnings(rows: list[dict], cfg: dict) -> dict | None:
    for r in rows:  # newest first, so a correction wins
        if EARN_TITLE.search(r["title"]) and in_window(r, cfg) and Q3_SPAN.search(r["text"]):
            return r
    return None


def find_dividend(rows: list[dict], cfg: dict) -> dict | None:
    for r in rows:
        if DIV_TITLE.search(r["title"]) and in_window(r, cfg) and re.search(r"분기\s*배당", r["text"]):
            return r
    return None


def first_num(text: str, pattern: str) -> float | None:
    hit = re.search(pattern, text)
    return num(hit.group(1)) if hit else None


def parse_earnings(item: dict) -> dict:
    body = item["text"]
    unit = re.search(r"단위\s*[:：]?\s*(조원|억원|백만원|천원|원)", body)
    factor = UNIT_EOK[unit.group(1)] if unit else 1.0
    row = r"\s*(?:\([^)]*\))?\s+당해실적\s+(-?[\d,]+(?:\.\d+)?)"
    sales = first_num(body, r"매출액" + row)
    op = first_num(body, r"영업이익" + row)
    ni = first_num(body, r"지배\s*기업\s*소유주\s*지분\s*순이익" + row)
    out = {k: (round(v * factor) if v is not None else None) for k, v in {"sales": sales, "op": op, "ni": ni}.items()}
    if out["sales"] is not None and not 200_000 <= out["sales"] <= 900_000:
        out["sales"] = None
    if out["op"] is not None and not -60_000 <= out["op"] <= 120_000:
        out["op"] = None
    if out["ni"] is not None and not -60_000 <= out["ni"] <= 120_000:
        out["ni"] = None
    out["unit"] = unit.group(1) if unit else "억원(추정)"
    return out


def parse_dividend(item: dict) -> dict:
    body = item["text"]
    seg = body[body.find("1주당"):] if "1주당" in body else body
    common = first_num(seg, r"보통주식?\s*([\d,]+)")
    pref = first_num(seg, r"종류주식?\s*([\d,]+)")
    record = re.search(r"배당\s*기준일\s*(\d{4}-\d{2}-\d{2})", body)
    pay = re.search(r"배당금\s*지급\s*예정\s*일자\s*(\d{4}-\d{2}-\d{2})", body)
    return {
        "common": int(common) if common is not None else None,
        "pref": int(pref) if pref is not None else None,
        "record": record.group(1) if record else None,
        "pay": pay.group(1) if pay else None,
    }


def live_consensus(code: str) -> int | None:
    """Naver finance/quarter 2026.09 operating-profit consensus (억원), only while still a consensus."""
    try:
        d = get_json(f"https://m.stock.naver.com/api/stock/{code}/finance/quarter")["financeInfo"]
        key = next((t["key"] for t in d["trTitleList"] if str(t.get("key", "")).startswith("202609")), None)
        if not key or next(t for t in d["trTitleList"] if t["key"] == key).get("isConsensus") != "Y":
            return None
        row = next(r for r in d["rowList"] if r.get("title") == "영업이익")
        v = num(row["columns"][key].get("value"))
        return int(v) if v is not None and 5_000 <= v <= 80_000 else None
    except Exception as exc:  # consensus refresh is optional; config value is the fallback
        print(f"live consensus skipped: {type(exc).__name__}", file=sys.stderr)
        return None


def eok(v: int | None) -> str:
    if v is None:
        return "미공시"
    return f"{v / 10000:.2f}조원" if abs(v) >= 10000 else f"{v:,}억원"


def verdict(cfg: dict, op: int, cons: int, pref_div: int) -> tuple[str, str, str, int]:
    r = cfg["rules"]
    if pref_div < r["div_floor_pref"]:
        return ("재검토", "ISA 장기 보유 재검토, 추매 중단",
                f"2우B 배당이 {r['div_floor_pref']:,}원 아래로 줄었습니다. 연 1만원 하한 후퇴 신호입니다. "
                "추가 매수를 멈추고, 회사 설명(실적 자료, 컨콜)에서 배당 정책 변경 여부를 확인한 뒤 보유를 다시 판단하세요.", 5)
    if op < 0 or op < cons * r["op_soft_ratio"]:
        return ("유지(추매 보류)", "ISA 장기 보유 유지, 추매 보류",
                "배당 하한은 지켜졌으니 보유는 유지합니다. 다만 이익이 크게 빠져 자사주 여력이 줄 수 있으니 "
                "추가 매수는 4분기 회복 확인 전까지 보류하세요.", 4)
    return ("유지", "ISA 장기 보유 유지",
            "배당 하한과 실적 모두 판단을 바꿀 수준이 아닙니다. 그대로 보유하세요. 추가 매수는 [2UB] 가격 구간만 따르세요.", 3)


def send(cfg: dict, title: str, lines: list[str], priority: int) -> None:
    publish(resolve_topic("port", cfg.get("ntfy_topic")), title, "\n".join(lines), priority,
            ["car", "chart_with_upwards_trend"], resolve_server(cfg.get("ntfy_server")))


def problem(cfg: dict, state: dict, now: datetime, kind: str, detail: str, dry: bool) -> None:
    key = f"{kind}:{detail}"
    if state.get("last_problem_key") == key:
        print(f"problem already reported: {kind}")
        return
    title = f"[HMCQ3] 현대차 3Q {kind} | 판정 유보"
    lines = [
        "이게 뭐 알림: 현대차 3분기 실적과 2우B 분기배당을 확인해 ISA 장기 보유 판단을 내리는 일회 알림입니다.",
        f"지금 결과/상태: {kind}. {detail}. 조회 {now:%Y-%m-%d %H:%M} KST. 공시: {NOTICE_PAGE}",
        "지금 할 일: 공시 원문을 직접 확인하기 전에는 보유 판단을 바꾸지 마세요.",
        "나중에: 남은 원샷 시각에 다시 확인합니다. 마지막 확인은 11/2 09:20 KST입니다.",
        "익절/줄일 때: 가격, 괴리율 기준은 [2UB] 알림을 따르세요.",
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
    if dry:
        print("DRY RUN: no ntfy, no state write")
    if now.year != int(cfg["year_guard"]) or not (cfg["window_start"] <= day <= cfg["window_end"]):
        if not dry:
            print("outside 2026 one-shot window; silent")
            return 0
        print("outside window; dry run continues for wiring check")
    if state.get("result_notified") and mode == "auto":
        print("result already notified; silent")
        return 0
    final = now >= datetime.strptime(cfg["final_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)

    try:
        rows = notices(cfg["ticker"], 40)
    except Exception as exc:
        problem(cfg, state, now, "조회 실패", f"공시 목록 {type(exc).__name__}", dry)
        return 0
    if not rows:
        problem(cfg, state, now, "조회 실패", "공시 목록이 비었습니다", dry)
        return 0

    earn, div = find_earnings(rows, cfg), find_dividend(rows, cfg)
    print(f"notices={len(rows)} earnings={earn and earn['id']} dividend={div and div['id']}")
    if not earn or not div:
        missing = "실적 공시와 배당 공시" if not earn and not div else ("실적 공시" if not earn else "3분기 배당 공시")
        later_day = bool(state.get("earn_seen_at")) and state["earn_seen_at"][:10] < day
        if final or (earn and later_day):
            problem(cfg, state, now, "미게시", f"2026년 3분기 {missing}를 찾지 못했습니다", dry)
        else:
            if earn and not dry and not state.get("earn_seen_at"):
                state["earn_seen_at"] = now.isoformat(timespec="seconds")
                save_state(state, now)
            print(f"{missing} not posted yet; silent")
        return 0

    e, d = parse_earnings(earn), parse_dividend(div)
    print(f"parsed earnings={e} dividend={d}")
    if e["op"] is None:
        problem(cfg, state, now, "숫자 미확인", f"실적 공시 {earn['id']}에서 영업이익을 읽지 못했습니다", dry)
        return 0
    pref_div = d["pref"] if d["pref"] is not None else d["common"]
    if pref_div is None:
        problem(cfg, state, now, "숫자 미확인", f"배당 공시 {div['id']}에서 1주당 배당금을 읽지 못했습니다", dry)
        return 0

    live = live_consensus(cfg["ticker"])
    cons = live or int(cfg["consensus"]["op_eok"])
    cons_src = "네이버 컨센서스(실행 시점)" if live else f"FnGuide {cfg['consensus']['as_of']}"
    gap = (e["op"] - cons) / cons * 100.0
    word, act, todo, pri = verdict(cfg, e["op"], cons, pref_div)
    div_word = "유지" if pref_div >= cfg["rules"]["div_floor_pref"] else "축소"
    title = f"[HMCQ3] 배당 {pref_div:,}원 {div_word}, 영업익 컨센 {gap:+.0f}% | {act}"
    pref_note = "" if d["pref"] is not None else " (종류주 칸 미확인, 보통주 금액 사용)"
    lines = [
        "이게 뭐 알림: 현대차 3분기 잠정실적과 3분기 분기배당(2우B)을 확인해 ISA 장기 보유 판단을 한 줄로 냅니다.",
        (f"지금 결과/상태: 결론 {word}. 영업이익 {eok(e['op'])} (컨센서스 {eok(cons)}, {cons_src} 대비 {gap:+.1f}%), "
         f"매출 {eok(e['sales'])}, 지배순이익 {eok(e['ni'])}. 2우B 분기배당 {pref_div:,}원{pref_note}"
         f" (기준 {cfg['rules']['div_floor_pref']:,}원), 보통주 {d['common'] or 0:,}원."
         f" 배당기준일 {d['record'] or '공시 확인'}, 지급 예정 {d['pay'] or '공시 확인'}."
         f" 공시 {day_of(earn)} / {day_of(div)}, 조회 {now:%Y-%m-%d %H:%M} KST."),
        f"지금 할 일: {todo}",
        "나중에: 4분기 실적(1월 말)에서 연간 배당 1만원 유지와 2028년 이후 주주환원 정책 방향을 다시 확인하세요.",
        "익절/줄일 때: 가격, 괴리율 기준(괴리 35%, 25%, 20%)은 [2UB] 알림을 따르세요.",
        "하지 말 것: 실적 미스 하나로 ISA 물량을 던지거나, ISA 안에서 단타하지 마세요(한도 초과분 차익도 9.9% 과세).",
    ]
    if dry:
        print("DRY RUN (result)", title, *lines, "dry-run; no ntfy", sep="\n")
        return 0
    send(cfg, title, lines, pri)
    state.update({
        "result_notified": True,
        "last_problem_key": None,
        "earn_id": earn["id"],
        "div_id": div["id"],
        "op_eok": e["op"],
        "consensus_eok": cons,
        "pref_div": pref_div,
        "verdict": word,
    })
    save_state(state, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
