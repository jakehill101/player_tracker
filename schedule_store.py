"""
SQLite storage for the scheduler bot.

The scheduler keeps its own small database instead of Google Sheets: several
people can click at once, and SQLite gives real transactions, so two writes
never overwrite each other. One file on the VM, backed up nightly.

Kept free of discord imports so it can be unit tested. All methods are
synchronous; the bot calls them through asyncio.to_thread.
"""
import os
import sqlite3
import threading
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from schedule_times import DAY, Window, merge, subtract

GM_STATES = ('open', 'blocked')
PLAYER_STATES = ('free', 'busy')
SESSION_LIVE = ('proposed', 'countered', 'confirmed')
SESSION_OPEN = ('proposed', 'countered')   # waiting on the GM or the proposer
GOAL_MAX = 100


class ProposalLimitError(Exception):
    """The proposer already has the maximum number of open proposals."""

    def __init__(self, limit: int):
        super().__init__(f'limit {limit}')
        self.limit = limit

# Each entry upgrades the schema by one version. Append only; never edit a
# migration that has shipped.
MIGRATIONS = [
    # 1: availability
    """
    CREATE TABLE gm_availability (
        day        TEXT PRIMARY KEY,
        state      TEXT NOT NULL CHECK (state IN ('open', 'blocked')),
        note       TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL
    );
    CREATE TABLE player_availability (
        user_id      TEXT NOT NULL,
        day          TEXT NOT NULL,
        free         INTEGER NOT NULL CHECK (free IN (0, 1)),
        display_name TEXT NOT NULL DEFAULT '',
        updated_at   TEXT NOT NULL,
        PRIMARY KEY (user_id, day)
    );
    CREATE INDEX player_availability_day ON player_availability (day);
    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    # 2: times of day. Existing whole-day rows become all-day windows.
    """
    CREATE TABLE gm_availability_v2 (
        day        TEXT NOT NULL,
        state      TEXT NOT NULL CHECK (state IN ('open', 'blocked')),
        start_min  INTEGER NOT NULL CHECK (start_min >= 0 AND start_min < 1440),
        end_min    INTEGER NOT NULL CHECK (end_min > start_min AND end_min <= 1440),
        note       TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL,
        PRIMARY KEY (day, state, start_min)
    );
    INSERT INTO gm_availability_v2 (day, state, start_min, end_min, note, updated_at)
        SELECT day, state, 0, 1440, note, updated_at FROM gm_availability;
    DROP TABLE gm_availability;
    ALTER TABLE gm_availability_v2 RENAME TO gm_availability;

    CREATE TABLE player_availability_v2 (
        user_id      TEXT NOT NULL,
        day          TEXT NOT NULL,
        state        TEXT NOT NULL CHECK (state IN ('free', 'busy')),
        start_min    INTEGER NOT NULL CHECK (start_min >= 0 AND start_min < 1440),
        end_min      INTEGER NOT NULL CHECK (end_min > start_min AND end_min <= 1440),
        display_name TEXT NOT NULL DEFAULT '',
        updated_at   TEXT NOT NULL,
        PRIMARY KEY (user_id, day, state, start_min)
    );
    INSERT INTO player_availability_v2 (user_id, day, state, start_min, end_min, display_name, updated_at)
        SELECT user_id, day, CASE free WHEN 1 THEN 'free' ELSE 'busy' END, 0, 1440, display_name, updated_at
        FROM player_availability;
    DROP TABLE player_availability;
    ALTER TABLE player_availability_v2 RENAME TO player_availability;
    CREATE INDEX player_availability_day ON player_availability (day);
    """,
    # 3: sessions (proposals and confirmed sessions)
    """
    CREATE TABLE sessions (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        status        TEXT NOT NULL CHECK (status IN
                      ('proposed', 'countered', 'confirmed', 'declined', 'cancelled', 'expired', 'done')),
        day           TEXT NOT NULL,
        start_min     INTEGER NOT NULL CHECK (start_min >= 0 AND start_min < 1440),
        end_min       INTEGER NOT NULL CHECK (end_min > start_min AND end_min <= 1440),
        goal          TEXT NOT NULL,
        party_size    INTEGER NOT NULL CHECK (party_size >= 1),
        proposer_id   TEXT NOT NULL,
        proposer_name TEXT NOT NULL DEFAULT '',
        guild_id      TEXT NOT NULL DEFAULT '',
        channel_id    TEXT NOT NULL DEFAULT '',
        message_id    TEXT NOT NULL DEFAULT '',
        event_id      TEXT NOT NULL DEFAULT '',
        sug_day       TEXT,
        sug_start_min INTEGER,
        sug_end_min   INTEGER,
        sug_note      TEXT NOT NULL DEFAULT '',
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL
    );
    CREATE INDEX sessions_status_day ON sessions (status, day);
    CREATE TABLE session_players (
        session_id   INTEGER NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
        user_id      TEXT NOT NULL,
        display_name TEXT NOT NULL DEFAULT '',
        joined_at    TEXT NOT NULL,
        PRIMARY KEY (session_id, user_id)
    );
    """,
]

NOTE_MAX = 100
NAME_MAX = 64


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _clean(text: Optional[str], limit: int) -> str:
    """Single line, trimmed, length-capped."""
    text = ' '.join((text or '').split())
    return text[:limit]


class ScheduleStore:
    def __init__(self, path: str):
        self.path = path
        if path != ':memory:':
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        # One connection shared by worker threads, serialised by a lock.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute('PRAGMA foreign_keys = ON')
            if path != ':memory:':
                self._conn.execute('PRAGMA journal_mode = WAL')
            self._migrate()

    def _migrate(self):
        version = self._conn.execute('PRAGMA user_version').fetchone()[0]
        if version == len(MIGRATIONS):
            return
        if version > 0 and self.path != ':memory:':
            # Keep a copy of the data as it was before the upgrade
            self._copy_to(f'{self.path}.before-v{version + 1}')
        for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
            try:
                self._conn.executescript('BEGIN;' + sql + f'PRAGMA user_version = {i}; COMMIT;')
            except Exception:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise

    def _copy_to(self, dest_path: str):
        """Consistent copy of the live database. Caller holds the lock (or is still starting up)."""
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
        tmp = dest_path + '.tmp'
        dest = sqlite3.connect(tmp)
        try:
            self._conn.backup(dest)
        finally:
            dest.close()
        os.replace(tmp, dest_path)

    @property
    def schema_version(self) -> int:
        with self._lock:
            return self._conn.execute('PRAGMA user_version').fetchone()[0]

    def close(self):
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------- shared logic
    # `table` and `owner` only ever come from the constants below, never from
    # user input, so formatting them into SQL is safe. All values are bound.

    @staticmethod
    def _check_windows(windows: Optional[Iterable[Window]]) -> List[Window]:
        ws = merge(windows) if windows is not None else [DAY]
        if not ws:
            raise ValueError('at least one time window is needed')
        for s, e in ws:
            if not (0 <= s < e <= 1440):
                raise ValueError(f'bad window {(s, e)}')
        return ws

    def _set_windows(self, table, owner, days, state, windows, extra):
        """Latest command wins: replace this owner's windows of `state` on each day,
        and cut the new windows out of anything of the other state."""
        where = ''.join(f' AND {c} = ?' for c in owner)
        owner_vals = list(owner.values())
        now = _now()
        count = 0
        for day in days:
            iso = day.isoformat()
            self._conn.execute(
                f'DELETE FROM {table} WHERE day = ?{where} AND state = ?', [iso, *owner_vals, state]
            )
            for row in self._conn.execute(f'SELECT * FROM {table} WHERE day = ?{where}', [iso, *owner_vals]).fetchall():
                span = (row['start_min'], row['end_min'])
                pieces = subtract([span], windows)
                if pieces == [span]:
                    continue
                self._conn.execute(
                    f'DELETE FROM {table} WHERE day = ?{where} AND state = ? AND start_min = ?',
                    [iso, *owner_vals, row['state'], row['start_min']],
                )
                for ps, pe in pieces:
                    values = dict(zip(row.keys(), tuple(row)))
                    values.update(start_min=ps, end_min=pe, updated_at=now)
                    self._insert(table, values)
            for ws, we in windows:
                self._insert(table, {'day': iso, **owner, 'state': state, 'start_min': ws, 'end_min': we,
                                     **extra, 'updated_at': now})
            count += 1
        return count

    def _insert(self, table, values: dict):
        cols = ', '.join(values)
        marks = ', '.join('?' for _ in values)
        self._conn.execute(f'INSERT INTO {table} ({cols}) VALUES ({marks})', list(values.values()))

    def _remove_windows(self, table, owner, days, windows):
        """Remove availability on each day: all of it, or just the given windows."""
        where = ''.join(f' AND {c} = ?' for c in owner)
        owner_vals = list(owner.values())
        changed = 0
        for day in days:
            iso = day.isoformat()
            rows = self._conn.execute(f'SELECT * FROM {table} WHERE day = ?{where}', [iso, *owner_vals]).fetchall()
            if not rows:
                continue
            if windows is None or merge(windows) == [DAY]:
                self._conn.execute(f'DELETE FROM {table} WHERE day = ?{where}', [iso, *owner_vals])
                changed += 1
                continue
            touched = False
            for row in rows:
                span = (row['start_min'], row['end_min'])
                pieces = subtract([span], windows)
                if pieces == [span]:
                    continue
                touched = True
                self._conn.execute(
                    f'DELETE FROM {table} WHERE day = ?{where} AND state = ? AND start_min = ?',
                    [iso, *owner_vals, row['state'], row['start_min']],
                )
                for ps, pe in pieces:
                    values = dict(zip(row.keys(), tuple(row)))
                    values.update(start_min=ps, end_min=pe)
                    self._insert(table, values)
            changed += 1 if touched else 0
        return changed

    # ------------------------------------------------------------------ GM

    def set_gm_days(self, days: Iterable[date], state: str, windows: Optional[Iterable[Window]] = None,
                    note: str = '') -> int:
        """Mark days open or blocked for the given time windows (default all day)."""
        if state not in GM_STATES:
            raise ValueError(f'state must be one of {GM_STATES}')
        ws = self._check_windows(windows)
        with self._lock, self._conn:
            return self._set_windows('gm_availability', {}, days, state, ws, {'note': _clean(note, NOTE_MAX)})

    def clear_gm_days(self, days: Iterable[date], windows: Optional[Iterable[Window]] = None) -> int:
        """Clear whole days, or only the given time windows. Returns days changed."""
        with self._lock, self._conn:
            return self._remove_windows('gm_availability', {}, days, windows)

    def gm_days(self, start: date, end: date) -> Dict[date, List[Tuple[str, int, int, str]]]:
        """{date: [(state, start_min, end_min, note)]} for start..end inclusive."""
        with self._lock:
            rows = self._conn.execute(
                'SELECT day, state, start_min, end_min, note FROM gm_availability '
                'WHERE day BETWEEN ? AND ? ORDER BY day, start_min',
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        out = defaultdict(list)
        for r in rows:
            out[date.fromisoformat(r['day'])].append((r['state'], r['start_min'], r['end_min'], r['note']))
        return dict(out)

    # ------------------------------------------------------------- players

    def set_player_days(self, user_id, display_name: str, days: Iterable[date], free: bool,
                        windows: Optional[Iterable[Window]] = None) -> int:
        """Mark days free or busy for the given time windows (default all day)."""
        user_id = str(user_id)
        name = _clean(display_name, NAME_MAX)
        ws = self._check_windows(windows)
        with self._lock, self._conn:
            n = self._set_windows('player_availability', {'user_id': user_id}, days,
                                  'free' if free else 'busy', ws, {'display_name': name})
            # Keep the name current on all of this player's rows
            self._conn.execute('UPDATE player_availability SET display_name = ? WHERE user_id = ?', (name, user_id))
        return n

    def clear_player_days(self, user_id, days: Iterable[date], windows: Optional[Iterable[Window]] = None) -> int:
        with self._lock, self._conn:
            return self._remove_windows('player_availability', {'user_id': str(user_id)}, days, windows)

    def player_days(self, start: date, end: date) -> Dict[date, Dict[str, Tuple[str, List[Tuple[str, int, int]]]]]:
        """{date: {user_id: (name, [(state, start_min, end_min)])}} for start..end inclusive."""
        with self._lock:
            rows = self._conn.execute(
                'SELECT user_id, day, state, start_min, end_min, display_name FROM player_availability '
                'WHERE day BETWEEN ? AND ? ORDER BY day, display_name COLLATE NOCASE, start_min',
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        out: Dict = defaultdict(dict)
        for r in rows:
            day = out[date.fromisoformat(r['day'])]
            name, windows = day.setdefault(r['user_id'], (r['display_name'], []))
            windows.append((r['state'], r['start_min'], r['end_min']))
        return dict(out)

    def player_own_days(self, user_id, start: date, end: date) -> Dict[date, List[Tuple[str, int, int]]]:
        with self._lock:
            rows = self._conn.execute(
                'SELECT day, state, start_min, end_min FROM player_availability '
                'WHERE user_id = ? AND day BETWEEN ? AND ? ORDER BY day, start_min',
                (str(user_id), start.isoformat(), end.isoformat()),
            ).fetchall()
        out = defaultdict(list)
        for r in rows:
            out[date.fromisoformat(r['day'])].append((r['state'], r['start_min'], r['end_min']))
        return dict(out)

    # ------------------------------------------------------------ sessions

    # Columns callers may change through transition(); everything else has its own method.
    _SESSION_FIELDS = {'day', 'start_min', 'end_min', 'goal', 'party_size', 'event_id',
                       'sug_day', 'sug_start_min', 'sug_end_min', 'sug_note'}

    def _session(self, row) -> dict:
        s = {k: row[k] for k in row.keys()}
        s['day'] = date.fromisoformat(s['day'])
        s['sug_day'] = date.fromisoformat(s['sug_day']) if s['sug_day'] else None
        s['players'] = [
            (r['user_id'], r['display_name'])
            for r in self._conn.execute(
                'SELECT user_id, display_name FROM session_players WHERE session_id = ? '
                'ORDER BY joined_at, rowid', (s['id'],))
        ]
        return s

    def create_session(self, proposer_id, proposer_name: str, day: date, start_min: int, end_min: int,
                       goal: str, party_size: int, guild_id='', max_open: int = 3) -> int:
        """Create a proposal and join the proposer to it. Raises ProposalLimitError."""
        proposer_id = str(proposer_id)
        name = _clean(proposer_name, NAME_MAX)
        goal = _clean(goal, GOAL_MAX)
        if not goal:
            raise ValueError('a goal is needed')
        self._check_windows([(start_min, end_min)])
        now = _now()
        with self._lock, self._conn:
            open_now = self._conn.execute(
                'SELECT COUNT(*) FROM sessions WHERE proposer_id = ? AND status IN (?, ?)',
                (proposer_id, *SESSION_OPEN)).fetchone()[0]
            if open_now >= max_open:
                raise ProposalLimitError(max_open)
            cur = self._conn.execute(
                'INSERT INTO sessions (status, day, start_min, end_min, goal, party_size, proposer_id, '
                'proposer_name, guild_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                ('proposed', day.isoformat(), start_min, end_min, goal, int(party_size), proposer_id,
                 name, str(guild_id), now, now))
            sid = cur.lastrowid
            self._conn.execute(
                'INSERT INTO session_players (session_id, user_id, display_name, joined_at) VALUES (?, ?, ?, ?)',
                (sid, proposer_id, name, now))
        return sid

    def get_session(self, sid: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute('SELECT * FROM sessions WHERE id = ?', (sid,)).fetchone()
            return self._session(row) if row else None

    def list_sessions(self, statuses: Iterable[str], start: Optional[date] = None,
                      end: Optional[date] = None) -> List[dict]:
        statuses = list(statuses)
        sql = f'SELECT * FROM sessions WHERE status IN ({",".join("?" * len(statuses))})'
        params: list = list(statuses)
        if start:
            sql += ' AND day >= ?'
            params.append(start.isoformat())
        if end:
            sql += ' AND day <= ?'
            params.append(end.isoformat())
        with self._lock:
            rows = self._conn.execute(sql + ' ORDER BY day, start_min, id', params).fetchall()
            return [self._session(r) for r in rows]

    def set_message(self, sid: int, channel_id, message_id):
        with self._lock, self._conn:
            self._conn.execute('UPDATE sessions SET channel_id = ?, message_id = ?, updated_at = ? WHERE id = ?',
                               (str(channel_id), str(message_id), _now(), sid))

    def join_session(self, sid: int, user_id, name: str) -> str:
        """'joined', 'already' (name refreshed) or 'closed'."""
        user_id, name = str(user_id), _clean(name, NAME_MAX)
        with self._lock, self._conn:
            row = self._conn.execute('SELECT status FROM sessions WHERE id = ?', (sid,)).fetchone()
            if not row or row['status'] not in SESSION_LIVE:
                return 'closed'
            cur = self._conn.execute(
                'INSERT OR IGNORE INTO session_players (session_id, user_id, display_name, joined_at) '
                'VALUES (?, ?, ?, ?)', (sid, user_id, name, _now()))
            if cur.rowcount == 0:
                self._conn.execute('UPDATE session_players SET display_name = ? WHERE session_id = ? AND user_id = ?',
                                   (name, sid, user_id))
                return 'already'
            return 'joined'

    def leave_session(self, sid: int, user_id) -> str:
        """'left', 'not_in' or 'closed'."""
        with self._lock, self._conn:
            row = self._conn.execute('SELECT status FROM sessions WHERE id = ?', (sid,)).fetchone()
            if not row or row['status'] not in SESSION_LIVE:
                return 'closed'
            cur = self._conn.execute('DELETE FROM session_players WHERE session_id = ? AND user_id = ?',
                                     (sid, str(user_id)))
            return 'left' if cur.rowcount else 'not_in'

    def transition(self, sid: int, from_statuses: Iterable[str], to_status: str, **fields) -> bool:
        """Move a session to a new status only if it is currently in `from_statuses`.

        Returns False if it wasn't, so two people clicking Approve at once can't both win.
        """
        bad = set(fields) - self._SESSION_FIELDS
        if bad:
            raise ValueError(f'cannot set {sorted(bad)}')
        from_statuses = list(from_statuses)
        sets = ', '.join(f'{k} = ?' for k in fields)
        sql = (f'UPDATE sessions SET status = ?, updated_at = ?{", " + sets if sets else ""} '
               f'WHERE id = ? AND status IN ({",".join("?" * len(from_statuses))})')
        params = [to_status, _now(), *[v.isoformat() if isinstance(v, date) else v for v in fields.values()],
                  sid, *from_statuses]
        with self._lock, self._conn:
            return self._conn.execute(sql, params).rowcount == 1

    def suggest_time(self, sid: int, day: date, start_min: int, end_min: int, note: str = '') -> bool:
        """GM counter-offer on an open proposal."""
        self._check_windows([(start_min, end_min)])
        return self.transition(sid, SESSION_OPEN, 'countered', sug_day=day, sug_start_min=start_min,
                               sug_end_min=end_min, sug_note=_clean(note, 150))

    def accept_suggestion(self, sid: int) -> bool:
        """The suggested time becomes the session time and the session is confirmed."""
        with self._lock, self._conn:
            return self._conn.execute(
                "UPDATE sessions SET status = 'confirmed', day = sug_day, start_min = sug_start_min, "
                "end_min = sug_end_min, sug_day = NULL, sug_start_min = NULL, sug_end_min = NULL, "
                "sug_note = '', updated_at = ? WHERE id = ? AND status = 'countered' AND sug_day IS NOT NULL",
                (_now(), sid)).rowcount == 1

    def move_session(self, sid: int, day: date, start_min: int, end_min: int) -> bool:
        """Change the time of a confirmed session."""
        self._check_windows([(start_min, end_min)])
        return self.transition(sid, ('confirmed',), 'confirmed', day=day, start_min=start_min, end_min=end_min)

    def close_finished(self, today_: date, now_min: int) -> List[Tuple[int, str]]:
        """Open proposals whose start has passed become 'expired'; confirmed sessions whose end
        has passed become 'done'. Returns [(id, new_status)]."""
        iso = today_.isoformat()
        changed: List[Tuple[int, str]] = []
        with self._lock, self._conn:
            for statuses, col, new in ((SESSION_OPEN, 'start_min', 'expired'), (('confirmed',), 'end_min', 'done')):
                marks = ','.join('?' * len(statuses))
                rows = self._conn.execute(
                    f'SELECT id FROM sessions WHERE status IN ({marks}) AND (day < ? OR (day = ? AND {col} <= ?))',
                    [*statuses, iso, iso, now_min]).fetchall()
                for r in rows:
                    self._conn.execute('UPDATE sessions SET status = ?, updated_at = ? WHERE id = ?',
                                       (new, _now(), r['id']))
                    changed.append((r['id'], new))
        return changed

    # ------------------------------------------------------------ settings

    def get_setting(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
        return row['value'] if row else None

    def set_setting(self, key: str, value: Optional[str]):
        with self._lock, self._conn:
            if value is None:
                self._conn.execute('DELETE FROM settings WHERE key = ?', (key,))
            else:
                self._conn.execute(
                    'INSERT INTO settings (key, value) VALUES (?, ?) '
                    'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                    (key, str(value)),
                )

    # ------------------------------------------------------------ backups

    def backup(self, dest_path: str):
        """Consistent copy of the live database (safe while the bot is running)."""
        with self._lock:
            self._copy_to(dest_path)


def prune_backups(backup_dir: str, keep: int, prefix: str = 'schedule-') -> List[str]:
    """Delete all but the newest `keep` backups. Returns the deleted paths."""
    if not os.path.isdir(backup_dir):
        return []
    files = sorted(
        f for f in os.listdir(backup_dir) if f.startswith(prefix) and f.endswith('.db')
    )
    doomed = files[:-keep] if keep > 0 else files
    for f in doomed:
        os.remove(os.path.join(backup_dir, f))
    return [os.path.join(backup_dir, f) for f in doomed]
