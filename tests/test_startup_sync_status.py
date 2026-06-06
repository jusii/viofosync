"""When ADDRESS is unset at startup, the Hub's sync_status must be
'error' before any client connects — otherwise the very first MQTT
publish or WS snapshot would show 'paused'."""
from __future__ import annotations

import types

from web.services.hub import Hub
from web.services.sync_status import compute_sync_status


def test_hub_sync_status_is_error_when_address_unset():
    snap = types.SimpleNamespace(
        address=None, recordings="/r", disk_critical_pct=95,
    )
    provider = types.SimpleNamespace(get=lambda: snap)
    hub = Hub(settings_provider=provider)
    # No events yet, but compute is total — should report error.
    state, reason = compute_sync_status(hub, None, snap)
    assert state == "error"
    assert reason == "camera address not configured"
