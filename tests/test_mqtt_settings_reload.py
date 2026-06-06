"""Tests for MqttService.on_settings_changed."""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_on_settings_changed_restarts_on_connection_keys(monkeypatch):
    from web.services.mqtt import MqttService

    started = []
    stopped = []

    class S(MqttService):
        def start(self):
            started.append(1)
        async def stop(self):
            stopped.append(1)

    svc = S(db=None, provider=None, hub=None, app=None)
    # Stub provider snapshot
    svc._provider = type("P", (), {
        "get": staticmethod(lambda: type("Snap", (), {
            "mqtt_enabled": True, "mqtt_host": "h",
        })()),
    })()
    await svc.on_settings_changed({"MQTT_HOST"}, svc._provider.get())
    assert started and stopped


@pytest.mark.asyncio
async def test_on_settings_changed_skips_irrelevant_keys():
    from web.services.mqtt import MqttService
    started = []

    class S(MqttService):
        def start(self):
            started.append(1)
        async def stop(self):
            return

    svc = S(db=None, provider=None, hub=None, app=None)
    svc._provider = type("P", (), {
        "get": staticmethod(lambda: type("Snap", (), {
            "mqtt_enabled": True, "mqtt_host": "h",
        })()),
    })()
    await svc.on_settings_changed({"ADDRESS"}, svc._provider.get())
    assert started == []


@pytest.mark.asyncio
async def test_on_settings_changed_node_rename_emits_cleanup_publishes():
    """When MQTT_NODE_ID changes, the service publishes empty payloads
    to every old discovery topic before restarting."""
    from web.services.mqtt import MqttService

    cleared: list[str] = []

    class S(MqttService):
        def start(self): pass
        async def stop(self): pass
        async def _publish_now(self, topic, payload, retain, qos):
            cleared.append((topic, payload, retain))

    svc = S(db=None, provider=None, hub=None, app=None)
    svc._last_node_id = "viofosync"
    svc._last_discovery_prefix = "homeassistant"

    new_snap = type("Snap", (), {
        "mqtt_enabled": True, "mqtt_host": "h",
        "mqtt_node_id": "viofosync_garage",
        "mqtt_discovery_prefix": "homeassistant",
    })()
    svc._provider = type("P", (), {"get": staticmethod(lambda: new_snap)})()
    await svc.on_settings_changed({"MQTT_NODE_ID"}, new_snap)

    assert any("homeassistant/sensor/viofosync/queue_pending/config" in t
                for (t, _p, _r) in cleared)
    # Empty payload = HA delete signal
    for _t, p, retain in cleared:
        assert p == b""
        assert retain is True
