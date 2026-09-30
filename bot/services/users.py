"""Users, roles, activation, pausing, muting."""
from datetime import datetime

from ..config import CONFIG
from ..db import tx
from ..notices import Notice, kb
from ..timeutil import to_db
from . import audit


def get(conn, tg_id: int):
    return conn.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)).fetchone()


def is_mentor(user) -> bool:
    return bool(user) and user["role"] == "mentor"


def register(conn, tg_id: int, name: str, role: str, now: datetime) -> None:
    with tx(conn):
        order = conn.execute("SELECT COALESCE(MAX(rotation_order), 0) + 1 FROM users").fetchone()[0]
        conn.execute(
            "INSERT INTO users(tg_id, name, role, rotation_order, created_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(tg_id) DO UPDATE SET name = excluded.name, role = excluded.role, active = 1",
            (tg_id, name.strip()[:64], role, order, to_db(now)),
        )


def role_for_new_user(tg_id: int, requested: str) -> str:
    """Mentors are pre-approved by the MENTOR_IDS whitelist; everyone else is a student."""
    return "mentor" if tg_id in CONFIG.mentor_ids else "student"


def mentors(conn):
    return conn.execute(
        "SELECT * FROM users WHERE role = 'mentor' AND active = 1 ORDER BY name"
    ).fetchall()


def students(conn, active_only: bool = True):
    q = "SELECT * FROM users WHERE role = 'student'"
    if active_only:
        q += " AND active = 1"
    return conn.execute(q + " ORDER BY rotation_order").fetchall()


def everyone(conn):
    return conn.execute("SELECT * FROM users WHERE active = 1 ORDER BY name").fetchall()


def all_users(conn):
    return conn.execute("SELECT * FROM users ORDER BY active DESC, role DESC, name").fetchall()


def notify_mentors(conn, text: str, markup=None, silent: bool = False, photo=None) -> list[Notice]:
    return [Notice(m["tg_id"], text, markup, silent=silent, photo=photo) for m in mentors(conn)]


def name_of(conn, tg_id: int | None) -> str:
    if tg_id is None:
        return "—"
    row = get(conn, tg_id)
    return row["name"] if row else str(tg_id)


def set_role(conn, actor: int, tg_id: int, role: str) -> None:
    with tx(conn):
        conn.execute("UPDATE users SET role = ? WHERE tg_id = ?", (role, tg_id))
        audit.log(conn, actor, "set_role", f"{name_of(conn, tg_id)} -> {role}")


def set_muted(conn, actor: int, tg_id: int, muted: bool) -> None:
    with tx(conn):
        conn.execute("UPDATE users SET muted = ? WHERE tg_id = ?", (int(muted), tg_id))
        audit.log(conn, actor, "mute" if muted else "unmute", name_of(conn, tg_id))


def set_paused(conn, actor: int, tg_id: int, until: str | None) -> None:
    with tx(conn):
        conn.execute("UPDATE users SET paused_until = ? WHERE tg_id = ?", (until, tg_id))
        audit.log(conn, actor, "pause" if until else "unpause", f"{name_of(conn, tg_id)} until {until}")


def deactivate(conn, actor: int, tg_id: int, now: datetime) -> list[Notice]:
    """Student leaves: rotation skips them, recurring freed, future bookings cancelled."""
    from . import washer  # local import avoids a cycle

    notices: list[Notice] = []
    with tx(conn):
        conn.execute("UPDATE users SET active = 0 WHERE tg_id = ?", (tg_id,))
        conn.execute(
            "UPDATE recurring SET status = 'ended' WHERE user_id = ? AND status IN ('pending', 'approved')",
            (tg_id,),
        )
        conn.execute(
            "UPDATE waitlist SET status = 'left' WHERE user_id = ? AND status IN ('waiting', 'offered')",
            (tg_id,),
        )
        future = conn.execute(
            "SELECT * FROM bookings WHERE user_id = ? AND status IN ('booked', 'active') AND ends_at > ?",
            (tg_id, to_db(now)),
        ).fetchall()
        for b in future:
            notices += washer.cancel_booking(conn, b["id"], now, reason="account deactivated", notify_owner=False)
        audit.log(conn, actor, "deactivate", name_of(conn, tg_id))
    return notices


def activate(conn, actor: int, tg_id: int) -> None:
    with tx(conn):
        conn.execute("UPDATE users SET active = 1 WHERE tg_id = ?", (tg_id,))
        audit.log(conn, actor, "activate", name_of(conn, tg_id))


def new_user_notice(conn, tg_id: int, name: str, role: str) -> list[Notice]:
    return notify_mentors(
        conn,
        f"👋 New {role} registered: {name} (id {tg_id}).",
        kb([("Manage user", f"a:uv:{tg_id}")]),
        silent=True,
    )
