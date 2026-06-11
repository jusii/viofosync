"""Failed/cancelled exports must not leave orphaned files in .exports.

ffmpeg wrote straight to the final {job_id}.mp4, so a cancel or
failure left a partial that was unreferenced (output_path NULL) yet
counted against the recordings quota. Outputs now stage to a .part
name renamed only on verified success; failures/cancels remove the
partial, and a startup sweep clears pre-existing orphans.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from web.db import Database
from web.services import exporter as exp_mod
from web.services.exporter import ExportWorker, _ExportCancelled


async def _noop(_e):
    pass


@pytest.fixture
def env(tmp_path: Path):
    rec = tmp_path / "rec"
    (rec / exp_mod.EXPORT_DIR_NAME).mkdir(parents=True)
    db = Database(str(rec / "t.db"))
    provider = MagicMock()
    provider.get.return_value = MagicMock(recordings=str(rec))
    worker = ExportWorker(db=db, provider=provider, broadcast=_noop)
    return rec, db, worker


def _job(db: Database, jid: int, state: str = "running") -> None:
    with db.write() as c:
        c.execute(
            "INSERT INTO export_jobs (id, type, clip_ids, state, "
            "progress, created_at) VALUES (?, 'pip', '{}', ?, 0, 0)",
            (jid, state),
        )


def test_partial_path_keeps_inferable_extension(tmp_path: Path):
    """ffmpeg chooses its muxer from the output filename's extension, so
    the staged partial must end in a real container extension (.mp4), not
    a bare .part it can't infer (regression: '{id}.mp4.part' made ffmpeg
    fail with 'Unable to choose an output format')."""
    rec = str(tmp_path)
    part = exp_mod._partial_path(rec, 3)
    assert part.endswith(".mp4"), part
    assert ".part" in Path(part).name
    assert part != exp_mod._output_path(rec, 3)


async def test_finish_success_renames_part_to_final(env):
    rec, db, worker = env
    _job(db, 5)
    part = exp_mod._partial_path(str(rec), 5)
    Path(part).write_bytes(b"video-bytes")

    worker._finish(5, True, None, part)

    final = exp_mod._output_path(str(rec), 5)
    assert Path(final).read_bytes() == b"video-bytes"
    assert not Path(part).exists()
    with db.conn() as c:
        assert c.execute(
            "SELECT output_path FROM export_jobs WHERE id=5"
        ).fetchone()["output_path"] == final


async def test_finish_failure_removes_partial(env):
    rec, db, worker = env
    _job(db, 6)
    part = exp_mod._partial_path(str(rec), 6)
    Path(part).write_bytes(b"half")

    worker._finish(6, False, "ffmpeg exit 1", None)

    assert not Path(part).exists(), "failed export left a partial behind"


async def test_cancel_removes_partial(env):
    rec, db, worker = env
    _job(db, 7)
    part = exp_mod._partial_path(str(rec), 7)
    Path(part).write_bytes(b"interrupted")

    async def _boom(job):
        worker._current_job_id = job["id"]
        raise _ExportCancelled()

    worker._run_job = _boom
    await worker._process({"id": 7})

    assert not Path(part).exists(), "cancelled export left a partial behind"


def test_startup_sweep_removes_orphans(env):
    rec, db, worker = env
    edir = rec / exp_mod.EXPORT_DIR_NAME
    # Orphaned partial from a crashed render.
    (edir / "9.mp4.part").write_bytes(b"x")
    # Pre-fix orphan: a {id}.mp4 with no done row.
    (edir / "10.mp4").write_bytes(b"y")
    # A legitimately finished export must survive.
    (edir / "11.mp4").write_bytes(b"keep")
    with db.write() as c:
        c.execute(
            "INSERT INTO export_jobs (id, type, clip_ids, state, progress, "
            "created_at, output_path) VALUES "
            "(11, 'pip', '{}', 'done', 1.0, 0, ?)",
            (str(edir / "11.mp4"),),
        )

    removed = exp_mod.sweep_orphan_exports(db, str(rec))

    assert not (edir / "9.mp4.part").exists()
    assert not (edir / "10.mp4").exists()
    assert (edir / "11.mp4").exists()
    assert removed == 2
