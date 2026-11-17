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

GM_STATES = ('open', 'blocked')

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
        for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
            try:
                self._conn.executescript('BEGIN;' + sql + f'PRAGMA user_version = {i}; COMMIT;')
            except Exception:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise

    @property
    def schema_version(self) -> int:
        with self._lock:
            return self._conn.execute('PRAGMA user_version').fetchone()[0]

    def close(self):
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ GM

    def set_gm_days(self, days: Iterable[date], state: str, note: str = '') -> int:
        if state not in GM_STATES:
            raise ValueError(f'state must be one of {GM_STATES}')
        note = _clean(note, NOTE_MAX)
        now = _now()
        rows = [(d.isoformat(), state, note, now) for d in days]
        with self._lock, self._conn:
            self._conn.executemany(
                'INSERT INTO gm_availability (day, state, note, updated_at) VALUES (?, ?, ?, ?) '
                'ON CONFLICT(day) DO UPDATE SET state = excluded.state, note = excluded.note, '
                'updated_at = excluded.updated_at',
                rows,
            )
        return len(rows)

    def clear_gm_days(self, days: Iterable[date]) -> int:
        keys = [(d.isoformat(),) for d in days]
        with self._lock, self._conn:
            before = self._conn.total_changes
            self._conn.executemany('DELETE FROM gm_availability WHERE day = ?', keys)
            return self._conn.total_changes - before

    def gm_days(self, start: date, end: date) -> Dict[date, Tuple[str, str]]:
        """{date: (state, note)} for start..end inclusive."""
        with self._lock:
            rows = self._conn.execute(
                'SELECT day, state, note FROM gm_availability WHERE day BETWEEN ? AND ? ORDER BY day',
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        return {date.fromisoformat(r['day']): (r['state'], r['note']) for r in rows}

    # ------------------------------------------------------------- players

    def set_player_days(self, user_id, display_name: str, days: Iterable[date], free: bool) -> int:
        user_id = str(user_id)
        name = _clean(display_name, NAME_MAX)
        now = _now()
        rows = [(user_id, d.isoformat(), 1 if free else 0, name, now) for d in days]
        with self._lock, self._conn:
            self._conn.executemany(
                'INSERT INTO player_availability (user_id, day, free, display_name, updated_at) '
                'VALUES (?, ?, ?, ?, ?) '
                'ON CONFLICT(user_id, day) DO UPDATE SET free = excluded.free, '
                'display_name = excluded.display_name, updated_at = excluded.updated_at',
                rows,
            )
            # Keep the name current on all of this player's rows
            self._conn.execute(
                'UPDATE player_availability SET display_name = ? WHERE user_id = ?', (name, user_id)
            )
        return len(rows)

    def clear_player_days(self, user_id, days: Iterable[date]) -> int:
        user_id = str(user_id)
        keys = [(user_id, d.isoformat()) for d in days]
        with self._lock, self._conn:
            before = self._conn.total_changes
            self._conn.executemany(
                'DELETE FROM player_availability WHERE user_id = ? AND day = ?', keys
            )
            return self._conn.total_changes - before

    def player_days(self, start: date, end: date) -> Dict[date, Dict[str, List[str]]]:
        """{date: {'free': [names], 'busy': [names]}} for start..end inclusive."""
        with self._lock:
            rows = self._conn.execute(
                'SELECT day, free, display_name FROM player_availability '
                'WHERE day BETWEEN ? AND ? ORDER BY display_name COLLATE NOCASE',
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        out = defaultdict(lambda: {'free': [], 'busy': []})
        for r in rows:
            out[date.fromisoformat(r['day'])]['free' if r['free'] else 'busy'].append(r['display_name'])
        return dict(out)

    def player_own_days(self, user_id, start: date, end: date) -> Dict[date, bool]:
        with self._lock:
            rows = self._conn.execute(
                'SELECT day, free FROM player_availability '
                'WHERE user_id = ? AND day BETWEEN ? AND ? ORDER BY day',
                (str(user_id), start.isoformat(), end.isoformat()),
            ).fetchall()
        return {date.fromisoformat(r['day']): bool(r['free']) for r in rows}

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
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
        tmp = dest_path + '.tmp'
        dest = sqlite3.connect(tmp)
        try:
            with self._lock:
                self._conn.backup(dest)
        finally:
            dest.close()
        os.replace(tmp, dest_path)


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
