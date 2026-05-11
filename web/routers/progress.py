"""WebSocket endpoint streaming live download + export events.

Authentication: the handshake's ``Cookie`` header is checked
for a valid session token. No CSRF on WS because the Origin
header is the relevant protection, and browsers won't let
scripts send arbitrary cookies cross-origin anyway.
"""

from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..auth import SESSION_COOKIE

router = APIRouter()


@router.websocket("/api/progress")
async def progress(ws: WebSocket) -> None:
    auth = ws.app.state.auth
    token = ws.cookies.get(SESSION_COOKIE)
    if not auth.validate_session(token):
        await ws.close(code=4401)
        return

    hub = ws.app.state.hub
    await hub.connect(ws)
    try:
        while True:
            # Read messages just to detect disconnects; we
            # don't expect the client to send anything useful.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await hub.disconnect(ws)
