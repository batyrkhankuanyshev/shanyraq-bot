"""Maintenance reports and anonymous reports.

ANONYMITY RULES for anonymous reports (do not break these):
  * no Telegram ID, name or chat ID is ever written to the database or the logs;
  * the rate limit uses an HMAC of the user ID with a random key that lives only
    in memory and is replaced every day, so yesterday's counters can't be linked
    to anyone, and nothing about it is persisted;
  * replies are relayed through a random token only the reporter knows; the bot
    stores only its SHA-256 hash and never pushes messages to the reporter.
"""
import hashlib
import hmac
import secrets
from datetime import date, datetime, timedelta

from ..db import tx
from ..notices import Notice, kb
from ..timeutil import fmt_dt, from_db, local_today, to_db
from . import audit, users

AREAS = ["Кір жуғыш", "Ас үй", "Жуынатын бөлме", "Электр", "Сантехника", "Wi-Fi", "Жиһаз", "Басқа"]
ANON_CATEGORIES = ["Шу", "Тазалық", "Ереже бұзу", "Жанжал", "Қауіпсіздік", "Басқа"]
STATUSES = {"open": "🔴 ашық", "in_progress": "🟡 жөнделуде", "fixed": "🟢 жөнделді"}
DUPLICATE_WINDOW = timedelta(hours=2)
ANON_DAILY_LIMIT = 3


# ---------------------------------------------------------------- maintenance

def recent_duplicate(conn, area: str, now: datetime):
    return conn.execute(
        "SELECT * FROM reports WHERE area = ? AND status != 'fixed' AND created_at >= ? ORDER BY id DESC LIMIT 1",
        (area, to_db(now - DUPLICATE_WINDOW)),
    ).fetchone()


def report_text(conn, r) -> str:
    who = users.name_of(conn, r["reporter_id"]) if r["reporter_id"] else "анонимді"
    return (
        f"🛠 Ақаулық №{r['id']} — {r['area']}\n{r['description']}\n"
        f"Хабарлаған: {who}, {fmt_dt(from_db(r['created_at']))}\nКүйі: {STATUSES[r['status']]}"
    )


def status_buttons(rid: int):
    return kb([("🟡 Жөнделуде", f"r:ms:{rid}:in_progress"), ("🟢 Жөнделді", f"r:ms:{rid}:fixed")])


def create_maintenance(conn, uid: int, area: str, description: str, photo_id: str | None, now: datetime,
                       anonymous: bool) -> tuple[int, list[Notice]]:
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO reports(area, reporter_id, description, photo_id, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'open', ?, ?)",
            (area, None if anonymous else uid, description, photo_id, to_db(now), to_db(now)),
        )
        r = conn.execute("SELECT * FROM reports WHERE id = ?", (cur.lastrowid,)).fetchone()
        notices = users.notify_mentors(conn, report_text(conn, r), status_buttons(r["id"]), silent=False, photo=photo_id)
    return r["id"], notices


def set_status(conn, actor: int, rid: int, status: str, now: datetime) -> list[Notice]:
    with tx(conn):
        r = conn.execute("SELECT * FROM reports WHERE id = ?", (rid,)).fetchone()
        if not r or status not in STATUSES or r["status"] == status:
            return []
        conn.execute(
            "UPDATE reports SET status = ?, updated_by = ?, updated_at = ? WHERE id = ?",
            (status, actor, to_db(now), rid),
        )
        audit.log(conn, actor, "report_status", f"#{rid} {r['area']} -> {status}")
        notices = []
        if r["reporter_id"] and status in ("fixed", "in_progress"):
            msg = "жөнделді ✅ Хабарлағаныңызға рақмет!" if status == "fixed" else "бойынша жұмыс басталды 🔧"
            notices.append(Notice(r["reporter_id"], f"Сіздің №{rid} өтінішіңіз ({r['area']}) {msg}"))
    return notices


def open_reports(conn):
    return conn.execute("SELECT * FROM reports WHERE status != 'fixed' ORDER BY id DESC LIMIT 20").fetchall()


# ---------------------------------------------------------------- anonymous

class _AnonRateLimiter:
    """In-memory only. Key = HMAC(daily random secret, user id). Never persisted."""

    def __init__(self):
        self._day: date | None = None
        self._secret = b""
        self._counts: dict[str, int] = {}

    def _rotate(self, today: date) -> None:
        if self._day != today:
            self._day = today
            self._secret = secrets.token_bytes(32)
            self._counts = {}

    def _key(self, uid: int) -> str:
        return hmac.new(self._secret, str(uid).encode(), hashlib.sha256).hexdigest()

    def allowed(self, uid: int, now: datetime) -> bool:
        self._rotate(local_today(now))
        return self._counts.get(self._key(uid), 0) < ANON_DAILY_LIMIT

    def hit(self, uid: int, now: datetime) -> None:
        self._rotate(local_today(now))
        k = self._key(uid)
        self._counts[k] = self._counts.get(k, 0) + 1


anon_limiter = _AnonRateLimiter()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.strip().upper().encode()).hexdigest()


def anon_text(r, messages=()) -> str:
    lines = [f"🕶 Анонимді шағым №{r['id']} — {r['category']}", r["description"],
             f"Түскен уақыты: {fmt_dt(from_db(r['created_at']))}"]
    if r["status"] == "closed":
        lines.append("Күйі: жабылды")
    for m in messages:
        who = "Тәлімгер" if m["sender"] == "mentor" else "Шағым иесі"
        lines.append(f"— {who}: {m['text']}")
    return "\n".join(lines)


def anon_buttons(r):
    rows = []
    if r["token_hash"] and r["status"] == "open":
        rows.append([("💬 Анонимді жауап беру", f"r:ar:{r['id']}")])
    if r["status"] == "open":
        rows.append([("Жабу", f"r:acl:{r['id']}")])
    return kb(*rows) if rows else None


def create_anonymous(conn, uid: int, category: str, description: str, photo_id: str | None,
                     want_replies: bool, now: datetime) -> tuple[str | None, list[Notice]]:
    """`uid` is used ONLY for the in-memory rate limit and is never stored."""
    if not anon_limiter.allowed(uid, now):
        raise PermissionError(f"Күніне ең көбі {ANON_DAILY_LIMIT} анонимді шағым жіберуге болады.")
    token = ("A-" + secrets.token_hex(4)).upper() if want_replies else None
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO anon_reports(category, description, photo_id, token_hash, status, created_at) "
            "VALUES (?, ?, ?, ?, 'open', ?)",
            # created_at is rounded to the hour so timing can't be matched to activity logs
            (category, description, photo_id, _hash_token(token) if token else None,
             to_db(now.replace(minute=0, second=0))),
        )
        r = conn.execute("SELECT * FROM anon_reports WHERE id = ?", (cur.lastrowid,)).fetchone()
        notices = users.notify_mentors(conn, anon_text(r), anon_buttons(r), silent=False, photo=photo_id)
    anon_limiter.hit(uid, now)
    return token, notices


def find_by_token(conn, token: str):
    return conn.execute("SELECT * FROM anon_reports WHERE token_hash = ?", (_hash_token(token),)).fetchone()


def anon_messages(conn, rid: int):
    return conn.execute("SELECT * FROM anon_messages WHERE report_id = ? ORDER BY id", (rid,)).fetchall()


def mentor_reply(conn, actor: int, rid: int, text: str, now: datetime) -> list[Notice]:
    with tx(conn):
        r = conn.execute("SELECT * FROM anon_reports WHERE id = ?", (rid,)).fetchone()
        if not r or not r["token_hash"] or r["status"] != "open":
            raise ValueError("Бұл шағымға жауап беру мүмкін емес.")
        conn.execute(
            "INSERT INTO anon_messages(report_id, sender, text, created_at) VALUES (?, 'mentor', ?, ?)",
            (rid, text, to_db(now)),
        )
        audit.log(conn, actor, "anon_reply", f"#{rid}")
        name = users.name_of(conn, actor)
        return [Notice(m["tg_id"], f"💬 {name} №{rid} анонимді шағымға жауап берді: {text}")
                for m in users.mentors(conn) if m["tg_id"] != actor]


def reporter_reply(conn, token: str, text: str, now: datetime) -> list[Notice]:
    with tx(conn):
        r = find_by_token(conn, token)
        if not r or r["status"] != "open":
            raise ValueError("Бұл сұхбат жабылған немесе код қате.")
        conn.execute(
            "INSERT INTO anon_messages(report_id, sender, text, created_at) VALUES (?, 'reporter', ?, ?)",
            (r["id"], text, to_db(now)),
        )
        return users.notify_mentors(
            conn, f"🕶 Анонимді шағым иесі (№{r['id']}) жазды: {text}", anon_buttons(r), silent=False
        )


def close_anonymous(conn, actor: int, rid: int) -> None:
    with tx(conn):
        conn.execute("UPDATE anon_reports SET status = 'closed' WHERE id = ?", (rid,))
        audit.log(conn, actor, "anon_close", f"#{rid}")


def open_anonymous(conn):
    return conn.execute("SELECT * FROM anon_reports WHERE status = 'open' ORDER BY id DESC LIMIT 20").fetchall()
