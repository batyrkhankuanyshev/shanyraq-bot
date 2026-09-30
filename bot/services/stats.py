from datetime import datetime

from . import duty, washer


def overview(conn, now: datetime) -> str:
    return washer.stats_text(conn, now) + "\n\n" + duty.fairness_text(conn)
