"""Entry point: python -m bot.main"""
import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from .config import CONFIG
from .db import connect
from .handlers import admin, announce, common, duty, events, guests, reports, washer
from .jobs import scheduler_loop

COMMANDS = [
    ("start", "Open main menu"),
    ("book", "Book the washing machine"),
    ("mybookings", "My washer bookings"),
    ("report", "Report a problem"),
    ("low", "Flag a low-stock item"),
    ("duty", "My cleaning / buying duty"),
    ("announcements", "Recent announcements"),
    ("guests", "Guest log"),
    ("events", "Upcoming events"),
    ("admin", "Mentor panel"),
    ("cancel", "Stop what I'm doing"),
]


def setup_logging() -> None:
    # No user IDs or message contents are logged anywhere (anonymous reports depend on this).
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)  # would log update ids per message


def build_dispatcher(conn) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    middleware = common.UserMiddleware(conn)
    dp.message.outer_middleware(middleware)
    dp.callback_query.outer_middleware(middleware)
    dp.include_routers(
        common.router, washer.router, reports.router, duty.router, announce.router,
        guests.router, events.router, admin.router, common.fallback_router,
    )

    @dp.errors()
    async def on_error(event) -> bool:
        # Log the exception type/trace only; never the update (it contains user IDs and text).
        logging.getLogger("bot").error("handler error: %r", event.exception, exc_info=event.exception)
        update = event.update
        target = update.callback_query or update.message
        try:
            if update.callback_query:
                await update.callback_query.answer("😕 Something went wrong. Please try again or send /menu.", show_alert=True)
            elif target:
                await target.answer("😕 Something went wrong. Please try again or send /menu.")
        except Exception:
            pass
        return True

    return dp


async def main() -> None:
    setup_logging()
    if not CONFIG.bot_token:
        sys.exit("BOT_TOKEN is missing. Copy .env.example to .env and paste your token from @BotFather.")
    conn = connect(CONFIG.db_path)
    bot = Bot(CONFIG.bot_token)
    dp = build_dispatcher(conn)
    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS])
    me = await bot.get_me()
    logging.info("Started as @%s, database %s", me.username, CONFIG.db_path)
    scheduler = asyncio.create_task(scheduler_loop(bot, conn))
    try:
        await dp.start_polling(bot, conn=conn)
    finally:
        scheduler.cancel()
        await bot.session.close()
        conn.close()


if __name__ == "__main__":
    asyncio.run(main())
