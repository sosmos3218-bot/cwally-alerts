#!/usr/bin/env python3
"""KRX trading-day calendar (2026, 2027 provisional). Update KR_HOLIDAYS each December."""
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
    "2026-07-17",                              # 제헌절 (2026-04 공휴일 지정, KRX 휴장)
    "2026-08-17",                              # 광복절 대체
    "2026-09-24", "2026-09-25",                # 추석 (9/26 토와 겹쳐도 대체 없음, 9/28 은 KRX 개장)
    "2026-10-05",                              # 개천절 대체
    "2026-10-09",                              # 한글날
    "2026-12-25",                              # 성탄절
    "2026-12-31",                              # 연말 휴장
    # 2027: PROVISIONAL (added 2026-10-23). Not yet formally announced by KRX (open.krx.co.kr year
    # selector stops at 2026; KRX confirms next-year holidays in December). Source: 2027 월력요항
    # (우주항공청 관보 2026-06-29, 노동절/제헌절 공휴일 지정 2026-04 반영) + KASI 2027 달력자료, and
    # the same 16 dates already returned by KRX's own holiday data (open.krx.co.kr mkd01100305_01,
    # search_bas_yy=2027) on 2026-10-23. No national election in 2027.
    # TODO(2026-12): reconcile with the official KRX 2027 휴장일 list and drop this PROVISIONAL note.
    "2027-01-01",                              # 신정
    "2027-02-08", "2027-02-09",                # 설날 (2/7 일) + 설날 대체 (2/6 토, 2/7 일 겹침)
    "2027-03-01",                              # 삼일절
    "2027-05-03",                              # 노동절 대체 (5/1 토)
    "2027-05-05",                              # 어린이날
    "2027-05-13",                              # 부처님오신날
    "2027-07-19",                              # 제헌절 대체 (7/17 토)
    "2027-08-16",                              # 광복절 대체 (8/15 일)
    "2027-09-14", "2027-09-15", "2027-09-16",  # 추석
    "2027-10-04",                              # 개천절 대체 (10/3 일)
    "2027-10-11",                              # 한글날 대체 (10/9 토)
    "2027-12-27",                              # 성탄절 대체 (12/25 토)
    "2027-12-31",                              # 연말 휴장
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


def session_close_ready(day: str, today: str, now: datetime, close_hm: tuple[int, int] = (15, 30)) -> bool:
    """True once the KRX regular close for `day` is known at `now`.

    `today` is usually session_day (rolls back before 06:00 KST). A late GitHub backup
    after midnight still has session_day = yesterday, but wall-clock hour is 0..5, so a
    plain `now.hour < 15:30` check would treat yesterday's close as still intraday and
    skip it (2026-11-04: 005930 last_day stuck after Persist push failed; fixed in
    86854a0, then shared here). Past session days are always ready; the current session
    day is ready after market close, or once the calendar date has moved past it.
    Also safe when `today` is the calendar date instead of session_day.
    """
    now = now.astimezone(KST)
    if day > today:
        return False
    if day < today:
        return True
    return (now.hour, now.minute) >= close_hm or now.date().isoformat() > today


if __name__ == "__main__":
    import sys

    d = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else datetime.now(KST).date()
    print(d, "trading" if is_trading_day(d) else "holiday")
    sys.exit(0 if is_trading_day(d) else 1)
