"""End-to-end test for the export-download filename.

Exercises GET /api/exports/{id}/download through FastAPI and
asserts the Content-Disposition carries the friendly name derived
from the source clips (date range + camera + count), with a
graceful fallback to the legacy name when the clips are gone.
"""
from __future__ import annotations

import datetime as _dt

import pytest

from web.services.naming import export_download_name


class _FakeMqttService:
    def __init__(self, **kwargs):
        self._last_node_id = ""
        self._last_discovery_prefix = ""

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


def _ts(y, mo, d, h, mi) -> int:
    return int(_dt.datetime(y, mo, d, h, mi).timestamp())


def _insert_clip(db, clip_id, ts, camera, path):
    with db.write() as c:
        c.execute(
            "INSERT INTO clip_index "
            "(id, path, basename, group_name, timestamp, camera, "
            " sequence, event_type, has_gpx, gps_examined, scanned_at) "
            "VALUES (?,?,?,?,?,?,?,?,0,0,?)",
            (clip_id, path, path.split("/")[-1], "2024-03-15", ts,
             camera, clip_id, "normal", ts),
        )


def _insert_job(db, job_type, clip_ids, output_path):
    import json
    with db.write() as c:
        cur = c.execute(
            "INSERT INTO export_jobs "
            "(type, clip_ids, state, output_path, created_at, "
            " finished_at) VALUES (?,?, 'done', ?, ?, ?)",
            (job_type, json.dumps({"clip_ids": clip_ids,
                                   "encoder": "software"}),
             output_path, 1, 2),
        )
        return cur.lastrowid


def test_download_uses_derived_filename(logged_in_client,
                                        tmp_recordings_dir):
    db = logged_in_client.app.state.db
    out = tmp_recordings_dir / "1.mp4"
    out.write_bytes(b"\0" * 1024)

    ts1 = _ts(2024, 3, 15, 14, 30)
    ts2 = _ts(2024, 3, 15, 15, 2)
    _insert_clip(db, 1, ts1, "F", "/rec/2024_0315_143000_0001F.MP4")
    _insert_clip(db, 2, ts2, "F", "/rec/2024_0315_150200_0001F.MP4")
    job_id = _insert_job(db, "join_front", [1, 2], str(out))

    r = logged_in_client.get(
        f"/api/exports/{job_id}/download", follow_redirects=True
    )
    assert r.status_code == 200
    expected = export_download_name(
        "join_front",
        [{"timestamp": ts1}, {"timestamp": ts2}],
        job_id,
    )
    # Sanity: the helper really produced the rich name, not fallback.
    assert expected == "2024-03-15_1430-1502_front_2clips.mp4"
    assert expected in r.headers["content-disposition"]


def _insert_job_with_range(db, job_type, clip_ids, clip_start, clip_end):
    import json
    with db.write() as c:
        cur = c.execute(
            "INSERT INTO export_jobs "
            "(type, clip_ids, state, created_at, clip_start, clip_end) "
            "VALUES (?,?, 'done', 1, ?, ?)",
            (job_type, json.dumps({"clip_ids": clip_ids,
                                   "encoder": "software"}),
             clip_start, clip_end),
        )
        return cur.lastrowid


def test_list_jobs_returns_clip_range_and_count(logged_in_client):
    """GET /api/exports surfaces the stored footage date range and a
    clip count derived from clip_ids, so the UI can render the
    'Footage' column without per-row clip lookups."""
    db = logged_in_client.app.state.db
    ts1 = _ts(2024, 3, 15, 14, 30)
    ts2 = _ts(2024, 3, 15, 15, 2)
    job_id = _insert_job_with_range(db, "join_front", [1, 2], ts1, ts2)

    r = logged_in_client.get("/api/exports")
    assert r.status_code == 200
    job = next(j for j in r.json()["jobs"] if j["id"] == job_id)
    assert job["clip_start"] == ts1
    assert job["clip_end"] == ts2
    assert job["clip_count"] == 2


def test_download_falls_back_when_clips_pruned(logged_in_client,
                                               tmp_recordings_dir):
    """A done job whose source clips were retention-pruned still
    downloads, under the legacy name."""
    db = logged_in_client.app.state.db
    out = tmp_recordings_dir / "9.mp4"
    out.write_bytes(b"\0" * 1024)
    # clip ids 90/91 are never inserted into clip_index.
    job_id = _insert_job(db, "pip", [90, 91], str(out))

    r = logged_in_client.get(
        f"/api/exports/{job_id}/download", follow_redirects=True
    )
    assert r.status_code == 200
    assert (
        f"viofosync_export_{job_id}.mp4"
        in r.headers["content-disposition"]
    )
