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
CFG_PATH = ROOT / "configs" / "ratesbell.json"
STATE_PATH = ROOT / "state" / "ratesbell.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts ratesbell"}
MONTHS = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
}


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


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def yahoo_tnx() -> tuple[float | None, str | None]:
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ETNX?interval=1d&range=10d"
    raw = json.loads(fetch(url))
    result = raw["chart"]["result"][0]
    meta = result.get("meta", {})
    price = meta.get("regularMarketPrice")
    ts = meta.get("regularMarketTime")
    if price is None:
        return None, None
    day = datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d") if ts else None
    return float(price), day


def freddie_weeks() -> list[tuple[str, float]]:
    html = fetch("https://www.freddiemac.com/pmms/archive")
    pairs = re.findall(
        r"<strong[^>]*>\s*([A-Za-z]+ \d{2}, \d{4})\s*</strong>[\s\S]{0,900}?"
        r"<td class=\"text-center\">\s*([0-9]+\.[0-9]+)%</td>",
        html,
    )
    out = []
    for label, rate in pairs:
        month, day, year = label.replace(",", "").split()
        iso = f"{int(year):04d}-{MONTHS[month]:02d}-{int(day):02d}"
        out.append((iso, float(rate)))
    out.sort()
    return out[-8:]


def status_of(ten: float | None, mtg: float | None, streak: int, cfg: dict) -> str:
    if ten is None or mtg is None:
        return "FAIL"
    hi = float(cfg["ten_escalate"])
    lo = float(cfg["ten_relief"])
    mtg_hi = float(cfg["mtg_escalate"])
    need = int(cfg["mtg_weeks"])
    if ten >= hi or (mtg >= mtg_hi and streak >= need):
        return "ESCALATE"
    if ten < lo and mtg < mtg_hi:
        return "RELIEF"
    return "WATCH"


def body(stamp: str, cfg: dict, ten, ten_day, mtg, mtg_day, streak: int, status: str, kind: str):
    ten_txt = f"{ten:.2f}%" if ten is not None else "없음"
    mtg_txt = f"{mtg:.2f}%" if mtg is not None else "없음"
    common = [
        f"{stamp} | FLOW | 미국 10년물, 30년 모기지",
        "이게 뭐 알림: 국채금리와 모기지 금리가 2008년식 위기 진입 조건에 닿는지 보는 알림입니다. 위기 확정이 아닙니다.",
    ]
    if kind == "FAIL" or status == "FAIL":
        title = "[FLOW] 금리 조회 실패 | 직접 확인"
        lines = common + [
            "지금 결과/상태: 10년물 또는 모기지 조회 실패. 실패를 관망으로 처리하지 않습니다.",
            "지금 할 일: 야후 ^TNX와 프레디맥 PMMS를 직접 확인하세요.",
            "나중에: 다음 평일 09:00 KST에 재시도합니다.",
            "익절/줄일 때: 이번 알림은 매매 신호가 아닙니다.",
            "하지 말 것: 조회 실패를 금리 하락이나 매수 신호로 보기. 신용, 미수.",
        ]
        return title, lines, 4
    if status == "ESCALATE":
        title = f"[FLOW] 10년 {ten_txt} | 위기경계, 추격 금지"
        action = "변경조건 충족. 신규 위험자산 추격 금지. 국채 듀레이션은 분할만 검토."
        pri = 5
    elif status == "RELIEF":
        title = f"[FLOW] 10년 {ten_txt} | 5% 아래 되돌림"
        action = "10년물이 5% 아래로 되밀렸습니다. 위기경계 해제 후보. 하루 수치로 전량 전환하지 마세요."
        pri = 4
    else:
        title = f"[FLOW] 10년 {ten_txt} | 관망, 추격 금지"
        action = "5.00~5.50% 관망 구간. 모기지 7.50% 2주 연속 전에는 위기 진입으로 보지 않습니다."
        pri = 3
    lines = common + [
        (
            f"지금 결과/상태: 10년물 {ten_txt} ({ten_day or '날짜없음'}). "
            f"30년 모기지 {mtg_txt} ({mtg_day or '날짜없음'}). "
            f"7.50% 이상 연속 {streak}주. 판정 {status}."
        ),
        f"지금 할 일: {action}",
        "나중에: 10년 종가 5.50% 안착, 또는 모기지 7.50%가 2주 연속이면 경계로 바꿉니다. 10년 5% 아래면 되돌림.",
        "익절/줄일 때: 경계에서는 위험자산 추격을 줄입니다. 되돌림이어도 레버리지 복구는 금지.",
        "하지 말 것: 이 알림만으로 2008년 재현이라 단정. 신용, 미수, 조회 실패를 정상으로 보기.",
    ]
    return title, lines, pri


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    dry = mode in {"test", "dry", "dry-run"}
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = load_json(STATE_PATH, {
        "dgs10": None,
        "dgs10_date": None,
        "mortgage": None,
        "mortgage_date": None,
        "status": None,
        "mtg_streak": 0,
        "last_kind": None,
        "fail_sent_date": None,
        "weekly_sent_date": None,
        "updated_at": None,
    })
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    year_ok = now.year == int(cfg["year_guard"])
    valid = today <= cfg["valid_until"]
    print(f"{stamp} mode={mode} year_ok={year_ok} valid={valid}")
    if mode not in {"force", "dry", "test", "dry-run"} and (not year_ok or not valid):
        print("outside window; silent")
        return 0

    ten = ten_day = None
    weeks = []
    fails = []
    try:
        ten, ten_day = yahoo_tnx()
    except Exception as exc:
        fails.append(f"tnx {exc}")
        print(f"tnx fail: {exc}", file=sys.stderr)
    try:
        weeks = freddie_weeks()
    except Exception as exc:
        fails.append(f"pmms {exc}")
        print(f"pmms fail: {exc}", file=sys.stderr)

    mtg = mtg_day = None
    streak = int(state.get("mtg_streak") or 0)
    if weeks:
        mtg_day, mtg = weeks[-1]
        prev_date = state.get("mortgage_date")
        if mtg_day != prev_date:
            streak = streak + 1 if mtg >= float(cfg["mtg_escalate"]) else 0
        elif mtg < float(cfg["mtg_escalate"]):
            streak = 0
    status = status_of(ten, mtg, streak, cfg)
    print(f"ten={ten} {ten_day} mtg={mtg} {mtg_day} streak={streak} status={status} fails={fails}")

    kind = None
    monday = now.weekday() == 0
    if status == "FAIL":
        if state.get("fail_sent_date") != today or mode in {"force", "dry", "test"}:
            kind = "FAIL"
    elif status != state.get("status") or mode in {"force", "dry", "test"}:
        kind = "CHANGE"
    elif monday and state.get("weekly_sent_date") != today:
        kind = "WEEKLY"

    if kind is None:
        print("no ratesbell trigger; silent")
        state.update({
            "dgs10": ten, "dgs10_date": ten_day, "mortgage": mtg,
            "mortgage_date": mtg_day, "status": status, "mtg_streak": streak,
        })
        if not dry:
            save_state(state, now)
        return 0

    title, lines, pri = body(stamp, cfg, ten, ten_day, mtg, mtg_day, streak, status, kind)
    msg = "\n".join(lines)
    print(title)
    print(msg)
    print(f"kind={kind} dry={dry}")
    if dry:
        print("dry-run; no ntfy")
    else:
        publish(
            resolve_topic("port", cfg.get("ntfy_topic")),
            title,
            msg,
            pri,
            ["chart_with_upwards_trend"],
            resolve_server(cfg.get("ntfy_server")),
        )
        if kind == "FAIL":
            state["fail_sent_date"] = today
        if kind == "WEEKLY":
            state["weekly_sent_date"] = today
        if monday and kind == "CHANGE":
            state["weekly_sent_date"] = today
    state.update({
        "dgs10": ten, "dgs10_date": ten_day, "mortgage": mtg,
        "mortgage_date": mtg_day, "status": status, "mtg_streak": streak,
        "last_kind": kind,
    })
    if not dry:
        save_state(state, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
