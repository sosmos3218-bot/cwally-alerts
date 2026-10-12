#!/usr/bin/env python3
"""Dear U 376300 current investment attractiveness based on price levels and analysis.

One-shot window. Fetches latest close via stock_trend. Silent outside window or after notified.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from naver_api import stock_trend  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "dearattr.json"
STATE_PATH = ROOT / "state" / "dearattr.json"


def read_state() -> dict:
    if not STATE_PATH.exists():
        return {"notified": False}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict, now: datetime) -> None:
    state["updated_at"] = now.isoformat(timespec="seconds")
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def judge(price: float, cfg: dict) -> tuple[str, str]:
    rules = cfg["rules"]
    base = cfg["baseline"]
    if price <= rules["very_attract"]:
        return "매력 매우 높음", f"가격 {price:,.0f}원이 52주 저점 {base['price_low52']:,}원 근접. 애널 목표 {base['analyst_avg_target']:,}원 대비 대폭 저평가"
    if price <= rules["attract"]:
        return "매력 높음", f"가격 {price:,.0f}원. 자사주 150억 소각, 외국 성장 기대. 목표 대비 상승 여력 큼"
    if price <= rules["neutral"]:
        return "중립", f"가격 {price:,.0f}원. 저평가 구간은 지났으나 추가 확인 필요"
    return "관망", f"가격 {price:,.0f}원이 매력 구간 위. 추격 주의"


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode not in {"auto", "dry", "force"}:
        raise SystemExit(f"unknown mode: {mode}")
    dry = mode == "dry"
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = read_state()
    now = datetime.now(KST)
    day = now.strftime("%Y-%m-%d")
    if now.year != int(cfg["year_guard"]) or not cfg["window_start"] <= day <= cfg["window_end"]:
        print("outside 2026 one-shot window; silent")
        return 0
    if state.get("notified") and mode != "force":
        print("already notified; silent")
        return 0
    try:
        rows = stock_trend(cfg["ticker"], 5)
    except Exception as exc:
        print(f"fetch fail: {exc}")
        return 0
    if not rows or rows[0].get("close") is None:
        print("no price data")
        return 0
    price = rows[0]["close"]
    verdict, reason = judge(price, cfg)
    base = cfg["baseline"]
    title = f"[DEAR] 디어유 {verdict} | {price:,.0f}원"
    lines = [
        "이게 뭐 알림: 디어유 376300 현시점 투자매력. 가격 위치, 자사주 소각, 애널리스트 목표 대비.",
        f"지금 결과/상태: 종가 {price:,.0f}원 (52주 저 {base['price_low52']:,}, 애널 평균 목표 {base['analyst_avg_target']:,}). 자사주 {base['buyback_eok']}억 신탁 진행 중. 2Q 구독 {base['q2_subs_man']}만, OPM {base['q2_opm']}%. 한 줄: {verdict} ({reason}). 조회 {now:%Y-%m-%d %H:%M} KST.",
        "지금 할 일: 관망 유지. 추가 매수는 구독자 안정·해외 가속 확인 후.",
        "나중에: 3Q 잠정(대략 11월) 구독자·OPM 확인. 자사주 소각 진행 감시.",
        "익절/줄일 때: 가격 1.7만 이하 또는 구독 190만 미만·OPM 35% 미만 지속 시 비중 재검토.",
        "하지 말 것: 목표주가 괴리만으로 즉시 매수하지 마세요. 투자 권유 아님.",
    ]
    if dry:
        print(f"[dry] topic={resolve_topic('port')}", title, *lines, sep="\n")
        return 0
    publish(
        resolve_topic("port", cfg.get("ntfy_topic")),
        title,
        "\n".join(lines),
        3,
        ["chart_with_upwards_trend"],
        resolve_server(cfg.get("ntfy_server")),
    )
    state["notified"] = True
    state["last_price"] = price
    state["last_verdict"] = verdict
    save_state(state, now)
    print(f"sent: {title}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
