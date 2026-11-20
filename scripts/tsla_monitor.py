#!/usr/bin/env python3
"""[TSLA] last price during US regular hours (approved 2026-10-29).

Stage 1: last <= 310 USD, priority 4, once per America/New_York date.
Stage 2: last <= 300 USD, priority 5, once per America/New_York date.
A run at or under 300 sends only stage 2 and marks stage 1 sent for that date.

Session gate: weekdays 09:30-16:00 America/New_York inclusive, so EDT and EST
both stay inside the cash session. The GitHub backup cron is a broad UTC window
(*/30 13-21 * * 1-5). Ticks outside the session, including late backups, stay silent.

Modes: auto | force | dry [price] | selftest.
dry never calls publish() and never writes state/.
Not investment advice.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import append_ends, publish, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
NY = ZoneInfo("America/New_York")
CFG_PATH = ROOT / "configs" / "tsla.json"
STATE_PATH = ROOT / "state" / "tsla.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}
OPEN_MIN = 9 * 60 + 30
CLOSE_MIN = 16 * 60
OFF_SESSION = {"PRE", "POST", "PREPRE", "POSTPOST", "CLOSED"}
FORBIDDEN = ("\u00b7", "\u2014")  # middle dot, em dash; kept as escapes so copy stays free of them
CHECKLIST = (
    "확인할 것: 비감독 무인 마일 증가 유지, 2027년 2분기 고객 무인 일정 유지 여부. "
    "조건 유지 시 총자산 소수 % 위성, 지정가 분할로만."
)


def load_cfg() -> dict:
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    return {"sent": {"s310": None, "s300": None}, "fail_day": None, "updated_at": None}


def save_state(state: dict, dry: bool) -> None:
    if dry:
        return
    state["updated_at"] = datetime.now(KST).isoformat(timespec="seconds")
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def in_regular_session(now_ny: datetime) -> bool:
    if now_ny.weekday() >= 5:
        return False
    minutes = now_ny.hour * 60 + now_ny.minute
    return OPEN_MIN <= minutes <= CLOSE_MIN


def pick_stage(price: float) -> str | None:
    """Deeper level wins. 300 is stage 2 only; 310 is stage 1 only."""
    if price <= 300:
        return "s300"
    if price <= 310:
        return "s310"
    return None


def should_send(stage: str | None, day: str, sent: dict) -> str | None:
    if stage is None:
        return None
    if (sent or {}).get(stage) == day:
        return None
    return stage


def mark_sent(sent: dict, day: str, stage: str) -> dict:
    out = dict(sent or {})
    out[stage] = day
    if stage == "s300":
        out["s310"] = day
    return out


def message(cfg: dict, stage: str, price: float, stamp: str) -> tuple[str, str, int]:
    spec = cfg["stages"][stage]
    if stage == "s300":
        later = "310달러 안내는 이 알림으로 갈음합니다. 각 단계는 미국 거래일당 한 번입니다."
    else:
        later = "300달러 이하면 위성 분할 매수 검토 알림이 따로 옵니다. 각 단계는 미국 거래일당 한 번입니다."
    lines = [
        f"이게 뭐 알림: {cfg['explain']}",
        f"지금 결과/상태: {spec['status']} 현재가 {price:.2f}달러. 시각 {stamp}.",
        f"지금 할 일: {cfg['checklist']}",
        f"나중에: {later}",
        "익절/줄일 때: 이 알림은 매도 신호가 아닙니다. 익절 가격은 여기서 정하지 않습니다.",
        "하지 말 것: 전량 즉시 매수, 신용, 미수, 시장가 추격. 조회 실패를 매수 신호로 보지 말 것.",
    ]
    body = append_ends("\n".join(lines), "tsla")
    return spec["title"], body, int(spec["priority"])


def _chart_url(host: str, ticker: str) -> str:
    return f"{host}{ticker}?interval=1m&range=1d"


def fetch_last(ticker: str) -> tuple[float | None, str | None]:
    """Return (last regular price, marketState). (None, None) means the fetch failed."""
    last_exc: Exception | None = None
    for host in (
        "https://query1.finance.yahoo.com/v8/finance/chart/",
        "https://query2.finance.yahoo.com/v8/finance/chart/",
    ):
        req = urllib.request.Request(_chart_url(host, ticker), headers=UA)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode())
            result = (payload.get("chart") or {}).get("result") or []
            if not result:
                raise ValueError("empty chart result")
            meta = result[0].get("meta") or {}
            raw = meta.get("regularMarketPrice")
            if raw is None:
                raise ValueError("regularMarketPrice missing")
            price = float(raw)
            if price != price or price <= 0:  # NaN or unusable
                raise ValueError(f"bad price {raw!r}")
            state = meta.get("marketState")
            return price, (str(state) if state else None)
        except Exception as exc:  # noqa: BLE001 - both hosts, then one ops notice
            last_exc = exc
            print(f"yahoo fail {host}: {exc}", file=sys.stderr)
    print(f"yahoo failed both hosts: {last_exc}", file=sys.stderr)
    return None, None


def fail_message(stamp: str) -> tuple[str, str]:
    title = "[TSLA] 시세 조회 실패 | 매매 신호 아님"
    lines = [
        "이게 뭐 알림: 테슬라 현재가를 읽지 못했습니다. 매매 신호가 아닙니다.",
        f"지금 결과/상태: 시세 조회 실패. 현재가 없음. 시각 {stamp}.",
        "지금 할 일: 증권앱에서 TSLA 현재가를 직접 확인하세요.",
        "나중에: 다음 미국 정규장 정시(1시간) 실행에서 다시 읽습니다. 실패 안내는 하루 한 번입니다.",
        "익절/줄일 때: 이번 안내는 매매 신호가 아닙니다.",
        "하지 말 것: 조회 실패를 매수나 매도 신호로 보지 말 것.",
    ]
    return title, append_ends("\n".join(lines), "tsla")


def _emit(title: str, body: str, priority: int, topic: str, dry: bool) -> None:
    print(f"DRY RUN would send (p{priority}): {title}\n{body}" if dry else f"send p{priority}: {title}")
    if dry:
        print("DRY RUN: no ntfy, no state write")
        return
    publish(topic, title, body, priority, tags=["chart_with_downwards_trend"], config_name="tsla")


def _parse_price_arg(rest: list[str]) -> float | None:
    if not rest:
        return None
    raw = rest[1] if rest[0] == "--price" else rest[0]
    try:
        return float(raw)
    except ValueError:
        print(f"bad price {raw!r}", file=sys.stderr)
        raise SystemExit(2)


def run(mode: str, price_arg: float | None, now_ny: datetime | None = None) -> int:
    dry = mode in {"dry", "test", "dry-run"}
    cfg = load_cfg()
    now_ny = now_ny or datetime.now(NY)
    now_kst = now_ny.astimezone(KST)
    stamp = now_kst.strftime("%Y-%m-%d %H:%M KST")
    day = now_ny.date().isoformat()
    print(f"{stamp} ny={now_ny:%Y-%m-%d %H:%M %Z} mode={mode} day={day}")
    if cfg.get("checklist") != CHECKLIST:
        print("checklist mismatch", file=sys.stderr)
        return 2
    if price_arg is None and not in_regular_session(now_ny):
        print("outside NY regular session; silent")
        return 0

    if price_arg is None:
        price, market_state = fetch_last(cfg["ticker"])
    else:
        price, market_state = price_arg, "REGULAR"
    print(f"price={price} marketState={market_state}")

    state = load_state()
    sent = state.get("sent") if isinstance(state.get("sent"), dict) else {}

    if price is None:
        if state.get("fail_day") == day and mode != "force":
            print("fetch failed; ops notice already sent today")
            return 0
        title, body = fail_message(stamp)
        _emit(title, body, 3, resolve_topic("ops"), dry)
        if not dry:
            state["fail_day"] = day
            save_state(state, dry)
        return 0

    if market_state in OFF_SESSION:
        print(f"marketState {market_state}; not a regular last price; silent")
        return 0

    stage = pick_stage(price)
    if mode != "force":
        stage = should_send(stage, day, sent)
    if stage is None:
        print("no new stage; silent")
        return 0

    title, body, priority = message(cfg, stage, price, stamp)
    if any(ch in title or ch in body for ch in FORBIDDEN):
        print("refusing send: forbidden punctuation in copy", file=sys.stderr)
        return 2
    topic = resolve_topic("ops") if "조회 실패" in title else resolve_topic("port")
    _emit(title, body, priority, topic, dry)
    if not dry:
        state["sent"] = mark_sent(sent, day, stage)
        save_state(state, dry)
    return 0


def selftest() -> int:
    cfg = load_cfg()
    fails = 0
    stamp = "2026-10-29 23:40 KST"

    def check(label: str, got, want) -> None:
        nonlocal fails
        ok = got == want
        fails += not ok
        print(("ok  " if ok else "FAIL"), label, got, "" if ok else f"want {want}")

    check("checklist", cfg.get("checklist"), CHECKLIST)
    check("ends_kst null", cfg.get("ends_kst"), None)
    check("no valid_until", "valid_until" in cfg, False)
    check("no year_guard", "year_guard" in cfg, False)
    check("305 stage", pick_stage(305), "s310")
    check("310 exact", pick_stage(310), "s310")
    check("310.01", pick_stage(310.01), None)
    check("300.01 stage1", pick_stage(300.01), "s310")
    check("300 exact stage2", pick_stage(300), "s300")
    check("295 stage2 only", pick_stage(295), "s300")
    check("311 none", pick_stage(311), None)

    sent: dict = {}
    check("305 first", should_send("s310", "2026-10-29", sent), "s310")
    sent = mark_sent(sent, "2026-10-29", "s310")
    check("305 again same day", should_send("s310", "2026-10-29", sent), None)
    check("295 after 305", should_send("s300", "2026-10-29", sent), "s300")
    sent = mark_sent({}, "2026-10-29", "s300")
    check("stage2 marks stage1", sent.get("s310"), "2026-10-29")
    check("305 after stage2 same day", should_send("s310", "2026-10-29", sent), None)
    check("next day stage2", should_send("s300", "2026-10-30", sent), "s300")

    title, body, pri = message(cfg, "s310", 305, stamp)
    check("305 title", title, "[TSLA] 310달러 이하 | 7월 저점 접근")
    check("305 pri", pri, 4)
    check("305 price", "305.00달러" in body, True)
    check("305 stamp", stamp in body, True)
    check("305 status", "TSLA 310달러 이하, 297달러 7월 저점 접근 중." in body, True)
    check("305 checklist", CHECKLIST in body, True)
    check("305 not stage2 title", "[TSLA] 300달러 이하" not in title, True)

    title2, body2, pri2 = message(cfg, "s300", 295, stamp)
    check("295 title", title2, "[TSLA] 300달러 이하 | 위성 분할 매수 검토")
    check("295 pri", pri2, 5)
    check("295 price", "295.00달러" in body2, True)
    check("295 stamp", stamp in body2, True)
    check("295 status", "TSLA 300달러 이하, 위성 분할 매수 검토 구간." in body2, True)
    check("295 checklist", CHECKLIST in body2, True)
    check("295 not stage1 status", "7월 저점 접근 중" not in body2, True)
    check("295 higher than 305", pri2 > pri, True)

    order = ["이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것", "꺼지는 때"]
    for text in (body, body2):
        idx = [text.find(label) for label in order]
        check("body order", idx, sorted(idx))
        check("no forbidden", any(ch in text for ch in FORBIDDEN), False)
        check("no fixed expiry", "2027-07-01" in text or "2027-06-30" in text, False)
        check("open ended", "종료일 없음" in text, True)

    def walk(obj) -> None:
        nonlocal fails
        if isinstance(obj, str) and any(ch in obj for ch in FORBIDDEN):
            fails += 1
            print("FAIL config punctuation", obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(cfg)

    edt_open = datetime(2026, 10, 30, 13, 30, tzinfo=timezone.utc).astimezone(NY)
    est_open = datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc).astimezone(NY)
    check("EDT 13:30 UTC is 09:30", edt_open.strftime("%H:%M"), "09:30")
    check("EST 14:30 UTC is 09:30", est_open.strftime("%H:%M"), "09:30")
    check("open inclusive", in_regular_session(edt_open), True)
    check("09:29 out", in_regular_session(edt_open.replace(minute=29)), False)
    close = edt_open.replace(hour=16, minute=0)
    check("16:00 in", in_regular_session(close), True)
    check("16:01 out", in_regular_session(close.replace(minute=1)), False)
    check("saturday out", in_regular_session(edt_open.replace(day=31)), False)  # 2026-10-31 Sat
    check("est open in", in_regular_session(est_open), True)
    early_est = datetime(2026, 11, 2, 13, 30, tzinfo=timezone.utc).astimezone(NY)
    check("13:30 UTC in EST is before open", in_regular_session(early_est), False)

    print(f"selftest fails={fails}")
    return 1 if fails else 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = (argv[0] if argv else "auto").lower()
    if mode in {"selftest", "self-test"}:
        return selftest()
    if mode not in {"auto", "force", "dry", "test", "dry-run"}:
        print(f"unknown mode {mode}", file=sys.stderr)
        return 2
    price_arg = _parse_price_arg(argv[1:])
    dry = mode in {"dry", "test", "dry-run"}
    if dry:
        print("DRY RUN")
    return run(mode, price_arg)


if __name__ == "__main__":
    raise SystemExit(main())
