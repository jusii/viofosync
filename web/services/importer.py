"""Local import / ingest core.

Feeds locally-available Viofo clips (browser upload, drop folder, or
an external/USB mount) into the archive, reusing the same filename
patterns, path layout, GPX, indexing, and retention as Wi-Fi sync.

Pure-ish: ``scan_source`` and ``ingest_clip`` do filesystem + DB work
but no asyncio; ``run_folder_ingest`` is the batch driver invoked on a
worker thread, broadcasting progress via the hub.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass, field

import viofosync_lib as vfs

from ..db import Database
from . import retention as _retention
from . import scanner

log = logging.getLogger("viofosync.importer")

STAGING_DIRNAME = ".import_tmp"


@dataclass
class ClipResult:
    filename: str
    status: str          # imported|already_present|not_recognised
                         #          |over_quota_older|error
    detail: str = ""
    size_bytes: int = 0
    event_type: str = "normal"


@dataclass
class ScanItem:
    src_path: str
    source_rel_path: str
    basename: str
    timestamp: int
    camera: str
    sequence: int
    event_type: str
    size_bytes: int


@dataclass
class Manifest:
    items: list[ScanItem] = field(default_factory=list)      # newest-first
    skipped: list[dict] = field(default_factory=list)        # [{"name","reason"}]
    total_bytes: int = 0


def _is_ro(source_rel_path: str) -> bool:
    norm = "/" + source_rel_path.replace("\\", "/").strip("/").upper()
    return "/RO/" in norm


def classify_event_type(camera_field: str, source_rel_path: str) -> str:
    if _is_ro(source_rel_path):
        return "ro"
    if camera_field.upper().startswith("P"):
        return "parking"
    return "normal"


def scan_item_from_match(
    m: re.Match[str], name: str, *, source_rel_path: str, size: int, src_path: str,
) -> ScanItem:
    ts = int(_dt.datetime(
        int(m.group("year")), int(m.group("month")), int(m.group("day")),
        int(m.group("hour")), int(m.group("minute")), int(m.group("second")),
    ).timestamp())
    cam = m.group("camera")
    return ScanItem(
        src_path=src_path, source_rel_path=source_rel_path, basename=name,
        timestamp=ts, camera=cam.upper(), sequence=int(m.group("sequence")),
        event_type=classify_event_type(cam, source_rel_path), size_bytes=size,
    )


def scan_source(root: str) -> Manifest:
    """Recurse ``root``, returning recognised Viofo clips (newest-first)
    and a list of skipped files with reasons."""
    man = Manifest()
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(dirpath, name)
            m = vfs.downloaded_filename_re.match(name)
            if not m:
                man.skipped.append({"name": name, "reason": "not_recognised"})
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            rel = os.path.relpath(full, root)
            try:
                item = scan_item_from_match(
                    m, name, source_rel_path=rel, size=size, src_path=full,
                )
            except ValueError:
                man.skipped.append({"name": name, "reason": "bad_timestamp"})
                continue
            man.items.append(item)
            man.total_bytes += size
    man.items.sort(key=lambda it: it.timestamp, reverse=True)
    return man


def scan_item_dict(it: ScanItem) -> dict:
    return asdict(it)


def clip_result_dict(res: ClipResult) -> dict:
    return asdict(res)


def dest_for(snap, item: ScanItem) -> str:
    group = vfs.get_group_name(
        _dt.datetime.fromtimestamp(item.timestamp), snap.grouping,
    )
    return vfs.get_filepath(snap.recordings, group or "", item.basename)


def _origin_source_dir(item: ScanItem) -> str:
    # Invariant: contains "/RO/" iff the clip is locked, so scanner.scan
    # re-derives event_type='ro' on every future rescan.
    return "/import/RO/" if item.event_type == "ro" else "/import/"


def _record_origin(db: Database, item: ScanItem) -> None:
    now = int(time.time())
    with db.write() as c:
        c.execute(
            "INSERT OR IGNORE INTO download_queue "
            "(filename, source_dir, remote_size, recorded_at, camera, "
            " event_type, state, priority, enqueued_at, finished_at, manual) "
            "VALUES (?, ?, ?, ?, ?, ?, 'done', 0, ?, ?, 1)",
            (item.basename, _origin_source_dir(item), item.size_bytes,
             item.timestamp, item.camera, item.event_type, now, now),
        )


def is_cross_volume(root: str, recordings: str) -> bool:
    try:
        return os.stat(root).st_dev != os.stat(recordings).st_dev
    except OSError:
        return True  # safe default: copy, never a destructive move


def ingest_clip(
    db: Database, snap, item: ScanItem, *,
    cross_volume: bool, staged: bool = False,
) -> ClipResult:
    """Place one clip into the archive. ``staged`` means ``item.src_path``
    is already in ``.import_tmp`` and make-room was done by the caller
    (the upload path); we then go straight to the final rename."""
    recordings = snap.recordings
    dest = dest_for(snap, item)
    if os.path.exists(dest):
        return ClipResult(item.basename, "already_present",
                          size_bytes=item.size_bytes, event_type=item.event_type)

    if not staged:
        ok = _retention.make_room_for(
            db, recordings, size=item.size_bytes, before_ts=item.timestamp,
            disk_pct=snap.retention_disk_pct, quota_gb=snap.recordings_quota_gb,
            protect_ro=snap.retention_protect_ro,
            exclude=_retention.import_exclude_set(recordings, snap.import_path),
        )
        if not ok:
            return ClipResult(item.basename, "over_quota_older",
                              size_bytes=item.size_bytes, event_type=item.event_type)

    staging = os.path.join(recordings, STAGING_DIRNAME)
    tmp = item.src_path if staged else os.path.join(staging, item.basename)
    if not staged:
        os.makedirs(staging, exist_ok=True)
        try:
            if cross_volume:
                shutil.copy2(item.src_path, tmp)
                if os.path.getsize(tmp) != item.size_bytes:
                    os.remove(tmp)
                    log.warning(
                        "import size mismatch for %s after copy", item.basename
                    )
                    return ClipResult(item.basename, "error",
                                      detail="size mismatch after copy",
                                      size_bytes=item.size_bytes,
                                      event_type=item.event_type)
            else:
                os.replace(item.src_path, tmp)  # same-volume move into staging
        except OSError as e:
            log.warning("import staging failed for %s: %s", item.basename, e)
            return ClipResult(item.basename, "error", detail=str(e),
                              size_bytes=item.size_bytes,
                              event_type=item.event_type)

    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.replace(tmp, dest)
    except OSError as e:
        # Don't strand the staged file. A same-volume move has already
        # consumed the source, so restore it (no data loss); cross-volume
        # keeps an intact original and upload (staged) will be retried, so
        # the temp is safe to drop there.
        if not staged and not cross_volume:
            try:
                os.replace(tmp, item.src_path)
            except OSError as restore_err:
                log.error(
                    "import: failed to restore source for %s after a failed "
                    "placement; staged file %s may be lost: %s",
                    item.basename, tmp, restore_err,
                )
        else:
            try:
                os.remove(tmp)
            except OSError:
                pass
        log.warning("import placement failed for %s: %s", item.basename, e)
        return ClipResult(item.basename, "error", detail=str(e),
                          size_bytes=item.size_bytes, event_type=item.event_type)

    if snap.gps_extract:
        try:
            vfs.extract_gps_data(dest)
        except Exception as e:  # clips without a GPS lock — non-fatal
            log.info("gpx extract failed for %s: %s", item.basename, e)

    _record_origin(db, item)
    return ClipResult(item.basename, "imported",
                      size_bytes=item.size_bytes, event_type=item.event_type)


def _clean_staging(recordings: str) -> None:
    staging = os.path.join(recordings, STAGING_DIRNAME)
    if not os.path.isdir(staging):
        return
    for name in os.listdir(staging):
        try:
            os.remove(os.path.join(staging, name))
        except OSError:  # pragma: no cover — best-effort
            pass


def _broadcast(hub, loop, event: dict) -> None:
    if hub is None:
        return
    hub.schedule_broadcast(loop, event)


_SUMMARY_KEYS = (
    "imported", "already_present",
    "over_quota_older", "errors",
)


def run_folder_ingest(db: Database, snap, hub, loop, *, root: str) -> dict:
    """Ingest every recognised clip under ``root`` (newest-first),
    then run the post-sync pipeline. Runs on a worker thread."""
    recordings = snap.recordings
    _clean_staging(recordings)  # clear debris from any prior aborted run
    man = scan_source(root)
    cross = is_cross_volume(root, recordings)

    summary = {k: 0 for k in _SUMMARY_KEYS}
    summary["not_recognised"] = len(man.skipped)   # from the manifest, not per-clip
    summary["bytes_imported"] = 0
    total = len(man.items)
    _broadcast(hub, loop, {"type": "import_started", "total": total})

    for i, item in enumerate(man.items, 1):
        res = ingest_clip(db, snap, item, cross_volume=cross)
        if res.status in summary:
            summary[res.status] += 1
        else:  # 'error'
            summary["errors"] += 1
        if res.status == "imported":
            summary["bytes_imported"] += res.size_bytes
        _broadcast(hub, loop, {
            "type": "import_progress", "done": i, "total": total,
            "filename": res.filename, "result": res.status,
        })

    # Reuse the post-sync pipeline. scanner.scan handles its own
    # broadcast + threadsafe scheduling.
    scanner.scan(db, recordings, snap.grouping, hub, loop)
    if loop is not None:
        asyncio.run_coroutine_threadsafe(
            scanner.sweep_missing_thumbs(db, recordings), loop,
        )
    _retention.sweep(
        db, recordings,
        max_days=snap.retention_max_days,
        disk_pct=snap.retention_disk_pct,
        protect_ro=snap.retention_protect_ro,
        quota_gb=snap.recordings_quota_gb,
        exclude=_retention.import_exclude_set(recordings, snap.import_path),
    )
    log.info(
        "import complete: %d imported, %d already_present, %d not_recognised, "
        "%d over_quota_older, %d error(s)",
        summary["imported"], summary["already_present"],
        summary["not_recognised"], summary["over_quota_older"],
        summary["errors"],
    )
    _broadcast(hub, loop, {"type": "import_done", **summary})
    _clean_staging(recordings)
    return summary
