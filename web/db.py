"""SQLite state store for the web UI.

Lives at ``$RECORDINGS/.viofosync.db`` so it travels with the
archive volume. WAL mode so readers don't block the SyncWorker
writer.

Schema is created on first boot and migrated idempotently via
``CREATE TABLE IF NOT EXISTS``. There is no migration system
yet — keep columns additive and nullable.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator


_SCHEMA = """
CREATE TABLE IF NOT EXISTS clip_index (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    path          TEXT NOT NULL UNIQUE,
    basename      TEXT NOT NULL,
    group_name    TEXT,
    timestamp     INTEGER NOT NULL,   -- unix seconds
    camera        TEXT NOT NULL,      -- 'F' or 'R' or other
    sequence      INTEGER NOT NULL,
    event_type    TEXT,               -- 'normal'|'parking'|'ro'
    size_bytes    INTEGER,
    has_gpx       INTEGER NOT NULL DEFAULT 0,
    gps_examined  INTEGER NOT NULL DEFAULT 0,
    duration_s    REAL,
    scanned_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clip_index_ts
    ON clip_index(timestamp);
CREATE INDEX IF NOT EXISTS idx_clip_index_group
    ON clip_index(group_name);

CREATE TABLE IF NOT EXISTS download_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    filename        TEXT NOT NULL UNIQUE,
    source_dir      TEXT NOT NULL,
    remote_size     INTEGER,
    recorded_at     INTEGER,
    camera          TEXT,
    event_type      TEXT,
    state           TEXT NOT NULL,    -- pending|downloading|done|failed|gone
    priority        INTEGER NOT NULL DEFAULT 0,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    last_attempt_at INTEGER,
    enqueued_at     INTEGER NOT NULL,
    started_at      INTEGER,
    finished_at     INTEGER,
    manual          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_queue_state
    ON download_queue(state, priority DESC, enqueued_at ASC);

CREATE TABLE IF NOT EXISTS export_jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    type          TEXT NOT NULL,      -- join_front|join_rear|pip
    clip_ids      TEXT NOT NULL,      -- JSON array
    state         TEXT NOT NULL,      -- queued|running|done|failed|cancelled
    progress      REAL NOT NULL DEFAULT 0.0,
    output_path   TEXT,
    error         TEXT,
    created_at    INTEGER NOT NULL,
    started_at    INTEGER,
    finished_at   INTEGER
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS geocode_cache (
    lat_key     REAL NOT NULL,
    lon_key     REAL NOT NULL,
    label       TEXT NOT NULL,
    fetched_at  INTEGER NOT NULL,
    PRIMARY KEY (lat_key, lon_key)
);
"""


class Database:
    """Thread-safe SQLite wrapper.

    SQLite's own thread-safety guarantees work for us because
    we're using ``check_same_thread=False`` with a short-lived
    connection per operation via the :meth:`conn` context
    manager. A single shared connection would be simpler but
    risks contention with the async workers.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # One initial connection just to set pragmas + schema.
        with self.conn() as c:
            c.executescript(_SCHEMA)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            self._migrate(c)

    @staticmethod
    def _migrate(c: sqlite3.Connection) -> None:
        """Idempotent column-additions for databases that pre-date
        the current schema. ``CREATE TABLE IF NOT EXISTS`` only
        creates fresh tables, so any column added after the first
        release needs an explicit ALTER on existing volumes.

        SQLite raises ``OperationalError("duplicate column …")``
        when the column is already present, which we treat as a
        success. Any other error propagates so an actually-broken
        DB doesn't get silently ignored.
        """
        def _add_column(table: str, col: str, ddl: str) -> None:
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise

        _add_column(
            "clip_index", "gps_examined",
            "INTEGER NOT NULL DEFAULT 0",
        )
        # Clips that already have a sidecar have, by definition,
        # been examined — backfill the flag so the manual
        # "Extract GPS" button doesn't waste cycles re-parsing
        # them. Idempotent: rows already at 1 stay at 1.
        c.execute(
            "UPDATE clip_index SET gps_examined = 1 "
            "WHERE has_gpx = 1 AND gps_examined = 0"
        )

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection with row-factory set.

        We serialise writers via ``self._lock`` because WAL
        allows concurrent readers but only one writer.
        """
        c = sqlite3.connect(
            self.path,
            timeout=10.0,
            isolation_level=None,   # autocommit
            check_same_thread=False,
        )
        c.row_factory = sqlite3.Row
        # Retry on SQLITE_BUSY for 30 s before surfacing
        # OperationalError — lets a brief writer collision during
        # the initial scan resolve silently.
        c.execute("PRAGMA busy_timeout=30000")
        try:
            yield c
        finally:
            c.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Serialised write connection."""
        with self._lock, self.conn() as c:
            yield c
