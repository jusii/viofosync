"""queue_changed broadcasts must survive any caller context.

The prioritize/retry routes are sync ``def`` handlers running in the
threadpool — no running loop, no ``loop`` argument — and their
queue_changed events used to be silently dropped, leaving every
client's queue badges stale until the next worker cycle.
"""
from __future__ import annotations

import asyncio

import pytest

from web.db import Database
from web.services import queue as q
from web.services.hub import Hub


@pytest.fixture
def db(tmp_path) -> Database:
    return Database(str(tmp_path / "t.db"))


async def test_emit_from_threadpool_reaches_hub(db):
    hub = Hub()
    hub.bind_loop(asyncio.get_running_loop())

    received: list = []

    async def _spy(event):
        received.append(event)

    hub.broadcast = _spy  # type: ignore[assignment]

    # Threadpool context: no running loop, no loop kwarg — exactly
    # how the sync queue routes call it.
    await asyncio.to_thread(q.emit_queue_changed, db, hub)
    await asyncio.sleep(0.05)  # let the scheduled coroutine run

    assert any(e.get("type") == "queue_changed" for e in received), \
        "queue_changed dropped when emitted off-loop without a loop arg"


async def test_schedule_broadcast_falls_back_to_bound_loop():
    hub = Hub()
    hub.bind_loop(asyncio.get_running_loop())

    received: list = []

    async def _spy(event):
        received.append(event)

    hub.broadcast = _spy  # type: ignore[assignment]

    await asyncio.to_thread(
        hub.schedule_broadcast, None, {"type": "ping"}
    )
    await asyncio.sleep(0.05)

    assert received == [{"type": "ping"}]
