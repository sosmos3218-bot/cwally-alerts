#!/usr/bin/env python3
"""377990 judgment-flip watch. Silent unless a new price change condition hits or a fetch fails."""
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
CFG_PATH = ROOT / "configs" / "377990.json"
STATE_PATH = ROOT / "state" / "377990.json"
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
    if not cleaned or cleaned == "-":
        return 0.0
    return float(cleaned)


def save(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fail_once(state: dict, now: datetime, cfg: dict, title: str, note: str, dry: bool) -> None:
    day = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    body = "\n".join(
        [
            f"{stamp} | 377990",
            "이게 뭐 알림: 377990 판단 변경 조건 점검 중 시세 조회가 실패했습니다.",
            f"지금 상태: {note}",
            "지금 할 일: 실패를 조건 충족으로 보지 마세요. TIGER Fn신재생에너지 종가를 직접 확인하세요.",
            "나중에: 다음 평일 15:40 KST 슬롯에서 다시 봅니다.",
            "익절/줄일 때: 이 실패로 사거나 팔지 마세요.",
            "하지 말 것: 조회 실패를 39,000 재접근이나 박스 이탈로 해석. 추격매수. 신용/미수.",
        ]
    )
    print(body)
    if not dry and state.get("last_fail_day") != day:
        publish(resolve_topic("port", cfg.get("ntfy_topic")), title, body, 4, ["warning"], resolve_server(cfg.get("ntfy_server")))
        state["last_fail_day"] = day
    state["updated_at"] = stamp
    save(state)


def completed(rows: list[dict], basic: dict) -> list[dict]:
    status = str((basic or {}).get("marketStatus") or "").upper()
    today = datetime.now(KST).strftime("%Y-%m-%d")
    out = []
    for row in rows or []:
        day = str(row.get("localTradedAt") or "")[:10]
        if status == "CLOSE" or day < today:
            out.append(row)
    return out


def main() -> int:
    dry = "dry" in [a.lower() for a in sys.argv[1:]]
    cfg = load_json(CFG_PATH, {})
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    state = load_json(
        STATE_PATH,
        {"updated_at": None, "fired": {}, "last_sent": None, "last_fail_day": None},
    )
    fired = dict(state.get("fired") or {})
    fired.setdefault("chase", False)
    fired.setdefault("box_fail", False)

    if now.year != int(cfg.get("year_guard") or 2026):
        print("377990 year-guard silent")
        return 0
    expire = datetime.strptime(cfg.get("expire_date") or "2026-12-31", "%Y-%m-%d").date()
    if now.date() > expire:
        print("377990 expired silent")
        return 0
    if now.weekday() >= 5:
        print("377990 weekend silent")
        return 0

    sources = cfg.get("sources") or {}
    try:
        basic = json.loads(fetch(sources["basic"]))
        rows = json.loads(fetch(sources["price"]))
    except Exception as exc:
        fail_once(state, now, cfg, "[377990] 시세 조회 실패 | 판정 보류", f"377990 시세 조회 실패: {exc}", dry)
        return 0

    bars = completed(rows, basic)
    if not bars:
        print("377990 no completed bar; silent")
        state["updated_at"] = stamp
        save(state)
        return 0

    levels = cfg.get("levels") or {}
    baseline = cfg.get("baseline") or {}
    base_day = str(baseline.get("day") or "2026-10-02")
    chase_close = float(levels.get("chase_close") or 39000)
    chase_reset = float(levels.get("chase_reset_below") or 37000)
    box_fail = float(levels.get("box_fail_close") or 22600)

    latest = bars[0]
    day = str(latest.get("localTradedAt") or "")[:10]
    close = num(latest.get("closePrice"))
    if day <= base_day:
        print("377990 baseline day or earlier; silent", day, close)
        state["updated_at"] = stamp
        state["last_bar"] = {"day": day, "close": close, "note": "baseline"}
        save(state)
        return 0

    if close < chase_reset:
        fired["chase"] = False
    if close >= box_fail:
        fired["box_fail"] = False

    new_hits = []
    if close >= chase_close and not fired.get("chase"):
        fired["chase"] = True
        new_hits.append("chase")
    if close < box_fail and not fired.get("box_fail"):
        fired["box_fail"] = True
        new_hits.append("box_fail")

    state["fired"] = fired
    state["updated_at"] = stamp
    state["last_bar"] = {"day": day, "close": close}
    print(stamp, "day", day, "close", close, "new", new_hits)

    if not new_hits:
        save(state)
        print("377990 silent; no new change-condition")
        return 0

    if "chase" in new_hits and "box_fail" in new_hits:
        title = "[377990] 조건 충돌 | 매수 보류"
        action = "가격 조건이 겹쳤습니다. 사지 마세요."
    elif "chase" in new_hits:
        title = "[377990] 39,000 재접근 | 추격 금지"
        action = "하향 조건입니다. 이익 개정은 미확인이므로 추격하지 마세요."
    else:
        title = "[377990] 박스 하단 이탈 | 상향 조건 닫힘"
        action = "최근 박스 하단을 깨뜨렸습니다. 상향 조건의 가격 유지는 닫히고 기다리세요."

    body = "\n".join(
        [
            f"{stamp} | 377990",
            "이게 뭐 알림: TIGER Fn신재생에너지 판단 변경 조건입니다. 매수 신호가 아닙니다.",
            f"지금 상태: {day} 완료 종가 {close:,.0f}. 추격 기준 {chase_close:,.0f}, 박스 하단 {box_fail:,.0f}. 수주잔고와 영업이익 추정 동시 상향은 미확인.",
            f"지금 할 일: {action}",
            "나중에: 37,000 미만으로 내려오면 39,000 알림은 다시 열립니다. 22,600 이상 복귀를 박스 알림은 다시 열립니다. 2026-12-31 이후 침묵.",
            "익절/줄일 때: 이 알림은 익절 주문이 아닙니다. 손절은 진입 전에 본인이 고정하세요.",
            "하지 말 것: 이익 개정이 확인된 것처럼 해석. 코어 편입. 신용/미수. 조건 미충족을 매수 신호로 해석.",
        ]
    )
    print(body)
    if not dry:
        publish(resolve_topic("port", cfg.get("ntfy_topic")), title, body, 4, ["warning"], resolve_server(cfg.get("ntfy_server")))
        state["last_sent"] = {"at": stamp, "hits": new_hits}
    save(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
