"""Import endpoints — folder scan/ingest + per-file browser upload.

Module is named ``imports`` (not ``import``, a Python keyword). All
routes require an authenticated session; mutating routes also require
CSRF, matching the other routers.
"""
from __future__ import annotations

import asyncio
import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

import viofosync_lib as vfs

from ..auth import require_csrf, require_session
from ..services import importer
from ..services import retention as _retention

log = logging.getLogger("viofosync.import")

router = APIRouter(
    prefix="/api/import",
    tags=["import"],
    dependencies=[Depends(require_session)],
)


class _PathBody(BaseModel):
    path: str | None = None


def _db(request: Request):
    return request.app.state.db


def _snap(request: Request):
    return request.app.state.settings_provider.get()


def _resolve_root(snap, override: str | None) -> str:
    return (
        override or snap.import_path
        or os.path.join(snap.recordings, "import")
    ).strip()


@router.post("/scan", dependencies=[Depends(require_csrf)])
def scan(request: Request, body: _PathBody) -> dict:
    snap = _snap(request)
    root = _resolve_root(snap, body.path)
    if not os.path.isdir(root):
        raise HTTPException(400, f"not a readable directory: {root}")
    man = importer.scan_source(root)
    return {
        "path": root,
        "cross_volume": importer.is_cross_volume(root, snap.recordings),
        "total_bytes": man.total_bytes,
        "recognised": [importer.scan_item_dict(it) for it in man.items],
        "skipped": man.skipped,
    }


@router.post("/ingest", dependencies=[Depends(require_csrf)])
async def ingest(request: Request, body: _PathBody) -> dict:
    if getattr(request.app.state, "import_running", False):
        raise HTTPException(409, "import already running")
    snap = _snap(request)
    root = _resolve_root(snap, body.path)
    if not os.path.isdir(root):
        raise HTTPException(400, f"not a readable directory: {root}")

    request.app.state.import_running = True
    db = _db(request)
    hub = request.app.state.hub
    loop = asyncio.get_running_loop()

    def _work():
        try:
            importer.run_folder_ingest(db, snap, hub, loop, root=root)
        except Exception:  # pragma: no cover — never wedge the flag
            log.exception("folder ingest failed")
        finally:
            request.app.state.import_running = False

    loop.run_in_executor(None, _work)
    return {"ok": True, "started": True}


@router.post("/upload", dependencies=[Depends(require_csrf)])
async def upload(request: Request) -> dict:
    snap = _snap(request)
    db = _db(request)
    rel = request.headers.get("X-Import-Path", "")
    name = os.path.basename(rel.replace("\\", "/"))
    try:
        size = int(request.headers.get("X-Import-Size")
                   or request.headers.get("Content-Length") or 0)
    except ValueError:
        size = 0

    m = vfs.downloaded_filename_re.match(name)
    if not m:
        return {"status": "not_recognised", "filename": name}

    # Destination derives ONLY from the parsed basename — the client's
    # relative path is used solely for RO detection, never the write path.
    item = importer.scan_item_from_match(
        m, name, source_rel_path=rel, size=size, src_path="",
    )
    dest = importer.dest_for(snap, item)
    if os.path.exists(dest):
        return {"status": "already_present", "filename": name}

    # Evict to fit BEFORE writing bytes (size known from the header).
    if not _retention.make_room_for(
        db, snap.recordings, size=item.size_bytes, before_ts=item.timestamp,
        disk_pct=snap.retention_disk_pct, quota_gb=snap.recordings_quota_gb,
        protect_ro=snap.retention_protect_ro,
        exclude=_retention.import_exclude_set(snap.recordings, snap.import_path),
    ):
        log.warning("upload rejected (over quota, older than retained set): %s", name)
        return {"status": "over_quota_older", "filename": name}

    staging = os.path.join(snap.recordings, importer.STAGING_DIRNAME)
    os.makedirs(staging, exist_ok=True)
    tmp = os.path.join(staging, name)
    written = 0
    try:
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
                written += len(chunk)
    except Exception as e:  # pragma: no cover — client abort / disk error
        _silent_remove(tmp)
        log.warning("upload stream failed for %s: %s", name, e)
        return {"status": "error", "filename": name, "detail": str(e)}

    if size and written != size:
        _silent_remove(tmp)
        log.warning("upload size mismatch for %s: got %d, expected %d", name, written, size)
        return {"status": "error", "filename": name, "detail": "size mismatch"}

    item.size_bytes = written
    item.src_path = tmp
    res = await asyncio.to_thread(
        importer.ingest_clip, db, snap, item, cross_volume=False, staged=True,
    )
    return importer.clip_result_dict(res)


def _silent_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
