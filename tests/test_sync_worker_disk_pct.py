"""The sync worker broadcasts disk_pct so compute_sync_status can flip
to error when the disk is full.

The metric broadcast here is *filesystem* percentage — the OS-level
"how full is the disk we're writing to". It explicitly does NOT use
the quota-aware ``disk_used_pct`` because quota retention is designed
to keep the recordings dir *at* the quota, so the quota-aware metric
would read ~100% during normal operation and trigger a spurious
critical-disk error every cycle.
"""
from __future__ import annotations

import types

from web.services.sync_worker import SyncWorker


class _Hub:
    def __init__(self):
        self.events = []
    async def broadcast(self, event):
        self.events.append(event)


def _make_worker(snap, hub):
    sw = SyncWorker.__new__(SyncWorker)
    sw.hub = hub
    sw._provider = types.SimpleNamespace(get=lambda: snap)
    sw._loop = None
    return sw


async def test_emit_disk_pct_uses_filesystem_pct_not_quota(monkeypatch):
    """When a quota is configured, _emit_disk_pct MUST broadcast the
    filesystem percentage, not the quota percentage. Quota retention
    keeps the dir at quota by design — using quota % here would trip
    the critical-disk error perpetually."""
    snap = types.SimpleNamespace(
        recordings="/r",
        recordings_quota_gb=3100,  # quota IS set
    )
    hub = _Hub()
    sw = _make_worker(snap, hub)

    # Quota-aware reading would be ~97% (this should NOT be what we broadcast)
    monkeypatch.setattr(
        "web.services.retention.disk_used_pct",
        lambda recordings, quota_gb=0: 97.0,
    )
    # Filesystem reading: 87% (this is what we SHOULD broadcast)
    monkeypatch.setattr(
        "web.services.retention.filesystem_used_pct",
        lambda recordings: 87.0,
    )

    await sw._emit_disk_pct()
    assert hub.events == [{"type": "disk_pct", "pct": 87.0}]


async def test_emit_disk_pct_uses_filesystem_pct_when_no_quota(monkeypatch):
    """Without a quota, the filesystem percentage is still what we want.
    Same code path — the broadcast doesn't change behaviour based on quota."""
    snap = types.SimpleNamespace(
        recordings="/r",
        recordings_quota_gb=0,
    )
    hub = _Hub()
    sw = _make_worker(snap, hub)
    monkeypatch.setattr(
        "web.services.retention.filesystem_used_pct",
        lambda recordings: 42.5,
    )

    await sw._emit_disk_pct()
    assert hub.events == [{"type": "disk_pct", "pct": 42.5}]


async def test_emit_disk_pct_swallows_none(monkeypatch):
    """filesystem_used_pct returns None when the path is missing —
    don't emit a bogus event."""
    snap = types.SimpleNamespace(recordings="/missing", recordings_quota_gb=0)
    hub = _Hub()
    sw = _make_worker(snap, hub)
    monkeypatch.setattr(
        "web.services.retention.filesystem_used_pct",
        lambda recordings: None,
    )
    await sw._emit_disk_pct()
    assert hub.events == []
