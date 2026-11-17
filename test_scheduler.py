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
from schedule_store import ScheduleStore, prune_backups

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


def test_store_gm():
    s = ScheduleStore(':memory:')
    assert s.schema_version == 1
    days = parse_dates('3-5 Dec', TODAY)
    assert s.set_gm_days(days, 'open', '  evenings\nonly  ') == 3
    got = s.gm_days(d(1, 12), d(31, 12))
    assert got[d(3, 12)] == ('open', 'evenings only')
    # Re-marking overwrites rather than duplicating
    s.set_gm_days([d(4, 12)], 'blocked')
    got = s.gm_days(d(1, 12), d(31, 12))
    assert got[d(4, 12)] == ('blocked', '') and len(got) == 3
    assert s.clear_gm_days([d(3, 12), d(9, 12)]) == 1
    assert d(3, 12) not in s.gm_days(d(1, 12), d(31, 12))
    try:
        s.set_gm_days([d(1, 12)], 'maybe')
        raise AssertionError('bad state accepted')
    except ValueError:
        pass
    print('✓ GM availability store')


def test_store_players():
    s = ScheduleStore(':memory:')
    s.set_player_days(111, 'Sam', [d(3, 12), d(4, 12)], True)
    s.set_player_days(222, 'Alex', [d(3, 12)], False)
    s.set_player_days(333, 'bea', [d(3, 12)], True)
    day = s.player_days(d(3, 12), d(3, 12))[d(3, 12)]
    assert day == {'free': ['bea', 'Sam'], 'busy': ['Alex']}, day
    # Changing your mind flips the row; renames follow through
    s.set_player_days(111, 'Sammy', [d(3, 12)], False)
    day = s.player_days(d(3, 12), d(4, 12))
    assert day[d(3, 12)]['busy'] == ['Alex', 'Sammy']
    assert day[d(4, 12)]['free'] == ['Sammy']
    assert s.player_own_days(111, d(1, 12), d(31, 12)) == {d(3, 12): False, d(4, 12): True}
    assert s.clear_player_days(111, [d(3, 12), d(4, 12)]) == 2
    assert s.player_own_days(111, d(1, 12), d(31, 12)) == {}
    print('✓ Player availability store')


def test_store_text_is_stored_raw():
    """Player text is stored as plain data: no formula or SQL tricks survive as anything else."""
    s = ScheduleStore(':memory:')
    evil = "=IMPORTXML(\"x\"); DROP TABLE settings;--"
    s.set_player_days(1, evil, [d(3, 12)], True)
    s.set_gm_days([d(3, 12)], 'open', evil)
    assert s.player_days(d(3, 12), d(3, 12))[d(3, 12)]['free'] == [evil]
    assert s.gm_days(d(3, 12), d(3, 12))[d(3, 12)][1] == evil
    s.set_setting('k', 'v')
    assert s.get_setting('k') == 'v'
    print('✓ Text is stored as data')


def test_store_concurrent_writes():
    """Many simultaneous writers: nothing is lost."""
    with tempfile.TemporaryDirectory() as tmp:
        s = ScheduleStore(os.path.join(tmp, 'schedule.db'))

        def mark(uid):
            for n in range(1, 11):
                s.set_player_days(uid, f'P{uid}', [d(n, 12)], True)

        threads = [threading.Thread(target=mark, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        days = s.player_days(d(1, 12), d(10, 12))
        assert all(len(days[d(n, 12)]['free']) == 20 for n in range(1, 11))
        s.close()
    print('✓ Concurrent writes')


def test_settings_and_persistence():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'data', 'schedule.db')
        s = ScheduleStore(path)
        s.set_setting('pinned', '123')
        s.set_gm_days([d(3, 12)], 'open')
        s.close()
        s = ScheduleStore(path)  # reopening doesn't re-run migrations
        assert s.schema_version == 1
        assert s.get_setting('pinned') == '123'
        s.set_setting('pinned', None)
        assert s.get_setting('pinned') is None
        assert d(3, 12) in s.gm_days(d(1, 12), d(31, 12))
        s.close()
    print('✓ Settings and persistence')


def test_backup_and_prune():
    with tempfile.TemporaryDirectory() as tmp:
        s = ScheduleStore(os.path.join(tmp, 'schedule.db'))
        s.set_gm_days([d(3, 12)], 'open', 'backup me')
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
    gm = {d(3, 12): ('open', 'evenings only, a long note that needs trimming'), d(24, 12): ('blocked', '')}
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
    test_store_gm()
    test_store_players()
    test_store_text_is_stored_raw()
    test_store_concurrent_writes()
    test_settings_and_persistence()
    test_backup_and_prune()
    test_render_month()
    print('\n✅ All tests passed!')


if __name__ == '__main__':
    run_tests()
