from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


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
        # Detach the live log handler so startup/runtime records can't
        # land in app_log after we seed, then wait for the drain to flush
        # whatever it already enqueued. We clear the table and reset the
        # AUTOINCREMENT sequence so the seeded rows are deterministically
        # ids 1..3 — the id/count assertions below depend on that.
        handler = getattr(c.app.state, "log_handler", None)
        if handler is not None:
            logging.getLogger().removeHandler(handler)

        def _row_count() -> int:
            with c.app.state.db.conn() as conn:
                return conn.execute(
                    "SELECT COUNT(*) FROM app_log"
                ).fetchone()[0]

        prev, stable = -1, 0
        for _ in range(100):  # up to ~2s for the async drain to settle
            n = _row_count()
            stable = stable + 1 if n == prev else 0
            if stable >= 3:
                break
            prev = n
            time.sleep(0.02)
        with c.app.state.db.write() as conn:
            conn.execute("DELETE FROM app_log")
            conn.execute(
                "DELETE FROM sqlite_sequence WHERE name = 'app_log'"
            )
        # Seed rows directly so the test does not depend on drain timing.
        with c.app.state.db.write() as conn:
            for r in [
                (1.0, 20, "INFO", "viofosync.scanner", "scan start"),
                (2.0, 30, "WARNING", "viofosync.sync_worker", "retry 1"),
                (3.0, 40, "ERROR", "viofosync.sync_worker", "boom"),
            ]:
                conn.execute(
                    "INSERT INTO app_log "
                    "(ts, levelno, level, logger, message) "
                    "VALUES (?, ?, ?, ?, ?)",
                    r,
                )
        yield c


def test_logs_requires_auth(tmp_config_dir, tmp_recordings_dir, monkeypatch):
    from web import app as app_mod
    from web import settings as settings_mod
    monkeypatch.setenv("VIOFOSYNC_RESTART_DISABLED", "1")
    settings_mod.reset_for_tests()
    with TestClient(app_mod.create_app()) as c:
        c.post("/setup", data={
            "address": "192.168.1.230",
            "password": "twelve-chars-min!",
            "confirm": "twelve-chars-min!",
        })
        c.cookies.clear()
        r = c.get("/api/logs")
    assert r.status_code == 401


def test_logs_default_warning_plus(authed_client):
    r = authed_client.get("/api/logs")
    assert r.status_code == 200
    msgs = [e["message"] for e in r.json()["entries"]]
    assert msgs == ["boom", "retry 1"]


def test_logs_level_info_includes_all(authed_client):
    r = authed_client.get("/api/logs?level=INFO")
    assert len(r.json()["entries"]) == 3


def test_logs_filter_logger_and_q(authed_client):
    r = authed_client.get("/api/logs?level=INFO&logger=scanner")
    assert [e["message"] for e in r.json()["entries"]] == ["scan start"]
    r = authed_client.get("/api/logs?level=INFO&q=retry")
    assert [e["message"] for e in r.json()["entries"]] == ["retry 1"]


def test_logs_before_pagination(authed_client):
    page1 = authed_client.get("/api/logs?level=INFO&limit=2").json()["entries"]
    assert [e["id"] for e in page1] == [3, 2]
    before = page1[-1]["id"]
    page2 = authed_client.get(
        f"/api/logs?level=INFO&limit=2&before={before}"
    ).json()["entries"]
    assert [e["id"] for e in page2] == [1]


def test_emitted_log_reaches_api(tmp_config_dir, tmp_recordings_dir, monkeypatch):
    """A real logging call after startup is persisted and served."""
    from web import app as app_mod
    from web import settings as settings_mod
    monkeypatch.setenv("VIOFOSYNC_RESTART_DISABLED", "1")
    settings_mod.reset_for_tests()
    with TestClient(app_mod.create_app()) as c:
        c.post("/setup", data={
            "address": "192.168.1.230",
            "password": "twelve-chars-min!",
            "confirm": "twelve-chars-min!",
        })
        logging.getLogger("viofosync.endtoend").warning("hello-from-test")
        found = []
        for _ in range(100):  # up to ~2s for the async drain
            time.sleep(0.02)
            r = c.get("/api/logs?level=INFO&q=hello-from-test")
            if r.status_code == 200 and r.json()["entries"]:
                found = r.json()["entries"]
                break
        assert found and found[0]["message"] == "hello-from-test"
        assert found[0]["logger"] == "viofosync.endtoend"
