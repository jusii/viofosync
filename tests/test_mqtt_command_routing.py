"""Command handler factory + routing tests."""
from __future__ import annotations

import types

import pytest


def _fake_app(tmp_path):
    from web.db import Database
    sw_calls = []

    class FakeSync:
        def resume(self): sw_calls.append("resume")
        def start(self): sw_calls.append("start")
        def kick(self): sw_calls.append("kick")
        def pause(self): sw_calls.append("pause")
        def skip_current(self): sw_calls.append("skip")
    fake_state = types.SimpleNamespace(
        sync_worker=FakeSync(),
        db=Database(str(tmp_path / "v.db")),
        hub=None,  # emit_queue_changed no-ops when hub is None
        settings_provider=types.SimpleNamespace(
            get=lambda: types.SimpleNamespace(
                recordings=str(tmp_path), grouping="daily",
            ),
        ),
    )
    fake_state.sync_worker_calls = sw_calls  # so the test can read them
    return types.SimpleNamespace(state=fake_state, version="0.2.0")


@pytest.mark.asyncio
async def test_start_sync_handler(tmp_path):
    from web.services.mqtt_topology import build_command_handlers
    app = _fake_app(tmp_path)
    handlers = build_command_handlers(app)
    await handlers["start_sync"](b"PRESS")
    assert app.state.sync_worker_calls == ["resume", "start", "kick"]


@pytest.mark.asyncio
async def test_pause_sync_handler(tmp_path):
    from web.services.mqtt_topology import build_command_handlers
    app = _fake_app(tmp_path)
    await build_command_handlers(app)["pause_sync"](b"PRESS")
    assert app.state.sync_worker_calls == ["pause"]


@pytest.mark.asyncio
async def test_skip_current_handler(tmp_path):
    from web.services.mqtt_topology import build_command_handlers
    app = _fake_app(tmp_path)
    await build_command_handlers(app)["skip_current"](b"PRESS")
    assert app.state.sync_worker_calls == ["skip"]


@pytest.mark.asyncio
async def test_refresh_queue_handler(tmp_path):
    from web.services.mqtt_topology import build_command_handlers
    app = _fake_app(tmp_path)
    await build_command_handlers(app)["refresh_queue"](b"PRESS")
    assert app.state.sync_worker_calls == ["kick"]


@pytest.mark.asyncio
async def test_retry_failed_handler(tmp_path):
    import time as _t

    from web.services.mqtt_topology import build_command_handlers
    app = _fake_app(tmp_path)
    with app.state.db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, state, enqueued_at, attempts) "
            "VALUES (?, ?, ?, ?, ?)",
            ("a.MP4", "/DCIM", "failed", int(_t.time()), 2),
        )
    await build_command_handlers(app)["retry_failed"](b"PRESS")
    with app.state.db.conn() as c:
        state = c.execute(
            "SELECT state FROM download_queue"
        ).fetchone()["state"]
    assert state == "pending"
    assert app.state.sync_worker_calls == ["kick"]


@pytest.mark.asyncio
async def test_rescan_archive_handler(tmp_path, monkeypatch):
    from web.services import scanner
    from web.services.mqtt_topology import build_command_handlers
    calls = []
    monkeypatch.setattr(scanner, "scan",
                         lambda db, dest, grouping, *a, **kw: calls.append((dest, grouping)) or 0)
    app = _fake_app(tmp_path)
    await build_command_handlers(app)["rescan_archive"](b"PRESS")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_prioritize_recent_valid_payload(tmp_path):
    import json
    import time as _t

    from web.services.mqtt_topology import build_command_handlers
    app = _fake_app(tmp_path)
    now = int(_t.time())
    with app.state.db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, state, enqueued_at, recorded_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("recent.MP4", "/DCIM", "pending", now, now - 60),
        )
    await build_command_handlers(app)["prioritize_recent"](
        json.dumps({"hours": 0.5}).encode()
    )
    with app.state.db.conn() as c:
        prio = c.execute(
            "SELECT priority FROM download_queue WHERE filename='recent.MP4'"
        ).fetchone()["priority"]
    assert prio > 0
    assert app.state.sync_worker_calls == ["kick"]


@pytest.mark.asyncio
async def test_prioritize_recent_rejects_bad_json(tmp_path, caplog):
    from web.services.mqtt_topology import build_command_handlers
    app = _fake_app(tmp_path)
    # Should not raise.
    await build_command_handlers(app)["prioritize_recent"](b"not json")
    # No worker call, no priority change.
    assert app.state.sync_worker_calls == []


@pytest.mark.asyncio
async def test_prioritize_recent_rejects_out_of_range(tmp_path):
    import json

    from web.services.mqtt_topology import build_command_handlers
    app = _fake_app(tmp_path)
    await build_command_handlers(app)["prioritize_recent"](
        json.dumps({"hours": 0}).encode()
    )
    await build_command_handlers(app)["prioritize_recent"](
        json.dumps({"hours": 200}).encode()
    )
    assert app.state.sync_worker_calls == []
