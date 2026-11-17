"""
Time-of-day windows for the scheduler bot.

A window is (start_minute, end_minute) within one day: (0, 1440) is all day,
(1140, 1440) is "from 19:00". Everything here works in the campaign's local
time (the timezone in scheduler_config.yaml).

Parses what people type ("after 7pm", "7-10pm", "19:00-22:00", "evenings"),
combines windows, and works out when the GM and players overlap.

Kept free of discord imports so it can be unit tested.
"""
import re
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Tuple

Window = Tuple[int, int]

DAY: Window = (0, 1440)
MAX_WINDOWS = 6

PRESETS: Dict[str, Window] = {
    'all day': DAY, 'allday': DAY, 'all-day': DAY, 'any': DAY, 'anytime': DAY, 'any time': DAY,
    'day': DAY, 'whole day': DAY, 'all': DAY,
    'morning': (6 * 60, 12 * 60), 'mornings': (6 * 60, 12 * 60),
    'afternoon': (12 * 60, 17 * 60), 'afternoons': (12 * 60, 17 * 60),
    'evening': (18 * 60, 1440), 'evenings': (18 * 60, 1440),
}

_EXAMPLES = "Try '7pm-10pm', 'after 7pm', 'evenings', or leave the time empty for all day."


class TimeParseError(ValueError):
    """Raised when a time spec can't be understood. The message is user-facing."""


# ---------------------------------------------------------------- algebra

def merge(windows: Iterable[Window]) -> List[Window]:
    """Sort and join overlapping or touching windows."""
    out: List[Window] = []
    for s, e in sorted(w for w in windows if w[0] < w[1]):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def subtract(a: Iterable[Window], b: Iterable[Window]) -> List[Window]:
    """Parts of `a` not covered by `b`."""
    result = merge(a)
    for bs, be in merge(b):
        trimmed = []
        for s, e in result:
            if be <= s or bs >= e:
                trimmed.append((s, e))
                continue
            if s < bs:
                trimmed.append((s, bs))
            if be < e:
                trimmed.append((be, e))
        result = trimmed
    return result


def is_all_day(windows: Iterable[Window]) -> bool:
    return merge(windows) == [DAY]


# ---------------------------------------------------------------- display

def hhmm(minute: int) -> str:
    return f'{minute // 60:02d}:{minute % 60:02d}'


def format_window(w: Window) -> str:
    s, e = w
    if (s, e) == DAY:
        return 'all day'
    if s == 0:
        return f'until {hhmm(e)}'
    if e == 1440:
        return f'from {hhmm(s)}'
    return f'{hhmm(s)}–{hhmm(e)}'


def format_windows(windows: Iterable[Window]) -> str:
    ws = merge(windows)
    return ', '.join(format_window(w) for w in ws) if ws else 'no times'


# ---------------------------------------------------------------- parsing

_TIME_RE = re.compile(
    r'^(?:(?P<word>noon|midnight)'
    r'|(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<mer>am|pm|a\.m\.|p\.m\.)?'
    r'|(?P<four>\d{4}))$'
)


class _T:
    """One parsed clock time, before am/pm is settled."""
    def __init__(self, text, hour=None, minute=0, mer=None, colon=False, word=None):
        self.text, self.hour, self.minute, self.mer, self.colon, self.word = text, hour, minute, mer, colon, word

    @property
    def needs_meridiem(self) -> bool:
        """Bare 1-12 with nothing saying am or pm."""
        return self.word is None and self.mer is None and self.hour is not None and 1 <= self.hour <= 12


def _read(text: str) -> _T:
    text = text.strip()
    m = _TIME_RE.match(text)
    if not m:
        raise TimeParseError(f"I couldn't read the time '{text}'. {_EXAMPLES}")
    if m['word']:
        return _T(text, word=m['word'])
    if m['four']:
        hour, minute, colon, mer = int(m['four'][:2]), int(m['four'][2:]), True, None
    else:
        hour, minute, colon = int(m['h']), int(m['m'] or 0), m['m'] is not None
        mer = m['mer'][0] if m['mer'] else None  # 'a' or 'p'
    if minute > 59:
        raise TimeParseError(f"'{text}' isn't a real time.")
    if mer and not 1 <= hour <= 12:
        raise TimeParseError(f"'{text}' isn't a real time (am/pm times run 1-12).")
    if hour > 24 or (hour == 24 and minute):
        raise TimeParseError(f"'{text}' isn't a real time.")
    return _T(text, hour, minute, mer, colon)


def _minutes(t: _T, mer: Optional[str] = None) -> int:
    """Minutes since midnight. `mer` fills in a missing am/pm ('a' or 'p')."""
    if t.word == 'noon':
        return 12 * 60
    if t.word == 'midnight':
        return 0
    mer = t.mer or mer
    if mer:
        return (t.hour % 12 + (12 if mer == 'p' else 0)) * 60 + t.minute
    return t.hour * 60 + t.minute  # 24-hour


def _single(t: _T, role: str) -> int:
    """A time that stands alone ('after 7pm'): it has to be unambiguous."""
    if t.needs_meridiem and not t.colon:
        raise TimeParseError(f"Is '{t.text}' am or pm? Say '{t.text}am' or '{t.text}pm', or use 24-hour time.")
    return _minutes(t)


def _range(a_text: str, b_text: str) -> Window:
    a, b = _read(a_text), _read(b_text)
    a_mer = a.mer or b.mer if a.needs_meridiem else None
    b_mer = b.mer or a.mer if b.needs_meridiem else None
    if a.needs_meridiem and b.needs_meridiem and not (a.mer or b.mer):
        # Neither end says am/pm. Colon times ("19:00", "07:00") read as 24-hour; bare ones are unclear.
        if not (a.colon and b.colon):
            raise TimeParseError(
                f"Is '{a_text}-{b_text}' am or pm? Say '{a_text}pm-{b_text}pm' or use 24-hour time like 19:00-22:00."
            )
        a_mer = b_mer = None

    start, end = _minutes(a, a_mer), _minutes(b, b_mer)
    if end == 0 and start > 0:
        end = 1440  # "7pm-midnight", "7pm-12am"
    if start >= end:
        # "11-1pm" read 11pm-1pm; "9am-5" read 9am-5am: try the other half of the day
        if a_mer and not a.mer:
            start = _minutes(a, 'a' if a_mer == 'p' else 'p')
        elif b_mer and not b.mer:
            end = _minutes(b, 'a' if b_mer == 'p' else 'p')
    if start >= end:
        raise TimeParseError(
            f"'{a_text}-{b_text}' ends before it starts. For a time past midnight, set the next day separately."
        )
    return start, end


_SEP = re.compile(r'\s*(?:-|–|—|\bto\b|\buntil\b|\btill\b|\band\b)\s*')


def _parse_part(part: str) -> Window:
    if part in PRESETS:
        return PRESETS[part]
    body = re.sub(r'^(?:from|between)\s+', '', part)
    pieces = _SEP.split(body)
    if len(pieces) > 2:
        raise TimeParseError(f"'{part}' has too many times. Use one range per comma, e.g. '12-2pm, 7-10pm'.")
    if len(pieces) == 2 and all(pieces):
        return _range(pieces[0], pieces[1])

    m = re.fullmatch(r'(?:after|since|from)\s+(.+?)(?:\s*(?:onwards?|\+))?', part)
    if m:
        start = _single(_read(m[1]), 'start')
        return (start, 1440) if 0 < start < 1440 else _bad_edge(part)
    m = re.fullmatch(r'(?:before|until|till|by)\s+(.+)', part)
    if m:
        end = _single(_read(m[1]), 'end')
        if end == 0:
            end = 1440
        return (0, end) if end > 0 else _bad_edge(part)
    m = re.fullmatch(r'(.+?)\s*(?:onwards?|\+)', part)
    if m:
        start = _single(_read(m[1]), 'start')
        return (start, 1440) if 0 < start < 1440 else _bad_edge(part)
    try:
        _read(part)
    except TimeParseError:
        raise TimeParseError(f"I couldn't read the time '{part}'. {_EXAMPLES}")
    raise TimeParseError(
        f"'{part}' is a single time. Give a range ('{part}-10pm') or say 'after {part}' or 'before {part}'."
    )


def _bad_edge(part: str):
    raise TimeParseError(f"'{part}' leaves no time in the day. {_EXAMPLES}")


def parse_times(spec: Optional[str]) -> List[Window]:
    """Parse a time spec into windows. Empty means all day.

    Formats: 'after 7pm', 'before 5pm', '7pm-10pm', '7-10pm', '19:00-22:00',
    'morning', 'afternoon', 'evenings', 'all day', or several joined by commas
    ('12-2pm, 7-10pm'). Times without am/pm and with a colon are 24-hour.
    """
    if spec is None or not spec.strip():
        return [DAY]
    parts = [p.strip() for p in re.split(r'[,;]', spec.lower()) if p.strip()]
    if not parts:
        return [DAY]
    if len(parts) > MAX_WINDOWS:
        raise TimeParseError(f'That is more than {MAX_WINDOWS} time ranges for one day.')
    return merge(_parse_part(re.sub(r'\s+', ' ', p)) for p in parts)


# -------------------------------------------------- summaries for display

def gm_day_summary(rows) -> Optional[Tuple[str, List[Window], str]]:
    """Net GM status for a day from [(state, start, end, note)] rows.

    Returns ('open' | 'blocked', windows, note), or None if nothing is set.
    Blocked time is cut out of open time.
    """
    open_ = merge((s, e) for st, s, e, _ in rows if st == 'open')
    blocked = merge((s, e) for st, s, e, _ in rows if st == 'blocked')

    def note(state):
        return next((n for st, _, _, n in rows if st == state and n), '')

    net = subtract(open_, blocked)
    if net:
        return 'open', net, note('open')
    if blocked:
        return 'blocked', blocked, note('blocked')
    return None


def player_day_summary(rows) -> Tuple[List[Window], List[Window]]:
    """(free windows, busy windows) for one player's [(state, start, end)] rows.

    Busy time is cut out of free time.
    """
    free = merge((s, e) for st, s, e in rows if st == 'free')
    busy = merge((s, e) for st, s, e in rows if st == 'busy')
    return subtract(free, busy), busy


def gm_calendar_days(raw) -> Dict:
    """{date: [(state, s, e, note)]} -> {date: (state, windows, note)} for drawing."""
    out = {}
    for day, rows in raw.items():
        summary = gm_day_summary(rows)
        if summary:
            out[day] = summary
    return out


def player_calendar_days(raw) -> Dict:
    """{date: {user_id: (name, rows)}} -> {date: {'free': [names], 'busy': [names]}} for drawing."""
    out = {}
    for day, people in raw.items():
        free, busy = [], []
        for name, rows in people.values():
            f, b = player_day_summary(rows)
            if f:
                free.append(name)
            elif b:
                busy.append(name)
        if free or busy:
            key = lambda n: n.lower()
            out[day] = {'free': sorted(free, key=key), 'busy': sorted(busy, key=key)}
    return out


def best_slots(gm_windows: Iterable[Window], players: Dict[str, List[Window]]) -> List[Tuple[int, int, List[str]]]:
    """When the GM is free and at least one player is, as [(start, end, [names])].

    `players` maps name -> that player's free windows. Adjacent stretches with
    the same people are joined, so you get "19:00-22:00: Sam, Bea" not three rows.
    """
    gm = merge(gm_windows)
    free = {name: merge(ws) for name, ws in players.items()}
    points = sorted({p for w in gm for p in w} | {p for ws in free.values() for w in ws for p in w})

    def covers(ws, a, b):
        return any(s <= a and b <= e for s, e in ws)

    slots: List[Tuple[int, int, List[str]]] = []
    for a, b in zip(points, points[1:]):
        if not covers(gm, a, b):
            continue
        names = sorted((n for n, ws in free.items() if covers(ws, a, b)), key=str.lower)
        if not names:
            continue
        if slots and slots[-1][1] == a and slots[-1][2] == names:
            slots[-1] = (slots[-1][0], b, names)
        else:
            slots.append((a, b, names))
    return slots


def group_days_by_windows(per_day: Dict) -> Dict[str, List]:
    """{date: [windows]} -> {'from 19:00': [dates], 'all day': [dates]}, in first-seen order."""
    groups = defaultdict(list)
    for day in sorted(per_day):
        groups[format_windows(per_day[day])].append(day)
    return dict(groups)
