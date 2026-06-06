"""Tests for MQTT_* settings."""
from __future__ import annotations

import pytest


def test_defaults():
    from web.settings_schema import DEFAULT_VALUES
    assert DEFAULT_VALUES["MQTT_ENABLED"] is False
    assert DEFAULT_VALUES["MQTT_HOST"] == ""
    assert DEFAULT_VALUES["MQTT_PORT"] == 1883
    assert DEFAULT_VALUES["MQTT_USERNAME"] == ""
    assert DEFAULT_VALUES["MQTT_PASSWORD"] == ""
    assert DEFAULT_VALUES["MQTT_TLS"] is False
    assert DEFAULT_VALUES["MQTT_CLIENT_ID"] == ""
    assert DEFAULT_VALUES["MQTT_DISCOVERY_PREFIX"] == "homeassistant"
    assert DEFAULT_VALUES["MQTT_NODE_ID"] == "viofosync"
    assert DEFAULT_VALUES["MQTT_DISCOVERY_ENABLED"] is True
    assert DEFAULT_VALUES["MQTT_QOS"] == 1


def test_all_editable():
    from web.settings_schema import EDITABLE_KEYS
    for k in (
        "MQTT_ENABLED", "MQTT_HOST", "MQTT_PORT", "MQTT_USERNAME",
        "MQTT_PASSWORD", "MQTT_TLS", "MQTT_CLIENT_ID",
        "MQTT_DISCOVERY_PREFIX", "MQTT_NODE_ID",
        "MQTT_DISCOVERY_ENABLED", "MQTT_QOS",
    ):
        assert k in EDITABLE_KEYS


def test_port_validation():
    from web.settings_schema import validate_partial
    with pytest.raises(ValueError):
        validate_partial({"MQTT_PORT": 0})
    with pytest.raises(ValueError):
        validate_partial({"MQTT_PORT": 70000})
    assert validate_partial({"MQTT_PORT": 8883})["MQTT_PORT"] == 8883


def test_node_id_charset():
    from web.settings_schema import validate_partial
    validate_partial({"MQTT_NODE_ID": "viofosync_garage_2"})
    with pytest.raises(ValueError):
        validate_partial({"MQTT_NODE_ID": "Viofosync"})  # uppercase
    with pytest.raises(ValueError):
        validate_partial({"MQTT_NODE_ID": "viofosync-garage"})  # hyphen
    with pytest.raises(ValueError):
        validate_partial({"MQTT_NODE_ID": ""})


def test_discovery_prefix_no_slashes():
    from web.settings_schema import validate_partial
    validate_partial({"MQTT_DISCOVERY_PREFIX": "homeassistant"})
    with pytest.raises(ValueError):
        validate_partial({"MQTT_DISCOVERY_PREFIX": "/homeassistant"})
    with pytest.raises(ValueError):
        validate_partial({"MQTT_DISCOVERY_PREFIX": "homeassistant/"})


def test_qos_literal():
    from web.settings_schema import validate_partial
    for v in (0, 1, 2):
        assert validate_partial({"MQTT_QOS": v})["MQTT_QOS"] == v
    with pytest.raises(ValueError):
        validate_partial({"MQTT_QOS": 3})


def test_host_required_when_enabled(tmp_config_dir):
    """Cross-field rule: cannot enable MQTT without a host."""
    from web import settings as settings_mod
    settings_mod.reset_for_tests()
    p = settings_mod.get_provider()
    with pytest.raises(ValueError):
        p.update({"MQTT_ENABLED": True}, actor="test")
    # Setting both at once works:
    p.update({"MQTT_ENABLED": True, "MQTT_HOST": "broker.lan"}, actor="test")
    snap = p.get()
    assert snap.mqtt_enabled is True
    assert snap.mqtt_host == "broker.lan"
    settings_mod.reset_for_tests()


def test_snapshot_projection(tmp_config_dir):
    """The Snapshot dataclass exposes every MQTT_* key as a lower-snake-case attr."""
    from web import settings as settings_mod
    settings_mod.reset_for_tests()
    snap = settings_mod.get_provider().get()
    for attr in (
        "mqtt_enabled", "mqtt_host", "mqtt_port", "mqtt_username",
        "mqtt_password", "mqtt_tls", "mqtt_client_id",
        "mqtt_discovery_prefix", "mqtt_node_id",
        "mqtt_discovery_enabled", "mqtt_qos",
    ):
        assert hasattr(snap, attr), attr
    settings_mod.reset_for_tests()
