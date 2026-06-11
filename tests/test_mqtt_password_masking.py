"""MQTT_PASSWORD must be write-only over the settings API.

GET /api/settings used to return the broker password in cleartext to
anything that could read one authenticated GET. It is now masked with
a sentinel; submitting the sentinel back means "unchanged", and the
Test-connection endpoint substitutes the stored password when handed
the sentinel.
"""
from __future__ import annotations

import bcrypt
import pytest
from fastapi.testclient import TestClient

from web import settings as settings_mod
from web.settings_schema import MASKED_SECRET


@pytest.fixture
def client(tmp_config_dir, tmp_recordings_dir, monkeypatch):
    from web.app import create_app
    from web.services.sync_worker import SyncWorker

    digest = bcrypt.hashpw(b"pw" * 8, bcrypt.gensalt()).decode()
    settings_mod.reset_for_tests()
    p = settings_mod.get_provider()
    data = p._store.load()
    data["WEB_PASSWORD_HASH"] = digest
    data["MQTT_PASSWORD"] = "s3cr3t-broker-pw"
    p._store.write(data)
    settings_mod.reset_for_tests()
    monkeypatch.setattr(SyncWorker, "start", lambda self: None)

    class _FakeMqtt:
        def __init__(self, **kw): pass
        def start(self): pass
        async def stop(self): pass
        async def on_settings_changed(self, keys, snap): pass
        def get_status(self): return {"state": "idle", "detail": None,
                                       "last_published_at": None}

    monkeypatch.setattr("web.app.MqttService", _FakeMqtt)
    app = create_app()
    c = TestClient(app)
    c.__enter__()
    c.post("/api/auth/login", json={"password": "pwpwpwpwpwpwpwpw"})
    csrf = c.get("/api/auth/csrf").json()["csrf"]
    c.headers["X-CSRF-Token"] = csrf
    yield c
    c.__exit__(None, None, None)
    settings_mod.reset_for_tests()


def test_get_masks_existing_password(client):
    body = client.get("/api/settings").json()
    assert body["editable"]["MQTT_PASSWORD"] == MASKED_SECRET
    assert "s3cr3t-broker-pw" not in str(body)


def test_put_sentinel_leaves_password_unchanged(client):
    r = client.put("/api/settings", json={
        "MQTT_PASSWORD": MASKED_SECRET, "MQTT_USERNAME": "newuser",
    })
    assert r.status_code == 200
    snap = settings_mod.get_provider().get()
    assert snap.mqtt_password == "s3cr3t-broker-pw"  # untouched
    assert snap.mqtt_username == "newuser"


def test_put_real_value_updates_password(client):
    r = client.put("/api/settings", json={"MQTT_PASSWORD": "brand-new-pw"})
    assert r.status_code == 200
    assert settings_mod.get_provider().get().mqtt_password == "brand-new-pw"


def test_put_empty_string_clears_password(client):
    r = client.put("/api/settings", json={"MQTT_PASSWORD": ""})
    assert r.status_code == 200
    assert settings_mod.get_provider().get().mqtt_password == ""


def test_blank_password_is_not_masked(client):
    settings_mod.get_provider().update({"MQTT_PASSWORD": ""}, actor="t")
    body = client.get("/api/settings").json()
    assert body["editable"]["MQTT_PASSWORD"] == ""


def test_test_endpoint_substitutes_stored_password_for_sentinel(client, monkeypatch):
    captured = {}
    import aiomqtt

    class _Spy:
        def __init__(self, **kw):
            captured.update(kw)
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    monkeypatch.setattr(aiomqtt, "Client", _Spy)
    r = client.post("/api/mqtt/test", json={
        "host": "broker.local", "password": MASKED_SECRET,
    })
    assert r.status_code == 200
    assert captured["password"] == "s3cr3t-broker-pw", \
        "test endpoint did not substitute the stored password for the sentinel"
