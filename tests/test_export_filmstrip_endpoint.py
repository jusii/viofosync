"""Tests for GET /api/exports/{job_id}/filmstrip.jpg and DELETE cache cleanup."""
from __future__ import annotations

import os
from pathlib import Path

import pytest


class _FakeMqttService:
    def __init__(self, **kwargs): pass
    def start(self): pass
    async def stop(self): pass
    async def on_settings_changed(self, keys, snap): pass
    def get_status(self):
        return {"state": "idle", "detail": None, "last_published_at": None}


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
    monkeypatch.setattr("web.app.MqttService", _FakeMqttService)

    app = create_app()
    c = TestClient(app)
    c.__enter__()
    c.post("/api/auth/login", json={"password": "pwpwpwpwpwpwpwpw"})
    yield c
    c.__exit__(None, None, None)
    settings_mod.reset_for_tests()


def _insert_job(client, state, output_path):
    app = client.app
    with app.state.db.write() as c:
        cur = c.execute(
            "INSERT INTO export_jobs (type, clip_ids, state, created_at, "
            "output_path) VALUES ('join_front', '[1]', ?, 0, ?)",
            (state, output_path),
        )
        return cur.lastrowid


def test_list_jobs_reports_has_preview(logged_in_client, tmp_path):
    """The jobs list flags whether each done job's filmstrip is cached yet, so
    the UI can show a 'generating' placeholder until the strip exists."""
    from web.services import export_preview
    app = logged_in_client.app
    recordings = app.state.settings_provider.get().recordings
    done_with = _insert_job(logged_in_client, "done", str(tmp_path / "a.mp4"))
    done_without = _insert_job(logged_in_client, "done", str(tmp_path / "b.mp4"))
    running = _insert_job(logged_in_client, "running", None)
    _pv = Path(export_preview.preview_path(recordings, done_with))
    _pv.parent.mkdir(parents=True, exist_ok=True)
    _pv.write_bytes(
        b"\xff\xd8\xff\xd9"
    )

    jobs = {j["id"]: j for j in logged_in_client.get("/api/exports").json()["jobs"]}
    assert jobs[done_with]["has_preview"] is True
    assert jobs[done_without]["has_preview"] is False
    assert jobs[running]["has_preview"] is False


def test_filmstrip_jpg_streams_cached_sprite(logged_in_client, tmp_path):
    from pathlib import Path

    from web.services import export_preview
    app = logged_in_client.app
    recordings = app.state.settings_provider.get().recordings
    jid = _insert_job(logged_in_client, "done", str(tmp_path / "out.mp4"))
    _pv = Path(export_preview.preview_path(recordings, jid))
    _pv.parent.mkdir(parents=True, exist_ok=True)
    _pv.write_bytes(b"\xff\xd8\xff\xd9")
    r = logged_in_client.get(f"/api/exports/{jid}/filmstrip.jpg")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert "max-age" in r.headers.get("cache-control", "")


def test_filmstrip_jpg_never_generates_at_request_time(logged_in_client, tmp_path, monkeypatch):
    from web.services import export_preview

    async def _boom(*a, **k):
        raise AssertionError("endpoint must not generate previews")

    monkeypatch.setattr(export_preview, "ensure_export_preview", _boom)
    jid = _insert_job(logged_in_client, "done", str(tmp_path / "out.mp4"))
    # No cached sprite present -> must return placeholder WITHOUT calling ensure_*.
    r = logged_in_client.get(f"/api/exports/{jid}/filmstrip.jpg")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"


def test_filmstrip_jpg_placeholder_for_running_job(logged_in_client, tmp_path):
    jid = _insert_job(logged_in_client, "running", None)
    r = logged_in_client.get(f"/api/exports/{jid}/filmstrip.jpg")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"   # 1x1 placeholder


def test_filmstrip_jpg_placeholder_for_unknown_job(logged_in_client):
    r = logged_in_client.get("/api/exports/999999/filmstrip.jpg")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"


def test_delete_removes_cached_preview(logged_in_client, tmp_path, monkeypatch):
    from web.services import export_preview
    app = logged_in_client.app
    recordings = app.state.settings_provider.get().recordings
    jid = _insert_job(logged_in_client, "done", None)
    pv = export_preview.preview_path(recordings, jid)
    Path(pv).parent.mkdir(parents=True, exist_ok=True)
    Path(pv).write_bytes(b"\xff\xd8\xff\xd9")
    assert os.path.exists(pv)
    csrf = logged_in_client.get("/api/auth/csrf").json()["csrf"]
    r = logged_in_client.delete(
        f"/api/exports/{jid}", headers={"x-csrf-token": csrf}
    )
    assert r.status_code == 200
    assert not os.path.exists(pv)
