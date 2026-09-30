from datetime import date, timedelta

import pytest

from bot import jobs
from bot.services import announcements, events, guests
from bot.timeutil import at_local
from tests.conftest import MENTORS, STUDENTS

DAY = date(2026, 10, 1)
NOW = at_local(DAY, "12:00")
S1 = STUDENTS[0]


def test_guest_limits_and_approval(conn):
    msg, n = guests.register(conn, S1, "Ann", NOW, "4h", NOW)
    assert "auto-approved" in msg and all(x.silent for x in n)
    msg, n = guests.register(conn, S1, "Bob", NOW, "1n", NOW)
    assert "requested" in msg and {x.chat_id for x in n} == set(MENTORS)
    with pytest.raises(ValueError):
        guests.register(conn, S1, "Cid", NOW, "2h", NOW)
    bob = conn.execute("SELECT id FROM guests WHERE name = 'Bob'").fetchone()["id"]
    n = guests.decide(conn, MENTORS[0], bob, False, "exam week")
    assert n[0].chat_id == S1 and "exam week" in n[0].text
    guests.register(conn, S1, "Cid", NOW, "2h", NOW)  # rejected guest freed a place
    assert len(guests.today_list(conn, NOW)) == 2
    n = guests.tick(conn, NOW + timedelta(hours=5))
    assert {x.text.split("'")[0] for x in n} == {"🚪 Ann", "🚪 Cid"}


def test_event_rsvp_reminder_and_recurring(conn):
    start = at_local(DAY, "20:00")
    eid, n = events.create(conn, S1, "Movie night", "", "Common room", start, True, NOW)
    assert len(n) == 16 and all(x.chat_id != S1 for x in n)
    events.set_rsvp(conn, eid, STUDENTS[1], "going", NOW)
    events.set_rsvp(conn, eid, STUDENTS[2], "cant", NOW)
    assert events.counts(conn, eid) == {"going": 2, "maybe": 0, "cant": 1}
    n = events.tick(conn, at_local(DAY, "19:05"))
    assert {x.chat_id for x in n} == {S1, STUDENTS[1]}
    assert events.tick(conn, at_local(DAY, "19:06")) == []
    events.tick(conn, at_local(DAY, "20:01"))
    nxt = conn.execute("SELECT * FROM events WHERE id != ?", (eid,)).fetchone()
    assert nxt["starts_at"] == "2026-10-08T12:00:00Z"   # 20:00 HKT a week later


def test_scheduled_announcement_sent_once_even_after_downtime(conn):
    aid = announcements.create(conn, MENTORS[0], "Quiet", "Exams", None, "students", True, True,
                               at_local(DAY, "22:00"), NOW)
    assert announcements.tick(conn, NOW) == []
    n = announcements.tick(conn, at_local(DAY + timedelta(days=1), "03:00"))  # bot was down at 22:00
    assert len(n) == 12 and all(x.pin and x.ann_id == aid for x in n)
    assert announcements.tick(conn, at_local(DAY + timedelta(days=1), "03:01")) == []


def test_run_all_survives_a_failing_job(conn, monkeypatch):
    def boom(conn, now):
        raise RuntimeError("x")
    monkeypatch.setattr(jobs, "JOBS", [("boom", boom)] + jobs.JOBS)
    assert isinstance(jobs.run_all(conn, NOW), list)
