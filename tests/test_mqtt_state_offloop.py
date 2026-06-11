"""MQTT state_fn calls must not starve the event loop.

state_disk_used does a full archive tree walk in quota mode — on a
NAS that's seconds of blocking I/O, invoked from the 60s tick and
every full-state publish. It has to run off the loop.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

from web.db import Database
from web.services import mqtt_topology as topo
from web.services.hub import Hub
from web.services.mqtt import MqttService


class _Client:
    async def publish(self, *args, **kwargs) -> None:
        pass


def _entity(state_fn) -> topo.EntityDef:
    return topo.EntityDef(
        object_id="disk_used", component="sensor", name="Disk used",
        icon=None, device_class=None, state_class=None,
        unit_of_measurement=None, enabled_by_default=True,
        min_publish_interval_s=0.0, state_fn=state_fn,
        command_handler=None,
    )


async def test_slow_state_fn_does_not_starve_loop(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    svc = MqttService(db=db, provider=MagicMock(), hub=Hub(), app=None)

    def slow_state(hub, db_, snap):
        time.sleep(0.3)  # simulate the quota-mode archive walk
        return "42"

    ticks = 0

    async def _ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    t = asyncio.create_task(_ticker())
    try:
        with patch.object(topo, "TOPOLOGY", (_entity(slow_state),)):
            await svc._publish_full_state(
                _Client(), {"node_id": "n", "qos": 0},
            )
    finally:
        t.cancel()

    assert ticks >= 5, f"event loop starved by state_fn ({ticks} ticks)"
