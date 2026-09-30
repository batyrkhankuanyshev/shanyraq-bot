"""Background scheduler. Every job is state-based (it looks at the database and
does whatever is due), so after a restart or downtime it simply catches up."""
import asyncio
import logging
from datetime import datetime

from .services import announcements, duty, events, guests, washer
from .timeutil import utcnow

log = logging.getLogger(__name__)
JOBS = [
    ("washer", washer.tick),          # reminders, start prompts, no-shows, end prompts, waitlist windows, recurring
    ("duty", duty.tick),              # rotations (Sun 18:00 / 20:00), 08:00 reminders, missed-duty pings
    ("announcements", announcements.tick),  # scheduled announcements, 22:00 quiet hours
    ("guests", guests.tick),          # auto check-out at departure
    ("events", events.tick),          # 1h reminders, weekly regeneration
]


def run_all(conn, now: datetime) -> list:
    notices = []
    for name, job in JOBS:
        try:
            notices += job(conn, now)
        except Exception:
            log.exception("job %s failed", name)
    return notices


async def scheduler_loop(bot, conn) -> None:
    from .sender import deliver

    while True:
        started = utcnow()
        try:
            await deliver(bot, conn, run_all(conn, started))
        except Exception:
            log.exception("scheduler tick failed")
        await asyncio.sleep(max(5.0, 60 - (utcnow() - started).total_seconds()))
