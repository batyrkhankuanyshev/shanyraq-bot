"""Settings loaded from environment variables / .env file."""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _int_or_none(value: str | None) -> int | None:
    value = (value or "").strip()
    return int(value) if value else None


def _bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    bot_token: str
    mentor_ids: set[int]
    db_path: str
    group_chat_id: int | None
    events_chat_id: int | None
    events_thread_id: int | None
    duty_weekday: int
    quiet_hours_reminder: bool
    quiet_hours_text: str


def load_config() -> Config:
    mentors = {
        int(x) for x in os.getenv("MENTOR_IDS", "").replace(" ", "").split(",") if x
    }
    group = _int_or_none(os.getenv("GROUP_CHAT_ID"))
    return Config(
        bot_token=os.getenv("BOT_TOKEN", "").strip(),
        mentor_ids=mentors,
        db_path=os.getenv("DB_PATH", "shanyraq.db"),
        group_chat_id=group,
        events_chat_id=_int_or_none(os.getenv("EVENTS_CHAT_ID")) or group,
        events_thread_id=_int_or_none(os.getenv("EVENTS_THREAD_ID")),
        duty_weekday=int(os.getenv("DUTY_WEEKDAY", "5")),
        quiet_hours_reminder=_bool(os.getenv("QUIET_HOURS_REMINDER")),
        quiet_hours_text=os.getenv(
            "QUIET_HOURS_TEXT",
            "🌙 Quiet hours start now (22:00). Please keep the noise down. Thank you!",
        ),
    )


# Module-level config so services and handlers share one instance.
CONFIG = load_config()
