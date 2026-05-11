"""Export jobs router."""

from __future__ import annotations

import os
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from ..auth import require_csrf, require_session

router = APIRouter(
    prefix="/api/exports",
    tags=["exports"],
    dependencies=[Depends(require_session)],
)


def _resolve_default_encoder(app_state) -> str:
    """Resolve the current EXPORT_ENCODER preference against the
    cached encoder probe results into a concrete encoder name.

    Reads the settings snapshot per call so changes made through
    the config GUI take effect for newly created jobs without a
    restart. The encoder probe itself (expensive) stays cached on
    ``app_state.export_encoders``.
    """
    snap = app_state.settings_provider.get()
    pref = snap.export_encoder_pref
    encoders = getattr(app_state, "export_encoders", {}) or {}
    if pref == "auto":
        for name in (
            "videotoolbox", "nvenc", "qsv", "vaapi", "software",
        ):
            if encoders.get(name):
                return name
        return "software"
    if not encoders.get(pref):
        return "software"
    return pref


class CreateExport(BaseModel):
    type: str = Field(
        pattern="^(join_front|join_rear|pip)$"
    )
    clip_ids: List[int]
    encoder: str | None = Field(
        default=None,
        pattern="^(software|videotoolbox|nvenc|qsv|vaapi)$",
    )


@router.get("/encoders")
def list_encoders(request: Request) -> dict:
    """What video encoders this ffmpeg build supports + the
    server's default choice. The UI uses this to populate a
    dropdown limited to encoders that will actually work."""
    encoders = getattr(request.app.state, "export_encoders", {})
    default = _resolve_default_encoder(request.app.state)
    return {
        "available": [k for k, v in encoders.items() if v],
        "default": default,
    }


@router.post("", dependencies=[Depends(require_csrf)])
def create(body: CreateExport, request: Request) -> dict:
    worker = getattr(request.app.state, "export_worker", None)
    if worker is None:
        raise HTTPException(503, "export worker not running")
    encoders = getattr(request.app.state, "export_encoders", {})
    encoder = body.encoder or _resolve_default_encoder(
        request.app.state,
    )
    if not encoders.get(encoder):
        raise HTTPException(
            400,
            f"encoder '{encoder}' not available on this server",
        )
    try:
        job_id = worker.enqueue(
            body.type, body.clip_ids, encoder=encoder,
        )
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"job_id": job_id, "encoder": encoder}


@router.get("")
def list_jobs(request: Request) -> JSONResponse:
    with request.app.state.db.conn() as c:
        rows = c.execute(
            "SELECT id, type, state, progress, error, "
            "created_at, started_at, finished_at "
            "FROM export_jobs ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
    return JSONResponse({"jobs": [dict(r) for r in rows]})


@router.get("/{job_id}/download")
def download(job_id: int, request: Request):
    with request.app.state.db.conn() as c:
        row = c.execute(
            "SELECT output_path, state FROM export_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "job not found")
    if row["state"] != "done":
        raise HTTPException(409, f"job not ready (state={row['state']})")
    path = row["output_path"]
    if not path or not os.path.isfile(path):
        raise HTTPException(410, "output missing")
    return FileResponse(
        path,
        media_type="video/mp4",
        filename=f"viofosync_export_{job_id}.mp4",
    )


@router.delete("/{job_id}", dependencies=[Depends(require_csrf)])
def delete(job_id: int, request: Request) -> dict:
    with request.app.state.db.write() as c:
        row = c.execute(
            "SELECT output_path FROM export_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "job not found")
        if row["output_path"] and os.path.exists(row["output_path"]):
            try:
                os.remove(row["output_path"])
            except OSError:
                pass
        c.execute("DELETE FROM export_jobs WHERE id=?", (job_id,))
    return {"ok": True}
