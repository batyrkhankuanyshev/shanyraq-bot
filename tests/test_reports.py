import logging
from datetime import timedelta

import pytest

from bot.services import reports
from bot.timeutil import at_local
from tests.conftest import MENTORS, STUDENTS
from datetime import date

NOW = at_local(date(2026, 10, 1), "13:37")
REPORTER = STUDENTS[7]


@pytest.fixture(autouse=True)
def fresh_limiter(monkeypatch):
    monkeypatch.setattr(reports, "anon_limiter", reports._AnonRateLimiter())


def _dump_everything(conn) -> str:
    """Every value in every table, as text."""
    parts = []
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
        for row in conn.execute(f"SELECT * FROM {table}").fetchall():
            parts.extend(str(v) for v in tuple(row))
    return "\n".join(parts)


def test_anonymous_report_stores_no_identity_anywhere(conn, caplog, tmp_path):
    before = _dump_everything(conn)
    with caplog.at_level(logging.DEBUG):
        token, notices = reports.create_anonymous(conn, REPORTER, "Noise", "Loud music at 2am", None, True, NOW)
    after = _dump_everything(conn)
    new_values = set(after.split("\n")) - set(before.split("\n"))
    assert new_values, "report was stored"
    assert not any(str(REPORTER) in v for v in new_values)
    assert str(REPORTER) not in caplog.text
    # anon table has no column that could hold an identity
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(anon_reports)")}
    assert cols == {"id", "category", "description", "photo_id", "token_hash", "status", "created_at"}
    # the plain token is never stored, only its hash
    assert token not in after
    # mentors are notified; the reporter is not among the recipients
    assert {n.chat_id for n in notices} == set(MENTORS)
    assert all(str(REPORTER) not in n.text for n in notices)


def test_anonymous_rate_limit_three_per_day(conn):
    for i in range(3):
        reports.create_anonymous(conn, REPORTER, "Noise", f"r{i}", None, False, NOW)
    with pytest.raises(PermissionError):
        reports.create_anonymous(conn, REPORTER, "Noise", "r4", None, False, NOW)
    # someone else is not affected
    reports.create_anonymous(conn, STUDENTS[8], "Noise", "other", None, False, NOW)
    # next day resets
    reports.create_anonymous(conn, REPORTER, "Noise", "tomorrow", None, False, NOW + timedelta(days=1))


def test_anonymous_reply_relay_by_token(conn):
    token, _ = reports.create_anonymous(conn, REPORTER, "Conflict", "Help", None, True, NOW)
    r = reports.find_by_token(conn, token)
    reports.mentor_reply(conn, MENTORS[0], r["id"], "We'll talk to them", NOW)
    assert reports.find_by_token(conn, token.lower())  # case-insensitive
    n = reports.reporter_reply(conn, token, "Thanks", NOW)
    assert {x.chat_id for x in n} == set(MENTORS)
    texts = [m["text"] for m in reports.anon_messages(conn, r["id"])]
    assert texts == ["We'll talk to them", "Thanks"]
    reports.close_anonymous(conn, MENTORS[0], r["id"])
    with pytest.raises(ValueError):
        reports.reporter_reply(conn, token, "more", NOW)


def test_no_token_means_no_replies(conn):
    token, _ = reports.create_anonymous(conn, REPORTER, "Safety", "x", None, False, NOW)
    assert token is None
    rid = conn.execute("SELECT MAX(id) FROM anon_reports").fetchone()[0]
    with pytest.raises(ValueError):
        reports.mentor_reply(conn, MENTORS[0], rid, "hi", NOW)


def test_maintenance_duplicate_and_fixed_dm(conn):
    rid, n = reports.create_maintenance(conn, REPORTER, "Kitchen", "Stove broken", None, NOW, anonymous=False)
    assert {x.chat_id for x in n} == set(MENTORS)
    assert reports.recent_duplicate(conn, "Kitchen", NOW + timedelta(hours=1))["id"] == rid
    assert reports.recent_duplicate(conn, "Kitchen", NOW + timedelta(hours=3)) is None
    assert reports.set_status(conn, MENTORS[0], rid, "in_progress", NOW)[0].chat_id == REPORTER
    n = reports.set_status(conn, MENTORS[0], rid, "fixed", NOW)
    assert n[0].chat_id == REPORTER and "fixed" in n[0].text


def test_anonymous_maintenance_reporter_not_stored(conn):
    rid, _ = reports.create_maintenance(conn, REPORTER, "Wi-Fi", "Down", None, NOW, anonymous=True)
    assert conn.execute("SELECT reporter_id FROM reports WHERE id = ?", (rid,)).fetchone()[0] is None
    assert reports.set_status(conn, MENTORS[0], rid, "fixed", NOW) == []
