#!/usr/bin/env python3
"""Dear U 376300 2026 Q3 preliminary: subs 200/205만, OPM 35%, vs 2Q.

Silent until the preliminary filing is on Naver notices. Subscriber count is not estimated.
Missing sales/OP is an ops notice, not a trade signal. One result send, then silent.
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
CFG_PATH = ROOT / "configs" / "dearq3.json"
STATE_PATH = ROOT / "state" / "dearq3.json"
DISCLOSURES = "https://m.stock.naver.com/domestic/stock/376300/notice"
AMOUNT = r"(-?\d[\d,]*(?:\.\d+)?)"


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
    blob = title + " " + body
    if "잠정" not in title or "영업" not in title:
        return False
    if not re.search(r"디어유|DEAR\s*U", blob, re.I):
        return False
    return bool(re.search(r"2026-07-01\s*[~∼-]\s*2026-09-30|'26\.3Q|2026년\s*3분기", blob))


def num(raw: str) -> float:
    return float(raw.replace(",", ""))


def to_eok(value: float, unit: str) -> float:
    if unit == "백만원":
        return round(value / 100, 1)
    if unit == "천원":
        return round(value / 100000, 1)
    if unit == "원":
        return round(value / 100000000, 1)
    return value


def table_unit(body: str) -> str:
    hit = re.search(r"단위\s*[:：]\s*(억원|백만원|천원|원)", body)
    return hit.group(1) if hit else "억원"


def table_value(body: str, label: str, unit: str) -> float | None:
    hit = re.search(label + r"\s*당해실적\s*" + AMOUNT, body)
    if not hit or unit not in {"억원", "백만원", "천원", "원"}:
        return None
    return to_eok(num(hit.group(1)), unit)


def subs_man(body: str) -> float | None:
    """Average subscribers in 만. None if the filing does not state it."""
    patterns = [
        r"(?:평균\s*)?구독(?:자)?\s*(?:수)?\s*[:：]?\s*(\d[\d,]*(?:\.\d+)?)\s*만",
        r"(?:평균\s*)?구독(?:자)?\s*(?:수)?\s*[:：]?\s*(\d[\d,]*)\s*명",
    ]
    for pat in patterns:
        hit = re.search(pat, body)
        if not hit:
            continue
        value = num(hit.group(1))
        if "만" in pat:
            return value
        if value >= 10000:
            return round(value / 10000, 1)
    return None


def parse_result(item: dict) -> dict:
    body = item["text"]
    unit = table_unit(body)
    sales = table_value(body, r"매출액", unit)
    op = table_value(body, r"영업이익", unit)
    ni = table_value(body, r"(?:당기순이익|순이익)", unit)
    subs = subs_man(body)
    if sales is not None and not 0 < sales < 2000:
        sales = None
    if op is not None and not -200 < op < 500:
        op = None
    if ni is not None and not -500 < ni < 500:
        ni = None
    if subs is not None and not 50 <= subs <= 1000:
        subs = None
    opm = round(op / sales * 100, 1) if sales and op is not None else None
    return {"sales": sales, "op": op, "ni": ni, "subs": subs, "opm": opm}


def judge(p: dict, cfg: dict) -> tuple[str, str]:
    rules = cfg["rules"]
    base = cfg["baseline"]
    subs = p["subs"]
    opm = p["opm"]
    if subs is None:
        return "관망", "공시에 평균 구독자 수가 없어 200만·205만 조건을 확인하지 못함. 추정하지 않음"
    if subs < rules["avoid_subs_man"]:
        return "회피", f"평균 구독 {subs:g}만이 회피선 {rules['avoid_subs_man']}만 미만"
    if opm is None:
        return "관망", "영업이익률을 계산할 매출 또는 영업이익이 없음"
    if opm < rules["avoid_opm"]:
        return "회피", f"영업이익률 {opm:.1f}%가 {rules['avoid_opm']}% 미만"
    sales_up = p["sales"] is not None and p["sales"] > base["q2_sales_eok"]
    if subs >= rules["q4_bar_subs_man"] and sales_up:
        return "매수 검토", f"평균 구독 {subs:g}만이 4분기 조건 {rules['q4_bar_subs_man']}만 이상, 매출도 2분기보다 큼. 추가매수 신호가 아님"
    if subs >= rules["buy_review_subs_man"] and sales_up:
        return "매수 검토", f"평균 구독 {subs:g}만이 3분기 조건 {rules['buy_review_subs_man']}만 이상, 매출도 2분기보다 큼. 205만은 4분기 조건이라 아직 아님"
    if subs >= rules["buy_review_subs_man"] and not sales_up:
        return "관망", f"구독 {subs:g}만은 200만 이상이지만 매출이 2분기 {base['q2_sales_eok']}억원보다 크지 않음"
    return "관망", f"구독 {subs:g}만이 190만 이상 200만 미만"


def problem(cfg: dict, state: dict, now: datetime, kind: str, detail: str, dry: bool) -> None:
    key = f"{kind}:{detail}"
    if state.get("last_problem_key") == key:
        print(f"problem already reported: {kind}")
        return
    title = f"[DEAR] 376300 디어유 3Q {kind} | 관망 유지"
    lines = [
        "이게 뭐 알림: 디어유 376300 2026년 3분기 잠정실적이 나오면 매출, 영업이익, 순이익, 평균 구독자를 200만·205만·영업이익률 35%와 비교하는 알림의 운영 안내.",
        f"지금 결과/상태: {kind}. {detail}. {now:%Y-%m-%d %H:%M} KST. 공시: {DISCLOSURES}",
        "지금 할 일: 관망. 공시 원문을 직접 볼 때까지 비중을 바꾸지 마세요.",
        f"나중에: 다음 확인 시각에 다시 봅니다. 창은 {cfg['window_end']}까지입니다.",
        "익절/줄일 때: 숫자 미확인만으로 비중을 줄이지 마세요.",
        "하지 말 것: 구독자 수를 추정해서 매수 검토나 회피로 바꾸지 마세요.",
    ]
    topic = route_topic(resolve_topic("ops"), title)
    if dry:
        print(f"[dry] topic={topic}", title, *lines, sep="\n")
        return
    publish(topic, title, "\n".join(lines), 3, ["warning"], resolve_server(cfg.get("ntfy_server")))
    state["last_problem_key"] = key
    save_state(state, now)


def result_message(cfg: dict, item: dict, p: dict, now: datetime) -> tuple[str, list[str]]:
    verdict, reason = judge(p, cfg)
    base = cfg["baseline"]
    sales = f"{p['sales']:.1f}억원" if p["sales"] is not None else "미기재"
    op = f"{p['op']:.1f}억원" if p["op"] is not None else "미기재"
    ni = f"{p['ni']:.1f}억원" if p["ni"] is not None else "미기재"
    subs = f"{p['subs']:g}만" if p["subs"] is not None else "미기재"
    opm = f"{p['opm']:.1f}%" if p["opm"] is not None else "계산 불가"
    title = f"[DEAR] 376300 디어유 3Q {verdict} | 구독 {subs}"
    if verdict == "매수 검토":
        action = "매수 검토. 신규 비중 확대 검토만 해당하고 즉시 매수 신호가 아닙니다."
    elif verdict == "회피":
        action = "회피. 신규 매수하지 말고, 보유 중이면 비중 재검토하세요."
    else:
        action = "관망. 비중을 바꾸지 마세요."
    lines = [
        "이게 뭐 알림: 디어유 376300 2026년 3분기 잠정. 매출·영업이익·순이익·평균 구독자를 200만, 205만, 영업이익률 35%와 비교.",
        (
            f"지금 결과/상태: 매출 {sales}(2분기 {base['q2_sales_eok']}억원), 영업이익 {op}(2분기 {base['q2_op_eok']}억원), "
            f"순이익 {ni}(2분기 {base['q2_ni_eok']}억원), 평균 구독 {subs}(2분기 {base['q2_subs_man']}만), 영업이익률 {opm}. "
            f"한 줄: {verdict} ({reason}). 공시 {item['id']}, 조회 {now:%Y-%m-%d %H:%M} KST. {DISCLOSURES}"
        ),
        f"지금 할 일: {action}",
        "나중에: 205만은 4분기 조건입니다. 자사주 150억원 신탁은 소각 완료가 아니므로 이 판정에 넣지 않았습니다.",
        "익절/줄일 때: 구독 190만 미만 또는 영업이익률 35% 미만이 다음 분기에도 이어지면 비중을 줄이세요.",
        "하지 말 것: 목표주가 괴리만으로 이 판정을 매수로 바꾸지 마세요.",
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
        parsed = parse_result(item)
    except Exception as exc:
        problem(cfg, state, now, "파싱 실패", f"공시 {item['id']} {type(exc).__name__}", dry)
        return 0
    if parsed["sales"] is None or parsed["op"] is None:
        missing = [name for name, key in (("매출", "sales"), ("영업이익", "op")) if parsed[key] is None]
        problem(cfg, state, now, "숫자 미확인", f"공시 {item['id']}에서 {', '.join(missing)} 없음", dry)
        return 0
    title, lines = result_message(cfg, item, parsed, now)
    if dry:
        print(f"[dry] topic={resolve_topic('port')}", title, *lines, "dry-run; no ntfy", sep="\n")
        return 0
    publish(
        resolve_topic("port", cfg.get("ntfy_topic")),
        title,
        "\n".join(lines),
        4,
        ["chart_with_upwards_trend"],
        resolve_server(cfg.get("ntfy_server")),
    )
    state["result_notified"] = True
    state["last_key"] = item["id"]
    state["last_problem_key"] = None
    state["parsed"] = parsed
    save_state(state, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
