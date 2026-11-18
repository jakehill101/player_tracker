"""
End-to-end test of session proposals: propose, join, approve, suggest, accept, cancel,
restart and housekeeping, driven through the real SessionManager and SQLite store with
stand-ins for Discord's objects. Time is pinned, so the test never goes stale.

Run with: python test_scheduler_sessions.py
"""
import asyncio, os, sys, tempfile
sys.path.insert(0, os.getcwd())
from datetime import date, datetime
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo
import discord
from schedule_store import ScheduleStore
from scheduler_sessions import SessionManager
import yaml

with open('scheduler_config.yaml') as f:
    CFG = yaml.safe_load(f)     # also proves the shipped config has every key the code needs
TZ = ZoneInfo('Europe/London')
NOW = datetime(2026, 11, 18, 10, 0, tzinfo=TZ)   # pinned so the test doesn't depend on today's date
GM_ROLE = '777'
log = []

class FakeMsg:
    def __init__(s, ch, mid): s.ch, s.id = ch, mid
    async def edit(s, embed=None, view=None):
        if s.ch.no_access: raise discord.Forbidden(NS(status=403, reason='Missing Access'), {'message': 'Missing Access', 'code': 50001})
        s.ch.edits.append((s.id, embed, view))
class FakeChan:
    def __init__(s, cid): s.id, s.edits, s.sent, s.no_access = cid, [], [], False
    def get_partial_message(s, mid): return FakeMsg(s, mid)
    async def send(s, content, allowed_mentions=None):
        if s.no_access: raise discord.Forbidden(NS(status=403, reason='Missing Access'), {'message': 'Missing Access', 'code': 50001})
        s.sent.append((content, allowed_mentions))
class FakeEvent:
    def __init__(s, eid, kw): s.id, s.kw, s.deleted = eid, kw, False
    async def edit(s, **kw): s.kw.update(kw)
    async def delete(s): s.deleted = True
class FakeGuild:
    def __init__(s): s.events = {}; s.fail = None
    async def create_scheduled_event(s, **kw):
        if s.fail: raise s.fail
        e = FakeEvent(9000 + len(s.events), kw); s.events[e.id] = e; return e
    async def fetch_scheduled_event(s, eid): return s.events[eid]
class FakeBot:
    def __init__(s): s.chans, s.guild, s.views = {}, FakeGuild(), []
    def get_partial_messageable(s, cid): return s.chans.setdefault(cid, FakeChan(cid))
    def get_guild(s, gid): return s.guild
    def add_view(s, view, message_id=None): s.views.append((message_id, view))

class Resp:
    def __init__(s, i): s.i, s.done = i, False
    def is_done(s): return s.done
    async def send_message(s, content=None, **kw):
        s.done = True; s.i.out.append(('say', content, kw))
        s.i.posted = {'content': content, **kw}
    async def defer(s, **kw): s.done = True
    async def edit_message(s, **kw): s.done = True; s.i.out.append(('edit', None, kw))
    async def send_modal(s, modal): s.done = True; s.i.out.append(('modal', modal.title, {'modal': modal}))
class Follow:
    def __init__(s, i): s.i = i
    async def send(s, content=None, **kw): s.i.out.append(('follow', content, kw))

def member(uid, name, admin=False, role=False):
    return NS(id=uid, display_name=name, guild_permissions=NS(administrator=admin),
              roles=[NS(id=int(GM_ROLE))] if role else [])
def inter(user, channel_id=55):
    i = NS(user=user, out=[], guild_id=1, channel=NS(id=channel_id), posted=None, message=NS(id=4242))
    i.response, i.followup = Resp(i), Follow(i)
    async def original_response(): return NS(id=4242, channel=NS(id=channel_id))
    i.original_response = original_response
    return i
def last(i): return i.out[-1][1]
def is_gm(u): return u.guild_permissions.administrator or any(r.id == int(GM_ROLE) for r in u.roles)

async def click(mgr, view, user, label_or_id):
    i = inter(user)
    for child in view.children:
        if getattr(child, 'custom_id', None) and (child.label == label_or_id or child.custom_id.endswith(':' + label_or_id)):
            await child.callback(i); return i
    raise AssertionError(f'no button {label_or_id}: {[c.label for c in view.children]}')

def labels(view): return [c.label for c in view.children]
def field(embed, name): return next((f.value for f in embed.fields if f.name.startswith(name)), None)

async def main():
    tmp = tempfile.mkdtemp()
    store = ScheduleStore(os.path.join(tmp, 's.db'))
    bot = FakeBot(); refreshes = []
    mgr = SessionManager(bot, store, CFG, TZ, is_gm, GM_ROLE, lambda: refreshes.append(1))
    mgr.now = lambda: NOW
    sam, bea, gm = member(1, 'Sam'), member(2, 'Bea'), member(99, 'GM', role=True)

    # availability that the card should reflect
    store.set_gm_days([date(2026, 12, 12)], 'open', [(19*60, 1440)])
    store.set_player_days(2, 'Bea', [date(2026, 12, 12)], False)

    # --- propose
    i = inter(sam)
    await mgr.propose(i, '12 Dec', '7pm', 'Clear the cave', 4)
    assert i.posted['content'] == f'<@&{GM_ROLE}> new session proposal', i.posted
    am = i.posted['allowed_mentions']; assert am.everyone is False and am.users is False
    s = store.get_session(1); embed, view = await mgr.render(s)
    assert s['status'] == 'proposed' and (s['start_min'], s['end_min']) == (19*60, 23*60) and s['message_id'] == '4242'
    print('card title   :', embed.title, '|', embed.author.name)
    print('when         :', field(embed, 'When').replace('\n', ' / '))
    print('party        :', embed.fields[1].name, embed.fields[1].value)
    print('availability :', field(embed, 'Availability'))
    print('buttons      :', labels(view))
    assert 'Approve' in labels(view) and 'Google' not in labels(view)

    # --- Bea joins (she marked herself busy) -> warning on the card
    i = await click(mgr, view, bea, 'join')
    kind, _, kw = i.out[-1]; assert kind == 'edit'
    print('after join   :', kw['embed'].fields[1].name, '|', field(kw['embed'], 'Availability').replace('\n', ' / '))
    i = await click(mgr, view, bea, 'join'); assert 'already' in last(i)

    # --- permissions
    for who, btn in ((sam, 'approve'), (bea, 'decline'), (bea, 'suggest')):
        i = await click(mgr, view, who, btn); assert last(i) == 'Only the GM can do that.', (btn, last(i))
    i = await click(mgr, view, bea, 'withdraw'); assert 'proposed it' in last(i)

    # --- GM approves
    i = await click(mgr, view, gm, 'approve')
    s = store.get_session(1); assert s['status'] == 'confirmed' and s['event_id']
    ev = bot.guild.events[int(s['event_id'])]
    print('event        :', ev.kw['name'], '|', ev.kw['start_time'].isoformat(), '->', ev.kw['end_time'].isoformat(), '|', ev.kw['location'])
    kinds = [o[0] for o in i.out]; assert kinds == ['edit', 'follow', 'follow'], kinds    # card, GM-only note, public notice
    assert i.out[1][2]['ephemeral'] is True and not i.out[2][2].get('ephemeral')
    hub = i.out[1][1]; print('GM sees      :', hub.replace('\n', ' / '))
    assert 'next-session: 2026-12-12 19:00' in hub and 'seats: 4' in hub and 'signed-up: 2' in hub
    content, am = i.out[2][1], i.out[2][2]['allowed_mentions']
    print('announcement :', content.replace('\n', ' / '))
    assert [u.id for u in am.users] == [1, 2] and am.everyone is False and not am.roles
    card_view = i.out[0][2]['view']
    print('confirmed UI :', labels(card_view))
    urls = [c.url for c in card_view.children if getattr(c, 'url', None)]
    assert len(urls) == 2 and all(len(u) <= 512 for u in urls), urls
    # a second click on Approve (stale button) is refused
    i = await click(mgr, view, gm, 'approve')                                   # an old card, still showing Approve
    assert 'out of date' in last(i) and 'confirmed' in last(i), last(i)
    assert 'Cancel session' in labels(next(o for o in i.out if o[0] == 'edit')[2]['view']), 'stale card was not refreshed'
    assert len(bot.guild.events) == 1                                            # and no second event was made

    # --- ics button
    i = await click(mgr, card_view, bea, 'ics')
    f = i.out[-1][2]['file']; ics = f.fp.read().decode()
    assert f.filename == 'session-2026-12-12.ics' and 'DTSTART:20261212T190000Z' in ics and 'DTEND:20261212T230000Z' in ics
    assert 'SUMMARY:Fellgard: Clear the cave' in ics and 'METHOD:PUBLISH' in ics and ics.count('BEGIN:VEVENT') == 1
    print('ics          : ok,', len(ics.splitlines()), 'lines')

    # --- GM changes the time of the confirmed session via the modal
    i = await click(mgr, card_view, gm, 'suggest'); modal = i.out[-1][2]['modal']
    print('modal        :', modal.title, '| prefilled:', modal.day.default, modal.when.default)
    m = inter(gm); await mgr.apply_suggestion(m, 1, '13 Dec', '6pm-9pm', 'Bea has an exam')
    s = store.get_session(1); assert s['day'] == date(2026, 12, 13) and s['start_min'] == 18*60
    assert ev.kw['start_time'].hour == 18 and ev.kw['start_time'].day == 13
    print('time change  :', m.out[-1][1].replace('\n', ' / '))
    assert any(o[0] == 'edit' for o in m.out)                                    # card redrawn from the form submit

    # --- a second proposal: GM counters, proposer accepts
    i = inter(bea); await mgr.propose(i, '19 Dec', '2pm', 'Escort the caravan', None)
    s2 = store.get_session(2); assert s2['party_size'] == 5
    v2 = (await mgr.render(s2))[1]
    m = inter(gm); await mgr.apply_suggestion(m, 2, '20 Dec', '3pm-6pm', 'Sat is better')
    s2 = store.get_session(2); assert s2['status'] == 'countered'
    e2, v2 = await mgr.render(s2)
    print('countered UI :', labels(v2), '|', field(e2, 'GM suggests').replace('\n', ' / '))
    i = await click(mgr, v2, sam, 'accept'); assert 'proposed it' in last(i)
    i = await click(mgr, v2, bea, 'accept')
    s2 = store.get_session(2); assert s2['status'] == 'confirmed' and s2['day'] == date(2026, 12, 20) and s2['event_id']
    print('accepted     :', s2['status'], s2['day'], i.out[-1][1].split('\n')[0])
    assert i.out[-1][2]['allowed_mentions'].roles and i.out[-1][2]['allowed_mentions'].roles[0].id == int(GM_ROLE)

    # --- cancel + event removed; non-owners can't
    v2 = (await mgr.render(store.get_session(2)))[1]
    i = await click(mgr, v2, sam, 'cancel'); assert 'GM or the person' in last(i)
    i = await click(mgr, v2, gm, 'cancel')
    assert store.get_session(2)['status'] == 'cancelled' and bot.guild.events[int(s2['event_id'])].deleted
    assert (await mgr.render(store.get_session(2)))[1] is None            # no buttons left
    i = await click(mgr, v2, bea, 'join'); assert 'cancelled' in last(i)
    print('cancelled    : event deleted, buttons gone')

    # --- limits and bad input
    for n in range(4):
        i = inter(sam); await mgr.propose(i, f'{14+n} Dec', '7pm', f'goal {n}', 3); assert (n < 3) == ('already have' not in str(last(i))), (n, last(i))
    assert 'already have 3' in last(i), last(i)
    for args, expect in ((('1 Jan 2020', '7pm', 'x', 3), 'passed'), (('3-7 Dec', '7pm', 'x', 3), 'one date'),
                         (('12 Dec', 'after 7pm', 'x', 3), 'session time'), (('12 Dec', '7pm', '  ', 3), 'what the session'),
                         (('12 Dec', '7pm', 'x' * 101, 3), '100'), (('12 Dec', '7pm', 'x', 99), 'between 1'),
                         (('12 Dec', '7', 'x', 3), 'am or pm'), (('12 Dec', '7pm', '@everyone x', 3), None)):
        i = inter(member(50, 'Zed')); await mgr.propose(i, *args)
        got = last(i) if i.out else None
        assert (expect is None) or (expect in (got or '')), (args, got)
    print('bad input    : rejected with clear messages')
    ev_ping = store.get_session(max(x['id'] for x in store.list_sessions(['proposed'])))
    assert ev_ping['goal'] == '@everyone x'
    e, _ = await mgr.render(ev_ping); assert '@​everyone' in e.title, e.title     # defused in the card

    # --- Discord event failures don't block confirmation
    bot.guild.fail = discord.Forbidden(NS(status=403, reason='x'), 'no')
    i = inter(member(60, 'Cy')); await mgr.propose(i, '22 Dec', '7pm', 'No perms', 3)
    sid = max(x['id'] for x in store.list_sessions(['proposed']))
    v = (await mgr.render(store.get_session(sid)))[1]
    i = await click(mgr, v, gm, 'approve')
    assert store.get_session(sid)['status'] == 'confirmed' and 'Manage Events' in i.out[1][1]
    bot.guild.fail = None
    print('no event perm: confirmed anyway, GM told to grant Manage Events')

    # --- the reported bug: the bot can't see the channel, yet clicking must still redraw the card
    bot.get_partial_messageable(55).no_access = True
    i = inter(member(70, 'Di')); await mgr.propose(i, '23 Dec', '7pm', 'Hidden channel', 3)
    sid = max(x['id'] for x in store.list_sessions(['proposed']))
    v = (await mgr.render(store.get_session(sid)))[1]
    i = await click(mgr, v, gm, 'approve')
    assert store.get_session(sid)['status'] == 'confirmed'
    assert 'Cancel session' in labels(i.out[0][2]['view']) and 'Google' in labels(i.out[0][2]['view']), labels(i.out[0][2]['view'])
    assert 'confirmed' in i.out[-1][1] and i.out[-1][2]['allowed_mentions'].users          # public notice still sent
    v = i.out[0][2]['view']
    i = await click(mgr, v, gm, 'cancel')                                        # and cancelling works from that card
    assert store.get_session(sid)['status'] == 'cancelled' and i.out[0][2]['view'] is None
    bot.get_partial_messageable(55).no_access = False
    print('no channel access: card still redraws and cancel works')

    # --- calendar + restart + housekeeping
    booked = await mgr.calendar_sessions(date(2026, 12, 1), date(2026, 12, 31))
    print('on calendar  :', {str(k): v for k, v in booked.items()})
    assert date(2026, 12, 13) in booked and date(2026, 12, 22) in booked and date(2026, 12, 20) not in booked
    from calendar_render import render_month
    assert render_month(2026, 12, {}, {}, date(2026, 11, 18), 't', booked)[:4] == b'\x89PNG'
    bot2 = FakeBot(); mgr2 = SessionManager(bot2, store, CFG, TZ, is_gm, GM_ROLE, lambda: None)
    mgr2.now = lambda: NOW
    await mgr2.register_views()
    live = store.list_sessions(['proposed', 'countered', 'confirmed'])
    assert len(bot2.views) == len([x for x in live if x['message_id']]) > 0
    assert all(mid for mid, _ in bot2.views)
    # a proposal whose time has passed expires and loses its buttons
    old = store.create_session(5, 'Old', date(2026, 11, 17), 19*60, 22*60, 'too late', 4, 1)
    store.set_message(old, 55, 31337)
    before = len(bot.get_partial_messageable(55).edits); await mgr.housekeeping()
    assert store.get_session(old)['status'] == 'expired' and bot.get_partial_messageable(55).edits[-1][2] is None and len(bot.get_partial_messageable(55).edits) > before
    print('restart      : buttons re-attached;  housekeeping: stale proposal expired')
    print('\n✅ All session flows passed (calendar redraws requested:', len(refreshes), ')')


if __name__ == '__main__':
    asyncio.run(main())
