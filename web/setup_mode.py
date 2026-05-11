"""Middleware that hijacks every non-setup route while the app
is in setup mode (i.e. no admin password configured)."""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

SETUP_PATHS = ("/setup", "/api/setup")
PASSTHROUGH_PREFIXES = ("/static/",) + SETUP_PATHS


class SetupModeMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        provider = request.app.state.settings_provider
        if provider.get().is_unconfigured:
            path = request.url.path
            if not any(path == p or path.startswith(p) for p in PASSTHROUGH_PREFIXES):
                return RedirectResponse(url="/setup", status_code=307)
        return await call_next(request)
