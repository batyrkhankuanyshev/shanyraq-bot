"""Register the hostel's students and mentors from a CSV file.

Usage:  python -m scripts.seed users.csv
CSV columns: telegram_id,name,role   (role = student or mentor)
Safe to run again: existing users are updated, not duplicated.
"""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import CONFIG  # noqa: E402
from bot.db import connect  # noqa: E402
from bot.services import users  # noqa: E402
from bot.timeutil import utcnow  # noqa: E402


def main(path: str) -> None:
    conn = connect(CONFIG.db_path)
    counts = {"student": 0, "mentor": 0}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            tg_id = int(row["telegram_id"].strip())
            role = row.get("role", "student").strip().lower()
            if role not in counts:
                sys.exit(f"Bad role {role!r} for {row['name']}")
            if role == "mentor" and CONFIG.mentor_ids and tg_id not in CONFIG.mentor_ids:
                print(f"note: {row['name']} is a mentor in the CSV but not in MENTOR_IDS")
            users.register(conn, tg_id, row["name"].strip(), role, utcnow())
            counts[role] += 1
    print(f"Seeded {counts['student']} students and {counts['mentor']} mentors into {CONFIG.db_path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "users.csv")
