"""서울 시각과 KRX 거래일.

거래일 판단은 exchange_calendars XKRX 를 기준으로 합니다.
XKRX 4.13.2 가 휴장으로 보지 않지만 한국거래소가 쉬는 날은 EXTRA_CLOSURES 로 뺍니다.
(scripts/kr_calendar.py 와 맞춤, 2026-11-02 기준)
  2026-06-03 지방선거
  2026-07-17 제헌절
  2027-05-03 노동절 대체
  2027-07-19 제헌절 대체
달력 범위 밖 날짜는 추정하지 않고 오류로 멈춥니다.
"""

from __future__ import annotations

from datetime import date, datetime, time
from functools import lru_cache
from zoneinfo import ZoneInfo

from trend_monitor.errors import UserInputError

SEOUL = ZoneInfo("Asia/Seoul")
SESSION_CLOSE = time(15, 30)

# XKRX 가 개장으로 두지만 KRX 가 쉬는 날.
EXTRA_CLOSURES = frozenset(
    {
        date(2026, 6, 3),
        date(2026, 7, 17),
        date(2027, 5, 3),
        date(2027, 7, 19),
    }
)


def now_kst(clock: datetime | None = None) -> datetime:
    """Asia/Seoul 현재 시각. 시스템 시간대에 의존하지 않습니다."""
    if clock is None:
        return datetime.now(SEOUL)
    if clock.tzinfo is None:
        raise UserInputError("시각에 시간대가 없습니다. Asia/Seoul 로 넣으세요.")
    return clock.astimezone(SEOUL)


def parse_trade_date(text: str) -> date:
    """YYYY-MM-DD 또는 YYYYMMDD. 다른 형식은 받지 않습니다."""
    import re

    raw = text.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        fmt = "%Y-%m-%d"
    elif re.fullmatch(r"\d{8}", raw):
        fmt = "%Y%m%d"
    else:
        raise UserInputError(f"날짜 형식이 아닙니다. YYYY-MM-DD 또는 YYYYMMDD 를 쓰세요: {text!r}")
    try:
        return datetime.strptime(raw, fmt).date()
    except ValueError as exc:
        raise UserInputError(f"날짜 형식이 아닙니다. YYYY-MM-DD 또는 YYYYMMDD 를 쓰세요: {text!r}") from exc


@lru_cache(maxsize=1)
def xkrx_calendar():
    try:
        import exchange_calendars as xcals
    except ImportError as exc:
        raise UserInputError(
            "exchange_calendars 가 없습니다. trend_monitor/requirements.txt 를 설치하세요."
        ) from exc
    return xcals.get_calendar("XKRX")


def is_trading_day(day: date) -> bool:
    if day in EXTRA_CLOSURES:
        return False
    cal = xkrx_calendar()
    try:
        return bool(cal.is_session(day.isoformat()))
    except Exception as exc:
        name = type(exc).__name__
        if "DateOutOfBounds" in name or "out of bounds" in str(exc).lower():
            raise UserInputError(
                f"{day.isoformat()} 은 XKRX 달력 범위 밖입니다. 휴장 여부를 추정하지 않습니다."
            ) from exc
        raise


def previous_trading_day(day: date) -> date:
    """day 자신은 넣지 않고, 바로 앞 거래일을 찾습니다."""
    probe = date.fromordinal(day.toordinal() - 1)
    for _ in range(21):
        if is_trading_day(probe):
            return probe
        probe = date.fromordinal(probe.toordinal() - 1)
    raise UserInputError(f"{day.isoformat()} 이전 거래일을 찾지 못했습니다.")


def default_trade_date(clock: datetime | None = None) -> date:
    """서울 기준 최근 마감 거래일. 15:30 이전이면 전 거래일입니다."""
    now = now_kst(clock)
    day = now.date()
    closed = now.time() >= SESSION_CLOSE
    if is_trading_day(day) and closed:
        return day
    if is_trading_day(day):
        return previous_trading_day(day)
    probe = day
    for _ in range(21):
        if is_trading_day(probe):
            return probe
        probe = date.fromordinal(probe.toordinal() - 1)
    raise UserInputError(f"{day.isoformat()} 이전 거래일을 찾지 못했습니다.")


def assert_session_ready(target: date, clock: datetime | None, allow_partial: bool) -> str | None:
    """마감 전 당일이면 멈춥니다. 허용 시 경고 문장을 돌려줍니다."""
    now = now_kst(clock)
    if target > now.date():
        raise UserInputError(
            f"기준일 {target.isoformat()} 은 서울 오늘 {now.date().isoformat()} 보다 뒤입니다."
        )
    if not is_trading_day(target):
        raise UserInputError(f"{target.isoformat()} 은 KRX 거래일이 아닙니다.")
    close_dt = datetime.combine(target, SESSION_CLOSE, tzinfo=SEOUL)
    if now < close_dt and not allow_partial:
        raise UserInputError(
            f"서울 {now.strftime('%H:%M')} 는 15:30 이전이므로 {target.isoformat()} 봉이 마감 전입니다. "
            "마감 후에 다시 실행하거나 --allow-partial 을 쓰세요."
        )
    if now < close_dt and allow_partial:
        return f"{target.isoformat()} 봉은 15:30 이전이라 미완성일 수 있습니다 (--allow-partial)."
    return None
