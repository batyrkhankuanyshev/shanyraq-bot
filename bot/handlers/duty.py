"""Duty menu: my cleaning / buyer duty, shopping list, /low, rotation history."""
from datetime import date

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ..notices import grid, kb
from ..sender import deliver
from ..services import duty
from ..services.duty import DUTY_STATUS, SUPPLIES
from ..timeutil import fmt_date, utcnow
from .common import not_registered, show

router = Router(name="duty")


class Receipt(StatesGroup):
    photo = State()


@router.message(Command("duty"))
@router.callback_query(F.data == "d:menu")
async def menu(event: Message | CallbackQuery, user, conn):
    if not user:
        return await not_registered(event)
    now = utcnow()
    lines = ["🧹 Сіздің кезекшілігіңіз"]
    rows = []
    mine = duty.my_duties(conn, user["tg_id"], now)
    for d in mine:
        when = fmt_date(date.fromisoformat(d["due_date"]))
        if d["kind"] == "cleaning":
            lines.append(f"• «{d['zone']}» тазалау, {when} — {DUTY_STATUS[d['status']]}")
            if d["status"] == "assigned":
                rows.append([(f"✅ «{d['zone']}» орындалды", f"d:done:{d['id']}")])
        else:
            lines.append(f"• Сатып алушы, {fmt_date(date.fromisoformat(d['week_start']))} басталатын апта")
            rows.append([("🛒 Сатып алу тізімі", f"d:blist:{d['id']}")])
    if not mine:
        lines.append("Қазір сізге ештеңе тағайындалмаған.")
    items = duty.shopping_list(conn)
    lines.append("\n🛒 Сатып алу тізімі: " + (", ".join(i["item"] for i in items) if items else "бос"))
    buyer = duty.current_buyer(conn, now)
    if buyer:
        from ..services.users import name_of
        lines.append(f"Осы аптаның сатып алушысы: {name_of(conn, buyer['user_id'])}")
    rows += [[("📉 Таусылып жатыр", "d:low"), ("📜 Кезек тарихы", "d:hist")], [("⬅️ Мәзір", "menu")]]
    await show(event, "\n".join(lines), kb(*rows))


@router.callback_query(F.data.startswith("d:done:"))
async def mark_done(cb: CallbackQuery, user, conn, bot):
    if not user:
        return await not_registered(cb)
    text, notices = duty.mark_cleaning_done(conn, int(cb.data.split(":")[2]), user["tg_id"], utcnow())
    await show(cb, text, kb([("⬅️ Мәзір", "menu")]))
    await deliver(bot, conn, notices)


@router.message(Command("low"))
@router.callback_query(F.data == "d:low")
async def low(event: Message | CallbackQuery, user):
    if not user:
        return await not_registered(event)
    rows = grid([(item, f"d:lowi:{i}") for i, item in enumerate(SUPPLIES)], 2)
    rows.append([("⬅️ Артқа", "d:menu")])
    await show(event, "📉 Не таусылып жатыр?", kb(*rows))


@router.callback_query(F.data.startswith("d:lowi:"))
async def low_item(cb: CallbackQuery, user, conn, bot):
    if not user:
        return await not_registered(cb)
    text, notices = duty.flag_low(conn, user["tg_id"], SUPPLIES[int(cb.data.split(":")[2])], utcnow())
    await show(cb, text, kb([("📉 Тағы біреуін белгілеу", "d:low"), ("⬅️ Мәзір", "menu")]))
    await deliver(bot, conn, notices)


@router.callback_query(F.data.startswith("d:blist:"))
async def buyer_list(cb: CallbackQuery, user, conn):
    d = conn.execute("SELECT * FROM duty WHERE id = ?", (int(cb.data.split(":")[2]),)).fetchone()
    if not d:
        return await cb.answer("Табылмады.", show_alert=True)
    text, markup = duty.buyer_message(conn, d)
    await show(cb, text, markup or kb([("⬅️ Кезекшілік", "d:menu")]))


@router.callback_query(F.data.startswith("d:buy:"))
async def bought(cb: CallbackQuery, user, conn):
    if not user:
        return await not_registered(cb)
    text = duty.mark_bought(conn, user["tg_id"], int(cb.data.split(":")[2]), utcnow())
    buyer = duty.current_buyer(conn, utcnow())
    await cb.answer(text)
    if buyer:
        new_text, markup = duty.buyer_message(conn, buyer)
        await show(cb, text + "\n\n" + new_text, markup or kb([("⬅️ Мәзір", "menu")]))


@router.callback_query(F.data.startswith("d:rcpt:"))
async def receipt_ask(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Receipt.photo)
    await state.update_data(duty_id=int(cb.data.split(":")[2]))
    await show(cb, "📎 Чектің фотосын жіберіңіз. Өткізіп жіберу үшін /cancel.")


@router.message(Receipt.photo, F.photo)
async def receipt_save(message: Message, state: FSMContext, conn, user):
    duty_id = (await state.get_data())["duty_id"]
    await state.clear()
    ok = duty.save_receipt(conn, duty_id, user["tg_id"], message.photo[-1].file_id, utcnow())
    await message.answer("✅ Чек сақталды. Сатып алғаныңызға рақмет!" if ok else "Бұл сатып алу кезекшілігі сіздікі емес.",
                         reply_markup=kb([("⬅️ Мәзір", "menu")]))


@router.callback_query(F.data == "d:hist")
async def hist(cb: CallbackQuery, conn):
    rows = duty.history(conn, weeks=4)
    lines = ["📜 Кезек, соңғы 4 апта:"]
    week = None
    for r in rows:
        if r["week_start"] != week:
            week = r["week_start"]
            lines.append(f"\n{fmt_date(date.fromisoformat(week))} басталатын апта")
        what = r["zone"] if r["kind"] == "cleaning" else "🛒 Сатып алушы"
        lines.append(f"• {what}: {r['name']} — {DUTY_STATUS[r['status']]}")
    if not rows:
        lines.append("Әзірге тарих жоқ.")
    lines.append("\n" + duty.fairness_text(conn))
    await show(cb, "\n".join(lines)[:4000], kb([("⬅️ Кезекшілік", "d:menu")]))
