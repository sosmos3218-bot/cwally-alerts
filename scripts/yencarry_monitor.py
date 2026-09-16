#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "yencarry.json"
STATE_PATH = ROOT / "state" / "yencarry.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


def fetch(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def yahoo(ticker: str) -> float | None:
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + ticker + "?interval=1d&range=5d"
    try:
        payload = json.loads(fetch(url))
        meta = payload["chart"]["result"][0].get("meta", {})
        price = meta.get("regularMarketPrice") or meta.get("previousClose")
        return float(price) if price is not None else None
    except Exception as exc:
        print(f"yahoo fail {ticker}: {exc}", file=sys.stderr)
        return None


def jgb30() -> float | None:
    urls = [
        "https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv",
        "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/historical/jgbcme.csv",
    ]
    for url in urls:
        try:
            text = fetch(url)
        except Exception as exc:
            print(f"jgb fail {url}: {exc}", file=sys.stderr)
            continue
        rows = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if len(rows) < 2:
            continue
        header = [c.strip() for c in rows[0].split(",")]
        idx = None
        for i, name in enumerate(header):
            if "30" in name and ("年" in name or "Y" in name.upper() or name.endswith("30")):
                idx = i
        if idx is None and len(header) >= 12:
            idx = -2 if "40" in header[-1] else -1
        for line in reversed(rows[1:]):
            cols = [c.strip() for c in line.split(",")]
            if idx is None or idx >= len(cols):
                continue
            raw = cols[idx].replace("%", "")
            try:
                val = float(raw)
            except ValueError:
                continue
            if 0.1 < val < 10:
                return val
    return None


def fed_stance(day: datetime) -> str:
    ymd = day.strftime("%Y%m%d")
    url = f"https://www.federalreserve.gov/newsevents/pressreleases/monetary{ymd}a.htm"
    try:
        html = fetch(url)
    except Exception as exc:
        print(f"fed fetch fail: {exc}", file=sys.stderr)
        return "문장 없음"
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    low = text.lower()
    if "raise the target range" in low or "decided to raise" in low:
        return "금리 인상"
    if "lower the target range" in low or "decided to lower" in low:
        return "금리 인하"
    if "maintain the target range" in low or "decided to maintain" in low:
        return "금리 동결"
    return "문장 확인 실패"


def fmt(v: float | None, nd: int = 1) -> str:
    return f"{v:,.{nd}f}" if v is not None else "없음"


def breaks(cfg: dict, usdjpy: float | None, usdkrw: float | None, j30: float | None) -> list[str]:
    lv = cfg["levels"]
    out: list[str] = []
    if usdjpy is not None and usdjpy <= lv["usdjpy_low"]:
        out.append(f"달러/엔 {fmt(usdjpy)} → 152 부셨")
    if usdjpy is not None and usdjpy >= lv["usdjpy_high"]:
        out.append(f"달러/엔 {fmt(usdjpy)} → 160 돋파")
    if usdkrw is not None and usdkrw <= lv["usdkrw_low"]:
        out.append(f"원달러 {fmt(usdkrw, 0)}원 → 1,340 부셨")
    if usdkrw is not None and usdkrw >= lv["usdkrw_high"]:
        out.append(f"원달러 {fmt(usdkrw, 0)}원 → 1,400 돋파")
    if j30 is not None and j30 >= lv["jgb30_danger"]:
        out.append(f"일본 30년 {fmt(j30, 2)}% → 4.5 돋파")
    elif j30 is not None and j30 >= lv["jgb30_warn"]:
        out.append(f"일본 30년 {fmt(j30, 2)}% → 4.3 돋파")
    return out


def action_line(keys: list[str]) -> str:
    if not keys:
        return "레벨 안 깨졌으니 기존 북 유지. 추격·레버리지 금지."
    bits = []
    blob = " ".join(keys)
    if "160 돋파" in blob:
        bits.append("엔 롱 접기")
    if "152 부셨" in blob:
        bits.append("엔은 확인만, 원 약세(리스크오프) 대비")
    if "1,340 부셨" in blob:
        bits.append("달러 보유 줄이기")
    if "1,400 돋파" in blob:
        bits.append("달러 일부 사기")
    if "4.5 돋파" in blob:
        bits.append("미국 초장기 추가 축소")
    elif "4.3 돋파" in blob:
        bits.append("미국 초장기 비중 줄이기")
    return ". ".join(bits) + "."


def event_key(keys: list[str], extra: str = "") -> str:
    return "|".join(keys + ([extra] if extra else []))


def send(cfg: dict, title: str, lines: list[str], priority: int = 4) -> None:
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    publish(topic, title, "\n".join(lines), priority, ["jp", "chart_with_upwards_trend"], server)


def quotes() -> tuple[float | None, float | None, float | None]:
    return yahoo("USDJPY=X"), yahoo("USDKRW=X"), jgb30()


def load_state() -> dict:
    state = {
        "briefing_sent": False,
        "fed_sent_date": None,
        "weekly_sent_week": None,
        "last_event_key": None,
        "last_quotes": {},
        "updated_at": None,
    }
    if STATE_PATH.exists():
        try:
            state.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return state


def save_state(state: dict, now: datetime, quotes_d: dict) -> None:
    state["updated_at"] = now.isoformat()
    state["last_quotes"] = quotes_d
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def status_line(usdjpy: float | None, usdkrw: float | None, j30: float | None, keys: list[str]) -> str:
    base = f"달러/엔 {fmt(usdjpy)} | 원달러 {fmt(usdkrw, 0)}원 | 일본 30년 {fmt(j30, 2)}%"
    if keys:
        return base + " | 깨진 것: " + ", ".join(keys)
    return base + " | 깨진 레벨 없음"


def emit_fed(cfg: dict, now: datetime, usdjpy, usdkrw, j30, keys: list[str]) -> str:
    stance = fed_stance(now if now.hour >= 3 else now - timedelta(days=1))
    if now.hour < 3:
        stance = fed_stance(now - timedelta(days=1))
    # statement dated FOMC calendar day (US), which is previous KST date after midnight
    us_day = (now - timedelta(hours=13)).date()
    stance = fed_stance(datetime(us_day.year, us_day.month, us_day.day, tzinfo=timezone.utc))
    act = action_line(keys)
    title = f"[YENCARRY] {act}"
    lines = [
        f"{now.strftime('%Y-%m-%d %H:%M KST')} | YENCARRY | 연준 후속",
        "이게 뭐 알림: 연준 발표 뒤, 정해둔 레벨이 깨졌는지 보고 북을 바꿀지 결정하는 알림입니다.",
        f"지금 상태: 연준 {stance}. {status_line(usdjpy, usdkrw, j30, keys)}",
        f"지금 할 일: {act}",
        "나중에: 다음 이벤트는 일은 9/18, 연준 10/28. 주간 점검은 월요일 09:00.",
        "익절/줄일 때: 엔 롱은 160 재돌파면 접기. 원달러 1,340 아래면 달러 보유 축소.",
        "하지 말 것: 연준 한 방으로 원·코스피·미국채가 같이 무너진다고 몰뻕.",
    ]
    send(cfg, title, lines, 5 if keys else 4)
    return title


def emit_event(cfg: dict, now: datetime, usdjpy, usdkrw, j30, keys: list[str], why: str) -> str:
    act = action_line(keys)
    title = f"[YENCARRY] {act}"
    lines = [
        f"{now.strftime('%Y-%m-%d %H:%M KST')} | YENCARRY | 이벤트 {why}",
        "이게 뭐 알림: 정해둔 레벨이 깨졌거나 중앙은행 일정이라 북을 바꿈니다.",
        f"지금 상태: {status_line(usdjpy, usdkrw, j30, keys)}",
        f"지금 할 일: {act}",
        "나중에: 같은 레벨에 붙잡히면 다시 안 보냅니다. 주간 점검은 월요일 09:00.",
        "익절/줄일 때: 160 재돌파면 엔 롱 접기. 1,340 아래면 달러 줄이기.",
        "하지 말 것: 깨진 레벨 없이 추격. 엔 강세=원 강세로 같이 베팅.",
    ]
    send(cfg, title, lines, 5)
    return title


def emit_weekly(cfg: dict, now: datetime, usdjpy, usdkrw, j30, keys: list[str]) -> str:
    act = action_line(keys)
    title = f"[YENCARRY] 주간 점검 | {act}"
    lines = [
        f"{now.strftime('%Y-%m-%d %H:%M KST')} | YENCARRY | 주간",
        "이게 뭐 알림: 엔캐리 레벨을 한 주에 한 번 보는 점검입니다.",
        f"지금 상태: {status_line(usdjpy, usdkrw, j30, keys)}",
        f"지금 할 일: {act}",
        "나중에: 152/160, 1,340/1,400, 일본 30년 4.3 깨지면 그때 다시 보냅니다.",
        "익절/줄일 때: 160 재돌파면 엔 롱 접기. 1,340 아래면 달러 줄이기.",
        "하지 말 것: 매주 점검 대신 매일 추격. 월간으로 달리지 말 것.",
    ]
    send(cfg, title, lines, 3 if not keys else 4)
    return title


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    state = load_state()
    usdjpy, usdkrw, j30 = quotes()
    keys = breaks(cfg, usdjpy, usdkrw, j30)
    q = {"usdjpy": usdjpy, "usdkrw": usdkrw, "jgb30": j30}
    print(f"{now} | {mode} | {q} | {keys}")

    today = now.strftime("%Y-%m-%d")
    week = now.strftime("%G-W%V")
    fomc = set(cfg.get("fomc_dates") or [])
    boj = set(cfg.get("boj_dates") or [])
    us_cal = (now - timedelta(hours=13)).strftime("%Y-%m-%d")
    is_fed_window = us_cal in fomc or today in fomc
    is_boj = today in boj

    if mode == "fed" or (mode == "auto" and is_fed_window and now.hour >= 3):
        if state.get("fed_sent_date") == us_cal:
            print("fed already sent")
            save_state(state, now, q)
            return 0
        emit_fed(cfg, now, usdjpy, usdkrw, j30, keys)
        state["fed_sent_date"] = us_cal
        state["last_event_key"] = event_key(keys, "fed-" + us_cal)
        save_state(state, now, q)
        return 0

    if keys:
        ek = event_key(keys)
        if state.get("last_event_key") == ek and mode != "fed":
            print("same event; silent")
            save_state(state, now, q)
            return 0
        why = "레벨 탈출"
        if is_boj:
            why = "일은 회의일"
        emit_event(cfg, now, usdjpy, usdkrw, j30, keys, why)
        state["last_event_key"] = ek
        save_state(state, now, q)
        return 0

    if is_boj and state.get("last_event_key") != "boj-" + today:
        emit_event(cfg, now, usdjpy, usdkrw, j30, keys, "일은 회의일")
        state["last_event_key"] = "boj-" + today
        save_state(state, now, q)
        return 0

    if mode in {"weekly", "auto"} and now.weekday() == 0:
        if state.get("weekly_sent_week") == week:
            print("weekly already sent")
            save_state(state, now, q)
            return 0
        emit_weekly(cfg, now, usdjpy, usdkrw, j30, keys)
        state["weekly_sent_week"] = week
        save_state(state, now, q)
        return 0

    print("silent")
    save_state(state, now, q)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
