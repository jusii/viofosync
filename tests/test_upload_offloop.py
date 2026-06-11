"""The upload route must not block the event loop.

Its quota check (make_room_for) walks the whole archive in quota
mode, and the chunk writes hit a (typically NAS) volume — both used
to run synchronously inside the async handler, serialising the whole
server behind disk latency.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from starlette.requests import Request

from web.db import Database
from web.routers import imports as imports_router


def _fake_app(tmp_path) -> SimpleNamespace:
    snap = MagicMock()
    snap.recordings = str(tmp_path / "rec")
    snap.grouping = "daily"
    snap.import_path = None
    snap.retention_disk_pct = 0
    snap.recordings_quota_gb = 0
    snap.retention_protect_ro = True
    provider = MagicMock()
    provider.get.return_value = snap
    db = Database(str(tmp_path / "t.db"))
    return SimpleNamespace(
        state=SimpleNamespace(settings_provider=provider, db=db)
    )


def _request(app, name: str, body: bytes) -> Request:
    messages = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        return messages.pop(0)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/import/upload",
        "query_string": b"",
        "headers": [
            (b"x-import-path", name.encode()),
            (b"x-import-size", str(len(body)).encode()),
        ],
        "app": app,
    }
    return Request(scope, receive)


async def test_upload_does_not_block_loop(tmp_path, monkeypatch):
    app = _fake_app(tmp_path)

    def _slow_make_room(*args, **kwargs):
        time.sleep(0.3)  # simulate the quota-mode archive walk
        return True

    monkeypatch.setattr(
        imports_router._retention, "make_room_for", _slow_make_room
    )
    monkeypatch.setattr(
        imports_router._retention, "import_exclude_set",
        lambda *a, **k: set(),
    )

    ticks = 0

    async def _ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    t = asyncio.create_task(_ticker())
    try:
        res = await imports_router.upload(
            _request(app, "2026_0101_120000_0001F.MP4", b"x" * 1024)
        )
    finally:
        t.cancel()

    assert res["status"] == "imported"
    assert ticks >= 5, f"event loop starved during upload ({ticks} ticks)"
