#!/usr/bin/env python3
"""KOSPI downside level alert. Silent unless the daily close newly drops below a level or a fetch fails."""
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
CFG_PATH = ROOT / "configs" / "kosdown.json"
STATE_PATH = ROOT / "state" / "kosdown.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return dict(fallback)
    try:
        out = dict(fallback)
        out.update(json.loads(path.read_text(encoding="utf-8")))
        return out
    except json.JSONDecodeError:
        return dict(fallback)


def fetch(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def num(text: str) -> float:
    cleaned = str(text).replace(",", "").replace("+", "").strip()
    if not cleaned or cleaned == "-":
        return 0.0
    return float(cleaned)


def save(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def latest_closed(price_rows: list[dict], basic: dict) -> dict | None:
    status = str((basic or {}).get("marketStatus") or "").upper()
    today = datetime.now(KST).strftime("%Y-%m-%d")
    rows = list(price_rows or [])
    if status == "CLOSE" and rows:
        return rows[0]
    for row in rows:
        if str(row.get("localTradedAt") or "")[:10] < today:
            return row
    return None


def main() -> int:
    dry = "dry" in [a.lower() for a in sys.argv[1:]]
    cfg = load_json(CFG_PATH, {})
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    levels = sorted((float(x) for x in cfg.get("levels") or []), reverse=True)
    state = load_json(STATE_PATH, {"updated_at": None, "fired": {}, "last_sent": None, "last_fail_day": None})
    fired = {str(int(lv)): bool((state.get("fired") or {}).get(str(int(lv)))) for lv in levels}
    if dry:
        print("DRY RUN: no ntfy, no state write")
    if now.weekday() >= 5:
        print("kosdown weekend silent")
        return 0

    try:
        basic = json.loads(fetch(cfg["sources"]["basic"]))
        rows = json.loads(fetch(cfg["sources"]["price"]))
    except Exception as exc:
        day = now.strftime("%Y-%m-%d")
        title = "[KOSDOWN] 코스피 시세 조회 실패 | 판정 보류"
        body = "\n".join(
            [
                f"{stamp} | KOSDOWN",
                "이게 뭐 알림: 코스피 하락 레벨 점검 중 조회가 실패했습니다.",
                f"지금 상태: {exc}",
                "지금 할 일: 실패를 레벨 유지로 보지 마세요. 네이버 코스피 종가를 직접 확인하세요.",
                "나중에: 다음 평일 15:40 KST에 다시 봅니다.",
                "익절/줄일 때: 이 실패로 사거나 팔지 마세요.",
                "하지 말 것: 조회 실패를 하락 신호로 해석.",
            ]
        )
        print(body)
        if not dry and state.get("last_fail_day") != day:
            publish(resolve_topic("port", cfg.get("ntfy_topic")), title, body, 3, ["warning"], resolve_server(cfg.get("ntfy_server")))
            state["last_fail_day"] = day
            state["updated_at"] = stamp
            save(state)
        return 0

    bar = latest_closed(rows, basic)
    if not bar:
        print("kosdown no closed bar; silent")
        return 0
    day = str(bar.get("localTradedAt") or "")[:10]
    close = num(bar.get("closePrice"))

    new_hits = []
    for lv in levels:
        key = str(int(lv))
        if close > lv:
            fired[key] = False
        elif close < lv and not fired[key]:
            fired[key] = True
            new_hits.append(lv)

    print(stamp, "day", day, "close", close, "fired", fired, "new", new_hits)
    if dry:
        if new_hits:
            print("would send: [KOSDOWN] 코스피 종가 " + ", ".join(f"{lv:,.0f}" for lv in new_hits) + " 아래")
        return 0

    state.update({"fired": fired, "updated_at": stamp, "last_bar": {"day": day, "close": close}})
    if not new_hits:
        save(state)
        print("kosdown silent; no new level break")
        return 0

    lows = ", ".join(f"{lv:,.0f}" for lv in new_hits)
    below = [k for k, v in fired.items() if v]
    title = f"[KOSDOWN] 코스피 종가 {lows} 아래 | 새 매수 보류"
    body = "\n".join(
        [
            f"{stamp} | KOSDOWN",
            "이게 뭐 알림: 코스피 종가가 정해둔 하락 레벨 아래로 내려갔습니다.",
            f"지금 상태: {day} 종가 {close:,.2f}. 새로 깬 레벨 {lows}. 지금 아래인 레벨 {', '.join(below)}.",
            "지금 할 일: 새 매수는 잠시 멈추고 보유 비중을 확인하세요.",
            "나중에: 종가가 레벨 위로 회복하면 그 레벨은 리셋되고 다시 깨지면 또 알립니다.",
            "익절/줄일 때: 이 알림은 매도 주문이 아닙니다. 기존 종목별 규칙을 따르세요.",
            "하지 말 것: 공포 추격 매도. 신용/미수.",
        ]
    )
    print(body)
    publish(resolve_topic("port", cfg.get("ntfy_topic")), title, body, 4, ["chart_with_downwards_trend"], resolve_server(cfg.get("ntfy_server")))
    state["last_sent"] = {"at": stamp, "levels": [int(x) for x in new_hits]}
    save(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
