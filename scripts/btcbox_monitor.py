#!/usr/bin/env python3
"""BTC box rules: 82k close break, 85.8k, 87.2k, 3-day ETF inflow, 10y < 5.1."""
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
CFG_PATH = ROOT / "configs" / "btcbox.json"
STATE_PATH = ROOT / "state" / "btcbox.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}
MONTHS = {
    "Jan": "01",
    "Feb": "02",
    "Mar": "03",
    "Apr": "04",
    "May": "05",
    "Jun": "06",
    "Jul": "07",
    "Aug": "08",
    "Sep": "09",
    "Oct": "10",
    "Nov": "11",
    "Dec": "12",
}


def fetch(url: str) -> str:
    import urllib.request

    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def fetch_json(url: str):
    return json.loads(fetch(url))


def load_state() -> dict:
    state = {
        "verdict": None,
        "baseline_sent": False,
        "fail_notified": False,
        "missing_notified": 0,
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


def parse_num(raw: str) -> float | None:
    txt = raw.strip().replace(",", "").replace("$", "")
    if not txt or txt in {"-", "—"}:
        return None
    neg = txt.startswith("(") and txt.endswith(")")
    txt = txt.strip("()")
    try:
        val = float(txt)
    except ValueError:
        return None
    return -val if neg else val


def fetch_close(cfg: dict) -> tuple[float | None, str, list[str]]:
    errors = []
    try:
        rows = fetch_json(cfg["sources"]["price"][0])
        if rows:
            rows = sorted(rows, key=lambda r: int(r[0]))
            now_utc = datetime.now(timezone.utc)
            closed = [r for r in rows if int(r[0]) + 86400 <= now_utc.timestamp()]
            candle = closed[-1] if closed else rows[-2]
            close = float(candle[4])
            day = datetime.fromtimestamp(int(candle[0]), tz=timezone.utc).strftime("%Y-%m-%d")
            return close, f"coinbase UTC 일봉 {day} 종가 {close:.0f}", errors
        errors.append("coinbase: 캔들 없음")
    except Exception as exc:
        errors.append(f"coinbase 캔들 실패: {exc}")
    try:
        data = fetch_json(cfg["sources"]["price"][1])
        pair = next(k for k in data["result"] if k != "last")
        rows = data["result"][pair]
        candle = rows[-2]
        close = float(candle[4])
        day = datetime.fromtimestamp(int(candle[0]), tz=timezone.utc).strftime("%Y-%m-%d")
        return close, f"kraken UTC 일봉 {day} 종가 {close:.0f}", errors
    except Exception as exc:
        errors.append(f"kraken 실패: {exc}")
    try:
        data = fetch_json(cfg["sources"]["price"][2])
        last = float(data["data"]["amount"])
        return last, f"coinbase 현재가 {last:.0f} (종가 대체, 이탈 판정 아님)", errors
    except Exception as exc:
        errors.append(f"coinbase spot 실패: {exc}")
    return None, "", errors


def fetch_etf(cfg: dict) -> tuple[list[dict] | None, list[str]]:
    errors = []
    try:
        html = fetch(cfg["sources"]["etf"])
    except Exception as exc:
        return None, [f"farside 조회 실패: {exc}"]
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.I | re.S):
        cells = [
            re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip()
            for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, flags=re.I | re.S)
        ]
        if len(cells) < 3:
            continue
        m = re.match(
            r"(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(20\d{2})$",
            cells[0],
            re.I,
        )
        if not m:
            continue
        iso = f"{m.group(3)}-{MONTHS[m.group(2).title()]}-{int(m.group(1)):02d}"
        total = parse_num(cells[-1])
        incomplete = any(c in {"-", "—"} for c in cells[1:])
        if total is None:
            continue
        rows.append({"date": iso, "total_m": total, "incomplete": incomplete})
    done = [r for r in rows if not r["incomplete"]]
    if len(done) < 3:
        errors.append("farside: 마감된 3거래일 없음")
        return None, errors
    return done[-3:], errors


def fetch_yield(cfg: dict) -> tuple[float | None, str, list[str]]:
    errors = []
    try:
        csv = fetch(cfg["sources"]["yield"][0])
        last = None
        for line in csv.splitlines()[1:]:
            parts = line.split(",")
            if len(parts) < 2 or parts[1] in {"", "."}:
                continue
            last = (parts[0], float(parts[1]))
        if last:
            return last[1], f"FRED DGS10 {last[0]} {last[1]:.2f}%", errors
        errors.append("fred: 숫자 없음")
    except Exception as exc:
        errors.append(f"fred 실패: {exc}")
    try:
        data = fetch_json(cfg["sources"]["yield"][1])
        closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        vals = [v for v in closes if isinstance(v, (int, float))]
        if vals:
            raw = float(vals[-1])
            yld = raw / 10.0 if raw > 20 else raw
            return yld, f"Yahoo ^TNX {yld:.2f}%", errors
        errors.append("yahoo: 종가 없음")
    except Exception as exc:
        errors.append(f"yahoo 실패: {exc}")
    return None, "", errors


def verdict(close: float | None, close_note: str, etf: list[dict] | None, yld: float | None, cfg: dict) -> str:
    if close is None:
        return "FETCH_FAIL"
    if "종가 대체" in close_note or etf is None or yld is None:
        return "DATA_MISSING"
    support = float(cfg["levels"]["support_close"])
    top = float(cfg["levels"]["range_top"])
    chase = float(cfg["levels"]["chase_close"])
    gate = float(cfg["yield_below"])
    inflow = all(r["total_m"] > 0 for r in etf)
    y_ok = yld < gate
    if close < support:
        return "BREAK_82"
    if close >= chase and inflow and y_ok:
        return "CHASE_87"
    if close >= chase:
        return "TOUCH_87_WAIT"
    if close >= top and inflow and y_ok:
        return "TOP_WAIT"
    if close >= top:
        return "RANGE_TOP"
    if inflow and y_ok:
        return "SETUP_WAIT"
    return "RANGE_HOLD"


def copy_for(kind: str, cfg: dict, close, close_note: str, etf, yld, y_note: str) -> tuple[str, list[str], int]:
    px = f"{close:,.0f}달러" if close is not None else "없음"
    etf_txt = "마감 3일 없음"
    if etf:
        etf_txt = ", ".join(f"{r['date']} {r['total_m']:+.1f}" for r in etf) + " 백만달러"
    y_txt = y_note or "없음"
    explain = cfg["explain"]
    titles = {
        "FETCH_FAIL": "[PORT] BTC 조회 실패 | 판정 보류",
        "DATA_MISSING": "[PORT] BTC 자료 없음 | 판정 보류",
        "BREAK_82": "[PORT] 82,000 종가 이탈 | 비중 축소",
        "CHASE_87": "[PORT] 87,200 안착+ETF+금리 | 추격 후보",
        "TOUCH_87_WAIT": "[PORT] 87,200만 터치 | 추격 금지",
        "TOP_WAIT": "[PORT] 85,800 위, 조건 충족 | 돌파 대기",
        "RANGE_TOP": "[PORT] 85,800 상단 | 추가매수 금지",
        "SETUP_WAIT": "[PORT] ETF·금리만 충족 | 가격 대기",
        "RANGE_HOLD": "[PORT] 박스 유지 | 추가매수 보류",
    }
    actions = {
        "FETCH_FAIL": "시세를 직접 확인하고 이 알림으로 매매하지 마세요.",
        "DATA_MISSING": "종가, ETF 3일, 10년물 중 하나가 없습니다. 맞춘 숫자로 행동하지 마세요.",
        "BREAK_82": "마감 종가가 82,000달러 아래입니다. 비중을 줄이고 7만달러 후반을 열어 두세요.",
        "CHASE_87": "종가 87,200달러 이상, ETF 3거래일 순유입, 10년물 5.1% 미만입니다. 추격 후보만 열고 신용은 쓰지 마세요.",
        "TOUCH_87_WAIT": "87,200달러는 넘었지만 ETF 3일 또는 10년물 5.1% 조건이 없습니다. 추격하지 마세요.",
        "TOP_WAIT": "85,800달러는 넘고 보조 조건은 맞지만 87,200달러 안착 전입니다. 추가매수하지 마세요.",
        "RANGE_TOP": "85,800~87,200달러 구간입니다. 추가매수하지 마세요.",
        "SETUP_WAIT": "ETF 3일과 10년물 5.1% 하회는 맞지만 가격이 85,800달러 아래입니다. 아직 사지 마세요.",
        "RANGE_HOLD": "82,000~85,800달러 박스입니다. 보유 유지, 추가매수 보류입니다.",
    }
    pri = 5 if kind == "BREAK_82" else 4 if kind in {"CHASE_87", "FETCH_FAIL"} else 3
    return (
        titles[kind],
        [
            f"이게 뭐 알림: {explain}",
            f"지금 결과: {kind} | BTC {px} | {close_note} | ETF {etf_txt} | 10년물 {y_txt}.",
            f"지금 할 일: {actions[kind]}",
            "나중에: 평일 09:00 KST에만 다시 봅니다. 판정이 같으면 알림을 반복하지 않습니다. 10월 31일 이후 종료.",
            "익절/줄일 때: 82,000달러 종가 이탈이면 줄입니다. 그 전에는 이 알림으로 팔지 마세요.",
            "하지 말 것: 85,800달러 돌파만 보고 추격, 신용/미수, 주말 공백을 정시 체크로 착각.",
        ],
        pri,
    )


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

    close, close_note, price_err = fetch_close(cfg)
    etf, etf_err = fetch_etf(cfg)
    yld, y_note, y_err = fetch_yield(cfg)
    kind = verdict(close, close_note, etf, yld, cfg)
    title, body, pri = copy_for(kind, cfg, close, close_note, etf, yld, y_note)
    lines = [f"{now.strftime('%Y-%m-%d %H:%M KST')} | PORT | BTC box", *body]
    errs = price_err + etf_err + y_err
    if errs:
        lines.append("조회 메모: " + " / ".join(errs[:4]))
    msg = "\n".join(lines)
    print(msg)
    print(f"verdict={kind} dry={dry}")

    same = state.get("verdict") == kind and state.get("baseline_sent")
    send = False
    if kind == "FETCH_FAIL":
        send = not state.get("fail_notified")
        if send and not dry:
            state["fail_notified"] = True
    elif kind == "DATA_MISSING":
        send = int(state.get("missing_notified") or 0) < 2
        if send and not dry:
            state["missing_notified"] = int(state.get("missing_notified") or 0) + 1
    else:
        send = not same
        if send and not dry:
            state["baseline_sent"] = True
            state["fail_notified"] = False

    if dry:
        print("dry-run; no ntfy")
    elif send:
        publish(
            resolve_topic("port", cfg.get("ntfy_topic")),
            title,
            msg,
            pri,
            ["chart_with_upwards_trend"],
            resolve_server(cfg.get("ntfy_server")),
        )
        print("ntfy sent")
    else:
        print("no ntfy; duplicate suppressed")

    state.update(
        {
            "verdict": kind,
            "close": close,
            "close_note": close_note,
            "etf": etf,
            "yield": yld,
            "yield_note": y_note,
            "updated_at": now.isoformat(),
        }
    )
    if not dry:
        save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
