"""
Keeps a shared Google Calendar in step with the confirmed sessions.

The bot writes to one dedicated calendar (shared with the same service account
the Google Sheet uses). Players subscribe to that calendar from Google, Outlook
or Apple, so confirmed sessions show up in their own calendar and update by
themselves.

How it stays correct: instead of reacting to each change, `sync_sessions`
compares what *should* be on the calendar with what was last pushed (a hash of
each event is remembered per session) and fixes the difference. So a failed
push, a restart, or a change made while Google was down all repair themselves
on the next run.

Events carry the title, time, goal and a Discord link. No player names.

Talks to Google's REST API directly with the credentials the bot already has,
so there is nothing extra to install. Kept free of discord imports so it can
be unit tested.
"""
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List
from urllib.parse import quote

API = 'https://www.googleapis.com/calendar/v3'
SCOPES = ['https://www.googleapis.com/auth/calendar']
TIMEOUT = 20
LOOKBACK_DAYS = 30   # only sessions this recent are checked on each run


class CalendarError(Exception):
    """A Google Calendar request failed. `status` is the HTTP status (0 if none)."""

    def __init__(self, status: int, reason: str):
        super().__init__(f'{status}: {reason}' if status else reason)
        self.status, self.reason = status, reason


def event_id_for(session_id: int) -> str:
    """A stable Google event id. Google only allows the characters 0-9 and a-v."""
    return f'fellgardsession{int(session_id)}'


def build_event_body(title: str, description: str, location: str, start: datetime, end: datetime,
                     tz_name: str) -> Dict:
    return {
        'summary': title,
        'description': description,
        'location': location,
        'start': {'dateTime': start.isoformat(), 'timeZone': tz_name},
        'end': {'dateTime': end.isoformat(), 'timeZone': tz_name},
        # Subscribers' own calendar apps decide their reminders; don't add the owner's defaults
        'reminders': {'useDefault': False},
    }


def body_hash(body: Dict) -> str:
    return hashlib.sha1(json.dumps(body, sort_keys=True).encode('utf-8')).hexdigest()[:16]


class GoogleCalendarClient:
    def __init__(self, calendar_id: str, credentials_file: str = 'credentials.json', http=None):
        self.calendar_id = calendar_id
        self._base = f'{API}/calendars/{quote(calendar_id, safe="")}'
        self.service_account_email = ''
        if http is None:
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2 import service_account
            creds = service_account.Credentials.from_service_account_file(credentials_file, scopes=SCOPES)
            self.service_account_email = creds.service_account_email
            http = AuthorizedSession(creds)
        self.http = http

    def _fail(self, resp) -> CalendarError:
        try:
            reason = resp.json()['error']['message']
        except Exception:
            reason = (getattr(resp, 'text', '') or 'no details')[:200]
        if resp.status_code in (403, 404):
            who = self.service_account_email or 'the service account'
            reason += (f" (is the calendar shared with {who} with 'Make changes to events', "
                       "and is the Google Calendar API turned on for the project?)")
        return CalendarError(resp.status_code, reason)

    def check_access(self) -> None:
        """Raises CalendarError if the calendar can't be read."""
        resp = self.http.get(self._base, timeout=TIMEOUT)
        if resp.status_code != 200:
            raise self._fail(resp)

    def upsert_event(self, event_id: str, body: Dict) -> None:
        """Create the event, or update it if it already exists (even if it was deleted before)."""
        resp = self.http.post(f'{self._base}/events', json={**body, 'id': event_id}, timeout=TIMEOUT)
        if resp.status_code == 409:
            resp = self.http.put(f'{self._base}/events/{event_id}',
                                 json={**body, 'id': event_id, 'status': 'confirmed'}, timeout=TIMEOUT)
        if resp.status_code not in (200, 201):
            raise self._fail(resp)

    def delete_event(self, event_id: str) -> None:
        """Delete the event. Already gone counts as success."""
        resp = self.http.delete(f'{self._base}/events/{event_id}', timeout=TIMEOUT)
        if resp.status_code in (200, 204, 404, 410):
            return
        raise self._fail(resp)


@dataclass
class SyncResult:
    pushed: int = 0
    deleted: int = 0
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        parts = [f'{self.pushed} added or updated', f'{self.deleted} removed']
        return ', '.join(parts) + (f'; {len(self.errors)} failed' if self.errors else '')


def sync_sessions(store, client: GoogleCalendarClient, today: date,
                  describe: Callable[[dict], Dict]) -> SyncResult:
    """Make the calendar match the store.

    Confirmed (and finished) sessions are pushed when new or changed; cancelled ones
    that were pushed are removed. `describe(session)` returns the event body.
    One failing session doesn't stop the others; it is retried next time.
    """
    result = SyncResult()
    since = today - timedelta(days=LOOKBACK_DAYS)
    for s in store.list_sessions(('confirmed', 'done', 'cancelled'), since):
        sid = s['id']
        try:
            if s['status'] in ('confirmed', 'done'):
                body = describe(s)
                digest = body_hash(body)
                if s['gcal_hash'] != digest:
                    client.upsert_event(event_id_for(sid), body)
                    store.set_gcal_hash(sid, digest)
                    result.pushed += 1
            elif s['gcal_hash'] not in ('', 'deleted'):
                client.delete_event(event_id_for(sid))
                store.set_gcal_hash(sid, 'deleted')
                result.deleted += 1
        except CalendarError as e:
            result.errors.append(f'session #{sid}: {e}')
        except Exception as e:   # network trouble etc.
            result.errors.append(f'session #{sid}: {type(e).__name__}: {e}')
    return result
