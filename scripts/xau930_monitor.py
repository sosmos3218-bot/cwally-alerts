#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "xau930.json"
STATE_PATH = ROOT / "state" / "xau930.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def yahoo_last(ticker: str) -> float | None:
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + ticker + "?interval=1d&range=10d"
    try:
        payload = json.loads(fetch(url))
        result = payload["chart"]["result"][0]
        meta = result.get("meta") or {}
        price = meta.get("regularMarketPrice") or meta.get("previousClose")
        closes = ((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        daily = [c for c in closes if c is not None]
        if daily:
            return float(daily[-1])
        return float(price) if price is not None else None
    except Exception as exc:
        print(f"yahoo fail {ticker}: {exc}", file=sys.stderr)
        return None


def last_fred(url: str, min_date: str):
    try:
        raw = fetch(url)
    except Exception as exc:
        print(f"fred fail: {exc}", file=sys.stderr)
        return None
    rows = []
    for line in raw.strip().splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 2:
            continue
        date, val = parts[0].strip(), parts[1].strip()
        if val in {".", ""}:
            continue
        try:
            rows.append((date, float(val)))
        except ValueError:
            continue
    rows = [r for r in rows if r[0] >= min_date]
    return rows[-1] if rows else None


def tnx_to_yield(tnx: float | None) -> float | None:
    if tnx is None:
        return None
    return tnx / 10.0 if tnx > 20 else tnx


def page_out(url: str, needles: list[str]) -> bool | None:
    try:
        text = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", fetch(url))))
    except Exception as exc:
        print(f"page fail {url}: {exc}", file=sys.stderr)
        return None
    low = text.lower()
    if any(n.lower() in low for n in ("embargoed until", "scheduled release", "error 404")):
        if not any(n.lower() in low for n in needles):
            return False
    return any(n.lower() in low for n in needles)


def load_state() -> dict:
    state = {
        "pce_sent": False,
        "nfp_sent": False,
        "fail_pce": False,
        "fail_nfp": False,
        "updated_at": None,
        "last": {},
    }
    if STATE_PATH.exists():
        try:
            state.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return state


def save_state(state: dict, now: datetime) -> None:
    state["updated_at"] = now.isoformat()
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fmt_px(v) -> str:
    return f"{v:,.1f}" if isinstance(v, (int, float)) else "조회 실패"


def fmt_y(v) -> str:
    return f"{v:.2f}%" if isinstance(v, (int, float)) else "조회 실패"


def pick_event(cfg: dict, now: datetime, state: dict, mode: str) -> str | None:
    pce_at = datetime.fromisoformat(cfg["pce_at_kst"]).replace(tzinfo=KST)
    nfp_at = datetime.fromisoformat(cfg["nfp_at_kst"]).replace(tzinfo=KST)
    if now >= nfp_at and (mode in {"force", "dry"} or not state.get("nfp_sent")):
        return "nfp"
    if now >= pce_at and (mode in {"force", "dry"} or not state.get("pce_sent")):
        return "pce"
    return None


def judge(close: float | None, levels: dict) -> tuple[str, str, str]:
    if close is None:
        return "데이터 부족 | 판정 보류", "유지 보류", "금 종가를 못 읽어 시나리오를 바꾸지 않음."
    low, mid, high = levels["low"], levels["mid"], levels["high"]
    if close < low:
        return (
            f"금 {fmt_px(close)} | 4,110 하향 유지",
            "유지",
            "4,110 종가 이탈. 하방 연장 유지. 다음 구간 4,075~4,000.",
        )
    if close > high:
        return (
            f"금 {fmt_px(close)} | 4,300 회복 숏폐기",
            "폐기",
            "4,300 종가 회복. 단기 숏 우위 폐기. 추격 숏 접을 것.",
        )
    if close > mid:
        return (
            f"금 {fmt_px(close)} | 4,230 회복 붕괴무효",
            "유지(수정)",
            "4,230 종가 회복. 9/28 붕괴는 무효화에 가깝다. 4,230~4,300 박스.",
        )
    return (
        f"금 {fmt_px(close)} | 4,110~4,230 박스 유지",
        "유지",
        "4,110 위, 4,230 아래. 박스 유지. 추격 매매 금지.",
    )


def emit(cfg: dict, title: str, lines: list[str], priority: int) -> None:
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    publish(topic, title, "\n".join(lines), priority, ["chart_with_upwards_trend"], server)


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    state = load_state()
    start = datetime.fromisoformat(cfg["window_start_kst"]).replace(tzinfo=KST)
    expire = datetime.fromisoformat(cfg["expire_date"] + "T23:59:59").replace(tzinfo=KST)
    in_year = now.year == int(cfg.get("event_year", 2026))
    in_window = start <= now <= expire and in_year
    print(f"{now.isoformat()} mode={mode} window={in_window}")
    if mode not in {"force", "dry"} and not in_window:
        print("outside 2026 window; silent")
        save_state(state, now)
        return 0

    event = pick_event(cfg, now, state, mode)
    if event is None:
        print("condition not met; silent")
        save_state(state, now)
        return 0

    tickers = cfg["tickers"]
    xau = yahoo_last(tickers["xau"])
    silver = yahoo_last(tickers["silver"])
    dxy = yahoo_last(tickers["dxy"])
    ust2 = tnx_to_yield(yahoo_last(tickers["ust2"]))
    ust10 = tnx_to_yield(yahoo_last(tickers["ust10"]))
    fred2 = last_fred(cfg["sources"]["fred_dgs2"], "2026-09-01")
    fred10 = last_fred(cfg["sources"]["fred_dgs10"], "2026-09-01")
    fred_real = last_fred(cfg["sources"]["fred_dfii10"], "2026-09-01")
    if ust2 is None and fred2:
        ust2 = fred2[1]
    if ust10 is None and fred10:
        ust10 = fred10[1]
    real10 = fred_real[1] if fred_real else None
    tape_ok = xau is not None and dxy is not None

    if event == "pce":
        printed = page_out(cfg["sources"]["bea"], ["Personal Income and Outlays, August 2026", "PCE price index"])
    else:
        printed = page_out(cfg["sources"]["bls"], ["Employment Situation", "nonfarm", "unemployment rate"])

    last = {
        "event": event,
        "xau": xau,
        "silver": silver,
        "dxy": dxy,
        "ust2": ust2,
        "ust10": ust10,
        "real10": real10,
        "real_date": fred_real[0] if fred_real else None,
        "printed": printed,
        "oct_hike_pct": None,
    }
    state["last"] = last

    fail_key = "fail_pce" if event == "pce" else "fail_nfp"
    sent_key = "pce_sent" if event == "pce" else "nfp_sent"
    label = "PCE" if event == "pce" else "NFP"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")

    if mode != "dry" and not tape_ok:
        if not state.get(fail_key):
            emit(
                cfg,
                f"[PORT] {label} 후 금 조회 실패 | 재시도",
                [
                    f"{stamp} | XAU930 | {label} 후 금 단기 시나리오",
                    f"이게 뭐 알림: {cfg['explain']}",
                    "지금 상태: 금 또는 달러 시세 조회 실패. 시나리오를 유지/폐기로 처리하지 않음.",
                    "지금 할 일: 다음 09:00 또는 15:20까지 대기. 지금은 포지션 바꾸지 마세요.",
                    "나중에: 시세가 열리면 이벤트당 한 번만 판정합니다. 만료 2026-10-06.",
                    "익절/줄일 때: 실패 알림만으로 매매하지 마세요.",
                    "하지 말 것: 조회 실패를 급락/반등으로 읽기.",
                ],
                4,
            )
            state[fail_key] = True
        save_state(state, now)
        return 0

    action_title, verdict, do_now = judge(xau, cfg["levels"])
    if printed is False:
        action_title = f"[PORT] {label} 원문 없음 | 시세만 판정"
        later = f"{label} 원문이 아직 없습니다. 숫자는 시세만. 같은 이벤트는 다시 안 보냅니다."
    elif printed is None:
        action_title = f"[PORT] {label} 원문 조회실패 | 시세만 판정"
        later = f"{label} 페이지가 안 열림. 발표 내용을 추정하지 않음."
    else:
        action_title = f"[PORT] {action_title}"
        later = "10월 인상 확률은 이 잡에서 자동 수집하지 않음. CME를 따로 보세요. 만료 2026-10-06."

    real_txt = fmt_y(real10)
    if last["real_date"]:
        real_txt += f"({last['real_date']})"
    status = (
        f"금 {fmt_px(xau)} / 은 {fmt_px(silver)} / 달러 {fmt_px(dxy)} / "
        f"2년 {fmt_y(ust2)} / 10년 {fmt_y(ust10)} / 실질10년 {real_txt} / "
        f"10월 인상확률 확인 불가. {label} 원문 {'있음' if printed else '없음 또는 실패'}."
    )
    lines = [
        f"{stamp} | XAU930 | {label} 후 7지표 + 금 3선",
        f"이게 뭐 알림: {cfg['explain']}",
        f"지금 상태: {status} 판정={verdict}. {do_now}",
        f"지금 할 일: {do_now} 종가 기준만 사용.",
        f"나중에: {later}",
        "익절/줄일 때: 4,230 종가 회복이면 추격 숏 축소. 4,110 종가 이탈이면 반등 매수 보류.",
        "하지 말 것: 장중 터치만으로 시나리오 바꾸기. PCE26 물가 알림과 혼동하기. 추격, 신용, 미수.",
    ]
    if mode == "dry":
        print(action_title)
        print("\n".join(lines))
        print("dry-run; no ntfy")
        save_state(state, now)
        return 0

    emit(cfg, action_title, lines, 4)
    state[sent_key] = True
    save_state(state, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
