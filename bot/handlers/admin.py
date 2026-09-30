"""/admin — mentors run the hostel from here. Every action is audit-logged in the services."""
import inspect
from datetime import timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ..db import tx
from ..notices import grid, kb
from ..sender import deliver
from ..services import audit, duty, reports, stats, users, washer
from ..services.washer import CYCLES, SLOT_TIMES, WEEKDAYS, BookingError
from ..timeutil import (
    at_local, day_label, dkey, fmt_date, fmt_dt, from_db, local_today, parse_dkey, to_db, utcnow, week_start,
)
from .common import not_allowed, show

router = Router(name="admin")


class AdminFlow(StatesGroup):
    maintenance_reason = State()
    add_user = State()


def _mentor_only(handler):
    """aiogram passes every context value to a **kwargs handler, so forward only
    the ones the wrapped handler actually declares."""
    wanted = set(inspect.signature(handler).parameters) - {"event", "cb", "message"}

    async def wrapper(event, **kwargs):
        if not users.is_mentor(kwargs.get("user")):
            return await not_allowed(event)
        return await handler(event, **{k: v for k, v in kwargs.items() if k in wanted})
    wrapper.__name__ = handler.__name__
    wrapper.__wrapped__ = handler
    return wrapper


@router.message(Command("admin"))
@router.callback_query(F.data == "a:menu")
@_mentor_only
async def menu(event: Message | CallbackQuery, user, state: FSMContext):
    await state.clear()
    await show(event, "🛠 Admin Panel", kb(
        [("🧺 Washer", "a:w"), ("🧹 Rotation", "a:d")],
        [("🛠 Reports", "a:r"), ("🕶 Anonymous", "a:ra")],
        [("📢 Announcements", "a:n"), ("🚪 Guests", "a:g")],
        [("👥 Users", "a:u"), ("📊 Stats", "a:s")],
        [("📜 Audit log", "a:log"), ("⬅️ Menu", "menu")],
    ))


# ---------------------------------------------------------------- washer

@router.callback_query(F.data == "a:w")
@_mentor_only
async def washer_menu(cb: CallbackQuery, user, conn):
    now = utcnow()
    block = washer.active_block(conn, now)
    status = f"🛠 Unavailable: {block['reason']}" if block else "✅ Working normally"
    today = local_today(now)
    rows = [[(day_label(d, today), f"a:wd:{dkey(d)}")] for d in washer.bookable_days(now)]
    rows.append([("✅ End maintenance", "a:wml")] if block else [("🛠 Maintenance mode", "a:wm")])
    rows.append([("🔁 Recurring slots", "a:rec"), ("📊 Stats", "a:s")])
    rows.append([("⬅️ Admin", "a:menu")])
    await show(cb, f"🧺 Washer — {status}\nPick a day to view bookings, force-book or force-free:", kb(*rows))


@router.callback_query(F.data.startswith("a:wd:"))
@_mentor_only
async def washer_day(cb: CallbackQuery, user, conn):
    dk = cb.data.split(":")[2]
    d = parse_dkey(dk)
    buttons = []
    for t in SLOT_TIMES:
        b = washer.live_booking_at(conn, to_db(at_local(d, t)))
        label = f"{t} · {users.name_of(conn, b['user_id'])} ({b['cycle']}{', running' if b['status'] == 'active' else ''})" if b else f"{t} · free"
        buttons.append((label, f"a:ws:{dk}:{t.replace(':', '')}"))
    rows = grid(buttons, 1)
    rows.append([("⬅️ Washer", "a:w")])
    await show(cb, f"🧺 {fmt_date(d)} — tap a slot:", kb(*rows))


@router.callback_query(F.data.startswith("a:ws:"))
@_mentor_only
async def washer_slot(cb: CallbackQuery, user, conn):
    _, _, dk, t = cb.data.split(":")
    b = washer.live_booking_at(conn, to_db(at_local(parse_dkey(dk), f"{t[:2]}:{t[2:]}")))
    if b:
        await show(cb, f"{users.name_of(conn, b['user_id'])}: {washer.summary(b)} ({b['status']})",
                   kb([("🧹 Force free", f"a:wff:{b['id']}")], [("⬅️ Back", f"a:wd:{dk}")]))
    else:
        await show(cb, "This slot is free.", kb([("➕ Force book for someone", f"a:wfb:{dk}:{t}")], [("⬅️ Back", f"a:wd:{dk}")]))


@router.callback_query(F.data.startswith("a:wff:"))
@_mentor_only
async def force_free_confirm(cb: CallbackQuery, user, conn):
    bid = cb.data.split(":")[2]
    await show(cb, "Free this slot? The student will be told.", kb([("Yes, free it", f"a:wffy:{bid}"), ("No", "a:w")]))


@router.callback_query(F.data.startswith("a:wffy:"))
@_mentor_only
async def force_free(cb: CallbackQuery, user, conn, bot):
    notices = washer.force_free(conn, user["tg_id"], int(cb.data.split(":")[2]), utcnow())
    await show(cb, "✅ Slot freed (waitlist notified).", kb([("⬅️ Washer", "a:w")]))
    await deliver(bot, conn, notices)


@router.callback_query(F.data.startswith("a:wfb:"))
@_mentor_only
async def force_book_pick(cb: CallbackQuery, user, conn):
    _, _, dk, t = cb.data.split(":")
    rows = grid([(u["name"], f"a:wfu:{dk}:{t}:{u['tg_id']}") for u in users.everyone(conn)], 2)
    rows.append([("⬅️ Back", f"a:wd:{dk}")])
    await show(cb, "Book it for whom? (Normal cycle, limits ignored)", kb(*rows))


@router.callback_query(F.data.startswith("a:wfu:"))
@_mentor_only
async def force_book(cb: CallbackQuery, user, conn, bot):
    _, _, dk, t, uid = cb.data.split(":")
    try:
        notices = washer.force_book(conn, user["tg_id"], int(uid), parse_dkey(dk), f"{t[:2]}:{t[2:]}", "normal", utcnow())
    except BookingError as e:
        return await cb.answer(e.message, show_alert=True)
    await show(cb, "✅ Booked.", kb([("⬅️ Back", f"a:wd:{dk}")]))
    await deliver(bot, conn, notices)


@router.message(Command("washer_maintenance"))
@router.callback_query(F.data == "a:wm")
@_mentor_only
async def maintenance_ask(event: Message | CallbackQuery, user, state: FSMContext):
    await state.set_state(AdminFlow.maintenance_reason)
    await show(event, "🛠 Maintenance mode blocks all washer slots and cancels upcoming bookings (with an apology).\n"
                      "What's the reason? (shown to students) /cancel to stop.")


@router.message(AdminFlow.maintenance_reason, F.text)
async def maintenance_on(message: Message, state: FSMContext, user, conn, bot):
    await state.clear()
    notices = washer.start_maintenance(conn, user["tg_id"], message.text.strip()[:200], utcnow())
    await message.answer(f"🛠 Maintenance mode is ON. {len(notices)} people notified.",
                         reply_markup=kb([("✅ End maintenance", "a:wml")], [("⬅️ Admin", "a:menu")]))
    await deliver(bot, conn, notices)


@router.callback_query(F.data == "a:wml")
@_mentor_only
async def maintenance_off(cb: CallbackQuery, user, conn, bot):
    notices = washer.lift_maintenance(conn, user["tg_id"], utcnow())
    await show(cb, "✅ Washer is available again." if notices else "The washer wasn't blocked.", kb([("⬅️ Washer", "a:w")]))
    await deliver(bot, conn, notices)


@router.callback_query(F.data == "a:rec")
@_mentor_only
async def recurring_list(cb: CallbackQuery, user, conn):
    rows = []
    lines = [f"🔁 Recurring slots (max {washer.RECURRING_CAP} approved):"]
    for r in conn.execute("SELECT * FROM recurring WHERE status IN ('pending', 'approved') ORDER BY status, id"):
        label = f"{users.name_of(conn, r['user_id'])}: {WEEKDAYS[r['weekday']]} {r['slot_time']} {r['cycle']}"
        lines.append(f"• {label} ({r['status']})")
        if r["status"] == "pending":
            rows.append([(f"✅ {label}", f"a:recok:{r['id']}"), ("❌", f"a:recno:{r['id']}")])
        else:
            rows.append([(f"⏹ End: {label}", f"a:recend:{r['id']}")])
    rows.append([("⬅️ Washer", "a:w")])
    await show(cb, "\n".join(lines), kb(*rows))


@router.callback_query(F.data.startswith("a:recok:") | F.data.startswith("a:recno:"))
@_mentor_only
async def recurring_decide(cb: CallbackQuery, user, conn, bot):
    approve = cb.data.startswith("a:recok:")
    try:
        notices = washer.decide_recurring(conn, user["tg_id"], int(cb.data.split(":")[2]), approve, utcnow())
    except BookingError as e:
        return await cb.answer(e.message, show_alert=True)
    await show(cb, "✅ Approved." if approve else "Rejected.", kb([("⬅️ Recurring", "a:rec")]))
    await deliver(bot, conn, notices)


@router.callback_query(F.data.startswith("a:recend:"))
@_mentor_only
async def recurring_end(cb: CallbackQuery, user, conn, bot):
    notices = washer.end_recurring(conn, user["tg_id"], int(cb.data.split(":")[2]))
    await show(cb, "⏹ Ended.", kb([("⬅️ Recurring", "a:rec")]))
    await deliver(bot, conn, notices)


# ---------------------------------------------------------------- rotation

@router.callback_query(F.data == "a:d")
@_mentor_only
async def rotation_menu(cb: CallbackQuery, user, conn):
    ws = week_start(local_today(utcnow()))
    rows = []
    lines = [f"🧹 Rotation — week of {fmt_date(ws)} and later:"]
    for d in conn.execute(
        "SELECT * FROM duty WHERE week_start >= ? ORDER BY week_start, kind, zone", (ws.isoformat(),)
    ).fetchall():
        what = d["zone"] if d["kind"] == "cleaning" else "Buyer"
        name = users.name_of(conn, d["user_id"])
        lines.append(f"• {fmt_date(parse_dkey(d['week_start'].replace('-', '')))} {what}: {name} ({d['status']})")
        rows.append([(f"🔄 {what} {d['week_start'][5:]}: {name}", f"a:dra:{d['id']}")])
    rows += [
        [("⏸ Pause / unpause a student", "a:dp")],
        [("▶️ Generate this week now", "a:dgen"), ("📜 History", "d:hist")],
        [("⬅️ Admin", "a:menu")],
    ]
    await show(cb, "\n".join(lines), kb(*rows))


@router.callback_query(F.data.startswith("a:dra:"))
@_mentor_only
async def reassign_pick(cb: CallbackQuery, user, conn):
    did = cb.data.split(":")[2]
    rows = grid([(s["name"], f"a:drau:{did}:{s['tg_id']}") for s in users.students(conn)], 2)
    rows.append([("⬅️ Back", "a:d")])
    await show(cb, "Reassign to whom?", kb(*rows))


@router.callback_query(F.data.startswith("a:drau:"))
@_mentor_only
async def reassign(cb: CallbackQuery, user, conn, bot):
    _, _, did, uid = cb.data.split(":")
    notices = duty.reassign(conn, user["tg_id"], int(did), int(uid))
    await show(cb, "✅ Reassigned.", kb([("⬅️ Rotation", "a:d")]))
    await deliver(bot, conn, notices)


@router.callback_query(F.data == "a:dgen")
@_mentor_only
async def generate_now(cb: CallbackQuery, user, conn, bot):
    now = utcnow()
    ws = week_start(local_today(now))
    notices = duty.generate_buyer(conn, ws, now) + duty.generate_cleaning(conn, ws, now, force=True)
    with tx(conn):
        audit.log(conn, user["tg_id"], "duty_generate", ws.isoformat())
    await show(cb, "✅ Generated." if notices else "This week is already generated.", kb([("⬅️ Rotation", "a:d")]))
    await deliver(bot, conn, notices)


@router.callback_query(F.data == "a:dp")
@_mentor_only
async def pause_pick(cb: CallbackQuery, user, conn):
    buttons = [(("⏸ " if s["paused_until"] else "") + s["name"], f"a:dpu:{s['tg_id']}") for s in users.students(conn)]
    rows = grid(buttons, 2)
    rows.append([("⬅️ Rotation", "a:d")])
    await show(cb, "Who? (⏸ = currently paused)", kb(*rows))


@router.callback_query(F.data.startswith("a:dpu:"))
@_mentor_only
async def pause_duration(cb: CallbackQuery, user, conn):
    uid = cb.data.split(":")[2]
    u = users.get(conn, int(uid))
    state = f"paused until {u['paused_until']}" if u["paused_until"] else "active in rotation"
    await show(cb, f"{u['name']} is {state}. Pause for:", kb(
        [("1 week", f"a:dpw:{uid}:1"), ("2 weeks", f"a:dpw:{uid}:2"), ("4 weeks", f"a:dpw:{uid}:4")],
        [("▶️ Unpause", f"a:dpw:{uid}:0")], [("⬅️ Back", "a:dp")],
    ))


@router.callback_query(F.data.startswith("a:dpw:"))
@_mentor_only
async def pause_set(cb: CallbackQuery, user, conn):
    _, _, uid, weeks = cb.data.split(":")
    until = None
    if int(weeks):
        until = (local_today(utcnow()) + timedelta(weeks=int(weeks))).isoformat()
    users.set_paused(conn, user["tg_id"], int(uid), until)
    await show(cb, f"✅ {'Paused until ' + until if until else 'Unpaused'}. The rotation will skip them automatically.",
               kb([("⬅️ Rotation", "a:d")]))


# ---------------------------------------------------------------- reports

@router.callback_query(F.data == "a:r")
@_mentor_only
async def reports_list(cb: CallbackQuery, user, conn):
    items = reports.open_reports(conn)
    rows = [[(f"#{r['id']} {r['area']} · {reports.STATUSES[r['status']]}", f"a:rv:{r['id']}")] for r in items]
    rows.append([("⬅️ Admin", "a:menu")])
    await show(cb, "🛠 Open maintenance reports:" if items else "🛠 No open maintenance reports 🎉", kb(*rows))


@router.callback_query(F.data.startswith("a:rv:"))
@_mentor_only
async def report_view(cb: CallbackQuery, user, conn):
    r = conn.execute("SELECT * FROM reports WHERE id = ?", (int(cb.data.split(":")[2]),)).fetchone()
    await cb.answer()
    if r["photo_id"]:
        await cb.message.answer_photo(r["photo_id"], caption=reports.report_text(conn, r)[:1024], reply_markup=reports.status_buttons(r["id"]))
    else:
        await cb.message.answer(reports.report_text(conn, r), reply_markup=reports.status_buttons(r["id"]))


@router.callback_query(F.data == "a:ra")
@_mentor_only
async def anon_list(cb: CallbackQuery, user, conn):
    items = reports.open_anonymous(conn)
    rows = [[(f"#{r['id']} {r['category']}: {r['description'][:30]}", f"a:rav:{r['id']}")] for r in items]
    rows.append([("⬅️ Admin", "a:menu")])
    await show(cb, "🕶 Open anonymous reports:" if items else "🕶 No open anonymous reports.", kb(*rows))


@router.callback_query(F.data.startswith("a:rav:"))
@_mentor_only
async def anon_view(cb: CallbackQuery, user, conn):
    r = conn.execute("SELECT * FROM anon_reports WHERE id = ?", (int(cb.data.split(":")[2]),)).fetchone()
    await cb.answer()
    text = reports.anon_text(r, reports.anon_messages(conn, r["id"]))
    if r["photo_id"]:
        await cb.message.answer_photo(r["photo_id"])
    await cb.message.answer(text[:4000], reply_markup=reports.anon_buttons(r))


# ---------------------------------------------------------------- users

@router.callback_query(F.data == "a:u")
@_mentor_only
async def users_list(cb: CallbackQuery, user, conn):
    buttons = []
    for u in users.all_users(conn):
        flags = ("🧑‍🏫" if u["role"] == "mentor" else "🎓") + ("" if u["active"] else "💤") + ("🔇" if u["muted"] else "")
        buttons.append((f"{flags} {u['name']}", f"a:uv:{u['tg_id']}"))
    rows = grid(buttons, 2)
    rows += [[("➕ Add user", "a:uadd")], [("⬅️ Admin", "a:menu")]]
    await show(cb, "👥 Users (🎓 student, 🧑‍🏫 mentor, 💤 inactive, 🔇 muted):", kb(*rows))


@router.callback_query(F.data.startswith("a:uv:"))
@_mentor_only
async def user_view(cb: CallbackQuery, user, conn):
    uid = int(cb.data.split(":")[2])
    u = users.get(conn, uid)
    if not u:
        return await cb.answer("Unknown user.", show_alert=True)
    now = utcnow()
    ban = washer.ban_until(u, now)
    info = [
        f"👤 {u['name']} (id {u['tg_id']})",
        f"Role: {u['role']} · {'active' if u['active'] else 'deactivated'}" + (" · muted" if u["muted"] else ""),
        f"Rotation: {'paused until ' + u['paused_until'] if u['paused_until'] else 'in rotation'}",
        f"Washer: {'banned until ' + fmt_dt(ban) if ban else 'can book'} · no-shows this month: {washer.noshows_this_month(conn, uid, now)}",
    ]
    rows = [
        [("Make student" if u["role"] == "mentor" else "Make mentor", f"a:urole:{uid}"),
         ("Unmute" if u["muted"] else "Mute", f"a:umute:{uid}")],
        [("✅ Lift washer ban", f"a:uunban:{uid}") if ban else ("🚫 Washer ban 7d", f"a:uban:{uid}"),
         ("⏸ Rotation pause", f"a:dpu:{uid}")],
        [("♻️ Reactivate", f"a:uact:{uid}") if not u["active"] else ("💤 Deactivate (left hostel)", f"a:udeact:{uid}")],
        [("⬅️ Users", "a:u")],
    ]
    await show(cb, "\n".join(info), kb(*rows))


@router.callback_query(F.data.startswith("a:urole:"))
@_mentor_only
async def toggle_role(cb: CallbackQuery, user, conn):
    uid = int(cb.data.split(":")[2])
    if uid == user["tg_id"]:
        return await cb.answer("You can't change your own role.", show_alert=True)
    u = users.get(conn, uid)
    users.set_role(conn, user["tg_id"], uid, "student" if u["role"] == "mentor" else "mentor")
    await user_view.__wrapped__(cb, user=user, conn=conn)


@router.callback_query(F.data.startswith("a:umute:"))
@_mentor_only
async def toggle_mute(cb: CallbackQuery, user, conn):
    uid = int(cb.data.split(":")[2])
    u = users.get(conn, uid)
    users.set_muted(conn, user["tg_id"], uid, not u["muted"])
    await user_view.__wrapped__(cb, user=user, conn=conn)


@router.callback_query(F.data.startswith("a:uban:") | F.data.startswith("a:uunban:"))
@_mentor_only
async def toggle_ban(cb: CallbackQuery, user, conn, bot):
    uid = int(cb.data.split(":")[2])
    notices = washer.set_ban(conn, user["tg_id"], uid, utcnow(), washer.BAN_DAYS if cb.data.startswith("a:uban:") else None)
    await deliver(bot, conn, notices)
    cb_data_view = f"a:uv:{uid}"
    await show(cb, f"✅ Done. {users.name_of(conn, uid)} has been told.", kb([("👤 Back to user", cb_data_view)]))


@router.callback_query(F.data.startswith("a:udeact:"))
@_mentor_only
async def deactivate_confirm(cb: CallbackQuery, user, conn):
    uid = cb.data.split(":")[2]
    await show(cb, f"Deactivate {users.name_of(conn, int(uid))}? Their future bookings are cancelled, recurring slot freed, "
                   "and the rotation skips them.", kb([("Yes, deactivate", f"a:udeacty:{uid}"), ("No", f"a:uv:{uid}")]))


@router.callback_query(F.data.startswith("a:udeacty:"))
@_mentor_only
async def deactivate(cb: CallbackQuery, user, conn, bot):
    uid = int(cb.data.split(":")[2])
    if uid == user["tg_id"]:
        return await cb.answer("You can't deactivate yourself.", show_alert=True)
    notices = users.deactivate(conn, user["tg_id"], uid, utcnow())
    await show(cb, "💤 Deactivated.", kb([("⬅️ Users", "a:u")]))
    await deliver(bot, conn, notices)


@router.callback_query(F.data.startswith("a:uact:"))
@_mentor_only
async def reactivate(cb: CallbackQuery, user, conn):
    users.activate(conn, user["tg_id"], int(cb.data.split(":")[2]))
    await show(cb, "♻️ Reactivated.", kb([("⬅️ Users", "a:u")]))


@router.callback_query(F.data == "a:uadd")
@_mentor_only
async def add_user_ask(cb: CallbackQuery, user, state: FSMContext):
    await state.set_state(AdminFlow.add_user)
    await show(cb, "Send the Telegram ID and name, e.g.\n123456789 Aida\n(They can get their ID from @userinfobot.) /cancel to stop.")


@router.message(AdminFlow.add_user, F.text)
async def add_user_got(message: Message, state: FSMContext):
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) != 2 or not parts[0].isdigit():
        await message.answer("Format: 123456789 Aida")
        return
    await state.update_data(new_id=int(parts[0]), new_name=parts[1][:40])
    await message.answer(f"Add {parts[1]} as…", reply_markup=kb([("🎓 Student", "a:uaddr:student"), ("🧑‍🏫 Mentor", "a:uaddr:mentor")]))


@router.callback_query(AdminFlow.add_user, F.data.startswith("a:uaddr:"))
@_mentor_only
async def add_user_save(cb: CallbackQuery, user, conn, state: FSMContext):
    d = await state.get_data()
    await state.clear()
    role = cb.data.split(":")[2]
    now = utcnow()
    users.register(conn, d["new_id"], d["new_name"], role, now)
    with tx(conn):
        audit.log(conn, user["tg_id"], "add_user", f"{d['new_name']} ({role})")
    await show(cb, f"✅ Added {d['new_name']} as {role}. Ask them to open the bot and press Start.", kb([("⬅️ Users", "a:u")]))


# ---------------------------------------------------------------- stats & audit

@router.message(Command("stats"))
@router.callback_query(F.data == "a:s")
@_mentor_only
async def show_stats(event: Message | CallbackQuery, user, conn):
    await show(event, stats.overview(conn, utcnow())[:4000], kb([("⬅️ Admin", "a:menu")]))


@router.callback_query(F.data == "a:log")
@_mentor_only
async def audit_log(cb: CallbackQuery, user, conn):
    lines = ["📜 Last 20 mentor actions:"]
    for a in audit.recent(conn, 20):
        lines.append(f"• {fmt_dt(from_db(a['created_at']))} {a['actor_name'] or a['actor_id']}: {a['action']} {a['details']}")
    await show(cb, "\n".join(lines)[:4000], kb([("⬅️ Admin", "a:menu")]))
