"""MQTT reconnect backoff must reset after a stable connection,
discovery-cleanup state must be owned by the service (not poked in
from lifespan), and a timed-out ffprobe child must be reaped.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import aiomqtt

from web.db import Database
from web.services import durations
from web.services.hub import Hub
from web.services.mqtt import MqttService


def _service(tmp_path, **snap_attrs) -> MqttService:
    snap = MagicMock()
    snap.mqtt_node_id = snap_attrs.get("node_id", "viofosync")
    snap.mqtt_discovery_prefix = snap_attrs.get("prefix", "homeassistant")
    snap.mqtt_host = "broker.local"
    provider = MagicMock()
    provider.get.return_value = snap
    db = Database(str(tmp_path / "t.db"))
    return MqttService(db=db, provider=provider, hub=Hub(), app=None)


# ---- backoff reset after a stable connection ----

async def test_backoff_resets_after_stable_connection(tmp_path, monkeypatch):
    svc = _service(tmp_path)
    monkeypatch.setattr(MqttService, "BACKOFF_STEPS", (0.01, 5.0))
    monkeypatch.setattr(MqttService, "STABLE_RESET_S", 0.05, raising=False)

    attempt_times: list[float] = []
    calls = {"n": 0}

    async def fake_connect(aiomqtt_mod, cfg):
        calls["n"] += 1
        attempt_times.append(time.monotonic())
        if calls["n"] == 2:
            # Stable connection: outlives STABLE_RESET_S, then drops.
            await asyncio.sleep(0.1)
        if calls["n"] >= 3:
            svc._stop.set()
        raise aiomqtt.MqttError("broker went away")

    monkeypatch.setattr(svc, "_connect_and_loop", fake_connect)
    await asyncio.wait_for(svc._run(), timeout=5.0)

    assert calls["n"] >= 3
    # Attempt 1 fails instantly -> idx moves up the ladder. Attempt 2
    # was stable, so the delay before attempt 3 must be the FIRST
    # step (0.01s), not the escalated 5s one.
    gap = attempt_times[2] - (attempt_times[1] + 0.1)
    assert gap < 1.0, (
        f"backoff not reset after a stable connection (waited {gap:.2f}s)"
    )


# ---- discovery cleanup state owned by the service ----

def test_last_topology_initialised_at_construction(tmp_path):
    svc = _service(tmp_path, node_id="car2", prefix="ha")
    assert svc._last_node_id == "car2"
    assert svc._last_discovery_prefix == "ha"


# ---- ffprobe child reaped on timeout ----

async def test_ffprobe_timeout_kills_and_reaps_child(monkeypatch):
    state = {"killed": False, "reaped": 0}

    class _HangProc:
        def kill(self):
            state["killed"] = True

        async def wait(self):
            state["reaped"] += 1
            return -9

        async def communicate(self):
            await asyncio.sleep(60)

    async def fake_exec(*argv, **kwargs):
        return _HangProc()

    monkeypatch.setattr(durations.shutil, "which", lambda n: "/bin/ffprobe")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(durations, "_FFPROBE_TIMEOUT_S", 0.05, raising=False)

    got = await durations._probe_duration_ffprobe("/x.mp4")

    assert got is None
    assert state["killed"], "timed-out ffprobe left running"
    assert state["reaped"] == 1, "killed ffprobe never reaped (zombie)"
