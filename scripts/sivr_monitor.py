#!/usr/bin/env python3
"""SIVR price-ladder watch. Silent unless a tier level is crossed. Not a buy order.

Looks at the last US daily close (Yahoo chart API). Runs in the weekday 09:00 KST morning slot.
Modes: auto (default), dry (no send, no state write; SIVR_TEST_CLOSE overrides the close), force (ignore valid_until).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "sivr.json"
STATE_PATH = ROOT / "state" / "sivr.json"
UA = {"User-Agent": "cwally-alerts sivr research monitor research@example.com"}
STALE_DAYS = 6


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
    last: Exception | None = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=25) as resp:
                return resp.read().decode("utf-8", errors="ignore")
        except Exception as exc:  # retry network hiccups and 429
            last = exc
            time.sleep(3 * (attempt + 1))
    raise last if last else RuntimeError("fetch failed")


def last_close(url: str) -> tuple[float | None, str | None, str | None]:
    """Return (close, bar_date_iso_in_NY, error)."""
    data = json.loads(fetch(url))
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        return None, None, "quote empty"
    res = result[0]
    stamps = res.get("timestamp") or []
    closes = ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    gmtoffset = (res.get("meta") or {}).get("gmtoffset") or 0
    for ts, cl in zip(reversed(stamps), reversed(closes)):
        if cl is None:
            continue
        try:
            price = float(cl)
        except (TypeError, ValueError):
            continue
        day = datetime.fromtimestamp(int(ts) + int(gmtoffset), timezone.utc).date().isoformat()
        return price, day, None
    return None, None, "no close in range"


def body(stamp: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join(
        [
            f"{stamp} | SIVR | 은 현물 ETF 가격 사다리",
            "이게 뭐 알림: SIVR(은 현물 ETF)이 관망에서 매수로 바꿀 가격 기준에 닿았을 때만 울립니다. 매수 지시가 아닙니다.",
            f"지금 결과/상태: {status}",
            f"지금 할 일: {action}",
            f"나중에: {later}",
            f"익절/줄일 때: {trim}",
            f"하지 말 것: {avoid}",
        ]
    )


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    dry = mode == "dry" or os.environ.get("SIVR_DRY") == "1"
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = load_json(
        STATE_PATH,
        {"updated_at": None, "last_bar_date": None, "last_close": None, "fired": {}, "upper_on": False, "fetch_fail_date": None},
    )
    now = datetime.now(KST)
    today = now.date()
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    if today.isoformat() > cfg["valid_until"] and mode not in {"force", "dry"}:
        print(f"after {cfg['valid_until']}; silent")
        return 0

    tiers = sorted(cfg["tiers"], key=lambda t: -float(t["level"]))
    total = int(cfg.get("tier_total", len(tiers)))
    rearm = float(cfg.get("rearm_pct", 6.0)) / 100.0
    upper = cfg["upper"]
    jpm = cfg["baseline"]["jpm_target_sivr_equiv"]

    close, bar, qerr = None, None, None
    test = os.environ.get("SIVR_TEST_CLOSE")
    if dry and test:
        close, bar = float(test), today.isoformat()
    else:
        try:
            close, bar, qerr = last_close(cfg["sources"]["quote"])
        except Exception as exc:
            qerr = str(exc)
    print(f"close={close} bar={bar} err={qerr}")

    alerts: list[tuple[str, str, int]] = []
    stale = False
    if close is not None and bar:
        age = (today - datetime.strptime(bar, "%Y-%m-%d").date()).days
        stale = age > STALE_DAYS
    if close is None or stale:
        reason = qerr or f"마지막 종가 {bar}가 {STALE_DAYS}일 넘게 오래됨"
        if state.get("fetch_fail_date") != today.isoformat():
            alerts.append(
                (
                    "[SIVR] 시세 조회 실패 | 조건 미확인",
                    body(
                        stamp,
                        f"SIVR 종가 조회 실패. {reason}. 조건 미충족이 아닙니다.",
                        "야후나 증권 앱에서 SIVR 종가를 직접 보세요. 이 알림으로 50, 45, 40, 66달러 도달 여부를 판정하지 마세요.",
                        "다음 평일 09:00 KST에 재시도합니다.",
                        "이번 알림으로 비중을 바꾸지 마세요.",
                        "조회 실패를 가격 미도달이나 정상으로 취급.",
                    ),
                    4,
                )
            )
            state["fetch_fail_date"] = today.isoformat()
    elif state.get("last_bar_date") == bar and not dry:
        print(f"bar {bar} already evaluated; silent")
    else:
        fired = dict(state.get("fired") or {})
        # re-arm tiers once the close recovers above level * (1 + rearm)
        for t in tiers:
            if fired.get(t["id"]) and close >= float(t["level"]) * (1 + rearm):
                fired[t["id"]] = False
        reached = [t for t in tiers if close <= float(t["level"])]
        fresh = [t for t in reached if not fired.get(t["id"])]
        if fresh:
            deepest = max(fresh, key=lambda t: t["step"])
            k = len(reached)
            lvl = int(float(deepest["level"]))
            gap = f"{len(fresh)}개 단계를 한 번에 지났습니다. " if len(fresh) > 1 else ""
            alerts.append(
                (
                    f"[SIVR] {lvl}달러 이하 | {deepest['label']} 매수 구간",
                    body(
                        stamp,
                        f"SIVR 종가 {close:.2f}달러({bar} 미국장). {gap}기준 {lvl}달러 이하. 지금까지 닿은 단계 {k}/{total}.",
                        f"목표 비중의 {k}/{total}까지만 매수를 검토하세요. 이미 사 둔 단계는 다시 사지 마세요. 목표 비중은 포트폴리오의 5% 이하입니다.",
                        f"더 내려가면 다음 단계에서 1/{total}씩 추가합니다. 목표가(J.P. Morgan 환산 약 {jpm:.0f}달러)가 더 낮아졌는지 먼저 확인하세요.",
                        "정해 둔 익절선은 없습니다. 은 비중이 목표의 1.5배를 넘으면 줄이는 것을 검토하세요.",
                        "한 번에 전액 매수. 신용, 미수. 목표가 하향이나 연준 인상 여부를 확인하지 않고 매수.",
                    ),
                    4,
                )
            )
            for t in fresh:
                fired[t["id"]] = True
        state["fired"] = fired

        up_lvl = float(upper["level"])
        up_on = bool(state.get("upper_on"))
        if close >= up_lvl and not up_on:
            alerts.append(
                (
                    f"[SIVR] {int(up_lvl)}달러 이상 | 추격 금지",
                    body(
                        stamp,
                        f"SIVR 종가 {close:.2f}달러({bar} 미국장). 위쪽 기준 {int(up_lvl)}달러 이상. 매수 기준(50달러 이하)과 멀어졌습니다.",
                        "관망 유지. 새로 사지 마세요.",
                        f"{float(upper['rearm_level']):.0f}달러 아래로 내려오면 이 알림이 다시 켜집니다. 가격 사다리는 그대로입니다.",
                        "이미 보유 중이면 은 비중이 목표의 1.5배를 넘는지 확인하세요.",
                        "오른다고 추격 매수. 신용, 미수.",
                    ),
                    3,
                )
            )
            state["upper_on"] = True
        elif up_on and close <= float(upper["rearm_level"]):
            state["upper_on"] = False
        state["last_bar_date"] = bar
        state["last_close"] = close
        print(f"fired={state['fired']} upper_on={state['upper_on']}")

    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    for title, msg, pri in alerts:
        print(title)
        print(msg)
        print(f"topic={topic} send={not dry}")
        if not dry:
            publish(topic, title, msg, pri, ["chart_with_downwards_trend"], server)
    if not alerts:
        print("no alert")

    if dry:
        print("dry: state not written")
        return 0
    state["updated_at"] = now.isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
