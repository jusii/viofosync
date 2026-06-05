"""Persistent application log endpoint."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from ..auth import require_session
from ..services import log_store

router = APIRouter(
    prefix="/api",
    tags=["logs"],
    dependencies=[Depends(require_session)],
)


@router.get("/logs")
def list_logs(
    request: Request,
    level: str = Query("WARNING"),
    logger: str | None = Query(None),
    q: str | None = Query(None),
    before: int | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
) -> dict:
    # Unknown level strings fall back to WARNING (the default view).
    # Reference the table rather than a bare literal so the two can't drift.
    min_levelno = log_store.LEVELS.get(level.upper(), log_store.LEVELS["WARNING"])
    entries = log_store.query_logs(
        request.app.state.db,
        min_levelno=min_levelno,
        logger=logger.strip() if logger else None,
        q=q.strip() if q else None,
        before=before,
        limit=limit,
    )
    return {"entries": entries}
