#!/usr/bin/env python3
"""LG CNS 064400 2026 Q3: release date, latest consensus, and preliminary result vs the judgment-change bar.

Sources (official first):
  - stock.naver.com notice API (exchange/DART notices with full text):
      결산실적공시 예고 / 기업설명회(IR) 3분기 경영실적 발표  -> release date
      연결재무제표기준영업(잠정)실적 for 2026-07-01 ~ 2026-09-30 -> actual sales / OP / net income
  - m.stock.naver.com finance API (FnGuide consensus): 2026.09 column while isConsensus == "Y",
    2026.12 annual OP consensus. The last pre-release reading is kept in state, because the column
    turns into actuals after the release.

Bar (configs/lgcnsq3.json "rules", from the 2026-10-07 analysis):
  upgrade candidate : Q3 OP > consensus OP and OP margin >= 8.0%
  downgrade         : OP margin < 8.0% or OP < consensus OP x 0.95
  otherwise         : keep (중립~약한 매수, 분할 접근)
Order backlog / GPUaaS contracts are never inferred from text; they stay a manual check.

Modes: auto (one-shot; the first run also sends a pre-release snapshot), slot (afternoon slot;
silent unless a new release date or the result appears), dry (print only, no state), force
(re-send the result). Lookup/data problems go to cwally-ops, never as a trading line.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from naver_api import get_json, notices  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "lgcnsq3.json"
STATE_PATH = ROOT / "state" / "lgcnsq3.json"
DISCLOSURES = "https://m.stock.naver.com/domestic/stock/064400/notice"
Q3_SPAN = r"2026-07-01\s*[~∼-]\s*2026-09-30"
DEFAULT_STATE = {
    "snapshot_sent": False,
    "date_notified": None,
    "result_notified": False,
    "consensus_live": None,
    "ops_sent": {},
    "last_key": None,
    "updated_at": None,
}


# ---------- state ----------

def read_state() -> dict:
    state = dict(DEFAULT_STATE)
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                state.update({k: v for k, v in data.items() if k in DEFAULT_STATE})
        except json.JSONDecodeError:
            pass
    if not isinstance(state.get("ops_sent"), dict):
        state["ops_sent"] = {}
    return state


def save_state(state: dict, now: datetime) -> None:
    state["updated_at"] = now.isoformat(timespec="seconds")
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE_PATH)


# ---------- parsing ----------

def num(raw) -> float | None:
    if raw is None:
        return None
    s = str(raw).replace(",", "").strip()
    if s in {"", "-", "N/A"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def find_release_date(rows: list[dict]) -> tuple[str | None, str | None]:
    """(YYYY-MM-DD, source note) from the Q3 결산실적공시 예고 or the Q3 IR call notice."""
    for r in rows:
        title, body = r.get("title", ""), r.get("text", "")
        if "결산실적" in title and "예고" in title and re.search(r"2026-07-01.{0,20}2026-09-30", body):
            m = re.search(r"공시\s*예정일\s*(2026-\d{2}-\d{2})", body)
            if m:
                return m.group(1), f"결산실적공시 예고 공시 {r.get('id')}"
    for r in rows:
        title, body = r.get("title", ""), r.get("text", "")
        if "기업설명회" in title and re.search(r"2026년\s*3분기\s*경영실적\s*발표", body):
            m = re.search(r"일시\s*(2026-\d{2}-\d{2})\s*(\d{2}:\d{2})?", body)
            if m:
                at = m.group(1) + (f" {m.group(2)}" if m.group(2) else "")
                return m.group(1), f"3분기 실적발표 IR 공시 {r.get('id')} ({at} 컨퍼런스콜)"
    return None, None


def is_q3_result(item: dict) -> bool:
    title, body = item.get("title", ""), item.get("text", "")
    return "잠정" in title and "영업" in title and "실적" in title and bool(
        re.search(r"당기실적\s*" + Q3_SPAN, body) or re.search(r"'26\.\s*3Q", body)
    )


def table_unit(body: str) -> str | None:
    m = re.search(r"단위\s*[:：]\s*(억원|백만원|천원|원)", body)
    return m.group(1) if m else None


def table_value(body: str, label: str, unit: str) -> float | None:
    m = re.search(label + r"\s*당해실적\s*(-?\d[\d,]*)", body)
    if not m:
        return None
    v = num(m.group(1))
    if v is None:
        return None
    if unit == "백만원":
        return round(v / 100, 1)
    if unit == "억원":
        return v
    return None


def parse_result(item: dict) -> dict:
    body = item["text"]
    unit = table_unit(body) or "백만원"
    sales = table_value(body, r"매출액", unit)
    op = table_value(body, r"영업이익", unit)
    ni = table_value(body, r"(?<!법인세비용차감전계속사업이익 )당기순이익", unit)
    if sales is not None and not 5000 < sales < 60000:
        sales = None
    if op is not None and not -5000 < op < 10000:
        op = None
    return {"sales": sales, "op": op, "ni": ni, "unit": unit}


def fetch_consensus(code: str) -> dict | None:
    """Q3 2026 and FY2026 consensus in 억원 while Naver still shows them as consensus."""
    q = get_json(f"https://m.stock.naver.com/api/stock/{code}/finance/quarter")
    a = get_json(f"https://m.stock.naver.com/api/stock/{code}/finance/annual")

    def pick(raw: dict, key: str) -> tuple[bool, dict]:
        info = (raw or {}).get("financeInfo") or {}
        cols = {t.get("key"): t for t in info.get("trTitleList") or []}
        col = cols.get(key)
        if not col:
            return False, {}
        vals = {}
        for row in info.get("rowList") or []:
            cell = (row.get("columns") or {}).get(key) or {}
            vals[row.get("title")] = num(cell.get("value"))
        return col.get("isConsensus") == "Y", vals

    q_is_cons, qv = pick(q, "202609")
    a_is_cons, av = pick(a, "202612")
    if not q_is_cons:
        return None  # column is gone or already actuals; caller falls back to the stored reading
    sales, op = qv.get("매출액"), qv.get("영업이익")
    if sales is None or op is None:
        return None
    return {
        "sales": sales,
        "op": op,
        "fy_op": av.get("영업이익") if a_is_cons else None,
        "fy_sales": av.get("매출액") if a_is_cons else None,
    }


# ---------- judgment ----------

def judge(p: dict, cons: dict, rules: dict) -> tuple[str, str]:
    op, sales = p["op"], p["sales"]
    margin = op / sales * 100 if sales else None
    cop = float(cons["op"])
    floor = float(rules["margin_floor_pct"])
    miss = float(rules["miss_ratio"])
    if margin is not None and op > cop and margin >= floor:
        return "판단 상향 후보", f"영업이익 {op:,.0f}억 > 컨센 {cop:,.0f}억, 이익률 {margin:.1f}% ≥ {floor:.1f}%"
    if margin is not None and margin < floor:
        return "판단 하향", f"이익률 {margin:.1f}%가 {floor:.1f}% 미만(7%대 정체)"
    if op < cop * miss:
        return "판단 하향", f"영업이익 {op:,.0f}억이 컨센 {cop:,.0f}억의 {miss * 100:.0f}% 미만"
    return "판단 유지", f"영업이익 {op:,.0f}억, 컨센 {cop:,.0f}억 대비 {(op / cop - 1) * 100:+.1f}%, 이익률 {margin:.1f}%"


def gap(actual: float | None, est: float | None) -> str:
    if actual is None or not est:
        return "비교 불가"
    return f"{(actual / est - 1) * 100:+.1f}%"


# ---------- sending ----------

def send(cfg: dict, title: str, lines: list[str], dry: bool, priority: int = 4, kind: str = "port") -> None:
    topic = route_topic(resolve_topic(kind, cfg.get("ntfy_topic")), title)
    if dry:
        print(f"[dry] topic={topic}", title, *lines, "dry-run; no ntfy", sep="\n")
        return
    tags = ["warning"] if kind == "ops" else ["chart_with_upwards_trend"]
    publish(topic, title, "\n".join(lines), priority, tags, resolve_server(cfg.get("ntfy_server")))


def problem(cfg: dict, state: dict, now: datetime, kind: str, detail: str, dry: bool) -> None:
    day = now.strftime("%Y-%m-%d")
    if state["ops_sent"].get(kind) == day and not dry:
        print(f"problem already reported today: {kind}")
        return
    title = f"[LG] LG씨엔에스 3Q {kind} | 판단 유보"
    lines = [
        "이게 뭐 알림: LG씨엔에스 064400 2026년 3분기 발표일, 컨센서스, 잠정실적을 확인하는 알림의 운영 안내.",
        f"지금 결과/상태: {kind}. {detail}. {now:%Y-%m-%d %H:%M} KST. 공시: {DISCLOSURES}",
        "지금 할 일: 판단 유보. 숫자를 확인하기 전에는 비중을 바꾸지 마세요.",
        f"나중에: 다음 실행(평일 15:20 또는 원샷)에 다시 확인합니다. 창은 {cfg['window_end']}까지.",
        "익절/줄일 때: 조회 실패만으로 줄이지 마세요.",
        "하지 말 것: 기사나 추정치로 잠정실적을 대신하지 마세요.",
    ]
    send(cfg, title, lines, dry, 3, "ops")
    if not dry:
        state["ops_sent"][kind] = day
        save_state(state, now)


def cons_text(cons: dict | None) -> str:
    if not cons:
        return "컨센서스 확인 불가"
    fy = f", 연간 영업이익 {cons['fy_op']:,.0f}억" if cons.get("fy_op") else ""
    return f"3분기 컨센서스 매출 {cons['sales']:,.0f}억, 영업이익 {cons['op']:,.0f}억 ({cons.get('as_of', '?')}){fy}"


def pre_release_message(cfg: dict, cons: dict | None, date: str | None, src: str | None, now: datetime,
                        kind: str) -> tuple[str, list[str]]:
    b, r = cfg["baseline"], cfg["rules"]
    date_txt = f"{date} ({src})" if date else "회사 공시 전(미확정). 작년 3분기는 10-30"
    head = f"발표일 {date}" if date else "발표일 미확정"
    cop = f"{cons['op']:,.0f}억" if cons else "확인 불가"
    title = f"[LG] LG씨엔에스 3Q {head} | 컨센 영업익 {cop}"
    lines = [
        "이게 뭐 알림: LG씨엔에스 064400 3분기 실적 전 점검. " + ("발표일 공시가 나왔습니다." if kind == "date" else "발표 전 현재 판단과 기준."),
        (
            f"지금 결과/상태: 발표일 {date_txt}. {cons_text(cons)}. 작년 3분기 매출 {b['q3_2025_sales_eok']:,}억, "
            f"영업이익 {b['q3_2025_op_eok']:,}억. 현재 판단 {b['judgment']} (기준가 {b['price']:,}원, {b['price_asof']})."
        ),
        "지금 할 일: 발표 전에는 판단 유지. 사려면 나눠서만, 발표 전 몰아서 사지 마세요.",
        (
            f"나중에: 발표 당일 잠정공시로 다시 알립니다. 상향 후보: 영업이익이 컨센서스 초과이고 이익률 {r['margin_floor_pct']}% 이상. "
            f"하향: 이익률 {r['margin_floor_pct']}% 미만 또는 영업이익이 컨센서스의 {r['miss_ratio'] * 100:.0f}% 미만."
        ),
        "익절/줄일 때: 하향 판정이 나오고 LG 계열 IT 투자 축소까지 확인되면 비중 축소를 검토하세요.",
        "하지 말 것: 테마 급등 추격, 신용/미수. 수주와 GPU 임대 계약은 공시로만 확인.",
    ]
    return title, lines


def result_message(cfg: dict, item: dict, p: dict, cons: dict, now: datetime) -> tuple[str, list[str]]:
    b, r = cfg["baseline"], cfg["rules"]
    verdict, reason = judge(p, cons, r)
    margin = p["op"] / p["sales"] * 100
    yoy_op = (p["op"] / b["q3_2025_op_eok"] - 1) * 100
    q4_need = ""
    if cons.get("fy_op"):
        need = cons["fy_op"] - b["h1_op_eok"] - p["op"]
        q4_need = (
            f" 연간 컨센 {cons['fy_op']:,.0f}억을 맞추려면 4분기 {need:,.0f}억 필요"
            f"(작년 4분기 {b['q4_2025_op_eok']:,}억 대비 {(need / b['q4_2025_op_eok'] - 1) * 100:+.0f}%)."
        )
    ni = f", 순이익 {p['ni']:,.0f}억" if p.get("ni") is not None else ""
    title = f"[LG] LG씨엔에스 3Q {verdict} | 영업익 {p['op']:,.0f}억 (컨센 {gap(p['op'], cons['op'])})"
    if verdict == "판단 상향 후보":
        todo = "분할매수 유지/확대 검토. 단, 수주와 4분기 가이던스를 컨퍼런스콜 자료로 확인한 뒤에만 늘리세요."
    elif verdict == "판단 하향":
        todo = "판단을 '중립'으로 낮춥니다. 신규 매수 보류, 보유분은 유지하며 다음 분기까지 지켜보세요."
    else:
        todo = "판단 유지(중립~약한 매수). 사려면 나눠서만. 추가 확대는 수주 공시가 붙을 때."
    lines = [
        "이게 뭐 알림: LG씨엔에스 064400 2026년 3분기 잠정실적을 컨센서스와 판단 변경 조건에 대입한 결과.",
        (
            f"지금 결과/상태: 매출 {p['sales']:,.0f}억 (컨센 {cons['sales']:,.0f}억 대비 {gap(p['sales'], cons['sales'])}), "
            f"영업이익 {p['op']:,.0f}억 (컨센 {cons['op']:,.0f}억 대비 {gap(p['op'], cons['op'])}, 전년 {yoy_op:+.1f}%), "
            f"이익률 {margin:.1f}%{ni}. 한 줄: {verdict} ({reason}).{q4_need} "
            f"컨센 기준 {cons.get('as_of', '?')}. 공시 {item['id']}, 조회 {now:%Y-%m-%d %H:%M} KST. {DISCLOSURES}"
        ),
        f"지금 할 일: {todo}",
        "나중에: 잠정치입니다. 사업별(AI/클라우드, 스마트엔지니어링) 실적과 수주는 회사 IR 자료에서 확인하세요.",
        "익절/줄일 때: 하향 판정에 LG 계열 IT 투자 축소가 겹치면 비중 축소를 검토하세요.",
        "하지 말 것: 발표 당일 급등락 추격, 신용/미수. 이 알림은 투자 권유가 아닙니다.",
    ]
    return title, lines


# ---------- main ----------

def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode not in {"auto", "slot", "dry", "force"}:
        raise SystemExit(f"unknown mode: {mode}")
    dry = mode == "dry"
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = read_state()
    now = datetime.now(KST)
    day = now.strftime("%Y-%m-%d")
    if not dry and (now.year != int(cfg["year_guard"]) or not cfg["window_start"] <= day <= cfg["window_end"]):
        print("outside 2026 window; silent")
        return 0
    if state.get("result_notified") and mode != "force":
        print("result already notified; silent")
        return 0

    try:
        rows = notices(cfg["ticker"], 40)
    except Exception as exc:
        problem(cfg, state, now, "조회 실패", f"공시 목록 {type(exc).__name__}", dry)
        return 0
    if not rows:
        problem(cfg, state, now, "조회 실패", "공시 목록이 비었습니다", dry)
        return 0

    # consensus: live while still consensus, else the last stored pre-release reading, else config
    live = None
    try:
        live = fetch_consensus(cfg["ticker"])
    except Exception as exc:
        print(f"consensus fetch failed: {type(exc).__name__}")
    changed = False
    if live:
        live["as_of"] = day
        if state.get("consensus_live") != live:
            state["consensus_live"] = live
            changed = True
    cons = state.get("consensus_live") or dict(cfg["consensus"])

    matches = [r for r in rows if is_q3_result(r)]
    if matches:
        item = matches[0]  # newest first; a correction (정정) replaces the original
        try:
            p = parse_result(item)
        except Exception as exc:
            problem(cfg, state, now, "숫자 미확인", f"공시 {item['id']} 파싱 오류 {type(exc).__name__}", dry)
            return 0
        if p["sales"] is None or p["op"] is None:
            missing = [n for n, k in (("매출액", "sales"), ("영업이익", "op")) if p[k] is None]
            problem(cfg, state, now, "숫자 미확인", f"공시 {item['id']}에서 {', '.join(missing)} 없음", dry)
            return 0
        title, lines = result_message(cfg, item, p, cons, now)
        send(cfg, title, lines, dry, 4)
        if not dry:
            state["result_notified"] = True
            state["last_key"] = item["id"]
            save_state(state, now)
        return 0

    date, src = find_release_date(rows)
    if mode in {"auto", "dry"} and not state.get("snapshot_sent"):
        title, lines = pre_release_message(cfg, cons if (live or state.get("consensus_live")) else dict(cfg["consensus"]), date, src, now, "snapshot")
        send(cfg, title, lines, dry, 3)
        if not dry:
            state["snapshot_sent"] = True
            if date:
                state["date_notified"] = date
            save_state(state, now)
        return 0
    if date and state.get("date_notified") != date:
        title, lines = pre_release_message(cfg, cons, date, src, now, "date")
        send(cfg, title, lines, dry, 3)
        if not dry:
            state["date_notified"] = date
            save_state(state, now)
        return 0

    if day >= cfg["window_end"] and state["ops_sent"].get("미게시") is None:
        problem(cfg, state, now, "미게시", "창 마지막 날까지 2026년 3분기 잠정실적 공시를 찾지 못했습니다", dry)
        return 0
    if changed and not dry:
        save_state(state, now)  # keep the latest pre-release consensus for release day
    print(f"no new date/result; silent (date={date}, consensus={cons.get('op')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
