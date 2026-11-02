#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "btcoct.json"
STATE_PATH = ROOT / "state" / "btcoct.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


def fetch(url: str) -> str:
    import urllib.request

    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def fetch_json(url: str):
    return json.loads(fetch(url))


def load_state() -> dict:
    state = {
        "snapshot_sent": False,
        "fail_notified": False,
        "last_kind": None,
        "confirmed_sent": False,
        "invalidated_sent": False,
        "weekly_above_sent": False,
        "updated_at": None,
    }
    if STATE_PATH.exists():
        try:
            state.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return state


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def kraken_rows(url: str) -> list:
    data = fetch_json(url)
    if data.get("error"):
        raise RuntimeError(",".join(data["error"]))
    result = data.get("result") or {}
    for key, rows in result.items():
        if key == "last":
            continue
        return rows
    raise RuntimeError("kraken rows empty")


def closed_weeks(rows: list, now: datetime) -> list[tuple[datetime, float, float]]:
    out = []
    for row in rows:
        ts = datetime.fromtimestamp(int(row[0]), timezone.utc)
        if now - ts >= timedelta(days=7):
            out.append((ts, float(row[4]), float(row[3])))
    return out


def sma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def fetch_spot(url: str) -> float:
    data = fetch_json(url)
    return float(data["data"]["amount"])


def fetch_realized(url: str) -> tuple[float | None, str | None, list[str]]:
    errors: list[str] = []
    try:
        rows = fetch_json(url)
        if not rows:
            return None, None, ["실현가격 행 없음"]
        last = rows[-1]
        return float(last["realizedPrice"]), str(last.get("d") or ""), errors
    except Exception as exc:
        errors.append(f"실현가격 조회 실패: {exc}")
        return None, None, errors


def scan_daily(rows: list, cycle_low: float) -> dict:
    summer_lows = []
    oct_lows = []
    oct_breaks = []
    last_daily = None
    for row in rows:
        ts = datetime.fromtimestamp(int(row[0]), timezone.utc)
        low = float(row[3])
        close = float(row[4])
        last_daily = (ts, low, close)
        if ts.year == 2026 and ts.month in {6, 7, 8}:
            summer_lows.append((ts, low))
        if ts.year == 2026 and ts.month == 10:
            oct_lows.append((ts, low))
            if low < cycle_low:
                oct_breaks.append((ts, low))
    summer_print = min(summer_lows, key=lambda x: x[1]) if summer_lows else None
    oct_print = min(oct_lows, key=lambda x: x[1]) if oct_lows else None
    return {
        "summer_print": summer_print,
        "oct_print": oct_print,
        "broke": oct_breaks[-1] if oct_breaks else None,
        "last_daily": last_daily,
    }


def pct(a: float, b: float) -> str:
    if b == 0:
        return "n/a"
    return f"{(a / b - 1) * 100:+.1f}%"


def kind_for(now: datetime, cfg: dict, spot, sma50, week_close, scan) -> str:
    if spot is None or sma50 is None or week_close is None:
        return "FETCH_FAIL"
    if scan.get("broke"):
        return "CONFIRMED"
    if now.strftime("%Y-%m-%d") >= cfg["invalidate_date"] and not scan.get("broke"):
        return "INVALIDATED"
    if week_close > sma50:
        return "WEEKLY_ABOVE"
    return "ALIVE"


def copy_for(kind: str, cfg: dict, now: datetime, spot, sma50, sma200, week_close, week_date, realized, realized_d, scan) -> tuple[str, list[str], int]:
    prefix = cfg["title_prefix"]
    cycle_low = float(cfg["cycle_low_usd"])
    cycle_date = cfg["cycle_low_date"]
    px = f"{spot:,.0f}달러" if spot is not None else "없음"
    s50 = f"{sma50:,.0f}달러" if sma50 is not None else "없음"
    s200 = f"{sma200:,.0f}달러" if sma200 is not None else "없음"
    wc = f"{week_close:,.0f}달러" if week_close is not None else "없음"
    wd = week_date.strftime("%Y-%m-%d") if week_date else "없음"
    rz = f"{realized:,.0f}달러 ({realized_d})" if realized is not None else "없음"
    table = [
        f"현물 {px}" + (f" / 여름저점 대비 {pct(spot, cycle_low)}" if spot else ""),
        f"50주 이평 {s50}" + (f" / 현물 {pct(spot, sma50)}" if spot and sma50 else ""),
        f"200주 이평 {s200}" + (f" / 현물 {pct(spot, sma200)}" if spot and sma200 else ""),
        f"실현가격 {rz}" + (f" / 현물 {pct(spot, realized)}" if spot and realized else ""),
        f"여름 저점 {cycle_low:,.0f}달러 ({cycle_date} Kraken, 6월 25일 {float(cfg['june_low_usd']):,.0f}달러)",
        f"마감 주봉 {wd} 종가 {wc}" + (f" / 50주선 {pct(week_close, sma50)}" if week_close and sma50 else ""),
    ]
    rules = (
        f"폐기: {cfg['invalidate_date']} KST까지 10월 일중저가 {cycle_low:,.0f}달러 미만 없음. "
        f"조기 약화: 마감 주봉 종가가 50주 이평 위. "
        f"유효 확정: 10월 일중저가 {cycle_low:,.0f}달러 미만."
    )
    titles = {
        "FETCH_FAIL": f"{prefix} BTC 조회 실패 | 판정 보류",
        "SNAPSHOT": f"{prefix} BTC 구조 스냅샷 | 10월 저점 유지",
        "ALIVE": f"{prefix} BTC 10월 저점 시나리오 유지 | 추격 금지",
        "WEEKLY_ABOVE": f"{prefix} BTC 주봉이 50주선 위 | 조기 약화",
        "CONFIRMED": f"{prefix} BTC 여름 저점 하향 | 10월 시나리오 유효",
        "INVALIDATED": f"{prefix} BTC 10월 저점 폐기 | 여름 저점 유지",
    }
    now_do = {
        "FETCH_FAIL": "직접 시세만 보고, 이 알림으로 사지 마세요.",
        "SNAPSHOT": "숫자는 기록만 하세요. 83k 돌파 추격은 하지 마세요.",
        "ALIVE": "여름 저점이 10월에 깨지기 전에는 시나리오를 버리지 마세요. 추격 금지.",
        "WEEKLY_ABOVE": "타이밍 가설은 약해졌습니다. 그래도 11월 1일 전까지는 폐기하지 마세요.",
        "CONFIRMED": "10월에 여름 저점을 하향했습니다. 평균 단가를 낮출 계획만 점검하세요.",
        "INVALIDATED": "10월 저점 타이밍은 폐기입니다. 저점 대기 매수는 접으세요.",
    }
    later = {
        "FETCH_FAIL": "다음 평일 09:00 KST에 한 번 더 봅니다.",
        "SNAPSHOT": "이후에는 10월 여름저점 하향 또는 11월 1일 폐기가 아니면 다시 안 보냅니다.",
        "ALIVE": "평일 09:00 KST에만 보고, 조건이 안 바뀌면 조용히 넘어갑니다.",
        "WEEKLY_ABOVE": "11월 1일까지 여름 저점만 추가로 감시합니다.",
        "CONFIRMED": "이 작업은 확정 알림 후 재발송하지 않습니다.",
        "INVALIDATED": "이 작업은 폐기 알림 후 재발송하지 않습니다.",
    }
    action = {
        "FETCH_FAIL": "조회 실패를 호재/악재로 보지 마세요.",
        "SNAPSHOT": "이 표로 포지션을 늘리지 마세요.",
        "ALIVE": "10월 저점을 기다린다고 지금 사지 마세요.",
        "WEEKLY_ABOVE": "주봉 1개를 사이클 종료로 단정하지 마세요.",
        "CONFIRMED": "하향을 추격 매도로 연결하지 마세요.",
        "INVALIDATED": "폐기 이후에도 하락이 없다고 단정하지 마세요.",
    }
    title = titles[kind]
    body = [
        f"이게 뭐 알림: {cfg['explain']}",
        f"지금 결과: {kind} | " + " | ".join(table),
        f"지금 할 일: {now_do[kind]}",
        f"나중에: {later[kind]}",
        f"익절/줄일 때: {rules}",
        f"하지 말 것: {action[kind]} 신용/미수, 없는 숫자를 맞춰 넣기.",
    ]
    pri = 5 if kind in {"CONFIRMED", "INVALIDATED"} else 4 if kind == "FETCH_FAIL" else 3
    return title, body, pri


def should_send(kind: str, state: dict) -> bool:
    if kind == "FETCH_FAIL":
        return not state.get("fail_notified")
    if kind == "CONFIRMED":
        return not state.get("confirmed_sent")
    if kind == "INVALIDATED":
        return not state.get("invalidated_sent")
    if kind == "WEEKLY_ABOVE":
        return not state.get("weekly_above_sent")
    if kind == "SNAPSHOT":
        return not state.get("snapshot_sent")
    return False


def mark_sent(kind: str, state: dict) -> None:
    if kind == "FETCH_FAIL":
        state["fail_notified"] = True
    elif kind == "CONFIRMED":
        state["confirmed_sent"] = True
        state["snapshot_sent"] = True
    elif kind == "INVALIDATED":
        state["invalidated_sent"] = True
        state["snapshot_sent"] = True
    elif kind == "WEEKLY_ABOVE":
        state["weekly_above_sent"] = True
        state["snapshot_sent"] = True
    elif kind == "SNAPSHOT":
        state["snapshot_sent"] = True


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
    if state.get("confirmed_sent") or state.get("invalidated_sent"):
        print("terminal state; skip")
        return 0

    errors: list[str] = []
    spot = sma50 = sma200 = week_close = None
    week_date = None
    realized = None
    realized_d = None
    scan = {"summer_print": None, "oct_print": None, "broke": None, "last_daily": None}

    try:
        spot = fetch_spot(cfg["sources"]["spot"])
    except Exception as exc:
        errors.append(f"현물 실패: {exc}")
    try:
        weeks = closed_weeks(kraken_rows(cfg["sources"]["weekly"]), datetime.now(timezone.utc))
        closes = [w[1] for w in weeks]
        sma50 = sma(closes, 50)
        sma200 = sma(closes, 200)
        if weeks:
            week_date, week_close, _ = weeks[-1]
    except Exception as exc:
        errors.append(f"주봉 실패: {exc}")
    try:
        daily = kraken_rows(cfg["sources"]["daily"])
        scan = scan_daily(daily, float(cfg["cycle_low_usd"]))
        if scan["summer_print"] and scan["summer_print"][1] < float(cfg["cycle_low_usd"]):
            cfg = dict(cfg)
            cfg["cycle_low_usd"] = scan["summer_print"][1]
            cfg["cycle_low_date"] = scan["summer_print"][0].strftime("%Y-%m-%d")
    except Exception as exc:
        errors.append(f"일봉 실패: {exc}")
    realized, realized_d, rz_err = fetch_realized(cfg["sources"]["realized"])
    errors.extend(rz_err)

    kind = kind_for(now, cfg, spot, sma50, week_close, scan)
    if kind == "ALIVE" and not state.get("snapshot_sent"):
        kind = "SNAPSHOT"

    title, body, pri = copy_for(
        kind, cfg, now, spot, sma50, sma200, week_close, week_date, realized, realized_d, scan
    )
    lines = [f"{now.strftime('%Y-%m-%d %H:%M KST')} | BTCOCT | 10월 저점", *body]
    if errors:
        lines.append("조회 메모: " + " / ".join(errors[:4]))
    if scan.get("broke"):
        ts, low = scan["broke"]
        lines.append(f"하향 기록: {ts.strftime('%Y-%m-%d')} 일중저가 {low:,.0f}달러")
    if scan.get("oct_print"):
        ts, low = scan["oct_print"]
        lines.append(f"10월 최저: {ts.strftime('%Y-%m-%d')} {low:,.0f}달러")
    msg = "\n".join(lines)
    print(msg)
    print(f"verdict={kind} dry={dry}")

    send = should_send(kind, state)
    if dry:
        print("dry-run; no ntfy")
    elif send:
        publish(
            resolve_topic("port", cfg.get("ntfy_topic")),
            title,
            msg,
            pri,
            ["chart_with_upwards_trend", "orange_circle"],
            resolve_server(cfg.get("ntfy_server")),
        )
        mark_sent(kind, state)
    else:
        print("no ntfy; duplicate suppressed")

    state.update({"last_kind": kind, "spot": spot, "sma50": sma50, "sma200": sma200, "updated_at": now.isoformat()})
    if not dry:
        save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
