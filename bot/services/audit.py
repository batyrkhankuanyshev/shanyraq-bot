from ..timeutil import to_db, utcnow


def log(conn, actor_id: int, action: str, details: str = "") -> None:
    """Every mentor action is recorded: who did what, when."""
    conn.execute(
        "INSERT INTO audit(actor_id, action, details, created_at) VALUES (?, ?, ?, ?)",
        (actor_id, action, details, to_db(utcnow())),
    )


def recent(conn, limit: int = 20):
    return conn.execute(
        "SELECT a.*, u.name AS actor_name FROM audit a "
        "LEFT JOIN users u ON u.tg_id = a.actor_id ORDER BY a.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
