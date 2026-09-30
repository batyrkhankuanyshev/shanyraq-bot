"""Delivers Notices to Telegram. Failures are logged and never crash the bot."""
import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from .notices import Notice
from .services import announcements

log = logging.getLogger(__name__)
CAPTION_LIMIT = 1024


async def _send_one(bot: Bot, n: Notice):
    common = dict(disable_notification=n.silent, message_thread_id=n.thread_id)
    if n.photo and len(n.text) <= CAPTION_LIMIT:
        return await bot.send_photo(n.chat_id, n.photo, caption=n.text, reply_markup=n.markup, **common)
    if n.photo:
        await bot.send_photo(n.chat_id, n.photo, **common)
    return await bot.send_message(n.chat_id, n.text, reply_markup=n.markup, **common)


async def deliver(bot: Bot, conn, notices: list[Notice]) -> int:
    sent = 0
    for n in notices:
        for attempt in range(3):
            try:
                msg = await _send_one(bot, n)
                sent += 1
                if n.pin:
                    try:
                        await bot.pin_chat_message(n.chat_id, msg.message_id, disable_notification=n.silent)
                    except Exception:
                        log.warning("could not pin message in a chat")
                if n.ann_id:
                    announcements.record_message(conn, n.ann_id, n.chat_id, msg.message_id, bool(n.photo))
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError:
                log.info("a user has blocked the bot or never pressed Start")
                break
            except Exception as e:
                log.warning("send failed: %s", type(e).__name__)
                break
        await asyncio.sleep(0.05)  # stay well under Telegram's rate limits
    return sent
