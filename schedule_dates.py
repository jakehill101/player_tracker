"""
Date parsing for the scheduler bot.

Turns what people type into Discord ("3-7 Dec", "12 Dec, 19 Dec", "December",
"28 Dec - 3 Jan", "2026-12-03", "3/12") into a list of dates.

Kept free of discord imports so it can be unit tested.
"""
import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, List, Optional

MONTHS = {}
for _i in range(1, 13):
    MONTHS[calendar.month_name[_i].lower()] = _i
    MONTHS[calendar.month_abbr[_i].lower()] = _i
MONTHS['sept'] = 9

MAX_DAYS = 120


class DateParseError(ValueError):
    """Raised when a date spec can't be understood. The message is user-facing."""


@dataclass
class _Partial:
    day: Optional[int] = None     # None = whole month
    month: Optional[int] = None   # None = bare day, inherits from a neighbour
    year: Optional[int] = None    # None = infer
    fixed: Optional[date] = None  # today / tomorrow / ISO


_SUFFIX = r'(?:st|nd|rd|th)?'
_PATTERNS = [
    ('iso', re.compile(r'^(\d{4})-(\d{1,2})-(\d{1,2})$')),
    ('dmy', re.compile(r'^(\d{1,2})/(\d{1,2})(?:/(\d{2}|\d{4}))?$')),
    ('day_month', re.compile(r'^(\d{1,2})' + _SUFFIX + r'\s+([a-z]+)\.?(?:\s+(\d{4}))?$')),
    ('month_day', re.compile(r'^([a-z]+)\.?\s+(\d{1,2})' + _SUFFIX + r'(?:,?\s+(\d{4}))?$')),
    ('month', re.compile(r'^([a-z]+)\.?(?:\s+(\d{4}))?$')),
    ('day', re.compile(r'^(\d{1,2})' + _SUFFIX + r'$')),
]


def _month(name: str) -> int:
    if name not in MONTHS:
        raise DateParseError(f"I don't know the month '{name}'.")
    return MONTHS[name]


def _parse_endpoint(text: str, today: date) -> _Partial:
    text = text.strip().lower()
    if text in ('today', 'tonight'):
        return _Partial(fixed=today)
    if text == 'tomorrow':
        return _Partial(fixed=today + timedelta(days=1))

    for kind, pattern in _PATTERNS:
        m = pattern.match(text)
        if not m:
            continue
        if kind == 'iso':
            try:
                return _Partial(fixed=date(int(m[1]), int(m[2]), int(m[3])))
            except ValueError:
                raise DateParseError(f"'{text}' isn't a real date.")
        if kind == 'dmy':
            year = int(m[3]) if m[3] else None
            if year is not None and year < 100:
                year += 2000
            return _Partial(day=int(m[1]), month=int(m[2]), year=year)
        if kind == 'day_month':
            return _Partial(day=int(m[1]), month=_month(m[2]), year=int(m[3]) if m[3] else None)
        if kind == 'month_day':
            return _Partial(day=int(m[2]), month=_month(m[1]), year=int(m[3]) if m[3] else None)
        if kind == 'month':
            return _Partial(month=_month(m[1]), year=int(m[2]) if m[2] else None)
        if kind == 'day':
            return _Partial(day=int(m[1]))
    raise DateParseError(
        f"I couldn't read '{text}'. Try something like '3 Dec', '3-7 Dec', 'December' or '2026-12-03'."
    )


def _last_day(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _make(year: int, month: int, day: int, original: str) -> date:
    if not 1 <= month <= 12:
        raise DateParseError(f"'{original}' has a month that doesn't exist.")
    try:
        return date(year, month, day)
    except ValueError:
        raise DateParseError(f"'{original}' isn't a real date.")


def _resolve(p: _Partial, today: date, role: str, original: str, min_year: Optional[int] = None) -> date:
    """Turn a partial into a concrete date.

    role: 'start' (a month means its 1st), 'end' (a month means its last day).
    Without a year, use this year unless that puts the whole thing in the past.
    """
    if p.fixed:
        return p.fixed
    if p.month is None:
        raise DateParseError(f"'{original}' needs a month, e.g. '{p.day} Dec'.")

    def build(year: int) -> date:
        if p.day is None:
            day = 1 if role == 'start' else _last_day(year, p.month)
        else:
            day = p.day
        return _make(year, p.month, day, original)

    if p.year is not None:
        return build(p.year)

    year = min_year if min_year is not None else today.year
    # Judge "in the past" on the last day the endpoint covers, so "November"
    # on 17 Nov still means this November.
    if p.day is None:
        last = _make(year, p.month, _last_day(year, p.month), original)
    else:
        last = build(year)
    if last < today:
        year += 1
    return build(year)


def _split_range(part: str) -> List[str]:
    if _PATTERNS[0][1].match(part):  # a single ISO date, not a range
        return [part]
    m = re.match(r'^(\d{4}-\d{1,2}-\d{1,2})\s*-\s*(\d{4}-\d{1,2}-\d{1,2})$', part)
    if m:
        return [m[1], m[2]]
    pieces = re.split(r'\s+(?:to|until|till)\s+|\s*[-–—]\s*', part)
    if len(pieces) > 2:
        raise DateParseError(f"'{part}' has too many dashes. Use one range per comma, e.g. '3-7 Dec, 12-14 Dec'.")
    return pieces


def parse_dates(spec: str, today: date, max_days: int = MAX_DAYS) -> List[date]:
    """Parse a date spec into a sorted list of unique dates.

    Accepts comma-separated parts (or ' and '). Each part is a single date,
    a whole month, or a range of either. Bare days borrow the month from the
    other end of their range, or from the next part that has one, so
    "3, 5, 12 Dec" means three dates in December.
    """
    if not spec or not spec.strip():
        raise DateParseError("Give me some dates, e.g. '3-7 Dec' or 'December'.")

    parts = [p.strip() for p in re.split(r',|;|\band\b', spec.lower()) if p.strip()]
    if not parts:
        raise DateParseError("Give me some dates, e.g. '3-7 Dec' or 'December'.")

    parsed = []  # list of (original, [partials])
    for part in parts:
        endpoints = [_parse_endpoint(e, today) for e in _split_range(part)]
        if len(endpoints) == 2:
            a, b = endpoints
            # "3-7 Dec": a bare day borrows the month (and year) from the other end
            if a.month is None and not a.fixed and b.month is not None:
                a.month, a.year = b.month, b.year
            if b.month is None and not b.fixed and a.month is not None:
                b.month, b.year = a.month, a.year
            if a.month is None and not a.fixed and b.fixed:
                a.month, a.year = b.fixed.month, b.fixed.year
            if b.month is None and not b.fixed and a.fixed:
                b.month, b.year = a.fixed.month, a.fixed.year
        parsed.append((part, endpoints))

    # Bare single days ("3, 5, 12 Dec") borrow from the next part with a month,
    # otherwise the previous one.
    def month_of(endpoints):
        for e in endpoints:
            if e.fixed:
                return e.fixed.month, e.fixed.year
            if e.month is not None:
                return e.month, e.year
        return None

    for i, (part, endpoints) in enumerate(parsed):
        for e in endpoints:
            if e.month is None and not e.fixed:
                donor = None
                for _, later in parsed[i + 1:]:
                    donor = month_of(later)
                    if donor:
                        break
                if not donor:
                    for _, earlier in reversed(parsed[:i]):
                        donor = month_of(earlier)
                        if donor:
                            break
                if donor:
                    e.month, e.year = donor

    days = set()
    for part, endpoints in parsed:
        if len(endpoints) == 1:
            start = _resolve(endpoints[0], today, 'start', part)
            end = _resolve(endpoints[0], today, 'end', part, min_year=start.year)
        else:
            start = _resolve(endpoints[0], today, 'start', part)
            end = _resolve(endpoints[1], today, 'end', part, min_year=start.year)
            if end < start and endpoints[1].year is None and not endpoints[1].fixed:
                end = _resolve(endpoints[1], today, 'end', part, min_year=start.year + 1)
            if end < start:
                raise DateParseError(f"'{part}' ends before it starts.")
        if (end - start).days + 1 > max_days:
            raise DateParseError(f"'{part}' covers more than {max_days} days. Split it up a bit.")
        d = start
        while d <= end:
            days.add(d)
            d += timedelta(days=1)
        if len(days) > max_days:
            raise DateParseError(f"That's more than {max_days} days in one go. Split it up a bit.")

    return sorted(days)


def parse_month(spec: Optional[str], today: date):
    """Parse a month for /calendar. Empty means the current month. Returns (year, month)."""
    if not spec or not spec.strip():
        return today.year, today.month
    text = spec.strip().lower()
    m = re.match(r'^(\d{4})-(\d{1,2})$', text)
    if m:
        month = int(m[2])
        if not 1 <= month <= 12:
            raise DateParseError(f"'{spec}' has a month that doesn't exist.")
        return int(m[1]), month
    if text in ('next', 'next month'):
        return (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    p = _parse_endpoint(text, today)
    d = _resolve(p, today, 'start', text)
    return d.year, d.month


def format_days(days: Iterable[date]) -> str:
    """Compress dates into a short readable list: '3–7 Dec, 12 Dec, 30 Dec–2 Jan 2027'."""
    days = sorted(set(days))
    if not days:
        return 'no dates'
    runs = []
    start = prev = days[0]
    for d in days[1:]:
        if d - prev == timedelta(days=1):
            prev = d
            continue
        runs.append((start, prev))
        start = prev = d
    runs.append((start, prev))

    this_year = days[0].year

    def fmt(d: date, with_month: bool = True) -> str:
        s = str(d.day)
        if with_month:
            s += ' ' + calendar.month_abbr[d.month]
            if d.year != this_year:
                s += f' {d.year}'
        return s

    out = []
    for a, b in runs:
        if a == b:
            out.append(fmt(a))
        elif (a.year, a.month) == (b.year, b.month):
            out.append(f'{a.day}–{fmt(b)}')
        else:
            out.append(f'{fmt(a)}–{fmt(b)}')
    return ', '.join(out)
