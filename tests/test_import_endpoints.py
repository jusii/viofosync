"""Import endpoint tests (auth, scan, ingest, upload)."""
from __future__ import annotations

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
def client(tmp_config_dir, tmp_recordings_dir, monkeypatch):
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
    # CSRF: token comes from GET /api/auth/csrf and rides the
    # x-csrf-token header (bound to the session cookie), not a cookie.
    csrf = c.get("/api/auth/csrf").json()["csrf"]
    c.headers.update({"x-csrf-token": csrf})
    yield c, Path(tmp_recordings_dir)
    c.__exit__(None, None, None)
    settings_mod.reset_for_tests()


def test_scan_requires_session(tmp_config_dir, tmp_recordings_dir, monkeypatch):
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
    with TestClient(app) as c:
        r = c.post("/api/import/scan", json={})
        assert r.status_code in (401, 403)
    settings_mod.reset_for_tests()


def test_scan_lists_recognised_and_skipped(client):
    c, rec = client
    card = rec / "import" / "DCIM"
    card.mkdir(parents=True)
    (card / "2026_0101_080000_0001F.MP4").write_bytes(b"a" * 10)
    (card / "junk.bin").write_bytes(b"z")
    r = c.post("/api/import/scan", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["total_bytes"] == 10
    assert [it["basename"] for it in body["recognised"]] == [
        "2026_0101_080000_0001F.MP4"]
    # Counts only — the endpoint must not leak skipped filenames.
    assert body["skipped_count"] == 1
    assert "skipped" not in body


def test_present_reports_clips_already_in_archive(client):
    c, rec = client
    here = "2026_0101_080000_0001F.MP4"     # complete copy -> present
    partial = "2026_0102_090000_0002R.MP4"  # archive smaller -> redo -> absent
    gone = "2026_0103_100000_0003F.MP4"     # not imported -> absent
    (rec / "2026-01-01").mkdir()
    (rec / "2026-01-01" / here).write_bytes(b"a" * 10)
    (rec / "2026-01-02").mkdir()
    (rec / "2026-01-02" / partial).write_bytes(b"a" * 3)
    r = c.post("/api/import/present", json={"files": [
        {"name": here, "size": 10},
        {"name": partial, "size": 10},
        {"name": gone, "size": 10},
    ]})
    assert r.status_code == 200
    assert r.json()["present"] == [here]


def test_scan_marks_present_clips(client):
    c, rec = client
    card = rec / "import" / "DCIM"
    card.mkdir(parents=True)
    here = "2026_0101_080000_0001F.MP4"     # already archived, full size
    partial = "2026_0102_090000_0002R.MP4"  # archived but truncated
    fresh = "2026_0103_100000_0003F.MP4"    # not in archive
    (card / here).write_bytes(b"a" * 10)
    (card / partial).write_bytes(b"b" * 10)
    (card / fresh).write_bytes(b"c" * 10)
    (rec / "2026-01-01").mkdir()
    (rec / "2026-01-01" / here).write_bytes(b"a" * 10)
    (rec / "2026-01-02").mkdir()
    (rec / "2026-01-02" / partial).write_bytes(b"b" * 4)
    r = c.post("/api/import/scan", json={})
    assert r.status_code == 200
    present = {it["basename"]: it["present"] for it in r.json()["recognised"]}
    assert present == {here: True, partial: False, fresh: False}


def test_scan_bad_path_400(client):
    c, rec = client
    r = c.post("/api/import/scan", json={"path": str(rec / "nope")})
    assert r.status_code == 400


def test_upload_writes_clip_into_archive(client):
    c, rec = client
    name = "2026_0101_080000_0001F.MP4"
    r = c.post(
        "/api/import/upload",
        content=b"a" * 12,
        headers={
            "X-Import-Path": f"DCIM/Movie/{name}",
            "X-Import-Size": "12",
            "Content-Type": "application/octet-stream",
        },
    )
    assert r.status_code == 200
    assert r.json()["status"] == "imported"
    assert (rec / "2026-01-01" / name).exists()


def test_upload_rejects_non_viofo_name(client):
    c, rec = client
    r = c.post(
        "/api/import/upload",
        content=b"x",
        headers={"X-Import-Path": "DCIM/whatever.mp4", "X-Import-Size": "1"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "not_recognised"


def test_ingest_bad_path_400(client):
    c, rec = client
    r = c.post("/api/import/ingest", json={"path": str(rec / "nope")})
    assert r.status_code == 400


def test_ingest_409_when_already_running(client):
    c, rec = client
    c.app.state.import_running = True
    try:
        r = c.post("/api/import/ingest", json={})
        assert r.status_code == 409
    finally:
        c.app.state.import_running = False
