"""Guest log: every guest (day visit or overnight) needs a mentor's approval."""
from datetime import datetime, timedelta

from ..db import tx
from ..notices import Notice, kb
from ..timeutil import day_bounds, fmt_dt, from_db, local_today, to_db
from . import audit, users

MAX_ACTIVE = 2
DURATIONS = {  # code: (label, hours, overnight)
    "2h": ("2 сағат", 2, False), "4h": ("4 сағат", 4, False), "8h": ("8 сағат", 8, False),
    "1n": ("1 түн", 24, True), "2n": ("2 түн", 48, True), "3n": ("3 түн", 72, True),
}
LIVE = ("pending", "approved")
STATUS_KK = {"pending": "күтуде", "approved": "мақұлданды", "rejected": "қабылданбады",
             "checked_out": "кетті", "expired": "мерзімі бітті"}


def get(conn, gid: int):
    return conn.execute("SELECT * FROM guests WHERE id = ?", (gid,)).fetchone()


def active_for(conn, host: int, now: datetime):
    return conn.execute(
        "SELECT * FROM guests WHERE host_id = ? AND status IN ('pending', 'approved') AND depart_at > ? ORDER BY arrive_at",
        (host, to_db(now)),
    ).fetchall()


def describe(g) -> str:
    kind = "түнейді" if g["overnight"] else "күндізгі келу"
    return f"{g['name']} · {fmt_dt(from_db(g['arrive_at']))} → {fmt_dt(from_db(g['depart_at']))} ({kind}, {STATUS_KK[g['status']]})"


def register(conn, host: int, name: str, arrive: datetime, duration: str, now: datetime) -> tuple[str, list[Notice]]:
    label, hours, overnight = DURATIONS[duration]
    depart = arrive + timedelta(hours=hours)
    with tx(conn):
        if len(active_for(conn, host, now)) >= MAX_ACTIVE:
            raise ValueError(f"Сізде {MAX_ACTIVE} белсенді қонақ бар — алдымен біреуін шығарып жіберіңіз.")
        status = "pending"  # every guest waits for a mentor's decision
        cur = conn.execute(
            "INSERT INTO guests(host_id, name, arrive_at, depart_at, overnight, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (host, name, to_db(arrive), to_db(depart), int(overnight), status, to_db(now)),
        )
        g = get(conn, cur.lastrowid)
        host_name = users.name_of(conn, host)
        what = "қонағын түнетуге" if overnight else "қонақ шақыруға"
        notices = users.notify_mentors(
            conn, f"🚪 {host_name} {what} рұқсат сұрайды:\n{describe(g)}",
            kb([("✅ Мақұлдау", f"g:ap:{g['id']}"), ("❌ Қабылдамау", f"g:rj:{g['id']}")]), silent=False,
        )
        msg = "📨 Рұқсат сұралды — тәлімгер жақын арада шешім қабылдайды. Нәтижесін хабарлаймын."
    return msg, notices


def decide(conn, actor: int, gid: int, approve: bool, reason: str | None = None) -> list[Notice]:
    with tx(conn):
        g = get(conn, gid)
        if not g or g["status"] != "pending":
            raise ValueError("Бұл өтініш қаралып қойған.")
        conn.execute(
            "UPDATE guests SET status = ?, decided_by = ?, reason = ? WHERE id = ?",
            ("approved" if approve else "rejected", actor, reason, gid),
        )
        audit.log(conn, actor, "guest_" + ("approve" if approve else "reject"), f"#{gid} {g['name']} ({reason or ''})")
    if approve:
        return [Notice(g["host_id"], f"✅ {g['name']} қонағыңызға рұқсат берілді.")]
    return [Notice(g["host_id"], f"❌ {g['name']} қонағыңызға рұқсат берілмеді. Себебі: {reason or '—'}", silent=False)]


def check_out(conn, host: int, gid: int, now: datetime) -> bool:
    with tx(conn):
        cur = conn.execute(
            "UPDATE guests SET status = 'checked_out', depart_at = ? WHERE id = ? AND host_id = ? AND status IN ('pending', 'approved')",
            (to_db(now), gid, host),
        )
    return cur.rowcount == 1


def today_list(conn, now: datetime):
    start, end = day_bounds(local_today(now))
    return conn.execute(
        "SELECT g.*, u.name AS host_name FROM guests g JOIN users u ON u.tg_id = g.host_id "
        "WHERE g.status IN ('pending', 'approved', 'checked_out') AND g.arrive_at < ? AND g.depart_at > ? ORDER BY g.arrive_at",
        (to_db(end), to_db(start)),
    ).fetchall()


def pending(conn):
    return conn.execute(
        "SELECT g.*, u.name AS host_name FROM guests g JOIN users u ON u.tg_id = g.host_id WHERE g.status = 'pending' ORDER BY g.arrive_at"
    ).fetchall()


def tick(conn, now: datetime) -> list[Notice]:
    notices = []
    for g in conn.execute(
        "SELECT * FROM guests WHERE status IN ('pending', 'approved') AND depart_at <= ?", (to_db(now),)
    ).fetchall():
        conn.execute("UPDATE guests SET status = 'expired' WHERE id = ?", (g["id"],))
        if g["status"] == "pending":
            notices.append(Notice(g["host_id"], f"🚪 {g['name']} қонағыңыз бойынша тәлімгер шешім қабылдамады, өтініштің мерзімі өтті."))
        else:
            notices.append(Notice(g["host_id"], f"🚪 {g['name']} қонағыңыздың уақыты аяқталды (автоматты түрде шығарылды)."))
    return notices
