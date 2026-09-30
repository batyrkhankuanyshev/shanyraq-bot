"""Guest log: register, check out, mentor approvals and today's list."""
from datetime import timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ..notices import grid, kb
from ..sender import deliver
from ..services import guests, users
from ..services.guests import DURATIONS
from ..timeutil import at_local, day_label, dkey, fmt_dt, from_db, local_today, parse_dkey, to_db, utcnow
from .common import not_allowed, not_registered, show

router = Router(name="guests")
ARRIVAL_TIMES = ["10:00", "12:00", "14:00", "16:00", "18:00", "20:00"]


class GuestFlow(StatesGroup):
    name = State()
    details = State()
    reject_reason = State()


@router.message(Command("guests"))
@router.callback_query(F.data == "g:menu")
async def menu(event: Message | CallbackQuery, user, conn, state: FSMContext):
    if not user:
        return await not_registered(event)
    await state.clear()
    mine = guests.active_for(conn, user["tg_id"], utcnow())
    lines = ["🚪 Сіздің қонақтарыңыз:"] + [f"• {guests.describe(g)}" for g in mine] if mine else ["🚪 Сізде белсенді қонақ жоқ."]
    rows = [[(f"👋 {g['name']} кетті", f"g:out:{g['id']}")] for g in mine]
    rows.append([("➕ Қонақ тіркеу", "g:new")])
    if users.is_mentor(user):
        rows.append([("📋 Бүгінгі қонақтар", "g:today"), ("⏳ Күтудегілер", "a:g")])
    rows.append([("⬅️ Мәзір", "menu")])
    await show(event, "\n".join(lines), kb(*rows))


@router.callback_query(F.data == "g:new")
async def ask_name(cb: CallbackQuery, state: FSMContext):
    await state.set_state(GuestFlow.name)
    await show(cb, "Қонағыңыздың аты кім? Тоқтату үшін /cancel.")


@router.message(GuestFlow.name, F.text)
async def got_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip()[:60])
    await state.set_state(GuestFlow.details)
    today = local_today(utcnow())
    days = [today + timedelta(days=i) for i in range(7)]
    await message.answer("📅 Келетін күні?", reply_markup=kb(*grid([(day_label(d, today), f"g:d:{dkey(d)}") for d in days], 2)))


@router.callback_query(GuestFlow.details, F.data.startswith("g:d:"))
async def got_date(cb: CallbackQuery, state: FSMContext):
    dk = cb.data.split(":")[2]
    await state.update_data(day=dk)
    buttons = [(t, f"g:t:{t.replace(':', '')}") for t in ARRIVAL_TIMES]
    if parse_dkey(dk) == local_today(utcnow()):
        buttons.insert(0, ("Қазір", "g:t:now"))
    await show(cb, "🕒 Келетін уақыты?", kb(*grid(buttons, 3)))


@router.callback_query(GuestFlow.details, F.data.startswith("g:t:"))
async def got_time(cb: CallbackQuery, state: FSMContext):
    raw = cb.data.split(":")[2]
    d = await state.get_data()
    arrive = utcnow() if raw == "now" else at_local(parse_dkey(d["day"]), f"{raw[:2]}:{raw[2:]}")
    await state.update_data(arrive=to_db(arrive))
    await show(cb, "⏱ Қанша уақыт болады? (түнеу үшін тәлімгердің рұқсаты керек)",
               kb(*grid([(label, f"g:dur:{code}") for code, (label, _, _) in DURATIONS.items()], 3)))


@router.callback_query(GuestFlow.details, F.data.startswith("g:dur:"))
async def got_duration(cb: CallbackQuery, state: FSMContext, conn, bot, user):
    d = await state.get_data()
    await state.clear()
    try:
        msg, notices = guests.register(conn, user["tg_id"], d["name"], from_db(d["arrive"]), cb.data.split(":")[2], utcnow())
    except ValueError as e:
        await show(cb, f"😕 {e}", kb([("⬅️ Қонақтар", "g:menu")]))
        return
    await show(cb, msg, kb([("⬅️ Қонақтар", "g:menu")]))
    await deliver(bot, conn, notices)


@router.callback_query(F.data.startswith("g:out:"))
async def check_out(cb: CallbackQuery, user, conn):
    ok = guests.check_out(conn, user["tg_id"], int(cb.data.split(":")[2]), utcnow())
    await show(cb, "👋 Қонақ кетті деп белгіленді. Рақмет!" if ok else "Бұл қонақ кетті деп белгіленген.", kb([("⬅️ Қонақтар", "g:menu")]))


@router.callback_query(F.data.startswith("g:ap:"))
async def approve(cb: CallbackQuery, user, conn, bot):
    if not users.is_mentor(user):
        return await not_allowed(cb)
    try:
        notices = guests.decide(conn, user["tg_id"], int(cb.data.split(":")[2]), True)
    except ValueError as e:
        return await cb.answer(str(e), show_alert=True)
    await cb.answer("Мақұлданды ✅")
    try:
        await cb.message.edit_text(cb.message.text + f"\n\n✅ {user['name']} мақұлдады")
    except Exception:
        pass
    await deliver(bot, conn, notices)


@router.callback_query(F.data.startswith("g:rj:"))
async def reject_ask(cb: CallbackQuery, user, state: FSMContext):
    if not users.is_mentor(user):
        return await not_allowed(cb)
    await state.set_state(GuestFlow.reject_reason)
    await state.update_data(gid=int(cb.data.split(":")[2]))
    await cb.answer()
    await cb.message.answer("Неге қабылданбады? (қонақ иесі көреді) Тоқтату үшін /cancel.")


@router.message(GuestFlow.reject_reason, F.text)
async def reject(message: Message, state: FSMContext, user, conn, bot):
    gid = (await state.get_data())["gid"]
    await state.clear()
    try:
        notices = guests.decide(conn, user["tg_id"], gid, False, message.text.strip()[:300])
    except ValueError as e:
        await message.answer(str(e))
        return
    await message.answer("❌ Қабылданбады, қонақ иесіне хабарланды.")
    await deliver(bot, conn, notices)


@router.message(Command("guests_today"))
@router.callback_query(F.data == "g:today")
async def today(event: Message | CallbackQuery, user, conn):
    if not users.is_mentor(user):
        return await not_allowed(event)
    rows = guests.today_list(conn, utcnow())
    lines = ["📋 Бүгінгі қонақтар:"] + [f"• {g['name']} (қабылдаушы: {g['host_name']}) {fmt_dt(from_db(g['arrive_at']))} → "
                                    f"{fmt_dt(from_db(g['depart_at']))} · {guests.STATUS_KK[g['status']]}" for g in rows]
    if not rows:
        lines.append("Бүгін қонақ жоқ.")
    await show(event, "\n".join(lines), kb([("⬅️ Мәзір", "menu")]))


@router.callback_query(F.data == "a:g")
async def pending(cb: CallbackQuery, user, conn):
    if not users.is_mentor(user):
        return await not_allowed(cb)
    rows = guests.pending(conn)
    if not rows:
        await show(cb, "Түнеуге күтудегі өтініш жоқ.", kb([("⬅️ Басқару", "a:menu")]))
        return
    await show(cb, "⏳ Түнеуге күтудегі өтініштер:", kb([("⬅️ Басқару", "a:menu")]))
    for g in rows:
        await cb.message.answer(f"{g['host_name']}: {guests.describe(g)}",
                                reply_markup=kb([("✅ Мақұлдау", f"g:ap:{g['id']}"), ("❌ Қабылдамау", f"g:rj:{g['id']}")]))
