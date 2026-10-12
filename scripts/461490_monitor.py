#!/usr/bin/env python3
"""RISE 글로벌자산배분액티브(461490) 주기 점검 알림. 기존 afternoon 슬롯에 연결."""
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
CFG_PATH = ROOT / "configs" / "461490.json"
STATE_PATH = ROOT / "state" / "461490.json"
UA = {"User-Agent": "cwally-alerts 461490 monitor"}


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


def save_state(state: dict, now: datetime) -> None:
    state["updated_at"] = now.isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def yahoo_price(ticker: str):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(ticker)
        + "?interval=1d&range=5d"
    )
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode())
        meta = payload["chart"]["result"][0].get("meta", {})
        price = meta.get("regularMarketPrice") or meta.get("previousClose")
        return float(price) if price is not None else None
    except Exception as exc:
        print(f"price fetch failed: {exc}", file=sys.stderr)
        return None


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    dry = mode in {"test", "dry", "dry-run"}
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = load_json(
        STATE_PATH,
        {"last_price": None, "last_sent": None, "fail_sent_date": None, "updated_at": None},
    )
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    year_ok = now.year == int(cfg.get("year_guard", 2026))
    valid = today <= cfg.get("valid_until", "2026-12-31")
    print(f"{stamp} mode={mode} year_ok={year_ok} valid={valid}")
    if mode not in {"force", "dry", "test", "dry-run"} and (not year_ok or not valid):
        print("outside window; silent")
        return 0

    price = yahoo_price(cfg["ticker"])
    print(f"price={price}")
    if price is None:
        if state.get("fail_sent_date") != today and not dry:
            title = "[461490] 시세 조회 실패 | 직접 확인"
            lines = [
                f"{stamp} | 461490 | {cfg['display']}",
                f"이게 뭐 알림: {cfg['explain']}",
                "지금 결과/상태: 가격 조회 실패. 실패를 정상 처리하지 않습니다.",
                "지금 할 일: 증권앱에서 461490 종가를 직접 확인하세요.",
                "나중에: 다음 평일 15:20 KST에 재시도합니다.",
                "익절/줄일 때: 이번 알림은 매매 신호가 아닙니다.",
                "하지 말 것: 실패를 매수/매도 근거로 쓰기.",
            ]
            msg = "\n".join(lines)
            print(title)
            print(msg)
            publish(
                resolve_topic("port", cfg.get("ntfy_topic")),
                title,
                msg,
                4,
                ["warning"],
                resolve_server(cfg.get("ntfy_server")),
            )
            state["fail_sent_date"] = today
            save_state(state, now)
        return 0

    near_low = price <= cfg["levels"]["near_low"]
    last = state.get("last_price")
    changed = last is None or abs(price - last) / last > 0.015  # 1.5% change
    if (near_low or changed) and (mode in {"force", "dry", "test"} or state.get("last_sent") != today):
        title = f"[461490] {price:,.0f}원 | 장기 코어 점검"
        status = "52주 저점 근접" if near_low else "가격 변동 감지"
        lines = [
            f"{stamp} | 461490 | {cfg['display']}",
            f"이게 뭐 알림: {cfg['explain']}",
            f"지금 결과/상태: {status}. 현재가 {price:,.0f}원. 최근 수익률 부진, 장기 분할매수 검토.",
            "지금 할 일: 전액 매수 피하고 6~10주 분할 매수 검토. 기존 금 비중과 중복 확인.",
            "나중에: 금리 피크아웃 확인 시 매력 상승. 미국 CPI/FOMC 이후 재평가.",
            "익절/줄일 때: 최소 수익률 확인 시 추가 증액. 최대 낙폭 8% 수준 유지.",
            "하지 말 것: 단기 추격, 전액 매수, 실패를 신호로 보기.",
        ]
        msg = "\n".join(lines)
        print(title)
        print(msg)
        if not dry:
            publish(
                resolve_topic("port", cfg.get("ntfy_topic")),
                title,
                msg,
                4,
                ["chart_with_upwards_trend"],
                resolve_server(cfg.get("ntfy_server")),
            )
            state["last_sent"] = today
        state["last_price"] = price
        if not dry:
            save_state(state, now)
    else:
        print("no trigger; silent")
        state["last_price"] = price
        if not dry:
            save_state(state, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
