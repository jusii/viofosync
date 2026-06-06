"""Tests for queue.retry_failed."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def test_retry_failed_resets_state_and_attempts(tmp_path):
    from web.db import Database
    from web.services.queue import retry_failed
    db = Database(str(tmp_path / "v.db"))
    now = int(time.time())
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, state, enqueued_at, attempts, last_error) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("a.MP4", "/DCIM", "failed", now, 3, "boom"),
        )
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, state, enqueued_at, attempts) "
            "VALUES (?, ?, ?, ?, ?)",
            ("b.MP4", "/DCIM", "pending", now, 0),
        )
    n = retry_failed(db)
    assert n == 1
    with db.conn() as c:
        rows = {r["filename"]: dict(r) for r in c.execute(
            "SELECT * FROM download_queue"
        ).fetchall()}
    assert rows["a.MP4"]["state"] == "pending"
    assert rows["a.MP4"]["attempts"] == 0
    assert rows["a.MP4"]["last_error"] is None
    assert rows["b.MP4"]["state"] == "pending"  # untouched


def test_retry_failed_noop_when_no_failed_rows(tmp_path):
    from web.db import Database
    from web.services.queue import retry_failed
    db = Database(str(tmp_path / "v.db"))
    assert retry_failed(db) == 0


@pytest.fixture
def authed_client(tmp_config_dir: Path, tmp_recordings_dir: Path, monkeypatch):
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


def _seed_failed(client) -> None:
    now = int(time.time())
    with client.app.state.db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, state, enqueued_at, attempts, last_error) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("a.MP4", "/DCIM", "failed", now, 3, "boom"),
        )
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, state, enqueued_at, attempts, last_error) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("b.MP4", "/DCIM", "failed", now, 5, "kaboom"),
        )
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, state, enqueued_at, attempts) "
            "VALUES (?, ?, ?, ?, ?)",
            ("c.MP4", "/DCIM", "pending", now, 0),
        )


def _states(client) -> dict[str, str]:
    with client.app.state.db.conn() as c:
        return {
            r["filename"]: r["state"]
            for r in c.execute(
                "SELECT filename, state FROM download_queue"
            ).fetchall()
        }


def test_retry_endpoint_empty_body_retries_all_failed(authed_client) -> None:
    _seed_failed(authed_client)
    r = authed_client.post("/api/queue/retry", json={})
    assert r.status_code == 200
    assert r.json()["updated"] == 2
    states = _states(authed_client)
    assert states["a.MP4"] == "pending"
    assert states["b.MP4"] == "pending"
    assert states["c.MP4"] == "pending"  # already pending, untouched


def test_retry_endpoint_with_filenames_retries_only_those(authed_client) -> None:
    _seed_failed(authed_client)
    r = authed_client.post("/api/queue/retry", json={"filenames": ["a.MP4"]})
    assert r.status_code == 200
    assert r.json()["updated"] == 1
    states = _states(authed_client)
    assert states["a.MP4"] == "pending"
    assert states["b.MP4"] == "failed"  # not requested, still failed
