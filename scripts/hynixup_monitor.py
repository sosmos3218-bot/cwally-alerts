#!/usr/bin/env python3
"""HYNIXUP upside level break alert."""
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
CFG_PATH = ROOT / "configs" / "hynixup.json"
STATE_PATH = ROOT / "state" / "hynixup.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}
CODE = "000660"


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


def fetch(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def num(text: str) -> float:
    cleaned = str(text).replace(",", "").replace("+", "").strip()
    return float(cleaned) if cleaned and cleaned != "-" else 0.0


def save(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    dry = "dry" in [a.lower() for a in sys.argv[1:]]
    cfg = load_json(CFG_PATH, {})
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    state = load_json(STATE_PATH, {"updated_at": None, "fired": {}, "last_sent": None, "last_fail_day": None})
    levels = sorted([float(x) for x in (cfg.get("levels") or [1300000, 1400000])])
    fired = dict(state.get("fired") or {})
    for lv in levels:
        fired.setdefault(str(int(lv)), False)

    if now.year != int(cfg.get("year_guard") or 2026):
        print("hynixup year-guard silent")
        return 0
    expire = datetime.strptime(cfg.get("expire_date") or "2026-12-31", "%Y-%m-%d").date()
    if now.date() > expire:
        print("hynixup expired silent")
        return 0
    if now.weekday() >= 5:
        print("hynixup weekend silent")
        return 0

    try:
        basic = json.loads(fetch(f"https://m.stock.naver.com/api/stock/{CODE}/basic"))
        close = num(basic.get("closePrice"))
        day = now.strftime("%Y-%m-%d")
    except Exception as exc:
        print(f"fetch fail: {exc}")
        return 0

    for lv in levels:
        key = str(int(lv))
        if close < lv:
            fired[key] = False

    new_hits = []
    for lv in levels:
        key = str(int(lv))
        if close >= lv and not fired.get(key):
            fired[key] = True
            new_hits.append(lv)

    state["fired"] = fired
    state["updated_at"] = stamp
    state["last_bar"] = {"day": day, "close": close}
    print(stamp, "close", close, "new", new_hits)

    if not new_hits:
        save(state)
        print("hynixup silent")
        return 0

    broken = ", ".join(f"{int(lv)}" for lv in new_hits)
    title = f"[HYNIXUP] 종가 {close:,.0f} | {broken} 돌파 | 상방 확인"
    body = "\n".join([
        f"{stamp} | HYNIXUP",
        "이게 뭐 알림: SK하이닉스 상방 레벨 돌파입니다. 매수 신호가 아닙니다.",
        f"지금 상태: 종가 {close:,.0f}. 돌파 레벨: {broken}.",
        "지금 할 일: 안착과 수급을 점검하세요. 하방 시나리오 유지.",
        "나중에: 종가가 레벨 아래로 내려오면 리셋됩니다.",
        "익절/줄일 때: 이 알림은 매수 주문이 아닙니다.",
        "하지 말 것: 즉시 추격 매수 해석. 신용/미수.",
    ])
    print(body)
    if not dry:
        publish(resolve_topic("port", cfg.get("ntfy_topic")), title, body, 4, ["warning"], resolve_server(cfg.get("ntfy_server")))
        state["last_sent"] = {"at": stamp, "hits": new_hits}
    save(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
