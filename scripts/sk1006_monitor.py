#!/usr/bin/env python3
"""SK Inc 034730 block-anchor check. One shot after 2026-10-06 close."""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish  # noqa: E402

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "sk1006.json"
STATE_PATH = ROOT / "state" / "sk1006.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return dict(fallback)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        fallback.update(data)
        return fallback
    except json.JSONDecodeError:
        return fallback


def http_json(url: str):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def num(value):
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "").replace("+", ""))
    except ValueError:
        return None


def save(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def pick_row(rows: list, target: str) -> dict | None:
    exact = None
    latest = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        day = str(row.get("bizdate") or row.get("localDate") or row.get("date") or "")[:10].replace("-", "")
        if latest is None or day > str(latest.get("_day") or ""):
            latest = dict(row)
            latest["_day"] = day
        if day == target:
            exact = dict(row)
            exact["_day"] = day
    return exact or latest


def fetch(code: str, target: str) -> tuple[dict, str | None]:
    errors = []
    basic = {}
    try:
        raw = http_json(f"https://m.stock.naver.com/api/stock/{code}/basic")
        basic = raw if isinstance(raw, dict) else {}
    except Exception as exc:
        errors.append(f"basic {exc}")
    trend = None
    try:
        raw = http_json(f"https://m.stock.naver.com/api/stock/{code}/trend?pageSize=10")
        rows = raw if isinstance(raw, list) else raw.get("trend") or raw.get("items") or raw.get("result") or []
        trend = pick_row(rows, target)
    except Exception as exc:
        errors.append(f"trend {exc}")
    close = num((trend or {}).get("closePrice")) or num(basic.get("closePrice"))
    volume = num((trend or {}).get("accumulatedTradingVolume")) or num(basic.get("accumulatedTradingVolume"))
    foreign = num((trend or {}).get("foreignerPureBuyQuant"))
    organ = num((trend or {}).get("organPureBuyQuant"))
    day = (trend or {}).get("_day") or ""
    parsed = {
        "day": day,
        "close": close,
        "volume": volume,
        "foreign": foreign,
        "organ": organ,
    }
    if close is None or volume is None or foreign is None or organ is None or day != target:
        return parsed, "; ".join(errors) or f"10/6 수급 누락 day={day or '없음'}"
    return parsed, None


def judge(parsed: dict, cfg: dict) -> tuple[str, str, str]:
    anchor = cfg["anchor"]
    close = parsed["close"]
    volume = parsed["volume"]
    foreign = parsed["foreign"]
    organ = parsed["organ"]
    block = anchor["block_price"]
    base = anchor["close"]
    cap = anchor["volume_cap"]
    reasons = []
    if close < block:
        reasons.append(f"종가 {close:,.0f}원이 블록가 {block:,.0f}원 미만")
    if foreign < 0 and organ < 0:
        reasons.append("외국인·기관 동반 순매도")
    if volume >= cap and close < base:
        reasons.append(f"거래량 {volume:,.0f}주가 20만 이상이고 종가가 10/2 {base:,.0f}원 미만")
    held = close >= block
    if not reasons and held:
        return "BUY", "매수", "블록 할인 앵커 유지. 수급이 동반 매도가 아님."
    return "PASS", "패스", " / ".join(reasons) or "앵커 유지 조건 미충족"


def main() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    state = load_json(STATE_PATH, {"final_notified": False, "fail_notified": False, "last_action": None, "parsed": {}})
    if now.year != int(cfg.get("event_year") or 2026) or today > cfg.get("expire_date", "2026-10-06"):
        print("year guard or expired; silent")
        return 0
    if today != cfg.get("expire_date"):
        print(f"not event date {today}; silent")
        return 0
    if state.get("final_notified"):
        print("already notified; silent")
        return 0

    parsed, err = fetch(cfg["code"], "20261006")
    state["parsed"] = parsed
    state["updated_at"] = stamp
    if err:
        if not state.get("fail_notified"):
            publish(
                "cwally-port",
                "[FLOW] SK 조회 실패 | 재실행",
                "\n".join([
                    f"{stamp} | SK주 034730 블록 앵커",
                    "이게 뭐 알림: 10월 6일 종가·거래량·외국인·기관 조회가 실패했습니다. 패스가 아닙니다.",
                    f"지금 상태: {err}",
                    "지금 할 일: 월요장 원문을 네이버에서 확인. 재실행은 sk1006 수동 실행.",
                    "나중에: 조회가 되면 매수/패스를 다시 보냅니다.",
                    "익절/줄일 때: 해당 없음.",
                    "하지 말 것: 조회 실패를 패스로 보지 말 것.",
                ]),
                4,
            )
            state["fail_notified"] = True
        state["last_action"] = "FAIL"
        save(state)
        print(f"fetch failed: {err}", file=sys.stderr)
        return 1

    action, label, why = judge(parsed, cfg)
    held = "유지" if parsed["close"] >= cfg["anchor"]["block_price"] else "이탈"
    title = f"[FLOW] SK 앵커 {held} | {label}"
    msg = "\n".join([
        f"{stamp} | SK주 034730 블록 앵커",
        "이게 뭐 알림: 10월 6일 장 마감 후 블록 할인가 55만9387원이 유지되는지 보는 1회 알림입니다.",
        (
            f"지금 상태: 종가 {parsed['close']:,.0f}원 | 거래량 {parsed['volume']:,.0f}주 | "
            f"외국인 {parsed['foreign']:,.0f}주 | 기관 {parsed['organ']:,.0f}주 | {why}"
        ),
        f"지금 할 일: {label}. 신규 매수는 앵커 유지와 수급 확인 후만.",
        "나중에: 11월 2일 시간외 대량매매 체결가와 SI 실명을 본다.",
        "익절/줄일 때: 종가가 블록가 아래로 물리면 추가 매수 금지.",
        "하지 말 것: 신용·미수. 공시 소화 전 추격. 조회 실패를 패스로 보기.",
    ])
    print(msg)
    publish("cwally-port", title, msg, 4 if action == "PASS" else 3)
    state["final_notified"] = True
    state["last_action"] = action
    save(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
