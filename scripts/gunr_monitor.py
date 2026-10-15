#!/usr/bin/env python3
"""GUNR watch: weekly summary + daily change alerts. No position (entry waiting)."""
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
CFG_PATH = ROOT / "configs" / "gunr.json"
STATE_PATH = ROOT / "state" / "gunr.json"
UA = {"User-Agent": "cwally-alerts gunr monitor research@example.com"}


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


def fetch_quote(url: str) -> dict | None:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="ignore"))
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        return None
    meta = result[0].get("meta") or {}
    price = meta.get("regularMarketPrice")
    prev = meta.get("previousClose") or meta.get("chartPreviousClose")
    high52 = meta.get("fiftyTwoWeekHigh")
    low52 = meta.get("fiftyTwoWeekLow")
    try:
        return {
            "price": float(price) if price is not None else None,
            "prev": float(prev) if prev is not None else None,
            "high52": float(high52) if high52 is not None else None,
            "low52": float(low52) if low52 is not None else None,
        }
    except (TypeError, ValueError):
        return None


def body(stamp: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join(
        [
            f"{stamp} | GUNR | 진입대기 모니터링",
            "이게 뭐 알림: GUNR(글로벌 업스트림 천연자원 ETF) 가격과 성과를 보는 알림입니다. 매수 지시가 아닙니다.",
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
    change_pct = float(cfg.get("change_pct") or 5.0)

    if mode == "install":
        publish(
            topic,
            "[GUNR] 설치 확인 | 매매 신호 아님",
            body(
                stamp,
                "평일 09:00 KST morning 슬롯에 붙였습니다. 미보유(진입대기). 주간 요약 + 일간 ±5% 변화 시 알림.",
                "설치 확인만 하세요. 사지 마세요.",
                "월요일 상태 요약과 큰 변동 때만 보냅니다.",
                "이번 알림으로 진입하지 마세요.",
                "설치 확인을 매수 신호로 읽기.",
            ),
            3,
            ["information_source"],
            server,
        )
        print("install confirmation sent")
        return 0

    dry = mode == "dry" or os.environ.get("GUNR_DRY") == "1"
    state = load_json(
        STATE_PATH,
        {
            "updated_at": None,
            "last_price": None,
            "last_weekly": None,
            "fetch_fail_date": None,
        },
    )
    today = now.date()
    q = None
    try:
        q = fetch_quote(cfg["sources"]["quote"])
    except Exception as exc:
        print(f"quote fail: {exc}")

    alerts = []
    if not q or q.get("price") is None:
        if state.get("fetch_fail_date") != today.isoformat():
            alerts.append(
                (
                    "[GUNR] 주가 조회 실패 | 조건 미확인",
                    body(
                        stamp,
                        "주가 조회 실패. 조건 미충족이 아닙니다.",
                        "증권 앱에서 GUNR 전일 종가를 직접 보세요.",
                        "다음 평일 09:00 KST에 재시도합니다.",
                        "이번 알림으로 진입하지 마세요.",
                        "조회 실패를 정상이나 매수 신호로 취급.",
                    ),
                    4,
                )
            )
            state["fetch_fail_date"] = today.isoformat()
    else:
        price = q["price"]
        prev = q.get("prev") or state.get("last_price")
        day_pct = ((price / prev - 1) * 100) if prev and prev > 0 else 0.0
        high52 = q.get("high52")
        low52 = q.get("low52")
        from_high = ((price / high52 - 1) * 100) if high52 and high52 > 0 else None
        from_low = ((price / low52 - 1) * 100) if low52 and low52 > 0 else None

        weekly = today.weekday() == 0 and state.get("last_weekly") != today.isoformat()
        big_change = abs(day_pct) >= change_pct
        force = mode == "force"

        if weekly or big_change or force:
            why = "월요일 주간 요약" if weekly else f"일간 {day_pct:+.1f}%" if big_change else "강제"
            range_note = ""
            if from_high is not None:
                range_note += f" 52주 고점 대비 {from_high:+.1f}%."
            if from_low is not None:
                range_note += f" 저점 대비 {from_low:+.1f}%."
            status = f"GUNR {price:.2f}달러. 전일 대비 {day_pct:+.1f}%.{range_note} 미보유(진입대기)."
            if abs(day_pct) >= 5 or (from_high is not None and from_high < -10):
                action = "큰 변동 또는 고점 대비 할인 구간입니다. 추가 분석 후 진입 여부를 판단하세요. 이 알림만으로 사지 마세요."
            else:
                action = "관망 유지. 진입 조건(밸류에이션, 원자재 사이클)을 직접 확인하세요."
            alerts.append(
                (
                    f"[GUNR] {why} | 진입대기 상태",
                    body(
                        stamp,
                        status,
                        action,
                        "원자재(원유/구리) 흐름과 지정학을 함께 보세요. 장기 사이클 의존.",
                        "고점 근접 시 추격 금지. 큰 하락 시 분할 고려 가능하나 확인 필수.",
                        "이 알림을 매수 신호로 쓰기. 신용/미수/추격.",
                    ),
                    4 if big_change else 3,
                )
            )
            if weekly:
                state["last_weekly"] = today.isoformat()
            state["last_price"] = price
        else:
            state["last_price"] = price
            print(f"no change ({day_pct:+.1f}%); silent")

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
