"""Time helpers. Everything is stored in UTC and shown in the hostel's zone."""
import os
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()
TZ = ZoneInfo(os.getenv("HOSTEL_TZ", "Asia/Hong_Kong"))
DB_FMT = "%Y-%m-%dT%H:%M:%SZ"


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_db(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(DB_FMT)


def from_db(value: str) -> datetime:
    return datetime.strptime(value, DB_FMT).replace(tzinfo=timezone.utc)


def local(dt: datetime) -> datetime:
    return dt.astimezone(TZ)


def local_today(now: datetime) -> date:
    return local(now).date()


def at_local(d: date, hhmm: str) -> datetime:
    """Local date + 'HH:MM' -> aware UTC datetime (DST-safe)."""
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(d, time(h, m), tzinfo=TZ).astimezone(timezone.utc)


def day_bounds(d: date) -> tuple[datetime, datetime]:
    start = datetime.combine(d, time(0, 0), tzinfo=TZ).astimezone(timezone.utc)
    end = datetime.combine(d + timedelta(days=1), time(0, 0), tzinfo=TZ).astimezone(timezone.utc)
    return start, end


def week_start(d: date) -> date:
    """Monday of the week containing d."""
    return d - timedelta(days=d.weekday())


def dkey(d: date) -> str:
    """Compact date for callback data: 20261001."""
    return d.strftime("%Y%m%d")


def parse_dkey(value: str) -> date:
    return datetime.strptime(value, "%Y%m%d").date()


def fmt_date(d: date) -> str:
    return d.strftime("%a %d %b")


def fmt_dt(dt: datetime) -> str:
    return local(dt).strftime("%a %d %b %H:%M")


def fmt_time(dt: datetime) -> str:
    return local(dt).strftime("%H:%M")


def day_label(d: date, today: date) -> str:
    delta = (d - today).days
    if delta == 0:
        return f"Today ({d.strftime('%d %b')})"
    if delta == 1:
        return f"Tomorrow ({d.strftime('%d %b')})"
    return fmt_date(d)


def parse_user_datetime(text: str, now: datetime) -> datetime | None:
    """Accept 'YYYY-MM-DD HH:MM', 'DD.MM HH:MM', 'DD.MM.YYYY HH:MM' (local)."""
    text = text.strip()
    today = local_today(now)
    for fmt in ("%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M", "%d/%m/%Y %H:%M"):
        try:
            dt = datetime.strptime(text, fmt)
            return at_local(dt.date(), dt.strftime("%H:%M"))
        except ValueError:
            pass
    for fmt in ("%d.%m %H:%M", "%d/%m %H:%M"):
        try:
            dt = datetime.strptime(text, fmt).replace(year=today.year)
            result = at_local(dt.date(), dt.strftime("%H:%M"))
            if result < now - timedelta(days=1):
                result = at_local(dt.date().replace(year=today.year + 1), dt.strftime("%H:%M"))
            return result
        except ValueError:
            pass
    return None


def parse_hhmm(text: str) -> str | None:
    try:
        return datetime.strptime(text.strip().replace(".", ":"), "%H:%M").strftime("%H:%M")
    except ValueError:
        return None
