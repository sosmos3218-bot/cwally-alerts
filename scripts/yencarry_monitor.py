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
            if "30" in name and ("\u5e74" in name or "Y" in name.upper() or name.endswith("30")):
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
        return "\ubb38\uc7a5 \uc5c6\uc74c"
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).lower()
    if "raise the target range" in text or "decided to raise" in text:
        return "\uae08\ub9ac \uc778\uc0c1"
    if "lower the target range" in text or "decided to lower" in text:
        return "\uae08\ub9ac \uc778\ud558"
    if "maintain the target range" in text or "decided to maintain" in text:
        return "\uae08\ub9ac \ub3d9\uacb0"
    return "\ubb38\uc7a5 \ud655\uc778 \uc2e4\ud328"


def boj_stance(day: datetime) -> str:
    ymd = day.strftime("%y%m%d")
    pages = [
        "https://www.boj.or.jp/mopo/mpmdeci/mpr_2026/index.htm",
        "https://www.boj.or.jp/en/mopo/mpmdeci/mpr_2026/index.htm",
        f"https://www.boj.or.jp/mopo/mpmdeci/mpr_2026/k{ymd}a.pdf",
    ]
    blob = ""
    for url in pages:
        try:
            blob += " " + fetch(url)
        except Exception as exc:
            print(f"boj fetch fail {url}: {exc}", file=sys.stderr)
    if not blob.strip():
        return "\uacf8\ud45c\ubb38 \uc544\uc9c1 \uc5c6\uc74c"
    text = re.sub(r"<[^>]+>", " ", blob)
    text = re.sub(r"\s+", " ", text)
    day_ko = f"{day.month}\u6708{day.day}\u65e5"
    if day_ko not in text and ymd not in text and day.strftime("%Y%m%d") not in text:
        return "\uacf8\ud45c\ubb38 \uc544\uc9c1 \uc5c6\uc74c"
    if "\u91d1\u878d\u5e02\u5834\u8abf\u7bc0\u65b9\u91dd\u306e\u5909\u66f4" in text or "raise" in text.lower() and "policy rate" in text.lower():
        return "\uae08\ub9ac \ubcc0\uacbd(\uc778\uc0c1 \uac00\ub2a5)"
    if "\u5f53\u9762\u306e\u91d1\u878d\u653f\u7b56\u904b\u55b6" in text or "maintain" in text.lower():
        return "\uae08\ub9ac \ub3d9\uacb0 \uac00\ub2a5"
    return "\uacf8\ud45c\ubb38 \ud655\uc778. \uc778\uc0c1\u00b7\ub3d9\uacb0\uc740 \ubcf8\ubb38 \ud655\uc778"


def fmt(v: float | None, nd: int = 1) -> str:
    return f"{v:,.{nd}f}" if v is not None else "\uc5c6\uc74c"


def breaks(cfg: dict, usdjpy: float | None, usdkrw: float | None, j30: float | None) -> list[str]:
    lv = cfg["levels"]
    out: list[str] = []
    if usdjpy is not None and usdjpy <= lv["usdjpy_low"]:
        out.append(f"\ub2ec\ub7ec/\uc5d4 {fmt(usdjpy)} \u2192 152 \uc774\ud0c8")
    if usdjpy is not None and usdjpy >= lv["usdjpy_high"]:
        out.append(f"\ub2ec\ub7ec/\uc5d4 {fmt(usdjpy)} \u2192 160 \ub3cc\ud30c")
    if usdkrw is not None and usdkrw <= lv["usdkrw_low"]:
        out.append(f"\uc6d0\ub2ec\ub7ec {fmt(usdkrw, 0)}\uc6d0 \u2192 1,340 \uc774\ud0c8")
    if usdkrw is not None and usdkrw >= lv["usdkrw_high"]:
        out.append(f"\uc6d0\ub2ec\ub7ec {fmt(usdkrw, 0)}\uc6d0 \u2192 1,400 \ub3cc\ud30c")
    if j30 is not None and j30 >= lv["jgb30_danger"]:
        out.append(f"\uc77c\ubcf8 30\ub144 {fmt(j30, 2)}% \u2192 4.5 \ub3cc\ud30c")
    elif j30 is not None and j30 >= lv["jgb30_warn"]:
        out.append(f"\uc77c\ubcf8 30\ub144 {fmt(j30, 2)}% \u2192 4.3 \ub3cc\ud30c")
    return out


def action_line(keys: list[str]) -> str:
    if not keys:
        return "\ub808\ubca8 \uc548 \uae68\uc84c\uc73c\ub2c8 \uae30\uc874 \ubd81 \uc720\uc9c0. \ucd94\uaca9\u00b7\ub808\ubc84\ub9ac\uc9c0 \uae08\uc9c0."
    bits = []
    blob = " ".join(keys)
    if "160 \ub3cc\ud30c" in blob:
        bits.append("\uc5d4 \ub871 \uc811\uae30")
    if "152 \uc774\ud0c8" in blob:
        bits.append("\uc5d4\uc740 \ud655\uc778\ub9cc, \uc6d0 \uc57d\uc138(\ub9ac\uc2a4\ud06c\uc624\ud504) \ub300\ube44")
    if "1,340 \uc774\ud0c8" in blob:
        bits.append("\ub2ec\ub7ec \ubcf4\uc720 \uc904\uc774\uae30")
    if "1,400 \ub3cc\ud30c" in blob:
        bits.append("\ub2ec\ub7ec \uc77c\ubd80 \uc0ac\uae30")
    if "4.5 \ub3cc\ud30c" in blob:
        bits.append("\ubbf8\uad6d \ucd08\uc7a5\uae30 \ucd94\uac00 \ucd95\uc18c")
    elif "4.3 \ub3cc\ud30c" in blob:
        bits.append("\ubbf8\uad6d \ucd08\uc7a5\uae30 \ube44\uc911 \uc904\uc774\uae30")
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
        "boj_sent_date": None,
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
    base = f"\ub2ec\ub7ec/\uc5d4 {fmt(usdjpy)} | \uc6d0\ub2ec\ub7ec {fmt(usdkrw, 0)}\uc6d0 | \uc77c\ubcf8 30\ub144 {fmt(j30, 2)}%"
    if keys:
        return base + " | \uae68\uc9c4 \uac83: " + ", ".join(keys)
    return base + " | \uae68\uc9c4 \ub808\ubca8 \uc5c6\uc74c"


def emit_fed(cfg: dict, now: datetime, usdjpy, usdkrw, j30, keys: list[str]) -> None:
    us_day = (now - timedelta(hours=13)).date()
    stance = fed_stance(datetime(us_day.year, us_day.month, us_day.day, tzinfo=timezone.utc))
    act = action_line(keys)
    title = f"[YENCARRY] {act}"
    lines = [
        f"{now.strftime('%Y-%m-%d %H:%M KST')} | YENCARRY | \uc5f0\uc900 \ud6c4\uc18d",
        "\uc774\uac8c \ubb50 \uc54c\ub9bc: \uc5f0\uc900 \ubc1c\ud45c \ub4a4, \uc815\ud574\ub454 \ub808\ubca8\uc774 \uae68\uc84c\ub294\uc9c0 \ubcf4\uace0 \ubd81\uc744 \ubc14\uafc0\uc9c0 \uacb0\uc815\ud558\ub294 \uc54c\ub9bc\uc785\ub2c8\ub2e4.",
        f"\uc9c0\uae08 \uc0c1\ud0dc: \uc5f0\uc900 {stance}. {status_line(usdjpy, usdkrw, j30, keys)}",
        f"\uc9c0\uae08 \ud560 \uc77c: {act}",
        "\ub098\uc911\uc5d0: \uc77c\uc740 9/18\uc740 \ub808\ubca8\uacfc \uc0c1\uad00\uc5c6\uc774 \ubc18\ub4dc\uc2dc \ubcf4\ub0c5\ub2c8\ub2e4. \uc8fc\uac04 \uc810\uac80\uc740 \uc6d4\uc694\uc77c 09:00.",
        "\uc775\uc808/\uc904\uc77c \ub54c: \uc5d4 \ub871\uc740 160 \uc7ac\ub3cc\ud30c\uba74 \uc811\uae30. \uc6d0\ub2ec\ub7ec 1,340 \uc544\ub798\uba74 \ub2ec\ub7ec \ubcf4\uc720 \ucd95\uc18c.",
        "\ud558\uc9c0 \ub9d0 \uac83: \uc5f0\uc900 \ud55c \ubc29\uc73c\ub85c \uc6d0\u00b7\ucf54\uc2a4\ud53c\u00b7\ubbf8\uad6d\ucc44\uac00 \uac19\uc774 \ubb34\ub108\uc9c4\ub2e4\uace0 \ubab0\ubed5.",
    ]
    send(cfg, title, lines, 5 if keys else 4)


def emit_boj(cfg: dict, now: datetime, usdjpy, usdkrw, j30, keys: list[str], stance: str) -> None:
    act = action_line(keys)
    title = f"[YENCARRY] \uc77c\uc740 {stance} | {act}"
    lines = [
        f"{now.strftime('%Y-%m-%d %H:%M KST')} | YENCARRY | \uc77c\uc740 \uacb0\uacfc",
        "\uc774\uac8c \ubb50 \uc54c\ub9bc: \uc77c\ubcf8\uc740\ud589 \uacb0\uc815. \ub808\ubca8\uc774 \uc548 \uae68\uc838\ub3c4 \uacb0\uacfc\ub294 \ubc18\ub4dc\uc2dc \ubcf4\ub0c5\ub2c8\ub2e4.",
        f"\uc9c0\uae08 \uc0c1\ud0dc: \uc77c\uc740 {stance}. {status_line(usdjpy, usdkrw, j30, keys)}",
        f"\uc9c0\uae08 \ud560 \uc77c: {act}",
        "\ub098\uc911\uc5d0: \ub2e4\uc74c \uc77c\uc740 10/30, 12/18\ub3c4 \uac19\uc774 \ubb34\uc870\uac74 \ubcf4\ub0c5\ub2c8\ub2e4.",
        "\uc775\uc808/\uc904\uc77c \ub54c: 160 \uc7ac\ub3cc\ud30c\uba74 \uc5d4 \ub871 \uc811\uae30. 1,340 \uc544\ub798\uba74 \ub2ec\ub7ec \uc904\uc774\uae30.",
        "\ud558\uc9c0 \ub9d0 \uac83: \uacf8\ud45c\ubb38 \uc5c6\uc774 \ucd94\uaca9. \uc5d4 \uac15\uc138=\uc6d0 \uac15\uc138\ub85c \uac19\uc774 \ubca0\ud305.",
    ]
    send(cfg, title, lines, 5)


def emit_event(cfg: dict, now: datetime, usdjpy, usdkrw, j30, keys: list[str], why: str) -> None:
    act = action_line(keys)
    title = f"[YENCARRY] {act}"
    lines = [
        f"{now.strftime('%Y-%m-%d %H:%M KST')} | YENCARRY | \uc774\ubca4\ud2b8 {why}",
        "\uc774\uac8c \ubb50 \uc54c\ub9bc: \uc815\ud574\ub454 \ub808\ubca8\uc774 \uae68\uc84c\uc2b5\ub2c8\ub2e4.",
        f"\uc9c0\uae08 \uc0c1\ud0dc: {status_line(usdjpy, usdkrw, j30, keys)}",
        f"\uc9c0\uae08 \ud560 \uc77c: {act}",
        "\ub098\uc911\uc5d0: \uac19\uc740 \ub808\ubca8\uc5d0 \ubd99\uc7a1\ud788\uba74 \ub2e4\uc2dc \uc548 \ubcf4\ub0c5\ub2c8\ub2e4. \uc8fc\uac04 \uc810\uac80\uc740 \uc6d4\uc694\uc77c 09:00.",
        "\uc775\uc808/\uc904\uc77c \ub54c: 160 \uc7ac\ub3cc\ud30c\uba74 \uc5d4 \ub871 \uc811\uae30. 1,340 \uc544\ub798\uba74 \ub2ec\ub7ec \uc904\uc774\uae30.",
        "\ud558\uc9c0 \ub9d0 \uac83: \uae68\uc9c4 \ub808\ubca8 \uc5c6\uc774 \ucd94\uaca9. \uc5d4 \uac15\uc138=\uc6d0 \uac15\uc138\ub85c \uac19\uc774 \ubca0\ud305.",
    ]
    send(cfg, title, lines, 5)


def emit_weekly(cfg: dict, now: datetime, usdjpy, usdkrw, j30, keys: list[str]) -> None:
    act = action_line(keys)
    title = f"[YENCARRY] \uc8fc\uac04 \uc810\uac80 | {act}"
    lines = [
        f"{now.strftime('%Y-%m-%d %H:%M KST')} | YENCARRY | \uc8fc\uac04",
        "\uc774\uac8c \ubb50 \uc54c\ub9bc: \uc5d4\uce90\ub9ac \ub808\ubca8\uc744 \ud55c \uc8fc\uc5d0 \ud55c \ubc88 \ubcf4\ub294 \uc810\uac80\uc785\ub2c8\ub2e4.",
        f"\uc9c0\uae08 \uc0c1\ud0dc: {status_line(usdjpy, usdkrw, j30, keys)}",
        f"\uc9c0\uae08 \ud560 \uc77c: {act}",
        "\ub098\uc911\uc5d0: 152/160, 1,340/1,400, \uc77c\ubcf8 30\ub144 4.3 \uae68\uc9c0\uba74 \uadf8\ub54c \ub2e4\uc2dc \ubcf4\ub0c5\ub2c8\ub2e4.",
        "\uc775\uc808/\uc904\uc77c \ub54c: 160 \uc7ac\ub3cc\ud30c\uba74 \uc5d4 \ub871 \uc811\uae30. 1,340 \uc544\ub798\uba74 \ub2ec\ub7ec \uc904\uc774\uae30.",
        "\ud558\uc9c0 \ub9d0 \uac83: \ub9e4\uc8fc \uc810\uac80 \ub300\uc2e0 \ub9e4\uc77c \ucd94\uaca9.",
    ]
    send(cfg, title, lines, 3 if not keys else 4)


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
    boj_after = 12
    raw_after = str(cfg.get("boj_send_after_kst") or "12:00")
    try:
        boj_after = int(raw_after.split(":")[0])
    except ValueError:
        boj_after = 12

    if mode == "fed" or (mode == "auto" and is_fed_window and now.hour >= 3):
        if state.get("fed_sent_date") == us_cal:
            print("fed already sent")
        else:
            emit_fed(cfg, now, usdjpy, usdkrw, j30, keys)
            state["fed_sent_date"] = us_cal
            state["last_event_key"] = event_key(keys, "fed-" + us_cal)
            save_state(state, now, q)
            return 0

    if is_boj and cfg.get("boj_force", True):
        if now.hour < boj_after and mode != "fed":
            print("boj morning; wait for statement")
        elif state.get("boj_sent_date") == today:
            print("boj already sent")
        else:
            stance = boj_stance(now)
            emit_boj(cfg, now, usdjpy, usdkrw, j30, keys, stance)
            state["boj_sent_date"] = today
            save_state(state, now, q)
            return 0

    if keys:
        ek = event_key(keys)
        if state.get("last_event_key") == ek:
            print("same event; silent")
            save_state(state, now, q)
            return 0
        emit_event(cfg, now, usdjpy, usdkrw, j30, keys, "\ub808\ubca8 \ud0c8\ucd9c")
        state["last_event_key"] = ek
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
