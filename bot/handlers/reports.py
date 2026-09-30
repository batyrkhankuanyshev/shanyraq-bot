"""/report: maintenance reports and anonymous reports.

Anonymous handlers must never log or store the sender's ID. The draft lives only
in the in-memory FSM storage and is cleared as soon as the report is sent."""
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ..notices import grid, kb
from ..sender import deliver
from ..services import reports, users
from ..services.reports import ANON_CATEGORIES, AREAS
from ..timeutil import from_db, utcnow
from .common import not_allowed, not_registered, show

router = Router(name="reports")


class ReportFlow(StatesGroup):
    m_desc = State()
    a_desc = State()
    a_token = State()
    a_reply = State()
    mentor_reply = State()


@router.message(Command("report"))
@router.callback_query(F.data == "r:menu")
async def menu(event: Message | CallbackQuery, user, state: FSMContext):
    if not user:
        return await not_registered(event)
    await state.clear()
    rows = [
        [("🛠 Maintenance", "r:m")],
        [("🕶 Anonymous report", "r:a")],
        [("💬 Check anonymous replies", "r:ak")],
        [("⬅️ Menu", "menu")],
    ]
    if users.is_mentor(user):
        rows.insert(3, [("📂 Open reports (mentors)", "a:r")])
    await show(event, "🚨 What would you like to report?", kb(*rows))


# ---------------------------------------------------------------- maintenance

@router.callback_query(F.data == "r:m")
async def m_area(cb: CallbackQuery):
    rows = grid([(a, f"r:ma:{i}") for i, a in enumerate(AREAS)], 2)
    rows.append([("⬅️ Back", "r:menu")])
    await show(cb, "🛠 What needs fixing?", kb(*rows))


@router.callback_query(F.data.startswith("r:ma:"))
async def m_ask_desc(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ReportFlow.m_desc)
    await state.update_data(area=AREAS[int(cb.data.split(":")[2])])
    await show(cb, "Describe the problem in a message. You can attach a photo (add the description as its caption).\n/cancel to stop.")


def _text_and_photo(message: Message) -> tuple[str, str | None]:
    if message.photo:
        return (message.caption or "").strip(), message.photo[-1].file_id
    return (message.text or "").strip(), None


@router.message(ReportFlow.m_desc, F.text | F.photo)
async def m_got_desc(message: Message, state: FSMContext, conn):
    text, photo = _text_and_photo(message)
    if not text:
        await message.answer("Please add a short description (as text or as the photo's caption).")
        return
    await state.update_data(desc=text[:1500], photo=photo)
    area = (await state.get_data())["area"]
    dup = reports.recent_duplicate(conn, area, utcnow())
    if dup:
        mins = int((utcnow() - from_db(dup["created_at"])).total_seconds() // 60)
        ago = f"{mins} min" if mins < 60 else f"{mins // 60}h {mins % 60}m"
        await message.answer(
            f"ℹ️ A {area} problem was reported {ago} ago:\n“{dup['description'][:200]}”\nIs yours a different/still an issue?",
            reply_markup=kb([("Yes, report it", "r:mdup:1"), ("No, it's the same", "r:mdup:0")]),
        )
        return
    await _ask_name(message)


async def _ask_name(message: Message):
    await message.answer(
        "Should mentors see your name? (If yes, I'll DM you when it's fixed.)",
        reply_markup=kb([("Yes, include my name", "r:mname:1"), ("Stay anonymous", "r:mname:0")]),
    )


@router.callback_query(ReportFlow.m_desc, F.data.startswith("r:mdup:"))
async def m_dup(cb: CallbackQuery, state: FSMContext):
    if cb.data.endswith(":0"):
        await state.clear()
        await show(cb, "👍 Thanks — mentors already know about it.", kb([("⬅️ Menu", "menu")]))
        return
    await cb.answer()
    await _ask_name(cb.message)


@router.callback_query(ReportFlow.m_desc, F.data.startswith("r:mname:"))
async def m_submit(cb: CallbackQuery, state: FSMContext, conn, bot, user):
    data = await state.get_data()
    await state.clear()
    rid, notices = reports.create_maintenance(
        conn, user["tg_id"], data["area"], data["desc"], data.get("photo"), utcnow(), anonymous=cb.data.endswith(":0"),
    )
    await show(cb, f"✅ Report #{rid} sent. Mentors have been notified.", kb([("⬅️ Menu", "menu")]))
    await deliver(bot, conn, notices)


@router.callback_query(F.data.startswith("r:ms:"))
async def m_status(cb: CallbackQuery, user, conn, bot):
    if not users.is_mentor(user):
        return await not_allowed(cb)
    _, _, rid, status = cb.data.split(":")
    notices = reports.set_status(conn, user["tg_id"], int(rid), status, utcnow())
    r = conn.execute("SELECT * FROM reports WHERE id = ?", (int(rid),)).fetchone()
    await cb.answer(f"Marked {reports.STATUSES[r['status']]}")
    try:
        markup = None if r["status"] == "fixed" else reports.status_buttons(r["id"])
        if cb.message.photo:
            await cb.message.edit_caption(caption=reports.report_text(conn, r), reply_markup=markup)
        else:
            await cb.message.edit_text(reports.report_text(conn, r), reply_markup=markup)
    except Exception:
        pass
    await deliver(bot, conn, notices)


# ---------------------------------------------------------------- anonymous

@router.callback_query(F.data == "r:a")
async def a_category(cb: CallbackQuery):
    rows = grid([(c, f"r:ac:{i}") for i, c in enumerate(ANON_CATEGORIES)], 2)
    rows.append([("⬅️ Back", "r:menu")])
    await show(cb, "🕶 Anonymous report\nYour identity is never stored — not in the database, not in logs. "
                   "Mentors only see the category and what you write.\n\nWhat is it about?", kb(*rows))


@router.callback_query(F.data.startswith("r:ac:"))
async def a_ask_desc(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ReportFlow.a_desc)
    await state.update_data(category=ANON_CATEGORIES[int(cb.data.split(":")[2])])
    await show(cb, "Describe what's happening. A photo is optional (put the text in its caption).\n"
                   "Tip: avoid details that would reveal who you are. /cancel to stop.")


@router.message(ReportFlow.a_desc, F.text | F.photo)
async def a_got_desc(message: Message, state: FSMContext):
    text, photo = _text_and_photo(message)
    if not text:
        await message.answer("Please add a short description.")
        return
    await state.update_data(desc=text[:1500], photo=photo)
    await message.answer(
        "Would you like to be able to receive anonymous replies from mentors? "
        "You'll get a secret code to check replies — nobody (not even the bot) links it to you.",
        reply_markup=kb([("Yes, give me a code", "r:ao:1"), ("No, just send it", "r:ao:0")]),
    )


@router.callback_query(ReportFlow.a_desc, F.data.startswith("r:ao:"))
async def a_submit(cb: CallbackQuery, state: FSMContext, conn, bot):
    data = await state.get_data()
    await state.clear()
    try:
        token, notices = reports.create_anonymous(
            conn, cb.from_user.id, data["category"], data["desc"], data.get("photo"), cb.data.endswith(":1"), utcnow(),
        )
    except PermissionError as e:
        await show(cb, f"😕 {e}", kb([("⬅️ Menu", "menu")]))
        return
    text = "✅ Sent anonymously to the mentors. Thank you for speaking up."
    if token:
        text += (f"\n\nYour secret code: {token}\nSave it! Use Report → Check anonymous replies and enter it "
                 "to read mentor replies. If you lose it, replies can't be recovered.")
    await show(cb, text, kb([("⬅️ Menu", "menu")]))
    await deliver(bot, conn, notices)


@router.callback_query(F.data == "r:ak")
async def a_ask_token(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ReportFlow.a_token)
    await show(cb, "Send your secret code (looks like A-1F2E3D4C). /cancel to stop.")


def _thread_text(conn, r) -> str:
    return reports.anon_text(r, reports.anon_messages(conn, r["id"]))


@router.message(ReportFlow.a_token, F.text)
async def a_check(message: Message, state: FSMContext, conn):
    r = reports.find_by_token(conn, message.text)
    if not r:
        await message.answer("I couldn't find that code. Check it and try again, or /cancel.")
        return
    await state.update_data(token=message.text.strip())
    await state.set_state(None)
    markup = kb([("💬 Reply", "r:akr")], [("⬅️ Menu", "menu")]) if r["status"] == "open" else kb([("⬅️ Menu", "menu")])
    try:
        await message.delete()  # don't leave the code visible in the chat
    except Exception:
        pass
    await message.answer(_thread_text(conn, r), reply_markup=markup)


@router.callback_query(F.data == "r:akr")
async def a_reply_ask(cb: CallbackQuery, state: FSMContext):
    if not (await state.get_data()).get("token"):
        await cb.answer("Please enter your code again.", show_alert=True)
        return
    await state.set_state(ReportFlow.a_reply)
    await show(cb, "Write your reply (it stays anonymous). /cancel to stop.")


@router.message(ReportFlow.a_reply, F.text)
async def a_reply_send(message: Message, state: FSMContext, conn, bot):
    token = (await state.get_data()).get("token", "")
    await state.clear()
    try:
        notices = reports.reporter_reply(conn, token, message.text[:1500], utcnow())
    except ValueError as e:
        await message.answer(str(e))
        return
    await message.answer("✅ Sent anonymously.", reply_markup=kb([("⬅️ Menu", "menu")]))
    await deliver(bot, conn, notices)


@router.callback_query(F.data.startswith("r:ar:"))
async def mentor_reply_ask(cb: CallbackQuery, state: FSMContext, user):
    if not users.is_mentor(user):
        return await not_allowed(cb)
    await state.set_state(ReportFlow.mentor_reply)
    await state.update_data(rid=int(cb.data.split(":")[2]))
    await cb.answer()
    await cb.message.answer("Write your reply to the anonymous reporter. They'll see it when they check with their code. /cancel to stop.")


@router.message(ReportFlow.mentor_reply, F.text)
async def mentor_reply_send(message: Message, state: FSMContext, conn, bot, user):
    rid = (await state.get_data())["rid"]
    await state.clear()
    try:
        notices = reports.mentor_reply(conn, user["tg_id"], rid, message.text[:1500], utcnow())
    except ValueError as e:
        await message.answer(str(e))
        return
    await message.answer("✅ Reply saved. The reporter will see it next time they check.")
    await deliver(bot, conn, notices)


@router.callback_query(F.data.startswith("r:acl:"))
async def anon_close(cb: CallbackQuery, conn, user):
    if not users.is_mentor(user):
        return await not_allowed(cb)
    rid = int(cb.data.split(":")[2])
    reports.close_anonymous(conn, user["tg_id"], rid)
    await cb.answer("Closed.")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
