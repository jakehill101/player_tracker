"""
When reminders are due. Pure scheduling logic, kept free of discord imports so it
can be unit tested; scheduler_sessions.py does the sending.

Three kinds of reminder for a confirmed session:
  * a DM to each person in the party, a set number of hours before (default 24 and 1)
  * a post in the channel on the morning of the session day, pinging the party
  * a daily nudge to the GM about proposals that have been waiting a while

A reminder is "due" from its moment until the next reminder's moment (the 24-hour
reminder stays due until the 1-hour one starts), so a bot that was briefly offline
still sends it, but never after something more urgent has taken over. Each one is
recorded per person once sent, so a restart never repeats it, and someone who joins
late still gets the reminders they haven't had.
"""
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Iterable, List, Optional, Set, Tuple


@dataclass(frozen=True)
class Due:
    session_id: int
    kind: str       # 'dm24', 'dm1', 'day'
    user_id: str    # '' for the channel post


def parse_hhmm(text) -> Optional[time]:
    """'09:00' -> time(9, 0). Empty / None / False turns the feature off (returns None)."""
    if text in (None, '', False):
        return None
    try:
        hours, minutes = str(text).strip().split(':')
        return time(int(hours), int(minutes))
    except ValueError:
        raise ValueError(f"'{text}' isn't a time like '09:00'")


def parse_hours(values) -> List[float]:
    """[24, 1] -> [24.0, 1.0]: positive, unique, longest lead first. Empty turns DMs off."""
    out = sorted({float(v) for v in (values or [])}, reverse=True)
    if any(v <= 0 for v in out):
        raise ValueError('reminder_hours_before must be positive numbers')
    return out


def dm_kind(hours: float) -> str:
    return f'dm{hours:g}'


def start_utc(s: dict, tz) -> datetime:
    local = datetime.combine(s['day'], time(s['start_min'] // 60, s['start_min'] % 60), tzinfo=tz)
    return local.astimezone(timezone.utc)


def _confirmed_by(s: dict, moment: datetime) -> bool:
    """True if the session was already confirmed at `moment`. Unknown (older rows) counts as yes."""
    stamp = s.get('confirmed_at') or ''
    return not stamp or datetime.fromisoformat(stamp) <= moment


def dm_windows(s: dict, tz, hours_before: List[float]) -> List[Tuple[float, datetime, datetime]]:
    """[(lead_hours, opens, closes)]: each DM is due from `opens` until the next one opens."""
    start = start_utc(s, tz)
    out = []
    for i, lead in enumerate(hours_before):
        nxt = hours_before[i + 1] if i + 1 < len(hours_before) else 0
        out.append((lead, start - timedelta(hours=lead), start - timedelta(hours=nxt)))
    return out


def day_post_moment(s: dict, tz, day_post_time: Optional[time]) -> Optional[datetime]:
    """When the on-the-day channel post goes out, or None if it's off or would be too close to the start."""
    if day_post_time is None:
        return None
    moment = datetime.combine(s['day'], day_post_time, tzinfo=tz).astimezone(timezone.utc)
    return moment if moment < start_utc(s, tz) - timedelta(hours=1) else None


def due_reminders(sessions: Iterable[dict], sent: Set[Tuple[int, str, str]], now: datetime, tz,
                  hours_before: List[float], day_post_time: Optional[time]) -> List[Due]:
    """Everything that should be sent right now. `sent` holds (session_id, kind, user_id) already done."""
    now = now.astimezone(timezone.utc)
    due: List[Due] = []
    for s in sessions:
        if s['status'] != 'confirmed':
            continue
        start = start_utc(s, tz)
        if start <= now:
            continue
        for lead, opens, closes in dm_windows(s, tz, hours_before):
            if not (opens <= now < closes) or not _confirmed_by(s, opens):
                continue
            kind = dm_kind(lead)
            for uid, _ in s['players']:
                if (s['id'], kind, uid) not in sent:
                    due.append(Due(s['id'], kind, uid))
        post = day_post_moment(s, tz, day_post_time)
        if (post and post <= now < start - timedelta(hours=1) and _confirmed_by(s, post)
                and (s['id'], 'day', '') not in sent):
            due.append(Due(s['id'], 'day', ''))
    return due


def reminder_plan(s: dict, sent: Set[Tuple[int, str, str]], now: datetime, tz, hours_before: List[float],
                  day_post_time: Optional[time]) -> List[Tuple[str, datetime, str]]:
    """What is scheduled for one session, for the GM: [(label, when (local), state)]."""
    now = now.astimezone(timezone.utc)
    plan = []
    people = [uid for uid, _ in s['players']]
    for lead, opens, closes in dm_windows(s, tz, hours_before):
        kind = dm_kind(lead)
        done = sum(1 for uid in people if (s['id'], kind, uid) in sent)
        if not _confirmed_by(s, opens):
            state = 'skipped (the session was confirmed after this)'
        elif done == len(people) and people:
            state = f'sent to {done}'
        elif now >= closes:
            state = 'missed' if not done else f'sent to {done} of {len(people)}'
        elif now >= opens:
            state = f'due now ({done} of {len(people)} sent)'
        else:
            state = 'pending'
        plan.append((f'DM {lead:g}h before', opens.astimezone(tz), state))
    post = day_post_moment(s, tz, day_post_time)
    if post:
        if (s['id'], 'day', '') in sent:
            state = 'sent'
        elif not _confirmed_by(s, post):
            state = 'skipped (the session was confirmed after this)'
        elif now >= start_utc(s, tz) - timedelta(hours=1):
            state = 'missed'
        else:
            state = 'due now' if now >= post else 'pending'
        plan.append(('Channel post on the day', post.astimezone(tz), state))
    return plan


def waiting_proposals(sessions: Iterable[dict], now: datetime, tz, after_hours: float) -> List[dict]:
    """Proposals still waiting for the GM that have been waiting at least `after_hours`."""
    now = now.astimezone(timezone.utc)
    cutoff = now - timedelta(hours=after_hours)
    return [s for s in sessions
            if s['status'] == 'proposed' and start_utc(s, tz) > now
            and datetime.fromisoformat(s['created_at']) <= cutoff]


def digest_due(now: datetime, digest_time: Optional[time], last_date: Optional[str]) -> bool:
    """True once per day, after `digest_time` (local), if it hasn't been sent today."""
    if digest_time is None:
        return False
    return now.timetz().replace(tzinfo=None) >= digest_time and last_date != now.date().isoformat()
