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
        [("🛠 Ақаулық", "r:m")],
        [("🕶 Анонимді шағым", "r:a")],
        [("💬 Анонимді жауаптарды тексеру", "r:ak")],
        [("⬅️ Мәзір", "menu")],
    ]
    if users.is_mentor(user):
        rows.insert(3, [("📂 Ашық өтініштер (тәлімгерлер)", "a:r")])
    await show(event, "🚨 Не туралы хабарлағыңыз келеді?", kb(*rows))


# ---------------------------------------------------------------- maintenance

@router.callback_query(F.data == "r:m")
async def m_area(cb: CallbackQuery):
    rows = grid([(a, f"r:ma:{i}") for i, a in enumerate(AREAS)], 2)
    rows.append([("⬅️ Артқа", "r:menu")])
    await show(cb, "🛠 Не жөндеу керек?", kb(*rows))


@router.callback_query(F.data.startswith("r:ma:"))
async def m_ask_desc(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ReportFlow.m_desc)
    await state.update_data(area=AREAS[int(cb.data.split(":")[2])])
    await show(cb, "Мәселені хабарламамен сипаттаңыз. Фото да жіберуге болады (сипаттаманы фотоның астына жазыңыз).\nТоқтату үшін /cancel.")


def _text_and_photo(message: Message) -> tuple[str, str | None]:
    if message.photo:
        return (message.caption or "").strip(), message.photo[-1].file_id
    return (message.text or "").strip(), None


@router.message(ReportFlow.m_desc, F.text | F.photo)
async def m_got_desc(message: Message, state: FSMContext, conn):
    text, photo = _text_and_photo(message)
    if not text:
        await message.answer("Қысқаша сипаттама қосыңыз (мәтін ретінде немесе фотоның астына).")
        return
    await state.update_data(desc=text[:1500], photo=photo)
    area = (await state.get_data())["area"]
    dup = reports.recent_duplicate(conn, area, utcnow())
    if dup:
        mins = int((utcnow() - from_db(dup["created_at"])).total_seconds() // 60)
        ago = f"{mins} минут" if mins < 60 else f"{mins // 60} сағ {mins % 60} мин"
        await message.answer(
            f"ℹ️ «{area}» бойынша мәселе {ago} бұрын хабарланған:\n“{dup['description'][:200]}”\nСіздікі басқа мәселе ме, әлде әлі шешілмеді ме?",
            reply_markup=kb([("Иә, хабарлау", "r:mdup:1"), ("Жоқ, дәл сол мәселе", "r:mdup:0")]),
        )
        return
    await _ask_name(message)


async def _ask_name(message: Message):
    await message.answer(
        "Тәлімгерлер атыңызды көрсін бе? (Иә десеңіз, жөнделгенде хабарлаймын.)",
        reply_markup=kb([("Иә, атым көрсетілсін", "r:mname:1"), ("Анонимді қалу", "r:mname:0")]),
    )


@router.callback_query(ReportFlow.m_desc, F.data.startswith("r:mdup:"))
async def m_dup(cb: CallbackQuery, state: FSMContext):
    if cb.data.endswith(":0"):
        await state.clear()
        await show(cb, "👍 Рақмет — тәлімгерлер бұл туралы біледі.", kb([("⬅️ Мәзір", "menu")]))
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
    await show(cb, f"✅ №{rid} өтініш жіберілді. Тәлімгерлерге хабарланды.", kb([("⬅️ Мәзір", "menu")]))
    await deliver(bot, conn, notices)


@router.callback_query(F.data.startswith("r:ms:"))
async def m_status(cb: CallbackQuery, user, conn, bot):
    if not users.is_mentor(user):
        return await not_allowed(cb)
    _, _, rid, status = cb.data.split(":")
    notices = reports.set_status(conn, user["tg_id"], int(rid), status, utcnow())
    r = conn.execute("SELECT * FROM reports WHERE id = ?", (int(rid),)).fetchone()
    await cb.answer(f"Белгіленді: {reports.STATUSES[r['status']]}")
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
    rows.append([("⬅️ Артқа", "r:menu")])
    await show(cb, "🕶 Анонимді шағым\nСіздің кім екеніңіз ешқайда сақталмайды — дерекқорда да, журналдарда да. "
                   "Тәлімгерлер тек санатты және жазғаныңызды көреді.\n\nНе туралы?", kb(*rows))


@router.callback_query(F.data.startswith("r:ac:"))
async def a_ask_desc(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ReportFlow.a_desc)
    await state.update_data(category=ANON_CATEGORIES[int(cb.data.split(":")[2])])
    await show(cb, "Не болып жатқанын сипаттаңыз. Фото міндетті емес (мәтінді фотоның астына жазыңыз).\n"
                   "Кеңес: өзіңізді танытатын мәліметтерді жазбаңыз. Тоқтату үшін /cancel.")


@router.message(ReportFlow.a_desc, F.text | F.photo)
async def a_got_desc(message: Message, state: FSMContext):
    text, photo = _text_and_photo(message)
    if not text:
        await message.answer("Қысқаша сипаттама қосыңыз.")
        return
    await state.update_data(desc=text[:1500], photo=photo)
    await message.answer(
        "Тәлімгерлерден анонимді жауап алғыңыз келе ме? "
        "Жауаптарды тексеру үшін құпия код аласыз — оны сізбен ешкім (тіпті бот та) байланыстыра алмайды.",
        reply_markup=kb([("Иә, код беріңіз", "r:ao:1"), ("Жоқ, жай жіберу", "r:ao:0")]),
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
        await show(cb, f"😕 {e}", kb([("⬅️ Мәзір", "menu")]))
        return
    text = "✅ Тәлімгерлерге анонимді түрде жіберілді. Үндемей қалмағаныңызға рақмет."
    if token:
        text += (f"\n\nҚұпия кодыңыз: {token}\nОны сақтап қойыңыз! Тәлімгер жауаптарын оқу үшін Хабарлау → "
                 "Анонимді жауаптарды тексеру бөліміне кіріп, кодты енгізіңіз. Код жоғалса, жауаптарды қалпына келтіру мүмкін емес.")
    await show(cb, text, kb([("⬅️ Мәзір", "menu")]))
    await deliver(bot, conn, notices)


@router.callback_query(F.data == "r:ak")
async def a_ask_token(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ReportFlow.a_token)
    await show(cb, "Құпия кодыңызды жіберіңіз (мысалы, A-1F2E3D4C). Тоқтату үшін /cancel.")


def _thread_text(conn, r) -> str:
    return reports.anon_text(r, reports.anon_messages(conn, r["id"]))


@router.message(ReportFlow.a_token, F.text)
async def a_check(message: Message, state: FSMContext, conn):
    r = reports.find_by_token(conn, message.text)
    if not r:
        await message.answer("Бұл код табылмады. Тексеріп, қайталап көріңіз немесе /cancel.")
        return
    await state.update_data(token=message.text.strip())
    await state.set_state(None)
    markup = kb([("💬 Жауап беру", "r:akr")], [("⬅️ Мәзір", "menu")]) if r["status"] == "open" else kb([("⬅️ Мәзір", "menu")])
    try:
        await message.delete()  # don't leave the code visible in the chat
    except Exception:
        pass
    await message.answer(_thread_text(conn, r), reply_markup=markup)


@router.callback_query(F.data == "r:akr")
async def a_reply_ask(cb: CallbackQuery, state: FSMContext):
    if not (await state.get_data()).get("token"):
        await cb.answer("Кодыңызды қайта енгізіңіз.", show_alert=True)
        return
    await state.set_state(ReportFlow.a_reply)
    await show(cb, "Жауабыңызды жазыңыз (ол анонимді болып қалады). Тоқтату үшін /cancel.")


@router.message(ReportFlow.a_reply, F.text)
async def a_reply_send(message: Message, state: FSMContext, conn, bot):
    token = (await state.get_data()).get("token", "")
    await state.clear()
    try:
        notices = reports.reporter_reply(conn, token, message.text[:1500], utcnow())
    except ValueError as e:
        await message.answer(str(e))
        return
    await message.answer("✅ Анонимді түрде жіберілді.", reply_markup=kb([("⬅️ Мәзір", "menu")]))
    await deliver(bot, conn, notices)


@router.callback_query(F.data.startswith("r:ar:"))
async def mentor_reply_ask(cb: CallbackQuery, state: FSMContext, user):
    if not users.is_mentor(user):
        return await not_allowed(cb)
    await state.set_state(ReportFlow.mentor_reply)
    await state.update_data(rid=int(cb.data.split(":")[2]))
    await cb.answer()
    await cb.message.answer("Анонимді шағым иесіне жауабыңызды жазыңыз. Ол кодымен тексергенде көреді. Тоқтату үшін /cancel.")


@router.message(ReportFlow.mentor_reply, F.text)
async def mentor_reply_send(message: Message, state: FSMContext, conn, bot, user):
    rid = (await state.get_data())["rid"]
    await state.clear()
    try:
        notices = reports.mentor_reply(conn, user["tg_id"], rid, message.text[:1500], utcnow())
    except ValueError as e:
        await message.answer(str(e))
        return
    await message.answer("✅ Жауап сақталды. Шағым иесі келесі жолы тексергенде көреді.")
    await deliver(bot, conn, notices)


@router.callback_query(F.data.startswith("r:acl:"))
async def anon_close(cb: CallbackQuery, conn, user):
    if not users.is_mentor(user):
        return await not_allowed(cb)
    rid = int(cb.data.split(":")[2])
    reports.close_anonymous(conn, user["tg_id"], rid)
    await cb.answer("Жабылды.")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
