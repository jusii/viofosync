"""SyncWorker lifecycle: bounded shutdown + settings-driven start/stop.

Two regressions pinned here:

- ``stop()`` awaited the run_coroutine_threadsafe future without any
  timeout (the wait_for branch was dead code), so a cycle stuck in an
  uncancellable executor call hung SIGTERM shutdown forever.
- Changing ADDRESS / ENABLE_SCHEDULED_SYNC at runtime neither started
  nor stopped the worker — only a restart applied them, despite the
  settings UI claiming they were applied.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
from unittest.mock import MagicMock

import bcrypt
import pytest
from fastapi.testclient import TestClient

from web import settings as settings_mod
from web.db import Database
from web.services import sync_worker as sw_mod
from web.services.hub import Hub
from web.services.sync_worker import SyncWorker


@pytest.fixture
def db(tmp_path) -> Database:
    return Database(str(tmp_path / "t.db"))


# ---- stop() must not hang on a stuck cycle ----

async def test_stop_returns_despite_stuck_cycle(db, monkeypatch):
    monkeypatch.setattr(sw_mod, "STOP_TIMEOUT", 0.2, raising=False)
    provider = MagicMock()
    worker = SyncWorker(db, provider, Hub())
    # Simulate a cycle wedged in an uncancellable executor call: a
    # future that never resolves.
    worker._task = concurrent.futures.Future()

    done = True
    try:
        await asyncio.wait_for(worker.stop(), timeout=2.0)
    except TimeoutError:
        done = False
    assert done, "stop() hung on a stuck cycle future"


# ---- start/stop decision from settings changes ----

def test_sync_worker_action_decision():
    from web.app import _sync_worker_action

    def snap(addr, enabled):
        s = MagicMock()
        s.address = addr
        s.enable_scheduled_sync = enabled
        return s

    # Irrelevant keys → no action.
    assert _sync_worker_action({"TZ"}, snap("1.2.3.4", True)) is None
    # Address set + sync enabled → start.
    assert _sync_worker_action({"ADDRESS"}, snap("1.2.3.4", True)) == "start"
    # Sync disabled → stop, regardless of address.
    assert _sync_worker_action(
        {"ENABLE_SCHEDULED_SYNC"}, snap("1.2.3.4", False)) == "stop"
    # Address cleared → stop.
    assert _sync_worker_action({"ADDRESS"}, snap(None, True)) == "stop"


# ---- integration: setting ADDRESS at runtime starts the worker ----

def test_setting_address_starts_worker(tmp_config_dir, tmp_recordings_dir,
                                       monkeypatch):
    from web.app import create_app

    class _FakeMqtt:
        def __init__(self, **kwargs): pass
        def start(self): pass
        async def stop(self): pass
        async def on_settings_changed(self, keys, snap): pass

    digest = bcrypt.hashpw(b"pw" * 8, bcrypt.gensalt()).decode()
    settings_mod.reset_for_tests()
    p = settings_mod.get_provider()
    data = p._store.load()
    data["WEB_PASSWORD_HASH"] = digest
    p._store.write(data)
    settings_mod.reset_for_tests()

    started: list = []
    monkeypatch.setattr(SyncWorker, "start",
                        lambda self: started.append(True))
    monkeypatch.setattr("web.app.MqttService", _FakeMqtt)

    app = create_app()
    with TestClient(app):
        assert started == []  # no ADDRESS at boot — worker idle
        provider = settings_mod.get_provider()
        provider.update({"ADDRESS": "192.0.2.5"}, actor="test")
        assert started == [True], \
            "setting ADDRESS at runtime did not start the sync worker"
    settings_mod.reset_for_tests()
