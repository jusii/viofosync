"""Status reporting on MqttService (in-process, no broker)."""
from __future__ import annotations


def test_initial_status_idle():
    from web.services.mqtt import ConnState, MqttService
    svc = MqttService(db=None, provider=None, hub=None, app=None)
    s = svc.get_status()
    assert s["state"] == ConnState.IDLE.value
    assert s["detail"] is None


def test_status_after_marking_connected():
    from web.services.mqtt import ConnState, MqttService
    svc = MqttService(db=None, provider=None, hub=None, app=None)
    svc._set_state(ConnState.CONNECTED, detail="broker:1883")
    s = svc.get_status()
    assert s["state"] == "connected"
    assert s["detail"] == "broker:1883"


def test_status_after_marking_error():
    from web.services.mqtt import ConnState, MqttService
    svc = MqttService(db=None, provider=None, hub=None, app=None)
    svc._set_state(ConnState.ERROR, detail="auth failed")
    s = svc.get_status()
    assert s["state"] == "error"
    assert s["detail"] == "auth failed"


def test_status_endpoint_requires_session(tmp_config_dir, tmp_recordings_dir,
                                            monkeypatch):
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
    with TestClient(app) as c:
        # No session → 401
        r = c.get("/api/mqtt/status")
        assert r.status_code == 401


def test_status_endpoint_returns_idle_when_disabled(
    tmp_config_dir, tmp_recordings_dir, monkeypatch,
):
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
    with TestClient(app) as c:
        # login
        c.post("/api/auth/login", json={"password": "pwpwpwpwpwpwpwpw"})
        r = c.get("/api/mqtt/status")
        assert r.status_code == 200
        body = r.json()
        # MQTT defaults to disabled, so state is idle
        assert body["state"] in ("idle", "disabled")
