"""SQLite storage. All state lives here so a restart loses nothing."""
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id            INTEGER PRIMARY KEY,
    name             TEXT NOT NULL,
    role             TEXT NOT NULL DEFAULT 'student' CHECK (role IN ('student', 'mentor')),
    active           INTEGER NOT NULL DEFAULT 1,
    muted            INTEGER NOT NULL DEFAULT 0,
    paused_until     TEXT,              -- local date (YYYY-MM-DD), rotation skips until then
    washer_ban_until TEXT,              -- UTC timestamp
    rotation_order   INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bookings (
    id                INTEGER PRIMARY KEY,
    user_id           INTEGER NOT NULL REFERENCES users(tg_id),
    slot_start        TEXT NOT NULL,    -- UTC
    slot_date         TEXT NOT NULL,    -- local date
    slot_time         TEXT NOT NULL,    -- local HH:MM
    cycle             TEXT NOT NULL,
    ends_at           TEXT NOT NULL,    -- UTC, slot_start + cycle length
    status            TEXT NOT NULL,    -- booked | active | done | cancelled | noshow
    forced            INTEGER NOT NULL DEFAULT 0,
    recurring_id      INTEGER,
    late_cancel       INTEGER NOT NULL DEFAULT 0,
    remind_sent       INTEGER NOT NULL DEFAULT 0,
    start_prompt_sent INTEGER NOT NULL DEFAULT 0,
    still_prompt_at   TEXT,
    end_prompt_sent   INTEGER NOT NULL DEFAULT 0,
    late_until        TEXT,
    started_at        TEXT,
    finished_at       TEXT,
    cancelled_at      TEXT,
    cancel_reason     TEXT,
    created_at        TEXT NOT NULL
);
-- The database itself guarantees no double bookings, even under concurrent taps.
CREATE UNIQUE INDEX IF NOT EXISTS one_live_booking_per_slot
    ON bookings(slot_start) WHERE status IN ('booked', 'active');
CREATE INDEX IF NOT EXISTS bookings_user ON bookings(user_id, slot_date);

CREATE TABLE IF NOT EXISTS waitlist (
    id          INTEGER PRIMARY KEY,
    slot_start  TEXT NOT NULL,
    user_id     INTEGER NOT NULL REFERENCES users(tg_id),
    status      TEXT NOT NULL,          -- waiting | offered | claimed | expired | cleared | left
    offered_at  TEXT,
    created_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_waitlist_entry
    ON waitlist(slot_start, user_id) WHERE status IN ('waiting', 'offered');

CREATE TABLE IF NOT EXISTS washer_blocks (
    id          INTEGER PRIMARY KEY,
    starts_at   TEXT NOT NULL,
    ends_at     TEXT,                   -- NULL = until a mentor lifts it
    reason      TEXT NOT NULL,
    created_by  INTEGER,
    created_at  TEXT NOT NULL,
    lifted_at   TEXT
);

CREATE TABLE IF NOT EXISTS recurring (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(tg_id),
    weekday     INTEGER NOT NULL,       -- 0=Mon
    slot_time   TEXT NOT NULL,
    cycle       TEXT NOT NULL,
    status      TEXT NOT NULL,          -- pending | approved | rejected | ended
    decided_by  INTEGER,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    id          INTEGER PRIMARY KEY,
    area        TEXT NOT NULL,
    reporter_id INTEGER,                -- NULL when the reporter chose to stay anonymous
    description TEXT NOT NULL,
    photo_id    TEXT,
    status      TEXT NOT NULL DEFAULT 'open',   -- open | in_progress | fixed
    updated_by  INTEGER,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Anonymous reports: deliberately NO user column of any kind.
CREATE TABLE IF NOT EXISTS anon_reports (
    id          INTEGER PRIMARY KEY,
    category    TEXT NOT NULL,
    description TEXT NOT NULL,
    photo_id    TEXT,
    token_hash  TEXT,                   -- sha256 of the reply token, only if reporter opted in
    status      TEXT NOT NULL DEFAULT 'open',
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS anon_messages (
    id          INTEGER PRIMARY KEY,
    report_id   INTEGER NOT NULL REFERENCES anon_reports(id),
    sender      TEXT NOT NULL,          -- 'mentor' | 'reporter'
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS duty (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,          -- cleaning | buyer
    week_start  TEXT NOT NULL,          -- local Monday
    zone        TEXT NOT NULL DEFAULT '',
    user_id     INTEGER NOT NULL REFERENCES users(tg_id),
    due_date    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'assigned',   -- assigned | done | missed
    reminded    INTEGER NOT NULL DEFAULT 0,
    done_at     TEXT,
    receipt_id  TEXT,
    changed_by  INTEGER,                -- mentor who reassigned it, if any
    created_at  TEXT NOT NULL,
    UNIQUE (kind, week_start, zone)
);

CREATE TABLE IF NOT EXISTS supplies (
    id          INTEGER PRIMARY KEY,
    item        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'needed',     -- needed | bought
    flagged_by  INTEGER,
    flagged_at  TEXT NOT NULL,
    bought_by   INTEGER,
    bought_at   TEXT
);

CREATE TABLE IF NOT EXISTS announcements (
    id          INTEGER PRIMARY KEY,
    author_id   INTEGER NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    photo_id    TEXT,
    target      TEXT NOT NULL,          -- all | students | mentors
    pin         INTEGER NOT NULL DEFAULT 0,
    silent      INTEGER NOT NULL DEFAULT 1,
    send_at     TEXT NOT NULL,
    sent_at     TEXT,
    cancelled   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS announcement_msgs (
    ann_id      INTEGER NOT NULL REFERENCES announcements(id),
    chat_id     INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    has_photo   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS guests (
    id          INTEGER PRIMARY KEY,
    host_id     INTEGER NOT NULL REFERENCES users(tg_id),
    name        TEXT NOT NULL,
    arrive_at   TEXT NOT NULL,
    depart_at   TEXT NOT NULL,
    overnight   INTEGER NOT NULL,
    status      TEXT NOT NULL,          -- pending | approved | rejected | checked_out | expired
    decided_by  INTEGER,
    reason      TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY,
    creator_id    INTEGER NOT NULL,
    title         TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    location      TEXT NOT NULL DEFAULT '',
    starts_at     TEXT NOT NULL,
    recurring     INTEGER NOT NULL DEFAULT 0,
    regenerated   INTEGER NOT NULL DEFAULT 0,
    reminder_sent INTEGER NOT NULL DEFAULT 0,
    deleted       INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rsvps (
    event_id    INTEGER NOT NULL REFERENCES events(id),
    user_id     INTEGER NOT NULL REFERENCES users(tg_id),
    status      TEXT NOT NULL,          -- going | maybe | cant
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (event_id, user_id)
);

CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY,
    actor_id    INTEGER NOT NULL,
    action      TEXT NOT NULL,
    details     TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_runs (
    name        TEXT NOT NULL,
    period_key  TEXT NOT NULL,
    ran_at      TEXT NOT NULL,
    PRIMARY KEY (name, period_key)
);

CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def tx(conn: sqlite3.Connection):
    """Write transaction. Re-entrant: nested calls join the outer transaction."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def get_setting(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def claim_job_run(conn, name: str, period_key: str, ran_at: str) -> bool:
    """Return True the first time (name, period_key) is seen; used for catch-up jobs."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO job_runs(name, period_key, ran_at) VALUES (?, ?, ?)",
        (name, period_key, ran_at),
    )
    return cur.rowcount == 1
