"""Settings subscribers must be removable, and the app lifespan must
not leak them across runs.

The provider is a module-level singleton; each lifespan subscribed
auth/sync/mqtt callbacks and never unsubscribed, so repeated lifespans
(tests, uvicorn reload) accumulated callbacks pinning dead app objects.
"""
from __future__ import annotations

import bcrypt
from fastapi.testclient import TestClient

from web import settings as settings_mod


def test_subscribe_returns_working_unsubscribe(tmp_config_dir, tmp_recordings_dir):
    settings_mod.reset_for_tests()
    p = settings_mod.get_provider()
    seen = []
    unsub = p.subscribe(lambda keys, snap: seen.append(keys))

    p.update({"GROUPING": "weekly"}, actor="t")
    assert len(seen) == 1

    unsub()
    p.update({"GROUPING": "daily"}, actor="t")
    assert len(seen) == 1, "callback still fired after unsubscribe"


def test_lifespan_unsubscribes_on_shutdown(tmp_config_dir, tmp_recordings_dir,
                                           monkeypatch):
    from web.app import create_app
    from web.services.sync_worker import SyncWorker

    class _FakeMqtt:
        def __init__(self, **kw): pass
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
    monkeypatch.setattr(SyncWorker, "start", lambda self: None)
    monkeypatch.setattr("web.app.MqttService", _FakeMqtt)

    provider = settings_mod.get_provider()
    baseline = len(provider._subscribers)

    for _ in range(3):
        app = create_app()
        with TestClient(app):
            pass  # enter + exit one full lifespan

    assert len(provider._subscribers) == baseline, (
        f"subscribers leaked across lifespans: "
        f"{len(provider._subscribers)} vs baseline {baseline}"
    )
