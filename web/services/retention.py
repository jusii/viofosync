"""Local archive retention.

Two independent rules, both optional and zero-disabled:

* time cap (``max_days``) — delete clips older than N days
* disk-pressure cap (``disk_pct``) — when used disk % is at/above
  the threshold, delete oldest first until back below it

A separate flag (``protect_ro``) shields read-only / locked clips
from both rules. The actual ``sweep()`` function tying these to
the database lives below; the eligibility decision is factored out
as a pure function so it can be unit-tested without touching disk.
"""
from __future__ import annotations

import logging
import os
import shutil
from typing import Optional

from ..db import Database
from . import thumbs as _thumbs

log = logging.getLogger("viofosync.retention")


def _eligible_by_time(
    clip: dict,
    *,
    now: int,
    max_days: int,
    protect_ro: bool,
) -> tuple[bool, str]:
    """Pure decision: should this clip be deleted by the time rule?

    Returns ``(delete, reason)`` where ``reason`` is one of
    ``'time'`` (yes, delete), ``'kept'`` (no, within retention
    window or rule disabled), or ``'ro_protected'`` (no, RO clip
    and protection is on).
    """
    if protect_ro and (clip.get("event_type") or "") == "ro":
        return False, "ro_protected"
    if max_days <= 0:
        return False, "kept"
    if clip["timestamp"] < now - max_days * 86400:
        return True, "time"
    return False, "kept"


def _delete_clip_files(rec: dict, recordings: str) -> int:
    """Delete the .mp4, .gpx sidecar, and cached thumb for one
    clip. Returns the number of bytes freed (best-effort; 0 on
    failure)."""
    freed = 0
    path = rec["path"]
    try:
        freed = os.path.getsize(path)
    except OSError:
        freed = 0
    for p in (path, path + ".gpx", _thumbs.thumb_path(recordings, rec["id"])):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
        except OSError as e:  # pragma: no cover — best-effort
            log.warning("retention: could not remove %s: %s", p, e)
    # Best-effort prune of an empty group folder.
    parent = os.path.dirname(path)
    try:
        os.rmdir(parent)
    except OSError:
        pass
    return freed


def _delete_index_row(db: Database, clip_id: int) -> None:
    with db.write() as c:
        c.execute("DELETE FROM clip_index WHERE id = ?", (clip_id,))


def _broadcast(sink, filename: str, reason: str) -> None:
    if sink is None:
        return
    try:
        sink.retention_deleted(filename, reason=reason)
    except Exception:  # pragma: no cover — never let UI plumbing break a sweep
        log.exception("retention: sink.retention_deleted raised")


def sweep(
    db: Database,
    recordings: str,
    *,
    max_days: int,
    disk_pct: int,
    protect_ro: bool,
    sink=None,
    _now: Optional[int] = None,
) -> dict:
    """Run the retention pass. Returns a summary dict.

    ``_now`` is for tests only — production callers should leave
    it None so the function reads the current time.
    """
    import time as _time
    now = _now if _now is not None else int(_time.time())
    deleted_time = 0
    protected = 0
    bytes_freed = 0

    # Phase 1: time-based.
    if max_days > 0:
        with db.conn() as c:
            rows = [
                dict(r) for r in c.execute(
                    "SELECT id, path, basename, timestamp, event_type "
                    "FROM clip_index WHERE timestamp < ?",
                    (now - max_days * 86400,),
                ).fetchall()
            ]
        if rows:
            # "examining" rather than "deleting": with protect_ro on,
            # some rows here are RO-locked and will be skipped. The
            # end-of-sweep summary reports the real deleted count.
            log.info(
                "retention sweep: %d clip(s) older than %d days "
                "— examining",
                len(rows), max_days,
            )
        for row in rows:
            ok, reason = _eligible_by_time(
                row, now=now, max_days=max_days, protect_ro=protect_ro,
            )
            if not ok:
                if reason == "ro_protected":
                    protected += 1
                continue
            bytes_freed += _delete_clip_files(row, recordings)
            _delete_index_row(db, row["id"])
            deleted_time += 1
            _broadcast(sink, row["basename"], "time")
            if deleted_time % 10 == 0:
                log.info(
                    "retention sweep: %d/%d clip(s) deleted "
                    "(%.1f MB freed so far)",
                    deleted_time, len(rows), bytes_freed / (1 << 20),
                )

    # Phase 2: disk-pressure.
    deleted_disk = 0
    if disk_pct > 0:
        deleted_disk, freed_2, protected_2 = _disk_pressure_pass(
            db, recordings,
            disk_pct=disk_pct,
            protect_ro=protect_ro,
            sink=sink,
        )
        bytes_freed += freed_2
        protected += protected_2

    summary = {
        "deleted_time": deleted_time,
        "deleted_disk": deleted_disk,
        "protected": protected,
        "bytes_freed": bytes_freed,
    }
    if deleted_time or deleted_disk or protected:
        log.info(
            "retention sweep: %d by time, %d by disk, %d protected, "
            "%.1f MB freed",
            deleted_time, deleted_disk, protected,
            bytes_freed / (1 << 20),
        )
    return summary


_BATCH_SIZE = 16


def _used_pct(recordings: str) -> float:
    du = shutil.disk_usage(recordings)
    if du.total <= 0:
        return 0.0
    return du.used / du.total * 100.0


def _disk_pressure_pass(
    db: Database,
    recordings: str,
    *,
    disk_pct: int,
    protect_ro: bool,
    sink,
) -> tuple[int, int, int]:
    """Delete oldest clips first until disk usage is under the
    threshold or no more eligible candidates remain.

    Disk usage is re-checked at the top of each batch (cheap
    syscall) and again after every individual delete inside the
    batch — the inner check lets us bail the moment we drop
    under the threshold, avoiding overshoot when a single delete
    is already enough.

    If we exit still over-threshold AND ``protect_ro`` is on,
    counts the surviving RO clips and reports them as
    ``protected`` so an operator can see why disk usage didn't
    drop. Returns ``(deleted, bytes_freed, protected)``.
    """
    deleted = 0
    bytes_freed = 0
    while _used_pct(recordings) >= disk_pct:
        where = ""
        if protect_ro:
            where = "WHERE COALESCE(event_type, '') != 'ro'"
        with db.conn() as c:
            rows = [
                dict(r) for r in c.execute(
                    f"SELECT id, path, basename, event_type "
                    f"FROM clip_index {where} "
                    f"ORDER BY timestamp ASC LIMIT ?",
                    (_BATCH_SIZE,),
                ).fetchall()
            ]
        if not rows:
            break
        for row in rows:
            bytes_freed += _delete_clip_files(row, recordings)
            _delete_index_row(db, row["id"])
            deleted += 1
            _broadcast(sink, row["basename"], "disk")
            if _used_pct(recordings) < disk_pct:
                return deleted, bytes_freed, 0

    protected = 0
    if protect_ro and _used_pct(recordings) >= disk_pct:
        with db.conn() as c:
            protected = c.execute(
                "SELECT COUNT(*) AS n FROM clip_index "
                "WHERE COALESCE(event_type, '') = 'ro'"
            ).fetchone()["n"]
    return deleted, bytes_freed, protected
