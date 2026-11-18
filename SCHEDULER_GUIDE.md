# Scheduler Bot Guide

The scheduler bot is the campaign's session calendar: the GM says when they can run, players say when they can play (whole days or just part of a day), and everyone can see it as a month calendar in Discord.

Still to come: a subscribable Google Calendar of confirmed sessions, and reminders.

## Proposing and joining sessions

Anyone in the server can propose a session. The GM approves it, and everyone can see it on the calendar.

1. **Propose:** `/propose date:12 Dec time:7pm goal:Clear the goblin cave party_size:4`. The time can be a start (`7pm`, which runs for 4 hours by default) or a range (`7pm-10pm`). Party size is a guide, not a limit. The bot posts a card in that channel and pings the GM role. You can have 3 proposals waiting at once.
2. **Join:** anyone can press **Join** or **Leave** on the card. The card shows who is in, whether the GM is free then, and a warning if someone in the party has marked themselves busy at that time.
3. **The GM answers** with the buttons under the card (only the GM can use them):
   - **Approve:** the session is confirmed, a Discord event is created, the day turns green on the calendar, and the party is pinged. You also get the three lines to paste into the hub (see below).
   - **Decline.**
   - **Suggest another time:** pops up a form for a new date, time and a note. The proposer gets an **Accept suggestion** button, which confirms the session at the new time straight away.
4. **After approval** the card gets **Google**, **Outlook** and **.ics** buttons so anyone can add that one session to their own calendar. The GM also gets **Change time** (moves the session, the event and the card, and pings the party) and **Cancel session** (deletes the event). The proposer can **Withdraw** a proposal and cancel a confirmed session.

Proposals nobody answered by their start time expire on their own, and the buttons disappear from finished sessions.

`/sessions` lists what's coming up, with links to the cards. `/gm hub_line` gives the GM the next confirmed session as lines to paste into the hub home note:

```yaml
next-session: 2026-12-12 19:00
seats: 4
signed-up: 2
```

The hub is still updated by hand; the bot just gives you the text.

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

## Typing times

Every availability command takes an optional `time`. Leave it empty for the whole day.

| You type | Means |
|---|---|
| *(empty)*, `all day` | the whole day |
| `after 7pm`, `from 7pm`, `7pm+` | 19:00 until the end of the day |
| `before 5pm`, `until 17:30` | the start of the day until then |
| `7pm-10pm`, `7pm to 10pm`, `7-10pm` | a range (am/pm carries across: `9am-5` is 09:00–17:00) |
| `19:00-22:00`, `1900-2200` | 24-hour clock (no am/pm needed with a colon) |
| `morning` / `afternoon` / `evenings` | 06:00–12:00 / 12:00–17:00 / from 18:00 |
| `12-2pm, 7-10pm` | several ranges on the same day |

Times are in the campaign timezone (UK time by default). A bare number like `7-10` is rejected because it could be am or pm; the bot asks you to be specific.

**Setting a day again replaces what you set before for that day.** So `/available 12 Dec after 7pm` followed by `/available 12 Dec after 8pm` leaves you free from 20:00, not 19:00. Setting free time also overrides busy time it overlaps, and the reverse, so "busy all week, then free on Saturday evening" works the way you'd expect. To free up only part of a day, use `/clear_availability <dates> <time>`.

## Player commands

| Command | What it does |
|---|---|
| `/available <dates> [time]` | Say when you can play, e.g. `/available 12 Dec after 7pm` |
| `/unavailable <dates> [time]` | Say when you can't, e.g. `/unavailable 20 Dec - 3 Jan` or `/unavailable 14 Dec 12-4pm` |
| `/clear_availability <dates> [time]` | Remove your availability (all of it for those days, or just that time of day) |
| `/my_availability` | Your availability for the next 90 days, grouped by time (only you see it) |
| `/calendar [month] [private]` | Month view as an image. `month` can be `January`, `next` or `2027-01` |
| `/propose <date> <time> <goal> [party_size]` | Propose a session for the GM to approve |
| `/sessions [private]` | What's coming up, with links |
| `/whosfree <date>` | GM status, who is free or busy, and **when the GM and players overlap**, with the most people first |
| `/schedule_help` | Short help |

## GM commands

Uses the same `GM_ROLE_ID` as the other bots (server admins always count).

| Command | What it does |
|---|---|
| `/gm open <dates> [time] [note]` | When you can run. The note (e.g. "at the inn") shows on the calendar |
| `/gm block <dates> [time] [note]` | When you can't. Blocked time is cut out of any open time |
| `/gm clear <dates> [time]` | Remove your availability |
| `/gm hub_line` | The next confirmed session as lines to paste into the hub home note |
| `/gm pin_calendar` | Post this month and next in the current channel and pin them. They redraw themselves whenever anyone changes availability, and just after midnight. Running it again moves the pinned calendar and deletes the old one |

Typical use: `/gm open 3, 5, 12, 19 Dec after 7pm` then `/gm block 23 Dec - 1 Jan note:Christmas`. Day cells on the calendar show your times ("from 19:00") and the note.

## Setup

1. **Create the bot.** In the [Discord Developer Portal](https://discord.com/developers/applications) make a new application, add a bot and copy its token. It needs no privileged intents.
2. **Invite it.** OAuth2 → URL Generator → scopes `bot` and `applications.commands`; permissions **View Channels, Send Messages, Embed Links, Attach Files, Read Message History, Manage Messages** (only for pinning) and **Manage Events** (so it can create the Discord event when you approve a session). Without Manage Events everything still works; approving just tells you it couldn't create the event.
3. **Configure.** Add `SCHEDULER_BOT_TOKEN=...` to `.env`. `GM_ROLE_ID` is shared with the other bots, and it is also the role pinged when someone proposes a session. Adjust `scheduler_config.yaml` if you want a different calendar title or backup count.
4. **Install.** `pip install -r requirements.txt` (adds Pillow), then `./deploy.sh`, or install just this service:
   ```bash
   sed -e "s|/home/YOUR_USERNAME/player_tracker|$PWD|g" -e "s|YOUR_USERNAME|$(whoami)|g" scheduler_bot.service | sudo tee /etc/systemd/system/scheduler_bot.service
   sudo systemctl daemon-reload && sudo systemctl enable --now scheduler_bot
   ```
5. **Pin a calendar.** In your scheduling channel, run `/gm pin_calendar`.

Slash commands can take a few minutes to appear the first time.

## Data, backups and security

- Data lives in `data/schedule.db` (SQLite), **not** the Google Sheet. Scheduling has lots of small writes from several people at once, and SQLite handles that safely. The other bots are unchanged.
- Upgrading from the first version (whole days only) is automatic on startup: existing days become "all day", and a copy of the old data is kept next to the database as `schedule.db.before-v2`.
- A backup is taken at startup and every 24 hours into `backups/schedule-YYYYMMDD-HHMM.db`; the newest 14 are kept. To restore: stop the bot, copy a backup over `data/schedule.db`, start it.
- `data/` and `backups/` are gitignored. `deploy.sh` makes them owner-only and sets `.env` and `credentials.json` to `chmod 600`.
- The bot never pings anyone: all mentions are switched off, so text people type (names, notes) can't trigger `@everyone`.
- Notes and names are stored as plain text, trimmed to one line and capped in length.
- Availability stays inside Discord. Nothing is published outside it. The calendar buttons carry only the session's title, time, goal and the names of the party; the `.ics` is generated on demand and nothing is hosted.
- Button clicks are checked every time: the GM-only buttons check the GM role on each click, and a stale or double click can't approve, decline or cancel something twice.
- Only the people who joined a session (and the GM role, for new proposals) are ever pinged. A goal or note containing `@everyone` is just text.

Logs: `./logs.sh scheduler -f`.
