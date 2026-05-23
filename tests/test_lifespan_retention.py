"""Integration tests for the lifespan-level retention scheduling."""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient


def test_lifespan_creates_background_retention_task(
    tmp_config_dir: Path, tmp_recordings_dir: Path,
) -> None:
    """Startup must not block on the retention sweep — the sweep is
    launched as a background asyncio task and the lifespan yields
    immediately."""
    from web import app as app_mod
    from web import settings as settings_mod
    settings_mod.reset_for_tests()
    app = app_mod.create_app()

    with TestClient(app):
        assert hasattr(app.state, "retention_task"), (
            "lifespan must set app.state.retention_task; without it the "
            "startup retention sweep is still on the critical path"
        )
        assert isinstance(app.state.retention_task, asyncio.Task)


def test_lifespan_shutdown_cancels_retention_task() -> None:
    """The lifespan's `finally` block must explicitly cancel the
    background retention task, so a container stop isn't blocked by
    an in-flight sweep.

    Behavioural assertions on this don't work: TestClient cancels
    pending tasks during its own cleanup, so `task.cancelled()` ends
    up True regardless of whether the lifespan called `.cancel()` on
    `retention_task` or not. We inspect the lifespan source directly
    to confirm the cancellation is wired up.
    """
    import inspect

    from web import app as app_mod

    src = inspect.getsource(app_mod.lifespan)
    finally_idx = src.index("finally:")
    finally_block = src[finally_idx:]

    assert "retention_task" in finally_block, (
        "lifespan's finally block must cancel app.state.retention_task; "
        "otherwise a long-running sweep blocks container shutdown"
    )
    # Regression guard — the pre-existing initial_scan_task cancel must
    # stay in place when we extend the loop.
    assert "initial_scan_task" in finally_block, (
        "initial_scan_task cancellation must remain in the finally block"
    )
