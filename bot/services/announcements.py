"""Announcements: immediate or scheduled, targeted, optionally pinned, silent or loud."""
from datetime import datetime

from ..config import CONFIG
from ..db import claim_job_run, tx
from ..notices import Notice
from ..timeutil import from_db, fmt_dt, local, to_db
from . import audit, users

TARGETS = {"all": "Барлығына", "students": "Студенттерге", "mentors": "Тәлімгерлерге"}


def text_of(a) -> str:
    return f"📢 {a['title']}\n\n{a['body']}"


def create(conn, author: int, title: str, body: str, photo_id: str | None, target: str, pin: bool,
           silent: bool, send_at: datetime, now: datetime) -> int:
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO announcements(author_id, title, body, photo_id, target, pin, silent, send_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (author, title, body, photo_id, target, int(pin), int(silent), to_db(max(send_at, now)), to_db(now)),
        )
        when = "қазір" if send_at <= now else fmt_dt(send_at)
        audit.log(conn, author, "announce", f"№{cur.lastrowid} «{title}» → {TARGETS[target]}, {when}")
        return cur.lastrowid


def recipients(conn, target: str):
    if target == "students":
        return users.students(conn)
    if target == "mentors":
        return users.mentors(conn)
    return users.everyone(conn)


def build_notices(conn, a) -> list[Notice]:
    notices = [
        Notice(u["tg_id"], text_of(a), silent=bool(a["silent"]), photo=a["photo_id"], pin=bool(a["pin"]), ann_id=a["id"])
        for u in recipients(conn, a["target"])
    ]
    if a["target"] == "all" and CONFIG.group_chat_id:
        notices.append(Notice(CONFIG.group_chat_id, text_of(a), silent=bool(a["silent"]), photo=a["photo_id"],
                              pin=bool(a["pin"]), ann_id=a["id"]))
    return notices


def tick(conn, now: datetime) -> list[Notice]:
    notices = []
    for a in conn.execute(
        "SELECT * FROM announcements WHERE sent_at IS NULL AND cancelled = 0 AND send_at <= ? ORDER BY send_at",
        (to_db(now),),
    ).fetchall():
        conn.execute("UPDATE announcements SET sent_at = ? WHERE id = ?", (to_db(now), a["id"]))
        notices += build_notices(conn, a)
    loc = local(now)
    # Quiet-hours reminder at 22:00 (only if still within that hour, so a late restart doesn't spam at 3am)
    if CONFIG.quiet_hours_reminder and loc.hour == 22 and claim_job_run(conn, "quiet_hours", loc.date().isoformat(), to_db(now)):
        notices += [Notice(u["tg_id"], CONFIG.quiet_hours_text) for u in users.everyone(conn)]
    return notices


def recent(conn, limit: int = 5):
    return conn.execute(
        "SELECT * FROM announcements WHERE sent_at IS NOT NULL AND cancelled = 0 ORDER BY sent_at DESC LIMIT ?",
        (limit,),
    ).fetchall()


def visible_to(a, user) -> bool:
    return a["target"] == "all" or (a["target"] == "students") == (user["role"] == "student")


def scheduled(conn):
    return conn.execute(
        "SELECT * FROM announcements WHERE sent_at IS NULL AND cancelled = 0 ORDER BY send_at"
    ).fetchall()


def get(conn, ann_id: int):
    return conn.execute("SELECT * FROM announcements WHERE id = ?", (ann_id,)).fetchone()


def cancel(conn, actor: int, ann_id: int) -> None:
    with tx(conn):
        conn.execute("UPDATE announcements SET cancelled = 1 WHERE id = ? AND sent_at IS NULL", (ann_id,))
        audit.log(conn, actor, "announce_cancel", f"#{ann_id}")


def record_message(conn, ann_id: int, chat_id: int, message_id: int, has_photo: bool) -> None:
    conn.execute(
        "INSERT INTO announcement_msgs(ann_id, chat_id, message_id, has_photo) VALUES (?, ?, ?, ?)",
        (ann_id, chat_id, message_id, int(has_photo)),
    )


def messages(conn, ann_id: int):
    return conn.execute("SELECT * FROM announcement_msgs WHERE ann_id = ?", (ann_id,)).fetchall()


def update_body(conn, actor: int, ann_id: int, body: str) -> None:
    with tx(conn):
        conn.execute("UPDATE announcements SET body = ? WHERE id = ?", (body, ann_id))
        audit.log(conn, actor, "announce_edit", f"#{ann_id}")


def set_pinned(conn, actor: int, ann_id: int) -> None:
    with tx(conn):
        conn.execute("UPDATE announcements SET pin = 1 WHERE id = ?", (ann_id,))
        audit.log(conn, actor, "announce_pin", f"#{ann_id}")


def label(a) -> str:
    when = fmt_dt(from_db(a["sent_at"] or a["send_at"]))
    return f"{when} · {a['title']} → {TARGETS[a['target']]}"
