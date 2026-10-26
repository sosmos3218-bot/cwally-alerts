#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "poly.json"
STATE_PATH = ROOT / "state" / "poly.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}
GAMMA = "https://gamma-api.polymarket.com/events"


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return dict(fallback)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        fallback.update(data)
        return fallback
    except json.JSONDecodeError:
        return dict(fallback)


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch_top_events(limit: int = 8) -> list[dict]:
    params = urllib.parse.urlencode({
        "active": "true",
        "closed": "false",
        "order": "volume24hr",  # gamma API rejects volume_24hr (422 "order fields are not valid")
        "ascending": "false",
        "limit": str(limit),
    })
    url = f"{GAMMA}?{params}"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode())
    if isinstance(data, dict):
        data = data.get("events") or data.get("data") or []
    return data[:limit] if isinstance(data, list) else []


def yes_prob(market: dict) -> float | None:
    prices = market.get("outcomePrices")
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except Exception:
            return None
    if not isinstance(prices, list) or not prices:
        return None
    try:
        return float(prices[0])
    except (TypeError, ValueError):
        return None


def fmt_pct(v: float | None) -> str:
    if v is None:
        return "조회실패"
    return f"{v*100:.1f}%"


def fmt_vol(v) -> str:
    if v is None:
        return "?"
    try:
        n = float(v)
        if n >= 1_000_000:
            return f"${n/1_000_000:.1f}M"
        if n >= 1_000:
            return f"${n/1_000:.0f}K"
        return f"${n:.0f}"
    except Exception:
        return "?"


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    week = now.strftime("%G-W%V")
    state = load_json(STATE_PATH, {"weekly_sent_week": None})
    limit = int(cfg.get("limit") or 8)

    sunday = now.weekday() == 6
    should = mode in {"weekly", "force", "dry"} or (mode == "auto" and sunday)
    if mode == "event":
        print("poly event; weekly only")
        return 0
    if not should and mode != "dry":
        print("poly silent; wait sunday 21:00 KST")
        if mode != "dry":
            state["updated_at"] = now.isoformat()
            state["last_skip_reason"] = "not_sunday"
            save_state(state)
        return 0
    if mode not in {"force", "dry"} and state.get("weekly_sent_week") == week:
        print("poly weekly already sent")
        if mode != "dry":
            state["updated_at"] = now.isoformat()
            state["last_skip_reason"] = "dup_week"
            save_state(state)
        return 0

    try:
        events = fetch_top_events(limit)
    except Exception as exc:
        print(f"poly fetch fail: {exc}", file=sys.stderr)
        events = []

    lines = []
    for ev in events:
        title = (ev.get("title") or ev.get("slug") or "?")[:60]
        vol = fmt_vol(ev.get("volume24hr") or ev.get("volume_24hr"))
        markets = ev.get("markets") or []
        yp = None
        if markets:
            yp = yes_prob(markets[0])
        lines.append(f"{title} | Yes {fmt_pct(yp)} | 24h {vol}")

    summary = " / ".join(lines) if lines else "조회 실패 또는 이벤트 없음"

    title = f"[POLY] 상위 {len(lines)}개 | 참고용"
    body = "\n".join([
        f"{now.strftime('%Y-%m-%d %H:%M KST')} | POLY 주간",
        "이게 뭐 알림: 폴리마켓 주간 상위 이벤트 브리핑. 투자권유 아님.",
        f"지금 결과: {summary}.",
        "지금 할 일: 참고만. 포지션 변경 없음.",
        "나중에: 특정 이벤트 상세는 polymarket.com에서 확인.",
        "익절/줄일 때: 적용 없음.",
        "하지 말 것: 확률을 매수 신호로 해석하지 말 것.",
    ])

    if mode == "dry":
        print("DRY RUN")
        print(title)
        print(body)
        return 0

    if not lines:
        publish(
            resolve_topic("port", cfg.get("ntfy_topic")),
            "[POLY] 조회 실패 | 신호 미적용",
            body.replace("지금 결과: 조회 실패 또는 이벤트 없음.", "지금 결과: 조회 실패. 항목=events."),
            4,
            ["warning"],
            resolve_server(cfg.get("ntfy_server")),
        )
        state.update({"updated_at": now.isoformat(), "weekly_sent_week": week, "last_skip_reason": "fetch_fail_alert"})
        save_state(state)
        return 1

    publish(
        resolve_topic("port", cfg.get("ntfy_topic")),
        title,
        body,
        3,
        ["chart_with_upwards_trend"],
        resolve_server(cfg.get("ntfy_server")),
    )
    state.update({
        "updated_at": now.strftime("%Y-%m-%d %H:%M KST"),
        "weekly_sent_week": week,
        "last_skip_reason": None,
        "last_count": len(lines),
    })
    save_state(state)
    print(title)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
