"""
Tests for the scheduler: date parsing, the SQLite store and the calendar image.

Run with: python test_scheduler.py   (or pytest test_scheduler.py)
"""
import io
import os
import sqlite3
import tempfile
import threading
from datetime import date

from schedule_dates import DateParseError, format_days, parse_dates, parse_month
from schedule_store import MIGRATIONS, ScheduleStore, prune_backups
from schedule_times import (
    DAY, TimeParseError, best_slots, format_windows, gm_calendar_days, gm_day_summary,
    group_days_by_windows, merge, parse_times, player_calendar_days, player_day_summary, subtract,
)

TODAY = date(2026, 11, 17)


def d(day, month, year=2026):
    return date(year, month, day)


def expect_error(spec, today=TODAY):
    try:
        parse_dates(spec, today)
    except DateParseError:
        return
    raise AssertionError(f'{spec!r} should not parse')


def test_single_dates():
    assert parse_dates('3 Dec', TODAY) == [d(3, 12)]
    assert parse_dates('3rd December', TODAY) == [d(3, 12)]
    assert parse_dates('Dec 3', TODAY) == [d(3, 12)]
    assert parse_dates('2026-12-03', TODAY) == [d(3, 12)]
    assert parse_dates('3/12', TODAY) == [d(3, 12)]
    assert parse_dates('3/12/27', TODAY) == [d(3, 12, 2027)]
    assert parse_dates('today', TODAY) == [TODAY]
    assert parse_dates('tomorrow', TODAY) == [d(18, 11)]
    print('✓ Single dates')


def test_year_inference():
    # A date earlier in the year than today rolls into next year
    assert parse_dates('5 Jan', TODAY) == [d(5, 1, 2027)]
    assert parse_dates('10 Nov', TODAY) == [d(10, 11, 2027)]
    # Today's own month still means this month
    nov = parse_dates('November', TODAY)
    assert nov[0] == d(1, 11) and nov[-1] == d(30, 11)
    # An explicit year is respected even in the past (the bot drops past dates)
    assert parse_dates('5 Jan 2026', TODAY) == [d(5, 1, 2026)]
    print('✓ Year inference')


def test_ranges():
    assert parse_dates('3-7 Dec', TODAY) == [d(n, 12) for n in range(3, 8)]
    assert parse_dates('3 - 7 Dec', TODAY) == [d(n, 12) for n in range(3, 8)]
    assert parse_dates('3 Dec to 7 Dec', TODAY) == [d(n, 12) for n in range(3, 8)]
    assert parse_dates('3 Dec – 7', TODAY) == [d(n, 12) for n in range(3, 8)]
    # Across the year end
    span = parse_dates('28 Dec - 3 Jan', TODAY)
    assert span[0] == d(28, 12) and span[-1] == d(3, 1, 2027) and len(span) == 7
    # Whole months
    dec = parse_dates('December', TODAY)
    assert len(dec) == 31 and dec[0] == d(1, 12)
    both = parse_dates('Dec - Jan', TODAY)
    assert both[0] == d(1, 12) and both[-1] == d(31, 1, 2027)
    assert parse_dates('2026-12-01 - 2026-12-03', TODAY) == [d(1, 12), d(2, 12), d(3, 12)]
    print('✓ Ranges')


def test_lists():
    assert parse_dates('3, 5, 12 Dec', TODAY) == [d(3, 12), d(5, 12), d(12, 12)]
    assert parse_dates('5 and 12 Dec', TODAY) == [d(5, 12), d(12, 12)]
    assert parse_dates('3-4 Dec, 10 Jan', TODAY) == [d(3, 12), d(4, 12), d(10, 1, 2027)]
    # A bare day after a dated part borrows from it
    assert parse_dates('3 Dec, 5', TODAY) == [d(3, 12), d(5, 12)]
    # Duplicates collapse
    assert parse_dates('3 Dec, 3-4 Dec', TODAY) == [d(3, 12), d(4, 12)]
    print('✓ Lists')


def test_errors():
    for bad in ['', '   ', 'soon', '31 Feb', '3', '3 Fooember', '1-2-3 Dec',
                '2026-12-10 - 2026-12-01', '13/13', '1 Jan 2027 - 1 Jan 2028']:
        expect_error(bad)
    print('✓ Bad input is rejected')


def test_parse_month():
    assert parse_month(None, TODAY) == (2026, 11)
    assert parse_month('January', TODAY) == (2027, 1)
    assert parse_month('next', TODAY) == (2026, 12)
    assert parse_month('2027-03', TODAY) == (2027, 3)
    assert parse_month('next', d(5, 12)) == (2027, 1)
    print('✓ Month parsing')


def test_format_days():
    assert format_days([]) == 'no dates'
    assert format_days([d(3, 12)]) == '3 Dec'
    assert format_days([d(n, 12) for n in range(3, 8)] + [d(12, 12)]) == '3–7 Dec, 12 Dec'
    assert format_days(parse_dates('30 Dec - 2 Jan', TODAY)) == '30 Dec–2 Jan 2027'
    print('✓ Date formatting')


def h(hour, minute=0):
    return hour * 60 + minute


def expect_time_error(spec):
    try:
        parse_times(spec)
    except TimeParseError:
        return
    raise AssertionError(f'{spec!r} should not parse')


def test_parse_times():
    assert parse_times(None) == [DAY] and parse_times('') == [DAY] and parse_times('  ') == [DAY]
    assert parse_times('all day') == [DAY] and parse_times('Anytime') == [DAY]
    assert parse_times('after 7pm') == [(h(19), 1440)]
    assert parse_times('from 7pm') == [(h(19), 1440)]
    assert parse_times('7pm onwards') == [(h(19), 1440)] and parse_times('7pm+') == [(h(19), 1440)]
    assert parse_times('before 5pm') == [(0, h(17))]
    assert parse_times('until 17:30') == [(0, h(17, 30))]
    assert parse_times('7pm-10pm') == [(h(19), h(22))]
    assert parse_times('7pm to 10pm') == [(h(19), h(22))]
    assert parse_times('from 7pm until 10pm') == [(h(19), h(22))]
    assert parse_times('between 7pm and 10pm') == [(h(19), h(22))]
    assert parse_times('7-10pm') == [(h(19), h(22))]            # pm carries back
    assert parse_times('9am-5') == [(h(9), h(17))]               # and forward
    assert parse_times('11-1pm') == [(h(11), h(13))]             # crossing noon
    assert parse_times('12-2pm') == [(h(12), h(14))]
    assert parse_times('12am-6am') == [(0, h(6))]
    assert parse_times('7:30pm-9:45pm') == [(h(19, 30), h(21, 45))]
    assert parse_times('19:00-22:00') == [(h(19), h(22))]
    assert parse_times('1900-2200') == [(h(19), h(22))]
    assert parse_times('07:00-10:00') == [(h(7), h(10))]         # colon, no am/pm: 24-hour
    assert parse_times('18:00-24:00') == [(h(18), 1440)]
    assert parse_times('7pm-midnight') == [(h(19), 1440)] and parse_times('7pm-12am') == [(h(19), 1440)]
    assert parse_times('noon-3pm') == [(h(12), h(15))]
    assert parse_times('evenings') == [(h(18), 1440)]
    assert parse_times('morning') == [(h(6), h(12))]
    assert parse_times('afternoon') == [(h(12), h(17))]
    assert parse_times('12-2pm, 7-10pm') == [(h(12), h(14)), (h(19), h(22))]
    assert parse_times('7PM - 10PM') == [(h(19), h(22))]
    # Overlapping pieces merge
    assert parse_times('6pm-9pm, 8pm-10pm') == [(h(18), h(22))]
    print('✓ Time parsing')


def test_parse_times_errors():
    for bad in ['soon', '7', '7-10', 'after 7', '25:00', '7:75pm', '13pm', '0pm', '10pm-7pm', '9pm-9pm',
                '7pm', '7pm-', '-7pm', '1pm-2pm-3pm', 'after midnight', 'before 12am-ish',
                '1,2,3,4,5,6,7,8']:
        expect_time_error(bad)
    print('✓ Bad times are rejected')


def test_window_algebra():
    assert merge([(10, 20), (15, 30), (30, 40), (50, 60)]) == [(10, 40), (50, 60)]
    assert subtract([DAY], [(h(12), h(14))]) == [(0, h(12)), (h(14), 1440)]
    assert subtract([(100, 200)], [(0, 1440)]) == []
    assert subtract([(100, 200)], [(300, 400)]) == [(100, 200)]
    assert subtract([(100, 200), (300, 400)], [(150, 350)]) == [(100, 150), (350, 400)]
    assert format_windows([DAY]) == 'all day'
    assert format_windows([(h(19), 1440)]) == 'from 19:00'
    assert format_windows([(0, h(17))]) == 'until 17:00'
    assert format_windows([(h(12), h(14)), (h(19), h(22))]) == '12:00–14:00, 19:00–22:00'
    assert format_windows([]) == 'no times'
    print('✓ Window maths and formatting')


def test_summaries():
    # Blocked time is cut out of open time
    rows = [('open', 0, 1440, 'hi'), ('blocked', h(12), h(14), '')]
    assert gm_day_summary(rows) == ('open', [(0, h(12)), (h(14), 1440)], 'hi')
    assert gm_day_summary([('blocked', 0, 1440, 'away')]) == ('blocked', [DAY], 'away')
    assert gm_day_summary([('open', h(9), h(10), ''), ('blocked', 0, 1440, '')])[0] == 'blocked'
    assert gm_day_summary([]) is None
    free, busy = player_day_summary([('free', h(18), 1440), ('busy', h(20), h(21))])
    assert free == [(h(18), h(20)), (h(21), 1440)] and busy == [(h(20), h(21))]
    raw = {d(3, 12): {'1': ('bea', [('free', h(19), 1440)]), '2': ('Alex', [('busy', 0, 1440)]),
                      '3': ('Sam', [('free', 0, 1440)])}}
    assert player_calendar_days(raw) == {d(3, 12): {'free': ['bea', 'Sam'], 'busy': ['Alex']}}
    assert gm_calendar_days({d(3, 12): rows, d(4, 12): []}) == {d(3, 12): gm_day_summary(rows)}
    assert group_days_by_windows({d(3, 12): [DAY], d(4, 12): [(h(19), 1440)], d(5, 12): [DAY]}) == {
        'all day': [d(3, 12), d(5, 12)], 'from 19:00': [d(4, 12)]}
    print('✓ Summaries')


def test_best_slots():
    gm = [(h(18), h(23))]
    players = {'Sam': [DAY], 'Bea': [(h(20), 1440)], 'Alex': [(0, h(19))]}
    assert best_slots(gm, players) == [
        (h(18), h(19), ['Alex', 'Sam']),
        (h(19), h(20), ['Sam']),
        (h(20), h(23), ['Bea', 'Sam']),
    ]
    # Adjacent stretches with the same people join up
    assert best_slots([DAY], {'Sam': [(h(9), h(12)), (h(12), h(15))]}) == [(h(9), h(15), ['Sam'])]
    # No overlap at all
    assert best_slots([(h(9), h(12))], {'Sam': [(h(19), 1440)]}) == []
    assert best_slots([], {'Sam': [DAY]}) == []
    print('✓ Best slots')


def test_store_gm():
    s = ScheduleStore(':memory:')
    assert s.schema_version == len(MIGRATIONS)
    days = parse_dates('3-5 Dec', TODAY)
    assert s.set_gm_days(days, 'open', None, '  evenings\nonly  ') == 3
    got = s.gm_days(d(1, 12), d(31, 12))
    assert got[d(3, 12)] == [('open', 0, 1440, 'evenings only')]
    # Same state again replaces; it doesn't pile up
    s.set_gm_days([d(3, 12)], 'open', [(h(19), 1440)], '')
    assert s.gm_days(d(3, 12), d(3, 12))[d(3, 12)] == [('open', h(19), 1440, '')]
    # Blocking cuts open time rather than erasing it
    s.set_gm_days([d(4, 12)], 'blocked', [(h(12), h(14))])
    assert gm_day_summary(s.gm_days(d(4, 12), d(4, 12))[d(4, 12)]) == (
        'open', [(0, h(12)), (h(14), 1440)], 'evenings only')
    # Opening a window clears blocked time inside it ("block the month, then open a day")
    s.set_gm_days([d(5, 12)], 'blocked')
    s.set_gm_days([d(5, 12)], 'open', [(h(19), h(22))])
    assert gm_day_summary(s.gm_days(d(5, 12), d(5, 12))[d(5, 12)])[:2] == ('open', [(h(19), h(22))])
    # Clearing: a window, then the whole day
    assert s.clear_gm_days([d(3, 12)], [(h(20), h(21))]) == 1
    assert gm_day_summary(s.gm_days(d(3, 12), d(3, 12))[d(3, 12)])[1] == [(h(19), h(20)), (h(21), 1440)]
    assert s.clear_gm_days([d(3, 12), d(9, 12)]) == 1
    assert d(3, 12) not in s.gm_days(d(1, 12), d(31, 12))
    for bad in (lambda: s.set_gm_days([d(1, 12)], 'maybe'),
                lambda: s.set_gm_days([d(1, 12)], 'open', [(100, 50)]),
                lambda: s.set_gm_days([d(1, 12)], 'open', [])):
        try:
            bad()
            raise AssertionError('bad input accepted')
        except ValueError:
            pass
    print('✓ GM availability store')


def test_store_players():
    s = ScheduleStore(':memory:')
    s.set_player_days(111, 'Sam', [d(3, 12), d(4, 12)], True)
    s.set_player_days(222, 'Alex', [d(3, 12)], False)
    s.set_player_days(333, 'bea', [d(3, 12)], True, [(h(19), 1440)])
    day = player_calendar_days(s.player_days(d(3, 12), d(3, 12)))[d(3, 12)]
    assert day == {'free': ['bea', 'Sam'], 'busy': ['Alex']}, day
    # Changing your mind replaces; renames follow through
    s.set_player_days(111, 'Sammy', [d(3, 12)], False)
    day = player_calendar_days(s.player_days(d(3, 12), d(4, 12)))
    assert day[d(3, 12)]['busy'] == ['Alex', 'Sammy']
    assert day[d(4, 12)]['free'] == ['Sammy']
    # Free evenings after being busy all day: evenings win, the rest stays busy
    s.set_player_days(111, 'Sammy', [d(4, 12)], False)
    s.set_player_days(111, 'Sammy', [d(4, 12)], True, [(h(18), 1440)])
    own = s.player_own_days(111, d(4, 12), d(4, 12))[d(4, 12)]
    assert player_day_summary(own) == ([(h(18), 1440)], [(0, h(18))])
    assert s.clear_player_days(111, [d(3, 12), d(4, 12)]) == 2
    assert s.player_own_days(111, d(1, 12), d(31, 12)) == {}
    print('✓ Player availability store')


def test_store_text_is_stored_raw():
    """Player text is stored as plain data: no formula or SQL tricks survive as anything else."""
    s = ScheduleStore(':memory:')
    evil = "=IMPORTXML(\"x\"); DROP TABLE settings;--"
    s.set_player_days(1, evil, [d(3, 12)], True)
    s.set_gm_days([d(3, 12)], 'open', None, evil)
    assert s.player_days(d(3, 12), d(3, 12))[d(3, 12)]['1'][0] == evil
    assert s.gm_days(d(3, 12), d(3, 12))[d(3, 12)][0][3] == evil
    s.set_setting('k', 'v')
    assert s.get_setting('k') == 'v'
    print('✓ Text is stored as data')


def test_store_concurrent_writes():
    """Many simultaneous writers: nothing is lost."""
    with tempfile.TemporaryDirectory() as tmp:
        s = ScheduleStore(os.path.join(tmp, 'schedule.db'))

        def mark(uid):
            for n in range(1, 11):
                s.set_player_days(uid, f'P{uid}', [d(n, 12)], True, [(h(19), 1440)])

        threads = [threading.Thread(target=mark, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        days = s.player_days(d(1, 12), d(10, 12))
        assert all(len(days[d(n, 12)]) == 20 for n in range(1, 11))
        s.close()
    print('✓ Concurrent writes')


def test_migration_keeps_existing_data():
    """A phase-1 database (whole-day rows) upgrades without losing anything."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'schedule.db')
        old = sqlite3.connect(path)
        old.executescript(MIGRATIONS[0] + 'PRAGMA user_version = 1;')
        old.executemany('INSERT INTO gm_availability VALUES (?, ?, ?, ?)',
                        [('2026-12-03', 'open', 'evenings', 'x'), ('2026-12-24', 'blocked', 'Christmas', 'x')])
        old.executemany('INSERT INTO player_availability VALUES (?, ?, ?, ?, ?)',
                        [('1', '2026-12-03', 1, 'Sam', 'x'), ('2', '2026-12-03', 0, 'Alex', 'x')])
        old.execute("INSERT INTO settings VALUES ('pinned_calendar_message', '555')")
        old.commit()
        old.close()

        s = ScheduleStore(path)
        assert s.schema_version == len(MIGRATIONS)
        assert s.gm_days(d(1, 12), d(31, 12)) == {
            d(3, 12): [('open', 0, 1440, 'evenings')], d(24, 12): [('blocked', 0, 1440, 'Christmas')]}
        assert player_calendar_days(s.player_days(d(3, 12), d(3, 12)))[d(3, 12)] == {
            'free': ['Sam'], 'busy': ['Alex']}
        assert s.get_setting('pinned_calendar_message') == '555'
        s.set_player_days(1, 'Sam', [d(4, 12)], True, [(h(19), 1440)])  # new schema is usable
        assert os.path.exists(path + '.before-v2')            # pre-upgrade copy kept
        s.close()
        s = ScheduleStore(path)                                # reopening doesn't re-migrate
        assert s.schema_version == len(MIGRATIONS)
        s.close()
    print('✓ Migration from phase 1')


def test_settings_and_persistence():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'data', 'schedule.db')
        s = ScheduleStore(path)
        s.set_setting('pinned', '123')
        s.set_gm_days([d(3, 12)], 'open')
        s.close()
        s = ScheduleStore(path)
        assert s.schema_version == len(MIGRATIONS)
        assert s.get_setting('pinned') == '123'
        s.set_setting('pinned', None)
        assert s.get_setting('pinned') is None
        assert d(3, 12) in s.gm_days(d(1, 12), d(31, 12))
        s.close()
        assert not os.path.exists(path + '.before-v2')          # a fresh database needs no upgrade copy
    print('✓ Settings and persistence')


def test_backup_and_prune():
    with tempfile.TemporaryDirectory() as tmp:
        s = ScheduleStore(os.path.join(tmp, 'schedule.db'))
        s.set_gm_days([d(3, 12)], 'open', None, 'backup me')
        backups = os.path.join(tmp, 'backups')
        for stamp in ['20261201-0000', '20261202-0000', '20261203-0000']:
            s.backup(os.path.join(backups, f'schedule-{stamp}.db'))
        copy = sqlite3.connect(os.path.join(backups, 'schedule-20261203-0000.db'))
        assert copy.execute('SELECT note FROM gm_availability').fetchone()[0] == 'backup me'
        copy.close()
        removed = prune_backups(backups, keep=2)
        assert [os.path.basename(p) for p in removed] == ['schedule-20261201-0000.db']
        assert sorted(os.listdir(backups)) == ['schedule-20261202-0000.db', 'schedule-20261203-0000.db']
        s.close()
    print('✓ Backup and prune')


def test_render_month():
    from PIL import Image
    from calendar_render import render_month, next_month, month_bounds
    gm = {d(3, 12): ('open', [(h(19), 1440)], 'a long note that needs trimming'),
          d(4, 12): ('open', [(h(12), h(14)), (h(19), h(22))], ''),
          d(24, 12): ('blocked', [DAY], '')}
    players = {d(3, 12): {'free': ['Sam', 'Alex', 'Bea', 'Cass', 'Dev'], 'busy': ['Fen']}}
    png = render_month(2026, 12, gm, players, TODAY, 'Tales from Fellgard · sessions')
    img = Image.open(io.BytesIO(png))
    assert img.format == 'PNG' and img.width == 28 * 2 + 160 * 7
    # Empty month renders too
    assert render_month(2027, 2, {}, {}, TODAY)[:8] == b'\x89PNG\r\n\x1a\n'
    assert next_month(2026, 12) == (2027, 1)
    assert month_bounds(2028, 2) == (date(2028, 2, 1), date(2028, 2, 29))
    print('✓ Calendar renders')


def run_tests():
    print('Running scheduler tests...\n')
    test_single_dates()
    test_year_inference()
    test_ranges()
    test_lists()
    test_errors()
    test_parse_month()
    test_format_days()
    test_parse_times()
    test_parse_times_errors()
    test_window_algebra()
    test_summaries()
    test_best_slots()
    test_store_gm()
    test_store_players()
    test_store_text_is_stored_raw()
    test_store_concurrent_writes()
    test_migration_keeps_existing_data()
    test_settings_and_persistence()
    test_backup_and_prune()
    test_render_month()
    print('\n✅ All tests passed!')


if __name__ == '__main__':
    run_tests()
