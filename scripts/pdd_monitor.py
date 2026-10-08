#!/usr/bin/env python3
"""PDD change-condition watch. Price multiple daily; fundamentals on new filing. Silent unless change."""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "pdd.json"
STATE_PATH = ROOT / "state" / "pdd.json"
UA = {"User-Agent": "cwally-alerts pdd research monitor research@example.com"}
Q3_JUDGE = "2026-11-18"


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return dict(fallback)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return dict(fallback)
    out = dict(fallback)
    out.update(data)
    return out


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def quote(url: str) -> tuple[float | None, str | None]:
    data = json.loads(fetch(url))
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        return None, "quote empty"
    price = (result[0].get("meta") or {}).get("regularMarketPrice")
    try:
        return (float(price) if price is not None else None), None
    except (TypeError, ValueError):
        return None, "price parse"


def latest_sec_date(url: str) -> str | None:
    data = json.loads(fetch(url))
    recent = ((data.get("filings") or {}).get("recent") or {})
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    for form, day in zip(forms, dates):
        if form in {"6-K", "20-F", "10-Q", "10-K", "8-K"}:
            return day
    return None


def adj_multiple(price: float, cfg: dict) -> tuple[float, float, float]:
    base = cfg["baseline"]
    haircut = float(cfg["levels"]["cash_haircut"])
    mcap = price * float(base["shares_b"])
    ev = mcap + float(base["financial_debt_b"]) - haircut * float(base["cash_sti_b"])
    oi = float(base["ttm_oi_b"])
    return mcap, ev, ev / oi


def zone_of(multiple: float, ceiling: float) -> str:
    return "watch" if multiple >= ceiling else "buy_split"


def body(stamp: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join(
        [
            f"{stamp} | PDD | 변경조건 점검",
            "이게 뭐 알림: 핀둬둬 변경조건 알림입니다. 매수·매도 지시가 아닙니다.",
            f"지금 결과/상태: {status}",
            f"지금 할 일: {action}",
            f"나중에: {later}",
            f"익절/줄일 때: {trim}",
            f"하지 말 것: {avoid}",
        ]
    )


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    if mode == "install":
        publish(
            topic,
            "[PDD] 설치 확인 | 매매 신호 아님",
            body(
                stamp,
                "평일 09:00 KST morning 슬롯. 기준 2026-10-07 78.50달러, 현금 673억달러의 50%만 인정. 조정 EV/영업이익 8배면 관망.",
                "설치 확인만 하세요. 사지 마세요.",
                "월요일 상태, 8배 구간 변경, 11/18 실적 창, 새 6-K만 보냅니다.",
                "이번 알림으로 비중을 바꾸지 마세요.",
                "설치 확인을 매수나 매도 신호로 읽기.",
            ),
            3,
            ["information_source"],
            server,
        )
        print("install confirmation sent")
        return 0

    dry = mode == "dry" or os.environ.get("PDD_DRY") == "1"
    state = load_json(
        STATE_PATH,
        {
            "updated_at": None,
            "last_price": None,
            "zone": "buy_split",
            "last_weekly": None,
            "last_filing": cfg["baseline"]["last_known_filing"],
            "sent_dates": [],
            "fetch_fail_date": None,
        },
    )
    today = now.date()
    if today.isoformat() > cfg["valid_until"] and mode not in {"force", "dry"}:
        print(f"after {cfg['valid_until']}; silent")
        return 0

    base = cfg["baseline"]
    ceiling = float(cfg["levels"]["watch_ev_oi"])
    price, qerr = None, None
    try:
        price, qerr = quote(cfg["sources"]["quote"])
    except Exception as exc:
        qerr = str(exc)
    print(f"price={price} qerr={qerr}")

    alerts: list[tuple[str, str, int]] = []
    if price is None:
        if state.get("fetch_fail_date") != today.isoformat():
            alerts.append(
                (
                    "[PDD] 주가 조회 실패 | 조건 미확인",
                    body(
                        stamp,
                        f"주가 조회 실패. {qerr or '빈 응답'}. 조건 미충족이 아닙니다.",
                        "증권 앱에서 PDD 전일 종가를 직접 보세요. 8배 관망을 이 알림으로 판정하지 마세요.",
                        "다음 평일 09:00 KST에 재시도합니다.",
                        "이번 알림으로 줄이거나 사지 마세요.",
                        "조회 실패를 관망 전환이나 정상으로 취급.",
                    ),
                    4,
                )
            )
            state["fetch_fail_date"] = today.isoformat()
    else:
        mcap, ev, multiple = adj_multiple(price, cfg)
        zone = zone_of(multiple, ceiling)
        prev = state.get("zone") or "buy_split"
        changed = zone != prev
        weekly = today.weekday() == 0 and state.get("last_weekly") != today.isoformat()
        window = today.isoformat() == Q3_JUDGE and Q3_JUDGE not in (state.get("sent_dates") or [])
        if changed or weekly or window or mode == "force":
            if zone == "watch":
                title = "[PDD] 조정 EV 8배 | 신규 매수 중단"
                action = "변경조건 충족. 신규 매수를 멈추고 관망하세요. 보유 수량은 이 알림이 모릅니다."
            else:
                title = "[PDD] 조정 EV 8배 미만 | 분할 매수 유지"
                action = "가격 조건은 아직 관망 전환이 아닙니다. 실적 조건은 공시 전 미확인입니다."
            why = "구간 변경" if changed else "월요일 상태" if weekly else "3Q 창"
            alerts.append(
                (
                    title,
                    body(
                        stamp,
                        f"PDD {price:.2f}달러. 시총 {mcap:.1f}억달러. 현금 50% 차감 EV {ev:.1f}억달러, TTM 영업이익 {base['ttm_oi_b']}억달러 대비 {multiple:.1f}배. {prev} -> {zone}. {why}. 영업이익은 집계치.",
                        action,
                        "관망은 광고 2분기 연속 역성장, 거래서비스 5% 이하와 비GAAP 영업이익률 20% 미만 동시, 현금 1년 15% 감소. 회피는 영업제한·현금흐름 2분기 적자·현금 접근 의문. 상향은 광고 5%·거래서비스 10%·이익률 25% 2분기 또는 자본배분.",
                        "8배 이상은 추가 중단이지 매도 지시가 아닙니다.",
                        "가격만으로 광고·Temu 실적을 충족이라고 단정. 신용, 미수, 추격.",
                    ),
                    4 if zone == "watch" else 3,
                )
            )
            state["zone"] = zone
            state["last_price"] = price
            state["last_multiple"] = round(multiple, 2)
            if weekly:
                state["last_weekly"] = today.isoformat()
            if window:
                sent = list(state.get("sent_dates") or [])
                sent.append(Q3_JUDGE)
                state["sent_dates"] = sent
        else:
            state["last_price"] = price
            state["zone"] = zone
            state["last_multiple"] = round(multiple, 2)
            print(f"price zone unchanged ({zone} {multiple:.2f}x); silent")

    filing = None
    ferr = None
    try:
        filing = latest_sec_date(cfg["sources"]["sec"])
    except Exception as exc:
        ferr = str(exc)
        print(f"sec fail: {exc}")
    last_filing = state.get("last_filing") or base["last_known_filing"]
    if filing and last_filing and filing > last_filing and filing != state.get("filing_alerted"):
        alerts.append(
            (
                "[PDD] 새 공시 | 변경조건 수동 확인",
                body(
                    stamp,
                    f"새 공시 {filing}. 광고 성장, 거래서비스 성장, 비GAAP 영업이익률, 영업현금흐름, 현금은 이 알림이 확정하지 못했습니다.",
                    "investor.pddholdings.com 원문에서 온라인 마케팅, 거래서비스, 비GAAP 영업이익률, 영업현금흐름, 현금·단기투자를 직접 보세요.",
                    "2분기 연속 광고 0% 이하 또는 거래서비스 5% 이하와 이익률 20% 미만이면 관망. 영업제한·현금흐름 적자 2분기면 회피.",
                    "공시 미확인을 매수 유지로 쓰지 마세요.",
                    "파싱 없음을 조건 충족으로 세기. 주가 반응으로 실적 단정.",
                ),
                4,
            )
        )
        state["filing_alerted"] = filing
        state["last_filing"] = filing
    elif ferr and state.get("fetch_fail_date") != today.isoformat() and today.weekday() == 0:
        alerts.append(
            (
                "[PDD] 공시 조회 실패 | 실적 조건 미확인",
                body(
                    stamp,
                    f"SEC 조회 실패. {ferr}. 실적 조건은 미확인입니다.",
                    "investor.pddholdings.com 최신 실적을 직접 보세요.",
                    "다음 월요일에 재시도합니다.",
                    "이번 알림으로 비중을 바꾸지 마세요.",
                    "조회 실패를 조건 미충족으로 두기.",
                ),
                4,
            )
        )
        state["fetch_fail_date"] = today.isoformat()

    for title, msg, pri in alerts:
        print(title)
        print(msg)
        print(f"topic={topic} send={not dry}")
        if not dry:
            publish(topic, title, msg, pri, ["chart_with_upwards_trend"], server)
    if not alerts:
        print("no alert")

    state["updated_at"] = now.isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())