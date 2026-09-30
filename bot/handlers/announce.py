"""Announcements: everyone can read; mentors create, schedule, edit and pin."""
from datetime import timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ..notices import kb
from ..sender import deliver
from ..services import announcements, users
from ..services.announcements import TARGETS
from ..timeutil import at_local, fmt_dt, from_db, local_today, parse_user_datetime, to_db, utcnow
from .common import not_allowed, not_registered, show

router = Router(name="announce")


class AnnounceFlow(StatesGroup):
    title = State()
    body = State()
    photo = State()
    options = State()
    custom_time = State()
    edit_body = State()


@router.message(Command("announcements"))
@router.callback_query(F.data == "n:list")
async def list_recent(event: Message | CallbackQuery, user, conn):
    if not user:
        return await not_registered(event)
    items = [a for a in announcements.recent(conn, 15) if announcements.visible_to(a, user)][:5]
    if not items:
        text = "📢 No announcements yet."
    else:
        text = "\n\n".join(f"🗓 {fmt_dt(from_db(a['sent_at']))}\n{announcements.text_of(a)}" for a in items)
    await show(event, text[:4000], kb([("⬅️ Menu", "menu")]))


@router.message(Command("announce"))
@router.callback_query(F.data == "n:new")
async def new(event: Message | CallbackQuery, user, state: FSMContext):
    if not users.is_mentor(user):
        return await not_allowed(event)
    await state.clear()
    await state.set_state(AnnounceFlow.title)
    await show(event, "📣 New announcement. What's the title? /cancel to stop.")


@router.message(AnnounceFlow.title, F.text)
async def got_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip()[:120])
    await state.set_state(AnnounceFlow.body)
    await message.answer("Now the text of the announcement:")


@router.message(AnnounceFlow.body, F.text)
async def got_body(message: Message, state: FSMContext):
    await state.update_data(body=message.text.strip()[:3000])
    await state.set_state(AnnounceFlow.photo)
    await message.answer("Add a photo? Send it now, or tap Skip.", reply_markup=kb([("Skip", "n:nophoto")]))


@router.message(AnnounceFlow.photo, F.photo)
async def got_photo(message: Message, state: FSMContext):
    await state.update_data(photo=message.photo[-1].file_id)
    await _ask_target(message, state)


@router.callback_query(AnnounceFlow.photo, F.data == "n:nophoto")
async def no_photo(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await _ask_target(cb.message, state)


async def _ask_target(message: Message, state: FSMContext):
    await state.set_state(AnnounceFlow.options)
    await message.answer("Who should get it?", reply_markup=kb([(label, f"n:t:{code}") for code, label in TARGETS.items()]))


@router.callback_query(AnnounceFlow.options, F.data.startswith("n:t:"))
async def got_target(cb: CallbackQuery, state: FSMContext):
    await state.update_data(target=cb.data.split(":")[2])
    await show(cb, "📌 Pin it?", kb([("Pin", "n:p:1"), ("Don't pin", "n:p:0")]))


@router.callback_query(AnnounceFlow.options, F.data.startswith("n:p:"))
async def got_pin(cb: CallbackQuery, state: FSMContext):
    await state.update_data(pin=cb.data.endswith(":1"))
    await show(cb, "🔔 Loud (with sound) or silent?", kb([("🔕 Silent", "n:s:1"), ("🔔 Loud (urgent)", "n:s:0")]))


@router.callback_query(AnnounceFlow.options, F.data.startswith("n:s:"))
async def got_silent(cb: CallbackQuery, state: FSMContext):
    await state.update_data(silent=cb.data.endswith(":1"))
    await show(cb, "🕒 When should it go out?", kb(
        [("Now", "n:w:now")],
        [("Tonight 22:00", "n:w:tonight"), ("Tomorrow 08:00", "n:w:tomorrow")],
        [("Pick date & time…", "n:w:custom")],
    ))


@router.callback_query(AnnounceFlow.options, F.data.startswith("n:w:"))
async def got_when(cb: CallbackQuery, state: FSMContext):
    choice = cb.data.split(":")[2]
    now = utcnow()
    today = local_today(now)
    if choice == "custom":
        await state.set_state(AnnounceFlow.custom_time)
        await show(cb, "Send the date and time, e.g. 25.12 18:00 or 2026-12-25 18:00 (Hong Kong time).")
        return
    when = {"now": now, "tonight": at_local(today, "22:00"), "tomorrow": at_local(today + timedelta(days=1), "08:00")}[choice]
    if when < now:
        when = now
    await state.update_data(send_at=to_db(when))
    await _preview(cb.message, state)
    await cb.answer()


@router.message(AnnounceFlow.custom_time, F.text)
async def got_custom_time(message: Message, state: FSMContext):
    when = parse_user_datetime(message.text, utcnow())
    if not when or when < utcnow() - timedelta(minutes=1):
        await message.answer("I couldn't read that (or it's in the past). Try e.g. 25.12 18:00")
        return
    await state.update_data(send_at=to_db(when))
    await state.set_state(AnnounceFlow.options)
    await _preview(message, state)


async def _preview(message: Message, state: FSMContext):
    d = await state.get_data()
    when = from_db(d["send_at"])
    info = (f"To: {TARGETS[d['target']]} · {'📌 pinned' if d['pin'] else 'not pinned'} · "
            f"{'🔕 silent' if d['silent'] else '🔔 loud'} · {'now' if when <= utcnow() else fmt_dt(when)}"
            + (" · with photo" if d.get("photo") else ""))
    await message.answer(f"Preview:\n\n📢 {d['title']}\n\n{d['body']}\n\n{info}",
                         reply_markup=kb([("✅ Send", "n:go"), ("❌ Discard", "menu")]))


@router.callback_query(AnnounceFlow.options, F.data == "n:go")
async def send(cb: CallbackQuery, state: FSMContext, conn, bot, user):
    d = await state.get_data()
    await state.clear()
    now = utcnow()
    ann_id = announcements.create(conn, user["tg_id"], d["title"], d["body"], d.get("photo"), d["target"], d["pin"],
                                  d["silent"], from_db(d["send_at"]), now)
    if from_db(d["send_at"]) <= now:
        await show(cb, "📤 Sending…")
        notices = announcements.tick(conn, now)
        sent = await deliver(bot, conn, notices)
        await cb.message.answer(f"✅ Announcement #{ann_id} delivered to {sent} chats.", reply_markup=kb([("⬅️ Menu", "menu")]))
    else:
        await show(cb, f"🕒 Scheduled for {fmt_dt(from_db(d['send_at']))}.", kb([("⬅️ Menu", "menu")]))


# ---------------------------------------------------------------- admin: manage

@router.callback_query(F.data == "a:n")
async def manage(cb: CallbackQuery, user, conn):
    if not users.is_mentor(user):
        return await not_allowed(cb)
    rows = [[("🕒 " + announcements.label(a), f"a:nv:{a['id']}")] for a in announcements.scheduled(conn)]
    rows += [[("📢 " + announcements.label(a), f"a:nv:{a['id']}")] for a in announcements.recent(conn, 8)]
    rows += [[("📣 New announcement", "n:new")], [("⬅️ Admin", "a:menu")]]
    await show(cb, "📢 Announcements (🕒 scheduled, 📢 sent):", kb(*rows))


@router.callback_query(F.data.startswith("a:nv:"))
async def view(cb: CallbackQuery, user, conn):
    if not users.is_mentor(user):
        return await not_allowed(cb)
    a = announcements.get(conn, int(cb.data.split(":")[2]))
    rows = [[("✏️ Edit text", f"a:ne:{a['id']}")]]
    if a["sent_at"]:
        if not a["pin"]:
            rows.append([("📌 Pin everywhere", f"a:np:{a['id']}")])
    else:
        rows.append([("🗑 Cancel scheduled", f"a:nx:{a['id']}")])
    rows.append([("⬅️ Back", "a:n")])
    await show(cb, announcements.text_of(a) + "\n\n" + announcements.label(a), kb(*rows))


@router.callback_query(F.data.startswith("a:nx:"))
async def cancel_scheduled(cb: CallbackQuery, user, conn):
    if not users.is_mentor(user):
        return await not_allowed(cb)
    announcements.cancel(conn, user["tg_id"], int(cb.data.split(":")[2]))
    await show(cb, "🗑 Scheduled announcement cancelled.", kb([("⬅️ Back", "a:n")]))


@router.callback_query(F.data.startswith("a:np:"))
async def pin(cb: CallbackQuery, user, conn, bot):
    if not users.is_mentor(user):
        return await not_allowed(cb)
    ann_id = int(cb.data.split(":")[2])
    announcements.set_pinned(conn, user["tg_id"], ann_id)
    for m in announcements.messages(conn, ann_id):
        try:
            await bot.pin_chat_message(m["chat_id"], m["message_id"], disable_notification=True)
        except Exception:
            pass
    await show(cb, "📌 Pinned.", kb([("⬅️ Back", "a:n")]))


@router.callback_query(F.data.startswith("a:ne:"))
async def edit_ask(cb: CallbackQuery, user, state: FSMContext):
    if not users.is_mentor(user):
        return await not_allowed(cb)
    await state.set_state(AnnounceFlow.edit_body)
    await state.update_data(ann_id=int(cb.data.split(":")[2]))
    await show(cb, "Send the new text (the title stays the same). /cancel to stop.")


@router.message(AnnounceFlow.edit_body, F.text)
async def edit_save(message: Message, state: FSMContext, conn, bot, user):
    ann_id = (await state.get_data())["ann_id"]
    await state.clear()
    announcements.update_body(conn, user["tg_id"], ann_id, message.text.strip()[:3000])
    a = announcements.get(conn, ann_id)
    text = announcements.text_of(a) + "\n\n(edited)"
    edited = 0
    for m in announcements.messages(conn, ann_id):
        try:
            if m["has_photo"]:
                await bot.edit_message_caption(chat_id=m["chat_id"], message_id=m["message_id"], caption=text[:1024])
            else:
                await bot.edit_message_text(text, chat_id=m["chat_id"], message_id=m["message_id"])
            edited += 1
        except Exception:
            pass
    await message.answer(f"✅ Updated ({edited} delivered messages edited).", reply_markup=kb([("⬅️ Back", "a:n")]))
