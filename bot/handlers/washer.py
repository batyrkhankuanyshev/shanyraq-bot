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
    rows.append([("🔁 Request a weekly slot", "w:rec")])
    rows.append([("⬅️ Menu", "menu")])
    return kb(*rows)


@router.message(Command("book"))
@router.callback_query(F.data == "w:days")
async def days(event: Message | CallbackQuery, user, conn):
    if not user:
        return await not_registered(event)
    now = utcnow()
    block = washer.active_block(conn, now)
    text = "🧺 Which day would you like to wash?"
    if block:
        text = f"🛠 The washer is unavailable right now: {block['reason']}\nYou can still look at other days." \
            if block["ends_at"] else f"🛠 The washer is under maintenance: {block['reason']}\nBooking is paused until a mentor reopens it."
    count = washer.weekly_count(conn, user["tg_id"], local_today(now))
    if user["role"] == "student":
        text += f"\n(You have {count}/{washer.WEEKLY_LIMIT} bookings this week.)"
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
            buttons.append((f"⭐ {s['time']} (yours)", "w:my"))
        elif s["state"] == "taken":
            buttons.append((f"⚪ {s['time']} · join waitlist", f"w:wl:{dkey(d)}:{s['time'].replace(':', '')}"))
        else:
            buttons.append((f"🛠 {s['time']} unavailable", "noop"))
    rows = grid(buttons, 2)
    rows.append([("⬅️ Days", "w:days")])
    text = f"🧺 {day_label(d, local_today(now))}\n🟢 free · ⚪ taken (tap to join waitlist)"
    if not slots:
        text += "\nNo slots left today."
    await show(cb, text, kb(*rows))


def _hhmm(raw: str) -> str:
    return f"{raw[:2]}:{raw[2:]}"


@router.callback_query(F.data.startswith("w:slot:"))
async def pick_cycle(cb: CallbackQuery, user):
    if not user:
        return await not_registered(cb)
    _, _, dk, t = cb.data.split(":")
    rows = [[(washer.cycle_label(c), f"w:cyc:{dk}:{t}:{c}")] for c in CYCLES]
    rows.append([("⬅️ Back", f"w:day:{dk}")])
    await show(cb, f"🧺 {washer.fmt_date(parse_dkey(dk))} {_hhmm(t)} — which cycle?", kb(*rows))


@router.callback_query(F.data.startswith("w:cyc:"))
async def do_book(cb: CallbackQuery, user, conn):
    if not user:
        return await not_registered(cb)
    _, _, dk, t, cycle = cb.data.split(":")
    d = parse_dkey(dk)
    try:
        b = washer.book(conn, user["tg_id"], d, _hhmm(t), cycle, utcnow())
    except BookingError as e:
        rows = [[("⬅️ Pick another slot", f"w:day:{dk}")]]
        if e.code == "taken":
            rows.insert(0, [("📝 Join waitlist", f"w:wl:{dk}:{t}")])
        await show(cb, f"😕 {e.message}", kb(*rows))
        return
    await show(
        cb,
        f"✅ Booked!\n{washer.summary(b)}{washer.heavy_warning(conn, b)}\n\nI'll remind you 15 minutes before.",
        kb([("❌ Cancel booking", f"w:cx:{b['id']}")], [("⬅️ Menu", "menu")]),
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
        await show(cb, "Good news — that slot is free now!", kb([("Book it", f"w:slot:{dk}:{t}")], [("⬅️ Back", f"w:day:{dk}")]))
        return
    await show(cb, result, kb([("⬅️ Days", "w:days"), ("⬅️ Menu", "menu")]))


@router.message(Command("mybookings"))
@router.callback_query(F.data == "w:my")
async def my_bookings(event: Message | CallbackQuery, user, conn):
    if not user:
        return await not_registered(event)
    now = utcnow()
    bookings = washer.upcoming_for_user(conn, user["tg_id"], now)
    waits = washer.waitlist_for_user(conn, user["tg_id"], now)
    lines = ["📋 Your washer bookings:"]
    rows = []
    for b in bookings:
        state = " (running)" if b["status"] == "active" else ""
        lines.append(f"• {washer.summary(b)}{state}")
        if b["status"] == "booked":
            rows.append([(f"▶️ Started {b['slot_time']}", f"w:st:{b['id']}"), (f"❌ Cancel {b['slot_time']}", f"w:cx:{b['id']}")])
        else:
            rows.append([(f"✅ Done {b['slot_time']}", f"w:done:{b['id']}"), ("⚠️ Washer broke", f"w:brk:{b['id']}")])
    if not bookings:
        lines.append("None yet.")
    if waits:
        lines.append("\n📝 Waitlists:")
        for w in waits:
            lines.append(f"• {fmt_dt(from_db(w['slot_start']))} ({w['status']})")
            rows.append([(f"Leave waitlist {fmt_dt(from_db(w['slot_start']))}", f"w:wlx:{w['id']}")])
    rec = conn.execute(
        "SELECT * FROM recurring WHERE user_id = ? AND status IN ('pending', 'approved')", (user["tg_id"],)
    ).fetchone()
    if rec:
        lines.append(f"\n🔁 Weekly slot: every {WEEKDAYS[rec['weekday']]} {rec['slot_time']} ({rec['status']})")
    rows.append([("🧺 Book another", "w:days"), ("⬅️ Menu", "menu")])
    await show(event, "\n".join(lines), kb(*rows))


@router.callback_query(F.data.regexp(r"^w:cx:\d+$"))
async def cancel_confirm(cb: CallbackQuery, user, conn):
    if not user:
        return await not_registered(cb)
    b = washer.get_booking(conn, int(cb.data.split(":")[2]))
    if not b or b["user_id"] != user["tg_id"] or b["status"] not in washer.LIVE:
        await cb.answer("That booking is no longer active.", show_alert=True)
        return
    await show(cb, f"Cancel this booking?\n{washer.summary(b)}",
               kb([("Yes, cancel", f"w:cxy:{b['id']}"), ("No, keep it", "w:my")]))


@router.callback_query(F.data.startswith("w:cxy:"))
async def cancel_do(cb: CallbackQuery, user, conn, bot):
    if not user:
        return await not_registered(cb)
    try:
        notices = washer.user_cancel(conn, int(cb.data.split(":")[2]), user["tg_id"], utcnow())
    except BookingError as e:
        await cb.answer(e.message, show_alert=True)
        return
    await show(cb, "❌ Booking cancelled. It doesn't count toward your weekly limit.", kb([("⬅️ Menu", "menu")]))
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
    await show(cb, text, kb([("✅ Done early", f"w:done:{bid}"), ("⚠️ Washer broke", f"w:brk:{bid}")]))


@router.callback_query(F.data.startswith("w:done:"))
async def done(cb: CallbackQuery, user, conn, bot):
    if not user:
        return await not_registered(cb)
    try:
        text, notices = washer.finish_booking(conn, int(cb.data.split(":")[2]), user["tg_id"], utcnow())
    except BookingError as e:
        await cb.answer(e.message, show_alert=True)
        return
    await show(cb, text, kb([("⬅️ Menu", "menu")]))
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
    await show(cb, text, kb([("✅ Done", f"w:done:{bid}")]))
    await deliver(bot, conn, notices)


@router.callback_query(F.data.regexp(r"^w:brk:\d+$"))
async def breakdown_confirm(cb: CallbackQuery, user):
    if not user:
        return await not_registered(cb)
    bid = cb.data.split(":")[2]
    await show(cb, "⚠️ Report that the washer broke? This blocks the washer for the rest of today and notifies mentors.",
               kb([("Yes, it's broken", f"w:brky:{bid}"), ("No", "w:my")]))


@router.callback_query(F.data.startswith("w:brky:"))
async def breakdown(cb: CallbackQuery, user, conn, bot):
    if not user:
        return await not_registered(cb)
    notices = washer.report_breakdown(conn, user["tg_id"], int(cb.data.split(":")[2]), utcnow())
    await show(cb, "🛠 Thanks — mentors have been notified and today's remaining bookings were cancelled.", kb([("⬅️ Menu", "menu")]))
    await deliver(bot, conn, notices)


@router.callback_query(F.data.startswith("w:cl:"))
async def claim(cb: CallbackQuery, user, conn):
    if not user:
        return await not_registered(cb)
    _, _, wl_id, cycle = cb.data.split(":")
    try:
        b = washer.claim(conn, int(wl_id), user["tg_id"], cycle, utcnow())
    except BookingError as e:
        await show(cb, f"😕 {e.message}", kb([("⬅️ Menu", "menu")]))
        return
    extra = "\nIt has already started — tap Started once the machine is running." if b["status"] == "active" else ""
    await show(cb, f"🎉 Claimed!\n{washer.summary(b)}{extra}", kb([("❌ Cancel", f"w:cx:{b['id']}")], [("⬅️ Menu", "menu")]))


@router.callback_query(F.data.startswith("w:wlx:"))
async def leave_waitlist(cb: CallbackQuery, user, conn, bot):
    if not user:
        return await not_registered(cb)
    notices = washer.leave_waitlist(conn, int(cb.data.split(":")[2]), user["tg_id"], utcnow())
    await show(cb, "OK, you're off that waitlist.", kb([("⬅️ Menu", "menu")]))
    await deliver(bot, conn, notices)


# ---- recurring slot request: weekday -> time -> cycle
@router.callback_query(F.data == "w:rec")
async def rec_weekday(cb: CallbackQuery, user):
    if not user:
        return await not_registered(cb)
    rows = grid([(name, f"w:recd:{i}") for i, name in enumerate(WEEKDAYS)], 4)
    rows.append([("⬅️ Back", "w:days")])
    await show(cb, f"🔁 Weekly slot (needs mentor approval, max {washer.RECURRING_CAP} people). Which day?", kb(*rows))


@router.callback_query(F.data.startswith("w:recd:"))
async def rec_time(cb: CallbackQuery):
    wd = cb.data.split(":")[2]
    rows = grid([(t, f"w:rect:{wd}:{t.replace(':', '')}") for t in SLOT_TIMES], 5)
    rows.append([("⬅️ Back", "w:rec")])
    await show(cb, f"🔁 Every {WEEKDAYS[int(wd)]} at…", kb(*rows))


@router.callback_query(F.data.startswith("w:rect:"))
async def rec_cycle(cb: CallbackQuery):
    _, _, wd, t = cb.data.split(":")
    rows = [[(washer.cycle_label(c), f"w:recc:{wd}:{t}:{c}")] for c in CYCLES]
    await show(cb, "🔁 Which cycle?", kb(*rows))


@router.callback_query(F.data.startswith("w:recc:"))
async def rec_submit(cb: CallbackQuery, user, conn, bot):
    if not user:
        return await not_registered(cb)
    _, _, wd, t, cycle = cb.data.split(":")
    try:
        notices = washer.request_recurring(conn, user["tg_id"], int(wd), _hhmm(t), cycle, utcnow())
    except BookingError as e:
        await show(cb, f"😕 {e.message}", kb([("⬅️ Menu", "menu")]))
        return
    await show(cb, "📨 Request sent to the mentors. I'll let you know!", kb([("⬅️ Menu", "menu")]))
    await deliver(bot, conn, notices)
