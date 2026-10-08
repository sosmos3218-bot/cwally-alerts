#!/usr/bin/env python3
"""464310 change-condition watch. Silent unless zone change or Monday."""
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
CFG_PATH = ROOT / "configs" / "464310.json"
STATE_PATH = ROOT / "state" / "464310.json"
UA = {"User-Agent": "cwally-alerts 464310 research monitor research@example.com"}


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


def zone_of(price: float | None, low_break: float, near_low: float) -> str:
    if price is None:
        return "unknown"
    if price <= low_break:
        return "break"
    if price <= near_low:
        return "near"
    return "mid"


def body(stamp: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join(
        [
            f"{stamp} | 464310 | 변경조건 점검",
            "이게 뭐 알림: TIGER 글로벌AI&로보틱스 INDXX 가격 조건만 보는 알림입니다. 매수나 매도 지시가 아닙니다.",
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
    levels = cfg["levels"]
    base = cfg["baseline"]
    if mode == "install":
        publish(
            topic,
            "[464310] 설치 확인 | 매매 신호 아님",
            body(
                stamp,
                f"평일 15:20 KST afternoon 슬롯에 붙였습니다. 기준가 {base['price_date']} {base['price']:,.0f}원. 52주 저가 {levels['low_break']:,.0f}원, 근접 {levels['near_low']:,.0f}원, 고가 {levels['high_52w']:,.0f}원. 판단은 중립입니다.",
                "설치 확인만 하세요. 사지 마세요.",
                "월요일 상태, 구간 변경만 보냅니다. 일본 수주와 PMI는 확인하지 않습니다.",
                "이번 알림으로 비중을 바꾸지 마세요.",
                "설치 확인을 매수나 코어 편입 신호로 읽기. 국내 휴머노이드 ETF와 같은 위험으로 보기.",
            ),
            3,
            ["information_source"],
            server,
        )
        print("install confirmation sent")
        return 0

    dry = mode == "dry" or os.environ.get("ETF464310_DRY") == "1"
    state = load_json(
        STATE_PATH,
        {
            "updated_at": None,
            "last_price": None,
            "zone": None,
            "last_weekly": None,
            "fetch_fail_date": None,
        },
    )
    today = now.date()
    if today.isoformat() > cfg["valid_until"] and mode not in {"force", "dry"}:
        print(f"after {cfg['valid_until']}; silent")
        return 0

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
                    "[464310] 시세 조회 실패 | 조건 미확인",
                    body(
                        stamp,
                        f"가격을 읽지 못했습니다. {qerr or '빈 응답'}. 조건 미충족이 아닙니다.",
                        "증권 앱에서 464310 가격을 직접 보세요. 이 알림으로 구간을 판정하지 마세요.",
                        "다음 평일 15:20 KST에 재시도합니다.",
                        "이번 알림으로 줄이거나 사지 마세요.",
                        "조회 실패를 저점 이탈이나 정상으로 취급.",
                    ),
                    4,
                )
            )
            state["fetch_fail_date"] = today.isoformat()
    else:
        zone = zone_of(price, levels["low_break"], levels["near_low"])
        prev = state.get("zone") or "mid"
        changed = zone != prev and state.get("zone") is not None
        weekly = today.weekday() == 0 and state.get("last_weekly") != today.isoformat()
        if changed or weekly or mode == "force":
            gap = (price / base["price"] - 1) * 100
            dd = (price / levels["high_52w"] - 1) * 100
            px = f"{price:,.0f}원"
            if zone == "break":
                title = "[464310] 52주 저가 이탈 | 회피 검토"
                action = "가격 조건만 충족입니다. 매도 지시가 아닙니다. 일본 수주는 직접 확인하세요."
                pri = 5
            elif zone == "near":
                title = "[464310] 52주 저가 근접 | 위성만"
                action = "저점 근처입니다. 코어로 넣지 마세요. 수주 확인 전 비중을 늘리지 마세요."
                pri = 4
            else:
                title = "[464310] 변경조건 밖 | 관망"
                action = "12,720원 이탈도, 저점 3% 근접도 아닙니다. 비중을 바꾸지 마세요."
                pri = 3
            why = "구간 변경" if changed else "월요일 상태" if weekly else "수동 확인"
            alerts.append(
                (
                    title,
                    body(
                        stamp,
                        f"464310 {px}. 기준가 대비 {gap:+.1f}%. 52주 고가 대비 {dd:+.1f}%. 구간 {prev} -> {zone}. {why}. 기준 {base['price_date']} {base['price']:,.0f}원. 판단은 중립입니다.",
                        action,
                        "상향은 12,720원 유지와 일본 공장자동화 수주 바닥. 수주는 이 알림이 확인하지 않습니다.",
                        "12,720원 이탈은 재검토이지 전량매도 지시가 아닙니다. 위성 이상은 두지 마세요.",
                        "매수 보고서의 1개월 반등을 사실로 읽기. 국내 휴머노이드 ETF와 같은 표로 비교. 신용, 미수.",
                    ),
                    pri,
                )
            )
            state["zone"] = zone
            state["last_price"] = price
            if weekly:
                state["last_weekly"] = today.isoformat()
        else:
            state["last_price"] = price
            if state.get("zone") is None:
                state["zone"] = zone
            print(f"zone unchanged ({zone}); silent")

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
