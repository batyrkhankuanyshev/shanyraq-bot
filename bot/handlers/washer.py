"""Washer booking flow for students (mentor overrides live in admin.py)."""
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from ..notices import grid, kb
from ..sender import deliver
from ..services import washer
from ..services.washer import CYCLES, SLOT_TIMES, WEEKDAYS, BookingError
from ..timeutil import at_local, day_label, dkey, fmt_dt, from_db, local_today, parse_dkey, utcnow
from .common import not_registered, show

router = Router(name="washer")


def _days_markup(now):
    today = local_today(now)
    rows = [[(day_label(d, today), f"w:day:{dkey(d)}")] for d in washer.bookable_days(now)]
    rows.append([("🔁 Апта сайынғы слот сұрау", "w:rec")])
    rows.append([("⬅️ Мәзір", "menu")])
    return kb(*rows)


@router.message(Command("book"))
@router.callback_query(F.data == "w:days")
async def days(event: Message | CallbackQuery, user, conn):
    if not user:
        return await not_registered(event)
    now = utcnow()
    block = washer.active_block(conn, now)
    text = "🧺 Қай күні кір жуғыңыз келеді?"
    if block:
        text = f"🛠 Кір жуғыш қазір қолжетімсіз: {block['reason']}\nБасқа күндерді қарай аласыз." \
            if block["ends_at"] else f"🛠 Кір жуғыш жөндеуде: {block['reason']}\nТәлімгер қайта ашқанша брондау тоқтатылды."
    count = washer.weekly_count(conn, user["tg_id"], local_today(now))
    if user["role"] == "student":
        text += f"\n(Осы аптада {count}/{washer.WEEKLY_LIMIT} брондауыңыз бар.)"
    await show(event, text, _days_markup(now))


@router.callback_query(F.data.startswith("w:day:"))
async def day_slots(cb: CallbackQuery, user, conn):
    if not user:
        return await not_registered(cb)
    d = parse_dkey(cb.data.split(":")[2])
    now = utcnow()
    slots = washer.slots_for_day(conn, d, now, user["tg_id"])
    buttons = []
    for s in slots:
        if s["state"] == "free":
            buttons.append((f"🟢 {s['time']}", f"w:slot:{dkey(d)}:{s['time'].replace(':', '')}"))
        elif s["state"] == "mine":
            buttons.append((f"⭐ {s['time']} (сіздікі)", "w:my"))
        elif s["state"] == "taken":
            buttons.append((f"⚪ {s['time']} · күту тізімі", f"w:wl:{dkey(d)}:{s['time'].replace(':', '')}"))
        else:
            buttons.append((f"🛠 {s['time']} қолжетімсіз", "noop"))
    rows = grid(buttons, 2)
    rows.append([("⬅️ Күндер", "w:days")])
    text = f"🧺 {day_label(d, local_today(now))}\n🟢 бос · ⚪ бос емес (күту тізіміне жазылу үшін басыңыз)"
    if not slots:
        text += "\nБүгінге бос слот қалмады."
    await show(cb, text, kb(*rows))


def _hhmm(raw: str) -> str:
    return f"{raw[:2]}:{raw[2:]}"


@router.callback_query(F.data.startswith("w:slot:"))
async def pick_cycle(cb: CallbackQuery, user):
    if not user:
        return await not_registered(cb)
    _, _, dk, t = cb.data.split(":")
    rows = [[(washer.cycle_label(c), f"w:cyc:{dk}:{t}:{c}")] for c in CYCLES]
    rows.append([("⬅️ Артқа", f"w:day:{dk}")])
    await show(cb, f"🧺 {washer.fmt_date(parse_dkey(dk))} {_hhmm(t)} — қай режим?", kb(*rows))


@router.callback_query(F.data.startswith("w:cyc:"))
async def do_book(cb: CallbackQuery, user, conn):
    if not user:
        return await not_registered(cb)
    _, _, dk, t, cycle = cb.data.split(":")
    d = parse_dkey(dk)
    try:
        b = washer.book(conn, user["tg_id"], d, _hhmm(t), cycle, utcnow())
    except BookingError as e:
        rows = [[("⬅️ Басқа слот таңдау", f"w:day:{dk}")]]
        if e.code == "taken":
            rows.insert(0, [("📝 Күту тізіміне жазылу", f"w:wl:{dk}:{t}")])
        await show(cb, f"😕 {e.message}", kb(*rows))
        return
    await show(
        cb,
        f"✅ Брондалды!\n{washer.summary(b)}{washer.heavy_warning(conn, b)}\n\n15 минут бұрын еске саламын.",
        kb([("❌ Брондауды болдырмау", f"w:cx:{b['id']}")], [("⬅️ Мәзір", "menu")]),
    )


@router.callback_query(F.data.startswith("w:wl:"))
async def join_waitlist(cb: CallbackQuery, user, conn):
    if not user:
        return await not_registered(cb)
    _, _, dk, t = cb.data.split(":")
    try:
        result = washer.join_waitlist(conn, user["tg_id"], at_local(parse_dkey(dk), _hhmm(t)), utcnow())
    except BookingError as e:
        await cb.answer(e.message, show_alert=True)
        return
    if result == "free":
        await show(cb, "Жақсы жаңалық — бұл слот қазір бос!", kb([("Брондау", f"w:slot:{dk}:{t}")], [("⬅️ Артқа", f"w:day:{dk}")]))
        return
    await show(cb, result, kb([("⬅️ Күндер", "w:days"), ("⬅️ Мәзір", "menu")]))


@router.message(Command("mybookings"))
@router.callback_query(F.data == "w:my")
async def my_bookings(event: Message | CallbackQuery, user, conn):
    if not user:
        return await not_registered(event)
    now = utcnow()
    bookings = washer.upcoming_for_user(conn, user["tg_id"], now)
    waits = washer.waitlist_for_user(conn, user["tg_id"], now)
    lines = ["📋 Кір жуғышқа брондауларыңыз:"]
    rows = []
    for b in bookings:
        state = " (жүріп жатыр)" if b["status"] == "active" else ""
        lines.append(f"• {washer.summary(b)}{state}")
        if b["status"] == "booked":
            rows.append([(f"▶️ Бастадым {b['slot_time']}", f"w:st:{b['id']}"), (f"❌ Болдырмау {b['slot_time']}", f"w:cx:{b['id']}")])
        else:
            rows.append([(f"✅ Бітті {b['slot_time']}", f"w:done:{b['id']}"), ("⚠️ Машина бұзылды", f"w:brk:{b['id']}")])
    if not bookings:
        lines.append("Әзірге жоқ.")
    if waits:
        lines.append("\n📝 Күту тізімдері:")
        for w in waits:
            lines.append(f"• {fmt_dt(from_db(w['slot_start']))} ({'сізге ұсынылды' if w['status'] == 'offered' else 'күтуде'})")
            rows.append([(f"Тізімнен шығу {fmt_dt(from_db(w['slot_start']))}", f"w:wlx:{w['id']}")])
    rec = conn.execute(
        "SELECT * FROM recurring WHERE user_id = ? AND status IN ('pending', 'approved')", (user["tg_id"],)
    ).fetchone()
    if rec:
        lines.append(f"\n🔁 Апта сайынғы слот: әр {WEEKDAYS[rec['weekday']]} {rec['slot_time']} ({'мақұлданды' if rec['status'] == 'approved' else 'қаралуда'})")
    rows.append([("🧺 Тағы брондау", "w:days"), ("⬅️ Мәзір", "menu")])
    await show(event, "\n".join(lines), kb(*rows))


@router.callback_query(F.data.regexp(r"^w:cx:\d+$"))
async def cancel_confirm(cb: CallbackQuery, user, conn):
    if not user:
        return await not_registered(cb)
    b = washer.get_booking(conn, int(cb.data.split(":")[2]))
    if not b or b["user_id"] != user["tg_id"] or b["status"] not in washer.LIVE:
        await cb.answer("Бұл брондау енді белсенді емес.", show_alert=True)
        return
    await show(cb, f"Бұл брондауды болдырмайсыз ба?\n{washer.summary(b)}",
               kb([("Иә, болдырмау", f"w:cxy:{b['id']}"), ("Жоқ, қалсын", "w:my")]))


@router.callback_query(F.data.startswith("w:cxy:"))
async def cancel_do(cb: CallbackQuery, user, conn, bot):
    if not user:
        return await not_registered(cb)
    try:
        notices = washer.user_cancel(conn, int(cb.data.split(":")[2]), user["tg_id"], utcnow())
    except BookingError as e:
        await cb.answer(e.message, show_alert=True)
        return
    await show(cb, "❌ Брондау болдырылмады. Ол апталық шегіңізге есептелмейді.", kb([("⬅️ Мәзір", "menu")]))
    await deliver(bot, conn, notices)


@router.callback_query(F.data.startswith("w:st:") | F.data.startswith("w:yes:"))
async def started(cb: CallbackQuery, user, conn):
    if not user:
        return await not_registered(cb)
    bid = int(cb.data.split(":")[2])
    try:
        text = washer.start_booking(conn, bid, user["tg_id"], utcnow())
    except BookingError as e:
        await cb.answer(e.message, show_alert=True)
        return
    await show(cb, text, kb([("✅ Ерте бітті", f"w:done:{bid}"), ("⚠️ Машина бұзылды", f"w:brk:{bid}")]))


@router.callback_query(F.data.startswith("w:done:"))
async def done(cb: CallbackQuery, user, conn, bot):
    if not user:
        return await not_registered(cb)
    try:
        text, notices = washer.finish_booking(conn, int(cb.data.split(":")[2]), user["tg_id"], utcnow())
    except BookingError as e:
        await cb.answer(e.message, show_alert=True)
        return
    await show(cb, text, kb([("⬅️ Мәзір", "menu")]))
    await deliver(bot, conn, notices)


@router.callback_query(F.data.startswith("w:late:"))
async def late(cb: CallbackQuery, user, conn, bot):
    if not user:
        return await not_registered(cb)
    bid = int(cb.data.split(":")[2])
    try:
        text, notices = washer.running_late(conn, bid, user["tg_id"], utcnow())
    except BookingError as e:
        await cb.answer(e.message, show_alert=True)
        return
    await show(cb, text, kb([("✅ Бітті", f"w:done:{bid}")]))
    await deliver(bot, conn, notices)


@router.callback_query(F.data.regexp(r"^w:brk:\d+$"))
async def breakdown_confirm(cb: CallbackQuery, user):
    if not user:
        return await not_registered(cb)
    bid = cb.data.split(":")[2]
    await show(cb, "⚠️ Кір жуғыш бұзылғанын хабарлайсыз ба? Машина бүгінгі күннің соңына дейін бұғатталады және тәлімгерлерге хабар беріледі.",
               kb([("Иә, бұзылды", f"w:brky:{bid}"), ("Жоқ", "w:my")]))


@router.callback_query(F.data.startswith("w:brky:"))
async def breakdown(cb: CallbackQuery, user, conn, bot):
    if not user:
        return await not_registered(cb)
    notices = washer.report_breakdown(conn, user["tg_id"], int(cb.data.split(":")[2]), utcnow())
    await show(cb, "🛠 Рақмет — тәлімгерлерге хабарланды, бүгінгі қалған брондаулар болдырылмады.", kb([("⬅️ Мәзір", "menu")]))
    await deliver(bot, conn, notices)


@router.callback_query(F.data.startswith("w:cl:"))
async def claim(cb: CallbackQuery, user, conn):
    if not user:
        return await not_registered(cb)
    _, _, wl_id, cycle = cb.data.split(":")
    try:
        b = washer.claim(conn, int(wl_id), user["tg_id"], cycle, utcnow())
    except BookingError as e:
        await show(cb, f"😕 {e.message}", kb([("⬅️ Мәзір", "menu")]))
        return
    extra = "\nСлот басталып кетті — машинаны қосқан соң «Бастадым» батырмасын басыңыз." if b["status"] == "active" else ""
    await show(cb, f"🎉 Слот сіздікі!\n{washer.summary(b)}{extra}", kb([("❌ Болдырмау", f"w:cx:{b['id']}")], [("⬅️ Мәзір", "menu")]))


@router.callback_query(F.data.startswith("w:wlx:"))
async def leave_waitlist(cb: CallbackQuery, user, conn, bot):
    if not user:
        return await not_registered(cb)
    notices = washer.leave_waitlist(conn, int(cb.data.split(":")[2]), user["tg_id"], utcnow())
    await show(cb, "Жарайды, сіз күту тізімінен шықтыңыз.", kb([("⬅️ Мәзір", "menu")]))
    await deliver(bot, conn, notices)


# ---- recurring slot request: weekday -> time -> cycle
@router.callback_query(F.data == "w:rec")
async def rec_weekday(cb: CallbackQuery, user):
    if not user:
        return await not_registered(cb)
    rows = grid([(name, f"w:recd:{i}") for i, name in enumerate(WEEKDAYS)], 4)
    rows.append([("⬅️ Артқа", "w:days")])
    await show(cb, f"🔁 Апта сайынғы слот (тәлімгер мақұлдауы керек, ең көбі {washer.RECURRING_CAP} адам). Қай күн?", kb(*rows))


@router.callback_query(F.data.startswith("w:recd:"))
async def rec_time(cb: CallbackQuery):
    wd = cb.data.split(":")[2]
    rows = grid([(t, f"w:rect:{wd}:{t.replace(':', '')}") for t in SLOT_TIMES], 5)
    rows.append([("⬅️ Артқа", "w:rec")])
    await show(cb, f"🔁 Әр {WEEKDAYS[int(wd)]}, сағат…", kb(*rows))


@router.callback_query(F.data.startswith("w:rect:"))
async def rec_cycle(cb: CallbackQuery):
    _, _, wd, t = cb.data.split(":")
    rows = [[(washer.cycle_label(c), f"w:recc:{wd}:{t}:{c}")] for c in CYCLES]
    await show(cb, "🔁 Қай режим?", kb(*rows))


@router.callback_query(F.data.startswith("w:recc:"))
async def rec_submit(cb: CallbackQuery, user, conn, bot):
    if not user:
        return await not_registered(cb)
    _, _, wd, t, cycle = cb.data.split(":")
    try:
        notices = washer.request_recurring(conn, user["tg_id"], int(wd), _hhmm(t), cycle, utcnow())
    except BookingError as e:
        await show(cb, f"😕 {e.message}", kb([("⬅️ Мәзір", "menu")]))
        return
    await show(cb, "📨 Өтініш тәлімгерлерге жіберілді. Нәтижесін хабарлаймын!", kb([("⬅️ Мәзір", "menu")]))
    await deliver(bot, conn, notices)
