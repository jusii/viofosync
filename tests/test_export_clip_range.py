"""Export jobs capture the source clips' date range at creation.

The export jobs list UI shows the date range of the footage in each
export. The source clips get pruned by retention over time, so the
min/max clip timestamp is snapshotted onto the ``export_jobs`` row
when the job is enqueued rather than derived on every list load.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from web.db import Database
from web.services.exporter import ExportWorker

# export_jobs schema as it shipped before clip_start/clip_end existed.
_OLD_EXPORT_JOBS = """
CREATE TABLE export_jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    type          TEXT NOT NULL,
    clip_ids      TEXT NOT NULL,
    state         TEXT NOT NULL,
    progress      REAL NOT NULL DEFAULT 0.0,
    output_path   TEXT,
    error         TEXT,
    created_at    INTEGER NOT NULL,
    started_at    INTEGER,
    finished_at   INTEGER
);
"""


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / "test.db"))


def _insert_clip(db: Database, clip_id: int, ts: int, camera: str) -> None:
    with db.write() as c:
        c.execute(
            "INSERT INTO clip_index "
            "(id, path, basename, group_name, timestamp, camera, "
            " sequence, event_type, has_gpx, gps_examined, scanned_at) "
            "VALUES (?,?,?,?,?,?,?,?,0,0,?)",
            (clip_id, f"/rec/{clip_id}.mp4", f"{clip_id}.mp4",
             "2024-03-15", ts, camera, clip_id, "normal", ts),
        )


async def _async_noop(_event):  # pragma: no cover — broadcast stub
    pass


def test_existing_db_gains_clip_range_columns(tmp_path: Path) -> None:
    """A pre-existing DB with the old export_jobs schema is migrated to
    carry clip_start/clip_end, and its existing rows survive."""
    path = tmp_path / "old.db"
    raw = sqlite3.connect(str(path))
    raw.executescript(_OLD_EXPORT_JOBS)
    raw.execute(
        "INSERT INTO export_jobs (type, clip_ids, state, created_at) "
        "VALUES ('join_front', '{\"clip_ids\": [1]}', 'queued', 5)"
    )
    raw.commit()
    raw.close()

    db = Database(str(path))
    with db.conn() as c:
        cols = {
            r["name"]
            for r in c.execute("PRAGMA table_info(export_jobs)")
        }
        row = c.execute(
            "SELECT type, clip_start, clip_end FROM export_jobs"
        ).fetchone()

    assert "clip_start" in cols
    assert "clip_end" in cols
    # Existing row preserved; the new columns default to NULL.
    assert row["type"] == "join_front"
    assert row["clip_start"] is None
    assert row["clip_end"] is None


def test_enqueue_snapshots_clip_date_range(db: Database, monkeypatch) -> None:
    """enqueue records the min/max timestamp of the selected clips so
    the range survives even after the clips are retention-pruned."""
    monkeypatch.setattr(
        "web.services.exporter.ffmpeg_available", lambda: True
    )
    _insert_clip(db, 1, 1_700_000_500, "F")
    _insert_clip(db, 2, 1_700_000_100, "F")  # earliest
    _insert_clip(db, 3, 1_700_000_900, "F")  # latest

    worker = ExportWorker(db=db, provider=MagicMock(), broadcast=_async_noop)
    job_id = worker.enqueue("join_front", [1, 2, 3])

    with db.conn() as c:
        row = c.execute(
            "SELECT clip_start, clip_end FROM export_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
    assert row["clip_start"] == 1_700_000_100
    assert row["clip_end"] == 1_700_000_900


def test_enqueue_stores_null_range_for_unknown_clips(
    db: Database, monkeypatch
) -> None:
    """If none of the selected clips resolve in clip_index (e.g. a
    stale selection), the range columns stay NULL rather than 500ing."""
    monkeypatch.setattr(
        "web.services.exporter.ffmpeg_available", lambda: True
    )
    worker = ExportWorker(db=db, provider=MagicMock(), broadcast=_async_noop)
    job_id = worker.enqueue("pip", [90, 91])

    with db.conn() as c:
        row = c.execute(
            "SELECT clip_start, clip_end FROM export_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
    assert row["clip_start"] is None
    assert row["clip_end"] is None
