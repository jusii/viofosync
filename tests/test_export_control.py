"""Pause / resume an in-progress export, and kill-on-delete.

The single export worker tracks the ffmpeg child of the running job so the
HTTP layer can pause it (SIGSTOP), resume it (SIGCONT), or kill it when the
job is deleted mid-render. A killed job unwinds via _ExportCancelled without
being marked failed (its row is being deleted).
"""
from __future__ import annotations

import signal
from unittest.mock import MagicMock

import pytest

from web.db import Database
from web.services import exporter
from web.services.exporter import ExportWorker, reconcile_orphan_jobs


async def _noop(_event):  # broadcast stub
    pass


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "t.db"))


def _job(db: Database, jid: int, state: str = "running") -> None:
    with db.write() as c:
        c.execute(
            "INSERT INTO export_jobs (id, type, clip_ids, state, created_at) "
            "VALUES (?, 'switched', '{}', ?, 0)",
            (jid, state),
        )


def _state(db: Database, jid: int) -> str:
    with db.conn() as c:
        return c.execute(
            "SELECT state FROM export_jobs WHERE id=?", (jid,)
        ).fetchone()["state"]


class _FakeProc:
    def __init__(self):
        self.signals: list = []
        self.killed = False

    def send_signal(self, sig):
        self.signals.append(sig)

    def kill(self):
        self.killed = True


def _worker(db: Database) -> ExportWorker:
    return ExportWorker(db=db, provider=MagicMock(), broadcast=_noop)


# --- pause / resume ---

async def test_pause_signals_stop_and_sets_state(db):
    w = _worker(db); _job(db, 7, "running")
    w._current_job_id = 7
    w._current_proc = _FakeProc()
    assert await w.pause(7) is True
    assert signal.SIGSTOP in w._current_proc.signals
    assert w._paused is True
    assert _state(db, 7) == "paused"


async def test_resume_signals_cont_and_sets_state(db):
    w = _worker(db); _job(db, 7, "paused")
    fake = _FakeProc()
    w._current_job_id = 7
    w._current_proc = fake
    w._paused = True
    assert await w.resume(7) is True
    assert signal.SIGCONT in fake.signals
    assert w._paused is False
    assert _state(db, 7) == "running"


async def test_pause_false_for_non_current_job(db):
    w = _worker(db); _job(db, 7, "running")
    w._current_job_id = 7
    w._current_proc = _FakeProc()
    assert await w.pause(99) is False
    assert _state(db, 7) == "running"


# --- cancel / kill on delete ---

async def test_cancel_kills_current_proc(db):
    w = _worker(db)
    fake = _FakeProc()
    w._current_job_id = 7
    w._current_proc = fake
    assert await w.cancel(7) is True
    assert fake.killed is True
    assert w._cancel_current is True


async def test_cancel_false_when_job_not_running(db):
    w = _worker(db)
    assert await w.cancel(7) is False


async def test_run_ffmpeg_raises_when_cancelled(db):
    w = _worker(db)
    w._current_job_id = 7
    w._cancel_current = True
    with pytest.raises(exporter._ExportCancelled):
        await w._run_ffmpeg(7, ["-y", "out.mp4"], 1.0)


# --- _process: cancelled vs real failure ---

async def test_process_discards_cancelled_job_without_failing(db, monkeypatch):
    w = _worker(db); _job(db, 7, "running")

    async def cancelled(_job):
        raise exporter._ExportCancelled

    monkeypatch.setattr(w, "_run_job", cancelled)
    await w._process({"id": 7})
    # row is being deleted by the endpoint; must NOT be flipped to failed
    assert _state(db, 7) == "running"


async def test_process_marks_real_error_failed(db, monkeypatch):
    w = _worker(db); _job(db, 8, "running")

    async def boom(_job):
        raise ValueError("nope")

    monkeypatch.setattr(w, "_run_job", boom)
    await w._process({"id": 8})
    assert _state(db, 8) == "failed"


# --- restart reconcile includes paused ---

def test_reconcile_marks_paused_and_running_failed(db):
    _job(db, 1, "paused")
    _job(db, 2, "running")
    _job(db, 3, "done")
    n = reconcile_orphan_jobs(db)
    assert n == 2
    assert _state(db, 1) == "failed"
    assert _state(db, 2) == "failed"
    assert _state(db, 3) == "done"
