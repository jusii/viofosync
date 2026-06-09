"""Populate ``clip_index.duration_s`` via ffprobe.

The scanner indexes clips from filenames but never measures their
length. ``duration_s`` drives filmstrip frame counts and the
timeline layout, so probe any clip missing it and store the value.
Mirrors ``scanner.sweep_missing_thumbs``: bounded concurrency,
idempotent (only NULL/zero rows are probed), non-fatal on failure.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil

from ..db import Database

log = logging.getLogger("viofosync.durations")


# mvhd ``duration`` sentinel meaning "unknown" (all bits set), per the
# ISO base media format — 32-bit for a v0 header, 64-bit for v1.
_MVHD_UNKNOWN = {0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF}


def _read_box_header(f):
    """Read an ISO-BMFF box header at the current offset.

    Returns ``(size, type, header_len)`` where ``size`` is the total box
    length including the header (or ``None`` for the size==0 "to EOF" form),
    or ``None`` at EOF / on a short read.
    """
    hdr = f.read(8)
    if len(hdr) < 8:
        return None
    size = int.from_bytes(hdr[:4], "big")
    btype = hdr[4:8]
    header_len = 8
    if size == 1:                       # 64-bit largesize follows (big mdat)
        ext = f.read(8)
        if len(ext) < 8:
            return None
        size = int.from_bytes(ext, "big")
        header_len = 16
    elif size == 0:                     # extends to end of file
        size = None
    return size, btype, header_len


def _find_box(f, target: bytes, region_end: int):
    """Scan sibling boxes from the current offset up to ``region_end`` and
    return ``(payload_start, box_end)`` of the first box of ``target`` type,
    or ``None``. On a match the file is left positioned at ``payload_start``.
    Bails out (None) on a malformed/truncated box rather than looping."""
    while f.tell() + 8 <= region_end:
        start = f.tell()
        hdr = _read_box_header(f)
        if hdr is None:
            return None
        size, btype, header_len = hdr
        box_end = region_end if size is None else start + size
        # ``==`` is a valid empty box (e.g. ffmpeg's zero-payload ``free``);
        # only a box claiming to be smaller than its own header, or running
        # past the parent, is malformed.
        if box_end < start + header_len or box_end > region_end:
            return None
        if btype == target:
            return start + header_len, box_end
        f.seek(box_end)
    return None


def _probe_duration_mvhd(path: str) -> float | None:
    """Clip duration in seconds read directly from the MP4 ``moov/mvhd``
    box — no subprocess. Returns ``None`` when the file isn't a parseable
    MP4, ``mvhd`` is absent, or the duration is unknown, so the caller can
    fall back to ffprobe.

    Only a handful of box headers plus the ~108-byte ``mvhd`` are read; the
    huge ``mdat`` is seeked past, so this is cheap even when ``moov`` is at
    the end of a large file on a slow NAS volume.
    """
    try:
        end = os.path.getsize(path)
        with open(path, "rb") as f:
            moov = _find_box(f, b"moov", end)
            if moov is None:
                return None
            moov_start, moov_end = moov
            f.seek(moov_start)
            mvhd = _find_box(f, b"mvhd", moov_end)
            if mvhd is None:
                return None
            f.seek(mvhd[0])
            version_flags = f.read(4)
            if len(version_flags) < 4:
                return None
            if version_flags[0] == 1:
                buf = f.read(28)        # ctime(8) mtime(8) timescale(4) dur(8)
                if len(buf) < 28:
                    return None
                timescale = int.from_bytes(buf[16:20], "big")
                duration = int.from_bytes(buf[20:28], "big")
            else:
                buf = f.read(16)        # ctime(4) mtime(4) timescale(4) dur(4)
                if len(buf) < 16:
                    return None
                timescale = int.from_bytes(buf[8:12], "big")
                duration = int.from_bytes(buf[12:16], "big")
    except (OSError, ValueError):
        return None
    if timescale <= 0 or duration in _MVHD_UNKNOWN:
        return None
    secs = duration / timescale
    return secs if secs > 0 else None


async def _probe_with_method(path: str) -> tuple[float | None, str | None]:
    """``(duration, method)`` where ``method`` is ``"mvhd"``, ``"ffprobe"``
    or ``None``. The sweep uses this to report how clips were resolved;
    :func:`probe_duration` is the value-only wrapper."""
    secs = await asyncio.to_thread(_probe_duration_mvhd, path)
    if secs is not None:
        return secs, "mvhd"
    secs = await _probe_duration_ffprobe(path)
    return (secs, "ffprobe") if secs is not None else (None, None)


async def probe_duration(path: str) -> float | None:
    """Clip length in seconds. Fast path parses the MP4 ``mvhd`` box
    directly (no subprocess); falls back to ffprobe for anything that
    doesn't parse (odd containers, damaged moov, non-MP4)."""
    secs, _ = await _probe_with_method(path)
    return secs


async def _probe_duration_ffprobe(path: str) -> float | None:
    """Clip length in seconds via ffprobe, or None if ffprobe is
    missing / the probe fails / the value is non-positive."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            ffprobe, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15.0)
    except (TimeoutError, OSError):
        return None
    try:
        d = float(out.decode().strip())
    except ValueError:
        return None
    return d if d > 0 else None


def _flush(db: Database, batch: list[tuple[int, float]]) -> int:
    """Persist a batch of (clip_id, duration) pairs. Returns rows written."""
    if not batch:
        return 0
    with db.write() as c:
        for clip_id, dur in batch:
            c.execute(
                "UPDATE clip_index SET duration_s = ? WHERE id = ?",
                (dur, clip_id),
            )
    return len(batch)


async def sweep_missing_durations(
    db: Database, *, concurrency: int = 4, batch_size: int = 200
) -> int:
    """ffprobe every indexed clip with a NULL/zero ``duration_s`` and
    store the result. Returns the number of rows updated. Idempotent.

    Results are persisted in batches *as they are probed*, not all at the
    end, so an interrupted sweep (server restart/shutdown) keeps the work
    it has already done — successive runs whittle down the remainder
    instead of redoing all ~N clips every boot.
    """
    with db.conn() as c:
        rows = c.execute(
            "SELECT id, path FROM clip_index "
            "WHERE duration_s IS NULL OR duration_s <= 0"
        ).fetchall()
    todo = [(r["id"], r["path"]) for r in rows if os.path.isfile(r["path"])]
    if not todo:
        return 0

    log.info(
        "duration sweep: probing %d clip(s) via mvhd (ffprobe fallback), "
        "concurrency=%d", len(todo), concurrency,
    )
    sem = asyncio.Semaphore(concurrency)

    async def _one(clip_id: int, path: str) -> tuple[int, float | None, str | None]:
        async with sem:
            try:
                dur, method = await _probe_with_method(path)
                return clip_id, dur, method
            except asyncio.CancelledError:
                raise   # shutdown — let it propagate so we flush + stop
            except Exception as e:  # pragma: no cover — non-fatal
                log.warning("duration probe failed for %s: %s", path, e)
                return clip_id, None, None

    tasks = [asyncio.ensure_future(_one(cid, p)) for cid, p in todo]
    updated = 0
    methods = {"mvhd": 0, "ffprobe": 0}
    batch: list[tuple[int, float]] = []
    try:
        for t in tasks:
            clip_id, dur, method = await t
            if dur is not None:
                if method in methods:
                    methods[method] += 1
                batch.append((clip_id, dur))
                if len(batch) >= batch_size:
                    updated += _flush(db, batch)
                    batch = []
        updated += _flush(db, batch)
        batch = []
    finally:
        # On interruption, abandon the rest and persist what we have so
        # the next run resumes from here rather than starting over.
        for t in tasks:
            if not t.done():
                t.cancel()
        updated += _flush(db, batch)
    log.info(
        "duration sweep: %d updated (%d via mvhd, %d via ffprobe)",
        updated, methods["mvhd"], methods["ffprobe"],
    )
    return updated
