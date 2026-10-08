#!/usr/bin/env python3
"""Samsung SDI change-condition watch. Hold vs trim vs 562,000 baseline.

Periodic: weekday 09:00 KST morning slot. One-shots cover the Oct 27 window only.
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


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode not in {"auto", "dry", "force", "install"}:
        raise SystemExit(f"unknown mode: {mode}")
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    dry = mode == "dry"
    if mode == "install":
        send(
            cfg,
            "[SDI] 설치 확인 | 매매 신호 아님",
            "\n".join(lines_for(
                cfg, now,
                "평일 09:00 KST morning 슬롯과 10/27 08:40, 10/27 16:20, 10/28 16:22, 11/04 16:22 원샷. 기준가 2026-10-07 562,000원.",
                "설치 확인만 하세요. 사지 마세요.",
                "월요일 상태, 판정 변경, 10/27 전후와 11/04-05만 보냅니다. 숫자 없으면 미확인.",
            )),
            3, dry,
        )
        print("install confirmation sent")
        return 0
    if now.strftime("%Y-%m-%d %H:%M") > cfg["ends_kst"] and mode != "force":
        print("past ends_kst; silent")
        return 0
    state = read_state()
    today = now.date().isoformat()
    price = None
    perr = None
    try:
        rows = stock_trend(cfg["ticker"], 3)
        price = rows[0]["close"] if rows else None
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
    status += f" | 판정 {verdict} ({why})"
    if filing and filing.get("solid"):
        status += f" | 전고체 {filing['solid']}"
    event = today in set(cfg["event_dates"])
    weekly = now.weekday() == 0 and state.get("last_weekly") != today
    changed = verdict != state.get("verdict")
    must = event or weekly or changed or mode == "force"
    if perr and nerr and must:
        send(
            cfg,
            "[SDI] 조회 실패 | 보유 유지",
            "\n".join(lines_for(cfg, now, f"시세 {perr}, 공시 {nerr}. 조건 미충족이 아닙니다.", "보유. 증권 앱에서 직접 보세요.", "다음 평일 09:00에 재시도.")),
            4, dry, ops=True,
        )
        if not dry:
            state["last_problem"] = today
            save_state(state, now)
        return 0
    if not must:
        print(f"silent verdict={verdict} price={price}")
        if not dry:
            state["last_price"] = price
            save_state(state, now)
        return 0
    if verdict == "축소 점검":
        action = "축소 점검. 공시 원문과 562,000원 대비 평가를 보고 비중을 재검토하세요. 매도 지시가 아닙니다."
        title = "[SDI] 축소 점검 | 변경 조건"
    elif verdict == "분할매수 검토":
        action = "분할매수 검토. 추가 매수 신호가 아닙니다. 일회성 제외 흑자와 ESS 비중을 원문에서 다시 보세요."
        title = "[SDI] 분할매수 검토 | 조건 충족"
    else:
        action = "보유. 562,000원 대비 추격하지 마세요. 숫자 미확인과 선그로우 미확정은 상향 조건이 아닙니다."
        title = "[SDI] 보유 | 변경 조건 미충족"
    later = (
        "피어 373220은 10/8 잠정 매출 96,434억, 영업이익 7,560억, AMPC 4,169억, AMPC 제외 3,391억. "
        "보상금 금액은 공시에 없음. 확정 실적 11/3. 선그로우 10GWh는 확정 수주 아님. "
        "상향은 일회성 제외 흑자, ESS 28% 이상, LFP 지연 없음. 기간은 2027년 ESS 이익."
    )
    send(cfg, title, "\n".join(lines_for(cfg, now, status, action, later)), 4 if verdict != "보유" else 3, dry)
    if not dry:
        state["verdict"] = verdict
        state["last_price"] = price
        state["last_target"] = target
        state["last_core"] = None if not filing else filing.get("core")
        state["last_ess"] = None if not filing else filing.get("ess")
        if weekly:
            state["last_weekly"] = today
        if event:
            sent = list(state.get("event_sent") or [])
            if today not in sent:
                sent.append(today)
            state["event_sent"] = sent
        save_state(state, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
