import threading
from datetime import date, timedelta

import pytest

from bot.db import connect
from bot.services import washer
from bot.services.washer import BookingError
from bot.timeutil import at_local, to_db
from tests.conftest import MENTORS, STUDENTS

DAY = date(2026, 10, 1)            # a Thursday
NOW = at_local(DAY, "07:00")       # before the first slot
S1, S2, S3 = STUDENTS[:3]


def slot(hhmm="09:30", d=DAY):
    return at_local(d, hhmm)


# ---------------------------------------------------------------- concurrency

def test_concurrent_booking_same_slot_only_one_wins(conn, db_path):
    """20 students tap the same slot at the same instant from separate connections."""
    barrier = threading.Barrier(12)
    results = {}

    def worker(uid):
        c = connect(db_path)
        barrier.wait()
        try:
            washer.book(c, uid, DAY, "09:30", "normal", NOW)
            results[uid] = "ok"
        except BookingError as e:
            results[uid] = e.code
        finally:
            c.close()

    threads = [threading.Thread(target=worker, args=(uid,)) for uid in STUDENTS]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert list(results.values()).count("ok") == 1
    assert all(v in ("ok", "taken") for v in results.values())
    live = conn.execute("SELECT COUNT(*) FROM bookings WHERE status IN ('booked','active')").fetchone()[0]
    assert live == 1


def test_database_rejects_double_booking_even_without_app_checks(conn):
    washer.book(conn, S1, DAY, "09:30", "normal", NOW)
    with pytest.raises(BookingError) as e:
        washer.book(conn, S2, DAY, "09:30", "quick", NOW, check_rules=False, check_block=False)
    assert e.value.code == "taken"


def test_cancelled_slot_can_be_rebooked(conn):
    b = washer.book(conn, S1, DAY, "09:30", "normal", NOW)
    washer.user_cancel(conn, b["id"], S1, NOW)
    assert washer.book(conn, S2, DAY, "09:30", "normal", NOW)


# ---------------------------------------------------------------- fair-use rules

def test_weekly_limit_two(conn):
    washer.book(conn, S1, DAY, "08:00", "quick", NOW)
    washer.book(conn, S1, DAY + timedelta(days=1), "08:00", "quick", NOW)
    with pytest.raises(BookingError) as e:
        washer.book(conn, S1, DAY + timedelta(days=2), "08:00", "quick", NOW)
    assert e.value.code == "limit"


def test_cancel_does_not_count_toward_limit_but_late_cancel_is_logged(conn):
    b = washer.book(conn, S1, DAY, "08:00", "quick", NOW)
    washer.user_cancel(conn, b["id"], S1, at_local(DAY, "07:58"))
    row = washer.get_booking(conn, b["id"])
    assert row["status"] == "cancelled" and row["late_cancel"] == 1
    washer.book(conn, S1, DAY, "09:30", "quick", NOW)
    washer.book(conn, S1, DAY, "11:00", "quick", NOW)  # still 2 allowed


def test_mentors_are_not_limited(conn):
    for t in ("08:00", "09:30", "11:00"):
        washer.book(conn, MENTORS[0], DAY, t, "quick", NOW)


def test_three_days_ahead_max_and_past_slots(conn):
    with pytest.raises(BookingError) as e:
        washer.book(conn, S1, DAY + timedelta(days=4), "08:00", "quick", NOW)
    assert e.value.code == "too_far"
    with pytest.raises(BookingError) as e:
        washer.book(conn, S1, DAY, "08:00", "quick", at_local(DAY, "08:10"))
    assert e.value.code == "past"
    slots = washer.slots_for_day(conn, DAY, at_local(DAY, "10:00"), S1)
    assert slots[0]["time"] == "11:00"  # past slots hidden


def test_heavy_needs_next_slot_free(conn):
    washer.book(conn, S2, DAY, "11:00", "quick", NOW)
    with pytest.raises(BookingError) as e:
        washer.book(conn, S1, DAY, "09:30", "heavy", NOW)
    assert e.value.code == "heavy_conflict"


# ---------------------------------------------------------------- live cycle & no-shows

def _texts(notices, uid=None):
    return [n.text for n in notices if uid is None or n.chat_id == uid]


def test_reminder_start_and_end_flow(conn):
    b = washer.book(conn, S1, DAY, "09:30", "quick", NOW)
    n = washer.tick(conn, at_local(DAY, "09:15"))
    assert any("starts in 15 min" in t for t in _texts(n, S1))
    assert all(x.silent for x in n)                      # reminders are silent
    assert washer.tick(conn, at_local(DAY, "09:16")) == []  # no duplicate
    washer.start_booking(conn, b["id"], S1, at_local(DAY, "09:31"))
    n = washer.tick(conn, at_local(DAY, "10:30"))         # quick = 60 min
    assert any("time is up" in t for t in _texts(n, S1))
    assert washer.get_booking(conn, b["id"])["status"] == "done"


def test_no_show_detection_frees_slot_and_offers_waitlist(conn):
    b = washer.book(conn, S1, DAY, "09:30", "normal", NOW)
    washer.join_waitlist(conn, S2, slot(), NOW)
    washer.tick(conn, at_local(DAY, "09:15"))
    washer.tick(conn, at_local(DAY, "09:30"))
    n = washer.tick(conn, at_local(DAY, "09:45"))         # +15: still using it?
    assert any("Still using" in t for t in _texts(n, S1))
    assert washer.tick(conn, at_local(DAY, "09:50")) == []  # waiting for reply
    n = washer.tick(conn, at_local(DAY, "09:55"))         # +10 more: no-show
    assert washer.get_booking(conn, b["id"])["status"] == "noshow"
    assert any("no-show" in t for t in _texts(n, S1))
    offer = [x for x in n if x.chat_id == S2]
    assert offer and "Claim" in offer[0].text and not offer[0].silent


def test_answering_still_using_prevents_no_show(conn):
    b = washer.book(conn, S1, DAY, "09:30", "normal", NOW)
    washer.tick(conn, at_local(DAY, "09:45"))
    washer.start_booking(conn, b["id"], S1, at_local(DAY, "09:47"))
    washer.tick(conn, at_local(DAY, "09:58"))
    assert washer.get_booking(conn, b["id"])["status"] == "active"


def test_third_no_show_bans_and_pings_mentors(conn):
    for i, d in enumerate([DAY, DAY + timedelta(days=1), DAY + timedelta(days=5)]):
        now = at_local(d, "07:00")
        washer.book(conn, S1, d, "09:30", "quick", now, check_rules=False)
        washer.tick(conn, at_local(d, "09:45"))
        n = washer.tick(conn, at_local(d, "09:55"))
    assert washer.noshows_this_month(conn, S1, at_local(DAY, "12:00")) == 3
    assert {x.chat_id for x in n} >= set(MENTORS)
    with pytest.raises(BookingError) as e:
        washer.book(conn, S1, DAY + timedelta(days=6), "08:00", "quick", at_local(DAY + timedelta(days=5), "12:00"))
    assert e.value.code == "banned"


def test_done_early_notifies_next_holder(conn):
    b = washer.book(conn, S1, DAY, "09:30", "normal", NOW)
    washer.book(conn, S2, DAY, "11:00", "normal", NOW)
    washer.start_booking(conn, b["id"], S1, at_local(DAY, "09:30"))
    _, n = washer.finish_booking(conn, b["id"], S1, at_local(DAY, "10:20"))
    assert any(x.chat_id == S2 and "free early" in x.text for x in n)


def test_running_late_notifies_next_and_extends(conn):
    b = washer.book(conn, S1, DAY, "09:30", "normal", NOW)
    washer.book(conn, S2, DAY, "11:00", "normal", NOW)
    washer.start_booking(conn, b["id"], S1, at_local(DAY, "09:30"))
    washer.tick(conn, at_local(DAY, "11:00"))  # end prompt, auto-done
    _, n = washer.running_late(conn, b["id"], S1, at_local(DAY, "11:02"))
    assert any(x.chat_id == S2 and "running late" in x.text for x in n)
    assert washer.get_booking(conn, b["id"])["status"] == "active"
    washer.tick(conn, at_local(DAY, "11:15"))
    assert washer.get_booking(conn, b["id"])["status"] == "done"


# ---------------------------------------------------------------- waitlist claim window

def test_waitlist_claim_window_expires_and_moves_on(conn):
    b = washer.book(conn, S1, DAY, "14:00", "normal", NOW)
    washer.join_waitlist(conn, S2, slot("14:00"), NOW)
    washer.join_waitlist(conn, S3, slot("14:00"), NOW)
    t = at_local(DAY, "12:00")
    n = washer.user_cancel(conn, b["id"], S1, t)
    assert [x.chat_id for x in n] == [S2]
    wl2 = conn.execute("SELECT id FROM waitlist WHERE user_id = ?", (S2,)).fetchone()["id"]
    assert washer.tick(conn, t + timedelta(minutes=4)) == []       # still inside window
    n = washer.tick(conn, t + timedelta(minutes=5))                 # window over
    assert any(x.chat_id == S2 and "passed" in x.text for x in n)
    assert any(x.chat_id == S3 and "Claim" in x.text for x in n)
    with pytest.raises(BookingError) as e:
        washer.claim(conn, wl2, S2, "normal", t + timedelta(minutes=6))
    assert e.value.code == "expired"
    wl3 = conn.execute("SELECT id FROM waitlist WHERE user_id = ?", (S3,)).fetchone()["id"]
    booked = washer.claim(conn, wl3, S3, "quick", t + timedelta(minutes=7))
    assert booked["user_id"] == S3 and booked["status"] == "booked"


def test_claim_after_start_becomes_active(conn):
    b = washer.book(conn, S1, DAY, "09:30", "normal", NOW)
    washer.join_waitlist(conn, S2, slot(), NOW)
    washer.tick(conn, at_local(DAY, "09:45"))
    washer.tick(conn, at_local(DAY, "09:55"))  # no-show -> offer to S2
    wl = conn.execute("SELECT id FROM waitlist WHERE user_id = ?", (S2,)).fetchone()["id"]
    got = washer.claim(conn, wl, S2, "quick", at_local(DAY, "09:57"))
    assert got["status"] == "active"
    assert washer.get_booking(conn, b["id"])["status"] == "noshow"


def test_cannot_waitlist_free_slot(conn):
    assert washer.join_waitlist(conn, S1, slot(), NOW) == "free"


# ---------------------------------------------------------------- maintenance

def test_maintenance_cancels_bookings_and_clears_waitlist(conn):
    b = washer.book(conn, S1, DAY, "14:00", "normal", NOW)
    washer.join_waitlist(conn, S2, slot("14:00"), NOW)
    n = washer.start_maintenance(conn, MENTORS[0], "pipe leak", NOW)
    assert washer.get_booking(conn, b["id"])["status"] == "cancelled"
    assert any(x.chat_id == S1 and "pipe leak" in x.text for x in n)
    assert any(x.chat_id == S2 and "cleared" in x.text for x in n)
    with pytest.raises(BookingError) as e:
        washer.book(conn, S3, DAY, "17:00", "quick", NOW)
    assert e.value.code == "blocked"
    washer.lift_maintenance(conn, MENTORS[0], NOW)
    assert washer.book(conn, S3, DAY, "17:00", "quick", NOW)
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action LIKE 'washer_maintenance%'").fetchone()[0] == 2


def test_breakdown_blocks_rest_of_day_only(conn):
    b = washer.book(conn, S1, DAY, "09:30", "normal", NOW)
    later = washer.book(conn, S2, DAY, "17:00", "normal", NOW)
    tomorrow = washer.book(conn, S3, DAY + timedelta(days=1), "09:30", "normal", NOW)
    washer.start_booking(conn, b["id"], S1, at_local(DAY, "09:30"))
    n = washer.report_breakdown(conn, S1, b["id"], at_local(DAY, "10:00"))
    assert washer.get_booking(conn, later["id"])["status"] == "cancelled"
    assert washer.get_booking(conn, tomorrow["id"])["status"] == "booked"
    assert {x.chat_id for x in n} >= set(MENTORS)
    assert conn.execute("SELECT COUNT(*) FROM reports WHERE area = 'Washer'").fetchone()[0] == 1


# ---------------------------------------------------------------- recurring & deactivation

def test_recurring_cap_and_materialize(conn):
    for i, s in enumerate(STUDENTS[:4]):
        washer.request_recurring(conn, s, DAY.weekday(), washer.SLOT_TIMES[i + 2], "quick", NOW)
    for r in conn.execute("SELECT id FROM recurring").fetchall():
        washer.decide_recurring(conn, MENTORS[0], r["id"], True, NOW)
    with pytest.raises(BookingError) as e:
        washer.request_recurring(conn, STUDENTS[5], 0, "08:00", "quick", NOW)
    assert e.value.code == "cap"
    assert conn.execute("SELECT COUNT(*) FROM bookings WHERE recurring_id IS NOT NULL").fetchone()[0] == 4
    washer.materialize_recurring(conn, NOW)  # idempotent
    assert conn.execute("SELECT COUNT(*) FROM bookings WHERE recurring_id IS NOT NULL").fetchone()[0] == 4


def test_deactivate_cancels_future_bookings_and_recurring(conn):
    from bot.services import users
    b = washer.book(conn, S1, DAY, "14:00", "normal", NOW)
    washer.request_recurring(conn, S1, 0, "08:00", "quick", NOW)
    users.deactivate(conn, MENTORS[0], S1, NOW)
    assert washer.get_booking(conn, b["id"])["status"] == "cancelled"
    assert conn.execute("SELECT status FROM recurring WHERE user_id = ?", (S1,)).fetchone()[0] == "ended"
    with pytest.raises(BookingError):
        washer.book(conn, S1, DAY, "17:00", "quick", NOW)


def test_restart_catch_up(conn, db_path):
    """All state is in the DB: a fresh connection after 'downtime' picks up where it left off."""
    b = washer.book(conn, S1, DAY, "09:30", "normal", NOW)
    c2 = connect(db_path)
    washer.tick(c2, at_local(DAY, "09:50"))            # bot was down during reminder + start
    n = washer.tick(c2, at_local(DAY, "10:00"))
    assert washer.get_booking(c2, b["id"])["status"] == "noshow"
    c2.close()
