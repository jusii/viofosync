"""Fire-and-forget task helper.

asyncio holds only a weak reference to the object returned by
``create_task``, so a detached fire-and-forget task can be garbage
collected mid-flight and silently cancelled. ``spawn`` keeps a strong
reference until the task finishes and logs (rather than swallows) any
exception so a crash in a detached task is visible in the Logs tab.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Coroutine

log = logging.getLogger("viofosync.tasks")

# Strong references to in-flight detached tasks (discarded on done).
_background: set[asyncio.Task] = set()


def _on_done(task: asyncio.Task) -> None:
    _background.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("background task %r failed", task.get_name(), exc_info=exc)


def spawn(coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task:
    """Schedule ``coro`` on the running loop, retaining a reference to
    the task until it completes. Returns the task."""
    task = asyncio.ensure_future(coro)
    if name:
        task.set_name(name)
    _background.add(task)
    task.add_done_callback(_on_done)
    return task
