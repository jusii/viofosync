"""Reconnect vs. fatal-error classification in MqttService._run.

_connect_and_loop runs its workers inside an asyncio.TaskGroup, which
reports any task failure as an ExceptionGroup rather than the bare
exception. A routine broker disconnect (aiomqtt.MqttError, e.g.
"Disconnected during message iteration") therefore reaches _run wrapped
in a group. It must be classified as RECONNECTING, not logged as an
unexpected fatal ERROR.
"""
from __future__ import annotations

import asyncio

import aiomqtt

from web.services.mqtt import ConnState, MqttService


def _make_service(raises: BaseException) -> MqttService:
    svc = MqttService(db=None, provider=None, hub=None, app=None)
    svc._stop = asyncio.Event()
    svc._cfg = lambda: {"host": "broker", "port": 1883}

    async def _boom(_aiomqtt_mod, _cfg):
        # Break out of the while loop after this single attempt so the
        # backoff sleep is skipped (mirrors _run's `if _stop: break`).
        svc._stop.set()
        raise raises

    svc._connect_and_loop = _boom  # type: ignore[assignment]
    return svc


def test_taskgroup_disconnect_is_reconnecting():
    eg = ExceptionGroup(
        "unhandled errors in a TaskGroup (1 sub-exception)",
        [aiomqtt.MqttError("Disconnected during message iteration")],
    )
    svc = _make_service(eg)
    asyncio.run(svc._run())
    assert svc._state is ConnState.RECONNECTING
    assert "Disconnected during message iteration" in (svc._detail or "")


def test_bare_mqtterror_is_reconnecting():
    svc = _make_service(aiomqtt.MqttError("Operation timed out"))
    asyncio.run(svc._run())
    assert svc._state is ConnState.RECONNECTING
    assert "Operation timed out" in (svc._detail or "")


def test_genuine_error_in_group_is_error():
    eg = ExceptionGroup("boom", [ValueError("something truly unexpected")])
    svc = _make_service(eg)
    asyncio.run(svc._run())
    assert svc._state is ConnState.ERROR
    assert "something truly unexpected" in (svc._detail or "")
