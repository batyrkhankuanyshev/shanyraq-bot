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
from ..timeutil import (
    at_local, day_label, dkey, fmt_dt, from_db, local, local_today, parse_dkey, parse_hhmm, short_date, to_db, utcnow,
)
from .common import not_registered, show

router = Router(name="events")
FIELD_KK = {"title": "атауын", "description": "сипаттамасын", "location": "орнын"}
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
    lines = ["📅 Алдағы іс-шаралар:"]
    for e in items:
        c = events.counts(conn, e["id"])
        lines.append(f"• {fmt_dt(from_db(e['starts_at']))} — {e['title']} (барады {c['going']}, мүмкін {c['maybe']})")
        rows.append([(f"{short_date(local(from_db(e['starts_at'])).date())} · {e['title'][:30]}", f"e:v:{e['id']}")])
    if not items:
        lines.append("Әзірге ештеңе жоспарланбаған — іс-шара құрыңыз!")
    rows += [[("➕ Іс-шара құру", "e:new")], [("⬅️ Мәзір", "menu")]]
    await show(event, "\n".join(lines), kb(*rows))


def _view_markup(e, user):
    rows = [[(label, f"e:r:{e['id']}:{code}") for code, label in events.RSVP.items()]]
    if events.can_edit(e, user):
        rows.append([("✏️ Өзгерту", f"e:ed:{e['id']}"), ("🗑 Жою", f"e:del:{e['id']}")])
    rows.append([("⬅️ Іс-шаралар", "e:list")])
    return kb(*rows)


@router.callback_query(F.data.startswith("e:v:"))
async def view(cb: CallbackQuery, user, conn):
    e = events.get(conn, int(cb.data.split(":")[2]))
    if not e or e["deleted"]:
        return await cb.answer("Бұл іс-шара енді жоқ.", show_alert=True)
    mine = conn.execute("SELECT status FROM rsvps WHERE event_id = ? AND user_id = ?", (e["id"], user["tg_id"])).fetchone()
    extra = f"\nСіздің жауабыңыз: {events.RSVP[mine['status']]}" if mine else ""
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
            await cb.message.edit_text(events.text_of(conn, e) + f"\nСіздің жауабыңыз: {events.RSVP[status]}", reply_markup=_view_markup(e, user))
        except Exception:
            pass


# ---------------------------------------------------------------- create

@router.callback_query(F.data == "e:new")
async def new(cb: CallbackQuery, state: FSMContext):
    await state.set_state(EventFlow.title)
    await show(cb, "📅 Жаңа іс-шара. Атауы қандай? Тоқтату үшін /cancel.")


@router.message(EventFlow.title, F.text)
async def got_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip()[:100])
    await state.set_state(EventFlow.description)
    await message.answer("Қысқаша сипаттамасы?", reply_markup=kb([("Өткізу", "e:skip")]))


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
    await message.answer("📍 Қай жерде? (батырманы басыңыз немесе жазыңыз)", reply_markup=kb(
        [("Ортақ бөлме", "e:loc:Ортақ бөлме"), ("Ас үй", "e:loc:Ас үй")], [("Сыртта", "e:loc:Сыртта")]))


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
    await message.answer("📅 Қай күні?", reply_markup=kb(*grid([(day_label(d, today), f"e:d:{dkey(d)}") for d in days], 2)))


@router.callback_query(EventFlow.when, F.data.startswith("e:d:"))
async def got_date(cb: CallbackQuery, state: FSMContext):
    await state.update_data(day=cb.data.split(":")[2])
    await state.set_state(EventFlow.time)
    await show(cb, "🕒 Қай уақытта? Батырманы басыңыз немесе жазыңыз (мысалы, 19:30).", kb(*grid([(t, f"e:t:{t.replace(':', '')}") for t in TIMES], 4)))


async def _ask_recurring(message: Message, state: FSMContext, hhmm: str):
    d = await state.get_data()
    starts = at_local(parse_dkey(d["day"]), hhmm)
    if starts <= utcnow():
        await message.answer("Бұл уақыт өтіп кетті — кейінірек уақытты таңдаңыз.")
        return
    await state.update_data(starts_at=to_db(starts))
    await message.answer("🔁 Апта сайын қайталансын ба?", reply_markup=kb([("Иә, апта сайын", "e:rec:1"), ("Жоқ, бір рет", "e:rec:0")]))


@router.callback_query(EventFlow.time, F.data.startswith("e:t:"))
async def got_time_button(cb: CallbackQuery, state: FSMContext):
    raw = cb.data.split(":")[2]
    await cb.answer()
    await _ask_recurring(cb.message, state, f"{raw[:2]}:{raw[2:]}")


@router.message(EventFlow.time, F.text)
async def got_time_text(message: Message, state: FSMContext):
    hhmm = parse_hhmm(message.text)
    if not hhmm:
        await message.answer("Уақытты 19:30 сияқты жіберіңіз.")
        return
    await _ask_recurring(message, state, hhmm)


@router.callback_query(EventFlow.time, F.data.startswith("e:rec:"))
async def create(cb: CallbackQuery, state: FSMContext, conn, bot, user):
    d = await state.get_data()
    await state.clear()
    eid, notices = events.create(conn, user["tg_id"], d["title"], d.get("description", ""), d.get("location", ""),
                                 from_db(d["starts_at"]), cb.data.endswith(":1"), utcnow())
    await show(cb, "✅ Іс-шара құрылды, барлығы шақырылды!\n\n" + events.text_of(conn, events.get(conn, eid)),
               kb([("⬅️ Іс-шаралар", "e:list")]))
    await deliver(bot, conn, notices)


# ---------------------------------------------------------------- edit / delete

@router.callback_query(F.data.startswith("e:ed:"))
async def edit_menu(cb: CallbackQuery, user, conn):
    e = events.get(conn, int(cb.data.split(":")[2]))
    if not e or not events.can_edit(e, user):
        return await cb.answer("Мұны тек құрушы немесе тәлімгер өзгерте алады.", show_alert=True)
    eid = e["id"]
    await show(cb, "Нені өзгерткіңіз келеді?", kb(
        [("Атауы", f"e:ef:{eid}:title"), ("Сипаттамасы", f"e:ef:{eid}:description")],
        [("Орны", f"e:ef:{eid}:location"), ("Күні мен уақыты", f"e:ef:{eid}:starts_at")],
        [("⬅️ Артқа", f"e:v:{eid}")],
    ))


@router.callback_query(F.data.startswith("e:ef:"))
async def edit_ask(cb: CallbackQuery, state: FSMContext, user, conn):
    _, _, eid, field = cb.data.split(":")
    e = events.get(conn, int(eid))
    if not e or not events.can_edit(e, user):
        return await cb.answer("Рұқсат жоқ.", show_alert=True)
    await state.set_state(EventFlow.edit)
    await state.update_data(eid=int(eid), field=field)
    hint = "Жаңа күн мен уақытты жіберіңіз, мысалы 25.12 19:00." if field == "starts_at" else f"Жаңа {FIELD_KK[field]} жіберіңіз."
    await show(cb, hint + " Тоқтату үшін /cancel.")


@router.message(EventFlow.edit, F.text)
async def edit_save(message: Message, state: FSMContext, user, conn, bot):
    d = await state.get_data()
    value = message.text.strip()[:1000]
    if d["field"] == "starts_at":
        when = parse_user_datetime_or_none(value)
        if not when:
            await message.answer("Түсінбедім. Мысалы: 25.12 19:00")
            return
        value = to_db(when)
    await state.clear()
    notices = events.update_field(conn, user, d["eid"], d["field"], value)
    await message.answer("✅ Жаңартылды.\n\n" + events.text_of(conn, events.get(conn, d["eid"])),
                         reply_markup=kb([("⬅️ Іс-шаралар", "e:list")]))
    await deliver(bot, conn, notices)


def parse_user_datetime_or_none(text: str):
    from ..timeutil import parse_user_datetime

    when = parse_user_datetime(text, utcnow())
    return when if when and when > utcnow() else None


@router.callback_query(F.data.regexp(r"^e:del:\d+$"))
async def delete_confirm(cb: CallbackQuery, user, conn):
    e = events.get(conn, int(cb.data.split(":")[2]))
    if not e or not events.can_edit(e, user):
        return await cb.answer("Мұны тек құрушы немесе тәлімгер жоя алады.", show_alert=True)
    await show(cb, f"«{e['title']}» жойылсын ба? Жауап бергендердің бәріне хабарланады.",
               kb([("Иә, жою", f"e:dely:{e['id']}"), ("Жоқ", f"e:v:{e['id']}")]))


@router.callback_query(F.data.startswith("e:dely:"))
async def delete(cb: CallbackQuery, user, conn, bot):
    e = events.get(conn, int(cb.data.split(":")[2]))
    if not e or not events.can_edit(e, user):
        return await cb.answer("Рұқсат жоқ.", show_alert=True)
    notices = events.delete(conn, user, e["id"])
    await show(cb, "🗑 Іс-шара жойылды.", kb([("⬅️ Іс-шаралар", "e:list")]))
    await deliver(bot, conn, notices)
