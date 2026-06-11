"""On-demand filmstrip preview for a finished export job.

A small horizontal sprite of ``N_FRAMES`` frames sampled evenly across the
exported video, cached at ``$RECORDINGS/.export_previews/<job_id>.jpg``. The
UI shows one frame and scrubs through them on hover. Generation reuses the
montage core in :mod:`filmstrip`; this module only picks the timestamps and
the cache location, keyed by export-job id rather than clip id. Generated
lazily on first request, like the clip thumb/filmstrip caches.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time

from . import durations, filmstrip

log = logging.getLogger("viofosync.export_preview")

N_FRAMES = 10                 # fixed strip length, independent of duration
PREVIEW_DIR_NAME = ".export_previews"
_MAX_CONCURRENCY = 3

# Per-event-loop semaphore (matches filmstrip.py): a module-level Semaphore
# binds to whichever loop first touches it, which breaks pytest's
# function-scoped loops; keying by the running loop is correct in both.
_sems: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}


def _semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _sems.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(_MAX_CONCURRENCY)
        _sems[loop] = sem
    return sem


def preview_path(recordings: str, job_id: int) -> str:
    # Pure computation — no makedirs here. The directory is created
    # at generation time; a path helper with filesystem side effects
    # meant a mere GET /api/exports wrote to disk (and bare-MagicMock
    # test providers littered the repo root with MagicMock/ dirs).
    return os.path.join(recordings, PREVIEW_DIR_NAME, f"{job_id}.jpg")


def preview_timestamps(duration_s: float | None, n: int = N_FRAMES) -> list[float]:
    """``n`` timestamps (seconds) at the midpoints of ``n`` equal slices of the
    video, so the strip avoids the dead frames at the very start/end. Degrades
    to a single frame at t=0 when the duration is unknown/zero."""
    if not duration_s or duration_s <= 0:
        return [0.0]
    return [(i + 0.5) * duration_s / n for i in range(n)]


async def ensure_export_preview(
    recordings: str, job_id: int, output_path: str | None,
    duration_s: float | None,
) -> str | None:
    """Return the cached preview sprite path for ``job_id``, generating it on
    first call. ``None`` if ffmpeg is unavailable, the output is missing, or
    generation fails."""
    sp = preview_path(recordings, job_id)
    if os.path.exists(sp) and os.path.getsize(sp) > 0:
        return sp
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None or not (output_path and os.path.isfile(output_path)):
        return None
    if not duration_s or duration_s <= 0:
        duration_s = await durations.probe_duration(output_path) or 0.0
    timestamps = preview_timestamps(duration_s)
    log.info("export preview: generating job=%s (%d frames)", job_id, len(timestamps))
    started = time.monotonic()
    os.makedirs(os.path.dirname(sp), exist_ok=True)
    async with _semaphore():
        ok = await filmstrip.generate_sprite_at(ffmpeg, output_path, sp, timestamps)
    elapsed = time.monotonic() - started
    if not ok:
        log.warning(
            "export preview: job=%s generation failed after %.1fs", job_id, elapsed)
        return None
    log.info("export preview: job=%s done in %.1fs", job_id, elapsed)
    return sp
