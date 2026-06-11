"""Detached background tasks must be strongly referenced until done.

asyncio keeps only a weak reference to a bare create_task result, so
a GC pass can cancel a still-running fire-and-forget task. spawn()
holds a reference until completion and logs (never swallows) a crash.
"""
from __future__ import annotations

import asyncio
import logging

from web.services import tasks as task_mod


async def test_spawn_tracks_then_releases():
    gate = asyncio.Event()

    async def _work():
        await gate.wait()

    t = task_mod.spawn(_work())
    assert t in task_mod._background, "task not held while running"
    gate.set()
    await t
    assert t not in task_mod._background, "reference not released after done"


async def test_spawn_survives_gc_pressure():
    import gc
    done = asyncio.Event()

    async def _work():
        await asyncio.sleep(0.05)
        done.set()

    task_mod.spawn(_work())  # deliberately keep no local reference
    gc.collect()             # would collect a bare create_task result
    await asyncio.wait_for(done.wait(), timeout=1.0)


async def test_spawn_logs_exception(caplog):
    async def _boom():
        raise ValueError("detached failure")

    with caplog.at_level(logging.ERROR):
        t = task_mod.spawn(_boom())
        await asyncio.gather(t, return_exceptions=True)

    assert any("detached failure" in r.message or "detached failure" in str(r.exc_info)
               for r in caplog.records), "detached task exception not logged"
    assert t not in task_mod._background
