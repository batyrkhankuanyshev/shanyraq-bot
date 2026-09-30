"""Guest log: day visits auto-approved, overnight stays need a mentor."""
from datetime import datetime, timedelta

from ..db import tx
from ..notices import Notice, kb
from ..timeutil import day_bounds, fmt_dt, from_db, local_today, to_db
from . import audit, users

MAX_ACTIVE = 2
DURATIONS = {  # code: (label, hours, overnight)
    "2h": ("2 hours", 2, False), "4h": ("4 hours", 4, False), "8h": ("8 hours", 8, False),
    "1n": ("1 night", 24, True), "2n": ("2 nights", 48, True), "3n": ("3 nights", 72, True),
}
LIVE = ("pending", "approved")


def get(conn, gid: int):
    return conn.execute("SELECT * FROM guests WHERE id = ?", (gid,)).fetchone()


def active_for(conn, host: int, now: datetime):
    return conn.execute(
        "SELECT * FROM guests WHERE host_id = ? AND status IN ('pending', 'approved') AND depart_at > ? ORDER BY arrive_at",
        (host, to_db(now)),
    ).fetchall()


def describe(g) -> str:
    kind = "overnight" if g["overnight"] else "day visit"
    return f"{g['name']} · {fmt_dt(from_db(g['arrive_at']))} → {fmt_dt(from_db(g['depart_at']))} ({kind}, {g['status']})"


def register(conn, host: int, name: str, arrive: datetime, duration: str, now: datetime) -> tuple[str, list[Notice]]:
    label, hours, overnight = DURATIONS[duration]
    depart = arrive + timedelta(hours=hours)
    with tx(conn):
        if len(active_for(conn, host, now)) >= MAX_ACTIVE:
            raise ValueError(f"You already have {MAX_ACTIVE} active guests — check one out first.")
        status = "pending" if overnight else "approved"
        cur = conn.execute(
            "INSERT INTO guests(host_id, name, arrive_at, depart_at, overnight, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (host, name, to_db(arrive), to_db(depart), int(overnight), status, to_db(now)),
        )
        g = get(conn, cur.lastrowid)
        host_name = users.name_of(conn, host)
        if overnight:
            notices = users.notify_mentors(
                conn, f"🚪 Overnight guest request from {host_name}:\n{describe(g)}",
                kb([("✅ Approve", f"g:ap:{g['id']}"), ("❌ Reject", f"g:rj:{g['id']}")]), silent=False,
            )
            msg = "📨 Overnight stay requested — a mentor will approve it soon. I'll let you know."
        else:
            notices = users.notify_mentors(conn, f"🚪 Heads-up: {host_name} has a day visitor.\n{describe(g)}", silent=True)
            msg = "✅ Guest registered (day visit, auto-approved)."
    return msg, notices


def decide(conn, actor: int, gid: int, approve: bool, reason: str | None = None) -> list[Notice]:
    with tx(conn):
        g = get(conn, gid)
        if not g or g["status"] != "pending":
            raise ValueError("That request was already handled.")
        conn.execute(
            "UPDATE guests SET status = ?, decided_by = ?, reason = ? WHERE id = ?",
            ("approved" if approve else "rejected", actor, reason, gid),
        )
        audit.log(conn, actor, "guest_" + ("approve" if approve else "reject"), f"#{gid} {g['name']} ({reason or ''})")
    if approve:
        return [Notice(g["host_id"], f"✅ Your overnight guest {g['name']} was approved.")]
    return [Notice(g["host_id"], f"❌ Your overnight guest {g['name']} was not approved. Reason: {reason or '—'}", silent=False)]


def check_out(conn, host: int, gid: int, now: datetime) -> bool:
    with tx(conn):
        cur = conn.execute(
            "UPDATE guests SET status = 'checked_out', depart_at = ? WHERE id = ? AND host_id = ? AND status IN ('pending', 'approved')",
            (to_db(now), gid, host),
        )
    return cur.rowcount == 1


def today_list(conn, now: datetime):
    start, end = day_bounds(local_today(now))
    return conn.execute(
        "SELECT g.*, u.name AS host_name FROM guests g JOIN users u ON u.tg_id = g.host_id "
        "WHERE g.status IN ('pending', 'approved', 'checked_out') AND g.arrive_at < ? AND g.depart_at > ? ORDER BY g.arrive_at",
        (to_db(end), to_db(start)),
    ).fetchall()


def pending(conn):
    return conn.execute(
        "SELECT g.*, u.name AS host_name FROM guests g JOIN users u ON u.tg_id = g.host_id WHERE g.status = 'pending' ORDER BY g.arrive_at"
    ).fetchall()


def tick(conn, now: datetime) -> list[Notice]:
    notices = []
    for g in conn.execute(
        "SELECT * FROM guests WHERE status IN ('pending', 'approved') AND depart_at <= ?", (to_db(now),)
    ).fetchall():
        conn.execute("UPDATE guests SET status = 'expired' WHERE id = ?", (g["id"],))
        if g["status"] == "approved":
            notices.append(Notice(g["host_id"], f"🚪 {g['name']}'s visit has ended (auto check-out)."))
    return notices
