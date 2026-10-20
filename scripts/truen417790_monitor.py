#!/usr/bin/env python3
"""Truen 417790 2026 Q3 filing check.

Runs in the existing weekday 09:00 morning slot (no new cron). Silent until the Q3 filing
shows up on Naver notices. It gives no buy/sell verdict: it says the filing is out, quotes
the overseas/export lines, lists buyback/dividend notices since 2026-10-01, and lists what to
check. Numbers that cannot be read from the notice text are shown as "원문 확인", never guessed.
One result send, then silent. A fetch problem is an ops notice, never a trade signal.
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
CFG_PATH = ROOT / "configs" / "truen417790.json"
STATE_PATH = ROOT / "state" / "truen417790.json"
AMOUNT = r"(-?\d[\d,]*(?:\.\d+)?)"
PERIOD = re.compile(r"2026[-.]\s?0?7[-.]\s?0?1\s*[~∼-]\s*2026[-.]\s?0?9[-.]\s?30|'26\.3Q|2026년\s*3분기")
RELATED_TITLE = re.compile(r"자기주식|주식소각|소각|배당")


def read_state() -> dict:
    if not STATE_PATH.exists():
        return {"result_notified": False, "empty_notified": False, "last_problem_key": None}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict, now: datetime) -> None:
    state["updated_at"] = now.isoformat(timespec="seconds")
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def is_q3_filing(item: dict) -> bool:
    title = item.get("title", "")
    body = item.get("text", "")[:4000]
    if "분기보고서" in title and re.search(r"2026[.\-]\s?0?9", title):
        return True
    if "잠정" in title and "영업" in title:
        return bool(PERIOD.search(title + " " + body))
    return False


def digits_date(raw: str) -> str:
    return re.sub(r"\D", "", raw or "")[:8]


def to_eok(value: float, unit: str) -> float:
    return round(value / {"억원": 1, "백만원": 100, "천원": 100000, "원": 100000000}[unit], 1)


def table_value(body: str, label: str) -> float | None:
    unit_hit = re.search(r"단위\s*[:：]\s*(억원|백만원|천원|원)", body)
    hit = re.search(label + r"\s*당해실적\s*" + AMOUNT, body)
    if not unit_hit or not hit:
        return None
    return to_eok(float(hit.group(1).replace(",", "")), unit_hit.group(1))


def overseas_lines(body: str, limit: int = 2) -> list[str]:
    out: list[str] = []
    for part in re.split(r"(?<=[.다])\s+|\n", body):
        part = part.strip()
        if re.search(r"수출|해외", part) and re.search(r"\d", part):
            out.append(part[:110])
        if len(out) >= limit:
            break
    return out


def related_notices(rows: list[dict], since: str, limit: int = 4) -> list[str]:
    out = []
    for r in rows:
        if RELATED_TITLE.search(r["title"]) and digits_date(r["datetime"]) >= since.replace("-", ""):
            out.append(f"{r['datetime'][:10]} {r['title'][:50]}")
        if len(out) >= limit:
            break
    return out


def problem(cfg: dict, state: dict, now: datetime, kind: str, detail: str, dry: bool) -> None:
    key = f"{kind}:{detail}"
    if state.get("last_problem_key") == key:
        print(f"problem already reported: {kind}")
        return
    title = f"[TRUEN] 3Q {kind} | 비중 유지"
    lines = [
        "이게 뭐 알림: 트루엔 417790 2026년 3분기 공시를 찾는 알림의 운영 안내입니다. 매매 신호 아님.",
        f"지금 결과/상태: {kind}. {detail}. {now:%Y-%m-%d %H:%M} KST.",
        "지금 할 일: 비중 유지. 공시 원문을 직접 볼 때까지 바꾸지 마세요.",
        f"나중에: 다음 평일 09:00에 다시 찾습니다. 창은 {cfg['window_end']}까지입니다.",
        "익절/줄일 때: 조회 실패만으로 비중을 줄이지 마세요.",
        "하지 말 것: 공시를 못 찾았다는 이유로 해외 실적을 추정하지 마세요.",
    ]
    topic = route_topic(resolve_topic("ops"), title)
    if dry:
        print(f"[dry] topic={topic}", title, *lines, sep="\n")
        return
    publish(topic, title, "\n".join(lines), 3, ["warning"], resolve_server(cfg.get("ntfy_server")))
    state["last_problem_key"] = key
    save_state(state, now)


def result_message(cfg: dict, item: dict, rows: list[dict], now: datetime) -> tuple[str, list[str]]:
    body = item.get("text", "")
    sales, op, ni = (table_value(body, lab) for lab in ("매출액", "영업이익", r"(?:당기순이익|순이익)"))

    def fmt(v: float | None) -> str:
        return f"{v:.1f}억원" if v is not None else "원문 확인"

    over = overseas_lines(body)
    rel = related_notices(rows, cfg["related_since"])
    over_txt = " / ".join(over).rstrip(".") if over else "공시 본문에서 수출 또는 해외 숫자를 못 읽음. 원문 확인"
    rel_txt = " / ".join(rel) if rel else f"{cfg['related_since']} 이후 자사주 소각 또는 배당 관련 새 공시 없음"
    base = cfg["baseline"]
    title = "[TRUEN] 3Q 공시 나옴 | 확인 3가지"
    lines = [
        "이게 뭐 알림: 트루엔 417790 2026년 3분기 공시가 나오면 해외 매출 비중, 자사주 소각과 배당, 순현금 쓰임을 확인하라고 알려주는 점검 알림입니다. 매매 신호 아님.",
        (
            f"지금 결과/상태: 공시 '{item['title'][:40]}' ({item['datetime'][:10]}). 매출 {fmt(sales)}, 영업이익 {fmt(op)}, 순이익 {fmt(ni)}. "
            f"해외 원문 발췌: {over_txt}. 환원 관련 공시: {rel_txt}. "
            f"비교 기준(미검증 참고값): 순현금 약 {base['net_cash_eok']}억원, 소각 약 {base['buyback_done_eok']}억원, 주당 배당 약 {base['dps_won']}원. {item['url']}"
        ),
        "지금 할 일: 원문에서 3가지만 보세요. (1) 지역별 매출에서 해외 비중이 2분기보다 늘었는지 (2) 자사주 소각이나 배당을 추가로 결정했는지 (3) 현금을 성장 투자에 쓰는지 환원에 쓰는지. 확인 전에는 비중 유지.",
        "나중에: 샘플에서 양산 전환 공시, 아마존 영국 매출, 가스공사 전사 확대 여부를 다음 분기까지 봅니다.",
        "익절/줄일 때: 주가가 올라 순현금 프리미엄이 사라지거나 해외 성장이 늦어지면 비중 조절을 검토하세요. 수치 기준은 아직 정하지 않았습니다.",
        "하지 말 것: 해외 본 수주 확인 전 추격매수, 신용 또는 미수, 발췌 한 줄만 보고 판단.",
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
    if mode != "force" and (now.year != int(cfg["year_guard"]) or not cfg["window_start"] <= day <= cfg["window_end"]):
        print("outside 2026 window; silent")
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
    matches = [r for r in rows if is_q3_filing(r)]
    if not matches:
        if day >= cfg["window_end"] and not state.get("empty_notified"):
            problem(cfg, state, now, "미게시", "2026년 3분기 공시를 찾지 못했습니다", dry)
            if not dry:
                state["empty_notified"] = True
                save_state(state, now)
        else:
            print("Q3 filing not posted; silent")
        return 0
    item = matches[0]
    title, lines = result_message(cfg, item, rows, now)
    if dry:
        print("DRY RUN", f"[dry] topic={resolve_topic('port')}", title, *lines, "dry-run; no ntfy", sep="\n")
        return 0
    publish(
        resolve_topic("port", cfg.get("ntfy_topic")),
        title,
        "\n".join(lines),
        3,
        ["memo"],
        resolve_server(cfg.get("ntfy_server")),
    )
    state["result_notified"] = True
    state["last_key"] = item["id"]
    state["last_problem_key"] = None
    save_state(state, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
