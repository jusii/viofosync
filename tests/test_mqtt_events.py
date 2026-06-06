"""Regression tests for MQTT event emission (issues 1-2).

Issue 1: emit_queue_changed broadcasts queue state counts.
Issue 2: scanner.scan emits clip_indexed after indexing.
"""
from __future__ import annotations

import asyncio
import time

import pytest

# ---------------------------------------------------------------------------
# Issue 1: emit_queue_changed
# ---------------------------------------------------------------------------

def _make_db(tmp_path):
    from web.db import Database
    return Database(str(tmp_path / "v.db"))


class _FakeHub:
    """Minimal hub stand-in that records broadcast calls."""

    def __init__(self):
        self.broadcasts: list[dict] = []
        self.scheduled: list[dict] = []
        self.last_state: dict = {}

    async def broadcast(self, event: dict) -> None:
        self.broadcasts.append(event)

    def schedule_broadcast(self, loop, event: dict) -> None:
        self.scheduled.append(event)


@pytest.mark.asyncio
async def test_emit_queue_changed_from_async_context(tmp_path):
    """emit_queue_changed uses create_task when there is a running loop."""
    from web.services.queue import emit_queue_changed

    db = _make_db(tmp_path)
    hub = _FakeHub()

    now = int(time.time())
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, state, enqueued_at) VALUES (?,?,?,?)",
            ("a.MP4", "/DCIM", "pending", now),
        )
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, state, enqueued_at) VALUES (?,?,?,?)",
            ("b.MP4", "/DCIM", "failed", now),
        )

    emit_queue_changed(db, hub)
    # give the event loop a turn to run the created task
    await asyncio.sleep(0)

    assert len(hub.broadcasts) == 1
    ev = hub.broadcasts[0]
    assert ev["type"] == "queue_changed"
    assert ev["pending"] == 1
    assert ev["failed"] == 1
    assert ev["downloading"] == 0


def test_emit_queue_changed_noop_when_hub_none(tmp_path):
    """emit_queue_changed is a no-op when hub is None (safe default)."""
    from web.services.queue import emit_queue_changed
    db = _make_db(tmp_path)
    # Should not raise.
    emit_queue_changed(db, None)


def test_emit_queue_changed_uses_schedule_broadcast_from_thread(tmp_path):
    """When there's no running loop and loop is passed, schedule_broadcast fires."""
    from web.services.queue import emit_queue_changed

    db = _make_db(tmp_path)

    # Run emit_queue_changed from a plain thread context (no running loop).
    # We simulate this by calling it inside asyncio.run's shutdown gap;
    # the simplest approach is to use a fresh thread.
    result: list[dict] = []

    def _in_thread():
        # Allocate a loop but don't set it as running — mirrors executor thread.
        loop = asyncio.new_event_loop()
        try:
            hub_local = _FakeHub()
            emit_queue_changed(db, hub_local, loop=loop)
            result.extend(hub_local.scheduled)
        finally:
            loop.close()

    import threading
    t = threading.Thread(target=_in_thread)
    t.start()
    t.join(timeout=3.0)

    assert len(result) == 1
    assert result[0]["type"] == "queue_changed"


# ---------------------------------------------------------------------------
# Issue 2: scanner.scan emits clip_indexed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scan_emits_clip_indexed(tmp_path):
    """scanner.scan schedules clip_indexed after indexing (called from thread)."""
    from web.db import Database
    from web.services import scanner

    db = Database(str(tmp_path / "v.db"))
    hub = _FakeHub()

    # scan over an empty directory — total will be 0 but event must fire.
    # asyncio.to_thread runs scan on an executor thread, so get_running_loop()
    # raises inside scan — it falls back to schedule_broadcast with the loop.
    loop = asyncio.get_running_loop()
    n = await asyncio.to_thread(
        scanner.scan, db, str(tmp_path), "daily", hub, loop,
    )
    # give the loop a turn to drain any scheduled coroutines
    await asyncio.sleep(0)

    assert n == 0
    # scan runs on a thread → schedule_broadcast path is taken
    assert len(hub.scheduled) == 1
    ev = hub.scheduled[0]
    assert ev["type"] == "clip_indexed"
    assert ev["total"] == 0


@pytest.mark.asyncio
async def test_scan_no_event_when_hub_none(tmp_path):
    """scanner.scan with hub=None doesn't crash and returns count normally."""
    from web.db import Database
    from web.services import scanner

    db = Database(str(tmp_path / "v.db"))
    n = await asyncio.to_thread(
        scanner.scan, db, str(tmp_path), "daily",
    )
    assert n == 0  # empty dir


