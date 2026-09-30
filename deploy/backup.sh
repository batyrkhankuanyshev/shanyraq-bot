#!/bin/sh
# Nightly SQLite backup (safe while the bot is running). Keeps 30 days.
# crontab -e  →  30 3 * * * /home/pi/shanyraq-bot/deploy/backup.sh
set -e
cd "$(dirname "$0")/.."
mkdir -p backups
python3 -c "import sqlite3,sys; s=sqlite3.connect('shanyraq.db'); d=sqlite3.connect(sys.argv[1]); s.backup(d); d.close()" \
  "backups/shanyraq-$(date +%F).db"
find backups -name 'shanyraq-*.db' -mtime +30 -delete
