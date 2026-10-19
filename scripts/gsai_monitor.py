#!/usr/bin/env python3
"""GSAI: Samsung Electronics + SK Hynix foreign net-buy reversal signal.

Signal when combined foreign net buy is positive for 2 consecutive trading days
and at least one of those days has combined net buy >= 500 billion KRW.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "gsai.json"
STATE_PATH = ROOT / "state" / "gsai.json"
API = "https://m.stock.naver.com/api/stock/{code}/trend?pageSize=5"


def to_int(s: str) -> int:
    return int(str(s).replace(",", "").replace("+", "").strip() or 0)


def fetch(code: str, day: str) -> dict | None:
    import requests

    r = requests.get(API.format(code=code), headers={"User-Agent": "Mozilla/5.0 cwally-alerts"}, timeout=20)
    r.raise_for_status()
    for row in r.json():
        if row.get("bizdate") == day:
            close = to_int(row["closePrice"])
            fq = to_int(row["foreignerPureBuyQuant"])
            return {"close": close, "frgn_q": fq, "frgn_won": fq * close}
    return None


def eok(won: float) -> str:
    sign = "+" if won > 0 else ""
    if abs(won) >= 1e12:
        return f"{sign}{won / 1e12:.2f}조"
    return f"{sign}{won / 1e8:,.0f}억"


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return dict(fallback)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        out = dict(fallback)
        out.update(data)
        return out
    except json.JSONDecodeError:
        return dict(fallback)


def save(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    dry = "dry" in [a.lower() for a in sys.argv[1:]]
    cfg = load_json(CFG_PATH, {})
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    state = load_json(STATE_PATH, {"history": [], "last_signal_day": None, "updated_at": None})

    if now.year != int(cfg.get("year_guard") or 2026):
        print("gsai year-guard silent")
        return 0

    sys.path.insert(0, str(ROOT / "scripts"))
    import kr_calendar

    session = kr_calendar.session_day(now)
    if not kr_calendar.is_trading_day(session):
        print(f"{session} KRX 휴장/주말 skip")
        return 0
    if session == now.date() and now.strftime("%H:%M") < "15:35":
        print("장중 수급(잠정치)이라 판정 안 함; silent")
        return 0

    day = session.strftime("%Y%m%d")
    codes = cfg.get("codes") or ["005930", "000660"]
    threshold = int(cfg.get("threshold_won") or 500_000_000_000)
    need_days = int(cfg.get("consecutive_days") or 2)

    try:
        rows = {c: fetch(c, day) for c in codes}
    except Exception as exc:
        print(f"fetch fail: {exc}")
        return 0

    if any(v is None for v in rows.values()):
        print(f"{day} data missing for one or more codes; silent")
        return 0

    combined = sum(r["frgn_won"] for r in rows.values())
    history = list(state.get("history") or [])
    history.append({"day": day, "combined": combined, "detail": {c: r["frgn_won"] for c, r in rows.items()}})
    history = history[-10:]

    recent = history[-need_days:]
    consecutive = len(recent) == need_days and all(h["combined"] > 0 for h in recent)
    big_day = any(h["combined"] >= threshold for h in recent)
    signal = consecutive and big_day

    state["history"] = history
    state["updated_at"] = stamp
    print(stamp, "combined", eok(combined), "signal", signal, "recent", [(h["day"], eok(h["combined"])) for h in recent])

    if not signal or state.get("last_signal_day") == day:
        save(state)
        print("gsai silent")
        return 0

    detail_lines = []
    for c, r in rows.items():
        name = "삼성전자" if c == "005930" else "SK하이닉스"
        detail_lines.append(f"- {name} 외국인 {eok(r['frgn_won'])}")

    title = f"[GSAI] 합산 외국인 순매수 전환 | 연속 {need_days}일 + 하루 합산 {eok(threshold)} 이상"
    body = "\n".join([
        f"{stamp} | GSAI",
        "이게 뭐 알림: 삼성전자+SK하이닉스 합산 외국인 순매수 전환 시그널입니다. 매수 주문이 아닙니다.",
        f"지금 상태: 합산 {eok(combined)}. " + " / ".join(detail_lines),
        f"지금 할 일: 연속 {need_days}일 순매수와 하루 합산 {eok(threshold)} 이상 조건 충족. 추가 확인 후 기존 계획 유지.",
        "나중에: 조건 해제 시 신호 중단. 다음 슬롯에서 다시 확인.",
        "익절/줄일 때: 이 알림은 매수 주문이 아닙니다. 보유 기준을 따르세요.",
        "하지 말 것: 시그널만 보고 즉시 추격 매수. 신용/미수.",
    ])
    print(body)
    if not dry:
        publish(resolve_topic("port", cfg.get("ntfy_topic")), title, body, 4, ["rocket"], resolve_server(cfg.get("ntfy_server")))
        state["last_signal_day"] = day
    save(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
