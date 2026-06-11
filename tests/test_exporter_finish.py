"""Regression tests for ExportWorker._finish.

The first version of `_finish` wrote `progress=NULL` on failure,
which violated the `NOT NULL DEFAULT 0.0` schema and raised an
sqlite3.IntegrityError mid-write — leaving the job stuck in
state='running' and the frontend sitting at 0% forever.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from web.db import Database
from web.services.exporter import ExportWorker


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / "test.db"))


def _insert_running_job(db: Database, *, progress: float = 0.42) -> int:
    """Insert a job in 'running' state with non-trivial progress
    so we can assert it isn't trampled on failure."""
    with db.write() as c:
        cur = c.execute(
            "INSERT INTO export_jobs "
            "(type, clip_ids, state, progress, created_at, started_at) "
            "VALUES ('pip', '{\"clip_ids\": []}', 'running', ?, 0, 0)",
            (progress,),
        )
        return cur.lastrowid


async def _async_noop(_event):  # pragma: no cover — broadcast stub
    pass


async def test_finish_failed_job_does_not_violate_progress_constraint(
    db: Database,
) -> None:
    """The original bug: ffmpeg dies, _finish(... ok=False, ...None)
    is called, and the UPDATE fails because progress is NOT NULL.

    After the fix the UPDATE simply doesn't touch the progress
    column on failure — preserving partial-progress info AND
    avoiding the constraint violation."""
    job_id = _insert_running_job(db, progress=0.42)
    worker = ExportWorker(
        db=db,
        provider=MagicMock(),
        broadcast=_async_noop,
    )
    worker._finish(
        job_id,
        ok=False,
        err="qsv MFX init failed",
        output_path=None,
    )
    with db.conn() as c:
        row = c.execute(
            "SELECT * FROM export_jobs WHERE id=?", (job_id,)
        ).fetchone()
    assert row["state"] == "failed"
    assert row["error"] == "qsv MFX init failed"
    # progress was 0.42 going in; failure leaves it alone.
    assert row["progress"] == pytest.approx(0.42)
    assert row["finished_at"] is not None


async def test_finish_snapshots_output_size_and_duration(
    db: Database, tmp_path, monkeypatch,
) -> None:
    """A successful job records the output's byte size and length on the row so
    the export list can show them without re-probing."""
    from web.services import durations

    out = tmp_path / "out.mp4"
    out.write_bytes(b"\0" * 4096)
    monkeypatch.setattr(durations, "_probe_duration_mvhd", lambda p: 73.5)

    job_id = _insert_running_job(db, progress=0.9)
    worker = ExportWorker(db=db, provider=MagicMock(), broadcast=_async_noop)
    worker._finish(job_id, ok=True, err=None, output_path=str(out))

    with db.conn() as c:
        row = c.execute(
            "SELECT * FROM export_jobs WHERE id=?", (job_id,)
        ).fetchone()
    assert row["output_size"] == 4096
    assert row["output_duration_s"] == pytest.approx(73.5)


async def test_finish_failure_leaves_output_stats_null(
    db: Database,
) -> None:
    """A failed job has no usable output, so size/length stay NULL."""
    job_id = _insert_running_job(db, progress=0.3)
    worker = ExportWorker(db=db, provider=MagicMock(), broadcast=_async_noop)
    worker._finish(job_id, ok=False, err="boom", output_path=None)

    with db.conn() as c:
        row = c.execute(
            "SELECT * FROM export_jobs WHERE id=?", (job_id,)
        ).fetchone()
    assert row["output_size"] is None
    assert row["output_duration_s"] is None


async def test_make_export_preview_broadcasts_ready_on_success(
    db: Database, monkeypatch,
) -> None:
    """When the filmstrip lands, the worker emits export_preview_ready so the
    UI can swap its 'generating' placeholder for the real strip."""
    from web.services import export_preview

    events = []

    async def capture(ev):
        events.append(ev)

    snap = MagicMock()
    snap.recordings = "/rec"
    provider = MagicMock()
    provider.get.return_value = snap
    worker = ExportWorker(db=db, provider=provider, broadcast=capture)

    async def fake_ensure(recordings, job_id, output_path, duration_s):
        return "/rec/.export_previews/9.jpg"

    monkeypatch.setattr(export_preview, "ensure_export_preview", fake_ensure)
    await worker._make_export_preview(9, "/tmp/out.mp4")
    assert {"type": "export_preview_ready", "job_id": 9} in events


async def test_make_export_preview_silent_when_generation_fails(
    db: Database, monkeypatch,
) -> None:
    """No file -> no ready event (the placeholder simply stays)."""
    from web.services import export_preview

    events = []

    async def capture(ev):
        events.append(ev)

    snap = MagicMock()
    snap.recordings = "/rec"
    provider = MagicMock()
    provider.get.return_value = snap
    worker = ExportWorker(db=db, provider=provider, broadcast=capture)

    async def fake_ensure(*a, **k):
        return None

    monkeypatch.setattr(export_preview, "ensure_export_preview", fake_ensure)
    await worker._make_export_preview(9, "/tmp/out.mp4")
    assert events == []


async def test_finish_done_job_writes_full_progress(
    db: Database,
) -> None:
    """Successful jobs flip progress to 1.0 so the UI shows 100%
    even if the per-segment ticks were sparse."""
    job_id = _insert_running_job(db, progress=0.7)
    worker = ExportWorker(
        db=db,
        provider=MagicMock(),
        broadcast=_async_noop,
    )
    worker._finish(
        job_id,
        ok=True,
        err=None,
        output_path="/tmp/out.mp4",
    )
    with db.conn() as c:
        row = c.execute(
            "SELECT * FROM export_jobs WHERE id=?", (job_id,)
        ).fetchone()
    assert row["state"] == "done"
    assert row["progress"] == pytest.approx(1.0)
    assert row["output_path"] == "/tmp/out.mp4"
    assert row["error"] is None
