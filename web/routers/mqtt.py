"""HTTP endpoints for the MQTT service: status + connection test.

Both require an authenticated session; the test endpoint additionally
requires CSRF because it makes a one-shot outbound MQTT connection.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..auth import require_csrf, require_session
from ..settings_schema import MASKED_SECRET


router = APIRouter(prefix="/api/mqtt", tags=["mqtt"],
                   dependencies=[Depends(require_session)])


@router.get("/status")
def get_status(request: Request) -> dict:
    svc = getattr(request.app.state, "mqtt", None)
    if svc is None:
        return {"state": "idle", "detail": None, "last_published_at": None}
    return svc.get_status()


class _TestBody(BaseModel):
    host: str
    port: int = 1883
    username: str = ""
    password: str = ""
    tls: bool = False
    client_id: str = ""


@router.post("/test", dependencies=[Depends(require_csrf)])
async def post_test(body: _TestBody, request: Request) -> dict:
    if not body.host:
        raise HTTPException(400, "host is required")
    import asyncio
    import aiomqtt
    import ssl as _ssl

    # The settings form sends the masking sentinel when the user
    # didn't retype the password — substitute the stored secret so
    # "Test connection" works without exposing it to the client.
    password = body.password
    if password == MASKED_SECRET:
        password = request.app.state.settings_provider.get().mqtt_password

    kwargs: dict = dict(
        hostname=body.host, port=body.port,
        username=body.username or None,
        password=password or None,
        identifier=body.client_id or None,
        keepalive=10,
    )
    if body.tls:
        kwargs["tls_context"] = _ssl.create_default_context()

    async def _attempt() -> dict:
        try:
            async with aiomqtt.Client(**kwargs):
                return {"ok": True, "detail": "connected"}
        except aiomqtt.MqttError as e:
            return {"ok": False, "detail": f"connection failed: {e}"}
        except Exception as e:
            return {"ok": False, "detail": f"error: {e}"}

    try:
        return await asyncio.wait_for(_attempt(), timeout=5.0)
    except asyncio.TimeoutError:
        return {"ok": False, "detail": "connection timed out (5s)"}
