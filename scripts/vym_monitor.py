#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "vym.json"
STATE_PATH = ROOT / "state" / "vym.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


def fetch_text(url: str) -> str:
    import urllib.request

    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def load_state() -> dict:
    state = {"weekly_sent_week": None, "last_yield": None, "last_price": None, "updated_at": None}
    if STATE_PATH.exists():
        try:
            state.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return state


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def get_vym_data() -> dict:
    # Simple Yahoo Finance quote for VYM
    try:
        text = fetch_text("https://query1.finance.yahoo.com/v8/finance/chart/VYM?interval=1d&range=1d")
        data = json.loads(text)
        meta = data["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice")
        # Yield from trailing or approx
        # Better: use dividend history or fixed recent
        # For robustness, fetch dividend yield page or use known recent
        yield_pct = None
        try:
            ytext = fetch_text("https://finance.yahoo.com/quote/VYM")
            m = re.search(r'"trailingAnnualDividendYield"[^0-9]*([0-9.]+)', ytext)
            if m:
                yield_pct = float(m.group(1)) * 100
        except Exception:
            pass
        return {"price": price, "yield": yield_pct, "src": "yahoo"}
    except Exception as e:
        return {"price": None, "yield": None, "error": str(e), "src": "fail"}


def get_treasury() -> float | None:
    try:
        text = fetch_text("https://query1.finance.yahoo.com/v8/finance/chart/%5ETNX?interval=1d&range=1d")
        data = json.loads(text)
        return data["chart"]["result"][0]["meta"].get("regularMarketPrice")
    except Exception:
        return None


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    dry = mode in {"test", "dry", "dry-run"}
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    week = now.strftime("%G-W%V")
    state = load_state()

    if mode not in {"force", "test", "dry", "dry-run"}:
        if now.weekday() != int(cfg.get("weekday_kst", 0)):
            print("vym silent; wait monday 09:00 KST")
            return 0
        if state.get("weekly_sent_week") == week:
            print("vym weekly already sent")
            return 0

    vym = get_vym_data()
    tsy = get_treasury()
    price = vym.get("price")
    yld = vym.get("yield")
    thr = cfg.get("thresholds", {})

    judgment = "보통"
    if yld and yld >= thr.get("yield_strong", 3.0):
        judgment = "매력 높음"
    elif yld and yld >= thr.get("yield_attractive", 2.7):
        judgment = "매력 개선"
    elif yld and yld < 2.4:
        judgment = "매력 낮음"

    gap = None
    if yld and tsy:
        gap = tsy - yld

    title = f"[VYM] {judgment} | 수익률 {yld:.2f}%" if yld else "[VYM] 조회 실패 | 직접 확인"
    body = [
        f"이게 뭐 알림: {cfg['explain']}",
        f"지금 결과: 가격 ${price or '확인불가'} | 배당수익률 {yld or '확인불가'}% | 10Y 국채 {tsy or '확인불가'}% | 갭 {gap or '확인불가'}%p.",
        "지금 할 일: 성장주 편중 완화용 저비용 가치 분산이면 분할 진입 검토. 높은 현금흐름이 목적이면 SCHD나 국채 우선.",
        "나중에: 다음 월요일 09:00 KST에 다시 점검. 수익률 2.7% 이상 또는 가격 10% 이상 하락 시 매력 재평가.",
        "익절/줄일 때: 해당 없음. 장기 보유 목적.",
        "하지 말 것: 현재 수익률만 보고 적극 매수. 조회 실패를 무시.",
    ]
    if vym.get("error"):
        body.append(f"메모: 조회 오류 {vym['error']}")

    msg = "\n".join([f"{now.strftime('%Y-%m-%d %H:%M KST')} | VYM | 주간 매력 점검", *body])
    print(msg)
    print(f"dry={dry} judgment={judgment}")

    if dry:
        print("dry-run; no ntfy")
    else:
        publish(
            resolve_topic("port", cfg.get("ntfy_topic")),
            title,
            msg,
            3,
            ["chart_with_upwards_trend"],
            resolve_server(cfg.get("ntfy_server")),
        )
        state["weekly_sent_week"] = week
        state["last_yield"] = yld
        state["last_price"] = price

    state["updated_at"] = now.isoformat()
    if not dry:
        save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
