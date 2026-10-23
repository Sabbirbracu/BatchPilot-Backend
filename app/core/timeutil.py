"""Business dates are Asia/Dhaka; storage is UTC timestamptz."""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.core.config import get_settings

DHAKA = ZoneInfo(get_settings().business_tz)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_local() -> datetime:
    return datetime.now(DHAKA)


def today_local() -> date:
    return now_local().date()


def month_start(d: date) -> date:
    return d.replace(day=1)


def current_month() -> date:
    return month_start(today_local())


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


def due_date_for(month: date, due_day: int) -> date:
    # Clamp to 28 so February always works.
    return month.replace(day=min(max(due_day, 1), 28))


def local_day_bounds_utc(d: date) -> tuple[datetime, datetime]:
    start = datetime.combine(d, time.min, tzinfo=DHAKA)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def parse_month(value: str) -> date:
    """Accepts 'YYYY-MM' or 'YYYY-MM-DD' and returns the first of that month."""
    parts = value.split("-")
    return date(int(parts[0]), int(parts[1]), 1)
