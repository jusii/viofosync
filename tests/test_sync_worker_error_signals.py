"""Sync worker emits stateful sync_error events for conditions that
prevent normal operation: recordings path unwritable, auth failures."""
from __future__ import annotations

import types

from web.services.sync_worker import SyncWorker


def _make_worker(snap, hub):
    sw = SyncWorker.__new__(SyncWorker)
    sw.hub = hub
    sw._provider = types.SimpleNamespace(get=lambda: snap)
    sw._loop = None
    sw._last_error_kind = None
    return sw


class _RecordingHub:
    def __init__(self):
        self.events = []
    async def broadcast(self, event):
        self.events.append(event)


async def test_check_recordings_unwritable_emits_error(tmp_path):
    bad = tmp_path / "nope"  # doesn't exist
    snap = types.SimpleNamespace(recordings=str(bad))
    hub = _RecordingHub()
    sw = _make_worker(snap, hub)
    ok = await sw._check_recordings_writable()
    assert ok is False
    assert hub.events == [{
        "type": "sync_error",
        "kind": "recordings_unwritable",
        "message": "recordings path not writable",
    }]


async def test_check_recordings_writable_clears_previous_error(tmp_path):
    snap = types.SimpleNamespace(recordings=str(tmp_path))
    hub = _RecordingHub()
    sw = _make_worker(snap, hub)
    # Seed a pretend previous error so we can check clearance.
    sw._last_error_kind = "recordings_unwritable"
    ok = await sw._check_recordings_writable()
    assert ok is True
    assert {"type": "sync_error", "kind": None, "message": None} in hub.events


async def test_check_recordings_writable_does_not_emit_when_already_clear(tmp_path):
    snap = types.SimpleNamespace(recordings=str(tmp_path))
    hub = _RecordingHub()
    sw = _make_worker(snap, hub)
    sw._last_error_kind = None
    ok = await sw._check_recordings_writable()
    assert ok is True
    # No event of any kind — the path was fine and nothing was wrong before.
    assert hub.events == []


async def test_listing_http_401_emits_auth_failure_error():
    import urllib.error

    hub = _RecordingHub()
    snap = types.SimpleNamespace(address="cam.local")
    sw = _make_worker(snap, hub)
    err = urllib.error.HTTPError("http://cam.local/", 401, "Unauthorized", {}, None)
    await sw._classify_listing_failure(err)
    assert hub.events == [{
        "type": "sync_error",
        "kind": "auth_failure",
        "message": "camera authentication failed",
    }]


async def test_listing_non_auth_error_does_not_set_auth_failure():
    import urllib.error
    hub = _RecordingHub()
    sw = _make_worker(types.SimpleNamespace(), hub)
    err = urllib.error.HTTPError("http://cam.local/", 500, "Server error", {}, None)
    await sw._classify_listing_failure(err)
    # Not an auth failure — no sync_error emitted.
    assert hub.events == []


async def test_clear_sync_error_emits_only_when_was_set():
    hub = _RecordingHub()
    sw = _make_worker(types.SimpleNamespace(), hub)
    sw._last_error_kind = "auth_failure"
    await sw._clear_sync_error()
    assert hub.events == [{"type": "sync_error", "kind": None, "message": None}]
    # Calling it again is a no-op.
    hub.events.clear()
    await sw._clear_sync_error()
    assert hub.events == []
