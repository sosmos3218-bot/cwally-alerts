#!/usr/bin/env python3
"""Samsung SDI change-condition watch. Hold vs trim vs 562,000 baseline.

Retired as a standalone alert 2026-11-04 (pt11u): no slot, no one-shot. The verdict logic below
(rules, 562,000 baseline, 373220 peer, ends 2027-01-15 09:00 in configs/sdichg.json) is now called by
scripts/sdimix_monitor.py ([SDI] 006400/006405, k203780 19:00 slot) through evaluate_verdict().
Missing numbers stay missing. News is not a company filing.
Peer print 2026-10-08: LGES 373220 sales 96,434 / OP 7,560 / AMPC 4,169 / ex-AMPC 3,391 eok.
Compensation amount is not in that filing. Sungrow 10GWh is not a confirmed order.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from naver_api import get_json, notices, stock_trend  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "sdichg.json"
STATE_PATH = ROOT / "state" / "sdichg.json"
NOTICE = "https://m.stock.naver.com/domestic/stock/006400/notice"
AMOUNT = r"(-?\d[\d,]*)\s*(억원|백만원|%)"


def read_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(state: dict, now: datetime) -> None:
    state["updated_at"] = now.isoformat(timespec="seconds")
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def num(raw: str) -> int:
    return int(raw.replace(",", ""))


def to_eok(value: int, unit: str) -> int:
    return round(value / 100) if unit == "백만원" else value


def table_unit(body: str) -> str:
    hit = re.search(r"단위\s*[:：]\s*(억원|백만원|천원|원)", body)
    return hit.group(1) if hit else "억원"


def table_value(body: str, label: str, unit: str) -> int | None:
    hit = re.search(label + r"\s*당해실적\s*(-?\d[\d,]*)", body)
    if not hit or unit not in {"억원", "백만원"}:
        return None
    return to_eok(num(hit.group(1)), unit)


def note_amount(body: str, label: str) -> tuple[bool, int | None]:
    mentioned = False
    for m in re.finditer(label, body):
        mentioned = True
        tail = body[m.end(): m.end() + 180]
        hit = re.search(r"(-?\d[\d,]*)\s*(억원|백만원)", tail)
        if not hit:
            continue
        return True, to_eok(num(hit.group(1)), hit.group(2))
    return mentioned, None


def is_q3(item: dict) -> bool:
    blob = item.get("title", "") + item.get("text", "")
    return "잠정" in item.get("title", "") and "영업" in item.get("title", "") and bool(
        re.search(r"2026-07-01\s*[~∼-]\s*2026-09-30|'26\.3Q|2026년\s*3분기", blob)
    )


def ess_share(body: str) -> float | None:
    for pat in (
        r"ESS[^.%]{0,40}?매출[^.%]{0,20}?(\d{1,2}(?:\.\d+)?)\s*%",
        r"ESS[^.%]{0,24}?비중[^.%]{0,12}?(\d{1,2}(?:\.\d+)?)\s*%",
    ):
        hit = re.search(pat, body)
        if hit:
            val = float(hit.group(1))
            if 0 < val <= 100:
                return val
    return None


def lfp_status(blob: str) -> str:
    if not re.search(r"LFP|리튬인산철", blob):
        return "미확인"
    if re.search(r"지연|연기|중단|2027", blob) and re.search(r"가동|양산|라인", blob):
        return "지연 문구"
    if re.search(r"가동|양산|개시|시작", blob):
        return "가동 문구"
    return "언급만"


def solid_status(blob: str) -> str:
    if "전고체" not in blob:
        return "미확인"
    if re.search(r"지연|연기|중단", blob):
        return "지연 문구"
    return "언급만"


def parse_filing(item: dict) -> dict:
    body = item.get("text") or ""
    unit = table_unit(body)
    op = table_value(body, r"영업이익", unit)
    sales = table_value(body, r"매출액", unit)
    ampc_seen, ampc = note_amount(body, r"AMPC|첨단\s*제조\s*생산\s*세액\s*공제")
    comp_seen, comp = note_amount(body, r"보상금|보상\s*이익")
    core = None
    if op is not None:
        core = op - (ampc or 0) - (comp or 0)
        if ampc is None and comp is None:
            core = None  # cannot call it ex-one-off
    return {
        "id": item.get("id"),
        "op": op,
        "sales": sales,
        "ampc": ampc,
        "ampc_seen": ampc_seen,
        "comp": comp,
        "comp_seen": comp_seen,
        "core": core,
        "ess": ess_share(body),
        "lfp": lfp_status(body),
        "solid": solid_status(body),
    }


def target_price() -> int | None:
    try:
        raw = get_json("https://m.stock.naver.com/api/stock/006400/integration")
    except Exception:
        return None
    blob = json.dumps(raw, ensure_ascii=False)
    hit = re.search(r"targetPrice\"?\s*[:=]\s*\"?(\d{5,7})", blob)
    if not hit:
        hit = re.search(r"목표주가[^0-9]{0,12}(\d{5,7})", blob)
    if not hit:
        return None
    val = int(hit.group(1))
    return val if 100000 <= val <= 2000000 else None


def judge(price: float | None, filing: dict | None, lfp: str) -> tuple[str, str]:
    core = None if not filing else filing.get("core")
    ess = None if not filing else filing.get("ess")
    if lfp == "지연 문구" or (filing and filing.get("lfp") == "지연 문구") or (core is not None and core < 0):
        return "축소 점검", "일회성 제외 적자 또는 북미 LFP 지연 문구"
    if core is not None and core > 0 and ess is not None and ess >= 28 and lfp != "지연 문구":
        return "분할매수 검토", "일회성 제외 흑자와 ESS 비중 28% 이상, 지연 문구 없음"
    return "보유", "변경 조건 미충족 또는 숫자 미확인"


def fmt_price(price: float | None, base: int) -> str:
    if price is None:
        return "종가 미확인"
    gap = price / base - 1
    return f"종가 {price:,.0f}원, 562,000원 대비 {gap:+.1%}"


def table_line(price: float | None, base: int, filing: dict | None, target: int | None, base_target: int) -> str:
    if filing and filing.get("core") is not None:
        core = f"{filing['core']:,}억원"
    elif filing and filing.get("op") is not None:
        core = f"영업이익 {filing['op']:,}억원, 일회성 제외 미확인"
    else:
        core = "미확인"
    ess = f"{filing['ess']:.1f}%" if filing and filing.get("ess") is not None else "미확인"
    lfp = (filing or {}).get("lfp") or "미확인"
    if target is None:
        tgt = f"미확인(10/7 기준 {base_target:,}원은 과거값)"
    else:
        tgt = f"{target:,}원, 10/7 {base_target:,}원 대비 {target - base_target:+,}원"
    return f"{fmt_price(price, base)} | 일회성제외OP {core} | ESS비중 {ess} | 북미LFP {lfp} | 목표가 {tgt}"


def lines_for(cfg: dict, now: datetime, status: str, action: str, later: str) -> list[str]:
    return [
        "이게 뭐 알림: 삼성SDI 006400 변경 조건 점검. 매수 신호가 아닙니다.",
        f"지금 결과/상태: {status} 조회 {now:%Y-%m-%d %H:%M} KST. {NOTICE}",
        f"지금 할 일: {action}",
        f"나중에: {later}",
        "익절/줄일 때: 일회성 제외 적자 또는 북미 LFP 지연 공시가 있으면 축소 점검. 주가 상승만으로 줄이지 마세요.",
        "하지 말 것: 미확인 숫자를 추정으로 채우기. 목표주가 상향만으로 분할매수. 우선주 006405와 혼동. 선그로우 10GWh를 확정 수주로 읽기.",
    ]


def send(cfg: dict, title: str, body: str, pri: int, dry: bool, ops: bool = False) -> None:
    topic = resolve_topic("ops" if ops else "port", None if ops else cfg.get("ntfy_topic"))
    if ops:
        topic = route_topic(topic, title)
    if dry:
        print(f"[dry] topic={topic}\n{title}\n{body}")
        return
    publish(topic, title, body, pri, ["chart_with_upwards_trend"], resolve_server(cfg.get("ntfy_server")))


def evaluate_verdict(cfg: dict, now: datetime) -> dict:
    """Same verdict as the old standalone run. Used by sdimix_monitor.py.

    Returns verdict, why, status line, price, filing, target and fetch errors (perr/nerr).
    """
    price = None
    perr = None
    try:
        import krx_close
        bars = krx_close.completed(krx_close.regular_bars(cfg["ticker"], 5), now)
        price = bars[-1].close if bars else None
    except Exception as exc:
        perr = type(exc).__name__
    filing = None
    nerr = None
    try:
        items = notices(cfg["ticker"], 30)
        hits = [r for r in items if is_q3(r)]
        if hits:
            filing = parse_filing(hits[0])
        blob = "\n".join((r.get("title") or "") + " " + (r.get("text") or "")[:400] for r in items[:8])
        lfp = lfp_status(blob)
    except Exception as exc:
        nerr = type(exc).__name__
        lfp = "미확인"
    target = target_price()
    verdict, why = judge(price, filing, lfp)
    status = table_line(price, int(cfg["baseline_price"]), filing, target, int(cfg["baseline_target"]))
    if filing and filing.get("solid"):
        status += f" | 전고체 {filing['solid']}"
    return {"verdict": verdict, "why": why, "status": status, "price": price, "filing": filing,
            "target": target, "perr": perr, "nerr": nerr}


def main() -> int:
    print("sdichg retired 2026-11-04 as a standalone alert; its verdict runs inside sdimix_monitor.py ([SDI] 006400/006405, 19:00). No send, no state.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
