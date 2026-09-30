from datetime import datetime, timezone

import pytest

from bot.db import connect
from bot.services import users
from bot.timeutil import at_local

MENTORS = [100 + i for i in range(5)]
STUDENTS = [200 + i for i in range(12)]


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    for i, m in enumerate(MENTORS):
        users.register(c, m, f"Mentor{i}", "mentor", t0)
    for i, s in enumerate(STUDENTS):
        users.register(c, s, f"Student{i}", "student", t0)
    yield c
    c.close()


@pytest.fixture
def db_path(conn, tmp_path):
    return str(tmp_path / "test.db")


def local_dt(y, mo, d, hhmm):
    from datetime import date
    return at_local(date(y, mo, d), hhmm)
