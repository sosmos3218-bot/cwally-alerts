#!/usr/bin/env python3
"""278240 judgment-flip watch. Silent unless a new change condition hits or a fetch fails."""
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
CFG_PATH = ROOT / "configs" / "k278240.json"
STATE_PATH = ROOT / "state" / "k278240.json"
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
            f"{stamp} | 278240 | 사용자 Cwally",
            "이게 뭐 알림: 278240 판단 변경 조건 점검 중 조회가 실패했습니다.",
            f"지금 상태: {note}",
            "지금 할 일: 실패를 조건 충족으로 보지 마세요. 229200 종가와 278240 기관 순매수를 직접 확인하세요.",
            "나중에: 다음 평일 15:40 KST 슬롯에서 다시 봅니다.",
            "익절/줄일 때: 이 실패로 사거나 팔지 마세요.",
            "하지 말 것: 조회 실패를 추세 확인으로 해석. 278240 추격매수. 신용/미수.",
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
    fired.setdefault("confirm", False)
    fired.setdefault("fail", False)

    if now.year != int(cfg.get("year_guard") or 2026):
        print("k278240 year-guard silent")
        return 0
    expire = datetime.strptime(cfg.get("expire_date") or "2026-11-14", "%Y-%m-%d").date()
    if now.date() > expire:
        print("k278240 expired silent")
        return 0
    if now.weekday() >= 5:
        print("k278240 weekend silent")
        return 0

    sources = cfg.get("sources") or {}
    try:
        basic = json.loads(fetch(sources["proxy_basic"]))
        rows = json.loads(fetch(sources["proxy_price"]))
    except Exception as exc:
        fail_once(state, now, cfg, "[278240] 시세 조회 실패 | 판정 보류", f"229200 시세 조회 실패: {exc}", dry)
        return 0

    bars = completed(rows, basic)
    if len(bars) < 6:
        print("k278240 completed bars < 6; silent")
        state["updated_at"] = stamp
        save(state)
        return 0

    proxy = cfg.get("proxy") or {}
    baseline = cfg.get("baseline") or {}
    base_day = str(baseline.get("day") or "2026-10-01")
    hold_close = float(proxy.get("hold_close") or 15315)
    fail_close = float(proxy.get("fail_close") or 14315)
    room_max = float(proxy.get("room_max_close") or 18466)
    ret_min = float(proxy.get("ret_5d_min") or 0.08)
    hold_days = int(proxy.get("hold_days") or 2)

    latest = bars[0]
    day = str(latest.get("localTradedAt") or "")[:10]
    close = num(latest.get("closePrice"))
    prev5 = num(bars[5].get("closePrice"))
    ret5 = (close / prev5 - 1) if prev5 else 0.0
    after = [b for b in bars if str(b.get("localTradedAt") or "")[:10] > base_day]
    held = after[:hold_days]
    hold_ok = len(held) >= hold_days and all(num(b.get("closePrice")) >= hold_close for b in held)
    room_ok = close <= room_max
    trend_ok = ret5 >= ret_min and hold_ok and room_ok

    if close < hold_close or ret5 < 0:
        fired["confirm"] = False
    if close > fail_close:
        fired["fail"] = False

    flow_note = "수급 미확인"
    flow_ok = False
    try:
        trend = json.loads(fetch(sources["etf_trend"]))
        if not trend:
            flow_note = "기관 수급 행 없음"
        else:
            row = trend[0]
            biz = str(row.get("bizdate") or "")
            flow_day = f"{biz[:4]}-{biz[4:6]}-{biz[6:8]}" if len(biz) == 8 else ""
            organ = num(row.get("organPureBuyQuant"))
            flow_note = f"{flow_day} 기관 {organ:+,.0f}주"
            if flow_day != day:
                flow_note += f" (종가일 {day}와 불일치, 충족 아님)"
            elif flow_day <= base_day:
                flow_note += " (기준일 이전, 신규 충족 아님)"
            else:
                flow_ok = organ >= float((cfg.get("flow") or {}).get("organ_min") or 1)
    except Exception as exc:
        fail_once(state, now, cfg, "[278240] 수급 조회 실패 | 판정 보류", f"278240 기관 수급 조회 실패: {exc}", dry)
        return 0

    new_hits = []
    if trend_ok and flow_ok and not fired.get("confirm"):
        fired["confirm"] = True
        new_hits.append("confirm")
    if close <= fail_close and day > base_day and not fired.get("fail"):
        fired["fail"] = True
        new_hits.append("fail")

    state["fired"] = fired
    state["updated_at"] = stamp
    state["last_bar"] = {"day": day, "close": close, "ret5": round(ret5, 4), "flow": flow_note, "hold_ok": hold_ok}
    print(stamp, "day", day, "close", close, "ret5", round(ret5, 4), "hold", hold_ok, "flow", flow_note, "new", new_hits)

    if not new_hits:
        save(state)
        print("k278240 silent; no new change-condition")
        return 0

    if "confirm" in new_hits and "fail" in new_hits:
        title = "[278240] 조건 충돌 | 매수 보류"
        action = "신호가 겹쳤습니다. 사지 마세요."
    elif "confirm" in new_hits:
        title = "[278240] 추세+기관 확인 | 233740만 단기 검토"
        action = "판단 변경 여지만 열렸습니다. 산다면 278240이 아니라 233740을 수일만 보고, 진입 전 손절을 고정하세요."
    else:
        title = "[278240] 모멘텀 반대근거 깨짐 | 대기 유지"
        action = "10월 1일 저가 이탈입니다. 단기 추세 추종 여지를 닫고 기존 낮은 매력 판단을 유지하세요."

    body = "\n".join(
        [
            f"{stamp} | 278240 | 사용자 Cwally",
            "이게 뭐 알림: 278240 분석의 반대 근거와 판단 변경 조건입니다. 매수 신호가 아닙니다.",
            f"지금 상태: 229200 {day} 종가 {close:,.0f}. 5일 {ret5:+.1%}. 수급 {flow_note}. 실적 서프라이즈는 미확인.",
            f"지금 할 일: {action}",
            "나중에: 종가가 15,315 아래거나 5일 수익률이 음수면 확인 알림은 리셋됩니다. 2026-11-14 이후 침묵.",
            "익절/줄일 때: 이 알림은 익절 주문이 아닙니다. 손절은 진입 전에 본인이 고정하세요.",
            "하지 말 것: 278240 분할매수 후 방치. 코어 편입. 신용/미수. 실적 서프라이즈가 확인된 것처럼 해석.",
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
