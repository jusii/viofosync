"""Discovery payload builder tests."""
from __future__ import annotations


def _cfg(**overrides):
    base = {
        "discovery_prefix": "homeassistant",
        "node_id": "viofosync",
        "version": "0.2.0",
        "configuration_url": "http://host:8080/",
    }
    base.update(overrides)
    return base


def test_state_topic():
    from web.services.mqtt_topology import build_state_topic
    assert build_state_topic("queue_pending", _cfg()) == (
        "viofosync/queue_pending/state"
    )


def test_command_topic():
    from web.services.mqtt_topology import build_command_topic
    assert build_command_topic("pause_sync", _cfg()) == (
        "viofosync/pause_sync/cmd"
    )


def test_discovery_topic_sensor():
    from web.services.mqtt_topology import build_discovery_topic
    assert build_discovery_topic(
        "sensor", "queue_pending", _cfg(),
    ) == "homeassistant/sensor/viofosync/queue_pending/config"


def test_discovery_topic_button():
    from web.services.mqtt_topology import build_discovery_topic
    assert build_discovery_topic(
        "button", "pause_sync", _cfg(),
    ) == "homeassistant/button/viofosync/pause_sync/config"


def test_availability_topic():
    from web.services.mqtt_topology import build_availability_topic
    assert build_availability_topic(_cfg()) == "viofosync/availability"


def test_unique_id_includes_node_id():
    from web.services.mqtt_topology import build_unique_id
    assert build_unique_id("queue_pending", _cfg()) == (
        "viofosync_viofosync_queue_pending"
    )
    assert build_unique_id("queue_pending", _cfg(node_id="garage")) == (
        "viofosync_garage_queue_pending"
    )


def test_discovery_payload_for_sensor():
    from web.services.mqtt_topology import (
        EntityDef,
        build_discovery_payload,
    )
    entity = EntityDef(
        object_id="queue_pending",
        component="sensor",
        name="Queue pending",
        icon="mdi:download",
        device_class=None,
        state_class="measurement",
        unit_of_measurement=None,
        enabled_by_default=True,
        min_publish_interval_s=1.0,
        state_fn=None,
        command_handler=None,
        affected_by_hub_events=(),
    )
    payload = build_discovery_payload(entity, _cfg())
    assert payload["name"] == "Queue pending"
    assert payload["unique_id"] == "viofosync_viofosync_queue_pending"
    assert payload["state_topic"] == "viofosync/queue_pending/state"
    assert payload["availability_topic"] == "viofosync/availability"
    assert payload["payload_available"] == "online"
    assert payload["payload_not_available"] == "offline"
    assert payload["state_class"] == "measurement"
    assert payload["icon"] == "mdi:download"
    assert payload["enabled_by_default"] is True
    assert "command_topic" not in payload
    # device manifest is present and references the node_id
    assert payload["device"]["identifiers"] == ["viofosync_viofosync"]
    assert payload["device"]["sw_version"] == "0.2.0"


def test_discovery_payload_for_button():
    from web.services.mqtt_topology import (
        EntityDef,
        build_discovery_payload,
    )
    async def _h(_p): ...
    entity = EntityDef(
        object_id="pause_sync",
        component="button",
        name="Pause sync",
        icon="mdi:pause",
        device_class=None,
        state_class=None,
        unit_of_measurement=None,
        enabled_by_default=True,
        min_publish_interval_s=0.0,
        state_fn=None,
        command_handler=_h,
        affected_by_hub_events=(),
    )
    payload = build_discovery_payload(entity, _cfg())
    assert payload["command_topic"] == "viofosync/pause_sync/cmd"
    # Buttons don't have a state_topic in HA discovery
    assert "state_topic" not in payload


def test_device_manifest_omits_empty_configuration_url():
    """HA's MQTT discovery rejects the entire message with
    'invalid url for dictionary value' when configuration_url is
    present but empty. The builder must omit the key in that case."""
    from web.services.mqtt_topology import (
        EntityDef,
        build_discovery_payload,
    )
    entity = EntityDef(
        object_id="queue_pending", component="sensor",
        name="Queue pending",
        icon=None, device_class=None, state_class="measurement",
        unit_of_measurement=None,
        enabled_by_default=True, min_publish_interval_s=1.0,
        state_fn=None, command_handler=None,
        affected_by_hub_events=(),
    )
    payload = build_discovery_payload(entity, _cfg(configuration_url=""))
    assert "configuration_url" not in payload["device"]
    # Sanity: when present and non-empty, it IS included
    payload = build_discovery_payload(
        entity, _cfg(configuration_url="http://host:8080/"),
    )
    assert payload["device"]["configuration_url"] == "http://host:8080/"


def test_discovery_payload_unit_when_set():
    from web.services.mqtt_topology import (
        EntityDef,
        build_discovery_payload,
    )
    entity = EntityDef(
        object_id="disk_used", component="sensor", name="Disk used",
        icon=None, device_class=None, state_class="measurement",
        unit_of_measurement="%",
        enabled_by_default=True, min_publish_interval_s=0.0,
        state_fn=None, command_handler=None,
        affected_by_hub_events=(),
    )
    payload = build_discovery_payload(entity, _cfg())
    assert payload["unit_of_measurement"] == "%"


def test_discovery_payload_disabled_by_default():
    from web.services.mqtt_topology import (
        EntityDef,
        build_discovery_payload,
    )
    entity = EntityDef(
        object_id="queue_failed", component="sensor", name="Queue failed",
        icon=None, device_class=None, state_class="measurement",
        unit_of_measurement=None,
        enabled_by_default=False, min_publish_interval_s=1.0,
        state_fn=None, command_handler=None,
        affected_by_hub_events=(),
    )
    payload = build_discovery_payload(entity, _cfg())
    assert payload["enabled_by_default"] is False


def test_discovery_payload_includes_json_attributes_topic_when_attrs_fn():
    from web.services.mqtt_topology import (
        EntityDef,
        build_attrs_topic,
        build_discovery_payload,
    )

    def _stub_state(hub, db, snap): return "x"
    def _stub_attrs(hub, db, snap): return {"reason": None}

    ent = EntityDef(
        object_id="demo", component="sensor", name="Demo",
        icon=None, device_class=None, state_class=None,
        unit_of_measurement=None, enabled_by_default=True,
        min_publish_interval_s=0.0,
        state_fn=_stub_state, command_handler=None,
        attrs_fn=_stub_attrs,
    )
    cfg = {"node_id": "vfs", "discovery_prefix": "homeassistant",
           "version": "0.0.0", "configuration_url": ""}
    payload = build_discovery_payload(ent, cfg)
    assert payload["json_attributes_topic"] == build_attrs_topic("demo", cfg)


def test_discovery_payload_omits_json_attributes_topic_without_attrs_fn():
    from web.services.mqtt_topology import (
        EntityDef,
        build_discovery_payload,
    )

    def _stub_state(hub, db, snap): return "x"
    ent = EntityDef(
        object_id="demo2", component="sensor", name="Demo2",
        icon=None, device_class=None, state_class=None,
        unit_of_measurement=None, enabled_by_default=True,
        min_publish_interval_s=0.0,
        state_fn=_stub_state, command_handler=None,
    )
    cfg = {"node_id": "vfs", "discovery_prefix": "homeassistant",
           "version": "0.0.0", "configuration_url": ""}
    payload = build_discovery_payload(ent, cfg)
    assert "json_attributes_topic" not in payload
