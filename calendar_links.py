"""
Calendar files and "add to calendar" links for confirmed sessions.

Builds standard .ics text (RFC 5545) and the web links Google and Outlook
accept, so a session can be added to anyone's calendar with no server.

Kept free of discord imports so it can be unit tested.
"""
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, List, Optional
from urllib.parse import urlencode

DISCORD_URL_LIMIT = 512  # Discord rejects link buttons longer than this


def to_utc(day: date, minute: int, tz) -> datetime:
    """Local day + minutes since midnight (1440 = end of day) -> aware UTC datetime.

    The wall-clock time is attached to the zone, so BST/GMT changes are handled.
    """
    if minute >= 1440:
        local = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=tz)
    else:
        local = datetime.combine(day, time(minute // 60, minute % 60), tzinfo=tz)
    return local.astimezone(timezone.utc)


def _stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def ics_escape(text: str) -> str:
    text = (text or '').replace('\r\n', '\n').replace('\r', '\n')
    text = ''.join(ch for ch in text if ch == '\n' or ch >= ' ')
    return text.replace('\\', '\\\\').replace(';', '\\;').replace(',', '\\,').replace('\n', '\\n')


def fold(line: str) -> str:
    """Fold a content line at 75 octets, never splitting a character."""
    if len(line.encode('utf-8')) <= 75:
        return line
    parts: List[str] = []
    cur, size, limit = '', 0, 75
    for ch in line:
        n = len(ch.encode('utf-8'))
        if size + n > limit:
            parts.append(cur)
            cur, size, limit = ch, n, 74  # continuation lines start with a space
        else:
            cur += ch
            size += n
    parts.append(cur)
    return '\r\n '.join(parts)


def ics_event(uid: str, summary: str, start: datetime, end: datetime, description: str = '',
              location: str = '', status: str = 'CONFIRMED', sequence: int = 0, url: str = '',
              stamp: Optional[datetime] = None) -> List[str]:
    lines = [
        'BEGIN:VEVENT',
        f'UID:{uid}',
        f'DTSTAMP:{_stamp(stamp or datetime.now(timezone.utc))}',
        f'DTSTART:{_stamp(start)}',
        f'DTEND:{_stamp(end)}',
        f'SUMMARY:{ics_escape(summary)}',
        f'STATUS:{status}',
        f'SEQUENCE:{int(sequence)}',
    ]
    if description:
        lines.append(f'DESCRIPTION:{ics_escape(description)}')
    if location:
        lines.append(f'LOCATION:{ics_escape(location)}')
    if url:
        lines.append(f'URL:{url}')
    lines.append('END:VEVENT')
    return lines


def ics_calendar(events: Iterable[List[str]], name: str = 'Sessions',
                 prodid: str = '-//player_tracker//scheduler//EN') -> bytes:
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', f'PRODID:{prodid}', 'CALSCALE:GREGORIAN',
             'METHOD:PUBLISH', f'X-WR-CALNAME:{ics_escape(name)}']
    for ev in events:
        lines.extend(ev)
    lines.append('END:VCALENDAR')
    return ('\r\n'.join(fold(line) for line in lines) + '\r\n').encode('utf-8')


def _fit_url(base: str, params: dict, droppable: Iterable[str]) -> str:
    """Build base?params, dropping optional parts until it fits a Discord button."""
    params = dict(params)
    url = f'{base}?{urlencode(params)}'
    for key in droppable:
        if len(url) <= DISCORD_URL_LIMIT:
            break
        params.pop(key, None)
        url = f'{base}?{urlencode(params)}'
    return url


def google_url(title: str, start: datetime, end: datetime, details: str = '', location: str = '') -> str:
    params = {'action': 'TEMPLATE', 'text': title, 'dates': f'{_stamp(start)}/{_stamp(end)}',
              'details': details, 'location': location}
    return _fit_url('https://calendar.google.com/calendar/render', params, ['details', 'location'])


def outlook_url(title: str, start: datetime, end: datetime, details: str = '', location: str = '') -> str:
    iso = lambda dt: dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    params = {'path': '/calendar/action/compose', 'rru': 'addevent', 'subject': title,
              'startdt': iso(start), 'enddt': iso(end), 'body': details, 'location': location}
    return _fit_url('https://outlook.live.com/calendar/0/deeplink/compose', params, ['body', 'location'])


def webcal_url(ical_url: str) -> str:
    """The same feed with the webcal:// scheme, which Apple Calendar and others open directly."""
    for prefix in ('https://', 'http://'):
        if ical_url.startswith(prefix):
            return 'webcal://' + ical_url[len(prefix):]
    return ical_url


def google_subscribe_url(ical_url: str) -> str:
    """Opens Google Calendar's 'add calendar from URL' with the feed filled in."""
    return 'https://calendar.google.com/calendar/r?' + urlencode({'cid': webcal_url(ical_url)})


def outlook_subscribe_url(ical_url: str, name: str = 'Sessions') -> str:
    """Opens Outlook's 'subscribe from web' with the feed filled in."""
    return 'https://outlook.live.com/calendar/0/addfromweb?' + urlencode({'url': ical_url, 'name': name})
