"""WebSocket fan-out hub.

A tiny pub-sub used by background workers (downloader,
exporter, reachability probe) to push events to every
connected UI client without each worker knowing how many
there are.

Thread-safety: all ``broadcast`` calls happen from the event
loop (background tasks created via ``asyncio.create_task``).
The downloader progress sink is called from a blocking worker
thread; it schedules ``broadcast`` onto the loop via
``asyncio.run_coroutine_threadsafe`` — see
:class:`WebSink` in ``sync_worker.py``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Set

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

log = logging.getLogger("viofosync.hub")


class Hub:
    def __init__(self) -> None:
        self._clients: Set[WebSocket] = set()
        self._lock = asyncio.Lock()
        # Retain the last snapshot of major state so a newly-
        # connected client sees the current situation without
        # waiting for the next event.
        self.last_state: Dict[str, Any] = {
            "dashcam_online": None,
            "current_item": None,
        }

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)
        try:
            await ws.send_json(
                {"type": "snapshot", "state": self.last_state}
            )
        except (WebSocketDisconnect, RuntimeError, OSError):
            # Client closed during the handshake (e.g. tab hot-
            # reloaded between accept and the first send). The
            # route's finally-clause will remove us from
            # _clients via disconnect(); no need to raise out of
            # the route handler as a 500.
            log.debug(
                "client disconnected before initial snapshot",
            )
            async with self._lock:
                self._clients.discard(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, event: Dict[str, Any]) -> None:
        # Update snapshot for state-ish events so reconnects
        # land on a coherent view.
        t = event.get("type")
        if t == "dashcam_online":
            self.last_state["dashcam_online"] = True
        elif t == "dashcam_offline":
            self.last_state["dashcam_online"] = False
        elif t == "item_started":
            self.last_state["current_item"] = {
                "filename": event.get("filename"),
                "total": event.get("total"),
                "bytes": 0,
            }
        elif t == "item_progress":
            ci = self.last_state.get("current_item") or {}
            ci.update(
                filename=event.get("filename"),
                bytes=event.get("bytes"),
                total=event.get("total"),
                speed=event.get("speed"),
            )
            self.last_state["current_item"] = ci
        elif t == "item_finished":
            self.last_state["current_item"] = None
        elif t == "sync_state":
            self.last_state["sync_state"] = {
                "running": event.get("running"),
                "paused": event.get("paused"),
            }

        dead = []
        async with self._lock:
            clients = list(self._clients)
        for ws in clients:
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)

    def schedule_broadcast(
        self,
        loop: asyncio.AbstractEventLoop,
        event: Dict[str, Any],
    ) -> None:
        """Thread-safe entry point: used from the downloader
        worker thread, which doesn't own the event loop."""
        try:
            asyncio.run_coroutine_threadsafe(
                self.broadcast(event), loop
            )
        except RuntimeError:
            log.debug("event loop closed, dropping event %s", event)
