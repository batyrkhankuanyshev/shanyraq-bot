"""Onboarding, main menu, shared helpers and the user-loading middleware."""
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, TelegramObject

from ..notices import kb
from ..services import users
from ..timeutil import utcnow

router = Router(name="common")


class UserMiddleware(BaseMiddleware):
    """Loads the user row into handler data as `user`; blocks muted/deactivated users."""

    def __init__(self, conn):
        self.conn = conn

    async def __call__(self, handler: Callable[[TelegramObject, dict], Awaitable[Any]], event: TelegramObject, data: dict):
        tg_user = data.get("event_from_user")
        if tg_user is None:
            return None
        chat = data.get("event_chat")
        if chat is not None and chat.type != "private":
            return None  # the bot only talks in private chats; groups are for broadcasts
        user = users.get(self.conn, tg_user.id)
        if user is None and isinstance(event, CallbackQuery) and not (event.data or "").startswith("on:"):
            await event.answer("Алдымен /start жіберіңіз.", show_alert=True)
            return None
        if user and (not user["active"] or user["muted"]):
            text = "Аккаунтыңыз өшірілген. Тәлімгерге хабарласыңыз." if not user["active"] else \
                "Сізге уақытша шектеу қойылған. Тәлімгерге хабарласыңыз."
            if isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=True)
            elif isinstance(event, Message):
                await event.answer(text)
            return None
        data["user"] = user
        data["conn"] = self.conn
        return await handler(event, data)


class Onboarding(StatesGroup):
    name = State()


def main_menu(user):
    rows = [
        [("🧺 Кір жуғыш", "w:days"), ("📋 Брондауларым", "w:my")],
        [("🚨 Хабарлау", "r:menu"), ("🧹 Кезекшілік", "d:menu")],
        [("📢 Хабарландырулар", "n:list"), ("🚪 Қонақтар", "g:menu")],
        [("📅 Іс-шаралар", "e:list")],
    ]
    if users.is_mentor(user):
        rows += [
            [("🛠 Басқару панелі", "a:menu"), ("📣 Хабарландыру жасау", "n:new")],
            [("🚪 Бүгінгі қонақтар", "g:today"), ("📊 Статистика", "a:s")],
        ]
    return kb(*rows)


def back(target: str = "menu", label: str = "⬅️ Артқа"):
    return (label, target)


async def show(event: Message | CallbackQuery, text: str, markup=None) -> None:
    """Edit the menu message in place when coming from a button, else send a new one."""
    if isinstance(event, CallbackQuery):
        msg = event.message
        try:
            if msg and not msg.photo:
                await msg.edit_text(text, reply_markup=markup)
            else:
                await msg.answer(text, reply_markup=markup)
        except Exception:
            await event.bot.send_message(event.from_user.id, text, reply_markup=markup)
        await event.answer()
    else:
        await event.answer(text, reply_markup=markup)


async def send_menu(event: Message | CallbackQuery, user, prefix: str = "") -> None:
    await show(event, (prefix + "\n\n" if prefix else "") + f"Сәлем, {user['name']}! Не істегіңіз келеді?", main_menu(user))


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, user):
    await state.clear()
    if user:
        await send_menu(message, user)
        return
    await state.set_state(Onboarding.name)
    await message.answer("👋 Жатақхана ботына қош келдіңіз! Атыңыз кім? (тек есіміңіз жеткілікті)")


@router.message(Onboarding.name, F.text)
async def onboarding_name(message: Message, state: FSMContext, conn):
    name = message.text.strip()
    if not 1 <= len(name) <= 40 or name.startswith("/"):
        await message.answer("Тек атыңызды жіберіңіз (40 таңбаға дейін).")
        return
    await state.update_data(name=name)
    await message.answer(
        f"Танысқаныма қуаныштымын, {name}! Сіз студентсіз бе, әлде тәлімгерсіз бе?",
        reply_markup=kb([("🎓 Студент", "on:role:student"), ("🧑‍🏫 Тәлімгер", "on:role:mentor")]),
    )


@router.callback_query(F.data.startswith("on:role:"))
async def onboarding_role(cb: CallbackQuery, state: FSMContext, conn, bot, user):
    from ..sender import deliver

    if user:
        await state.clear()
        await send_menu(cb, user)
        return
    data = await state.get_data()
    name = data.get("name") or cb.from_user.first_name or "Дос"
    requested = cb.data.split(":")[2]
    role = users.role_for_new_user(cb.from_user.id, requested)
    users.register(conn, cb.from_user.id, name, role, utcnow())
    await state.clear()
    note = ""
    if requested == "mentor" and role != "mentor":
        note = "ℹ️ Тәлімгер аккаунттары ID бойынша алдын ала бекітіледі, сондықтан әзірге студент ретінде тіркелдіңіз. Қате болса, тәлімгерге хабарласыңыз."
    await deliver(bot, conn, users.new_user_notice(conn, cb.from_user.id, name, role))
    await send_menu(cb, users.get(conn, cb.from_user.id), ("✅ Тіркелдіңіз!\n" + note).strip())


@router.message(Command("menu"))
@router.message(Command("cancel"))
async def cmd_menu(message: Message, state: FSMContext, user):
    await state.clear()
    if not user:
        await message.answer("Алдымен /start жіберіңіз.")
        return
    await send_menu(message, user)


@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery, state: FSMContext, user):
    await state.clear()
    if not user:
        await cb.answer("Алдымен /start жіберіңіз.", show_alert=True)
        return
    await send_menu(cb, user)


@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


def require_user(user) -> bool:
    return user is not None


async def not_registered(event: Message | CallbackQuery) -> None:
    if isinstance(event, CallbackQuery):
        await event.answer("Алдымен /start жіберіңіз.", show_alert=True)
    else:
        await event.answer("Алдымен /start жіберіңіз.")


async def not_allowed(event: Message | CallbackQuery) -> None:
    if isinstance(event, CallbackQuery):
        await event.answer("Тек тәлімгерлерге арналған.", show_alert=True)
    else:
        await event.answer("Тек тәлімгерлерге арналған.")


# Fallback registered last in main.py: anything unexpected gets a friendly nudge.
fallback_router = Router(name="fallback")


@fallback_router.message(StateFilter(None))
async def fallback_message(message: Message, user):
    if not user:
        await message.answer("👋 Бастау үшін /start жіберіңіз.")
        return
    await send_menu(message, user, "Түсінбедім — төмендегі батырмаларды пайдаланыңыз.")


@fallback_router.message()
async def fallback_in_flow(message: Message):
    await message.answer("Жоғарыдағы сұраққа жауап беріңіз немесе тоқтату үшін /cancel жіберіңіз.")


@fallback_router.callback_query()
async def fallback_callback(cb: CallbackQuery):
    await cb.answer("Бұл батырманың мерзімі өтті. /menu жіберіңіз.", show_alert=True)
