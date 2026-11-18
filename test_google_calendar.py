"""
Tests for the shared Google Calendar sync: the REST client (against a fake HTTP layer),
reconciling the store with the calendar, and the subscribe links.

Run with: python test_google_calendar.py
"""
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import calendar_links as cl
from google_calendar import (
    CalendarError, GoogleCalendarClient, body_hash, build_event_body, event_id_for, sync_sessions,
)
from schedule_store import ScheduleStore

UK = ZoneInfo('Europe/London')
TODAY = date(2026, 11, 18)


def h(hour, minute=0):
    return hour * 60 + minute


class Resp:
    def __init__(self, status=200, body=None, text=''):
        self.status_code, self._body, self.text = status, body or {}, text

    def json(self):
        return self._body


class FakeHTTP:
    """Records calls; answers from a script of status codes (default 200)."""

    def __init__(self):
        self.calls, self.script = [], []

    def _answer(self, method, url, **kw):
        self.calls.append((method, url, kw.get('json')))
        status = self.script.pop(0) if self.script else 200
        body = {'error': {'message': f'boom {status}'}} if status >= 400 else {}
        return Resp(status, body)

    def get(self, url, **kw): return self._answer('GET', url, **kw)
    def post(self, url, **kw): return self._answer('POST', url, **kw)
    def put(self, url, **kw): return self._answer('PUT', url, **kw)
    def delete(self, url, **kw): return self._answer('DELETE', url, **kw)


def client(http=None, cal='abc@group.calendar.google.com'):
    c = GoogleCalendarClient(cal, http=http or FakeHTTP())
    c.service_account_email = 'bot@proj.iam.gserviceaccount.com'
    return c


def body(title='Fellgard: goal', start=None):
    start = start or datetime(2026, 12, 12, 19, 0, tzinfo=UK)
    return build_event_body(title, 'goal\nDetails: https://x', 'Discord', start, start.replace(hour=22), 'Europe/London')


def test_event_body_and_ids():
    for sid in (1, 7, 12345):
        assert re.fullmatch(r'[0-9a-v]{5,1024}', event_id_for(sid)), event_id_for(sid)   # Google's id rules
    b = body()
    assert b['start'] == {'dateTime': '2026-12-12T19:00:00+00:00', 'timeZone': 'Europe/London'}
    assert b['end']['dateTime'] == '2026-12-12T22:00:00+00:00' and b['reminders'] == {'useDefault': False}
    assert body_hash(b) == body_hash(body()) and body_hash(b) != body_hash(body('Fellgard: other'))
    # summer time keeps the wall-clock time and the right offset
    summer = body(start=datetime(2026, 7, 4, 19, 0, tzinfo=UK))
    assert summer['start']['dateTime'] == '2026-07-04T19:00:00+01:00'
    print('✓ Event bodies and ids')


def test_client_requests():
    http = FakeHTTP()
    c = client(http, cal='me+x@gmail.com')
    c.upsert_event('fellgardsession1', body())
    method, url, sent = http.calls[-1]
    assert method == 'POST' and url == 'https://www.googleapis.com/calendar/v3/calendars/me%2Bx%40gmail.com/events'
    assert sent['id'] == 'fellgardsession1' and sent['summary'] == 'Fellgard: goal'

    # Already exists (e.g. it was deleted earlier): fall back to an update that revives it
    http.script = [409, 200]
    c.upsert_event('fellgardsession1', body())
    assert [m for m, *_ in http.calls[-2:]] == ['POST', 'PUT']
    assert http.calls[-1][1].endswith('/events/fellgardsession1') and http.calls[-1][2]['status'] == 'confirmed'

    # Deleting something already gone is fine; real failures raise
    for status in (204, 404, 410):
        http.script = [status]
        c.delete_event('fellgardsession1')
    http.script = [500]
    try:
        c.delete_event('fellgardsession1')
        raise AssertionError('500 accepted')
    except CalendarError as e:
        assert e.status == 500 and 'boom 500' in str(e)

    # Setup mistakes get a pointer to the fix
    for status in (403, 404):
        http.script = [status]
        try:
            c.check_access()
            raise AssertionError('error accepted')
        except CalendarError as e:
            assert e.status == status and 'bot@proj.iam.gserviceaccount.com' in str(e) and 'Make changes to events' in str(e)
    http.script = [403]
    try:
        c.upsert_event('x' * 5, body())
        raise AssertionError('403 accepted')
    except CalendarError as e:
        assert e.status == 403
    print('✓ Calendar requests')


def make_store(tmp=None):
    return ScheduleStore(':memory:')


def describe_with(tz=UK):
    def describe(s):
        start = datetime(s['day'].year, s['day'].month, s['day'].day, s['start_min'] // 60, s['start_min'] % 60, tzinfo=tz)
        end = start.replace(hour=s['end_min'] // 60 % 24)
        return build_event_body(f"Fellgard: {s['goal']}", s['goal'], 'Discord', start, end, 'Europe/London')
    return describe


def confirmed(store, name, day, goal, **kw):
    sid = store.create_session(hash(name) % 1000 + 1, name, day, h(19), h(22), goal, 5, max_open=99)
    store.transition(sid, ('proposed',), kw.get('status', 'confirmed'))
    return sid


def test_sync_reconciles():
    store, http = make_store(), FakeHTTP()
    c, describe = client(http), describe_with()

    # Nothing confirmed yet: nothing to do, no requests
    store.create_session(1, 'Sam', date(2026, 12, 12), h(19), h(22), 'just proposed', 5)
    r = sync_sessions(store, c, TODAY, describe)
    assert (r.pushed, r.deleted, r.ok) == (0, 0, True) and http.calls == []

    # A confirmed session is pushed once, and never again while unchanged
    a = confirmed(store, 'Sam', date(2026, 12, 12), 'Clear the cave')
    r = sync_sessions(store, c, TODAY, describe)
    assert r.pushed == 1 and len(http.calls) == 1 and http.calls[0][2]['id'] == event_id_for(a)
    http.calls.clear()
    assert sync_sessions(store, c, TODAY, describe).pushed == 0 and http.calls == []

    # Moving it pushes an update (same id), and only that one
    store.move_session(a, date(2026, 12, 13), h(18), h(21))
    r = sync_sessions(store, c, TODAY, describe)
    assert r.pushed == 1 and http.calls[-1][2]['start']['dateTime'].startswith('2026-12-13T18:00')
    assert http.calls[-1][2]['id'] == event_id_for(a)
    http.calls.clear()

    # Players joining or leaving doesn't touch the shared calendar
    store.join_session(a, 2, 'Bea')
    assert sync_sessions(store, c, TODAY, describe).pushed == 0 and http.calls == []

    # Cancelling removes it, once
    store.transition(a, ('confirmed',), 'cancelled')
    r = sync_sessions(store, c, TODAY, describe)
    assert r.deleted == 1 and http.calls[-1][0] == 'DELETE' and http.calls[-1][1].endswith(event_id_for(a))
    http.calls.clear()
    assert sync_sessions(store, c, TODAY, describe).deleted == 0 and http.calls == []

    # Cancelled or declined before it was ever pushed: nothing to remove
    b = confirmed(store, 'Bea', date(2026, 12, 14), 'never pushed')
    store.transition(b, ('confirmed',), 'cancelled')
    d = store.create_session(3, 'Cass', date(2026, 12, 15), h(19), h(22), 'declined', 5)
    store.transition(d, ('proposed',), 'declined')
    assert sync_sessions(store, c, TODAY, describe).pushed == 0 and http.calls == []
    print('✓ Sync reconciles pushes, updates and removals')


def test_sync_survives_failures():
    store, http = make_store(), FakeHTTP()
    c, describe = client(http), describe_with()
    a = confirmed(store, 'Sam', date(2026, 12, 12), 'first')
    b = confirmed(store, 'Bea', date(2026, 12, 13), 'second')

    http.script = [500]          # first request fails, the second succeeds
    r = sync_sessions(store, c, TODAY, describe)
    assert r.pushed == 1 and len(r.errors) == 1 and f'#{a}' in r.errors[0] and not r.ok
    assert store.get_session(a)['gcal_hash'] == '' and store.get_session(b)['gcal_hash'] != ''
    assert 'failed' in r.summary()

    # Next run retries only what failed
    http.calls.clear()
    r = sync_sessions(store, c, TODAY, describe)
    assert r.ok and r.pushed == 1 and len(http.calls) == 1 and http.calls[0][2]['id'] == event_id_for(a)

    # Network trouble (not an HTTP error) is also contained
    class Down(FakeHTTP):
        def post(self, url, **kw): raise ConnectionError('no route')
    store.move_session(a, date(2026, 12, 20), h(19), h(22))
    r = sync_sessions(store, client(Down()), TODAY, describe)
    assert len(r.errors) == 1 and 'ConnectionError' in r.errors[0]
    print('✓ Failures are contained and retried')


def test_sync_scope_and_privacy():
    store, http = make_store(), FakeHTTP()
    c, describe = client(http), describe_with()
    old = confirmed(store, 'Old', date(2026, 9, 1), 'long ago')                  # more than 30 days back
    late = confirmed(store, 'Late', date(2026, 11, 10), 'finished last week')
    store.transition(late, ('confirmed',), 'done')
    soon = confirmed(store, 'Sam', date(2026, 12, 12), 'Clear the cave')
    store.join_session(soon, 2, 'Beatrice')
    sync_sessions(store, c, TODAY, describe)
    pushed = {call[2]['id'] for call in http.calls}
    assert pushed == {event_id_for(late), event_id_for(soon)} and event_id_for(old) not in pushed   # recent finished ones count
    for _, _, sent in http.calls:
        text = str(sent)
        assert 'Sam' not in text and 'Beatrice' not in text and '<@' not in text      # no player names in the shared calendar
    print('✓ Sync scope and privacy')


def test_migration_adds_hash_column():
    s = ScheduleStore(':memory:')
    sid = s.create_session(1, 'Sam', date(2026, 12, 12), h(19), h(22), 'goal', 5)
    assert s.get_session(sid)['gcal_hash'] == ''
    s.set_gcal_hash(sid, 'abc')
    assert s.get_session(sid)['gcal_hash'] == 'abc'
    print('✓ Hash column')


def test_subscribe_links():
    url = 'https://calendar.google.com/calendar/ical/abc%40gmail.com/private-1234567890/basic.ics'
    assert cl.webcal_url(url) == 'webcal://calendar.google.com/calendar/ical/abc%40gmail.com/private-1234567890/basic.ics'
    assert cl.webcal_url('webcal://already') == 'webcal://already'
    g = cl.google_subscribe_url(url)
    assert g.startswith('https://calendar.google.com/calendar/r?cid=webcal%3A%2F%2Fcalendar.google.com')
    o = cl.outlook_subscribe_url(url, 'Tales from Fellgard · sessions')
    assert o.startswith('https://outlook.live.com/calendar/0/addfromweb?url=https%3A%2F%2Fcalendar.google.com')
    assert 'name=Tales+from+Fellgard' in o and len(g) < 512 and len(o) < 512
    print('✓ Subscribe links')


def run_tests():
    print('Running Google Calendar tests...\n')
    test_event_body_and_ids()
    test_client_requests()
    test_sync_reconciles()
    test_sync_survives_failures()
    test_sync_scope_and_privacy()
    test_migration_adds_hash_column()
    test_subscribe_links()
    print('\n✅ All tests passed!')


if __name__ == '__main__':
    run_tests()
