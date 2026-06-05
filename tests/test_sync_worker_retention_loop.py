"""The sync worker enforces retention on its own periodic cadence,
independent of download activity.

Before this, ``retention.sweep`` only ran (a) once at startup and
(b) at the end of a download cycle that actually downloaded something
(gated behind ``did_any``). That left the archive over quota whenever
the camera was offline or had nothing new to download — it only got
cleaned again on restart. These tests pin the continuous behaviour:
a periodic loop that sweeps regardless of whether anything was
downloaded, using the current settings, and exits cleanly on stop.
"""
from __future__ import annotations

import asyncio
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
    sw._stop = asyncio.Event()
    sw.db = None
    return sw


def _snap(**over):
    base = dict(
        recordings="/r",
        retention_max_days=0,
        retention_disk_pct=0,
        retention_protect_ro=False,
        recordings_quota_gb=3000,
        import_path="",
    )
    base.update(over)
    return types.SimpleNamespace(**base)


async def test_run_retention_sweep_uses_current_settings(monkeypatch):
    """The periodic sweep must read the live snapshot and pass every
    retention rule through — in particular the GiB quota."""
    calls = []

    def fake_sweep(db, recordings, **kwargs):
        calls.append((recordings, kwargs))
        return {"deleted_time": 0, "deleted_disk": 0, "protected": 0, "bytes_freed": 0}

    monkeypatch.setattr("web.services.retention.sweep", fake_sweep)
    monkeypatch.setattr("web.services.retention.filesystem_used_pct", lambda r: None)

    sw = _make_worker(_snap(retention_protect_ro=True), _Hub())
    await sw._run_retention_sweep()

    assert len(calls) == 1
    recordings, kwargs = calls[0]
    assert recordings == "/r"
    assert kwargs["quota_gb"] == 3000
    assert kwargs["max_days"] == 0
    assert kwargs["disk_pct"] == 0
    assert kwargs["protect_ro"] is True


async def test_retention_loop_sweeps_without_any_download(monkeypatch):
    """The loop sweeps on its own cadence with no download cycle ever
    run, then exits when the worker is stopped."""
    calls = []

    def fake_sweep(db, recordings, **kwargs):
        calls.append(recordings)
        return {"deleted_time": 0, "deleted_disk": 0, "protected": 0, "bytes_freed": 0}

    monkeypatch.setattr("web.services.retention.sweep", fake_sweep)
    monkeypatch.setattr("web.services.retention.filesystem_used_pct", lambda r: None)

    sw = _make_worker(_snap(), _Hub())

    task = asyncio.create_task(sw._retention_loop())
    # Wait until the loop performs at least one sweep. No download
    # cycle was ever invoked — retention is fully independent.
    for _ in range(200):
        if calls:
            break
        await asyncio.sleep(0.005)
    sw._stop.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert calls  # swept at least once with zero downloads
