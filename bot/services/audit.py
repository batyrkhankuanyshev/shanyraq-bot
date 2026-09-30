from ..timeutil import to_db, utcnow


def log(conn, actor_id: int, action: str, details: str = "") -> None:
    """Every mentor action is recorded: who did what, when."""
    conn.execute(
        "INSERT INTO audit(actor_id, action, details, created_at) VALUES (?, ?, ?, ?)",
        (actor_id, action, details, to_db(utcnow())),
    )


ACTION_KK = {
    "set_role": "рөлді өзгертті", "mute": "шектеу қойды", "unmute": "шектеуді алды",
    "pause": "кезекті тоқтатты", "unpause": "кезекке қайта қосты", "deactivate": "өшірді", "activate": "белсендірді",
    "add_user": "пайдаланушы қосты", "washer_force_free": "слотты босатты", "washer_force_book": "мәжбүрлеп брондады",
    "washer_maintenance_on": "жөндеу режимін қосты", "washer_maintenance_off": "жөндеу режимін өшірді",
    "washer_ban": "кір жуғышқа тыйым салды", "washer_unban": "тыйымды алды",
    "recurring_approve": "апта сайынғы слотты мақұлдады", "recurring_reject": "апта сайынғы слотты қабылдамады",
    "recurring_end": "апта сайынғы слотты аяқтады", "report_status": "өтініш күйін өзгертті",
    "anon_reply": "анонимді шағымға жауап берді", "anon_close": "анонимді шағымды жапты",
    "duty_reassign": "кезекшілікті басқаға берді", "duty_generate": "кезекшілік құрды",
    "announce": "хабарландыру жасады", "announce_cancel": "хабарландыруды тоқтатты",
    "announce_edit": "хабарландыруды өзгертті", "announce_pin": "хабарландыруды бекітті",
    "guest_approve": "қонақты мақұлдады", "guest_reject": "қонақты қабылдамады",
    "event_edit": "іс-шараны өзгертті", "event_delete": "іс-шараны жойды",
}


def recent(conn, limit: int = 20):
    return conn.execute(
        "SELECT a.*, u.name AS actor_name FROM audit a "
        "LEFT JOIN users u ON u.tg_id = a.actor_id ORDER BY a.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
