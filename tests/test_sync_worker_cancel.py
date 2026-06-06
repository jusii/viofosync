"""Sync worker treats a cancelled download as a pause, not a failure,
and logs the lifecycle transitions a user needs to make sense of the
Logs tab (paused/resumed/skip/abort, dashcam online<->offline).
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
import types

import viofosync_lib as vfs
from web.db import Database
from web.services import queue as q
from web.services.sync_worker import SyncWorker


class _Hub:
    def __init__(self):
        self.events = []

    async def broadcast(self, event):
        self.events.append(event)


def _bare_worker() -> SyncWorker:
    """A worker with just the control-plane attributes wired — enough
    to exercise the lifecycle methods without an event loop."""
    sw = SyncWorker.__new__(SyncWorker)
    sw._paused = threading.Event()
    sw._cancel_current = threading.Event()
    sw._kick = asyncio.Event()
    sw._stop = asyncio.Event()
    sw._backoff_idx = 0
    sw._loop = None
    sw._task = None
    sw._online = None
    sw._current_filename = None
    return sw


# ---- cancel is not a failure ----

async def test_download_one_cancel_resets_to_pending(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "v.db"))
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, state, enqueued_at, attempts, "
            " remote_size, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("X.MP4", "/DCIM", "pending", 0, 0, 1000, int(time.time())),
        )
        item_id = c.execute("SELECT id FROM download_queue").fetchone()["id"]

    item = q.next_pending(db)
    snap = types.SimpleNamespace(
        grouping="none",
        recordings=str(rec_dir),
        download_attempts=3,
        timeout=10,
        gps_extract=False,
        delete_after_download=False,
        max_attempts=5,
    )
    hub = _Hub()
    sw = _bare_worker()
    sw.db = db
    sw.hub = hub
    sw._provider = types.SimpleNamespace(get=lambda: snap)
    sw._active_address = "192.168.1.230"

    def cancelled(*a, **k):
        raise vfs.DownloadCancelled("Download cancelled")

    monkeypatch.setattr(vfs, "download_file_with", cancelled)

    ok = await sw._download_one(item)

    assert ok is False
    with db.conn() as c:
        row = dict(
            c.execute(
                "SELECT * FROM download_queue WHERE id=?", (item_id,)
            ).fetchone()
        )
    assert row["state"] == "pending"     # NOT failed
    assert row["attempts"] == 0          # mark_downloading +1, cancel -1
    assert row["last_error"] is None
    # The UI must not be told the item failed.
    assert not any(
        e.get("type") == "item_state_change" and e.get("state") == "failed"
        for e in hub.events
    )


# ---- a paused worker does no dashcam work ----

async def test_cycle_skipped_while_paused(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    hub = _Hub()
    sw = _bare_worker()
    sw.db = db
    sw.hub = hub
    sw._provider = types.SimpleNamespace(
        get=lambda: types.SimpleNamespace(recordings=str(tmp_path))
    )
    sw._paused.set()

    did = await sw._cycle()

    assert did is False
    # No probe, no listing, no broadcasts while paused.
    assert hub.events == []


# ---- lifecycle logging ----

def test_pause_resume_logged(caplog):
    sw = _bare_worker()
    with caplog.at_level(logging.INFO, logger="viofosync.sync_worker"):
        sw.pause()
        sw.resume()
    msgs = [r.getMessage().lower() for r in caplog.records]
    assert any("pause" in m for m in msgs)
    assert any("resum" in m for m in msgs)


def test_reachability_transitions_logged_once(caplog):
    sw = _bare_worker()
    with caplog.at_level(logging.INFO, logger="viofosync.sync_worker"):
        sw._note_reachability(True, "primary")
        sw._note_reachability(True, "primary")   # no-op, same state
        sw._note_reachability(False)
    msgs = [r.getMessage().lower() for r in caplog.records]
    assert sum("online" in m for m in msgs) == 1
    assert sum("offline" in m for m in msgs) == 1
