"""Tests for GET /api/archive/day/{date}/route aggregation caching."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from web.routers import archive


@pytest.fixture
def authed_client(tmp_config_dir, tmp_recordings_dir, monkeypatch):
    from web import app as app_mod
    from web import settings as settings_mod
    monkeypatch.setenv("VIOFOSYNC_RESTART_DISABLED", "1")
    settings_mod.reset_for_tests()
    application = app_mod.create_app()
    with TestClient(application) as c:
        c.post("/setup", data={
            "address": "192.168.1.230",
            "password": "twelve-chars-min!",
            "confirm": "twelve-chars-min!",
        })
        csrf = c.get("/api/auth/csrf").json()["csrf"]
        c.headers.update({"x-csrf-token": csrf})
        yield c


def _add_gpx_clip(app, rec: Path, clip_id: int, date: str = "2026-06-02") -> str:
    day_dir = rec / date
    day_dir.mkdir(parents=True, exist_ok=True)
    mp4 = day_dir / f"{clip_id}.MP4"
    mp4.write_bytes(b"\0")
    gpx = day_dir / f"{clip_id}.MP4.gpx"
    gpx.write_text("<gpx></gpx>")
    with app.state.db.write() as c:
        c.execute(
            "INSERT INTO clip_index "
            "(id, path, basename, group_name, timestamp, camera, sequence, "
            " event_type, has_gpx, gps_examined, scanned_at) "
            "VALUES (?,?,?,?,?,?,?,?,1,0,?)",
            (clip_id, str(mp4), mp4.name, date, 1_717_312_440 + clip_id,
             "F", clip_id, "normal", 1_717_312_440),
        )
    return str(gpx)


def _counting_aggregate(calls):
    def _agg(paths):
        calls["n"] += 1
        return [], [], []
    return _agg


def test_route_aggregation_is_cached(authed_client, tmp_recordings_dir, monkeypatch):
    app = authed_client.app
    _add_gpx_clip(app, tmp_recordings_dir, 1)
    calls = {"n": 0}
    monkeypatch.setattr(archive.gps_service, "aggregate_day",
                        _counting_aggregate(calls))

    r1 = authed_client.get("/api/archive/day/2026-06-02/route")
    r2 = authed_client.get("/api/archive/day/2026-06-02/route")
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json() == r2.json()
    assert calls["n"] == 1   # second request served from cache


def test_route_cache_busts_when_gpx_changes(
    authed_client, tmp_recordings_dir, monkeypatch
):
    app = authed_client.app
    gpx = _add_gpx_clip(app, tmp_recordings_dir, 1)
    calls = {"n": 0}
    monkeypatch.setattr(archive.gps_service, "aggregate_day",
                        _counting_aggregate(calls))

    authed_client.get("/api/archive/day/2026-06-02/route")
    Path(gpx).write_text("<gpx>changed-and-larger</gpx>")
    st = Path(gpx).stat()
    os.utime(gpx, (st.st_atime, st.st_mtime + 10))
    authed_client.get("/api/archive/day/2026-06-02/route")
    assert calls["n"] == 2   # changed GPX -> recomputed
