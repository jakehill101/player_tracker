# Scheduler Bot Guide

The scheduler bot is the campaign's session calendar. This is phase 1: the GM marks when they can run, players mark when they can play, and everyone can see it as a month calendar in Discord.

Still to come: player session proposals with GM approval, confirmed sessions pushed to a shared Google Calendar (subscribable from Google, Outlook or Apple), and reminders.

## Typing dates

Every command that takes dates accepts the same loose formats:

| You type | Means |
|---|---|
| `12 Dec`, `Dec 12`, `12th December` | one day |
| `3-7 Dec`, `3 Dec to 7 Dec` | a range |
| `5, 12, 19 Dec` or `5 and 12 Dec` | several days (bare numbers borrow the month) |
| `28 Dec - 3 Jan` | a range across months or the new year |
| `December`, `Dec - Jan` | whole months |
| `today`, `tomorrow`, `2026-12-03`, `3/12` | also fine (`3/12` is 3 December) |

Without a year, the next one that isn't in the past is used, so `5 Jan` in November means next January. Past dates are skipped. One command can cover up to 120 days.

## Player commands

| Command | What it does |
|---|---|
| `/available <dates>` | Mark days you can play |
| `/unavailable <dates>` | Mark days you can't |
| `/clear_availability <dates>` | Remove your marks |
| `/my_availability` | Your marks for the next 90 days (only you see it) |
| `/calendar [month] [private]` | Month view as an image. `month` can be `January`, `next` or `2027-01` |
| `/whosfree <date>` | GM status and who is free or busy on one day |
| `/schedule_help` | Short help |

## GM commands

Uses the same `GM_ROLE_ID` as the other bots (server admins always count).

| Command | What it does |
|---|---|
| `/gm open <dates> [note]` | Days you can run. The note (e.g. "evenings only") shows on the calendar |
| `/gm block <dates> [note]` | Days you can't |
| `/gm clear <dates>` | Remove your marks |
| `/gm pin_calendar` | Post this month and next in the current channel and pin them. They redraw themselves whenever anyone changes availability, and just after midnight. Running it again moves the pinned calendar and deletes the old one |

Typical use: block out the month in one go, e.g. `/gm open 3, 5, 12, 19 Dec evenings` then `/gm block 23 Dec - 1 Jan Christmas`.

## Setup

1. **Create the bot.** In the [Discord Developer Portal](https://discord.com/developers/applications) make a new application, add a bot and copy its token. It needs no privileged intents.
2. **Invite it.** OAuth2 → URL Generator → scopes `bot` and `applications.commands`; permissions **View Channels, Send Messages, Embed Links, Attach Files, Read Message History, Manage Messages** (Manage Messages is only for pinning).
3. **Configure.** Add `SCHEDULER_BOT_TOKEN=...` to `.env`. `GM_ROLE_ID` is shared with the other bots. Adjust `scheduler_config.yaml` if you want a different calendar title or backup count.
4. **Install.** `pip install -r requirements.txt` (adds Pillow), then `./deploy.sh`, or install just this service:
   ```bash
   sed -e "s|/home/YOUR_USERNAME/player_tracker|$PWD|g" -e "s|YOUR_USERNAME|$(whoami)|g" scheduler_bot.service | sudo tee /etc/systemd/system/scheduler_bot.service
   sudo systemctl daemon-reload && sudo systemctl enable --now scheduler_bot
   ```
5. **Pin a calendar.** In your scheduling channel, run `/gm pin_calendar`.

Slash commands can take a few minutes to appear the first time.

## Data, backups and security

- Data lives in `data/schedule.db` (SQLite), **not** the Google Sheet. Scheduling has lots of small writes from several people at once, and SQLite handles that safely. The other bots are unchanged.
- A backup is taken at startup and every 24 hours into `backups/schedule-YYYYMMDD-HHMM.db`; the newest 14 are kept. To restore: stop the bot, copy a backup over `data/schedule.db`, start it.
- `data/` and `backups/` are gitignored. `deploy.sh` makes them owner-only and sets `.env` and `credentials.json` to `chmod 600`.
- The bot never pings anyone: all mentions are switched off, so text people type (names, notes) can't trigger `@everyone`.
- Notes and names are stored as plain text, trimmed to one line and capped in length.
- Availability stays inside Discord. Nothing is published outside it.

Logs: `./logs.sh scheduler -f`.
