"""
Tests for reminder timing: who is due what, and when. Pure logic, no Discord.

Run with: python test_reminders.py
"""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from reminders import (
    Due, digest_due, due_reminders, parse_hhmm, parse_hours, reminder_plan, waiting_proposals,
)

UK = ZoneInfo('Europe/London')
UTC = timezone.utc
HOURS = [24.0, 1.0]
NINE = time(9, 0)


def at(y, m, d, hh, mm=0, tz=UK):
    return datetime(y, m, d, hh, mm, tzinfo=tz)


def session(sid=1, day=(2026, 12, 12), start=19 * 60, players=('1', '2', '3'), status='confirmed', confirmed_at=''):
    from datetime import date
    return {'id': sid, 'status': status, 'day': date(*day), 'start_min': start, 'end_min': start + 180,
            'players': [(p, f'P{p}') for p in players], 'confirmed_at': confirmed_at, 'goal': 'g',
            'party_size': 5, 'created_at': '2026-12-01T10:00:00+00:00'}


def due(sessions, now, sent=(), hours=HOURS, day=NINE):
    return due_reminders(sessions, set(sent), now, UK, hours, day)


def kinds(result):
    return sorted((d.kind, d.user_id) for d in result)


def test_parsing():
    assert parse_hhmm('09:00') == time(9, 0) and parse_hhmm('18:30') == time(18, 30)
    assert parse_hhmm('') is None and parse_hhmm(None) is None and parse_hhmm(False) is None
    for bad in ('9', 'noon', '25:00', '9:99'):
        try:
            parse_hhmm(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    assert parse_hours([1, 24, 24, 0.5]) == [24.0, 1.0, 0.5] and parse_hours([]) == [] and parse_hours(None) == []
    for bad in ([0], [-1]):
        try:
            parse_hours(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    print('✓ Parsing')


def test_dm_timing():
    s = [session()]
    assert due(s, at(2026, 12, 11, 18, 59), day=None) == []                      # just before 24h
    got = due(s, at(2026, 12, 11, 19, 0), day=None)
    assert kinds(got) == [('dm24', '1'), ('dm24', '2'), ('dm24', '3')]           # exactly 24h before
    sent = {(1, 'dm24', '1'), (1, 'dm24', '2'), (1, 'dm24', '3')}
    assert due(s, at(2026, 12, 12, 17, 59), sent, day=None) == []                # nothing new until 1h before
    assert kinds(due(s, at(2026, 12, 12, 18, 0), sent, day=None)) == [('dm1', '1'), ('dm1', '2'), ('dm1', '3')]
    done = sent | {(1, 'dm1', '1'), (1, 'dm1', '2'), (1, 'dm1', '3')}
    assert due(s, at(2026, 12, 12, 18, 30), done, day=None) == []                # once only
    assert due(s, at(2026, 12, 12, 19, 0), day=None) == []                       # started: too late
    assert due(s, at(2026, 12, 12, 20, 0), day=None) == []

    # The bot was down at the 24h mark: it still sends, until the 1h reminder takes over
    assert len(due(s, at(2026, 12, 12, 9, 0), day=None)) == 3 and {d.kind for d in due(s, at(2026, 12, 12, 9, 0), day=None)} == {'dm24'}
    assert {d.kind for d in due(s, at(2026, 12, 12, 18, 10), day=None)} == {'dm1'}      # 24h is no longer sent
    print('✓ DM timing')


def test_dm_people():
    s = session(players=('1', '2'))
    sent = {(1, 'dm24', '1'), (1, 'dm24', '2')}
    # Someone joins after the 24h reminder went out: they still get the one they haven't had
    s['players'].append(('4', 'P4'))
    assert kinds(due([s], at(2026, 12, 12, 10, 0), sent, day=None)) == [('dm24', '4')]
    # Someone who left gets nothing
    s['players'] = [('1', 'P1')]
    assert due([s], at(2026, 12, 11, 19, 0), day=None) == [Due(1, 'dm24', '1')]
    assert due([session(players=())], at(2026, 12, 11, 19, 0), day=None) == []
    print('✓ DM recipients')


def test_confirmed_late():
    # Confirmed 3 hours before the start: the 24h reminder would be silly (the confirmation just told them)
    s = [session(confirmed_at=at(2026, 12, 12, 16, 0).astimezone(UTC).isoformat())]
    assert due(s, at(2026, 12, 12, 16, 5), day=None) == []                        # 24h reminder skipped
    assert {d.kind for d in due(s, at(2026, 12, 12, 18, 0), day=None)} == {'dm1'}  # but 1h is still useful
    # Confirmed 30 minutes before: nothing at all
    s = [session(confirmed_at=at(2026, 12, 12, 18, 30).astimezone(UTC).isoformat())]
    assert due(s, at(2026, 12, 12, 18, 31), day=None) == []
    # Confirmed days ago: everything on time
    s = [session(confirmed_at=at(2026, 12, 1, 10).astimezone(UTC).isoformat())]
    assert len(due(s, at(2026, 12, 11, 19, 0), day=None)) == 3
    print('✓ Reminders skipped when the confirmation already covered them')


def test_other_statuses_and_hours():
    for status in ('proposed', 'countered', 'declined', 'cancelled', 'expired', 'done'):
        assert due([session(status=status)], at(2026, 12, 11, 19, 0)) == [], status
    assert due([session()], at(2026, 12, 11, 19, 0), hours=[], day=None) == []     # DMs switched off
    # Custom lead times chain: 48h, 2h, 30min
    s = [session(players=('1',))]
    h3 = [48.0, 2.0, 0.5]
    assert kinds(due(s, at(2026, 12, 10, 19, 0), hours=h3, day=None)) == [('dm48', '1')]
    assert kinds(due(s, at(2026, 12, 12, 17, 0), hours=h3, day=None)) == [('dm2', '1')]
    assert kinds(due(s, at(2026, 12, 12, 18, 30), hours=h3, day=None)) == [('dm0.5', '1')]
    print('✓ Statuses and custom lead times')


def test_day_post():
    s = [session()]
    sent_dm = {(1, 'dm24', p) for p in '123'}
    assert due(s, at(2026, 12, 12, 8, 59), sent_dm) == []
    assert due(s, at(2026, 12, 12, 9, 0), sent_dm) == [Due(1, 'day', '')]
    assert due(s, at(2026, 12, 12, 9, 0), sent_dm | {(1, 'day', '')}) == []               # once only
    assert due(s, at(2026, 12, 12, 17, 59), sent_dm) == [Due(1, 'day', '')]               # still fine later in the day
    assert [d.kind for d in due(s, at(2026, 12, 12, 18, 0), sent_dm)] == ['dm1', 'dm1', 'dm1']  # within the last hour: DMs only
    assert due(s, at(2026, 12, 12, 9, 0), sent_dm, day=None) == []                        # switched off
    # Early start (09:30): too close to the morning post, so skip it (DMs cover it)
    early = [session(start=9 * 60 + 30)]
    assert not any(d.kind == 'day' for d in due(early, at(2026, 12, 12, 9, 0)))
    # Confirmed on the day after the morning post time: no post
    late = [session(confirmed_at=at(2026, 12, 12, 11, 0).astimezone(UTC).isoformat())]
    assert not any(d.kind == 'day' for d in due(late, at(2026, 12, 12, 12, 0)))
    # The post is for the session's own day only
    assert not any(d.kind == 'day' for d in due(s, at(2026, 12, 11, 9, 0)))
    print('✓ On-the-day post')


def test_daylight_saving():
    # Clocks go forward on 2026-03-29. A 19:00 session that day (BST) starts at 18:00 UTC,
    # so "24 hours before" is 18:00 UTC the day before, which is 18:00 GMT on the wall clock, not 19:00.
    s = [session(day=(2026, 3, 29))]
    assert due(s, datetime(2026, 3, 28, 17, 59, tzinfo=UTC), day=None) == []
    assert len(due(s, datetime(2026, 3, 28, 18, 0, tzinfo=UTC), day=None)) == 3
    assert len(due(s, datetime(2026, 3, 29, 17, 0, tzinfo=UTC), sent={(1, 'dm24', p) for p in '123'}, day=None)) == 3  # 1h before
    # Same moment given in a different timezone works the same
    assert len(due(s, datetime(2026, 3, 28, 18, 0, tzinfo=ZoneInfo('America/New_York')) - timedelta(hours=4), day=None)) == 3
    print('✓ Daylight saving')


def test_plan():
    s = session(confirmed_at=at(2026, 12, 12, 16, 0).astimezone(UTC).isoformat())
    plan = reminder_plan(s, {(1, 'dm1', '1')}, at(2026, 12, 12, 18, 5), UK, HOURS, NINE)
    labels = {label: (when.strftime('%a %H:%M'), state) for label, when, state in plan}
    assert labels['DM 24h before'][0] == 'Fri 19:00' and 'skipped' in labels['DM 24h before'][1]
    assert labels['DM 1h before'] == ('Sat 18:00', 'due now (1 of 3 sent)')
    assert labels['Channel post on the day'][0] == 'Sat 09:00' and 'skipped' in labels['Channel post on the day'][1]
    plan = reminder_plan(session(), set(), at(2026, 12, 10, 12), UK, HOURS, NINE)
    assert [state for *_, state in plan] == ['pending', 'pending', 'pending']
    plan = reminder_plan(session(), {(1, 'dm24', p) for p in '123'} | {(1, 'day', '')}, at(2026, 12, 12, 12), UK, HOURS, NINE)
    assert [state for *_, state in plan] == ['sent to 3', 'pending', 'sent']
    print('✓ Reminder plan')


def test_gm_digest():
    now = at(2026, 12, 10, 9, 5)
    old = dict(session(sid=1, status='proposed'), created_at=at(2026, 12, 9, 8).astimezone(UTC).isoformat())
    fresh = dict(session(sid=2, status='proposed'), created_at=at(2026, 12, 10, 8).astimezone(UTC).isoformat())
    answered = dict(session(sid=3, status='confirmed'), created_at=at(2026, 12, 1, 10).astimezone(UTC).isoformat())
    countered = dict(session(sid=4, status='countered'), created_at=at(2026, 12, 1, 10).astimezone(UTC).isoformat())
    past = dict(session(sid=5, status='proposed', day=(2026, 12, 9)), created_at=at(2026, 12, 1, 10).astimezone(UTC).isoformat())
    got = waiting_proposals([old, fresh, answered, countered, past], now, UK, 24)
    assert [s['id'] for s in got] == [1]            # old enough, still waiting on the GM, not already past
    assert [s['id'] for s in waiting_proposals([old, fresh], now, UK, 0)] == [1, 2]

    assert not digest_due(at(2026, 12, 10, 8, 59), NINE, None)
    assert digest_due(at(2026, 12, 10, 9, 0), NINE, None) and digest_due(at(2026, 12, 10, 23, 0), NINE, '2026-12-09')
    assert not digest_due(at(2026, 12, 10, 9, 0), NINE, '2026-12-10')       # once a day
    assert not digest_due(at(2026, 12, 10, 12, 0), None, None)              # switched off
    print('✓ GM digest')


def run_tests():
    print('Running reminder tests...\n')
    test_parsing()
    test_dm_timing()
    test_dm_people()
    test_confirmed_late()
    test_other_statuses_and_hours()
    test_day_post()
    test_daylight_saving()
    test_plan()
    test_gm_digest()
    print('\n✅ All tests passed!')


if __name__ == '__main__':
    run_tests()
