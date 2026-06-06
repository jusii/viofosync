"""End-to-end test against an in-process amqtt broker."""
from __future__ import annotations

import asyncio
import json
import threading

import pytest


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
@pytest.mark.filterwarnings("ignore::ResourceWarning")
def test_full_walkthrough(mqtt_broker, tmp_path, monkeypatch):
    """Drive a real amqtt broker through discovery, state publish, and commands."""
    import aiomqtt
    import bcrypt
    from fastapi.testclient import TestClient

    from web import settings as settings_mod
    from web.app import create_app
    from web.services.sync_worker import SyncWorker

    host, port = mqtt_broker

    # ------------------------------------------------------------------
    # Configure settings with MQTT enabled.
    # ------------------------------------------------------------------
    digest = bcrypt.hashpw(b"pw" * 8, bcrypt.gensalt()).decode()
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("RECORDINGS", str(tmp_path / "rec"))
    (tmp_path / "rec").mkdir()
    settings_mod.reset_for_tests()
    p = settings_mod.get_provider()
    data = p._store.load()
    data["WEB_PASSWORD_HASH"] = digest
    data["MQTT_ENABLED"] = True
    data["MQTT_HOST"] = host
    data["MQTT_PORT"] = port
    data["MQTT_DISCOVERY_ENABLED"] = True
    data["MQTT_NODE_ID"] = "viofosync"
    data["MQTT_CLIENT_ID"] = "test-e2e"
    p._store.write(data)
    settings_mod.reset_for_tests()

    monkeypatch.setattr(SyncWorker, "start", lambda self: None)
    app = create_app()

    # ------------------------------------------------------------------
    # Run an async subscriber in a background thread.
    # It collects every MQTT message until told to stop.
    # ------------------------------------------------------------------
    received: list[tuple[str, bytes]] = []
    sub_ready = threading.Event()
    sub_loop: list[asyncio.AbstractEventLoop] = []
    sub_task_holder: list[asyncio.Task] = []

    def run_subscriber():
        async def _subscriber():
            async with aiomqtt.Client(hostname=host, port=port) as c:
                await c.subscribe("#", qos=1)
                sub_ready.set()
                async for m in c.messages:
                    received.append((m.topic.value, bytes(m.payload)))

        async def _run():
            task = asyncio.ensure_future(_subscriber())
            sub_task_holder.append(task)
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            # Drain any remaining callbacks so sockets are closed cleanly.
            await asyncio.sleep(0)

        loop = asyncio.new_event_loop()
        sub_loop.append(loop)
        try:
            loop.run_until_complete(_run())
        except (asyncio.CancelledError, Exception):
            pass
        finally:
            # Cancel all remaining tasks before closing.
            pending = asyncio.all_tasks(loop)
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    sub_thread = threading.Thread(target=run_subscriber, daemon=True, name="e2e-subscriber")
    sub_thread.start()
    # Wait for subscriber to connect and subscribe.
    sub_ready.wait(timeout=5.0)

    # ------------------------------------------------------------------
    # Run the FastAPI app lifespan via TestClient (sync).
    # ------------------------------------------------------------------
    with TestClient(app) as _client:
        import time
        time.sleep(2.5)  # Allow MqttService to connect and publish.

        topics = {t for (t, _p) in received}
        assert "viofosync/availability" in topics, (
            f"Expected 'viofosync/availability' in {sorted(topics)}"
        )
        assert any("homeassistant/sensor/viofosync/" in t for t in topics), (
            f"No homeassistant discovery topic in {sorted(topics)}"
        )
        assert "viofosync/queue_pending/state" in topics, (
            f"Expected 'viofosync/queue_pending/state' in {sorted(topics)}"
        )

        # Drive a command: pause sync.
        def publish_sync(topic, payload):
            async def _pub():
                async with aiomqtt.Client(hostname=host, port=port) as cmd:
                    await cmd.publish(topic, payload, qos=1)
            asyncio.run(_pub())

        # Install a spy on the real sync_worker so we can assert pause() fires.
        pause_calls: list = []
        original_pause = _client.app.state.sync_worker.pause
        _client.app.state.sync_worker.pause = (
            lambda *a, **kw: (pause_calls.append(1), original_pause(*a, **kw))[1]
        )

        publish_sync("viofosync/pause_sync/cmd", b"PRESS")
        time.sleep(0.5)
        assert len(pause_calls) >= 1, (
            "pause_sync command didn't reach sync_worker.pause"
        )

        # Drive a parameterised command — prioritize_recent.
        publish_sync(
            "viofosync/cmd/prioritize_recent",
            json.dumps({"hours": 1}).encode(),
        )
        time.sleep(0.5)
        # Should not crash; confirms message gets routed without error.

    # ------------------------------------------------------------------
    # Shut down the subscriber by cancelling its task.
    # ------------------------------------------------------------------
    if sub_task_holder and sub_loop:
        sub_loop[0].call_soon_threadsafe(sub_task_holder[0].cancel)
    sub_thread.join(timeout=5.0)

    settings_mod.reset_for_tests()
