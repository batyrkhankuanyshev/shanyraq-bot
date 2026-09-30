"""Washing machine: booking, waitlist, live cycle, no-shows, maintenance, recurring."""
import sqlite3
from datetime import date, datetime, timedelta

from ..db import claim_job_run, tx
from ..notices import Notice, kb
from ..timeutil import (
    at_local, day_bounds, fmt_date, fmt_dt, fmt_time, from_db, local, local_today, to_db, week_start,
)
from . import audit, users

SLOT_TIMES = ["08:00", "09:30", "11:00", "12:30", "14:00", "15:30", "17:00", "18:30", "20:00", "21:30"]
CYCLES = {"quick": ("Quick", 60), "normal": ("Normal", 90), "heavy": ("Heavy", 120)}
WEEKLY_LIMIT = 2
DAYS_AHEAD = 3
REMIND_BEFORE = timedelta(minutes=15)
START_GRACE = timedelta(minutes=15)      # wait this long after slot start before asking "still using it?"
STILL_REPLY = timedelta(minutes=10)      # then this long for an answer before marking a no-show
LATE_GRACE = timedelta(minutes=15)
CLAIM_WINDOW = timedelta(minutes=5)
OFFER_CUTOFF = timedelta(minutes=45)     # a freed slot is not offered once it is this far underway
LATE_CANCEL = timedelta(minutes=30)      # cancels closer than this are flagged for abuse review
NOSHOW_LIMIT = 3
BAN_DAYS = 7
RECURRING_CAP = 4
LIVE = ("booked", "active")
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class BookingError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def cycle_label(cycle: str) -> str:
    name, minutes = CYCLES[cycle]
    return f"{name} ({minutes}m)"


def summary(b) -> str:
    start = from_db(b["slot_start"])
    return (
        f"{fmt_date(local(start).date())} {b['slot_time']} · {cycle_label(b['cycle'])} "
        f"· until {fmt_time(from_db(b['ends_at']))}"
    )


def bookable_days(now: datetime) -> list[date]:
    today = local_today(now)
    return [today + timedelta(days=i) for i in range(DAYS_AHEAD + 1)]


def get_booking(conn, booking_id: int):
    return conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()


def live_booking_at(conn, slot_start: str):
    return conn.execute(
        "SELECT * FROM bookings WHERE slot_start = ? AND status IN ('booked', 'active')", (slot_start,)
    ).fetchone()


def active_block(conn, now: datetime):
    n = to_db(now)
    return conn.execute(
        "SELECT * FROM washer_blocks WHERE lifted_at IS NULL AND starts_at <= ? "
        "AND (ends_at IS NULL OR ends_at > ?) ORDER BY id DESC LIMIT 1",
        (n, n),
    ).fetchone()


def block_for(conn, slot_start: datetime):
    """A maintenance block overlapping the slot window, if any."""
    window_end = to_db(slot_start + timedelta(minutes=90))
    return conn.execute(
        "SELECT * FROM washer_blocks WHERE lifted_at IS NULL AND starts_at < ? "
        "AND (ends_at IS NULL OR ends_at > ?) ORDER BY id DESC LIMIT 1",
        (window_end, to_db(slot_start)),
    ).fetchone()


def _next_slot_start(slot_start: datetime) -> datetime | None:
    loc = local(slot_start)
    hhmm = loc.strftime("%H:%M")
    if hhmm not in SLOT_TIMES or hhmm == SLOT_TIMES[-1]:
        return None
    return at_local(loc.date(), SLOT_TIMES[SLOT_TIMES.index(hhmm) + 1])


def _prev_slot_start(slot_start: datetime) -> datetime | None:
    loc = local(slot_start)
    hhmm = loc.strftime("%H:%M")
    if hhmm not in SLOT_TIMES or hhmm == SLOT_TIMES[0]:
        return None
    return at_local(loc.date(), SLOT_TIMES[SLOT_TIMES.index(hhmm) - 1])


def slots_for_day(conn, d: date, now: datetime, uid: int) -> list[dict]:
    """Future slots of a day with their state: free | mine | taken | blocked."""
    result = []
    for hhmm in SLOT_TIMES:
        start = at_local(d, hhmm)
        if start <= now:
            continue  # past slots are hidden
        booking = live_booking_at(conn, to_db(start))
        if block_for(conn, start):
            state = "blocked"
        elif booking is None:
            state = "free"
        elif booking["user_id"] == uid:
            state = "mine"
        else:
            state = "taken"
        result.append({"time": hhmm, "start": start, "state": state, "booking": booking})
    return result


def weekly_count(conn, uid: int, d: date) -> int:
    ws = week_start(d)
    return conn.execute(
        "SELECT COUNT(*) FROM bookings WHERE user_id = ? AND slot_date BETWEEN ? AND ? "
        "AND status IN ('booked', 'active', 'done', 'noshow')",
        (uid, ws.isoformat(), (ws + timedelta(days=6)).isoformat()),
    ).fetchone()[0]


def noshows_this_month(conn, uid: int, now: datetime) -> int:
    prefix = local_today(now).strftime("%Y-%m")
    return conn.execute(
        "SELECT COUNT(*) FROM bookings WHERE user_id = ? AND status = 'noshow' AND slot_date LIKE ?",
        (uid, prefix + "%"),
    ).fetchone()[0]


def ban_until(user, now: datetime) -> datetime | None:
    if user and user["washer_ban_until"]:
        until = from_db(user["washer_ban_until"])
        if until > now:
            return until
    return None


# ---------------------------------------------------------------- booking

def book(
    conn,
    uid: int,
    d: date,
    hhmm: str,
    cycle: str,
    now: datetime,
    *,
    check_rules: bool = True,
    check_block: bool = True,
    as_active: bool = False,
    forced: bool = False,
    recurring_id: int | None = None,
):
    """Create a booking. The partial UNIQUE index makes double-booking impossible:
    if two people tap the same slot at the same moment, the second INSERT fails."""
    if hhmm not in SLOT_TIMES or cycle not in CYCLES:
        raise BookingError("invalid", "That slot doesn't exist.")
    start = at_local(d, hhmm)
    ends = start + timedelta(minutes=CYCLES[cycle][1])
    try:
        with tx(conn):
            user = users.get(conn, uid)
            if not user or not user["active"]:
                raise BookingError("inactive", "Your account isn't active. Please talk to a mentor.")
            if check_rules:
                if start <= now and not as_active:
                    raise BookingError("past", "That slot has already started.")
                if d > local_today(now) + timedelta(days=DAYS_AHEAD):
                    raise BookingError("too_far", f"You can book at most {DAYS_AHEAD} days ahead.")
                banned = ban_until(user, now)
                if banned:
                    raise BookingError(
                        "banned", f"You can't book until {fmt_dt(banned)} (too many no-shows). "
                        "A mentor can lift this."
                    )
                if user["role"] == "student" and weekly_count(conn, uid, d) >= WEEKLY_LIMIT:
                    raise BookingError(
                        "limit", f"You already have {WEEKLY_LIMIT} bookings that week — that's the fair-use limit."
                    )
                if cycle == "heavy":
                    nxt = _next_slot_start(start)
                    if nxt and live_booking_at(conn, to_db(nxt)):
                        raise BookingError(
                            "heavy_conflict",
                            "Heavy takes 120 min and the next slot is already booked. Please pick Quick or Normal.",
                        )
            if check_block:
                block = block_for(conn, start)
                if block:
                    raise BookingError("blocked", f"The washer is unavailable: {block['reason']}")
            remind_sent = int(start - REMIND_BEFORE <= now)
            started = as_active or start <= now
            cur = conn.execute(
                "INSERT INTO bookings(user_id, slot_start, slot_date, slot_time, cycle, ends_at, status, "
                "forced, recurring_id, remind_sent, start_prompt_sent, started_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    uid, to_db(start), d.isoformat(), hhmm, cycle, to_db(ends),
                    "active" if as_active else "booked", int(forced), recurring_id, remind_sent,
                    int(started), to_db(now) if as_active else None, to_db(now),
                ),
            )
            conn.execute(
                "UPDATE waitlist SET status = 'claimed' WHERE slot_start = ? AND user_id = ? "
                "AND status IN ('waiting', 'offered')",
                (to_db(start), uid),
            )
            return get_booking(conn, cur.lastrowid)
    except sqlite3.IntegrityError:
        raise BookingError("taken", "Just taken — join the waitlist?") from None


def heavy_warning(conn, b) -> str:
    """Heads-up if the previous slot is a Heavy wash that runs into this one."""
    prev = _prev_slot_start(from_db(b["slot_start"]))
    if prev:
        pb = live_booking_at(conn, to_db(prev))
        if pb and pb["cycle"] == "heavy":
            return f"\n⚠️ The wash before yours is Heavy and may run until {fmt_time(from_db(pb['ends_at']))}."
    return ""


def upcoming_for_user(conn, uid: int, now: datetime):
    return conn.execute(
        "SELECT * FROM bookings WHERE user_id = ? AND status IN ('booked', 'active') AND ends_at > ? "
        "ORDER BY slot_start",
        (uid, to_db(now)),
    ).fetchall()


def waitlist_for_user(conn, uid: int, now: datetime):
    return conn.execute(
        "SELECT * FROM waitlist WHERE user_id = ? AND status IN ('waiting', 'offered') AND slot_start > ? "
        "ORDER BY slot_start",
        (uid, to_db(now - OFFER_CUTOFF)),
    ).fetchall()


def cancel_booking(
    conn, booking_id: int, now: datetime, *, reason: str | None = None, by_mentor: int | None = None,
    notify_owner: bool = True, offer: bool = True,
) -> list[Notice]:
    notices: list[Notice] = []
    with tx(conn):
        b = get_booking(conn, booking_id)
        if not b or b["status"] not in LIVE:
            return []
        late = by_mentor is None and reason is None and from_db(b["slot_start"]) - now < LATE_CANCEL
        conn.execute(
            "UPDATE bookings SET status = 'cancelled', cancelled_at = ?, cancel_reason = ?, late_cancel = ? "
            "WHERE id = ?",
            (to_db(now), reason, int(late), booking_id),
        )
        if by_mentor:
            audit.log(conn, by_mentor, "washer_force_free", f"{users.name_of(conn, b['user_id'])} {summary(b)}")
        if notify_owner and reason:
            notices.append(Notice(b["user_id"], f"❌ Your washer booking was cancelled: {summary(b)}\nReason: {reason}", silent=False))
        if offer:
            notices += offer_next(conn, b["slot_start"], now)
    return notices


def user_cancel(conn, booking_id: int, uid: int, now: datetime) -> list[Notice]:
    b = get_booking(conn, booking_id)
    if not b or b["user_id"] != uid:
        raise BookingError("not_yours", "That booking isn't yours.")
    if b["status"] not in LIVE:
        raise BookingError("gone", "That booking is no longer active.")
    return cancel_booking(conn, booking_id, now)


def start_booking(conn, booking_id: int, uid: int, now: datetime) -> str:
    with tx(conn):
        b = get_booking(conn, booking_id)
        if not b or b["user_id"] != uid:
            raise BookingError("not_yours", "That booking isn't yours.")
        if b["status"] == "active":
            return "Already marked as started. Enjoy! 🧺"
        if b["status"] != "booked":
            raise BookingError("gone", "That booking is no longer active.")
        if now < from_db(b["slot_start"]) - REMIND_BEFORE:
            raise BookingError("early", "It's too early — you can tap Started from 15 minutes before your slot.")
        conn.execute(
            "UPDATE bookings SET status = 'active', started_at = ?, start_prompt_sent = 1 WHERE id = ?",
            (to_db(now), booking_id),
        )
    return "✅ Started! I'll check in when your cycle should be done."


def _notify_next(conn, slot_start: datetime, text: str) -> list[Notice]:
    """Tell the next slot's holder and its waitlist something (early free / delay)."""
    notices = []
    nxt = _next_slot_start(slot_start)
    if nxt:
        nb = live_booking_at(conn, to_db(nxt))
        if nb:
            notices.append(Notice(nb["user_id"], text))
        for w in conn.execute(
            "SELECT * FROM waitlist WHERE slot_start = ? AND status IN ('waiting', 'offered')", (to_db(nxt),)
        ):
            notices.append(Notice(w["user_id"], text))
    return notices


def finish_booking(conn, booking_id: int, uid: int, now: datetime) -> tuple[str, list[Notice]]:
    notices: list[Notice] = []
    with tx(conn):
        b = get_booking(conn, booking_id)
        if not b or b["user_id"] != uid:
            raise BookingError("not_yours", "That booking isn't yours.")
        if b["status"] == "done":
            return "Thanks! 🙌", []
        if b["status"] not in LIVE:
            raise BookingError("gone", "That booking is no longer active.")
        conn.execute(
            "UPDATE bookings SET status = 'done', finished_at = ?, end_prompt_sent = 1 WHERE id = ?",
            (to_db(now), booking_id),
        )
        start = from_db(b["slot_start"])
        nxt = _next_slot_start(start)
        if nxt and now < nxt:
            msg = f"🧺 The washer is free early (since {fmt_time(now)}) — you can start whenever you're ready."
            notices += _notify_next(conn, start, msg)
            # people waiting on *this* slot can use the leftover time
            for w in conn.execute(
                "SELECT * FROM waitlist WHERE slot_start = ? AND status IN ('waiting', 'offered')",
                (b["slot_start"],),
            ).fetchall():
                notices.append(Notice(
                    w["user_id"],
                    f"🧺 The washer is free now until {fmt_time(nxt)} if you want a quick wash.",
                ))
                conn.execute("UPDATE waitlist SET status = 'cleared' WHERE id = ?", (w["id"],))
    return "🙌 Thanks for freeing the washer!", notices


def running_late(conn, booking_id: int, uid: int, now: datetime) -> tuple[str, list[Notice]]:
    with tx(conn):
        b = get_booking(conn, booking_id)
        if not b or b["user_id"] != uid:
            raise BookingError("not_yours", "That booking isn't yours.")
        ends = from_db(b["ends_at"])
        if b["status"] not in ("active", "done") or now >= ends + LATE_GRACE or b["late_until"]:
            raise BookingError("gone", "Too late to extend this one.")
        late_until = ends + LATE_GRACE
        conn.execute(
            "UPDATE bookings SET status = 'active', finished_at = NULL, late_until = ? WHERE id = ?",
            (to_db(late_until), booking_id),
        )
        notices = _notify_next(
            conn, from_db(b["slot_start"]),
            f"⏳ Heads-up: the wash before yours is running late — the washer should be free by {fmt_time(late_until)}.",
        )
    return f"⏳ OK, you have until {fmt_time(late_until)}. The next person has been told.", notices


# ---------------------------------------------------------------- waitlist

def join_waitlist(conn, uid: int, slot_start: datetime, now: datetime) -> str:
    key = to_db(slot_start)
    with tx(conn):
        b = live_booking_at(conn, key)
        if b is None:
            return "free"
        if b["user_id"] == uid:
            raise BookingError("mine", "That's your own booking.")
        if block_for(conn, slot_start):
            raise BookingError("blocked", "The washer is unavailable then.")
        try:
            conn.execute(
                "INSERT INTO waitlist(slot_start, user_id, status, created_at) VALUES (?, ?, 'waiting', ?)",
                (key, uid, to_db(now)),
            )
        except sqlite3.IntegrityError:
            raise BookingError("already", "You're already on the waitlist for that slot.") from None
        pos = conn.execute(
            "SELECT COUNT(*) FROM waitlist WHERE slot_start = ? AND status IN ('waiting', 'offered')", (key,)
        ).fetchone()[0]
    return f"📝 You're #{pos} on the waitlist for {fmt_dt(slot_start)}. I'll DM you if it frees up."


def leave_waitlist(conn, wl_id: int, uid: int, now: datetime) -> list[Notice]:
    with tx(conn):
        w = conn.execute("SELECT * FROM waitlist WHERE id = ?", (wl_id,)).fetchone()
        if not w or w["user_id"] != uid or w["status"] not in ("waiting", "offered"):
            return []
        conn.execute("UPDATE waitlist SET status = 'left' WHERE id = ?", (wl_id,))
        return offer_next(conn, w["slot_start"], now) if w["status"] == "offered" else []


def offer_next(conn, slot_start: str, now: datetime) -> list[Notice]:
    """Give the first waiting person a 5-minute window to claim a freed slot."""
    start = from_db(slot_start)
    with tx(conn):
        if live_booking_at(conn, slot_start):
            return []
        if conn.execute(
            "SELECT 1 FROM waitlist WHERE slot_start = ? AND status = 'offered'", (slot_start,)
        ).fetchone():
            return []
        if now >= start + OFFER_CUTOFF or block_for(conn, start):
            conn.execute(
                "UPDATE waitlist SET status = 'cleared' WHERE slot_start = ? AND status = 'waiting'", (slot_start,)
            )
            return []
        w = conn.execute(
            "SELECT * FROM waitlist WHERE slot_start = ? AND status = 'waiting' ORDER BY id LIMIT 1", (slot_start,)
        ).fetchone()
        if not w:
            return []
        conn.execute("UPDATE waitlist SET status = 'offered', offered_at = ? WHERE id = ?", (to_db(now), w["id"]))
    deadline = now + CLAIM_WINDOW
    return [Notice(
        w["user_id"],
        f"🎉 A washer slot freed up: {fmt_dt(start)}.\nClaim it before {fmt_time(deadline)} (5 min) — pick your cycle:",
        kb(
            [("Quick 60m", f"w:cl:{w['id']}:quick"), ("Normal 90m", f"w:cl:{w['id']}:normal")],
            [("Heavy 120m", f"w:cl:{w['id']}:heavy"), ("No thanks", f"w:wlx:{w['id']}")],
        ),
        silent=False,
    )]


def claim(conn, wl_id: int, uid: int, cycle: str, now: datetime):
    w = conn.execute("SELECT * FROM waitlist WHERE id = ?", (wl_id,)).fetchone()
    if not w or w["user_id"] != uid:
        raise BookingError("not_yours", "That offer isn't yours.")
    if w["status"] != "offered" or from_db(w["offered_at"]) + CLAIM_WINDOW <= now:
        raise BookingError("expired", "Sorry, the 5-minute claim window has passed.")
    start = from_db(w["slot_start"])
    loc = local(start)
    return book(conn, uid, loc.date(), loc.strftime("%H:%M"), cycle, now, as_active=start <= now)


# ---------------------------------------------------------------- maintenance & overrides

def start_maintenance(conn, actor: int, reason: str, now: datetime, until: datetime | None = None,
                      apology: str = "Sorry for the trouble 🙏") -> list[Notice]:
    notices: list[Notice] = []
    with tx(conn):
        conn.execute(
            "INSERT INTO washer_blocks(starts_at, ends_at, reason, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
            (to_db(now), to_db(until) if until else None, reason, actor, to_db(now)),
        )
        q = "SELECT * FROM bookings WHERE status IN ('booked', 'active') AND ends_at > ?"
        args: list = [to_db(now)]
        if until:
            q += " AND slot_start < ?"
            args.append(to_db(until))
        for b in conn.execute(q, args).fetchall():
            if b["status"] == "active" and from_db(b["slot_start"]) <= now:
                continue  # a wash already running is left alone
            notices += cancel_booking(conn, b["id"], now, reason=f"washer unavailable — {reason}. {apology}", offer=False)
        wq = "SELECT * FROM waitlist WHERE status IN ('waiting', 'offered') AND slot_start > ?"
        wargs: list = [to_db(now - OFFER_CUTOFF)]
        if until:
            wq += " AND slot_start < ?"
            wargs.append(to_db(until))
        for w in conn.execute(wq, wargs).fetchall():
            conn.execute("UPDATE waitlist SET status = 'cleared' WHERE id = ?", (w["id"],))
            notices.append(Notice(
                w["user_id"],
                f"The waitlist for {fmt_dt(from_db(w['slot_start']))} was cleared: washer unavailable — {reason}.",
            ))
        audit.log(conn, actor, "washer_maintenance_on", reason)
    return notices


def lift_maintenance(conn, actor: int, now: datetime) -> list[Notice]:
    with tx(conn):
        cur = conn.execute(
            "UPDATE washer_blocks SET lifted_at = ? WHERE lifted_at IS NULL AND (ends_at IS NULL OR ends_at > ?)",
            (to_db(now), to_db(now)),
        )
        if cur.rowcount == 0:
            return []
        audit.log(conn, actor, "washer_maintenance_off")
        return [Notice(u["tg_id"], "✅ The washer is working again — booking is open.") for u in users.everyone(conn)]


def report_breakdown(conn, uid: int, booking_id: int | None, now: datetime) -> list[Notice]:
    """Washer broke mid-cycle: mark slot, create report, block rest of today, cancel today's bookings."""
    from . import reports

    notices: list[Notice] = []
    with tx(conn):
        if booking_id:
            b = get_booking(conn, booking_id)
            if b and b["user_id"] == uid and b["status"] in LIVE:
                conn.execute(
                    "UPDATE bookings SET status = 'done', finished_at = ?, cancel_reason = 'breakdown' WHERE id = ?",
                    (to_db(now), booking_id),
                )
        _, report_notices = reports.create_maintenance(
            conn, uid, "Washer", "Washer broke down during a cycle (reported from the washer menu).", None, now,
            anonymous=False,
        )
        notices += report_notices
        _, day_end = day_bounds(local_today(now))
        notices += start_maintenance(
            conn, uid, "washer broke down, mentors are on it", now, until=day_end,
            apology="Sorry for the inconvenience 🙏",
        )
    return notices


def force_free(conn, actor: int, booking_id: int, now: datetime) -> list[Notice]:
    return cancel_booking(conn, booking_id, now, reason="freed by a mentor", by_mentor=actor)


def force_book(conn, actor: int, target: int, d: date, hhmm: str, cycle: str, now: datetime) -> list[Notice]:
    with tx(conn):
        b = book(conn, target, d, hhmm, cycle, now, check_rules=False, check_block=False, forced=True)
        audit.log(conn, actor, "washer_force_book", f"{users.name_of(conn, target)} {summary(b)}")
    return [Notice(target, f"🧺 A mentor booked the washer for you: {summary(b)}")]


def set_ban(conn, actor: int, uid: int, now: datetime, days: int | None) -> list[Notice]:
    with tx(conn):
        until = to_db(now + timedelta(days=days)) if days else None
        conn.execute("UPDATE users SET washer_ban_until = ? WHERE tg_id = ?", (until, uid))
        audit.log(conn, actor, "washer_ban" if days else "washer_unban", users.name_of(conn, uid))
    if days:
        return [Notice(uid, f"🚫 A mentor paused your washer bookings for {days} days.", silent=False)]
    return [Notice(uid, "✅ You can book the washer again.")]


# ---------------------------------------------------------------- recurring slots

def request_recurring(conn, uid: int, weekday: int, hhmm: str, cycle: str, now: datetime) -> list[Notice]:
    with tx(conn):
        if conn.execute(
            "SELECT 1 FROM recurring WHERE user_id = ? AND status IN ('pending', 'approved')", (uid,)
        ).fetchone():
            raise BookingError("already", "You already have a recurring slot or a pending request.")
        if conn.execute("SELECT COUNT(*) FROM recurring WHERE status = 'approved'").fetchone()[0] >= RECURRING_CAP:
            raise BookingError("cap", f"All {RECURRING_CAP} recurring places are taken right now.")
        cur = conn.execute(
            "INSERT INTO recurring(user_id, weekday, slot_time, cycle, status, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?)",
            (uid, weekday, hhmm, cycle, to_db(now)),
        )
        rid = cur.lastrowid
        return users.notify_mentors(
            conn,
            f"🔁 {users.name_of(conn, uid)} requests a recurring washer slot: every {WEEKDAYS[weekday]} "
            f"{hhmm} ({cycle_label(cycle)}).",
            kb([("Approve", f"a:recok:{rid}"), ("Reject", f"a:recno:{rid}")]),
            silent=True,
        )


def decide_recurring(conn, actor: int, rid: int, approve: bool, now: datetime) -> list[Notice]:
    with tx(conn):
        r = conn.execute("SELECT * FROM recurring WHERE id = ?", (rid,)).fetchone()
        if not r or r["status"] != "pending":
            raise BookingError("gone", "That request was already handled.")
        if approve and conn.execute(
            "SELECT COUNT(*) FROM recurring WHERE status = 'approved'"
        ).fetchone()[0] >= RECURRING_CAP:
            raise BookingError("cap", f"Already {RECURRING_CAP} recurring slots approved.")
        conn.execute(
            "UPDATE recurring SET status = ?, decided_by = ? WHERE id = ?",
            ("approved" if approve else "rejected", actor, rid),
        )
        audit.log(conn, actor, "recurring_" + ("approve" if approve else "reject"),
                  f"{users.name_of(conn, r['user_id'])} {WEEKDAYS[r['weekday']]} {r['slot_time']}")
        text = (
            f"✅ Your recurring slot (every {WEEKDAYS[r['weekday']]} {r['slot_time']}) was approved. "
            "I'll book it automatically each week."
            if approve else "Your recurring slot request was not approved this time."
        )
        notices = [Notice(r["user_id"], text)]
    if approve:
        notices += materialize_recurring(conn, now)
    return notices


def end_recurring(conn, actor: int, rid: int) -> list[Notice]:
    with tx(conn):
        r = conn.execute("SELECT * FROM recurring WHERE id = ?", (rid,)).fetchone()
        if not r:
            return []
        conn.execute("UPDATE recurring SET status = 'ended' WHERE id = ?", (rid,))
        audit.log(conn, actor, "recurring_end", users.name_of(conn, r["user_id"]))
    return [Notice(r["user_id"], f"Your recurring washer slot (every {WEEKDAYS[r['weekday']]} {r['slot_time']}) has ended.")]


def materialize_recurring(conn, now: datetime) -> list[Notice]:
    notices = []
    for r in conn.execute(
        "SELECT r.* FROM recurring r JOIN users u ON u.tg_id = r.user_id "
        "WHERE r.status = 'approved' AND u.active = 1"
    ).fetchall():
        for d in bookable_days(now):
            if d.weekday() != r["weekday"] or at_local(d, r["slot_time"]) <= now:
                continue
            if not claim_job_run(conn, "recurring", f"{r['id']}:{d.isoformat()}", to_db(now)):
                continue
            try:
                b = book(conn, r["user_id"], d, r["slot_time"], r["cycle"], now, check_rules=False, recurring_id=r["id"])
                notices.append(Notice(r["user_id"], f"🔁 Your recurring washer slot is booked: {summary(b)}"))
            except BookingError as e:
                notices.append(Notice(
                    r["user_id"], f"🔁 Couldn't book your recurring slot on {fmt_date(d)} {r['slot_time']}: {e.message}"
                ))
    return notices


# ---------------------------------------------------------------- background tick

def tick(conn, now: datetime) -> list[Notice]:
    """Runs every minute: reminders, start prompts, no-shows, end prompts, waitlist expiry."""
    notices: list[Notice] = []
    n = to_db(now)

    for b in conn.execute("SELECT * FROM bookings WHERE status = 'booked' ORDER BY slot_start").fetchall():
        start = from_db(b["slot_start"])
        cancel_btn = ("Cancel", f"w:cx:{b['id']}")
        if not b["remind_sent"] and start - REMIND_BEFORE <= now < start:
            conn.execute("UPDATE bookings SET remind_sent = 1 WHERE id = ?", (b["id"],))
            notices.append(Notice(
                b["user_id"], f"🧺 Your washer slot starts in 15 min ({b['slot_time']}).{heavy_warning(conn, b)}",
                kb([("▶️ Started", f"w:st:{b['id']}"), cancel_btn]),
            ))
        elif not b["start_prompt_sent"] and start <= now < start + START_GRACE:
            conn.execute("UPDATE bookings SET start_prompt_sent = 1, remind_sent = 1 WHERE id = ?", (b["id"],))
            notices.append(Notice(
                b["user_id"], f"🧺 Your washer slot has started ({b['slot_time']}). Tap Started once the machine is running.",
                kb([("▶️ Started", f"w:st:{b['id']}"), cancel_btn]),
            ))
        elif b["still_prompt_at"] is None and now >= start + START_GRACE:
            conn.execute("UPDATE bookings SET still_prompt_at = ?, start_prompt_sent = 1 WHERE id = ?", (n, b["id"]))
            notices.append(Notice(
                b["user_id"], "❓ Still using the washer? If I don't hear back in 10 min, the slot goes to the next person.",
                kb([("Yes, I'm using it", f"w:yes:{b['id']}"), cancel_btn]),
                silent=False,
            ))
        elif b["still_prompt_at"] and now >= from_db(b["still_prompt_at"]) + STILL_REPLY:
            notices += _mark_noshow(conn, b, now)

    for b in conn.execute("SELECT * FROM bookings WHERE status = 'active'").fetchall():
        ends = from_db(b["ends_at"])
        if b["late_until"]:
            if now >= from_db(b["late_until"]):
                conn.execute("UPDATE bookings SET status = 'done', finished_at = ? WHERE id = ?", (b["late_until"], b["id"]))
        elif now >= ends:
            # Slot auto-expires at its scheduled end; buttons still work for a late answer.
            conn.execute(
                "UPDATE bookings SET status = 'done', finished_at = ?, end_prompt_sent = 1 WHERE id = ?",
                (b["ends_at"], b["id"]),
            )
            if not b["end_prompt_sent"]:
                notices.append(Notice(
                    b["user_id"], "⏰ Your washer time is up. Please collect your laundry!",
                    kb([("✅ Done", f"w:done:{b['id']}"), ("⏳ Running late (+15m)", f"w:late:{b['id']}")]),
                ))

    # Expired claim windows -> next person in line
    for w in conn.execute(
        "SELECT * FROM waitlist WHERE status = 'offered' AND offered_at <= ?", (to_db(now - CLAIM_WINDOW),)
    ).fetchall():
        conn.execute("UPDATE waitlist SET status = 'expired' WHERE id = ?", (w["id"],))
        notices.append(Notice(w["user_id"], "⌛ The claim window for the freed washer slot has passed."))
        notices += offer_next(conn, w["slot_start"], now)

    # Waitlists for slots long gone
    conn.execute(
        "UPDATE waitlist SET status = 'expired' WHERE status = 'waiting' AND slot_start <= ?",
        (to_db(now - OFFER_CUTOFF),),
    )
    notices += materialize_recurring(conn, now)
    return notices


def _mark_noshow(conn, b, now: datetime) -> list[Notice]:
    notices: list[Notice] = []
    with tx(conn):
        conn.execute("UPDATE bookings SET status = 'noshow', finished_at = ? WHERE id = ?", (to_db(now), b["id"]))
        count = noshows_this_month(conn, b["user_id"], now)
        text = f"🚫 Your washer slot ({b['slot_time']}) was marked as a no-show and given away. ({count} this month)"
        if count >= NOSHOW_LIMIT:
            user = users.get(conn, b["user_id"])
            if not ban_until(user, now):
                until = now + timedelta(days=BAN_DAYS)
                conn.execute("UPDATE users SET washer_ban_until = ? WHERE tg_id = ?", (to_db(until), b["user_id"]))
                text += f"\nThat's {NOSHOW_LIMIT} this month, so booking is paused until {fmt_dt(until)}. A mentor can lift this."
                notices += users.notify_mentors(
                    conn,
                    f"⚠️ {user['name']} has {count} washer no-shows this month and is banned from booking for {BAN_DAYS} days.",
                    kb([("Lift ban", f"a:uunban:{b['user_id']}")]),
                )
        notices.insert(0, Notice(b["user_id"], text, silent=False))
        notices += offer_next(conn, b["slot_start"], now)
    return notices


# ---------------------------------------------------------------- stats

def stats_text(conn, now: datetime, days: int = 30) -> str:
    since = (local_today(now) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT status, COUNT(*) c FROM bookings WHERE slot_date >= ? GROUP BY status", (since,)
    ).fetchall()
    by_status = {r["status"]: r["c"] for r in rows}
    total = sum(by_status.values())
    lines = [f"🧺 Washer — last {days} days", f"Bookings: {total} " + " · ".join(f"{k} {v}" for k, v in sorted(by_status.items()))]
    peak = conn.execute(
        "SELECT slot_time, COUNT(*) c FROM bookings WHERE slot_date >= ? AND status IN ('done', 'active', 'booked') "
        "GROUP BY slot_time ORDER BY c DESC LIMIT 3", (since,),
    ).fetchall()
    if peak:
        lines.append("Peak slots: " + ", ".join(f"{r['slot_time']} ({r['c']})" for r in peak))
    top = conn.execute(
        "SELECT u.name, COUNT(*) c FROM bookings b JOIN users u ON u.tg_id = b.user_id "
        "WHERE slot_date >= ? AND b.status IN ('done', 'active', 'booked') GROUP BY b.user_id ORDER BY c DESC LIMIT 5",
        (since,),
    ).fetchall()
    if top:
        lines.append("Top users: " + ", ".join(f"{r['name']} ({r['c']})" for r in top))
    ns = conn.execute(
        "SELECT u.name, SUM(b.status = 'noshow') ns, SUM(b.status IN ('done', 'noshow')) tot, SUM(b.late_cancel) lc "
        "FROM bookings b JOIN users u ON u.tg_id = b.user_id WHERE slot_date >= ? GROUP BY b.user_id "
        "HAVING ns > 0 OR lc > 0 ORDER BY ns DESC", (since,),
    ).fetchall()
    if ns:
        lines.append("No-shows / late cancels:")
        for r in ns:
            rate = f"{100 * r['ns'] // r['tot']}%" if r["tot"] else "—"
            lines.append(f"  • {r['name']}: {r['ns']} no-shows ({rate}), {r['lc']} late cancels")
    return "\n".join(lines)
