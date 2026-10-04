#!/usr/bin/env python3
"""ETH 2538 change conditions. No new cron. Attach to weekday 15:40 KST flow runner."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "ethhl.json"
STATE_PATH = ROOT / "state" / "ethhl.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts", "Content-Type": "application/json"}


def fetch(url: str, payload: dict | None = None) -> str:
    import urllib.request

    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def fetch_json(url: str, payload: dict | None = None):
    return json.loads(fetch(url, payload))


def load_state() -> dict:
    state = {"verdict": None, "hits": [], "fail_notified": False, "updated_at": None}
    if STATE_PATH.exists():
        try:
            state.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return state


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def eth_position(cfg: dict, user: str) -> tuple[dict | None, str]:
    data = fetch_json(cfg["sources"]["hyperliquid"], {"type": "clearinghouseState", "user": user})
    for ap in data.get("assetPositions") or []:
        pos = ap.get("position") or {}
        if pos.get("coin") == "ETH":
            return pos, ""
    return None, "ETH 포지션 없음"


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
            return close, f"coinbase UTC 일봉 {day} 종가 {close:.2f}", errors
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
        return close, f"kraken UTC 일봉 {day} 종가 {close:.2f}", errors
    except Exception as exc:
        errors.append(f"kraken 실패: {exc}")
    return None, "", errors


def num(pos: dict | None, key: str) -> float | None:
    if not pos or pos.get(key) in (None, ""):
        return None
    try:
        return float(pos[key])
    except (TypeError, ValueError):
        return None


def evaluate(cfg: dict) -> tuple[str, list[str], dict, list[str]]:
    errors = []
    snap = {}
    close, close_note, price_err = fetch_close(cfg)
    errors.extend(price_err)
    snap["close"] = close
    snap["close_note"] = close_note
    long_pos, long_note = None, ""
    short_pos, short_note = None, ""
    try:
        long_pos, long_note = eth_position(cfg, cfg["wallets"]["long"])
    except Exception as exc:
        errors.append(f"long 조회 실패: {exc}")
        long_note = "FETCH_FAIL"
    try:
        short_pos, short_note = eth_position(cfg, cfg["wallets"]["short"])
    except Exception as exc:
        errors.append(f"short 조회 실패: {exc}")
        short_note = "FETCH_FAIL"
    snap["long_note"] = long_note
    snap["short_note"] = short_note
    snap["long_entry"] = num(long_pos, "entryPx")
    snap["long_liq"] = num(long_pos, "liquidationPx")
    snap["long_value"] = num(long_pos, "positionValue")
    snap["long_upnl"] = num(long_pos, "unrealizedPnl")
    snap["short_entry"] = num(short_pos, "entryPx")
    snap["short_liq"] = num(short_pos, "liquidationPx")
    snap["short_value"] = num(short_pos, "positionValue")
    snap["short_upnl"] = num(short_pos, "unrealizedPnl")
    if close is None and long_note == "FETCH_FAIL" and short_note == "FETCH_FAIL":
        return "FETCH_FAIL", [], snap, errors
    hits = []
    lv = cfg["levels"]
    if close is not None and close < float(lv["daily_close_break"]):
        hits.append("CLOSE_2500")
    mark = close
    liq = snap["long_liq"]
    if mark and liq and mark > 0 and (mark - liq) / mark <= float(lv["liq_gap_pct"]):
        hits.append("LIQ_3PCT")
    if long_pos is None and long_note != "FETCH_FAIL":
        hits.append("LONG_GONE")
    base = float(lv["short_baseline_usd"])
    half = float(lv["short_half_usd"])
    if short_note == "FETCH_FAIL":
        pass
    elif short_pos is None or (snap["short_value"] is not None and snap["short_value"] <= half):
        hits.append("SHORT_HALF")
    kind = "NONE" if not hits else "+".join(hits)
    snap["short_baseline"] = base
    return kind, hits, snap, errors


def fmt_usd(v) -> str:
    if v is None:
        return "없음"
    return f"{v/1_000_000:.1f}억달러"


def fmt_px(v) -> str:
    if v is None:
        return "없음"
    return f"{v:.1f}달러"


def copy_for(kind: str, hits: list[str], cfg: dict, snap: dict) -> tuple[str, list[str], int]:
    explain = cfg["explain"]
    status = (
        f"ETH 종가 {fmt_px(snap.get('close'))} ({snap.get('close_note') or '종가 없음'}). "
        f"0x0392 롱 진입 {fmt_px(snap.get('long_entry'))}, 청산 {fmt_px(snap.get('long_liq'))}, "
        f"명목 {fmt_usd(snap.get('long_value'))}, 미실현 {fmt_usd(snap.get('long_upnl'))}. "
        f"2304 숏 진입 {fmt_px(snap.get('short_entry'))}, 명목 {fmt_usd(snap.get('short_value'))}, "
        f"기준 {fmt_usd(snap.get('short_baseline'))}, 절반 기준 1억3900만달러."
    )
    titles = {
        "FETCH_FAIL": "[FLOW] ETH 조회 실패 | 판정 보류",
        "NONE": "[FLOW] ETH 2538 해석 유지 | 추가매수 보류",
        "CLOSE_2500": "[FLOW] ETH 2500 이탈 | 2538 지지 해석 폐기",
        "LIQ_3PCT": "[FLOW] ETH 롱 청산 3% 안 | 추가매수 금지",
        "SHORT_HALF": "[FLOW] ETH 2304 숏 절반 | 반대 근거 약해",
        "LONG_GONE": "[FLOW] ETH 0x0392 롱 없음 | 2538 단서 소멸",
    }
    title = titles.get(kind) or "[FLOW] ETH 변경조건 복수 | 2538 지지 재검토"
    if kind not in titles and hits:
        title = "[FLOW] ETH 변경조건 복수 | 2538 지지 재검토"
    action = {
        "FETCH_FAIL": "시세와 포지션을 직접 확인하고 이 알림으로 매매하지 마세요.",
        "NONE": "변경 조건 없습니다. 2538달러를 지지로 보지 말고 추가매수하지 마세요.",
        "CLOSE_2500": "일봉 종가가 2500달러 아래입니다. 2538 지지 해석을 버리고 추가매수하지 마세요.",
        "LIQ_3PCT": "0x0392 롱 청산가가 종가 3% 안입니다. 강제청산 매도가 가까워지므로 추가매수하지 마세요.",
        "SHORT_HALF": "2304달러 대형 숏 명목이 기준의 절반 이하입니다. 반대 근거인 대형 숏이 줄었습니다. 이것만으로 추격하지 마세요.",
        "LONG_GONE": "2538 단서인 0x0392 롱이 없습니다. 해당 진입가를 지지로 쓰지 마세요.",
    }.get(kind, "변경 조건이 복수입니다. 2538달러 지지 해석을 다시 검토하고 추가매수하지 마세요.")
    pri = 5 if hits else 4 if kind == "FETCH_FAIL" else 3
    return (
        title,
        [
            f"이게 뭐 알림: {explain}",
            f"지금 결과: {kind} | {status}",
            f"지금 할 일: {action}",
            "나중에: 평일 15:40 KST에만 다시 봅니다. 같은 판정은 반복 알림하지 않습니다. 주말 종가는 월요일 15:40 KST에 확인. 2026-12-31 이후 종료.",
            "익절/줄일 때: 이 알림은 익절 신호가 아닙니다. 2500 이탈이나 청산 3% 안이면 누르지 말고, 숏이 절반으로 줄어도 추격 익절 목표를 올리지 마세요.",
            "하지 말 것: 2538달러를 시장 지지로 보고 추격, 신용/미수, 주말 공백을 정시 체크로 착각.",
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
    kind, hits, snap, errors = evaluate(cfg)
    title, body, pri = copy_for(kind, hits, cfg, snap)
    lines = [f"{now.strftime('%Y-%m-%d %H:%M KST')} | FLOW | ETH 2538 변경조건", *body]
    if errors:
        lines.append("조회 메모: " + " / ".join(errors[:4]))
    msg = "\n".join(lines)
    print(title)
    print(msg)
    print(f"verdict={kind} dry={dry} priority={pri}")
    same = state.get("verdict") == kind and state.get("verdict") not in (None,)
    send = False
    if kind == "FETCH_FAIL":
        send = not state.get("fail_notified")
        if send and not dry:
            state["fail_notified"] = True
    elif kind == "NONE":
        send = False
        if not dry:
            state["fail_notified"] = False
    else:
        send = not same
        if send and not dry:
            state["fail_notified"] = False
    if dry:
        print("dry-run; no ntfy")
    elif send:
        publish(
            resolve_topic("flow", cfg.get("ntfy_topic")),
            title,
            msg,
            pri,
            ["chart_with_downwards_trend"],
            resolve_server(cfg.get("ntfy_server")),
        )
        print("ntfy sent")
    else:
        print("no ntfy; condition not met or duplicate suppressed")
    state.update({"verdict": kind, "hits": hits, "snap": snap, "updated_at": now.isoformat()})
    if not dry:
        save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
