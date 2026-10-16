#!/usr/bin/env python3
"""HYNIXUP upside level alert (stock/000660). Silent unless the close newly crosses above a level or fetch fails."""
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
DRY = False
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


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
    if DRY:
        return
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fail_once(state: dict, now: datetime, cfg: dict, title: str, note: str, dry: bool) -> None:
    day = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    body = "\n".join([
        f"{stamp} | HYNIXUP",
        "이게 뭐 알림: SK하이닉스(000660) 상방 레벨 점검 중 시세 조회가 실패했습니다.",
        f"지금 상태: {note}",
        "지금 할 일: 네이버 SK하이닉스(000660) 종가를 직접 확인하세요.",
        "나중에: 다음 평일 15:40 KST에 다시 봅니다.",
        "익절/줄일 때: 이 실패로 매매하지 마세요.",
        "하지 말 것: 조회 실패를 레벨 돌파로 해석.",
    ])
    print(body)
    if not dry and state.get("last_fail_day") != day:
        publish(resolve_topic("port", cfg.get("ntfy_topic")), title, body, 4, ["warning"], resolve_server(cfg.get("ntfy_server")))
        state["last_fail_day"] = day
    state["updated_at"] = stamp
    save(state)


def latest_closed(price_rows: list, basic: dict) -> dict | None:
    status = str((basic or {}).get("marketStatus") or "").upper()
    today = datetime.now(KST).strftime("%Y-%m-%d")
    rows = list(price_rows or [])
    now = datetime.now(KST)
    after_close = now.weekday() < 5 and now.strftime("%H:%M") >= "15:30"
    if rows and (status == "CLOSE" or after_close) and str(rows[0].get("localTradedAt") or "")[:10] == today:
        return rows[0]
    for row in rows:
        if str(row.get("localTradedAt") or "")[:10] < today:
            return row
    return None


def main() -> int:
    global DRY
    dry = "dry" in [a.lower() for a in sys.argv[1:]]
    DRY = dry
    if dry:
        print("DRY RUN: no ntfy, no state write")
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
        basic = json.loads(fetch(cfg["sources"]["basic"]))
        price_rows = json.loads(fetch(cfg["sources"]["price"]))
    except Exception as exc:
        fail_once(state, now, cfg, "[HYNIXUP] 시세 조회 실패 | 판정 보류", f"SK하이닉스(000660) 시세 조회 실패: {exc}", dry)
        return 0

    bar = latest_closed(price_rows, basic)
    if not bar:
        print("hynixup no closed bar; silent")
        state["updated_at"] = stamp
        save(state)
        return 0

    day = str(bar.get("localTradedAt") or "")[:10]
    close = num(bar.get("closePrice"))

    # Reset if fell back below a level
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
    print(stamp, "day", day, "close", close, "fired", fired, "new", new_hits)

    if not new_hits:
        save(state)
        print("hynixup silent; no new level cross")
        return 0

    broken = ", ".join(f"{int(lv):,}" for lv in new_hits)
    title = f"[HYNIXUP] 종가 {close:,.0f} | {broken} 돌파 | 추격매수 금지"
    body = "\n".join([
        f"{stamp} | HYNIXUP",
        "이게 뭐 알림: SK하이닉스(000660) 종가가 상방 레벨을 위로 넘었습니다. 매수 신호가 아닙니다.",
        f"지금 상태: {day} 종가 {close:,.0f}. 돌파 레벨: {broken}.",
        "지금 할 일: 보유 비중과 익절 기준을 점검하세요. 새로 사지 않습니다.",
        "나중에: 종가가 해당 레벨 아래로 다시 내려가면 같은 레벨은 리셋됩니다.",
        "익절/줄일 때: 기존 종목별 익절 규칙을 따르세요. 이 알림 자체는 주문이 아닙니다.",
        "하지 말 것: 레벨 돌파를 보고 추격매수. 신용/미수.",
    ])
    print(body)
    if not dry:
        publish(resolve_topic("port", cfg.get("ntfy_topic")), title, body, 4, ["warning"], resolve_server(cfg.get("ntfy_server")))
        state["last_sent"] = {"at": stamp, "hits": new_hits}
    save(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
