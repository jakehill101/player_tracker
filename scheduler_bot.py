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

from calendar_render import month_bounds, next_month, render_month
from schedule_dates import DateParseError, format_days, parse_dates, parse_month
from schedule_store import ScheduleStore, prune_backups

load_dotenv()

TOKEN = os.getenv('SCHEDULER_BOT_TOKEN')
GM_ROLE_ID = os.getenv('GM_ROLE_ID')

DEFAULT_CONFIG = {
    'timezone': 'Europe/London',
    'database': 'data/schedule.db',
    'backup_dir': 'backups',
    'backup_keep': 14,
    'calendar_title': 'Tales from Fellgard · sessions',
    'max_days_per_command': 120,
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
_refresh_lock = asyncio.Lock()

PIN_CHANNEL_KEY = 'pinned_calendar_channel'
PIN_MESSAGE_KEY = 'pinned_calendar_message'


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


def past_note(dropped: int) -> str:
    if not dropped:
        return ''
    return f"\n-# Skipped {dropped} date{'s' if dropped != 1 else ''} already in the past."


async def render_month_file(year: int, month: int, filename: str = None) -> discord.File:
    start, end = month_bounds(year, month)
    gm = await asyncio.to_thread(store.gm_days, start, end)
    players = await asyncio.to_thread(store.player_days, start, end)
    png = await asyncio.to_thread(
        render_month, year, month, gm, players, today(), config['calendar_title']
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
        '**Session calendar**: this month and next. Green is when the GM is free.\n'
        'Mark your own dates with `/available` and `/unavailable`. '
        f'`/calendar` shows any month.\n-# Updated <t:{stamp}:R>'
    )


def schedule_refresh():
    """Fire-and-forget refresh after a change."""
    asyncio.create_task(refresh_pinned_calendar())


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
    if store is None:
        store = ScheduleStore(config['database'])
        print(f"Schedule database: {config['database']} (schema v{store.schema_version})")
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
    if isinstance(original, DateParseError):
        await send_error(interaction, str(original))
        return
    print(f'Command error in /{interaction.command.qualified_name if interaction.command else "?"}: {error!r}')
    await send_error(interaction, 'Something went wrong. The GM can check the bot logs.')


# ------------------------------------------------------------ GM commands

gm = app_commands.Group(name='gm', description='GM scheduling commands', guild_only=True)


@gm.command(name='open', description='Mark dates when you can run a session')
@app_commands.describe(
    dates="e.g. '3-7 Dec', '5, 12, 19 Dec', 'December'",
    note="Optional, e.g. 'evenings only' (shown on the calendar)",
)
@is_gm()
async def gm_open(interaction: discord.Interaction, dates: str, note: Optional[str] = ''):
    days, dropped = parse_future(dates)
    if not days:
        await send_error(interaction, 'All of those dates are in the past.')
        return
    await asyncio.to_thread(store.set_gm_days, days, 'open', note or '')
    await interaction.response.send_message(
        f'Marked you **free** on {format_days(days)}.' + (f' Note: {note}' if note else '') + past_note(dropped),
        ephemeral=True,
    )
    schedule_refresh()


@gm.command(name='block', description="Mark dates when you can't run a session")
@app_commands.describe(dates="e.g. '20 Dec - 3 Jan', 'January'", note='Optional note shown on the calendar')
@is_gm()
async def gm_block(interaction: discord.Interaction, dates: str, note: Optional[str] = ''):
    days, dropped = parse_future(dates)
    if not days:
        await send_error(interaction, 'All of those dates are in the past.')
        return
    await asyncio.to_thread(store.set_gm_days, days, 'blocked', note or '')
    await interaction.response.send_message(
        f'Marked you **away** on {format_days(days)}.' + past_note(dropped), ephemeral=True
    )
    schedule_refresh()


@gm.command(name='clear', description='Remove your open/blocked marks from dates')
@app_commands.describe(dates="e.g. '3-7 Dec' or 'December'")
@is_gm()
async def gm_clear(interaction: discord.Interaction, dates: str):
    days, _ = parse_future(dates)
    removed = await asyncio.to_thread(store.clear_gm_days, days)
    await interaction.response.send_message(
        f'Cleared {removed} date{"s" if removed != 1 else ""} ({format_days(days)}).', ephemeral=True
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

@bot.tree.command(name='available', description="Mark dates you're free to play")
@app_commands.describe(dates="e.g. '3-7 Dec', '5, 12, 19 Dec', 'December'")
@app_commands.guild_only()
async def available(interaction: discord.Interaction, dates: str):
    days, dropped = parse_future(dates)
    if not days:
        await send_error(interaction, 'All of those dates are in the past.')
        return
    await asyncio.to_thread(store.set_player_days, interaction.user.id, interaction.user.display_name, days, True)
    await interaction.response.send_message(
        f'Marked you **free** on {format_days(days)}.' + past_note(dropped), ephemeral=True
    )
    schedule_refresh()


@bot.tree.command(name='unavailable', description="Mark dates you can't play")
@app_commands.describe(dates="e.g. '20 Dec - 3 Jan'")
@app_commands.guild_only()
async def unavailable(interaction: discord.Interaction, dates: str):
    days, dropped = parse_future(dates)
    if not days:
        await send_error(interaction, 'All of those dates are in the past.')
        return
    await asyncio.to_thread(store.set_player_days, interaction.user.id, interaction.user.display_name, days, False)
    await interaction.response.send_message(
        f'Marked you **busy** on {format_days(days)}.' + past_note(dropped), ephemeral=True
    )
    schedule_refresh()


@bot.tree.command(name='clear_availability', description='Remove your free/busy marks from dates')
@app_commands.describe(dates="e.g. '3-7 Dec' or 'December'")
@app_commands.guild_only()
async def clear_availability(interaction: discord.Interaction, dates: str):
    days, _ = parse_future(dates)
    removed = await asyncio.to_thread(store.clear_player_days, interaction.user.id, days)
    await interaction.response.send_message(
        f'Cleared {removed} date{"s" if removed != 1 else ""}.', ephemeral=True
    )
    schedule_refresh()


@bot.tree.command(name='my_availability', description="See the dates you've marked over the next 90 days")
@app_commands.guild_only()
async def my_availability(interaction: discord.Interaction):
    t = today()
    own = await asyncio.to_thread(store.player_own_days, interaction.user.id, t, t + timedelta(days=90))
    free = [d for d, f in own.items() if f]
    busy = [d for d, f in own.items() if not f]
    if not own:
        msg = "You haven't marked any dates yet. Try `/available 3-7 Dec`."
    else:
        msg = f'**Free:** {format_days(free) if free else "none"}\n**Busy:** {format_days(busy) if busy else "none"}'
    await interaction.response.send_message(msg, ephemeral=True)


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


@bot.tree.command(name='whosfree', description='Who has marked themselves free or busy on a date')
@app_commands.rename(day='date')
@app_commands.describe(day="e.g. '12 Dec' or 'tomorrow'")
@app_commands.guild_only()
async def whosfree(interaction: discord.Interaction, day: str):
    days = parse_dates(day, today(), MAX_DAYS)
    if len(days) != 1:
        await send_error(interaction, 'Give me one date, e.g. `12 Dec`.')
        return
    d = days[0]
    gm_days = await asyncio.to_thread(store.gm_days, d, d)
    players = (await asyncio.to_thread(store.player_days, d, d)).get(d, {'free': [], 'busy': []})

    embed = discord.Embed(title=f'{d:%A} {d.day} {d:%B %Y}', color=discord.Color.dark_gold())
    if d in gm_days:
        state, note = gm_days[d]
        gm_line = ('🟢 Free' if state == 'open' else '🔴 Away') + (f': {note}' if note else '')
    else:
        gm_line = 'Not marked'
    embed.add_field(name='GM', value=gm_line, inline=False)
    embed.add_field(name=f"Free ({len(players['free'])})", value=', '.join(players['free']) or 'Nobody yet', inline=False)
    embed.add_field(name=f"Busy ({len(players['busy'])})", value=', '.join(players['busy']) or 'Nobody', inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name='schedule_help', description='How the session scheduler works')
async def schedule_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title='📅 Session scheduler',
        description=(
            'Dates can be typed loosely: `12 Dec`, `3-7 Dec`, `5, 12, 19 Dec`, '
            '`28 Dec - 3 Jan`, `December`, `tomorrow`, `2026-12-03`.'
        ),
        color=discord.Color.dark_gold(),
    )
    embed.add_field(
        name='Everyone',
        value=(
            '`/available <dates>`: mark when you can play\n'
            "`/unavailable <dates>`: mark when you can't\n"
            '`/clear_availability <dates>`: remove your marks\n'
            '`/my_availability`: your marks for the next 90 days\n'
            '`/calendar [month]`: month view\n'
            '`/whosfree <date>`: who can make a date'
        ),
        inline=False,
    )
    embed.add_field(
        name='GM',
        value=(
            '`/gm open <dates> [note]`: dates you can run\n'
            "`/gm block <dates> [note]`: dates you can't\n"
            '`/gm clear <dates>`: remove marks\n'
            '`/gm pin_calendar`: post a self-updating calendar in this channel'
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
