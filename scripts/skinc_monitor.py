#!/usr/bin/env python3
"""SK Inc (034730) event digest. Morning slot only. Not a trade signal."""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "skinc.json"
STATE_PATH = ROOT / "state" / "skinc.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


def load_state() -> dict:
    state = {"sent": {}, "last_close": None, "updated_at": None}
    if STATE_PATH.exists():
        try:
            state.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return state


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch_close(ticker: str) -> float | None:
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + ticker
        + "?interval=1d&range=5d"
    )
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode())
    closes = payload["chart"]["result"][0]["indicators"]["quote"][0]["close"]
    vals = [float(x) for x in closes if x is not None]
    return vals[-1] if vals else None


def phase(day: str) -> str:
    if day < "2026-11-02":
        return "결제 전"
    if day <= "2026-12-01":
        return "결제 창"
    if day < "2027-01-04":
        return "소각 대기"
    return "소각 예정일"


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    day = now.strftime("%Y-%m-%d")
    end = str(cfg.get("ends_kst") or "")[:10]
    if end and day > end:
        print(f"skinc ended {end}")
        return 0

    baseline = int(cfg["baseline_close"])
    events = set(cfg.get("event_dates") or [])
    move_pct = float(cfg.get("move_alert_pct") or 8)
    state = load_state()
    close = None
    fetch_err = None
    try:
        close = fetch_close(cfg["ticker"])
    except Exception as exc:  # noqa: BLE001
        fetch_err = str(exc)

    is_monday = now.weekday() == 0
    is_event = day in events
    moved = False
    if close and state.get("last_close"):
        moved = abs(close / float(state["last_close"]) - 1.0) * 100.0 >= move_pct
    should = mode in {"force", "install"} or is_monday or is_event or moved
    key = f"{day}:{'install' if mode == 'install' else 'digest'}"
    if mode != "force" and mode != "install" and state.get("sent", {}).get(key):
        print("skinc already sent", key)
        return 0
    if mode == "auto" and not should:
        print("skinc silent", day)
        if close:
            state["last_close"] = close
            state["updated_at"] = now.isoformat()
            save_state(state)
        return 0

    pct = None if close is None else (close / baseline - 1.0) * 100.0
    px = "조회 실패" if close is None else f"{close:,.0f}원 ({pct:+.1f}%, 10/7 58만원 대비)"
    label = phase(day)
    if mode == "install":
        title = "[SKINC] 034730 설치 확인 | 매매 신호 아님"
        do_now = "설치 확인입니다. 매수·매도하지 마세요."
    elif day == "2026-11-02":
        title = "[SKINC] 034730 11/2 결제 시작 | 정규장 추격 금지"
        do_now = "오늘부터 12/1까지 시간외 대량매매 창입니다. 정규장에 물량이 쏟아진다고 보지 마세요."
    elif day == "2026-12-01":
        title = "[SKINC] 034730 매각 창 종료일 | 체결 공시 확인"
        do_now = "창 마지막 날입니다. 체결 공시가 없으면 재원 확정으로 보지 마세요."
    elif day == "2027-01-04":
        title = "[SKINC] 034730 자사주 소각 예정일 | 의결권 회복 아님"
        do_now = "소각은 발행주식 지분율만 올립니다. 의결권 30.5%가 3분의 1로 되돌아가지 않습니다."
    elif close is None:
        title = "[SKINC] 034730 주가 조회 실패 | 매매 보류"
        do_now = "시세를 못 읽었습니다. 가격으로 판단하지 마세요."
    else:
        title = f"[SKINC] 034730 {label} | 추가매수 보류"
        do_now = "이중 할인은 남아 있으나 결제 전입니다. 비중을 늘리지 마세요."

    lines = [
        "이게 뭐 알림: SK㈜(034730) 최태원 지분 매각·자사주 소각 일정 점검. 매수 신호가 아닙니다.",
        f"지금 상태: {day} {label}. 주가 {px}. 결제 창 11/2~12/1, 소각 예정 2027-01-04.",
        f"지금 할 일: {do_now}",
        "나중에: 전략적 투자자 신원·의결권 약정, 소각 완료 공시가 나오면 그때 다시 보세요.",
        "익절/줄일 때: 10/7 종가 58만원 대비 -10% 이하나 하이닉스만 꺾이면 지주 추격은 줄이세요.",
        "하지 말 것: 예상 PER 3.7배를 현금 가치로 단정. 소각이 특별결의 거부권을 복구한다고 보지 마세요.",
    ]
    if fetch_err:
        lines.insert(2, f"시세 오류: {fetch_err[:120]}")
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    publish(topic, title, "\n".join(lines), 3, ["chart_with_upwards_trend"], server)
    if mode == "install":
        print("install send only; state untouched")
        return 0
    state.setdefault("sent", {})[key] = now.isoformat()
    if close:
        state["last_close"] = close
    state["updated_at"] = now.isoformat()
    save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
