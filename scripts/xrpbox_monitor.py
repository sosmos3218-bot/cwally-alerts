#!/usr/bin/env python3
"""XRP box: 1.45 close break, 1.54 reclaim, 1.60/1.70 settle. No new cron."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "xrpbox.json"
STATE_PATH = ROOT / "state" / "xrpbox.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


def fetch(url: str) -> str:
    import urllib.request

    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def fetch_json(url: str):
    return json.loads(fetch(url))


def load_state() -> dict:
    state = {
        "verdict": None,
        "baseline_sent": False,
        "fail_notified": False,
        "missing_notified": 0,
        "updated_at": None,
    }
    if STATE_PATH.exists():
        try:
            state.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return state


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch_close(cfg: dict) -> tuple[float | None, str, list[str]]:
    errors = []
    try:
        rows = fetch_json(cfg["sources"]["price"][0])
        if rows:
            rows = sorted(rows, key=lambda r: int(r[0]))
            now_utc = datetime.now(timezone.utc)
            closed = [r for r in rows if int(r[0]) + 86400 <= now_utc.timestamp()]
            candle = closed[-1] if closed else rows[-2]
            close = float(candle[4])
            day = datetime.fromtimestamp(int(candle[0]), tz=timezone.utc).strftime("%Y-%m-%d")
            return close, f"coinbase UTC 일봉 {day} 종가 {close:.4f}", errors
        errors.append("coinbase: 캔들 없음")
    except Exception as exc:
        errors.append(f"coinbase 캔들 실패: {exc}")
    try:
        data = fetch_json(cfg["sources"]["price"][1])
        pair = next(k for k in data["result"] if k != "last")
        rows = data["result"][pair]
        candle = rows[-2]
        close = float(candle[4])
        day = datetime.fromtimestamp(int(candle[0]), tz=timezone.utc).strftime("%Y-%m-%d")
        return close, f"kraken UTC 일봉 {day} 종가 {close:.4f}", errors
    except Exception as exc:
        errors.append(f"kraken 실패: {exc}")
    try:
        data = fetch_json(cfg["sources"]["price"][2])
        last = float(data["data"]["amount"])
        return last, f"coinbase 현재가 {last:.4f} (종가 대체, 이탈 판정 아님)", errors
    except Exception as exc:
        errors.append(f"coinbase spot 실패: {exc}")
    return None, "", errors


def verdict(close: float | None, close_note: str, cfg: dict) -> str:
    if close is None:
        return "FETCH_FAIL"
    if "종가 대체" in close_note:
        return "DATA_MISSING"
    lv = cfg["levels"]
    if close < float(lv["next_demand"]):
        return "BREAK_130"
    if close < float(lv["invalidate_close"]):
        return "BREAK_145"
    if close >= float(lv["settle_close"]):
        return "SETTLE_170"
    if close >= float(lv["breakout_close"]):
        return "BREAKOUT_160"
    if close >= float(lv["reclaim_close"]):
        return "RECLAIM_154"
    return "RANGE_HOLD"


def copy_for(kind: str, cfg: dict, close, close_note: str) -> tuple[str, list[str], int]:
    px = f"{close:.4f}달러" if close is not None else "없음"
    explain = cfg["explain"]
    titles = {
        "FETCH_FAIL": "[PORT] XRP 조회 실패 | 판정 보류",
        "DATA_MISSING": "[PORT] XRP 종가 없음 | 판정 보류",
        "BREAK_130": "[PORT] XRP 1.30 이탈 | 1.20 수요 확인",
        "BREAK_145": "[PORT] XRP 1.45 이탈 | 반등 논리 폐기",
        "SETTLE_170": "[PORT] XRP 1.70 안착 | 다음 공급 1.80",
        "BREAKOUT_160": "[PORT] XRP 1.60 안착 | 1.80 대기, 2.40 보류",
        "RECLAIM_154": "[PORT] XRP 1.54 회복 | 1.60 재시험 후보",
        "RANGE_HOLD": "[PORT] XRP 박스 유지 | 추가매수 보류",
    }
    actions = {
        "FETCH_FAIL": "시세를 직접 확인하고 이 알림으로 매매하지 마세요.",
        "DATA_MISSING": "마감 일봉 종가가 없습니다. 현재가로 이탈을 판정하지 마세요.",
        "BREAK_130": "종가가 1.30달러 아래입니다. 다음 수요는 1.20달러입니다. 반등 논리는 이미 폐기된 상태입니다.",
        "BREAK_145": "일봉 종가가 1.45달러 아래입니다. 단기 반등 논리를 폐기하고 1.30달러, 그다음 1.20달러를 보세요. 추가매수하지 마세요.",
        "SETTLE_170": "1.70달러 일봉 종가 안착입니다. 다음 공급은 1.80달러입니다. 2.40달러는 이제 지도에만 남기고 추격하지 마세요.",
        "BREAKOUT_160": "1.60달러는 넘었고 1.70달러 안착 전입니다. 다음 공급 후보는 1.80달러, 2.40달러는 보류입니다. 추가매수하지 마세요.",
        "RECLAIM_154": "1.45달러는 지켰고 1.54달러를 회복했습니다. 1.60달러 재시험 후보만 열고 비중은 늘리지 마세요.",
        "RANGE_HOLD": "1.45~1.54달러 박스입니다. 보유 유지, 추가매수 보류입니다. 1.45달러 종가 이탈 전에는 추격하지 마세요.",
    }
    pri = 5 if kind in {"BREAK_145", "BREAK_130"} else 4 if kind in {"SETTLE_170", "BREAKOUT_160", "FETCH_FAIL"} else 3
    return (
        titles[kind],
        [
            f"이게 뭐 알림: {explain}",
            f"지금 결과: {kind} | XRP {px} | {close_note}.",
            f"지금 할 일: {actions[kind]}",
            "나중에: 평일 09:00 KST에만 다시 봅니다. 판정이 같으면 알림을 반복하지 않습니다. 주말 종가는 월요일 09:00 KST에 확인. 10월 31일 이후 종료.",
            "익절/줄일 때: 1.45달러 종가 이탈이면 반등 논리를 폐기합니다. 1.70달러 안착 전에는 이 알림으로 익절 목표를 2.40달러로 올리지 마세요.",
            "하지 말 것: 1.60달러 언급만 보고 추격, 신용/미수, 주말 공백을 정시 체크로 착각.",
        ],
        pri,
    )


def main() -> int:
    dry = len(sys.argv) > 1 and sys.argv[1].lower() in {"test", "dry", "dry-run"}
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    state = load_state()
    if now.year != int(cfg.get("event_year", 2026)):
        print("year-guard skip")
        return 0
    if today < cfg["window_start_kst"][:10] or today > cfg["expire_date"]:
        print("outside window; silent")
        return 0

    close, close_note, price_err = fetch_close(cfg)
    kind = verdict(close, close_note, cfg)
    title, body, pri = copy_for(kind, cfg, close, close_note)
    lines = [f"{now.strftime('%Y-%m-%d %H:%M KST')} | PORT | XRP box", *body]
    if price_err:
        lines.append("조회 메모: " + " / ".join(price_err[:4]))
    msg = "\n".join(lines)
    print(title)
    print(msg)
    print(f"verdict={kind} dry={dry} priority={pri}")

    same = state.get("verdict") == kind and state.get("baseline_sent")
    send = False
    if kind == "FETCH_FAIL":
        send = not state.get("fail_notified")
        if send and not dry:
            state["fail_notified"] = True
    elif kind == "DATA_MISSING":
        send = int(state.get("missing_notified") or 0) < 2
        if send and not dry:
            state["missing_notified"] = int(state.get("missing_notified") or 0) + 1
    else:
        send = not same
        if send and not dry:
            state["baseline_sent"] = True
            state["fail_notified"] = False

    if dry:
        print("dry-run; no ntfy")
    elif send:
        publish(
            resolve_topic("port", cfg.get("ntfy_topic")),
            title,
            msg,
            pri,
            ["chart_with_upwards_trend"],
            resolve_server(cfg.get("ntfy_server")),
        )
        print("ntfy sent")
    else:
        print("no ntfy; duplicate suppressed")

    state.update(
        {
            "verdict": kind,
            "close": close,
            "close_note": close_note,
            "updated_at": now.isoformat(),
        }
    )
    if not dry:
        save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
