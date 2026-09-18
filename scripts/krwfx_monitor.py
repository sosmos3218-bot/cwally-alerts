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
CFG_PATH = ROOT / "configs" / "krwfx.json"
STATE_PATH = ROOT / "state" / "krwfx.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}
WINDOW_KO = {"1w": "1주", "2w": "2주", "1m": "1개월", "3m": "3개월"}


def fetch_closes(ticker: str) -> list[float]:
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + ticker
        + "?interval=1d&range=6mo"
    )
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode())
    result = payload["chart"]["result"][0]
    quotes = result["indicators"]["quote"][0]["close"]
    closes = [float(x) for x in quotes if x is not None]
    if not closes:
        raise RuntimeError(f"no closes for {ticker}")
    return closes


def pct_change(closes: list[float], sessions: int) -> float | None:
    if len(closes) < sessions + 1:
        sessions = max(1, len(closes) - 1)
    if sessions <= 0:
        return None
    old, new = closes[-(sessions + 1)], closes[-1]
    if old == 0:
        return None
    return (new / old - 1.0) * 100.0


def signed(pct: float | None) -> str:
    if pct is None:
        return "없음"
    tone = "강세" if pct > 0 else ("약세" if pct < 0 else "보합")
    return f"{pct:+.2f}% ({tone})"


def load_state() -> dict:
    state = {"weekly_sent_week": None, "last_snapshot": {}, "updated_at": None}
    if STATE_PATH.exists():
        try:
            state.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return state


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def decide(w1_krw: float, w1_jpy: float, gap: float, warn: float) -> tuple[str, str, str]:
    if gap <= -warn:
        title = "1주 원화 고유약세 | 원엔 동조화 금지"
        do_now = (
            f"같은 달러 강세인데 원화가 엔보다 {abs(gap):.2f}%p 더 밀렸습니다. "
            "원/엔만 보고 원화 약세로 단정하지 마세요. 엔 롱과 원 롱을 같이 넣지 마세요."
        )
        later = "1주 갭이 0.8%p 안으로 줄어들면 고유약세가 끝난 겁니다. 그전엔 원/엔 추격 금지."
    elif gap >= warn:
        title = "1주 원화 상대강세 | 엔약세=원약세 금지"
        do_now = (
            f"원화가 엔보다 {gap:.2f}%p 덜 밀리거나 더 강합니다. "
            "엔 약세를 이유로 원화를 같이 팔지 마세요."
        )
        later = "3개월 원화 강세가 깨지는지, 1주 갭이 반대로 뒤집히는지만 보세요."
    else:
        title = "1주 동조 | 기존 환율 북 유지"
        do_now = "달러 공통요인 위입니다. 원/엔을 독립 시그널로 쓰지 말고 기존 북 유지."
        later = "1주 갭이 0.8%p를 넘으면 그때 원화 고유요인으로 재분해하세요."
    if w1_krw < -2 and w1_jpy > -1:
        later = "단기 원화만 급하게 밀린 상태입니다. 3개월 강세가 남아 있으면 달러 추격매수는 절반만."
    return title, do_now, later


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    state = load_state()
    week = now.strftime("%G-W%V")
    warn = float(cfg.get("gap_warn_pct") or 0.8)

    try:
        usdkrw = fetch_closes(cfg["tickers"]["usdkrw"])
        usdjpy = fetch_closes(cfg["tickers"]["usdjpy"])
    except Exception as exc:
        print(f"krwfx fetch fail: {exc}", file=sys.stderr)
        state["updated_at"] = now.isoformat()
        save_state(state)
        return 0

    rows = {}
    for key, sessions in cfg["windows"].items():
        d_krw = pct_change(usdkrw, int(sessions))
        d_jpy = pct_change(usdjpy, int(sessions))
        krw_vs_usd = None if d_krw is None else -d_krw
        jpy_vs_usd = None if d_jpy is None else -d_jpy
        krw_vs_jpy = None if None in (krw_vs_usd, jpy_vs_usd) else krw_vs_usd - jpy_vs_usd
        rows[key] = {
            "krw_vs_usd": krw_vs_usd,
            "jpy_vs_usd": jpy_vs_usd,
            "krw_vs_jpy": krw_vs_jpy,
        }

    w1 = rows["1w"]
    gap = w1["krw_vs_jpy"] if w1["krw_vs_jpy"] is not None else 0.0
    title_bit, do_now, later = decide(
        w1["krw_vs_usd"] or 0.0,
        w1["jpy_vs_usd"] or 0.0,
        gap,
        warn,
    )

    jpykrw = usdkrw[-1] / usdjpy[-1] if usdjpy[-1] else None
    state["last_snapshot"] = {
        "usdkrw": usdkrw[-1],
        "usdjpy": usdjpy[-1],
        "jpykrw": jpykrw,
        "w1_krw_vs_usd": w1["krw_vs_usd"],
        "w1_jpy_vs_usd": w1["jpy_vs_usd"],
        "w1_krw_vs_jpy": w1["krw_vs_jpy"],
    }
    state["updated_at"] = now.isoformat()
    print(now, mode, state["last_snapshot"])

    should_send = mode in {"weekly", "force"} or (mode == "auto" and now.weekday() == 0)
    if mode == "event":
        print("krwfx event mode; weekly only")
        save_state(state)
        return 0
    if not should_send:
        print("krwfx silent; wait monday 09:00 KST")
        save_state(state)
        return 0
    if mode != "force" and state.get("weekly_sent_week") == week:
        print("krwfx weekly already sent")
        save_state(state)
        return 0

    table = ["구간 | 원화 대 달러 | 엔화 대 달러 | 원화 대 엔화"]
    for key in ("1w", "2w", "1m", "3m"):
        r = rows[key]
        table.append(
            f"{WINDOW_KO[key]} | {signed(r['krw_vs_usd'])} | {signed(r['jpy_vs_usd'])} | {signed(r['krw_vs_jpy'])}"
        )

    lines = [
        f"{now.strftime('%Y-%m-%d %H:%M KST')} | KRWFX | 주간 교차분해",
        "이게 뭐 알림: 100엔/원을 독립 시장으로 보지 않고 (원/달러)÷(엔/달러)로 분해한 주간 점검입니다.",
        (
            f"지금 상태: 원/달러 {usdkrw[-1]:,.1f} | 달러/엔 {usdjpy[-1]:,.2f} | "
            f"1엔당 {jpykrw:.3f}원(100엔 {jpykrw * 100:.1f}원). "
            f"1주 갭(원화-엔화, 달러대비) {gap:+.2f}%p."
        ),
        *table,
        f"지금 할 일: {do_now}",
        f"나중에: {later} 월요일 09:00에만 다시 옵니다.",
        "익절/줄일 때: 3개월 원화 강세가 꺾이면 달러 보유를 다시 늘리세요. 1주 되돌림만으로 환율 북을 갈아엎지 마세요.",
        "하지 말 것: 100엔/원만 보고 원화 약세/강세 단정. 엔 강세=원 강세로 같이 베팅. 추격 환전.",
    ]
    title = f"[KRWFX] {title_bit}"
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    pri = 4 if abs(gap) >= warn else 3
    publish(topic, title, "\n".join(lines), pri, ["kr", "chart_with_upwards_trend"], server)
    state["weekly_sent_week"] = week
    save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
