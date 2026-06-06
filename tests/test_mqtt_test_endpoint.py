"""Test for POST /api/mqtt/test."""
from __future__ import annotations

import pytest


@pytest.fixture
def logged_in_client(tmp_config_dir, tmp_recordings_dir, monkeypatch):
    import bcrypt
    from fastapi.testclient import TestClient

    from web import settings as settings_mod
    from web.app import create_app
    from web.services.sync_worker import SyncWorker

    digest = bcrypt.hashpw(b"pw" * 8, bcrypt.gensalt()).decode()
    settings_mod.reset_for_tests()
    p = settings_mod.get_provider()
    data = p._store.load()
    data["WEB_PASSWORD_HASH"] = digest
    p._store.write(data)
    settings_mod.reset_for_tests()
    monkeypatch.setattr(SyncWorker, "start", lambda self: None)
    app = create_app()
    c = TestClient(app)
    c.__enter__()
    c.post("/api/auth/login", json={"password": "pwpwpwpwpwpwpwpw"})
    csrf = c.get("/api/auth/csrf").json()["csrf"]
    c.headers["X-CSRF-Token"] = csrf
    yield c
    c.__exit__(None, None, None)
    settings_mod.reset_for_tests()


def test_test_endpoint_validates_host(logged_in_client):
    r = logged_in_client.post(
        "/api/mqtt/test",
        json={"host": "", "port": 1883},
    )
    assert r.status_code == 400


def test_test_endpoint_returns_failure_for_unreachable_host(logged_in_client, monkeypatch):
    # Monkeypatch aiomqtt.Client to raise on connect.
    import aiomqtt

    class _Boom:
        def __init__(self, **_kw): pass
        async def __aenter__(self): raise aiomqtt.MqttError("connection refused")
        async def __aexit__(self, *a): pass
    monkeypatch.setattr(aiomqtt, "Client", _Boom)
    r = logged_in_client.post(
        "/api/mqtt/test",
        json={"host": "127.0.0.1", "port": 1},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "connection" in body["detail"].lower()


def test_test_endpoint_returns_success(logged_in_client, monkeypatch):
    import aiomqtt

    class _Ok:
        def __init__(self, **_kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
    monkeypatch.setattr(aiomqtt, "Client", _Ok)
    r = logged_in_client.post(
        "/api/mqtt/test",
        json={"host": "h", "port": 1883},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
