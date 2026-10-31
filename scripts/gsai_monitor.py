#!/usr/bin/env python3
"""Exact KRX foreign-total net-buy signal, supplied by the browser relay only.

The relay must obtain TRDVAL4 (KRW, not quantity * price), and independently
cross-check trading_dates with Naver KOSPI daily dates. No network data fetch
or local holiday/year table is used here. Prefer 20-40 trading dates; at least
three complete dates are required to prove a newly started two-day streak.
Legacy estimated history is never reused as evidence.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ntfy_util

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "gsai.json"
STATE_PATH = ROOT / "state" / "gsai.json"
CODES = ("005930", "000660")
NAMES = {"005930": "삼성전자", "000660": "SK하이닉스"}
THRESHOLD = 500_000_000_000
META = {"schema_version": 1, "source": "KRX_MDCSTAT02302",
        "market": "KRX_REGULAR", "investor": "FOREIGN_TOTAL", "unit": "KRW"}


def no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_json(raw):
    return json.loads(raw, object_pairs_hook=no_duplicates,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))


def iso_day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("date must be YYYY-MM-DD")
    return date.fromisoformat(value)


def validate_payload(raw, now):
    p = parse_json(raw)
    if not isinstance(p, dict) or set(p) != set(META) | {"asof", "observed_at", "trading_dates", "series"}:
        raise ValueError("invalid payload fields; estimated/legacy data not accepted")
    if type(p["schema_version"]) is not int or any(p[k] != v for k, v in META.items()):
        raise ValueError("invalid schema/source/market/investor/unit")
    now = now.astimezone(KST)
    asof = iso_day(p["asof"])
    expected = now.date() - timedelta(days=1) if now.hour < 6 else now.date()
    if asof != expected:
        raise ValueError("asof is not the current session")
    cutoff = datetime.combine(asof, time(20, 30), KST)
    if now < cutoff:
        raise ValueError("confirmation requires 20:30 KST or later")
    stamp = p["observed_at"]
    if not isinstance(stamp, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?\+09:00", stamp):
        raise ValueError("observed_at requires ISO timestamp with +09:00")
    observed = datetime.fromisoformat(stamp)
    if observed < cutoff or not timedelta(0) <= now - observed <= timedelta(minutes=90):
        raise ValueError("observation is early, stale, or in the future")
    days = p["trading_dates"]
    if not isinstance(days, list) or not 3 <= len(days) <= 40:
        raise ValueError("require 3-40 cross-checked trading dates (prefer 20-40)")
    parsed = [iso_day(d) for d in days]
    if len(set(days)) != len(days) or parsed != sorted(parsed, reverse=True):
        raise ValueError("trading_dates must be unique descending dates")
    if days[0] != p["asof"] or any(d.weekday() >= 5 for d in parsed):
        raise ValueError("today's exact trading data missing or invalid weekday")
    series = p["series"]
    if not isinstance(series, dict) or set(series) != set(CODES):
        raise ValueError("both exact stock codes required, no other codes")
    for code in CODES:
        rows = series[code]
        if not isinstance(rows, list) or len(rows) != len(days):
            raise ValueError(f"{code}: complete values for all expected dates required")
        dates = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"date", "net_buy_won"}:
                raise ValueError(f"{code}: exact net_buy_won only, no estimates")
            iso_day(row["date"])
            if type(row["net_buy_won"]) is not int:
                raise ValueError(f"{code}: net_buy_won must be integer KRW")
            dates.append(row["date"])
        if len(set(dates)) != len(dates) or set(dates) != set(days):
            raise ValueError(f"{code}: dates differ from expected trading_dates")
        by_day = {row["date"]: row for row in rows}
        series[code] = [by_day[d] for d in days]
    return p


def fresh_state():
    return {"schema_version": 2,
            "per_code": {code: {"last_notified_streak_start": None} for code in CODES},
            "updated_at": None}


def load_state(path):
    if not path.exists():
        return fresh_state()
    old = parse_json(path.read_text(encoding="utf-8"))
    if not isinstance(old, dict):
        raise ValueError("invalid state")
    version = old.get("schema_version")
    if version is None or (type(version) is int and version == 1):
        # Old combined/estimated amounts cannot seed exact per-stock markers.
        # Migration is in memory only until a successful real publish.
        return fresh_state()
    if type(version) is not int or version != 2 or set(old.get("per_code", {})) != set(CODES):
        raise ValueError("invalid/unsupported state schema")
    for item in old["per_code"].values():
        if not isinstance(item, dict) or "last_notified_streak_start" not in item:
            raise ValueError("invalid state marker")
        marker = item["last_notified_streak_start"]
        if marker is not None:
            iso_day(marker)
    return old


def streak(rows):
    length = 0
    for row in rows:
        if row["net_buy_won"] < THRESHOLD:
            break
        length += 1
    # If all supplied rows qualify, the true start lies outside the snapshot.
    start = rows[length - 1]["date"] if length and length < len(rows) else None
    return length, start


def build_message(payload, eligible):
    details = []
    for code in eligible:
        rows = payload["series"][code]
        amounts = ", ".join(f"{r['date']} {r['net_buy_won']:+,}원" for r in reversed(rows[:2]))
        details.append(f"{NAMES[code]}({code}) {amounts}")
    title = "[GSAI] 005930/000660 동일 종목 외국인 5천억원 이상 2거래일 | 추격매수 금지"
    body = "\n".join([
        "이게 뭐 알림: 정규시장 외국인 합계 순매수 거래대금, 동일 종목 매일 5천억원 이상 2거래일 첫 충족. 매수 주문 아님.",
        "지금 상태: " + " / ".join(details),
        f"지금 할 일: KRX_MDCSTAT02302 TRDVAL4 원 단위 실제금액 확인. 기준 {payload['asof']}, 관측 {payload['observed_at']}. 기존 투자계획 점검.",
        "나중에: 3일째 이상 반복 발송 안 함. 미충족 거래일 뒤 새 2일 연속 충족 때 재알림.",
        "익절/줄일 때: 별도 보유 기준을 따르세요. 이 신호는 매수 주문이 아닙니다.",
        "하지 말 것: 두 종목 합산, 수량×종가 추정, 신호만 보고 추격매수 또는 신용/미수.",
    ])
    return title, body


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE_PATH)


def main(argv=None, environ=None, now=None):
    args = sys.argv[1:] if argv is None else argv
    env = os.environ if environ is None else environ
    raw = env.get("GSAI_PAYLOAD")
    if raw is None or raw == "":
        return 0  # Existing 15:40 bundle: absolutely no payload, no side effects.
    dry = "dry" in [a.lower() for a in args] or env.get("GSAI_MODE", "").lower() == "dry"
    try:
        p = validate_payload(raw, now or datetime.now(KST))
        cfg = parse_json(CFG_PATH.read_text(encoding="utf-8"))
        if (not isinstance(cfg, dict) or cfg.get("codes") != list(CODES)
                or type(cfg.get("threshold_won")) is not int or cfg["threshold_won"] != THRESHOLD
                or type(cfg.get("consecutive_days")) is not int or cfg["consecutive_days"] != 2):
            raise ValueError("config must retain the exact per-stock two-day rule")
        state = load_state(STATE_PATH)
        eligible = {}
        for code in CODES:
            length, start = streak(p["series"][code])
            marker = state["per_code"][code]["last_notified_streak_start"]
            print(f"{code} asof={p['asof']} streak={length} start={start or 'unconfirmed'}")
            if length == 2 and start is not None and (marker is None or start > marker):
                eligible[code] = start
        if not eligible:
            print("DRY RUN: no signal; no writes" if dry else "gsai no new two-day signal")
            return 0
        title, body = build_message(p, eligible)
        print(title + "\n" + body)
        if dry:
            print("DRY RUN: no publish, no state writes")
            return 0
        ntfy_util.LAST_PUBLISH.clear()  # A stale prior success is not this send's receipt.
        ntfy_util.publish(ntfy_util.resolve_topic("port", cfg.get("ntfy_topic")), title, body,
                          4, [], ntfy_util.resolve_server(cfg.get("ntfy_server")), config_name="gsai")
        if ntfy_util.LAST_PUBLISH.get("sent") is not True:
            raise ValueError("publish not confirmed; markers unchanged")
        for code, start in eligible.items():
            state["per_code"][code]["last_notified_streak_start"] = start
        state["updated_at"] = p["observed_at"]
        save_state(state)
        return 0
    except Exception as exc:
        print(f"GSAI fail closed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
