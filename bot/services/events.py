"""Events calendar with RSVPs, 1-hour reminders and weekly recurring events."""
from datetime import datetime, timedelta

from ..config import CONFIG
from ..db import tx
from ..notices import Notice, kb
from ..timeutil import fmt_dt, from_db, to_db
from . import audit, users

RSVP = {"going": "✅ Going", "maybe": "🤔 Maybe", "cant": "❌ Can't"}
REMIND_BEFORE = timedelta(hours=1)


def get(conn, eid: int):
    return conn.execute("SELECT * FROM events WHERE id = ?", (eid,)).fetchone()


def counts(conn, eid: int) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) c FROM rsvps WHERE event_id = ? GROUP BY status", (eid,)).fetchall()
    result = {k: 0 for k in RSVP}
    result.update({r["status"]: r["c"] for r in rows})
    return result


def text_of(conn, e, with_counts: bool = True) -> str:
    lines = [f"📅 {e['title']}" + (" (weekly)" if e["recurring"] else ""), f"🕒 {fmt_dt(from_db(e['starts_at']))}"]
    if e["location"]:
        lines.append(f"📍 {e['location']}")
    if e["description"]:
        lines.append(e["description"])
    if with_counts:
        c = counts(conn, e["id"])
        lines.append(f"Going {c['going']} · Maybe {c['maybe']} · Can't {c['cant']}")
    return "\n".join(lines)


def rsvp_buttons(eid: int):
    return kb([(label, f"e:r:{eid}:{code}") for code, label in RSVP.items()])


def _announce(conn, e, creator: int | None, silent: bool) -> list[Notice]:
    text = "🆕 New event!\n" + text_of(conn, e, with_counts=False)
    notices = [Notice(u["tg_id"], text, rsvp_buttons(e["id"]), silent=silent)
               for u in users.everyone(conn) if u["tg_id"] != creator]
    if CONFIG.events_chat_id:
        notices.append(Notice(CONFIG.events_chat_id, text, silent=True, thread_id=CONFIG.events_thread_id))
    return notices


def create(conn, creator: int, title: str, description: str, location: str, starts_at: datetime,
           recurring: bool, now: datetime) -> tuple[int, list[Notice]]:
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO events(creator_id, title, description, location, starts_at, recurring, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (creator, title, description, location, to_db(starts_at), int(recurring), to_db(now)),
        )
        e = get(conn, cur.lastrowid)
        conn.execute(
            "INSERT INTO rsvps(event_id, user_id, status, updated_at) VALUES (?, ?, 'going', ?)",
            (e["id"], creator, to_db(now)),
        )
        return e["id"], _announce(conn, e, creator, silent=True)


def set_rsvp(conn, eid: int, uid: int, status: str, now: datetime) -> str:
    e = get(conn, eid)
    if not e or e["deleted"] or status not in RSVP:
        return "This event no longer exists."
    if from_db(e["starts_at"]) < now:
        return "This event has already started."
    with tx(conn):
        conn.execute(
            "INSERT INTO rsvps(event_id, user_id, status, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(event_id, user_id) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at",
            (eid, uid, status, to_db(now)),
        )
    return f"{RSVP[status]} — got it!"


def upcoming(conn, now: datetime, limit: int = 10):
    return conn.execute(
        "SELECT * FROM events WHERE deleted = 0 AND starts_at > ? ORDER BY starts_at LIMIT ?",
        (to_db(now - timedelta(hours=2)), limit),
    ).fetchall()


def can_edit(e, user) -> bool:
    return user["role"] == "mentor" or e["creator_id"] == user["tg_id"]


def update_field(conn, actor, eid: int, field: str, value) -> list[Notice]:
    if field not in ("title", "description", "location", "starts_at"):
        raise ValueError("bad field")
    with tx(conn):
        conn.execute(f"UPDATE events SET {field} = ?, reminder_sent = 0 WHERE id = ?", (value, eid))
        audit.log(conn, actor["tg_id"], "event_edit", f"#{eid} {field}")
        e = get(conn, eid)
        if field != "starts_at":
            return []
        return [Notice(r["user_id"], "🔁 Event time changed:\n" + text_of(conn, e, with_counts=False))
                for r in conn.execute("SELECT user_id FROM rsvps WHERE event_id = ? AND status != 'cant'", (eid,))
                if r["user_id"] != actor["tg_id"]]


def delete(conn, actor, eid: int) -> list[Notice]:
    with tx(conn):
        e = get(conn, eid)
        if not e or e["deleted"]:
            return []
        conn.execute("UPDATE events SET deleted = 1 WHERE id = ?", (eid,))
        audit.log(conn, actor["tg_id"], "event_delete", f"#{eid} {e['title']}")
        return [Notice(r["user_id"], f"🗑 Event cancelled: {e['title']} ({fmt_dt(from_db(e['starts_at']))})")
                for r in conn.execute("SELECT user_id FROM rsvps WHERE event_id = ? AND status != 'cant'", (eid,))
                if r["user_id"] != actor["tg_id"]]


def tick(conn, now: datetime) -> list[Notice]:
    notices = []
    for e in conn.execute(
        "SELECT * FROM events WHERE deleted = 0 AND reminder_sent = 0 AND starts_at > ? AND starts_at <= ?",
        (to_db(now), to_db(now + REMIND_BEFORE)),
    ).fetchall():
        conn.execute("UPDATE events SET reminder_sent = 1 WHERE id = ?", (e["id"],))
        for r in conn.execute("SELECT user_id FROM rsvps WHERE event_id = ? AND status = 'going'", (e["id"],)):
            notices.append(Notice(r["user_id"], "⏰ Starting within the hour:\n" + text_of(conn, e, with_counts=False)))
    # Weekly recurring events regenerate once their current instance has started
    for e in conn.execute(
        "SELECT * FROM events WHERE deleted = 0 AND recurring = 1 AND regenerated = 0 AND starts_at <= ?", (to_db(now),)
    ).fetchall():
        nxt = from_db(e["starts_at"]) + timedelta(days=7)
        while nxt <= now:
            nxt += timedelta(days=7)
        conn.execute("UPDATE events SET regenerated = 1 WHERE id = ?", (e["id"],))
        cur = conn.execute(
            "INSERT INTO events(creator_id, title, description, location, starts_at, recurring, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?)",
            (e["creator_id"], e["title"], e["description"], e["location"], to_db(nxt), to_db(now)),
        )
        notices += _announce(conn, get(conn, cur.lastrowid), None, silent=True)
    return notices
