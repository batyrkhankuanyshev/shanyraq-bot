# Shanyraq Hostel Bot

A Telegram bot for a student hostel (12 students + 5 mentors): washing machine booking with waitlist,
maintenance and anonymous reports, cleaning and supplies rotations, announcements, guest log, events
calendar, and a mentor admin panel. Almost everything works through buttons.

- **Stack:** Python 3.11+, [aiogram 3](https://docs.aiogram.dev), SQLite, and a built-in scheduler that runs every minute.
- **Timezone:** Hong Kong (`HOSTEL_TZ=Asia/Hong_Kong`). Times are stored in UTC and shown in local time.
- **Language:** the whole bot talks to users in **Kazakh** (Cyrillic): menus, buttons, reminders, and day and month names (e.g. «Бс, 1 қаз 09:30»).
  Slash commands stay in Latin letters (`/book`, `/report`…) because Telegram requires it; their descriptions in the menu are in Kazakh.
  All messages live in `bot/handlers/*.py` and `bot/services/*.py`.
- **Runs on:** a Raspberry Pi or any small Linux machine. It uses polling, so you don't need a domain, HTTPS or port forwarding.

---

## 1. Create the bot in @BotFather

1. In Telegram, open **@BotFather** (it has the blue checkmark) and send `/newbot`. Choose a display name and a username ending in `bot`.
2. BotFather replies with a **token**. It works like a password:
   - Never post it in a chat or commit it to git.
   - If it ever leaks, send `/revoke` to BotFather and use the new token.
3. `/setdescription` sets the text people see before they press Start. `/setabouttext` sets a short bio and `/setuserpic` sets the avatar.
4. You don't need `/setcommands`. The bot sets its own command menu every time it starts.
5. Optional hostel group, if you want announcements and new events posted there too:
   - Use `/setjoingroups` → Enable and add the bot to the group.
   - Make the bot an **admin** so it can pin messages.
   - Leave `/setprivacy` **enabled**. The bot never needs to read group chat.
   - To get the group ID, add @RawDataBot to the group for a moment. The ID is a negative number like `-100…`.
6. Collect IDs:
   - Each mentor sends any message to **@userinfobot** and gives you the number it replies with.
   - Collect the students' IDs the same way if you want to pre-register them. Otherwise students can register themselves with `/start`.

> Telegram only lets a bot message people who have pressed **Start** on it. Ask all 17 people to open the bot once,
> otherwise they won't get reminders.

## 2. Install on the Raspberry Pi

```bash
sudo apt update && sudo apt install -y python3 python3-venv git
git clone https://github.com/batyrkhankuanyshev/shanyraq-bot.git
cd shanyraq-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
nano .env        # paste BOT_TOKEN, MENTOR_IDS, optional GROUP_CHAT_ID
```

## 3. Register everyone (seed script)

```bash
cp users.example.csv users.csv
nano users.csv   # telegram_id,name,role  — 12 students + 5 mentors
.venv/bin/python -m scripts.seed users.csv
```

You can run the seed script again at any time; it updates existing users instead of creating duplicates. `users.csv` is git-ignored.
You can skip seeding entirely: anyone who sends `/start` gets registered. People whose ID is in `MENTOR_IDS` become mentors;
everyone else becomes a student. Mentors get a notification for every new sign-up so they can deactivate strangers.

## 4. Run it

Try it in the foreground first:

```bash
.venv/bin/python -m bot.main
```

Then install it as a service, so it starts on boot and restarts itself after a crash:

```bash
sudo cp deploy/shanyraq-bot.service /etc/systemd/system/   # edit User/paths if not "pi"
sudo systemctl daemon-reload
sudo systemctl enable --now shanyraq-bot
journalctl -u shanyraq-bot -f      # live logs
```

Nightly backups, kept for 30 days in `backups/`:

```bash
crontab -e
30 3 * * * /home/pi/shanyraq-bot/deploy/backup.sh
```

To update: `git pull && sudo systemctl restart shanyraq-bot`.

## 5. Configuration (`.env`)

| Variable | Meaning |
|---|---|
| `BOT_TOKEN` | Token from @BotFather |
| `MENTOR_IDS` | Comma-separated Telegram IDs of the mentor whitelist |
| `HOSTEL_TZ` | Timezone, default `Asia/Hong_Kong` |
| `DB_PATH` | SQLite file, default `shanyraq.db` |
| `GROUP_CHAT_ID` | Optional group: announcements to "Everyone" are posted there too |
| `EVENTS_CHAT_ID` / `EVENTS_THREAD_ID` | Optional chat or forum topic for new events (defaults to the group) |
| `DUTY_WEEKDAY` | Cleaning day, `0`=Mon … `5`=Sat (default) … `6`=Sun |
| `QUIET_HOURS_REMINDER` / `QUIET_HOURS_TEXT` | Optional daily 22:00 quiet-hours message |

Rules you can change at the top of `bot/services/washer.py`:
- the 10 slot times
- the cycle lengths
- 2 bookings per week
- 3 days ahead
- 3 no-shows leads to a 7-day ban
- 4 recurring slots
- the 5-minute claim window

---

## How it works

### Main menu
- **Students:** Book Washer · My Bookings · Report · Duty · Announcements · Guests · Events
- **Mentors:** everything students have, plus Admin Panel · Announce · Guests Today · Stats

### Washer
- **Slots:** 08:00, 09:30, 11:00, 12:30, 14:00, 15:30, 17:00, 18:30, 20:00, 21:30.
- **Cycles:** Quick 60m, Normal 90m, Heavy 120m. Heavy is only allowed when the next slot is free, because it runs 30 minutes into it. Whoever books the slot after a Heavy wash gets a warning.
- **Fair use:**
  - Booking opens up to 3 days ahead.
  - Students get max 2 bookings per calendar week. Cancelled bookings don't count.
  - Cancels less than 30 minutes before the slot are flagged in the stats so mentors can spot abuse.
- **No double bookings:** a partial `UNIQUE` index on the slot in SQLite makes this impossible. If two people tap at once, one insert wins and the other person sees "Just taken — join the waitlist?"
- **Live cycle:**
  1. 15 minutes before: silent reminder with [Started] [Cancel].
  2. At the start time: a "tap Started" prompt.
  3. 15 minutes after the start: "Still using it?", sent loud.
  4. 10 minutes later with no answer: **no-show**. The slot is freed and the waitlist is offered it.
  5. On the 3rd no-show in a month: the student is banned from booking for 7 days and mentors get a ping. A mentor can lift the ban.
- **End of slot:** [Done] [Running late].
  - Running late gives 15 extra minutes and warns the next person.
  - With no answer, the booking closes automatically at its scheduled end.
  - [Done early] tells the next person the washer is free.
- **Waitlist:** when a slot frees up, the first person in line gets a DM with 5 minutes to claim it. If they don't, the offer passes to the next person.
- **Washer broke:** tapping this blocks the washer for the rest of the day, cancels that day's bookings with an apology, and files a report for the mentors.
- **Weekly recurring slot:** a student can request one; a mentor must approve it, and at most 4 are allowed at a time. The bot books it automatically each week.

### Reports
- **Maintenance:**
  1. Pick the area, then describe the problem (a photo is optional).
  2. If the same area was reported in the last 2 hours, the bot asks whether it's a duplicate.
  3. The reporter chooses whether to include their name.
  4. Mentors move the report through open → in progress → fixed. A reporter who included their name gets a DM at each step.
- **Anonymous:** see the anonymity section below.

### Anonymity (anonymous reports)
- The `anon_reports` table has **no column that could hold an identity**, and handlers never log user IDs or messages. aiogram's per-update log is turned off.
- The 3-reports-per-day limit uses an HMAC of the user ID with a random key. The key lives **in memory only** and is replaced every day, so nothing linking anyone to a report is ever stored.
- Replies go through a random code (`A-XXXXXXXX`) that only the reporter sees. The database stores only its SHA-256 hash.
  - The bot never messages the reporter first. They check for replies with Report → Check anonymous replies.
  - If they lose the code, the replies can't be recovered.
- The report time is stored rounded to the hour.
- `tests/test_reports.py` checks every table after a report is filed and confirms the reporter's ID appears nowhere.

### Rotations
- **Cleaning:**
  - Four zones: Kitchen, Bathroom, Common room, Hallway.
  - Every Sunday at 20:00 the bot assigns the next four students, in strict order, for the coming week. Zones shift each cycle so people don't always get the same one.
  - On cleaning day (Saturday by default) the student gets a DM at 08:00 with a [Mark done] button. When they tap it, the next person is notified.
  - If it isn't marked done by 23:30, mentors get a ping.
- **Supplies:**
  - Anyone can use `/low` to add an item to the shared list.
  - Every Sunday at 18:00 the next student becomes the buyer for the week and gets the list with buttons to tick items off.
  - The buyer can upload a receipt photo. They also get a DM whenever a new item is added.
- **Pausing:** a mentor can pause a student, e.g. while they're away, and the rotation skips them. Deactivated students are skipped permanently.
- **Fairness:** the full history is kept. Duty → Rotation history shows per-student totals.

### Announcements
- Mentors choose title → text → optional photo → audience (Everyone / Students / Mentors) → pin → silent or loud → when: now, tonight 22:00, tomorrow 08:00, or a custom date.
- Scheduled announcements survive restarts.
- Mentors can edit sent announcements (every copy is updated), pin them later, or cancel scheduled ones.
- `/announcements` shows the latest ones.

### Guests
- Registration asks for name → arrival day and time → duration. Each student can have at most 2 active guests.
- **Every guest needs mentor approval**, both day visits and overnight stays. Mentors get [Approve] [Reject] buttons, and the host is told the decision (with the reason if rejected).
- If no mentor decides before the visit ends, the request expires and the host is told.
- Guests are checked out automatically at departure time, or the host can tap Check out.
- Mentors see Guests Today.

### Events
- Anyone can create an event: title → description → location → day → time → weekly?
- Everyone gets an invitation with [Going] [Maybe] [Can't] buttons.
- Everyone who answered Going gets a reminder 1 hour before.
- Weekly events create next week's copy automatically.
- The creator or any mentor can edit or delete an event. People who RSVP'd are told about time changes and cancellations.

### Admin panel (`/admin`)
| Area | What mentors can do |
|---|---|
| Washer | See every booking, force-free, force-book, maintenance mode on/off (`/washer_maintenance`), recurring approvals, stats |
| Rotation | Reassign any duty, pause/unpause students, generate the current week, view history |
| Reports | Open maintenance reports with status buttons; anonymous reports with reply/close |
| Announcements | Post, edit, pin, cancel scheduled |
| Guests | Approve or reject guest requests, today's list |
| Users | Add (by ID), change role, mute, washer ban/unban, rotation pause, deactivate (cancels future bookings, frees recurring slot, removes from rotation) |
| Stats | Washer usage, peak slots, top users, no-show rates, late cancels, duty completion |
| Audit log | Every mentor action: who, what, when |

### Background jobs
One loop runs every minute. Every job checks the database for whatever is due, so after a restart or downtime the bot simply catches up. For example, a scheduled announcement missed while the Pi was off gets sent when it comes back.

| When | What |
|---|---|
| every minute | washer reminders, start prompts, "still using it?", no-shows, end prompts, 5-minute claim windows, recurring bookings, scheduled announcements, guest check-outs, event reminders and weekly copies |
| daily 08:00 | cleaning duty DMs |
| daily 22:00 | quiet-hours reminder (if enabled; skipped if the bot was down at that hour) |
| daily 23:30 | missed-duty pings to mentors |
| Sunday 18:00 / 20:00 | next week's buyer / cleaning rotation |
| monthly | no-show counts are per calendar month, so they reset on the 1st by themselves |

---

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The tests cover the tricky parts:
- concurrent booking: 12 threads on separate DB connections tap the same slot and exactly one wins
- no-show detection, the 3-strike ban, and catching up after a restart
- the waitlist claim window expiring and passing to the next person
- anonymous report storage: no ID anywhere in the database or logs, the rate limit, and the reply relay
- fair rotation, pausing, reminders and missed-duty pings
- guests, events and scheduled announcements
- an end-to-end test that drives the real aiogram dispatcher through every menu and admin button

**Code layout:**
- `bot/services/` — business logic. It returns `Notice` objects and never calls Telegram.
- `bot/handlers/` — buttons and conversation flows.
- `bot/jobs.py` — the scheduler.
- `bot/sender.py` — delivers messages, with retries and rate limiting.
