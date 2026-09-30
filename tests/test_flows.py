"""End-to-end smoke test: drives the real aiogram dispatcher with fake Telegram
updates and a fake HTTP session, tapping through every menu. Fails on any
handler exception."""
import logging
from datetime import datetime, timedelta, timezone
from itertools import count

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, PhotoSize, Update, User

from bot import main as app
from bot.config import CONFIG
from bot.services import reports as report_service
from bot.timeutil import dkey, local_today, utcnow
from tests.conftest import MENTORS, STUDENTS

TOKEN = "123456:TEST-token-not-real"
_ids = count(1)


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls: list[TelegramMethod] = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        rt = method.__returning__
        if rt is Message or getattr(rt, "__name__", "") == "Message" or "Message" in str(rt) and "bool" in str(rt):
            chat_id = getattr(method, "chat_id", None) or 1
            return Message(message_id=next(_ids), date=datetime.now(timezone.utc),
                           chat=Chat(id=chat_id, type="private"), text=getattr(method, "text", None) or "x")
        return True

    async def close(self):
        pass

    async def stream_content(self, *a, **k):
        yield b""

    def texts(self, chat_id=None):
        return [getattr(m, "text", None) or getattr(m, "caption", None) or ""
                for m in self.calls if chat_id is None or getattr(m, "chat_id", None) == chat_id]

    def alerts(self):
        return [m.text for m in self.calls if isinstance(m, AnswerCallbackQuery) and m.text]


class Harness:
    def __init__(self, conn):
        self.session = FakeSession()
        self.bot = Bot(TOKEN, session=self.session)
        from bot.handlers import admin, announce, common, duty, events, guests, reports
        from bot.handlers import washer as washer_h
        for mod in (admin, announce, duty, events, guests, reports, washer_h):
            mod.router._parent_router = None   # module-level routers: detach from the previous test's dispatcher
        common.router._parent_router = None
        common.fallback_router._parent_router = None
        self.dp = app.build_dispatcher(conn)
        self.conn = conn

    def _user(self, uid):
        return User(id=uid, is_bot=False, first_name=f"U{uid}")

    def _msg(self, uid, text=None, photo=False):
        kw = {}
        if photo:
            kw["photo"] = [PhotoSize(file_id="PHOTO", file_unique_id="u", width=1, height=1)]
            kw["caption"] = text
        else:
            kw["text"] = text
        return Message(message_id=next(_ids), date=datetime.now(timezone.utc), chat=Chat(id=uid, type="private"),
                       from_user=self._user(uid), **kw)

    async def say(self, uid, text, photo=False):
        await self.dp.feed_update(self.bot, Update(update_id=next(_ids), message=self._msg(uid, text, photo)), conn=self.conn)

    async def tap(self, uid, data):
        cb = CallbackQuery(id=str(next(_ids)), from_user=self._user(uid), chat_instance="c", data=data,
                           message=self._msg(uid, "menu"))
        await self.dp.feed_update(self.bot, Update(update_id=next(_ids), callback_query=cb), conn=self.conn)

    def last(self, uid):
        t = self.session.texts(uid)
        return t[-1] if t else ""


@pytest.fixture
def h(conn, monkeypatch):
    monkeypatch.setattr(report_service, "anon_limiter", report_service._AnonRateLimiter())
    harness = Harness(conn)
    yield harness
    # every tap must reach a real handler, never the "button expired" fallback
    assert not [a for a in harness.session.alerts() if "мерзімі өтті" in a]
    assert not [t for t in harness.session.texts() if "Жоғарыдағы сұраққа жауап беріңіз" in t]


@pytest.fixture(autouse=True)
def no_handler_errors(caplog):
    with caplog.at_level(logging.ERROR):
        yield
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors, [r.getMessage() for r in errors] + [str(r.exc_info) for r in errors]


async def test_onboarding_new_student(h, monkeypatch):
    uid = 999
    await h.say(uid, "/start")
    assert "Атыңыз кім" in h.last(uid)
    await h.say(uid, "Aida")
    await h.tap(uid, "on:role:mentor")      # not whitelisted -> student
    row = h.conn.execute("SELECT * FROM users WHERE tg_id = ?", (uid,)).fetchone()
    assert row["name"] == "Aida" and row["role"] == "student"
    assert any("Жаңа студент тіркелді" in t for t in h.session.texts(MENTORS[0]))


async def test_whitelisted_mentor_onboarding(h, monkeypatch):
    monkeypatch.setattr(CONFIG, "mentor_ids", {777})
    await h.say(777, "/start")
    await h.say(777, "Marat")
    await h.tap(777, "on:role:mentor")
    assert h.conn.execute("SELECT role FROM users WHERE tg_id = 777").fetchone()[0] == "mentor"


async def test_unregistered_cannot_tap_buttons(h):
    await h.tap(5555, "w:days")
    assert "Алдымен /start жіберіңіз." in h.session.alerts()


async def test_student_washer_flow_all_buttons(h):
    s = STUDENTS[0]
    tomorrow = dkey(local_today(utcnow()) + timedelta(days=1))
    await h.say(s, "/start")
    assert "Не істегіңіз келеді" in h.last(s)
    await h.tap(s, "w:days")
    await h.tap(s, f"w:day:{tomorrow}")
    await h.tap(s, f"w:slot:{tomorrow}:0930")
    await h.tap(s, f"w:cyc:{tomorrow}:0930:normal")
    assert "Брондалды" in h.last(s)
    await h.tap(STUDENTS[1], f"w:cyc:{tomorrow}:0930:quick")
    assert "Жаңа ғана біреу алып қойды" in h.last(STUDENTS[1])
    await h.tap(STUDENTS[1], f"w:wl:{tomorrow}:0930")
    assert "№1 орындасыз" in h.last(STUDENTS[1])
    await h.tap(s, "w:my")
    bid = h.conn.execute("SELECT id FROM bookings WHERE user_id = ?", (s,)).fetchone()[0]
    await h.tap(s, f"w:st:{bid}")          # too early -> alert
    await h.tap(s, f"w:cx:{bid}")
    await h.tap(s, f"w:cxy:{bid}")
    assert any("слоты босады" in t for t in h.session.texts(STUDENTS[1]))
    wl = h.conn.execute("SELECT id FROM waitlist WHERE user_id = ?", (STUDENTS[1],)).fetchone()[0]
    await h.tap(STUDENTS[1], f"w:cl:{wl}:normal")
    assert "Слот сіздікі" in h.last(STUDENTS[1])
    for data in ["w:rec", "w:recd:6", "w:recd:6", "w:rect:6:1000", "w:recc:6:1000:quick"]:
        await h.tap(s, data)
    assert any("апта сайынғы" in t for t in h.session.texts(MENTORS[0]))
    await h.say(s, "/book")
    await h.say(s, "/mybookings")


async def test_reports_flows(h):
    s = STUDENTS[2]
    await h.say(s, "/report")
    await h.tap(s, "r:m")
    await h.tap(s, "r:ma:1")
    await h.say(s, "Fridge is leaking", photo=True)
    await h.tap(s, "r:mname:1")
    assert any("Ас үй" in t for t in h.session.texts(MENTORS[0]))
    # duplicate within 2h
    await h.tap(STUDENTS[3], "r:ma:1")
    await h.say(STUDENTS[3], "fridge again")
    assert "бұрын хабарланған" in h.last(STUDENTS[3])
    await h.tap(STUDENTS[3], "r:mdup:0")
    rid = h.conn.execute("SELECT id FROM reports").fetchone()[0]
    await h.tap(MENTORS[0], f"r:ms:{rid}:fixed")
    assert any("жөнделді" in t for t in h.session.texts(s))
    # anonymous with replies
    await h.tap(s, "r:a")
    await h.tap(s, "r:ac:0")
    await h.say(s, "Music at 3am every night")
    await h.tap(s, "r:ao:1")
    token = next(t for t in h.session.texts(s) if "Құпия кодыңыз: " in t).split("Құпия кодыңыз: ")[1].split()[0]
    aid = h.conn.execute("SELECT id FROM anon_reports").fetchone()[0]
    await h.tap(MENTORS[1], f"r:ar:{aid}")
    await h.say(MENTORS[1], "Рақмет, шешеміз")
    await h.tap(s, "r:ak")
    await h.say(s, token)
    assert "Рақмет, шешеміз" in h.last(s)
    await h.tap(s, "r:akr")
    await h.say(s, "Thank you")
    await h.tap(MENTORS[1], f"r:acl:{aid}")
    assert h.conn.execute("SELECT status FROM anon_reports").fetchone()[0] == "closed"


async def test_duty_flows(h):
    from bot.services import duty
    duty.tick(h.conn, utcnow())
    s = STUDENTS[0]
    await h.say(s, "/duty")
    await h.say(s, "/low")
    await h.tap(s, "d:lowi:0")
    await h.tap(s, "d:hist")
    d = h.conn.execute("SELECT * FROM duty WHERE kind = 'buyer'").fetchone()
    if d:
        await h.tap(d["user_id"], f"d:blist:{d['id']}")
        item = h.conn.execute("SELECT id FROM supplies").fetchone()[0]
        await h.tap(d["user_id"], f"d:buy:{item}")
        await h.tap(d["user_id"], f"d:rcpt:{d['id']}")
        await h.say(d["user_id"], "", photo=True)
    c = h.conn.execute("SELECT * FROM duty WHERE kind = 'cleaning'").fetchone()
    if c:
        await h.tap(c["user_id"], f"d:done:{c['id']}")


async def test_announcement_flow(h):
    m = MENTORS[0]
    await h.tap(STUDENTS[0], "n:new")
    assert "Тек тәлімгерлерге арналған." in h.session.alerts()
    await h.say(m, "/announce")
    await h.say(m, "Water off")
    await h.say(m, "No water 10-12 tomorrow")
    await h.tap(m, "n:nophoto")
    await h.tap(m, "n:t:all")
    await h.tap(m, "n:p:1")
    await h.tap(m, "n:s:0")
    await h.tap(m, "n:w:now")
    await h.tap(m, "n:go")
    assert sum("Water off" in t for t in h.session.texts()) >= 17
    await h.say(STUDENTS[4], "/announcements")
    assert "Water off" in h.last(STUDENTS[4])
    aid = h.conn.execute("SELECT id FROM announcements").fetchone()[0]
    await h.tap(m, "a:n")
    await h.tap(m, f"a:nv:{aid}")
    await h.tap(m, f"a:ne:{aid}")
    await h.say(m, "Updated text")
    # scheduled
    await h.say(m, "/announce")
    await h.say(m, "Later")
    await h.say(m, "body")
    await h.tap(m, "n:nophoto")
    await h.tap(m, "n:t:students")
    await h.tap(m, "n:p:0")
    await h.tap(m, "n:s:1")
    await h.tap(m, "n:w:custom")
    await h.say(m, "25.12 18:00")
    await h.tap(m, "n:go")
    assert "уақытына жоспарланды" in h.last(m)


async def test_guest_flow(h):
    s = STUDENTS[5]
    today = dkey(local_today(utcnow()))
    await h.say(s, "/guests")
    await h.tap(s, "g:new")
    await h.say(s, "Dana")
    await h.tap(s, f"g:d:{today}")
    await h.tap(s, "g:t:now")
    await h.tap(s, "g:dur:1n")
    gid = h.conn.execute("SELECT id FROM guests").fetchone()[0]
    await h.tap(MENTORS[0], f"g:rj:{gid}")
    await h.say(MENTORS[0], "Бүгін орын жоқ")
    assert any("Бүгін орын жоқ" in t for t in h.session.texts(s))
    await h.tap(MENTORS[0], "g:today")
    await h.tap(MENTORS[0], "a:g")
    await h.tap(s, "g:new")
    await h.say(s, "Eli")
    await h.tap(s, f"g:d:{today}")
    await h.tap(s, "g:t:now")
    await h.tap(s, "g:dur:2h")
    gid = h.conn.execute("SELECT MAX(id) FROM guests").fetchone()[0]
    assert "Рұқсат сұралды" in h.last(s)
    assert any("қонақ шақыруға рұқсат сұрайды" in t for t in h.session.texts(MENTORS[1]))
    await h.tap(MENTORS[1], f"g:ap:{gid}")
    assert any("рұқсат берілді" in t for t in h.session.texts(s))
    await h.tap(s, f"g:out:{gid}")


async def test_event_flow(h):
    s = STUDENTS[6]
    day = dkey(local_today(utcnow()) + timedelta(days=2))
    await h.say(s, "/events")
    await h.tap(s, "e:new")
    await h.say(s, "Movie night")
    await h.tap(s, "e:skip")
    await h.tap(s, "e:loc:Ортақ бөлме")
    await h.tap(s, f"e:d:{day}")
    await h.say(s, "19:30")
    await h.tap(s, "e:rec:1")
    eid = h.conn.execute("SELECT id FROM events").fetchone()[0]
    await h.tap(STUDENTS[0], f"e:r:{eid}:going")
    await h.tap(STUDENTS[0], f"e:v:{eid}")
    await h.tap(STUDENTS[0], f"e:ed:{eid}")      # not allowed -> alert
    await h.tap(s, f"e:ed:{eid}")
    await h.tap(s, f"e:ef:{eid}:title")
    await h.say(s, "Film night")
    await h.tap(MENTORS[0], f"e:ef:{eid}:starts_at")
    await h.say(MENTORS[0], "31.12 20:00")
    await h.tap(MENTORS[0], f"e:del:{eid}")
    await h.tap(MENTORS[0], f"e:dely:{eid}")
    assert h.conn.execute("SELECT deleted FROM events").fetchone()[0] == 1


async def test_admin_panel_every_button(h):
    m = MENTORS[0]
    s = STUDENTS[0]
    day = dkey(local_today(utcnow()) + timedelta(days=1))
    await h.say(s, "/admin")
    assert "Тек тәлімгерлерге арналған." in h.last(s)
    for data in ["a:menu", "a:w", f"a:wd:{day}", f"a:ws:{day}:0800", f"a:wfb:{day}:0800", f"a:wfu:{day}:0800:{s}"]:
        await h.tap(m, data)
    bid = h.conn.execute("SELECT id FROM bookings").fetchone()[0]
    for data in [f"a:ws:{day}:0800", f"a:wff:{bid}", f"a:wffy:{bid}", "a:rec", "a:d", "a:dgen", "a:dp",
                 f"a:dpu:{s}", f"a:dpw:{s}:1", f"a:dpw:{s}:0", "a:r", "a:ra", "a:n", "a:g", "a:u", f"a:uv:{s}",
                 f"a:urole:{s}", f"a:urole:{s}", f"a:umute:{s}", f"a:umute:{s}", f"a:uban:{s}", f"a:uunban:{s}",
                 f"a:udeact:{s}", f"a:udeacty:{s}", f"a:uact:{s}", "a:s", "a:log"]:
        await h.tap(m, data)
    d = h.conn.execute("SELECT id FROM duty LIMIT 1").fetchone()
    if d:
        await h.tap(m, f"a:dra:{d[0]}")
        await h.tap(m, f"a:drau:{d[0]}:{STUDENTS[3]}")
    await h.say(m, "/washer_maintenance")
    await h.say(m, "Technician visit")
    assert h.conn.execute("SELECT COUNT(*) FROM washer_blocks WHERE lifted_at IS NULL").fetchone()[0] == 1
    await h.tap(m, "a:wml")
    await h.tap(m, "a:uadd")
    await h.say(m, "424242 Zara")
    await h.tap(m, "a:uaddr:student")
    assert h.conn.execute("SELECT name FROM users WHERE tg_id = 424242").fetchone()[0] == "Zara"
    await h.say(m, "/stats")
    actions = {r[0] for r in h.conn.execute("SELECT action FROM audit")}
    assert {"washer_force_book", "washer_force_free", "pause", "set_role", "mute", "washer_ban", "deactivate",
            "washer_maintenance_on", "add_user"} <= actions


async def test_muted_user_is_blocked(h):
    from bot.services import users
    users.set_muted(h.conn, MENTORS[0], STUDENTS[9], True)
    await h.say(STUDENTS[9], "/book")
    assert "шектеу қойылған" in h.last(STUDENTS[9])


async def test_fallback_text(h):
    await h.say(STUDENTS[0], "hello?")
    assert "батырмаларды пайдаланыңыз" in h.last(STUDENTS[0])
