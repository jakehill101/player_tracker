"""
Discord bot for scheduling West Marches sessions.

Phase 1: GM and player availability, and a month calendar posted as an image.
Data lives in a local SQLite file (see schedule_store.py), not Google Sheets.
"""
import asyncio
import io
import os
from datetime import date, datetime, timedelta
from typing import Optional

import discord
import yaml
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python 3.8
    from backports.zoneinfo import ZoneInfo

from calendar_links import google_subscribe_url, outlook_subscribe_url, webcal_url
from calendar_render import month_bounds, next_month, render_month
from google_calendar import CalendarError, GoogleCalendarClient
from schedule_dates import DateParseError, format_days, parse_dates, parse_month
from schedule_times import (
    TimeParseError, best_slots, format_windows, gm_calendar_days, group_days_by_windows,
    is_all_day, parse_times, player_calendar_days, player_day_summary,
)
from schedule_store import SESSION_LIVE, SESSION_OPEN, ScheduleStore, prune_backups
from scheduler_sessions import SessionManager

load_dotenv()

TOKEN = os.getenv('SCHEDULER_BOT_TOKEN')
GM_ROLE_ID = os.getenv('GM_ROLE_ID')
# Optional: a dedicated Google Calendar the bot keeps up to date with confirmed sessions
GOOGLE_CALENDAR_ID = os.getenv('GOOGLE_CALENDAR_ID')
# Optional: that calendar's "secret address in iCal format", shown to players by /subscribe
GOOGLE_CALENDAR_ICAL_URL = os.getenv('GOOGLE_CALENDAR_ICAL_URL')

DEFAULT_CONFIG = {
    'timezone': 'Europe/London',
    'database': 'data/schedule.db',
    'backup_dir': 'backups',
    'backup_keep': 14,
    'calendar_title': 'Tales from Fellgard · sessions',
    'max_days_per_command': 120,
    'default_session_hours': 4,
    'default_party_size': 5,
    'max_party_size': 12,
    'max_open_proposals': 3,
    'max_days_ahead': 180,
    'event_location': 'Discord',
    'event_name_prefix': 'Fellgard',
    'reminder_hours_before': [24, 1],
    'day_post_time': '09:00',
    'gm_nudge_time': '09:00',
    'gm_nudge_after_hours': 24,
}


def load_config(path: str = 'scheduler_config.yaml') -> dict:
    config = DEFAULT_CONFIG.copy()
    try:
        with open(path, 'r') as f:
            config.update(yaml.safe_load(f) or {})
    except FileNotFoundError:
        print(f'Warning: {path} not found. Using default configuration.')
    except Exception as e:
        print(f'Error loading {path}: {e}. Using default configuration.')
    return config


config = load_config()
TZ = ZoneInfo(config['timezone'])
MAX_DAYS = int(config['max_days_per_command'])

intents = discord.Intents.default()
bot = commands.Bot(
    command_prefix='!',
    intents=intents,
    # Nothing this bot posts may ping anyone: player-typed text goes into embeds.
    allowed_mentions=discord.AllowedMentions.none(),
)
store: Optional[ScheduleStore] = None
sessions: Optional[SessionManager] = None
_refresh_lock = asyncio.Lock()

PIN_CHANNEL_KEY = 'pinned_calendar_channel'
PIN_MESSAGE_KEY = 'pinned_calendar_message'


def build_google_client() -> Optional[GoogleCalendarClient]:
    if not GOOGLE_CALENDAR_ID:
        return None
    try:
        client = GoogleCalendarClient(GOOGLE_CALENDAR_ID, 'credentials.json')
    except Exception as e:
        print(f'Google Calendar sync is off: {e}')
        return None
    print(f'Google Calendar sync is on (share the calendar with {client.service_account_email})')
    return client


def today() -> date:
    return datetime.now(TZ).date()


def user_is_gm(user) -> bool:
    perms = getattr(user, 'guild_permissions', None)
    is_admin = bool(perms and perms.administrator)
    if not GM_ROLE_ID:
        return is_admin
    try:
        role_id = int(GM_ROLE_ID)
    except (ValueError, TypeError):
        return is_admin
    return is_admin or any(r.id == role_id for r in getattr(user, 'roles', []))


def is_gm():
    async def predicate(interaction: discord.Interaction) -> bool:
        return user_is_gm(interaction.user)
    return app_commands.check(predicate)


def parse_future(spec: str):
    """Parse a date spec and drop past dates. Returns (dates, n_dropped)."""
    days = parse_dates(spec, today(), MAX_DAYS)
    t = today()
    future = [d for d in days if d >= t]
    return future, len(days) - len(future)


TIME_HELP = "e.g. 'after 7pm', '7pm-10pm', 'evenings'. Leave empty for all day"


def at(windows) -> str:
    """' (from 19:00)' for a partial day, '' for a whole day."""
    return '' if is_all_day(windows) else f' ({format_windows(windows)})'


def past_note(dropped: int) -> str:
    if not dropped:
        return ''
    return f"\n-# Skipped {dropped} date{'s' if dropped != 1 else ''} already in the past."


async def render_month_file(year: int, month: int, filename: str = None) -> discord.File:
    start, end = month_bounds(year, month)
    gm = gm_calendar_days(await asyncio.to_thread(store.gm_days, start, end))
    players = player_calendar_days(await asyncio.to_thread(store.player_days, start, end))
    booked = await sessions.calendar_sessions(start, end)
    png = await asyncio.to_thread(
        render_month, year, month, gm, players, today(), config['calendar_title'], booked
    )
    return discord.File(io.BytesIO(png), filename=filename or f'calendar-{year}-{month:02d}.png')


async def refresh_pinned_calendar():
    """Redraw the pinned calendar (this month and next), if one has been set up."""
    if not store:
        return
    async with _refresh_lock:
        channel_id = await asyncio.to_thread(store.get_setting, PIN_CHANNEL_KEY)
        message_id = await asyncio.to_thread(store.get_setting, PIN_MESSAGE_KEY)
        if not channel_id or not message_id:
            return
        try:
            channel = bot.get_channel(int(channel_id)) or await bot.fetch_channel(int(channel_id))
            message = await channel.fetch_message(int(message_id))
        except (discord.NotFound, discord.Forbidden):
            print('Pinned calendar message is gone; clearing it. Run /gm pin_calendar to set up again.')
            await asyncio.to_thread(store.set_setting, PIN_MESSAGE_KEY, None)
            return
        except discord.HTTPException as e:
            print(f'Could not fetch pinned calendar: {e}')
            return
        t = today()
        y2, m2 = next_month(t.year, t.month)
        files = [
            await render_month_file(t.year, t.month, 'calendar-this-month.png'),
            await render_month_file(y2, m2, 'calendar-next-month.png'),
        ]
        try:
            await message.edit(content=pinned_text(), attachments=files)
        except discord.HTTPException as e:
            print(f'Could not update pinned calendar: {e}')


def pinned_text() -> str:
    stamp = int(datetime.now(TZ).timestamp())
    return (
        '**Session calendar**: this month and next. An amber badge shows when the GM is free.\n'
        'Add your own availability with `/available` and `/unavailable`, and propose a session with `/propose`. '
        f'`/calendar` shows any month.\n-# Updated <t:{stamp}:R>'
    )


_refresh_waiting = False


def schedule_refresh():
    """Redraw the pinned calendar soon. Several changes close together (a few people
    joining at once) share one redraw."""
    global _refresh_waiting
    if _refresh_waiting:
        return
    _refresh_waiting = True

    async def later():
        global _refresh_waiting
        await asyncio.sleep(2)
        _refresh_waiting = False
        await refresh_pinned_calendar()
        if sessions:
            await sessions.sync_google()

    asyncio.create_task(later())


async def send_error(interaction: discord.Interaction, message: str):
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


# ---------------------------------------------------------------- startup

@bot.event
async def on_ready():
    global store
    print(f'{bot.user} has connected to Discord!')
    global sessions
    if store is None:
        store = ScheduleStore(config['database'])
        print(f"Schedule database: {config['database']} (schema v{store.schema_version})")
    if sessions is None:
        sessions = SessionManager(bot, store, config, TZ, user_is_gm, GM_ROLE_ID, schedule_refresh,
                                  gcal=build_google_client())
        await sessions.register_views()
        if sessions.gcal:
            asyncio.create_task(sessions.sync_google())   # catch up anything missed while the bot was off
    if not session_housekeeping.is_running():
        session_housekeeping.start()
    if not reminder_loop.is_running():
        reminder_loop.start()
    if not nightly_backup.is_running():
        nightly_backup.start()
    if not daily_refresh.is_running():
        daily_refresh.start()
    try:
        synced = await bot.tree.sync()
        print(f'Synced {len(synced)} command(s)')
    except Exception as e:
        print(f'Failed to sync commands: {e}')


@tasks.loop(hours=24)
async def nightly_backup():
    """Copy the database to backups/ once a day and keep the newest N."""
    if not store:
        return
    stamp = datetime.now(TZ).strftime('%Y%m%d-%H%M')
    dest = os.path.join(config['backup_dir'], f'schedule-{stamp}.db')
    try:
        await asyncio.to_thread(store.backup, dest)
        removed = await asyncio.to_thread(prune_backups, config['backup_dir'], int(config['backup_keep']))
        print(f'Backed up schedule to {dest} (pruned {len(removed)})')
    except Exception as e:
        print(f'Schedule backup failed: {e}')


@tasks.loop(minutes=10)
async def session_housekeeping():
    """Expire proposals nobody answered in time and mark past sessions finished."""
    if sessions:
        try:
            await sessions.housekeeping()
            await sessions.sync_google()    # also retries anything that failed earlier
        except Exception as e:
            print(f'Session housekeeping failed: {e!r}')


@tasks.loop(minutes=1)
async def reminder_loop():
    """Send any reminders that have come due, and the GM's daily nudge."""
    if sessions:
        try:
            await sessions.send_reminders()
            await sessions.gm_digest()
        except Exception as e:
            print(f'Reminder run failed: {e!r}')


@tasks.loop(hours=1)
async def daily_refresh():
    """Redraw the pinned calendar after midnight so 'today' and past days stay right."""
    if datetime.now(TZ).hour == 0:
        await refresh_pinned_calendar()


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await send_error(interaction, 'Only the GM can do that.')
        return
    original = getattr(error, 'original', error)
    if isinstance(original, (DateParseError, TimeParseError)):
        await send_error(interaction, str(original))
        return
    print(f'Command error in /{interaction.command.qualified_name if interaction.command else "?"}: {error!r}')
    await send_error(interaction, 'Something went wrong. The GM can check the bot logs.')


# ------------------------------------------------------------ GM commands

gm = app_commands.Group(name='gm', description='GM scheduling commands', guild_only=True)


@gm.command(name='open', description='Mark when you can run a session')
@app_commands.describe(
    dates="e.g. '3-7 Dec', '5, 12, 19 Dec', 'December'",
    time=TIME_HELP,
    note="Optional, shown on the calendar",
)
@is_gm()
async def gm_open(interaction: discord.Interaction, dates: str, time: Optional[str] = None, note: Optional[str] = ''):
    days, dropped = parse_future(dates)
    windows = parse_times(time)
    if not days:
        await send_error(interaction, 'All of those dates are in the past.')
        return
    await asyncio.to_thread(store.set_gm_days, days, 'open', windows, note or '')
    await interaction.response.send_message(
        f'Marked you **free** on {format_days(days)}{at(windows)}.' + (f' Note: {note}' if note else '') + past_note(dropped),
        ephemeral=True,
    )
    schedule_refresh()


@gm.command(name='block', description="Mark when you can't run a session")
@app_commands.describe(dates="e.g. '20 Dec - 3 Jan', 'January'", time=TIME_HELP, note='Optional note shown on the calendar')
@is_gm()
async def gm_block(interaction: discord.Interaction, dates: str, time: Optional[str] = None, note: Optional[str] = ''):
    days, dropped = parse_future(dates)
    windows = parse_times(time)
    if not days:
        await send_error(interaction, 'All of those dates are in the past.')
        return
    await asyncio.to_thread(store.set_gm_days, days, 'blocked', windows, note or '')
    await interaction.response.send_message(
        f'Marked you **away** on {format_days(days)}{at(windows)}.' + past_note(dropped), ephemeral=True
    )
    schedule_refresh()


@gm.command(name='clear', description='Remove your availability from dates')
@app_commands.describe(dates="e.g. '3-7 Dec' or 'December'", time="Only clear this time of day. Leave empty to clear the whole day")
@is_gm()
async def gm_clear(interaction: discord.Interaction, dates: str, time: Optional[str] = None):
    days, _ = parse_future(dates)
    windows = parse_times(time)
    changed = await asyncio.to_thread(store.clear_gm_days, days, windows)
    await interaction.response.send_message(
        f'Cleared your availability on {changed} day{"s" if changed != 1 else ""}{at(windows)}.', ephemeral=True
    )
    schedule_refresh()


@gm.command(name='pin_calendar', description='Post a calendar here that updates itself, and pin it')
@is_gm()
async def gm_pin_calendar(interaction: discord.Interaction):
    channel = interaction.channel
    await interaction.response.defer(ephemeral=True)
    t = today()
    y2, m2 = next_month(t.year, t.month)
    files = [
        await render_month_file(t.year, t.month, 'calendar-this-month.png'),
        await render_month_file(y2, m2, 'calendar-next-month.png'),
    ]
    try:
        message = await channel.send(content=pinned_text(), files=files)
    except discord.Forbidden:
        await interaction.followup.send("I can't post in this channel.", ephemeral=True)
        return

    pinned = True
    try:
        await message.pin(reason='Session calendar')
    except discord.HTTPException:
        pinned = False

    # Replace any previous pinned calendar
    old_channel = await asyncio.to_thread(store.get_setting, PIN_CHANNEL_KEY)
    old_message = await asyncio.to_thread(store.get_setting, PIN_MESSAGE_KEY)
    await asyncio.to_thread(store.set_setting, PIN_CHANNEL_KEY, str(channel.id))
    await asyncio.to_thread(store.set_setting, PIN_MESSAGE_KEY, str(message.id))
    if old_channel and old_message:
        try:
            old = await (bot.get_channel(int(old_channel)) or await bot.fetch_channel(int(old_channel))) \
                .fetch_message(int(old_message))
            await old.delete()
        except discord.HTTPException:
            pass

    await interaction.followup.send(
        'Calendar posted' + (' and pinned.' if pinned else ". I couldn't pin it (needs Manage Messages), so pin it by hand."),
        ephemeral=True,
    )


bot.tree.add_command(gm)


# -------------------------------------------------------- player commands

async def set_own_availability(interaction: discord.Interaction, dates: str, time: Optional[str], free: bool):
    days, dropped = parse_future(dates)
    windows = parse_times(time)
    if not days:
        await send_error(interaction, 'All of those dates are in the past.')
        return
    await asyncio.to_thread(
        store.set_player_days, interaction.user.id, interaction.user.display_name, days, free, windows
    )
    await interaction.response.send_message(
        f"Marked you **{'free' if free else 'busy'}** on {format_days(days)}{at(windows)}." + past_note(dropped),
        ephemeral=True,
    )
    schedule_refresh()


@bot.tree.command(name='available', description="Say when you're free to play")
@app_commands.describe(dates="e.g. '3-7 Dec', '5, 12, 19 Dec', 'December'", time=TIME_HELP)
@app_commands.guild_only()
async def available(interaction: discord.Interaction, dates: str, time: Optional[str] = None):
    await set_own_availability(interaction, dates, time, True)


@bot.tree.command(name='unavailable', description="Say when you can't play")
@app_commands.describe(dates="e.g. '20 Dec - 3 Jan'", time=TIME_HELP)
@app_commands.guild_only()
async def unavailable(interaction: discord.Interaction, dates: str, time: Optional[str] = None):
    await set_own_availability(interaction, dates, time, False)


@bot.tree.command(name='clear_availability', description='Remove your availability from dates')
@app_commands.describe(dates="e.g. '3-7 Dec' or 'December'", time="Only clear this time of day. Leave empty to clear the whole day")
@app_commands.guild_only()
async def clear_availability(interaction: discord.Interaction, dates: str, time: Optional[str] = None):
    days, _ = parse_future(dates)
    windows = parse_times(time)
    changed = await asyncio.to_thread(store.clear_player_days, interaction.user.id, days, windows)
    await interaction.response.send_message(
        f'Cleared your availability on {changed} day{"s" if changed != 1 else ""}{at(windows)}.', ephemeral=True
    )
    schedule_refresh()


@bot.tree.command(name='my_availability', description="See the availability you've set over the next 90 days")
@app_commands.guild_only()
async def my_availability(interaction: discord.Interaction):
    t = today()
    own = await asyncio.to_thread(store.player_own_days, interaction.user.id, t, t + timedelta(days=90))
    free, busy = {}, {}
    for day, rows in own.items():
        f, b = player_day_summary(rows)
        if f:
            free[day] = f
        if b:
            busy[day] = b
    if not free and not busy:
        await interaction.response.send_message(
            "You haven't set any availability yet. Try `/available 3-7 Dec` or `/available 12 Dec after 7pm`.",
            ephemeral=True,
        )
        return

    def section(title, per_day):
        if not per_day:
            return f'**{title}:** none'
        lines = [f'• {label}: {format_days(days)}' for label, days in group_days_by_windows(per_day).items()]
        return f'**{title}**\n' + '\n'.join(lines)

    msg = section('Free', free) + '\n\n' + section('Busy', busy)
    await interaction.response.send_message(msg[:1900], ephemeral=True)


@bot.tree.command(name='calendar', description='Show the session calendar for a month')
@app_commands.describe(
    month="e.g. 'January', 'next', '2027-01'. Leave empty for this month",
    private='Only show it to you',
)
@app_commands.guild_only()
async def calendar_cmd(interaction: discord.Interaction, month: Optional[str] = None, private: bool = False):
    year, mon = parse_month(month, today())
    await interaction.response.defer(ephemeral=private)
    file = await render_month_file(year, mon)
    await interaction.followup.send(file=file, ephemeral=private)


def _people(entries, limit=1000) -> str:
    """'Sam, Bea (from 19:00)' lines, trimmed to fit an embed field."""
    text = '\n'.join(entries)
    return text if len(text) <= limit else text[:limit].rsplit('\n', 1)[0] + '\n…'


@bot.tree.command(name='whosfree', description='Who can play on a date, and when')
@app_commands.rename(day='date')
@app_commands.describe(day="e.g. '12 Dec' or 'tomorrow'")
@app_commands.guild_only()
async def whosfree(interaction: discord.Interaction, day: str):
    days = parse_dates(day, today(), MAX_DAYS)
    if len(days) != 1:
        await send_error(interaction, 'Give me one date, e.g. `12 Dec`.')
        return
    d = days[0]
    gm_rows = (await asyncio.to_thread(store.gm_days, d, d)).get(d, [])
    people = (await asyncio.to_thread(store.player_days, d, d)).get(d, {})
    gm_state = gm_calendar_days({d: gm_rows}).get(d)

    free, busy = {}, {}   # name -> windows
    for name, rows in people.values():
        name = name if name not in free and name not in busy else f'{name} (2)'
        f, b = player_day_summary(rows)
        if f:
            free[name] = f
        elif b:
            busy[name] = b

    embed = discord.Embed(title=f'{d:%A} {d.day} {d:%B %Y}', color=discord.Color.dark_gold())
    if gm_state:
        state, windows, note = gm_state
        gm_line = ('🟢 Free' if state == 'open' else '🔴 Away') + at(windows) + (f': {note}' if note else '')
    else:
        gm_line = 'Not set'
    embed.add_field(name='GM', value=gm_line, inline=False)

    if gm_state and gm_state[0] == 'open' and free:
        slots = best_slots(gm_state[1], free)
        if slots:
            slots.sort(key=lambda sl: (-len(sl[2]), sl[0]))
            lines = [f"**{format_windows([(a, b)])}**: {len(names)} · {', '.join(names)}" for a, b, names in slots[:5]]
            embed.add_field(name='When you can all play', value=_people(lines), inline=False)
        else:
            embed.add_field(name='When you can all play', value='No overlap between the GM and the players yet.', inline=False)

    embed.add_field(
        name=f'Free ({len(free)})',
        value=_people(f'{n}{at(w)}' for n, w in sorted(free.items(), key=lambda kv: kv[0].lower())) or 'Nobody yet',
        inline=False,
    )
    embed.add_field(
        name=f'Busy ({len(busy)})',
        value=_people(f'{n}{at(w)}' for n, w in sorted(busy.items(), key=lambda kv: kv[0].lower())) or 'Nobody',
        inline=False,
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name='propose', description='Propose a session. The GM approves it')
@app_commands.rename(day='date', when='time')
@app_commands.describe(
    day="One date, e.g. '12 Dec'",
    when="Start time, e.g. '7pm', or a range like '7pm-10pm'",
    goal="What you'd like to do, e.g. 'Clear the goblin cave'",
    party_size="How many players you're hoping for (a guide, not a hard limit)",
)
@app_commands.guild_only()
async def propose(interaction: discord.Interaction, day: str, when: str, goal: str,
                  party_size: Optional[int] = None):
    await sessions.propose(interaction, day, when, goal, party_size)


@bot.tree.command(name='sessions', description='Upcoming sessions and proposals')
@app_commands.describe(private='Only show it to you')
@app_commands.guild_only()
async def sessions_cmd(interaction: discord.Interaction, private: bool = False):
    upcoming = await asyncio.to_thread(store.list_sessions, SESSION_LIVE, today())
    if not upcoming:
        await interaction.response.send_message('Nothing is planned yet. Try `/propose`.', ephemeral=True)
        return
    icons = {'confirmed': '✅', 'proposed': '🗳️', 'countered': '↔️'}
    lines = []
    for s in upcoming[:20]:
        text = (f"{icons[s['status']]} **{sessions.when_text(s)}**: "
                f"{discord.utils.escape_markdown(s['goal'])} ({len(s['players'])}/{s['party_size']})")
        url = sessions.jump_url(s)
        lines.append(text + (f' [open]({url})' if url else ''))
    waiting = sum(1 for s in upcoming if s['status'] in SESSION_OPEN)
    embed = discord.Embed(title='Upcoming sessions', description='\n'.join(lines)[:4000],
                          color=discord.Color.dark_gold())
    if waiting:
        embed.set_footer(text=f'{waiting} waiting for the GM')
    await interaction.response.send_message(embed=embed, ephemeral=private)


@gm.command(name='hub_line', description='The next confirmed session, as lines to paste into the hub home note')
@is_gm()
async def gm_hub_line(interaction: discord.Interaction):
    upcoming = await asyncio.to_thread(store.list_sessions, ('confirmed',), today())
    if not upcoming:
        await interaction.response.send_message('There is no confirmed session coming up.', ephemeral=True)
        return
    nxt = upcoming[0]
    await interaction.response.send_message(
        f"Next: **{sessions.when_text(nxt)}**\n" + sessions.hub_lines(nxt), ephemeral=True)


@bot.tree.command(name='subscribe', description='Get every confirmed session in your own calendar app')
@app_commands.guild_only()
async def subscribe(interaction: discord.Interaction):
    url = GOOGLE_CALENDAR_ICAL_URL
    if not url:
        await interaction.response.send_message(
            "The GM hasn't set up the shared calendar yet. You can still add a single session with the "
            'Google, Outlook and .ics buttons on its card.', ephemeral=True)
        return
    view = discord.ui.View()
    for label, link in (('Google Calendar', google_subscribe_url(url)),
                        ('Outlook', outlook_subscribe_url(url, config['calendar_title']))):
        if len(link) <= 512:   # Discord's limit for link buttons
            view.add_item(discord.ui.Button(label=label, url=link))
    await interaction.response.send_message(
        'Subscribe to see every confirmed session in your own calendar. It updates by itself when sessions '
        'are added, moved or cancelled. Calendar apps refresh subscribed calendars every few hours (Google '
        'can take up to a day), so for anything urgent check Discord.\n\n'
        'For Apple Calendar or anything else, copy this address:\n'
        f'```{webcal_url(url)}```',
        view=view, ephemeral=True)


@gm.command(name='reminders', description='What reminders are scheduled for upcoming sessions, and what has gone out')
@is_gm()
async def gm_reminders(interaction: discord.Interaction):
    await interaction.response.send_message(await sessions.reminder_overview(), ephemeral=True)


@gm.command(name='test_reminder', description='DM yourself a sample reminder to check that DMs reach you')
@is_gm()
async def gm_test_reminder(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    await interaction.followup.send(await sessions.send_test_reminder(interaction.user), ephemeral=True)


@gm.command(name='calendar_status', description='Check the shared Google Calendar connection and sync now')
@is_gm()
async def gm_calendar_status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    if not sessions.gcal:
        await interaction.followup.send(
            '**Shared calendar: not set up.** Add `GOOGLE_CALENDAR_ID` (and `GOOGLE_CALENDAR_ICAL_URL` for '
            '`/subscribe`) to `.env` and restart the bot. The steps are in `SCHEDULER_GUIDE.md`.', ephemeral=True)
        return
    lines = []
    try:
        await asyncio.to_thread(sessions.gcal.check_access)
        lines.append('✅ I can reach the calendar.')
        result = await sessions.sync_google()
        lines.append(f'Synced just now: {result.summary()}.')
        for err in (result.errors if result else [])[:3]:
            lines.append(f'⚠️ {err}')
    except CalendarError as e:
        lines.append(f'❌ I can\'t use the calendar: {e}')
    lines.append(f'Service account to share the calendar with: `{sessions.gcal.service_account_email}`')
    lines.append('`/subscribe` for players: ' + ('✅ ready' if GOOGLE_CALENDAR_ICAL_URL
                                                  else "❌ add `GOOGLE_CALENDAR_ICAL_URL` to `.env`"))
    await interaction.followup.send('\n'.join(lines), ephemeral=True)


@bot.tree.command(name='schedule_help', description='How the session scheduler works')
async def schedule_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title='📅 Session scheduler',
        description=(
            'Dates can be typed loosely: `12 Dec`, `3-7 Dec`, `5, 12, 19 Dec`, '
            '`28 Dec - 3 Jan`, `December`, `tomorrow`, `2026-12-03`.\n'
            'Add a `time` if you are only free for part of the day: `after 7pm`, `before 5pm`, '
            '`7pm-10pm`, `19:00-22:00`, `morning`, `afternoon`, `evenings`, or several joined by '
            'commas (`12-2pm, 7-10pm`). Leave it empty for all day. Times are UK time.\n'
            'Setting a day again replaces what you set before for that day.'
        ),
        color=discord.Color.dark_gold(),
    )
    embed.add_field(
        name='Everyone',
        value=(
            '`/available <dates> [time]`: say when you can play\n'
            "`/unavailable <dates> [time]`: say when you can't\n"
            '`/clear_availability <dates> [time]`: remove your availability\n'
            '`/my_availability`: your availability for the next 90 days\n'
            '`/calendar [month]`: month view\n'
            '`/whosfree <date>`: who can play on a date, and when\n'
            '`/propose <date> <time> <goal> [party_size]`: suggest a session; the GM approves it\n'
            '`/sessions`: what is planned\n'
            '`/subscribe`: get every confirmed session in your own calendar app'
        ),
        inline=False,
    )
    embed.add_field(
        name='GM',
        value=(
            '`/gm open <dates> [time] [note]`: when you can run\n'
            "`/gm block <dates> [time] [note]`: when you can't\n"
            '`/gm clear <dates> [time]`: remove your availability\n'
            '`/gm pin_calendar`: post a self-updating calendar in this channel\n'
            '`/gm hub_line`: next session as lines to paste into the hub\n'
            '`/gm calendar_status`: check the shared Google Calendar and sync now\n'
            '`/gm reminders`: what reminders are scheduled and what has gone out\n'
            '`/gm test_reminder`: DM yourself a sample reminder'
        ),
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


if __name__ == '__main__':
    if not TOKEN:
        print('Error: SCHEDULER_BOT_TOKEN not found in environment variables!')
        print('Please add SCHEDULER_BOT_TOKEN to your .env file.')
        raise SystemExit(1)
    bot.run(TOKEN)
