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
    await show(event, "🛠 Басқару панелі", kb(
        [("🧺 Кір жуғыш", "a:w"), ("🧹 Кезекшілік", "a:d")],
        [("🛠 Ақаулықтар", "a:r"), ("🕶 Анонимді", "a:ra")],
        [("📢 Хабарландырулар", "a:n"), ("🚪 Қонақтар", "a:g")],
        [("👥 Пайдаланушылар", "a:u"), ("📊 Статистика", "a:s")],
        [("📜 Әрекеттер журналы", "a:log"), ("⬅️ Мәзір", "menu")],
    ))


# ---------------------------------------------------------------- washer

@router.callback_query(F.data == "a:w")
@_mentor_only
async def washer_menu(cb: CallbackQuery, user, conn):
    now = utcnow()
    block = washer.active_block(conn, now)
    status = f"🛠 Қолжетімсіз: {block['reason']}" if block else "✅ Қалыпты жұмыс істеп тұр"
    today = local_today(now)
    rows = [[(day_label(d, today), f"a:wd:{dkey(d)}")] for d in washer.bookable_days(now)]
    rows.append([("✅ Жөндеуді аяқтау", "a:wml")] if block else [("🛠 Жөндеу режимі", "a:wm")])
    rows.append([("🔁 Апта сайынғы слоттар", "a:rec"), ("📊 Статистика", "a:s")])
    rows.append([("⬅️ Басқару", "a:menu")])
    await show(cb, f"🧺 Кір жуғыш — {status}\nБрондауларды көру, мәжбүрлеп брондау немесе босату үшін күнді таңдаңыз:", kb(*rows))


@router.callback_query(F.data.startswith("a:wd:"))
@_mentor_only
async def washer_day(cb: CallbackQuery, user, conn):
    dk = cb.data.split(":")[2]
    d = parse_dkey(dk)
    buttons = []
    for t in SLOT_TIMES:
        b = washer.live_booking_at(conn, to_db(at_local(d, t)))
        label = f"{t} · {users.name_of(conn, b['user_id'])} ({washer.CYCLES[b['cycle']][0]}{', жүріп жатыр' if b['status'] == 'active' else ''})" if b else f"{t} · бос"
        buttons.append((label, f"a:ws:{dk}:{t.replace(':', '')}"))
    rows = grid(buttons, 1)
    rows.append([("⬅️ Кір жуғыш", "a:w")])
    await show(cb, f"🧺 {fmt_date(d)} — слотты таңдаңыз:", kb(*rows))


@router.callback_query(F.data.startswith("a:ws:"))
@_mentor_only
async def washer_slot(cb: CallbackQuery, user, conn):
    _, _, dk, t = cb.data.split(":")
    b = washer.live_booking_at(conn, to_db(at_local(parse_dkey(dk), f"{t[:2]}:{t[2:]}")))
    if b:
        await show(cb, f"{users.name_of(conn, b['user_id'])}: {washer.summary(b)} ({washer.BOOKING_STATUS[b['status']]})",
                   kb([("🧹 Мәжбүрлеп босату", f"a:wff:{b['id']}")], [("⬅️ Артқа", f"a:wd:{dk}")]))
    else:
        await show(cb, "Бұл слот бос.", kb([("➕ Біреуге мәжбүрлеп брондау", f"a:wfb:{dk}:{t}")], [("⬅️ Артқа", f"a:wd:{dk}")]))


@router.callback_query(F.data.startswith("a:wff:"))
@_mentor_only
async def force_free_confirm(cb: CallbackQuery, user, conn):
    bid = cb.data.split(":")[2]
    await show(cb, "Бұл слотты босатасыз ба? Студентке хабарланады.", kb([("Иә, босату", f"a:wffy:{bid}"), ("Жоқ", "a:w")]))


@router.callback_query(F.data.startswith("a:wffy:"))
@_mentor_only
async def force_free(cb: CallbackQuery, user, conn, bot):
    notices = washer.force_free(conn, user["tg_id"], int(cb.data.split(":")[2]), utcnow())
    await show(cb, "✅ Слот босатылды (күту тізіміне хабарланды).", kb([("⬅️ Кір жуғыш", "a:w")]))
    await deliver(bot, conn, notices)


@router.callback_query(F.data.startswith("a:wfb:"))
@_mentor_only
async def force_book_pick(cb: CallbackQuery, user, conn):
    _, _, dk, t = cb.data.split(":")
    rows = grid([(u["name"], f"a:wfu:{dk}:{t}:{u['tg_id']}") for u in users.everyone(conn)], 2)
    rows.append([("⬅️ Артқа", f"a:wd:{dk}")])
    await show(cb, "Кімге брондау керек? (Қалыпты режим, шектеулер ескерілмейді)", kb(*rows))


@router.callback_query(F.data.startswith("a:wfu:"))
@_mentor_only
async def force_book(cb: CallbackQuery, user, conn, bot):
    _, _, dk, t, uid = cb.data.split(":")
    try:
        notices = washer.force_book(conn, user["tg_id"], int(uid), parse_dkey(dk), f"{t[:2]}:{t[2:]}", "normal", utcnow())
    except BookingError as e:
        return await cb.answer(e.message, show_alert=True)
    await show(cb, "✅ Брондалды.", kb([("⬅️ Артқа", f"a:wd:{dk}")]))
    await deliver(bot, conn, notices)


@router.message(Command("washer_maintenance"))
@router.callback_query(F.data == "a:wm")
@_mentor_only
async def maintenance_ask(event: Message | CallbackQuery, user, state: FSMContext):
    await state.set_state(AdminFlow.maintenance_reason)
    await show(event, "🛠 Жөндеу режимі кір жуғыштың барлық слоттарын жабады және алдағы брондауларды (кешірім сұрап) болдырмайды.\n"
                      "Себебі қандай? (студенттерге көрсетіледі) Тоқтату үшін /cancel.")


@router.message(AdminFlow.maintenance_reason, F.text)
async def maintenance_on(message: Message, state: FSMContext, user, conn, bot):
    await state.clear()
    notices = washer.start_maintenance(conn, user["tg_id"], message.text.strip()[:200], utcnow())
    await message.answer(f"🛠 Жөндеу режимі ҚОСУЛЫ. {len(notices)} адамға хабарланды.",
                         reply_markup=kb([("✅ Жөндеуді аяқтау", "a:wml")], [("⬅️ Басқару", "a:menu")]))
    await deliver(bot, conn, notices)


@router.callback_query(F.data == "a:wml")
@_mentor_only
async def maintenance_off(cb: CallbackQuery, user, conn, bot):
    notices = washer.lift_maintenance(conn, user["tg_id"], utcnow())
    await show(cb, "✅ Кір жуғыш қайта қолжетімді." if notices else "Кір жуғыш бұғатталмаған еді.", kb([("⬅️ Кір жуғыш", "a:w")]))
    await deliver(bot, conn, notices)


@router.callback_query(F.data == "a:rec")
@_mentor_only
async def recurring_list(cb: CallbackQuery, user, conn):
    rows = []
    lines = [f"🔁 Апта сайынғы слоттар (ең көбі {washer.RECURRING_CAP} мақұлданады):"]
    for r in conn.execute("SELECT * FROM recurring WHERE status IN ('pending', 'approved') ORDER BY status, id"):
        label = f"{users.name_of(conn, r['user_id'])}: {WEEKDAYS[r['weekday']]} {r['slot_time']} {washer.CYCLES[r['cycle']][0]}"
        lines.append(f"• {label} ({'қаралуда' if r['status'] == 'pending' else 'мақұлданған'})")
        if r["status"] == "pending":
            rows.append([(f"✅ {label}", f"a:recok:{r['id']}"), ("❌", f"a:recno:{r['id']}")])
        else:
            rows.append([(f"⏹ Аяқтау: {label}", f"a:recend:{r['id']}")])
    rows.append([("⬅️ Кір жуғыш", "a:w")])
    await show(cb, "\n".join(lines), kb(*rows))


@router.callback_query(F.data.startswith("a:recok:") | F.data.startswith("a:recno:"))
@_mentor_only
async def recurring_decide(cb: CallbackQuery, user, conn, bot):
    approve = cb.data.startswith("a:recok:")
    try:
        notices = washer.decide_recurring(conn, user["tg_id"], int(cb.data.split(":")[2]), approve, utcnow())
    except BookingError as e:
        return await cb.answer(e.message, show_alert=True)
    await show(cb, "✅ Мақұлданды." if approve else "Қабылданбады.", kb([("⬅️ Апта сайынғы", "a:rec")]))
    await deliver(bot, conn, notices)


@router.callback_query(F.data.startswith("a:recend:"))
@_mentor_only
async def recurring_end(cb: CallbackQuery, user, conn, bot):
    notices = washer.end_recurring(conn, user["tg_id"], int(cb.data.split(":")[2]))
    await show(cb, "⏹ Аяқталды.", kb([("⬅️ Апта сайынғы", "a:rec")]))
    await deliver(bot, conn, notices)


# ---------------------------------------------------------------- rotation

@router.callback_query(F.data == "a:d")
@_mentor_only
async def rotation_menu(cb: CallbackQuery, user, conn):
    ws = week_start(local_today(utcnow()))
    rows = []
    lines = [f"🧹 Кезекшілік — {fmt_date(ws)} басталатын апта және кейінгі:"]
    for d in conn.execute(
        "SELECT * FROM duty WHERE week_start >= ? ORDER BY week_start, kind, zone", (ws.isoformat(),)
    ).fetchall():
        what = d["zone"] if d["kind"] == "cleaning" else "Сатып алушы"
        name = users.name_of(conn, d["user_id"])
        lines.append(f"• {fmt_date(parse_dkey(d['week_start'].replace('-', '')))} {what}: {name} ({duty.DUTY_STATUS[d['status']]})")
        rows.append([(f"🔄 {what} {d['week_start'][5:]}: {name}", f"a:dra:{d['id']}")])
    rows += [
        [("⏸ Студентті тоқтату / қайта қосу", "a:dp")],
        [("▶️ Осы аптаны қазір құру", "a:dgen"), ("📜 Тарих", "d:hist")],
        [("⬅️ Басқару", "a:menu")],
    ]
    await show(cb, "\n".join(lines), kb(*rows))


@router.callback_query(F.data.startswith("a:dra:"))
@_mentor_only
async def reassign_pick(cb: CallbackQuery, user, conn):
    did = cb.data.split(":")[2]
    rows = grid([(s["name"], f"a:drau:{did}:{s['tg_id']}") for s in users.students(conn)], 2)
    rows.append([("⬅️ Артқа", "a:d")])
    await show(cb, "Кімге беру керек?", kb(*rows))


@router.callback_query(F.data.startswith("a:drau:"))
@_mentor_only
async def reassign(cb: CallbackQuery, user, conn, bot):
    _, _, did, uid = cb.data.split(":")
    notices = duty.reassign(conn, user["tg_id"], int(did), int(uid))
    await show(cb, "✅ Басқа адамға берілді.", kb([("⬅️ Кезекшілік", "a:d")]))
    await deliver(bot, conn, notices)


@router.callback_query(F.data == "a:dgen")
@_mentor_only
async def generate_now(cb: CallbackQuery, user, conn, bot):
    now = utcnow()
    ws = week_start(local_today(now))
    notices = duty.generate_buyer(conn, ws, now) + duty.generate_cleaning(conn, ws, now, force=True)
    with tx(conn):
        audit.log(conn, user["tg_id"], "duty_generate", ws.isoformat())
    await show(cb, "✅ Құрылды." if notices else "Бұл апта бұрын құрылған.", kb([("⬅️ Кезекшілік", "a:d")]))
    await deliver(bot, conn, notices)


@router.callback_query(F.data == "a:dp")
@_mentor_only
async def pause_pick(cb: CallbackQuery, user, conn):
    buttons = [(("⏸ " if s["paused_until"] else "") + s["name"], f"a:dpu:{s['tg_id']}") for s in users.students(conn)]
    rows = grid(buttons, 2)
    rows.append([("⬅️ Кезекшілік", "a:d")])
    await show(cb, "Кім? (⏸ = қазір тоқтатылған)", kb(*rows))


@router.callback_query(F.data.startswith("a:dpu:"))
@_mentor_only
async def pause_duration(cb: CallbackQuery, user, conn):
    uid = cb.data.split(":")[2]
    u = users.get(conn, int(uid))
    state = f"{u['paused_until']} дейін тоқтатылған" if u["paused_until"] else "кезекте белсенді"
    await show(cb, f"{u['name']}: {state}. Қанша уақытқа тоқтату керек:", kb(
        [("1 апта", f"a:dpw:{uid}:1"), ("2 апта", f"a:dpw:{uid}:2"), ("4 апта", f"a:dpw:{uid}:4")],
        [("▶️ Қайта қосу", f"a:dpw:{uid}:0")], [("⬅️ Артқа", "a:dp")],
    ))


@router.callback_query(F.data.startswith("a:dpw:"))
@_mentor_only
async def pause_set(cb: CallbackQuery, user, conn):
    _, _, uid, weeks = cb.data.split(":")
    until = None
    if int(weeks):
        until = (local_today(utcnow()) + timedelta(weeks=int(weeks))).isoformat()
    users.set_paused(conn, user["tg_id"], int(uid), until)
    await show(cb, f"✅ {until + ' дейін тоқтатылды. Кезек оны автоматты түрде өткізіп жібереді.' if until else 'Қайта қосылды.'}",
               kb([("⬅️ Кезекшілік", "a:d")]))


# ---------------------------------------------------------------- reports

@router.callback_query(F.data == "a:r")
@_mentor_only
async def reports_list(cb: CallbackQuery, user, conn):
    items = reports.open_reports(conn)
    rows = [[(f"#{r['id']} {r['area']} · {reports.STATUSES[r['status']]}", f"a:rv:{r['id']}")] for r in items]
    rows.append([("⬅️ Басқару", "a:menu")])
    await show(cb, "🛠 Ашық ақаулық өтініштері:" if items else "🛠 Ашық ақаулық өтініші жоқ 🎉", kb(*rows))


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
    rows.append([("⬅️ Басқару", "a:menu")])
    await show(cb, "🕶 Ашық анонимді шағымдар:" if items else "🕶 Ашық анонимді шағым жоқ.", kb(*rows))


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
    rows += [[("➕ Пайдаланушы қосу", "a:uadd")], [("⬅️ Басқару", "a:menu")]]
    await show(cb, "👥 Пайдаланушылар (🎓 студент, 🧑‍🏫 тәлімгер, 💤 белсенді емес, 🔇 шектелген):", kb(*rows))


@router.callback_query(F.data.startswith("a:uv:"))
@_mentor_only
async def user_view(cb: CallbackQuery, user, conn):
    uid = int(cb.data.split(":")[2])
    u = users.get(conn, uid)
    if not u:
        return await cb.answer("Белгісіз пайдаланушы.", show_alert=True)
    now = utcnow()
    ban = washer.ban_until(u, now)
    info = [
        f"👤 {u['name']} (id {u['tg_id']})",
        f"Рөлі: {users.ROLE_KK[u['role']]} · {'белсенді' if u['active'] else 'өшірілген'}" + (" · шектелген" if u["muted"] else ""),
        f"Кезекшілік: {u['paused_until'] + ' дейін тоқтатылған' if u['paused_until'] else 'кезекте'}",
        f"Кір жуғыш: {fmt_dt(ban) + ' дейін тыйым салынған' if ban else 'брондай алады'} · осы айдағы келмеулер: {washer.noshows_this_month(conn, uid, now)}",
    ]
    rows = [
        [("Студент ету" if u["role"] == "mentor" else "Тәлімгер ету", f"a:urole:{uid}"),
         ("Шектеуді алу" if u["muted"] else "Шектеу қою", f"a:umute:{uid}")],
        [("✅ Кір жуғыш тыйымын алу", f"a:uunban:{uid}") if ban else ("🚫 Кір жуғышқа 7 күн тыйым", f"a:uban:{uid}"),
         ("⏸ Кезекті тоқтату", f"a:dpu:{uid}")],
        [("♻️ Қайта белсендіру", f"a:uact:{uid}") if not u["active"] else ("💤 Өшіру (жатақханадан кетті)", f"a:udeact:{uid}")],
        [("⬅️ Пайдаланушылар", "a:u")],
    ]
    await show(cb, "\n".join(info), kb(*rows))


@router.callback_query(F.data.startswith("a:urole:"))
@_mentor_only
async def toggle_role(cb: CallbackQuery, user, conn):
    uid = int(cb.data.split(":")[2])
    if uid == user["tg_id"]:
        return await cb.answer("Өз рөліңізді өзгерте алмайсыз.", show_alert=True)
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
    await show(cb, f"✅ Дайын. {users.name_of(conn, uid)} хабардар етілді.", kb([("👤 Пайдаланушыға оралу", cb_data_view)]))


@router.callback_query(F.data.startswith("a:udeact:"))
@_mentor_only
async def deactivate_confirm(cb: CallbackQuery, user, conn):
    uid = cb.data.split(":")[2]
    await show(cb, f"{users.name_of(conn, int(uid))} өшірілсін бе? Алдағы брондаулары болдырылмайды, апта сайынғы слоты босатылады "
                   "және кезекшіліктен шығарылады.", kb([("Иә, өшіру", f"a:udeacty:{uid}"), ("Жоқ", f"a:uv:{uid}")]))


@router.callback_query(F.data.startswith("a:udeacty:"))
@_mentor_only
async def deactivate(cb: CallbackQuery, user, conn, bot):
    uid = int(cb.data.split(":")[2])
    if uid == user["tg_id"]:
        return await cb.answer("Өзіңізді өшіре алмайсыз.", show_alert=True)
    notices = users.deactivate(conn, user["tg_id"], uid, utcnow())
    await show(cb, "💤 Өшірілді.", kb([("⬅️ Пайдаланушылар", "a:u")]))
    await deliver(bot, conn, notices)


@router.callback_query(F.data.startswith("a:uact:"))
@_mentor_only
async def reactivate(cb: CallbackQuery, user, conn):
    users.activate(conn, user["tg_id"], int(cb.data.split(":")[2]))
    await show(cb, "♻️ Қайта белсендірілді.", kb([("⬅️ Пайдаланушылар", "a:u")]))


@router.callback_query(F.data == "a:uadd")
@_mentor_only
async def add_user_ask(cb: CallbackQuery, user, state: FSMContext):
    await state.set_state(AdminFlow.add_user)
    await show(cb, "Telegram ID мен атын жіберіңіз, мысалы:\n123456789 Айдана\n(ID-ні @userinfobot арқылы білуге болады.) Тоқтату үшін /cancel.")


@router.message(AdminFlow.add_user, F.text)
async def add_user_got(message: Message, state: FSMContext):
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) != 2 or not parts[0].isdigit():
        await message.answer("Үлгі: 123456789 Айдана")
        return
    await state.update_data(new_id=int(parts[0]), new_name=parts[1][:40])
    await message.answer(f"{parts[1]} кім ретінде қосылады?", reply_markup=kb([("🎓 Студент", "a:uaddr:student"), ("🧑‍🏫 Тәлімгер", "a:uaddr:mentor")]))


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
    await show(cb, f"✅ {d['new_name']} {users.ROLE_KK[role]} ретінде қосылды. Оған ботты ашып, Start басуын айтыңыз.", kb([("⬅️ Пайдаланушылар", "a:u")]))


# ---------------------------------------------------------------- stats & audit

@router.message(Command("stats"))
@router.callback_query(F.data == "a:s")
@_mentor_only
async def show_stats(event: Message | CallbackQuery, user, conn):
    await show(event, stats.overview(conn, utcnow())[:4000], kb([("⬅️ Басқару", "a:menu")]))


@router.callback_query(F.data == "a:log")
@_mentor_only
async def audit_log(cb: CallbackQuery, user, conn):
    lines = ["📜 Тәлімгерлердің соңғы 20 әрекеті:"]
    for a in audit.recent(conn, 20):
        lines.append(f"• {fmt_dt(from_db(a['created_at']))} {a['actor_name'] or a['actor_id']}: {audit.ACTION_KK.get(a['action'], a['action'])} {a['details']}")
    await show(cb, "\n".join(lines)[:4000], kb([("⬅️ Басқару", "a:menu")]))
