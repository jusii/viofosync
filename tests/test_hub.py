"""Hub.connect handshake regressions."""
from __future__ import annotations

from starlette.websockets import WebSocketDisconnect

from web.services.hub import Hub


class _FakeWS:
    """Minimal WebSocket stand-in for testing Hub.connect.

    Records the call sequence and lets the test inject the
    behaviour of ``send_json`` (success or various disconnect
    flavours)."""

    def __init__(self, *, send_raises: BaseException | None = None) -> None:
        self.accept_calls = 0
        self.send_calls = 0
        self._send_raises = send_raises

    async def accept(self) -> None:
        self.accept_calls += 1

    async def send_json(self, _payload) -> None:
        self.send_calls += 1
        if self._send_raises is not None:
            raise self._send_raises


# ---- happy path ----

async def test_connect_sends_snapshot_and_keeps_client() -> None:
    hub = Hub()
    ws = _FakeWS()
    await hub.connect(ws)
    assert ws.accept_calls == 1
    assert ws.send_calls == 1
    assert ws in hub._clients


# ---- regression: client disconnects between accept() and send_json() ----

async def test_connect_swallows_disconnect_during_snapshot() -> None:
    """Browser hot-reload / page navigation can close the WS in
    the millisecond between accept() and the first send. The
    handshake must NOT propagate a 500 — and the now-dead client
    must not stay in the broadcast set."""
    hub = Hub()
    ws = _FakeWS(send_raises=WebSocketDisconnect(code=1006))
    # Should not raise.
    await hub.connect(ws)
    assert ws not in hub._clients


async def test_connect_swallows_runtime_error_during_snapshot() -> None:
    """uvicorn's WS impl can raise RuntimeError("Cannot call send
    once a close message has been sent.") on the same race."""
    hub = Hub()
    ws = _FakeWS(send_raises=RuntimeError("send after close"))
    await hub.connect(ws)
    assert ws not in hub._clients


async def test_connect_swallows_oserror_during_snapshot() -> None:
    """Pipe broken / network died between accept and send."""
    hub = Hub()
    ws = _FakeWS(send_raises=OSError("broken pipe"))
    await hub.connect(ws)
    assert ws not in hub._clients
