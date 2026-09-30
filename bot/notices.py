"""Outgoing messages produced by services. Services never talk to Telegram
directly; they return Notices and the sender delivers them. This keeps the
business logic testable without a network."""
from dataclasses import dataclass, field

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """kb([("Yes", "cb:yes"), ("No", "cb:no")], [("Back", "menu")])"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in rows
            if row
        ]
    )


def grid(buttons: list[tuple[str, str]], width: int = 2) -> list[list[tuple[str, str]]]:
    return [buttons[i : i + width] for i in range(0, len(buttons), width)]


@dataclass
class Notice:
    chat_id: int
    text: str
    markup: InlineKeyboardMarkup | None = None
    silent: bool = True          # quiet by default; only urgent pings are loud
    photo: str | None = None
    pin: bool = False
    thread_id: int | None = None
    ann_id: int | None = None    # set for announcements so message ids get recorded
    meta: dict = field(default_factory=dict)
