#!/usr/bin/env python3
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
CFG_PATH = ROOT / "configs" / "nov3.json"
STATE_PATH = ROOT / "state" / "nov3.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}

# 번호는 항상 뜻과 같이 보낸다. 4번은 저장소에 없고, "4개"는 감시선 개수다.
GLOSS = (
    "번호 뜻: 1번 선반영(중간선거 랠리가 이미 지수에 들어 있음). "
    "2번 10월 급락. "
    "3번 금리 막힘(10년물 5.30% 이상이 선거 후 자동 랠리를 막음). "
    "4개 선은 시나리오 4번이 아니라 감시선 4개(S&P 7,490, 코스피 6,560, SOX 11,130, 10년물 5.30%)다."
)

SCENARIO = {
    "spx": {
        "do_now": "1번 선반영(선거 랠리 이미 반영) 폐기. 미국 대형주는 2번 10월 급락 시작으로 본다.",
        "later": "다음 확인선은 7,350. 여기를 못 깨면 2번 10월 급락이 아니라 이탈만이다.",
        "cut": "7,490을 종가로 다시 회복하면 1번 선반영 재검토.",
        "dont": "이탈만으로 추격매수하거나 전량 청산하지 말 것.",
    },
    "kospi": {
        "do_now": "1번 선반영(선거 랠리 이미 반영) 폐기. 코스피는 2번 10월 급락 시작으로 본다.",
        "later": "다음 확인선은 6,440, 그다음 5,660. 6,560만 깨고 바로 반등하면 2번 10월 급락 미완.",
        "cut": "6,560을 종가로 회복하면 1번 선반영 재검토.",
        "dont": "코스피만 보고 미국 대형주 시나리오를 같이 바꾸지 말 것.",
    },
    "sox": {
        "do_now": "2번 10월 급락의 반도체 확인 구간. 9월 14일 저점(11,130) 이탈.",
        "later": "다음 확인선은 10,450(7월 29일 저점). 여기를 깨면 2번 10월 급락 확인.",
        "cut": "11,130을 종가로 회복하면 2번 10월 급락 확인 취소.",
        "dont": "반도체 이탈을 1번 선반영 폐기로 확대하지 말 것.",
    },
    "ust10": {
        "do_now": "3번 금리 막힘(10년물 5.30% 이상, 선거 후 자동 랠리 기대 축소) 가중치 상승.",
        "later": "5.20% 아래로 3거래일 못 내려오면 3번 금리 막힘 유지. 4.80% 이하면 3번 금리 막힘 폐기.",
        "cut": "금리가 높아도 S&P가 7,820을 넘으면 3번 금리 막힘은 틀린다.",
        "dont": "금리만 보고 지수를 먼저 팔지 말 것. 지수 이탈과 같이 볼 것.",
    },
}


def yahoo_last(ticker: str) -> float | None:
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + ticker
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
        print(f"fetch fail {ticker}: {exc}", file=sys.stderr)
        return None


def tnx_to_yield(tnx: float | None) -> float | None:
    if tnx is None:
        return None
    return tnx / 10.0 if tnx > 20 else tnx


def load_state() -> dict:
    state = {
        "updated_at": None,
        "fired": {"spx": False, "kospi": False, "sox": False, "ust10": False},
        "last": {"spx": None, "kospi": None, "sox": None, "ust10": None},
        "last_sent": None,
    }
    if STATE_PATH.exists():
        try:
            raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            state.update(raw)
            fired = state.get("fired") or {}
            for key in ("spx", "kospi", "sox", "ust10"):
                fired.setdefault(key, False)
            state["fired"] = fired
        except json.JSONDecodeError:
            pass
    return state


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def breached(op: str, value: float | None, level: float) -> bool:
    if value is None:
        return False
    return value <= level if op == "<=" else value >= level


def recovered(op: str, value: float | None, level: float, buffer_pct: float) -> bool:
    if value is None:
        return False
    if op == "<=":
        return value > level * (1 + buffer_pct / 100.0)
    return value < level * (1 - buffer_pct / 100.0)


def fmt(key: str, value: float | None) -> str:
    if value is None:
        return "조회 실패"
    if key == "ust10":
        return f"{value:.2f}%"
    return f"{value:,.2f}"


def main() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    state = load_state()

    if now.year != int(cfg.get("year_guard") or 2026):
        print("nov3 year-guard silent")
        return 0
    expire = datetime.strptime(cfg["expire_date"], "%Y-%m-%d").date()
    if now.date() > expire:
        print("nov3 expired silent")
        return 0

    tickers = cfg["tickers"]
    last = {
        "spx": yahoo_last(tickers["spx"]),
        "kospi": yahoo_last(tickers["kospi"]),
        "sox": yahoo_last(tickers["sox"]),
        "ust10": tnx_to_yield(yahoo_last(tickers["tnx"])),
    }
    lines = cfg["lines"]
    buffer_pct = float(cfg.get("rearm_buffer_pct") or 0.3)
    fired = dict(state.get("fired") or {})
    new_hits: list[str] = []

    for key, spec in lines.items():
        level = float(spec["level"])
        op = spec["op"]
        value = last[key]
        if recovered(op, value, level, buffer_pct):
            fired[key] = False
        if breached(op, value, level) and not fired.get(key):
            fired[key] = True
            new_hits.append(key)

    state["updated_at"] = stamp
    state["last"] = last
    state["fired"] = fired
    print(stamp, last, "new_hits", new_hits)

    if not new_hits:
        save_state(state)
        print("nov3 silent; no new line touch")
        return 0

    hit_labels = [lines[k]["label"] for k in new_hits]
    title = "[NOV3] SPY/KOSPI " + " / ".join(hit_labels) + " 터치 | 시나리오 변경"
    status_bits = [
        f"S&P {fmt('spx', last['spx'])} (선 7,490)",
        f"코스피 {fmt('kospi', last['kospi'])} (선 6,560)",
        f"SOX {fmt('sox', last['sox'])} (선 11,130)",
        f"10년물 {fmt('ust10', last['ust10'])} (선 5.30%)",
    ]
    do_now = " ".join(SCENARIO[k]["do_now"] for k in new_hits)
    later = " ".join(SCENARIO[k]["later"] for k in new_hits)
    cut = " ".join(SCENARIO[k]["cut"] for k in new_hits)
    dont = " ".join(SCENARIO[k]["dont"] for k in new_hits)
    body = "\n".join(
        [
            f"{stamp} | NOV3 | 중간선거 감시선",
            "이게 뭐 알림: 11월 3일 중간선거 감시선 4개 중 새로 터치한 선입니다. 매수 신호가 아닙니다.",
            GLOSS,
            "지금 상태: " + " | ".join(status_bits),
            f"지금 할 일: {do_now}",
            f"나중에: {later} 판정 마감은 11월 21일.",
            f"익절/줄일 때: {cut}",
            f"하지 말 것: {dont} 추격, 신용, 미수.",
        ]
    )
    print(body)
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    publish(topic, title, body, 4, ["warning", "chart_with_downwards_trend"], server)
    state["last_sent"] = {"at": stamp, "hits": new_hits}
    save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
