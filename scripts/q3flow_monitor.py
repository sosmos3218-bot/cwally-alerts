#!/usr/bin/env python3
"""2026-10-08 Samsung drop: flow thesis checkpoint. Not a trade signal.

Shots: 2026-10-12 08:42 KST (next session reminder), 2026-10-29 16:42 KST
(after the confirmed-results window). Year-guarded. Does not score HBM4
contracts or segment profit; those stay with [HBMASP] and [SECQ3].
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "q3flow.json"
STATE_PATH = ROOT / "state" / "q3flow.json"
NEWS = "https://news.samsung.com/kr/%EC%82%BC%EC%84%B1%EC%A0%84%EC%9E%90-2026%EB%85%84-3%EB%B6%84%EA%B8%B0-%EC%8B%A4%EC%A0%81-%EB%B0%9C%ED%91%9C"
UA = {"User-Agent": "Mozilla/5.0"}


def now_kst() -> datetime:
    return datetime.now(KST)


def load_cfg() -> dict:
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"sent": {}, "updated_at": None}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("sent"), dict):
            return data
    except json.JSONDecodeError:
        pass
    return {"sent": {}, "updated_at": None}


def save_state(state: dict, now: datetime) -> None:
    state["updated_at"] = now.isoformat(timespec="seconds")
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE_PATH)


def body_base(cfg: dict) -> str:
    b = cfg["baseline"]
    return (
        "이게 뭐 알림: 10월 8일 삼성전자 잠정실적 당일 하락이 실적 쇼크인지 수급인지 보는 점검입니다.\n"
        f"지금 결과/상태: 매출 {b['sales_jo']}조, 영업이익 {b['op_jo']}조. 종가 {b['close']:,}원({b['stock_pct']}%), "
        f"코스피 {b['kospi']}({b['kospi_pct']}%). 외국인 약 {b['foreign_sell_jo']}조, 기관 약 {b['inst_sell_jo']}조 순매도.\n"
        "지금 할 일: 당일 하락만으로 팔거나 쫓아 사지 마세요. 기준 판단은 수급·차익실현입니다.\n"
        f"나중에: {b['hbm4']} 확정 계약과 부문 손익은 10월 29일 창의 [SECQ3]를 보세요.\n"
        "익절/줄일 때: 회사 원문에 HBM4 매출이 전분기 대비 3배에 뚜렷이 못 미치거나, DX 적자가 3.1조원을 넘거나, 107.4조 대부분이 일회성이면 판단을 바꿉니다.\n"
        "하지 말 것: 제시가 3배를 확정 계약으로 보지 마세요. 이 알림은 매매 신호가 아닙니다."
    )


def news_posted() -> bool:
    try:
        req = urllib.request.Request(NEWS, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as resp:
            text = resp.read(8000).decode("utf-8", errors="ignore")
        return "3분기 실적 발표" in text and "잠정" not in text[:400]
    except Exception as exc:
        print(f"newsroom skipped: {exc}", file=sys.stderr)
        return False


def send(cfg: dict, title: str, message: str) -> None:
    publish(cfg["ntfy_topic"], title, message, priority=3, tags=["chart_with_downwards_trend"])


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "auto"
    cfg = load_cfg()
    now = now_kst()
    if mode == "install":
        send(cfg, "[Q3FLOW] 설치 확인 | 매매 신호 아님", body_base(cfg).replace(
            "이게 뭐 알림: 10월 8일 삼성전자 잠정실적 당일 하락이 실적 쇼크인지 수급인지 보는 점검입니다.",
            "이게 뭐 알림: 10월 8일 수급 판단 점검 알림 설치 확인입니다.",
        ))
        return 0
    if now.year != cfg["year_guard"]:
        print("year guard, silent")
        return 0
    stamp = now.strftime("%Y-%m-%d %H:%M")
    due = [t for t in cfg["oneshot_kst"] if t[:10] == stamp[:10]]
    if mode == "auto" and not due:
        print("no shot today, silent")
        return 0
    key = due[0] if due else stamp[:10]
    state = load_state()
    if mode == "auto" and state["sent"].get(key):
        print(f"already sent {key}")
        return 0
    if key.startswith("2026-10-29"):
        posted = news_posted()
        status = "확정 보도자료 페이지는 열렸습니다. 부문 숫자 판정은 [SECQ3]를 보세요." if posted else "확정 보도자료는 이 시점에 확인되지 않았습니다. 수급 판단을 유지합니다."
        title = "[Q3FLOW] 확정 창 | 수급 판단 유지"
        message = body_base(cfg) + f"\n지금 결과/상태 추가: {status}"
    else:
        title = "[Q3FLOW] 다음 장 전 | 당일 하락은 수급"
        message = body_base(cfg)
    if mode == "dry":
        print(title)
        print(message)
        return 0
    send(cfg, title, message)
    state["sent"][key] = now.isoformat(timespec="seconds")
    save_state(state, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())