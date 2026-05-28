"""Tests for GET /api/storage/usage."""
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
    yield c
    c.__exit__(None, None, None)
    settings_mod.reset_for_tests()


def test_usage_endpoint_requires_session(tmp_config_dir, tmp_recordings_dir,
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
        r = c.get("/api/storage/usage")
        assert r.status_code == 401


def test_usage_filesystem_mode_default(logged_in_client):
    """No quota set → reports against the filesystem."""
    r = logged_in_client.get("/api/storage/usage")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "filesystem"
    assert body["total_bytes"] > 0
    assert body["used_bytes"] >= 0
    assert 0 <= body["used_pct"] <= 100


def test_usage_quota_mode_reports_against_declared_quota(
    logged_in_client, tmp_recordings_dir,
):
    """Setting RECORDINGS_QUOTA_GB switches to quota mode."""
    from web import settings as settings_mod
    from web.services import retention

    rec = tmp_recordings_dir
    (rec / "clip.MP4").write_bytes(b"\0" * (2 << 20))
    retention._size_cache.clear()

    p = settings_mod.get_provider()
    p.update({"RECORDINGS_QUOTA_GB": 1}, actor="test")

    r = logged_in_client.get("/api/storage/usage")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "quota"
    assert body["total_bytes"] == 1 << 30
    # 2 MiB used → ~0.2% of a 1 GiB quota
    assert 0 < body["used_pct"] < 1


def test_usage_includes_threshold_when_set(
    tmp_config_dir, tmp_recordings_dir, monkeypatch,
):
    """Pre-seed the threshold in config.json BEFORE app startup so
    we don't fire the settings-change subscribers mid-test."""
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
    data["RETENTION_DISK_PCT"] = 80
    data["RETENTION_MAX_DAYS"] = 30
    p._store.write(data)
    settings_mod.reset_for_tests()

    monkeypatch.setattr(SyncWorker, "start", lambda self: None)

    with TestClient(create_app()) as c:
        c.post("/api/auth/login", json={"password": "pwpwpwpwpwpwpwpw"})
        body = c.get("/api/storage/usage").json()
    settings_mod.reset_for_tests()

    assert body["threshold_pct"] == 80
    assert body["max_days"] == 30


def test_usage_threshold_null_when_disabled(logged_in_client):
    # Defaults are 0 for both; the fixture doesn't change them.
    body = logged_in_client.get("/api/storage/usage").json()
    assert body["threshold_pct"] is None
    assert body["max_days"] is None
