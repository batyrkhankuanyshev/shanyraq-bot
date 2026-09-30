"""Weekly rotations: cleaning zones and supplies buyer. Strict cycle over active,
un-paused students (mentors excluded). History is kept so fairness can be shown."""
from datetime import date, datetime, timedelta

from ..config import CONFIG
from ..db import get_setting, set_setting, tx
from ..notices import Notice, kb
from ..timeutil import fmt_date, local, local_today, to_db, week_start
from . import audit, users

ZONES = ["Ас үй", "Жуынатын бөлме", "Ортақ бөлме", "Дәліз"]
SUPPLIES = ["Дәретхана қағазы", "Қағаз сүлгі", "Ыдыс жуғыш сұйықтық", "Қоқыс пакеттері", "Тазалау спрейі", "Губкалар", "Кір жуғыш ұнтақ"]
DUTY_STATUS = {"assigned": "тағайындалды", "done": "орындалды", "missed": "орындалмады"}
REMIND_AT = "08:00"
MISSED_AT = "23:30"
BUYER_ROTATE_AT = "18:00"      # Sunday
CLEANING_ROTATE_AT = "20:00"   # Sunday


def _eligible(conn, ws: date):
    return [
        s for s in users.students(conn)
        if not s["paused_until"] or s["paused_until"] < ws.isoformat()
    ]


def _take_next(students, pointer: int, count: int):
    """Strict cycle by rotation_order, starting after the last person assigned."""
    if not students:
        return []
    ordered = sorted(students, key=lambda s: s["rotation_order"])
    start = next((i for i, s in enumerate(ordered) if s["rotation_order"] > pointer), 0)
    rotated = ordered[start:] + ordered[:start]
    return rotated[: min(count, len(rotated))]


def generate_cleaning(conn, ws: date, now: datetime, force: bool = False) -> list[Notice]:
    due = ws + timedelta(days=CONFIG.duty_weekday)
    with tx(conn):
        if conn.execute("SELECT 1 FROM duty WHERE kind = 'cleaning' AND week_start = ?", (ws.isoformat(),)).fetchone():
            return []
        if due < local_today(now) and not force:
            return []  # launched after this week's cleaning day; start with next week
        picked = _take_next(_eligible(conn, ws), int(get_setting(conn, "cleaning_pointer", "0")), len(ZONES))
        if not picked:
            return []
        offset = int(get_setting(conn, "cleaning_weeks", "0"))
        notices = []
        for i, s in enumerate(picked):
            zone = ZONES[(i + offset) % len(ZONES)]
            conn.execute(
                "INSERT INTO duty(kind, week_start, zone, user_id, due_date, created_at) VALUES ('cleaning', ?, ?, ?, ?, ?)",
                (ws.isoformat(), zone, s["tg_id"], due.isoformat(), to_db(now)),
            )
            notices.append(Notice(s["tg_id"], f"🧹 Ескерту: {fmt_date(due)} күні сіз «{zone}» аймағында кезекшісіз. Сол күні таңертең еске саламын."))
        set_setting(conn, "cleaning_pointer", str(picked[-1]["rotation_order"]))
        set_setting(conn, "cleaning_weeks", str(offset + 1))
    return notices


def generate_buyer(conn, ws: date, now: datetime) -> list[Notice]:
    with tx(conn):
        if conn.execute("SELECT 1 FROM duty WHERE kind = 'buyer' AND week_start = ?", (ws.isoformat(),)).fetchone():
            return []
        picked = _take_next(_eligible(conn, ws), int(get_setting(conn, "buyer_pointer", "0")), 1)
        if not picked:
            return []
        s = picked[0]
        cur = conn.execute(
            "INSERT INTO duty(kind, week_start, zone, user_id, due_date, created_at) VALUES ('buyer', ?, '', ?, ?, ?)",
            (ws.isoformat(), s["tg_id"], (ws + timedelta(days=6)).isoformat(), to_db(now)),
        )
        set_setting(conn, "buyer_pointer", str(s["rotation_order"]))
        d = conn.execute("SELECT * FROM duty WHERE id = ?", (cur.lastrowid,)).fetchone()
        text, markup = buyer_message(conn, d)
        return [Notice(s["tg_id"], text, markup)]


def shopping_list(conn):
    return conn.execute("SELECT * FROM supplies WHERE status = 'needed' ORDER BY id").fetchall()


def buyer_message(conn, d) -> tuple[str, object]:
    items = shopping_list(conn)
    ws = date.fromisoformat(d["week_start"])
    head = f"🛒 {fmt_date(ws)} басталатын аптада сатып алушы — сізсіз."
    if not items:
        return head + "\nҚазір сатып алу тізімі бос — бірдеңе таусылса, хабарлаймын.", None
    text = head + "\nТізім:\n" + "\n".join(f"• {i['item']}" for i in items) + "\nСатып алғаннан кейін затты басыңыз:"
    rows = [[(f"✅ {i['item']}", f"d:buy:{i['id']}")] for i in items]
    rows.append([("📎 Чекті жүктеу", f"d:rcpt:{d['id']}")])
    return text, kb(*rows)


def current_buyer(conn, now: datetime):
    ws = week_start(local_today(now))
    return conn.execute(
        "SELECT * FROM duty WHERE kind = 'buyer' AND week_start = ?", (ws.isoformat(),)
    ).fetchone()


def flag_low(conn, uid: int, item: str, now: datetime) -> tuple[str, list[Notice]]:
    if item not in SUPPLIES:
        return "Белгісіз зат.", []
    with tx(conn):
        if conn.execute("SELECT 1 FROM supplies WHERE item = ? AND status = 'needed'", (item,)).fetchone():
            return f"«{item}» сатып алу тізімінде бар 👍", []
        conn.execute(
            "INSERT INTO supplies(item, status, flagged_by, flagged_at) VALUES (?, 'needed', ?, ?)",
            (item, uid, to_db(now)),
        )
        notices = []
        buyer = current_buyer(conn, now)
        if buyer and buyer["user_id"] != uid:
            text, markup = buyer_message(conn, buyer)
            notices.append(Notice(buyer["user_id"], f"🛒 Тізімге «{item}» қосылды.\n\n" + text, markup))
    return f"🛒 «{item}» ортақ сатып алу тізіміне қосылды. Рақмет!", notices


def mark_bought(conn, uid: int, supply_id: int, now: datetime) -> str:
    with tx(conn):
        s = conn.execute("SELECT * FROM supplies WHERE id = ?", (supply_id,)).fetchone()
        if not s or s["status"] != "needed":
            return "Сатып алынды деп белгіленген 👍"
        conn.execute(
            "UPDATE supplies SET status = 'bought', bought_by = ?, bought_at = ? WHERE id = ?",
            (uid, to_db(now), supply_id),
        )
    return f"✅ «{s['item']}» сатып алынды деп белгіленді. Рақмет!"


def save_receipt(conn, duty_id: int, uid: int, photo_id: str, now: datetime) -> bool:
    with tx(conn):
        cur = conn.execute(
            "UPDATE duty SET receipt_id = ?, status = 'done', done_at = COALESCE(done_at, ?) "
            "WHERE id = ? AND user_id = ? AND kind = 'buyer'",
            (photo_id, to_db(now), duty_id, uid),
        )
    return cur.rowcount == 1


def my_duties(conn, uid: int, now: datetime):
    ws = week_start(local_today(now))
    return conn.execute(
        "SELECT * FROM duty WHERE user_id = ? AND week_start >= ? ORDER BY week_start, kind",
        (uid, ws.isoformat()),
    ).fetchall()


def mark_cleaning_done(conn, duty_id: int, uid: int, now: datetime) -> tuple[str, list[Notice]]:
    with tx(conn):
        d = conn.execute("SELECT * FROM duty WHERE id = ?", (duty_id,)).fetchone()
        if not d or d["user_id"] != uid or d["kind"] != "cleaning":
            return "Бұл сіздің кезекшілігіңіз емес.", []
        if d["status"] == "done":
            return "Орындалды деп белгіленген 👍", []
        conn.execute("UPDATE duty SET status = 'done', done_at = ? WHERE id = ?", (to_db(now), duty_id))
        notices = []
        nxt = conn.execute(
            "SELECT * FROM duty WHERE kind = 'cleaning' AND zone = ? AND week_start > ? ORDER BY week_start LIMIT 1",
            (d["zone"], d["week_start"]),
        ).fetchone()
        if nxt:
            notices.append(Notice(nxt["user_id"], f"🧹 {users.name_of(conn, uid)} «{d['zone']}» аймағын тазалап бітірді. "
                                  f"Келесі кезек сіздікі: «{d['zone']}», {fmt_date(date.fromisoformat(nxt['due_date']))}."))
        else:
            upcoming = _take_next(_eligible(conn, week_start(local_today(now)) + timedelta(days=7)),
                                  int(get_setting(conn, "cleaning_pointer", "0")), 1)
            if upcoming and upcoming[0]["tg_id"] != uid:
                notices.append(Notice(upcoming[0]["tg_id"],
                                      f"🧹 {users.name_of(conn, uid)} «{d['zone']}» аймағын тазалап бітірді. Тазалау кезегінде келесі — сіз."))
    return f"✅ Рақмет! «{d['zone']}» орындалды деп белгіленді.", notices


def reassign(conn, actor: int, duty_id: int, new_uid: int) -> list[Notice]:
    with tx(conn):
        d = conn.execute("SELECT * FROM duty WHERE id = ?", (duty_id,)).fetchone()
        if not d:
            return []
        conn.execute(
            "UPDATE duty SET user_id = ?, changed_by = ?, reminded = 0, status = 'assigned' WHERE id = ?",
            (new_uid, actor, duty_id),
        )
        what = f"«{d['zone']}» тазалау" if d["kind"] == "cleaning" else "шаруашылық заттарын сатып алу"
        audit.log(conn, actor, "duty_reassign",
                  f"{what} week {d['week_start']}: {users.name_of(conn, d['user_id'])} -> {users.name_of(conn, new_uid)}")
        return [
            Notice(new_uid, f"📋 Тәлімгер сізге тапсырды: {what} ({fmt_date(date.fromisoformat(d['week_start']))} басталатын апта)."),
            Notice(d["user_id"], f"📋 Тәлімгер сіздің кезекшілігіңізді ({what}) басқа адамға берді."),
        ]


def history(conn, weeks: int = 4):
    since = (local_today(datetime.now().astimezone()) - timedelta(weeks=weeks)).isoformat()
    return conn.execute(
        "SELECT d.*, u.name FROM duty d JOIN users u ON u.tg_id = d.user_id WHERE week_start >= ? "
        "ORDER BY week_start DESC, kind, zone", (since,),
    ).fetchall()


def fairness_text(conn) -> str:
    rows = conn.execute(
        "SELECT u.name, u.active, "
        "SUM(d.kind = 'cleaning') cl, SUM(d.kind = 'cleaning' AND d.status = 'done') cl_done, "
        "SUM(d.kind = 'cleaning' AND d.status = 'missed') cl_missed, SUM(d.kind = 'buyer') buy "
        "FROM users u LEFT JOIN duty d ON d.user_id = u.tg_id WHERE u.role = 'student' "
        "GROUP BY u.tg_id ORDER BY u.rotation_order"
    ).fetchall()
    lines = ["📊 Кезекшілік қорытындысы (барлық уақыт): тазалау орындалды/тағайындалды · өткізіп алды · сатып алушы апталары"]
    for r in rows:
        tag = "" if r["active"] else " (белсенді емес)"
        lines.append(f"• {r['name']}{tag}: {r['cl_done'] or 0}/{r['cl'] or 0} · {r['cl_missed'] or 0} · {r['buy'] or 0}")
    return "\n".join(lines)


def tick(conn, now: datetime) -> list[Notice]:
    loc = local(now)
    today = loc.date()
    hhmm = loc.strftime("%H:%M")
    ws = week_start(today)
    notices = []
    # This week (covers first launch and catch-up after downtime)
    notices += generate_buyer(conn, ws, now)
    notices += generate_cleaning(conn, ws, now)
    # Sunday evening: next week's rotation
    if today.weekday() == 6 and hhmm >= BUYER_ROTATE_AT:
        notices += generate_buyer(conn, ws + timedelta(days=7), now)
    if today.weekday() == 6 and hhmm >= CLEANING_ROTATE_AT:
        notices += generate_cleaning(conn, ws + timedelta(days=7), now)

    if hhmm >= REMIND_AT:
        for d in conn.execute(
            "SELECT * FROM duty WHERE kind = 'cleaning' AND status = 'assigned' AND reminded = 0 AND due_date = ?",
            (today.isoformat(),),
        ).fetchall():
            conn.execute("UPDATE duty SET reminded = 1 WHERE id = ?", (d["id"],))
            notices.append(Notice(d["user_id"], f"🧹 Бүгін сіз «{d['zone']}» аймағында кезекшісіз.",
                                  kb([("✅ Орындалды", f"d:done:{d['id']}")])))

    missed_q = "SELECT * FROM duty WHERE kind = 'cleaning' AND status = 'assigned' AND (due_date < ?"
    args = [today.isoformat()]
    if hhmm >= MISSED_AT:
        missed_q += " OR due_date = ?"
        args.append(today.isoformat())
    for d in conn.execute(missed_q + ")", args).fetchall():
        conn.execute("UPDATE duty SET status = 'missed' WHERE id = ?", (d["id"],))
        name = users.name_of(conn, d["user_id"])
        notices += users.notify_mentors(
            conn, f"🧹 {name} «{d['zone']}» кезекшілігін орындалды деп белгілемеді ({fmt_date(date.fromisoformat(d['due_date']))}).",
            silent=True,
        )
    return notices
