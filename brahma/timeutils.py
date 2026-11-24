"""Timezone / market-session helpers (all trading logic is IST)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Asia/Kolkata"


def get_tz(name: str = DEFAULT_TZ) -> ZoneInfo:
    return ZoneInfo(name)


def now_ist(tz_name: str = DEFAULT_TZ) -> datetime:
    return datetime.now(get_tz(tz_name))


def ensure_aware(value: datetime, tz_name: str = DEFAULT_TZ) -> datetime:
    tz = get_tz(tz_name)
    if value.tzinfo is None:
        return value.replace(tzinfo=tz)
    return value.astimezone(tz)


def parse_hhmm(value: str) -> time:
    hour, minute = value.strip().split(":")
    return time(int(hour), int(minute))


def session_bounds(day: date, open_hhmm: str, close_hhmm: str, tz_name: str = DEFAULT_TZ) -> tuple[datetime, datetime]:
    tz = get_tz(tz_name)
    start = datetime.combine(day, parse_hhmm(open_hhmm), tzinfo=tz)
    end = datetime.combine(day, parse_hhmm(close_hhmm), tzinfo=tz)
    return start, end


def is_trading_day(day: date, holidays: tuple[str, ...] = ()) -> bool:
    if day.weekday() >= 5:
        return False
    return day.isoformat() not in set(holidays)


def market_state(
    now: datetime,
    open_hhmm: str = "09:15",
    close_hhmm: str = "15:30",
    holidays: tuple[str, ...] = (),
    tz_name: str = DEFAULT_TZ,
) -> str:
    """Return one of PRE, OPEN, CLOSED, HOLIDAY."""
    local = ensure_aware(now, tz_name)
    if not is_trading_day(local.date(), holidays):
        return "HOLIDAY"
    start, end = session_bounds(local.date(), open_hhmm, close_hhmm, tz_name)
    if local < start:
        return "PRE"
    if local >= end:
        return "CLOSED"
    return "OPEN"


def at_or_after(now: datetime, day: date, hhmm: str, tz_name: str = DEFAULT_TZ) -> bool:
    tz = get_tz(tz_name)
    target = datetime.combine(day, parse_hhmm(hhmm), tzinfo=tz)
    return ensure_aware(now, tz_name) >= target


def candle_age_seconds(candle_ts: datetime, now: datetime | None = None, tz_name: str = DEFAULT_TZ) -> float:
    current = ensure_aware(now or now_ist(tz_name), tz_name)
    ts = ensure_aware(candle_ts, tz_name)
    return max(0.0, (current - ts).total_seconds())


def timeframe_seconds(timeframe: str) -> int:
    tf = timeframe.strip().lower()
    if tf.endswith("m"):
        return int(float(tf[:-1]) * 60)
    if tf.endswith("h"):
        return int(float(tf[:-1]) * 3600)
    if tf.endswith("d"):
        return int(float(tf[:-1]) * 86400)
    return int(float(tf))


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return ensure_aware(value).isoformat()


def parse_iso(value: str | None, tz_name: str = DEFAULT_TZ) -> datetime | None:
    if not value:
        return None
    try:
        return ensure_aware(datetime.fromisoformat(value), tz_name)
    except ValueError:
        return None


def epoch_seconds(value: datetime) -> int:
    return int(ensure_aware(value).timestamp())


def previous_trading_days(end: date, count: int, holidays: tuple[str, ...] = ()) -> list[date]:
    days: list[date] = []
    cursor = end
    guard = 0
    while len(days) < count and guard < count * 10 + 30:
        if is_trading_day(cursor, holidays):
            days.append(cursor)
        cursor -= timedelta(days=1)
        guard += 1
    return list(reversed(days))
