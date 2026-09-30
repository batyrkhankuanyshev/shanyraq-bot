from collections import Counter
from datetime import date, timedelta

from bot.services import duty, users
from bot.timeutil import at_local, week_start
from tests.conftest import MENTORS, STUDENTS

MONDAY = date(2026, 10, 5)


def _sunday_evening(ws):
    return at_local(ws - timedelta(days=1), "20:05")


def test_rotation_is_a_strict_fair_cycle(conn):
    for w in range(6):  # 6 weeks * 4 zones = 24 = 2 full cycles of 12 students
        ws = MONDAY + timedelta(weeks=w)
        duty.tick(conn, _sunday_evening(ws))
    rows = conn.execute("SELECT user_id, zone FROM duty WHERE kind = 'cleaning'").fetchall()
    per_user = Counter(r["user_id"] for r in rows)
    assert set(per_user) == set(STUDENTS)          # mentors excluded
    assert set(per_user.values()) == {2}           # everyone exactly twice
    # zones vary for a given student
    zones = {s: {r["zone"] for r in rows if r["user_id"] == s} for s in STUDENTS}
    assert all(len(z) == 2 for z in zones.values())
    buyers = conn.execute("SELECT user_id FROM duty WHERE kind = 'buyer' ORDER BY week_start").fetchall()
    assert len({b["user_id"] for b in buyers}) == len(buyers)  # rotates, no repeats within 12 weeks


def test_paused_student_is_skipped(conn):
    users.set_paused(conn, MENTORS[0], STUDENTS[0], (MONDAY + timedelta(days=6)).isoformat())
    duty.tick(conn, _sunday_evening(MONDAY))
    assigned = {r["user_id"] for r in conn.execute("SELECT user_id FROM duty WHERE week_start = ?", (MONDAY.isoformat(),))}
    assert STUDENTS[0] not in assigned
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'pause'").fetchone()[0] == 1


def test_reminder_done_and_missed(conn):
    duty.tick(conn, _sunday_evening(MONDAY))
    due = MONDAY + timedelta(days=5)
    n = duty.tick(conn, at_local(due, "08:00"))
    reminders = [x for x in n if "Бүгін сіз" in x.text]
    assert len(reminders) == 4
    assert not any("Бүгін сіз" in x.text for x in duty.tick(conn, at_local(due, "08:01")))
    first = conn.execute("SELECT * FROM duty WHERE kind = 'cleaning' AND due_date = ? ORDER BY id", (due.isoformat(),)).fetchall()
    text, _ = duty.mark_cleaning_done(conn, first[0]["id"], first[0]["user_id"], at_local(due, "10:00"))
    assert "Рақмет" in text
    n = duty.tick(conn, at_local(due, "23:30"))
    missed = [x for x in n if "белгілемеді" in x.text]
    assert len(missed) == 3 * len(MENTORS)


def test_generation_is_idempotent_and_catches_up(conn):
    # bot was down all Sunday evening; Monday morning it generates the current week once
    duty.tick(conn, at_local(MONDAY, "09:00"))
    duty.tick(conn, at_local(MONDAY, "09:01"))
    assert conn.execute("SELECT COUNT(*) FROM duty WHERE kind = 'cleaning'").fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM duty WHERE kind = 'buyer'").fetchone()[0] == 1


def test_low_stock_list_and_buyer(conn):
    now = at_local(MONDAY, "09:00")
    duty.tick(conn, now)
    buyer = duty.current_buyer(conn, now)
    text, n = duty.flag_low(conn, STUDENTS[5] if buyer["user_id"] != STUDENTS[5] else STUDENTS[6], "Ыдыс жуғыш сұйықтық", now)
    assert "қосылды" in text and n[0].chat_id == buyer["user_id"]
    assert "тізімінде бар" in duty.flag_low(conn, STUDENTS[7], "Ыдыс жуғыш сұйықтық", now)[0]
    item = duty.shopping_list(conn)[0]
    duty.mark_bought(conn, buyer["user_id"], item["id"], now)
    assert duty.shopping_list(conn) == []


def test_reassign_is_logged(conn):
    duty.tick(conn, _sunday_evening(MONDAY))
    d = conn.execute("SELECT * FROM duty WHERE kind = 'cleaning' LIMIT 1").fetchone()
    other = next(s for s in STUDENTS if s != d["user_id"])
    duty.reassign(conn, MENTORS[1], d["id"], other)
    assert conn.execute("SELECT user_id FROM duty WHERE id = ?", (d["id"],)).fetchone()[0] == other
    assert conn.execute("SELECT actor_id FROM audit WHERE action = 'duty_reassign'").fetchone()[0] == MENTORS[1]
