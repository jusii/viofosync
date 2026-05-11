"""Reconcile any download_queue / export_jobs rows stuck at the
mid-flight states ('downloading' / 'running') after a crash.

The Database fixture spins up a real SQLite file in a tempdir
because the schema is created via the Database constructor —
we want the actual schema, not a stub.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from web.db import Database
from web.services import exporter, queue


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / "test.db"))


# ---- reconcile_orphan_downloads ----

def _insert_queue_row(
    db: Database,
    *,
    filename: str,
    state: str,
    started_at: int | None = None,
) -> None:
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, state, enqueued_at, started_at) "
            "VALUES (?, '/DCIM/Movie', ?, ?, ?)",
            (filename, state, int(time.time()), started_at),
        )


def _states(db: Database) -> dict[str, str]:
    with db.conn() as c:
        rows = c.execute(
            "SELECT filename, state FROM download_queue"
        ).fetchall()
    return {r["filename"]: r["state"] for r in rows}


def test_reconcile_no_orphans_returns_zero(db: Database) -> None:
    _insert_queue_row(db, filename="X.MP4", state="pending")
    _insert_queue_row(db, filename="Y.MP4", state="done")
    assert queue.reconcile_orphan_downloads(db) == 0
    assert _states(db) == {"X.MP4": "pending", "Y.MP4": "done"}


def test_reconcile_resets_downloading_to_pending(db: Database) -> None:
    _insert_queue_row(
        db, filename="X.MP4", state="downloading", started_at=12345,
    )
    _insert_queue_row(db, filename="Y.MP4", state="pending")
    _insert_queue_row(db, filename="Z.MP4", state="done")
    n = queue.reconcile_orphan_downloads(db)
    assert n == 1
    s = _states(db)
    assert s["X.MP4"] == "pending"
    assert s["Y.MP4"] == "pending"
    assert s["Z.MP4"] == "done"


def test_reconcile_clears_started_at_on_orphans(db: Database) -> None:
    _insert_queue_row(
        db, filename="X.MP4", state="downloading", started_at=12345,
    )
    queue.reconcile_orphan_downloads(db)
    with db.conn() as c:
        row = c.execute(
            "SELECT started_at FROM download_queue WHERE filename='X.MP4'"
        ).fetchone()
    assert row["started_at"] is None


def test_reconcile_does_not_bump_attempts(db: Database) -> None:
    """An orphan from a crash isn't a download failure — leaving
    attempts alone keeps the user's retry budget intact."""
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, state, enqueued_at, started_at, attempts) "
            "VALUES ('X.MP4', '/DCIM/Movie', 'downloading', ?, 1, 2)",
            (int(time.time()),),
        )
    queue.reconcile_orphan_downloads(db)
    with db.conn() as c:
        row = c.execute(
            "SELECT attempts FROM download_queue WHERE filename='X.MP4'"
        ).fetchone()
    assert row["attempts"] == 2


# ---- reconcile_orphan_jobs ----

def _insert_job_row(
    db: Database,
    *,
    job_type: str = "join_front",
    state: str,
    error: str | None = None,
) -> int:
    with db.write() as c:
        cur = c.execute(
            "INSERT INTO export_jobs "
            "(type, clip_ids, state, created_at, error) "
            "VALUES (?, '[1]', ?, ?, ?)",
            (job_type, state, int(time.time()), error),
        )
        return cur.lastrowid


def test_reconcile_jobs_no_orphans_returns_zero(db: Database) -> None:
    _insert_job_row(db, state="queued")
    _insert_job_row(db, state="done")
    assert exporter.reconcile_orphan_jobs(db) == 0


def test_reconcile_jobs_marks_running_as_failed(db: Database) -> None:
    running_id = _insert_job_row(db, state="running")
    queued_id = _insert_job_row(db, state="queued")
    done_id = _insert_job_row(db, state="done")
    n = exporter.reconcile_orphan_jobs(db)
    assert n == 1
    with db.conn() as c:
        rows = {
            r["id"]: r
            for r in c.execute(
                "SELECT id, state, error, finished_at FROM export_jobs"
            ).fetchall()
        }
    assert rows[running_id]["state"] == "failed"
    assert rows[running_id]["error"] == "interrupted by container restart"
    assert rows[running_id]["finished_at"] is not None
    assert rows[queued_id]["state"] == "queued"
    assert rows[done_id]["state"] == "done"


def test_reconcile_jobs_does_not_overwrite_existing_failed(db: Database) -> None:
    """A pre-existing 'failed' row keeps its original error message;
    we only touch rows currently 'running'."""
    failed_id = _insert_job_row(db, state="failed", error="ffmpeg crashed")
    exporter.reconcile_orphan_jobs(db)
    with db.conn() as c:
        row = c.execute(
            "SELECT state, error FROM export_jobs WHERE id=?",
            (failed_id,),
        ).fetchone()
    assert row["state"] == "failed"
    assert row["error"] == "ffmpeg crashed"
