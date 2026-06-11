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
            "VALUES (?, 'timeline', '{}', ?, 0)",
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
    w = _worker(db)
    _job(db, 7, "running")
    w._current_job_id = 7
    w._current_proc = _FakeProc()
    assert await w.pause(7) is True
    assert signal.SIGSTOP in w._current_proc.signals
    assert w._paused is True
    assert _state(db, 7) == "paused"


async def test_resume_signals_cont_and_sets_state(db):
    w = _worker(db)
    _job(db, 7, "paused")
    fake = _FakeProc()
    w._current_job_id = 7
    w._current_proc = fake
    w._paused = True
    assert await w.resume(7) is True
    assert signal.SIGCONT in fake.signals
    assert w._paused is False
    assert _state(db, 7) == "running"


async def test_pause_false_for_non_current_job(db):
    w = _worker(db)
    _job(db, 7, "running")
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


async def test_run_ffmpeg_silences_libva_info_chatter(db, monkeypatch):
    """ffmpeg runs with LIBVA_MESSAGING_LEVEL=1 so the QSV/VAAPI driver only
    logs real errors, not its benign init handshake — without dropping the
    rest of the inherited environment."""
    w = _worker(db)
    captured: dict = {}

    class _Stream:
        async def readline(self):
            return b""           # immediate EOF -> pump loops exit at once

    class _Proc:
        def __init__(self):
            self.stdout = _Stream()
            self.stderr = _Stream()
            self.returncode = 0

        async def wait(self):
            return 0

    async def fake_exec(*args, **kwargs):
        captured["env"] = kwargs.get("env")
        return _Proc()

    monkeypatch.setattr(exporter.asyncio, "create_subprocess_exec", fake_exec)
    rc, err = await w._run_ffmpeg(7, ["-y", "out.mp4"], 1.0)

    assert rc == 0
    assert captured["env"]["LIBVA_MESSAGING_LEVEL"] == "1"
    assert "PATH" in captured["env"]          # parent env preserved


# --- _process: cancelled vs real failure ---

async def test_process_discards_cancelled_job_without_failing(db, monkeypatch):
    w = _worker(db)
    _job(db, 7, "running")

    async def cancelled(_job):
        raise exporter._ExportCancelled

    monkeypatch.setattr(w, "_run_job", cancelled)
    await w._process({"id": 7})
    # row is being deleted by the endpoint; must NOT be flipped to failed
    assert _state(db, 7) == "running"


async def test_process_marks_real_error_failed(db, monkeypatch):
    w = _worker(db)
    _job(db, 8, "running")

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


# --- event-loop responsiveness ---

async def test_pop_next_does_not_block_loop_while_db_busy(db):
    """_pop_next runs a write transaction; with the DB lock held by a
    worker thread it must wait off the loop, not freeze the server."""
    import asyncio
    import threading

    w = _worker(db)
    _job(db, 3, "queued")

    held = threading.Event()
    release = threading.Event()

    def _hold_lock():
        with db.write():
            held.set()
            release.wait(timeout=5.0)

    t = threading.Thread(target=_hold_lock, daemon=True)
    t.start()
    assert held.wait(timeout=5.0)

    ticks = 0

    async def _ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    async def _call_pop():
        res = w._pop_next()
        if asyncio.iscoroutine(res):
            res = await res
        return res

    tick_task = asyncio.create_task(_ticker())
    pop_task = asyncio.create_task(_call_pop())
    try:
        await asyncio.sleep(0.3)
        release.set()
        job = await asyncio.wait_for(pop_task, timeout=5.0)
    finally:
        tick_task.cancel()
        release.set()
        t.join(timeout=5.0)

    assert job is not None and job["id"] == 3
    assert ticks >= 5, f"event loop starved while DB was busy ({ticks} ticks)"


# --- shutdown ---

async def test_stop_unfreezes_and_kills_inflight_child(db):
    """A paused job's encoder is SIGSTOP'd; shutdown must SIGCONT it
    before killing or the frozen ffmpeg outlives the server. A plain
    running child must be killed too — stop() used to abandon it."""
    w = _worker(db)
    _job(db, 9, "paused")
    w._current_job_id = 9
    w._paused = True
    w._resume.clear()
    proc = _FakeProc()
    w._current_proc = proc

    await w.stop()

    assert signal.SIGCONT in proc.signals, "paused child never resumed"
    assert proc.killed, "in-flight child not killed on shutdown"
    assert w._resume.is_set(), "paused job left parked forever"
