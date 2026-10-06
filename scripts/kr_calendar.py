#!/usr/bin/env python3
"""KRX trading-day calendar (2026). Update KR_HOLIDAYS each December."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

KR_HOLIDAYS = {
    "2026-01-01",                              # 신정
    "2026-02-16", "2026-02-17", "2026-02-18",  # 설날
    "2026-03-02",                              # 삼일절 대체
    "2026-05-01",                              # 근로자의 날 (KRX 휴장)
    "2026-05-05",                              # 어린이날
    "2026-05-25",                              # 부처님오신날 대체
    "2026-06-03",                              # 지방선거
    "2026-08-17",                              # 광복절 대체
    "2026-09-24", "2026-09-25", "2026-09-28",  # 추석 + 대체
    "2026-10-05",                              # 개천절 대체
    "2026-10-09",                              # 한글날
    "2026-12-25",                              # 성탄절
    "2026-12-31",                              # 연말 휴장
}


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d.isoformat() not in KR_HOLIDAYS


def prev_trading_day(d: date) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def session_day(now: datetime | None = None, rollover_hour: int = 6) -> date:
    """Session a post-close job should judge. Late GitHub runs after midnight
    (before rollover_hour KST) still belong to the previous calendar day."""
    now = (now or datetime.now(KST)).astimezone(KST)
    d = now.date()
    if now.hour < rollover_hour:
        d -= timedelta(days=1)
    return d


if __name__ == "__main__":
    import sys

    d = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else datetime.now(KST).date()
    print(d, "trading" if is_trading_day(d) else "holiday")
    sys.exit(0 if is_trading_day(d) else 1)
