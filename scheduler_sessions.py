"""
Discord side of session proposals: the card, its buttons, and what each button does.

A proposal is posted as an embed with Join / Leave buttons for everyone and
Approve / Decline / Suggest buttons that only the GM can use. The state lives
in the SQLite store; the card is redrawn from it after every change, so it is
always correct even after a restart (buttons are persistent).
"""
import asyncio
import io
from datetime import date, datetime, time
from typing import Callable, Iterable, List, Optional

import discord

import calendar_links as links
from schedule_dates import parse_dates
from schedule_store import ProposalLimitError, SESSION_LIVE, SESSION_OPEN, ScheduleStore
from schedule_times import (
    describe_session_time, format_windows, gm_day_summary, hhmm, overlaps,
    parse_session_time, player_day_summary, subtract,
)

STATUS_LOOK = {
    'proposed': (discord.Color.gold(), '🗳️ Proposed: waiting for the GM'),
    'countered': (discord.Color.orange(), '↔️ The GM suggested another time'),
    'confirmed': (discord.Color.green(), '✅ Confirmed'),
    'declined': (discord.Color.dark_grey(), '❌ Declined'),
    'cancelled': (discord.Color.dark_grey(), '🚫 Cancelled'),
    'expired': (discord.Color.dark_grey(), '⌛ Expired: nobody confirmed it in time'),
    'done': (discord.Color.dark_grey(), '🏁 Finished'),
}


class SessionManager:
    def __init__(self, bot, store: ScheduleStore, config: dict, tz, is_gm: Callable, gm_role_id: Optional[str],
                 refresh_calendar: Callable[[], None]):
        self.bot, self.store, self.config, self.tz = bot, store, config, tz
        self.is_gm = is_gm
        self.gm_role_id = int(gm_role_id) if gm_role_id and str(gm_role_id).isdigit() else None
        self.refresh_calendar = refresh_calendar

    # ----------------------------------------------------------------- time

    @property
    def tz_label(self) -> str:
        key = getattr(self.tz, 'key', str(self.tz))
        return 'UK time' if key == 'Europe/London' else key

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def start_dt(self, s: dict) -> datetime:
        return datetime.combine(s['day'], time(s['start_min'] // 60, s['start_min'] % 60), tzinfo=self.tz)

    def end_dt(self, s: dict) -> datetime:
        return links.to_utc(s['day'], s['end_min'], self.tz).astimezone(self.tz)

    def when_text(self, s: dict) -> str:
        return describe_session_time(s['day'], s['start_min'], s['end_min'])

    def check_future(self, day: date, start_min: int) -> Optional[str]:
        start = datetime.combine(day, time(start_min // 60, start_min % 60), tzinfo=self.tz)
        if start <= self.now():
            return "That time has already passed."
        if (day - self.now().date()).days > int(self.config['max_days_ahead']):
            return f"That's more than {self.config['max_days_ahead']} days away."
        return None

    def parse_when(self, date_text: str, time_text: str):
        """-> (day, start_min, end_min) or raises DateParseError / TimeParseError / ValueError."""
        days = parse_dates(date_text, self.now().date())
        if len(days) != 1:
            raise ValueError('Give me one date, e.g. `12 Dec`.')
        start, end = parse_session_time(time_text, int(float(self.config['default_session_hours']) * 60))
        problem = self.check_future(days[0], start)
        if problem:
            raise ValueError(problem)
        return days[0], start, end

    # ----------------------------------------------------------------- card

    def jump_url(self, s: dict) -> Optional[str]:
        if s['guild_id'] and s['channel_id'] and s['message_id']:
            return f"https://discord.com/channels/{s['guild_id']}/{s['channel_id']}/{s['message_id']}"
        return None

    def title(self, s: dict) -> str:
        return discord.utils.escape_mentions(s['goal'])

    async def _availability_notes(self, s: dict) -> List[str]:
        """What the GM's and players' availability says about this time. Only for live sessions."""
        window = (s['start_min'], s['end_min'])
        day = s['day']
        gm_rows = (await asyncio.to_thread(self.store.gm_days, day, day)).get(day, [])
        people = (await asyncio.to_thread(self.store.player_days, day, day)).get(day, {})
        notes = []
        if s['status'] in SESSION_OPEN:
            summary = gm_day_summary(gm_rows)
            if not summary:
                notes.append("GM hasn't set availability for this day.")
            else:
                state, windows, _ = summary
                if state == 'open':
                    notes.append('🟢 The GM is free then.' if not subtract([window], windows)
                                 else f'🟡 The GM is only free {format_windows(windows)}.')
                else:
                    notes.append(f'🔴 The GM is away {format_windows(windows)}.' if overlaps(windows, window)
                                 else "🟡 The GM hasn't marked this time as free.")
        busy = []
        for uid, name in s['players']:
            _, busy_windows = player_day_summary(people.get(uid, ('', []))[1])
            if overlaps(busy_windows, window):
                busy.append(f'<@{uid}>')
        if busy:
            notes.append('⚠️ Marked busy then: ' + ', '.join(busy))
        return notes

    async def build_embed(self, s: dict) -> discord.Embed:
        colour, label = STATUS_LOOK[s['status']]
        embed = discord.Embed(title=self.title(s), colour=colour)
        embed.set_author(name=label)
        start_ts = int(self.start_dt(s).timestamp())
        embed.add_field(
            name='When',
            value=f"{self.when_text(s)} ({self.tz_label})\n<t:{start_ts}:F> · <t:{start_ts}:R>",
            inline=False,
        )
        n = len(s['players'])
        roster = ', '.join(f'<@{uid}>' for uid, _ in s['players']) or 'Nobody yet'
        embed.add_field(name=f"Party ({n}/{s['party_size']})", value=roster[:1000], inline=False)

        if s['status'] == 'countered' and s['sug_day']:
            sug = describe_session_time(s['sug_day'], s['sug_start_min'], s['sug_end_min'])
            text = f"**{sug}**" + (f"\n{discord.utils.escape_mentions(s['sug_note'])}" if s['sug_note'] else '')
            embed.add_field(name='GM suggests', value=f"{text}\n<@{s['proposer_id']}> can accept it below.", inline=False)

        if s['status'] in SESSION_LIVE:
            notes = await self._availability_notes(s)
            if notes:
                embed.add_field(name='Availability', value='\n'.join(notes), inline=False)

        embed.set_footer(text=f"Session #{s['id']} · proposed by {s['proposer_name']}")
        return embed

    def event_title(self, s: dict) -> str:
        return f"{self.config['event_name_prefix']}: {s['goal']}"[:100]

    def event_details(self, s: dict) -> str:
        names = ', '.join(name for _, name in s['players'])
        text = f"{s['goal']}\nParty: {names} ({len(s['players'])}/{s['party_size']})"
        url = self.jump_url(s)
        return text + (f'\n{url}' if url else '')

    def build_view(self, s: dict) -> Optional[discord.ui.View]:
        status = s['status']
        if status not in SESSION_LIVE:
            return None
        view = discord.ui.View(timeout=None)
        sid = s['id']

        def button(label, action, style=discord.ButtonStyle.secondary, row=0):
            b = discord.ui.Button(label=label, style=style, custom_id=f'sched:{sid}:{action}', row=row)

            async def callback(interaction: discord.Interaction, _action=action):
                await self.handle(interaction, sid, _action)
            b.callback = callback
            view.add_item(b)

        button('Join', 'join', discord.ButtonStyle.primary)
        button('Leave', 'leave')
        if status == 'confirmed':
            start, end = self.start_dt(s), self.end_dt(s)
            title, details, where = self.event_title(s), self.event_details(s), self.config['event_location']
            view.add_item(discord.ui.Button(label='Google', url=links.google_url(title, start, end, details, where), row=0))
            view.add_item(discord.ui.Button(label='Outlook', url=links.outlook_url(title, start, end, details, where), row=0))
            button('.ics', 'ics', row=0)
            button('Change time', 'suggest', row=1)
            button('Cancel session', 'cancel', discord.ButtonStyle.danger, row=1)
        elif status == 'proposed':
            button('Approve', 'approve', discord.ButtonStyle.success, row=1)
            button('Decline', 'decline', discord.ButtonStyle.danger, row=1)
            button('Suggest another time', 'suggest', row=1)
            button('Withdraw', 'withdraw', row=1)
        elif status == 'countered':
            button('Accept suggestion', 'accept', discord.ButtonStyle.success, row=1)
            button('Suggest another time', 'suggest', row=1)
            button('Decline', 'decline', discord.ButtonStyle.danger, row=1)
            button('Withdraw', 'withdraw', row=1)
        return view

    async def render(self, s: dict):
        return await self.build_embed(s), self.build_view(s)

    async def refresh_card(self, sid: int):
        """Redraw a session's card in place."""
        s = await asyncio.to_thread(self.store.get_session, sid)
        if not s or not s['channel_id'] or not s['message_id']:
            return
        embed, view = await self.render(s)
        try:
            channel = self.bot.get_partial_messageable(int(s['channel_id']))
            await channel.get_partial_message(int(s['message_id'])).edit(embed=embed, view=view)
        except discord.NotFound:
            print(f"Card for session #{sid} was deleted.")
        except discord.HTTPException as e:
            print(f"Could not update card for session #{sid}: {e}")

    async def register_views(self):
        """Make buttons on existing cards work again after a restart."""
        count = 0
        for s in await asyncio.to_thread(self.store.list_sessions, SESSION_LIVE):
            if s['message_id']:
                view = self.build_view(s)
                if view:
                    self.bot.add_view(view, message_id=int(s['message_id']))
                    count += 1
        print(f'Re-attached buttons to {count} session card(s)')

    # --------------------------------------------------------- notifications

    async def notify(self, s: dict, text: str, user_ids: Iterable[str] = (), ping_gm: bool = False):
        """Post a short note in the session's channel, pinging only the listed people."""
        if not s['channel_id']:
            return
        users = [discord.Object(id=int(u)) for u in dict.fromkeys(user_ids)]
        roles = [discord.Object(id=self.gm_role_id)] if ping_gm and self.gm_role_id else []
        mentions = ' '.join([f'<@&{self.gm_role_id}>'] * bool(roles) + [f'<@{u.id}>' for u in users])
        url = self.jump_url(s)
        content = f"{text}{' ' + url if url else ''}\n{mentions}".strip()
        try:
            await self.bot.get_partial_messageable(int(s['channel_id'])).send(
                content, allowed_mentions=discord.AllowedMentions(everyone=False, users=users, roles=roles,
                                                                  replied_user=False))
        except discord.HTTPException as e:
            print(f'Could not post a notice for session #{s["id"]}: {e}')

    def people(self, s: dict) -> List[str]:
        return [uid for uid, _ in s['players']] or [s['proposer_id']]

    # ----------------------------------------------------- Discord events

    def _guild(self, s: dict):
        return self.bot.get_guild(int(s['guild_id'])) if s['guild_id'] else None

    async def create_event(self, s: dict) -> Optional[str]:
        """Create the server's scheduled event. Returns a problem description, or None."""
        guild = self._guild(s)
        if not guild:
            return "I couldn't find the server to create an event in."
        try:
            event = await guild.create_scheduled_event(
                name=self.event_title(s), description=self.event_details(s)[:1000],
                start_time=self.start_dt(s), end_time=self.end_dt(s),
                entity_type=discord.EntityType.external, location=self.config['event_location'][:100],
                privacy_level=discord.PrivacyLevel.guild_only, reason=f"Session #{s['id']} confirmed")
        except discord.Forbidden:
            return "I couldn't create the Discord event: I need the **Manage Events** permission."
        except discord.HTTPException as e:
            return f"I couldn't create the Discord event ({e.status})."
        await asyncio.to_thread(self.store.transition, s['id'], ('confirmed',), 'confirmed', event_id=str(event.id))
        return None

    async def _event(self, s: dict):
        guild = self._guild(s)
        if not guild or not s['event_id']:
            return None
        try:
            return await guild.fetch_scheduled_event(int(s['event_id']))
        except discord.HTTPException:
            return None

    async def update_event(self, s: dict):
        event = await self._event(s)
        if event:
            try:
                await event.edit(name=self.event_title(s), description=self.event_details(s)[:1000],
                                 start_time=self.start_dt(s), end_time=self.end_dt(s))
            except discord.HTTPException as e:
                print(f"Could not update the event for session #{s['id']}: {e}")

    async def delete_event(self, s: dict):
        event = await self._event(s)
        if event:
            try:
                await event.delete()
            except discord.HTTPException as e:
                print(f"Could not delete the event for session #{s['id']}: {e}")

    # -------------------------------------------------------------- propose

    async def propose(self, interaction: discord.Interaction, date_text: str, time_text: str, goal: str,
                      party_size: Optional[int]):
        goal = ' '.join((goal or '').split())
        if not goal:
            return await self._say(interaction, 'Say what the session is for, e.g. "clear the goblin cave".')
        if len(goal) > 100:
            return await self._say(interaction, 'Keep the goal to 100 characters or fewer.')
        size = party_size if party_size is not None else int(self.config['default_party_size'])
        if not 1 <= size <= int(self.config['max_party_size']):
            return await self._say(interaction, f"Party size should be between 1 and {self.config['max_party_size']}.")
        try:
            day, start, end = self.parse_when(date_text, time_text)
        except ValueError as e:   # DateParseError and TimeParseError are ValueErrors
            return await self._say(interaction, str(e))
        try:
            sid = await asyncio.to_thread(
                self.store.create_session, interaction.user.id, interaction.user.display_name, day, start, end,
                goal, size, interaction.guild_id or '', int(self.config['max_open_proposals']))
        except ProposalLimitError as e:
            return await self._say(
                interaction, f"You already have {e.limit} proposals waiting. Withdraw one or wait for the GM to answer.")

        s = await asyncio.to_thread(self.store.get_session, sid)
        embed, view = await self.render(s)
        roles = [discord.Object(id=self.gm_role_id)] if self.gm_role_id else []
        content = f'<@&{self.gm_role_id}> new session proposal' if self.gm_role_id else None
        try:
            await interaction.response.send_message(
                content=content, embed=embed, view=view,
                allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=roles))
            message = await interaction.original_response()
        except discord.HTTPException as e:
            await asyncio.to_thread(self.store.transition, sid, SESSION_OPEN, 'cancelled')
            print(f'Could not post proposal #{sid}: {e}')
            return
        await asyncio.to_thread(self.store.set_message, sid, message.channel.id, message.id)
        self.refresh_calendar()

    async def _say(self, interaction: discord.Interaction, text: str):
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    # -------------------------------------------------------------- buttons

    async def handle(self, interaction: discord.Interaction, sid: int, action: str):
        s = await asyncio.to_thread(self.store.get_session, sid)
        if not s:
            return await self._say(interaction, 'That session no longer exists.')
        user = interaction.user
        gm = self.is_gm(user)
        is_proposer = str(user.id) == s['proposer_id']

        if action in ('approve', 'decline', 'suggest') and not gm:
            return await self._say(interaction, 'Only the GM can do that.')
        if action == 'accept' and not is_proposer:
            return await self._say(interaction, 'Only the person who proposed it can accept the suggestion.')
        if action in ('withdraw', 'cancel') and not (gm or is_proposer):
            return await self._say(interaction, 'Only the GM or the person who proposed it can do that.')

        if s['status'] not in SESSION_LIVE:
            return await self._say(interaction, f"That session is {s['status']}, so there's nothing more to do.")

        handler = getattr(self, f'_do_{action}', None)
        if handler is None:
            return await self._say(interaction, "I don't know that button any more.")
        await handler(interaction, s)

    async def _do_join(self, interaction, s):
        if self.start_dt(s) <= self.now():
            return await self._say(interaction, 'That session has already started.')
        result = await asyncio.to_thread(self.store.join_session, s['id'], interaction.user.id,
                                         interaction.user.display_name)
        if result == 'closed':
            return await self._say(interaction, "That session isn't open any more.")
        if result == 'already':
            return await self._say(interaction, "You're already in this session.")
        await self._update_in_place(interaction, s['id'])

    async def _do_leave(self, interaction, s):
        if self.start_dt(s) <= self.now():
            return await self._say(interaction, 'That session has already started.')
        result = await asyncio.to_thread(self.store.leave_session, s['id'], interaction.user.id)
        if result == 'not_in':
            return await self._say(interaction, "You weren't in this session.")
        if result == 'closed':
            return await self._say(interaction, "That session isn't open any more.")
        await self._update_in_place(interaction, s['id'])

    async def _update_in_place(self, interaction, sid):
        """Answer a button click by redrawing the card the button is on."""
        s = await asyncio.to_thread(self.store.get_session, sid)
        embed, view = await self.render(s)
        await interaction.response.edit_message(embed=embed, view=view)
        if s['status'] == 'confirmed':
            await self.update_event(s)
        self.refresh_calendar()

    async def _do_approve(self, interaction, s):
        if self.start_dt(s) <= self.now():
            return await self._say(interaction, 'That time has already passed.')
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await asyncio.to_thread(self.store.transition, s['id'], ('proposed',), 'confirmed'):
            return await interaction.followup.send('Someone has already dealt with this one.', ephemeral=True)
        await self._confirmed(interaction, s['id'], 'Session confirmed')

    async def _do_accept(self, interaction, s):
        if self.check_future(s['sug_day'], s['sug_start_min']):
            return await self._say(interaction, 'The suggested time has already passed.')
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await asyncio.to_thread(self.store.accept_suggestion, s['id']):
            return await interaction.followup.send('That suggestion is no longer available.', ephemeral=True)
        await self._confirmed(interaction, s['id'], 'Suggestion accepted: session confirmed', ping_gm=True)

    async def _confirmed(self, interaction, sid, headline, ping_gm=False):
        s = await asyncio.to_thread(self.store.get_session, sid)
        problem = await self.create_event(s)
        await self.refresh_card(sid)
        s = await asyncio.to_thread(self.store.get_session, sid)
        await self.notify(s, f"✅ **{headline}** for {self.when_text(s)}.", self.people(s), ping_gm=ping_gm)
        self.refresh_calendar()
        await interaction.followup.send(
            ('⚠️ ' + problem + '\n\n' if problem else '') + self.hub_lines(s, intro=True), ephemeral=True)

    async def _do_decline(self, interaction, s):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await asyncio.to_thread(self.store.transition, s['id'], SESSION_OPEN, 'declined'):
            return await interaction.followup.send('Someone has already dealt with this one.', ephemeral=True)
        await self.refresh_card(s['id'])
        s = await asyncio.to_thread(self.store.get_session, s['id'])
        await self.notify(s, f"❌ **Declined**: {self.when_text(s)}.", [s['proposer_id']])
        self.refresh_calendar()
        await interaction.followup.send('Declined.', ephemeral=True)

    async def _do_withdraw(self, interaction, s):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await asyncio.to_thread(self.store.transition, s['id'], SESSION_OPEN, 'cancelled'):
            return await interaction.followup.send('Someone has already dealt with this one.', ephemeral=True)
        await self.refresh_card(s['id'])
        self.refresh_calendar()
        await interaction.followup.send('Withdrawn.', ephemeral=True)

    async def _do_cancel(self, interaction, s):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await asyncio.to_thread(self.store.transition, s['id'], ('confirmed',), 'cancelled'):
            return await interaction.followup.send('Someone has already dealt with this one.', ephemeral=True)
        await self.delete_event(s)
        await self.refresh_card(s['id'])
        s = await asyncio.to_thread(self.store.get_session, s['id'])
        await self.notify(s, f"🚫 **Cancelled**: {self.when_text(s)}.", self.people(s))
        self.refresh_calendar()
        await interaction.followup.send('Cancelled. The Discord event has been removed.', ephemeral=True)

    async def _do_ics(self, interaction, s):
        if s['status'] != 'confirmed':
            return await self._say(interaction, 'Only confirmed sessions have a calendar file.')
        ics = self.ics_for(s)
        name = f"session-{s['day']:%Y-%m-%d}.ics"
        await interaction.response.send_message(
            'Open this file to add the session to your calendar (Google, Outlook, Apple and others all accept it).',
            file=discord.File(io.BytesIO(ics), filename=name), ephemeral=True)

    async def _do_suggest(self, interaction, s):
        await interaction.response.send_modal(SuggestModal(self, s))

    def ics_for(self, s: dict) -> bytes:
        event = links.ics_event(
            f"session-{s['id']}@player-tracker", self.event_title(s), self.start_dt(s), self.end_dt(s),
            description=self.event_details(s), location=self.config['event_location'],
            url=self.jump_url(s) or '')
        return links.ics_calendar([event], name=self.config['calendar_title'])

    def hub_lines(self, s: dict, intro: bool = False) -> str:
        """The three hub frontmatter lines for a confirmed session, ready to paste."""
        lines = (f"next-session: {s['day']:%Y-%m-%d} {hhmm(s['start_min'])}\n"
                 f"seats: {s['party_size']}\nsigned-up: {len(s['players'])}")
        head = ("Confirmed. The hub's next-session card is still updated by hand. Paste this into the home note:\n"
                if intro else '')
        return f'{head}```yaml\n{lines}\n```'

    # --------------------------------------------------------- suggestions

    async def apply_suggestion(self, interaction: discord.Interaction, sid: int, date_text: str,
                               time_text: str, note: str):
        """Modal submit: the GM proposes a different time (or moves a confirmed session)."""
        s = await asyncio.to_thread(self.store.get_session, sid)
        if not s or s['status'] not in SESSION_LIVE:
            return await self._say(interaction, "That session isn't open any more.")
        if not self.is_gm(interaction.user):
            return await self._say(interaction, 'Only the GM can do that.')
        try:
            day, start, end = self.parse_when(date_text, time_text)
        except ValueError as e:
            return await self._say(interaction, str(e))
        await interaction.response.defer(ephemeral=True, thinking=True)
        if s['status'] == 'confirmed':
            if not await asyncio.to_thread(self.store.move_session, sid, day, start, end):
                return await interaction.followup.send("That session isn't confirmed any more.", ephemeral=True)
            s = await asyncio.to_thread(self.store.get_session, sid)
            await self.update_event(s)
            await self.refresh_card(sid)
            await self.notify(s, f"🕒 **Time changed** to {self.when_text(s)}." +
                              (f" {discord.utils.escape_mentions(note)}" if note else ''), self.people(s))
        else:
            if not await asyncio.to_thread(self.store.suggest_time, sid, day, start, end, note):
                return await interaction.followup.send('Someone has already dealt with this one.', ephemeral=True)
            await self.refresh_card(sid)
            s = await asyncio.to_thread(self.store.get_session, sid)
            await self.notify(s, f"↔️ The GM suggests **{describe_session_time(day, start, end)}** instead.",
                              [s['proposer_id']])
        self.refresh_calendar()
        await interaction.followup.send('Done.', ephemeral=True)

    # ---------------------------------------------------------- housekeeping

    async def housekeeping(self):
        """Expire stale proposals and finish past sessions; redraw their cards."""
        now = self.now()
        changed = await asyncio.to_thread(self.store.close_finished, now.date(), now.hour * 60 + now.minute)
        for sid, _ in changed:
            await self.refresh_card(sid)
        if changed:
            self.refresh_calendar()

    # ------------------------------------------------------------- calendar

    async def calendar_sessions(self, start: date, end: date) -> dict:
        """Confirmed and finished sessions in a date range, shaped for the calendar image."""
        out: dict = {}
        for s in await asyncio.to_thread(self.store.list_sessions, ('confirmed', 'done'), start, end):
            title = f"{s['goal']} ({len(s['players'])}/{s['party_size']})"
            out.setdefault(s['day'], []).append((format_windows([(s['start_min'], s['end_min'])]), title))
        return out


class SuggestModal(discord.ui.Modal):
    def __init__(self, manager: SessionManager, s: dict):
        super().__init__(title='Change time' if s['status'] == 'confirmed' else 'Suggest another time')
        self.manager, self.sid = manager, s['id']
        self.day = discord.ui.TextInput(label='Date', placeholder='e.g. 12 Dec', max_length=40, default=f"{s['day']:%d %b %Y}")
        self.when = discord.ui.TextInput(label='Time', placeholder='e.g. 7pm or 7pm-10pm', max_length=30,
                                         default=f"{hhmm(s['start_min'])}-{hhmm(s['end_min'])}")
        self.note = discord.ui.TextInput(label='Note (optional)', required=False, max_length=150,
                                         placeholder='Shown to the party')
        for item in (self.day, self.when, self.note):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        await self.manager.apply_suggestion(interaction, self.sid, self.day.value, self.when.value, self.note.value)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f'Suggest modal error: {error!r}')
        await self.manager._say(interaction, 'Something went wrong. The GM can check the bot logs.')
