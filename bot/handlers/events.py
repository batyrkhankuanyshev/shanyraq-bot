"""Events calendar: list, view + RSVP, create, edit, delete."""
from datetime import timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ..notices import grid, kb
from ..sender import deliver
from ..services import events
from ..timeutil import at_local, day_label, dkey, fmt_dt, from_db, local, local_today, parse_dkey, parse_hhmm, to_db, utcnow
from .common import not_registered, show

router = Router(name="events")
TIMES = ["10:00", "12:00", "15:00", "18:00", "19:00", "20:00", "21:00"]


class EventFlow(StatesGroup):
    title = State()
    description = State()
    location = State()
    when = State()
    time = State()
    edit = State()


@router.message(Command("events"))
@router.callback_query(F.data == "e:list")
async def list_events(event: Message | CallbackQuery, user, conn, state: FSMContext):
    if not user:
        return await not_registered(event)
    await state.clear()
    items = events.upcoming(conn, utcnow())
    rows = []
    lines = ["📅 Upcoming events:"]
    for e in items:
        c = events.counts(conn, e["id"])
        lines.append(f"• {fmt_dt(from_db(e['starts_at']))} — {e['title']} (going {c['going']}, maybe {c['maybe']})")
        rows.append([(f"{local(from_db(e['starts_at'])).strftime('%d %b')} · {e['title'][:30]}", f"e:v:{e['id']}")])
    if not items:
        lines.append("Nothing planned yet — create one!")
    rows += [[("➕ Create event", "e:new")], [("⬅️ Menu", "menu")]]
    await show(event, "\n".join(lines), kb(*rows))


def _view_markup(e, user):
    rows = [[(label, f"e:r:{e['id']}:{code}") for code, label in events.RSVP.items()]]
    if events.can_edit(e, user):
        rows.append([("✏️ Edit", f"e:ed:{e['id']}"), ("🗑 Delete", f"e:del:{e['id']}")])
    rows.append([("⬅️ Events", "e:list")])
    return kb(*rows)


@router.callback_query(F.data.startswith("e:v:"))
async def view(cb: CallbackQuery, user, conn):
    e = events.get(conn, int(cb.data.split(":")[2]))
    if not e or e["deleted"]:
        return await cb.answer("This event no longer exists.", show_alert=True)
    mine = conn.execute("SELECT status FROM rsvps WHERE event_id = ? AND user_id = ?", (e["id"], user["tg_id"])).fetchone()
    extra = f"\nYour answer: {events.RSVP[mine['status']]}" if mine else ""
    await show(cb, events.text_of(conn, e) + extra, _view_markup(e, user))


@router.callback_query(F.data.startswith("e:r:"))
async def rsvp(cb: CallbackQuery, user, conn):
    if not user:
        return await not_registered(cb)
    _, _, eid, status = cb.data.split(":")
    text = events.set_rsvp(conn, int(eid), user["tg_id"], status, utcnow())
    await cb.answer(text)
    e = events.get(conn, int(eid))
    if e and not e["deleted"]:
        try:
            await cb.message.edit_text(events.text_of(conn, e) + f"\nYour answer: {events.RSVP[status]}", reply_markup=_view_markup(e, user))
        except Exception:
            pass


# ---------------------------------------------------------------- create

@router.callback_query(F.data == "e:new")
async def new(cb: CallbackQuery, state: FSMContext):
    await state.set_state(EventFlow.title)
    await show(cb, "📅 New event. What's the title? /cancel to stop.")


@router.message(EventFlow.title, F.text)
async def got_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip()[:100])
    await state.set_state(EventFlow.description)
    await message.answer("Short description?", reply_markup=kb([("Skip", "e:skip")]))


@router.message(EventFlow.description, F.text)
async def got_description(message: Message, state: FSMContext):
    await state.update_data(description=message.text.strip()[:1000])
    await _ask_location(message, state)


@router.callback_query(EventFlow.description, F.data == "e:skip")
async def skip_description(cb: CallbackQuery, state: FSMContext):
    await state.update_data(description="")
    await cb.answer()
    await _ask_location(cb.message, state)


async def _ask_location(message: Message, state: FSMContext):
    await state.set_state(EventFlow.location)
    await message.answer("📍 Where?", reply_markup=kb([("Common room", "e:loc:Common room"), ("Kitchen", "e:loc:Kitchen")],
                                                     [("Outside", "e:loc:Outside")]))


@router.message(EventFlow.location, F.text)
async def got_location_text(message: Message, state: FSMContext):
    await state.update_data(location=message.text.strip()[:100])
    await _ask_date(message, state)


@router.callback_query(EventFlow.location, F.data.startswith("e:loc:"))
async def got_location_button(cb: CallbackQuery, state: FSMContext):
    await state.update_data(location=cb.data.split(":", 2)[2])
    await cb.answer()
    await _ask_date(cb.message, state)


async def _ask_date(message: Message, state: FSMContext):
    await state.set_state(EventFlow.when)
    today = local_today(utcnow())
    days = [today + timedelta(days=i) for i in range(14)]
    await message.answer("📅 Which day?", reply_markup=kb(*grid([(day_label(d, today), f"e:d:{dkey(d)}") for d in days], 2)))


@router.callback_query(EventFlow.when, F.data.startswith("e:d:"))
async def got_date(cb: CallbackQuery, state: FSMContext):
    await state.update_data(day=cb.data.split(":")[2])
    await state.set_state(EventFlow.time)
    await show(cb, "🕒 What time? Tap one or type it (e.g. 19:30).", kb(*grid([(t, f"e:t:{t.replace(':', '')}") for t in TIMES], 4)))


async def _ask_recurring(message: Message, state: FSMContext, hhmm: str):
    d = await state.get_data()
    starts = at_local(parse_dkey(d["day"]), hhmm)
    if starts <= utcnow():
        await message.answer("That time has already passed — pick a later one.")
        return
    await state.update_data(starts_at=to_db(starts))
    await message.answer("🔁 Repeat every week?", reply_markup=kb([("Yes, weekly", "e:rec:1"), ("No, just once", "e:rec:0")]))


@router.callback_query(EventFlow.time, F.data.startswith("e:t:"))
async def got_time_button(cb: CallbackQuery, state: FSMContext):
    raw = cb.data.split(":")[2]
    await cb.answer()
    await _ask_recurring(cb.message, state, f"{raw[:2]}:{raw[2:]}")


@router.message(EventFlow.time, F.text)
async def got_time_text(message: Message, state: FSMContext):
    hhmm = parse_hhmm(message.text)
    if not hhmm:
        await message.answer("Please send a time like 19:30.")
        return
    await _ask_recurring(message, state, hhmm)


@router.callback_query(EventFlow.time, F.data.startswith("e:rec:"))
async def create(cb: CallbackQuery, state: FSMContext, conn, bot, user):
    d = await state.get_data()
    await state.clear()
    eid, notices = events.create(conn, user["tg_id"], d["title"], d.get("description", ""), d.get("location", ""),
                                 from_db(d["starts_at"]), cb.data.endswith(":1"), utcnow())
    await show(cb, "✅ Event created and everyone has been invited!\n\n" + events.text_of(conn, events.get(conn, eid)),
               kb([("⬅️ Events", "e:list")]))
    await deliver(bot, conn, notices)


# ---------------------------------------------------------------- edit / delete

@router.callback_query(F.data.startswith("e:ed:"))
async def edit_menu(cb: CallbackQuery, user, conn):
    e = events.get(conn, int(cb.data.split(":")[2]))
    if not e or not events.can_edit(e, user):
        return await cb.answer("Only the creator or a mentor can edit this.", show_alert=True)
    eid = e["id"]
    await show(cb, "What do you want to change?", kb(
        [("Title", f"e:ef:{eid}:title"), ("Description", f"e:ef:{eid}:description")],
        [("Location", f"e:ef:{eid}:location"), ("Date & time", f"e:ef:{eid}:starts_at")],
        [("⬅️ Back", f"e:v:{eid}")],
    ))


@router.callback_query(F.data.startswith("e:ef:"))
async def edit_ask(cb: CallbackQuery, state: FSMContext, user, conn):
    _, _, eid, field = cb.data.split(":")
    e = events.get(conn, int(eid))
    if not e or not events.can_edit(e, user):
        return await cb.answer("Not allowed.", show_alert=True)
    await state.set_state(EventFlow.edit)
    await state.update_data(eid=int(eid), field=field)
    hint = "Send the new date and time, e.g. 25.12 19:00" if field == "starts_at" else f"Send the new {field}."
    await show(cb, hint + " /cancel to stop.")


@router.message(EventFlow.edit, F.text)
async def edit_save(message: Message, state: FSMContext, user, conn, bot):
    d = await state.get_data()
    value = message.text.strip()[:1000]
    if d["field"] == "starts_at":
        when = parse_user_datetime_or_none(value)
        if not when:
            await message.answer("I couldn't read that. Try e.g. 25.12 19:00")
            return
        value = to_db(when)
    await state.clear()
    notices = events.update_field(conn, user, d["eid"], d["field"], value)
    await message.answer("✅ Updated.\n\n" + events.text_of(conn, events.get(conn, d["eid"])),
                         reply_markup=kb([("⬅️ Events", "e:list")]))
    await deliver(bot, conn, notices)


def parse_user_datetime_or_none(text: str):
    from ..timeutil import parse_user_datetime

    when = parse_user_datetime(text, utcnow())
    return when if when and when > utcnow() else None


@router.callback_query(F.data.regexp(r"^e:del:\d+$"))
async def delete_confirm(cb: CallbackQuery, user, conn):
    e = events.get(conn, int(cb.data.split(":")[2]))
    if not e or not events.can_edit(e, user):
        return await cb.answer("Only the creator or a mentor can delete this.", show_alert=True)
    await show(cb, f"Delete “{e['title']}”? Everyone who RSVP'd will be told.",
               kb([("Yes, delete", f"e:dely:{e['id']}"), ("No", f"e:v:{e['id']}")]))


@router.callback_query(F.data.startswith("e:dely:"))
async def delete(cb: CallbackQuery, user, conn, bot):
    e = events.get(conn, int(cb.data.split(":")[2]))
    if not e or not events.can_edit(e, user):
        return await cb.answer("Not allowed.", show_alert=True)
    notices = events.delete(conn, user, e["id"])
    await show(cb, "🗑 Event deleted.", kb([("⬅️ Events", "e:list")]))
    await deliver(bot, conn, notices)
