"""ffmpeg export worker.

Jobs live in the ``export_jobs`` SQLite table. A single
:class:`ExportWorker` background task pulls them FIFO, shells
out to ffmpeg, and parses ``-progress pipe:1`` output so the
frontend can show a progress bar via the same WebSocket the
downloader uses.

Job types:
  * ``join_front`` — concat demuxer on front clips only
  * ``join_rear``  — same for rear
  * ``pip``        — picture-in-picture: front fullscreen +
                     rear inset. Requires paired clips.
  * ``pip_rear``   — picture-in-picture with rear fullscreen +
                     front inset. Requires paired clips.

Outputs land in ``$RECORDINGS/.exports/{job_id}.mp4`` and are
served by the archive router via a standard ``FileResponse``.

If ffmpeg isn't installed, jobs are rejected at creation time
with a 503 so the UI can tell the user why.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from typing import List, Optional


class _ExportCancelled(Exception):
    """Raised inside the worker when the running job is deleted/cancelled, so
    _run_job unwinds — cleaning its temp dirs via its ``finally`` blocks —
    without marking the (now-deleted) row as failed."""

from ..db import Database
from ..settings import SettingsProvider
from . import durations, export_preview
from . import tasks as _tasks
from .naming import channel_of

log = logging.getLogger("viofosync.exporter")

EXPORT_DIR_NAME = ".exports"
PROGRESS_TIME_RE = re.compile(r"out_time_ms=(\d+)")


def exports_dir(recordings: str) -> str:
    d = os.path.join(recordings, EXPORT_DIR_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def _output_path(recordings: str, job_id: int) -> str:
    return os.path.join(exports_dir(recordings), f"{job_id}.mp4")


def _partial_path(recordings: str, job_id: int) -> str:
    """ffmpeg writes here; the verified result is renamed onto the
    canonical path. A failed/cancelled job leaves only this, which is
    then removed — so a partial never lands at the final name (where
    it would be unreferenced yet count against the quota).

    The ``.part`` marker goes *before* the extension ({id}.part.mp4, not
    {id}.mp4.part): ffmpeg picks its muxer from the filename extension, so
    a bare ``.part`` tail makes it fail with "Unable to choose an output
    format". This mirrors the ``.part.jpg`` staging in thumbs/filmstrip."""
    base = _output_path(recordings, job_id)
    root, ext = os.path.splitext(base)
    return f"{root}.part{ext}"


def sweep_orphan_exports(db: Database, recordings: str) -> int:
    """Remove leftover files in .exports that no completed job owns:
    any staged partial (``*.part.mp4``, or legacy ``*.mp4.part``) from a
    crashed render — no job survives a restart — and any ``{id}.mp4``
    without a matching ``done`` row. Returns the count removed. Intended
    for the lifespan startup hook."""
    edir = os.path.join(recordings, EXPORT_DIR_NAME)
    if not os.path.isdir(edir):
        return 0
    with db.conn() as c:
        done = {
            r["output_path"] for r in c.execute(
                "SELECT output_path FROM export_jobs "
                "WHERE state='done' AND output_path IS NOT NULL"
            ).fetchall()
        }
    removed = 0
    for name in os.listdir(edir):
        path = os.path.join(edir, name)
        if name.endswith(".part") or (
            name.endswith(".mp4") and path not in done
        ):
            try:
                os.remove(path)
                removed += 1
            except OSError:  # pragma: no cover — best-effort
                pass
    if removed:
        log.info("removed %d orphaned export file(s) from %s", removed, edir)
    return removed


# Minimum trim length; sub-frame slivers from clamping are dropped.
_MIN_PIECE_S = 0.05


def build_switch_pieces(segments: list, clips: list) -> list:
    """Turn a timeline-export plan into an ordered list of trims.

    ``segments`` is ``[{channel, start_ts, end_ts}, ...]`` (in output
    order). ``clips`` is ``[{path, channel, start_ts, duration_s}, ...]``
    (``channel`` already derived via ``naming.channel_of``). Returns
    ``[{path, ss, t}, ...]`` — each piece trims ``path`` from offset
    ``ss`` for duration ``t`` seconds. Clips are clamped to the segment
    window; pieces shorter than ``_MIN_PIECE_S`` are dropped.
    """
    pieces: list = []
    for seg in segments:
        s, e, ch = seg["start_ts"], seg["end_ts"], seg["channel"]
        seg_clips = sorted(
            (
                c for c in clips
                if c["channel"] == ch
                and c["start_ts"] < e
                and c["start_ts"] + (c.get("duration_s") or 0) > s
            ),
            key=lambda c: c["start_ts"],
        )
        for c in seg_clips:
            cs = c["start_ts"]
            ce = cs + (c.get("duration_s") or 0)
            in_ = max(s, cs) - cs
            out_ = min(e, ce) - cs
            if out_ - in_ >= _MIN_PIECE_S:
                pieces.append({
                    "path": c["path"],
                    # float() so integer unix timestamps still yield
                    # float offsets (consistent ffmpeg -ss/-t strings).
                    "ss": round(float(in_), 3),
                    "t": round(float(out_ - in_), 3),
                })
    return pieces


def reconcile_orphan_jobs(db: Database) -> int:
    """Mark rows stuck at ``state='running'`` as failed.

    Intended caller is the lifespan startup hook: if the export
    worker crashed (or the container was replaced) mid-render,
    those rows would otherwise stay "running" forever in the UI.

    Returns the number of rows updated.
    """
    with db.write() as c:
        # 'paused' jobs are a SIGSTOP'd ffmpeg child that the restart killed,
        # so they can't resume either — reconcile them too.
        cur = c.execute(
            "UPDATE export_jobs "
            "SET state='failed', "
            "    error='interrupted by container restart', "
            "    finished_at=? "
            "WHERE state IN ('running', 'paused')",
            (int(time.time()),),
        )
        return cur.rowcount


# How far before clip_start a source clip may begin and still overlap
# a timeline export's window (clips run 1–5 min; 10 min is generous).
_TIMELINE_PROTECT_MARGIN_S = 600


def export_protect_ids(db: Database) -> frozenset[int]:
    """Clip ids that pending/active export jobs will read.

    Retention passes this as ``protect_ids`` so the sweep can't
    delete a source file mid-render (multi-segment jobs open inputs
    per segment — a vanished clip fails the job with ENOENT).
    join/pip jobs name their clips outright; timeline jobs resolve
    clips at run time by channel + time, so those are protected by
    timestamp range with a one-clip margin before the window.
    """
    ids: set[int] = set()
    if db is None:  # tests run the sync worker without a DB
        return frozenset()
    with db.conn() as c:
        rows = c.execute(
            "SELECT clip_ids, clip_start, clip_end FROM export_jobs "
            "WHERE state IN ('queued', 'running', 'paused')"
        ).fetchall()
        for r in rows:
            try:
                payload = json.loads(r["clip_ids"] or "null")
            except ValueError:
                continue
            if isinstance(payload, dict) and payload.get("clip_ids"):
                ids.update(int(i) for i in payload["clip_ids"])
                continue
            if isinstance(payload, list):
                ids.update(int(i) for i in payload)
                continue
            # Timeline job: protect everything overlapping its window.
            if r["clip_start"] is not None and r["clip_end"] is not None:
                hits = c.execute(
                    "SELECT id FROM clip_index "
                    "WHERE timestamp BETWEEN ? AND ?",
                    (r["clip_start"] - _TIMELINE_PROTECT_MARGIN_S,
                     r["clip_end"]),
                ).fetchall()
                ids.update(h["id"] for h in hits)
    return frozenset(ids)


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


# H.264 encoder options we know about. ``software`` is always
# listed because libx264 ships with every ffmpeg build; the
# others depend on the host's ffmpeg + hardware.
_ENCODER_NEEDLES = {
    "software": "libx264",
    "videotoolbox": "h264_videotoolbox",   # Apple Silicon / Intel Mac
    "nvenc": "h264_nvenc",                 # NVIDIA
    "vaapi": "h264_vaapi",                 # Linux AMD / Intel iGPU
    "qsv": "h264_qsv",                     # Intel Quick Sync
}


def _probe_encoders_sync() -> str:
    """Blocking ffmpeg -encoders call. Kept synchronous on purpose
    so we can run it in a worker thread — asyncio's subprocess
    machinery has been observed to hang on older kernels (e.g.
    Synology DSM 7) where the child becomes a zombie but the
    child-watcher never wakes, blocking startup forever."""
    try:
        result = subprocess.run(
            [shutil.which("ffmpeg") or "ffmpeg",
             "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=15,
        )
        return result.stdout
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("ffmpeg encoder probe failed: %s", e)
        return ""


def _test_encoder_sync(encoder: str) -> bool:
    """Try to encode a 1-frame synthetic clip with the given
    encoder. Returns True iff ffmpeg exits 0.

    ``ffmpeg -encoders`` only tells us what was compiled in. A
    hardware encoder (qsv/vaapi/nvenc) can be present in the
    build but fail at runtime when its kernel driver, GPU
    device node, or VA-API library isn't reachable from inside
    the container — common on Synology where ``/dev/dri``
    isn't mapped through by default. The 1-frame test exercises
    the exact init path the real export uses, so anything that
    survives this is genuinely usable.

    QSV takes a dedicated branch (its own device-init + scale_qsv command)
    rather than the generic path below."""
    if encoder == "software":
        # libx264 ships with every ffmpeg build; the -encoders
        # presence check is enough.
        return True
    if encoder == "qsv":
        # Exercise the real QSV init path: device creation (the step that
        # returned MFX session -9 on Alpine), a VPP filter, and the encoder.
        # lavfi yields software frames, so we hwupload here; the real export
        # uses -hwaccel qsv decode instead, a strictly easier init once the
        # MFX session exists.
        cmd = [
            shutil.which("ffmpeg") or "ffmpeg",
            "-hide_banner", "-loglevel", "error",
            "-init_hw_device", "qsv=hw", "-filter_hw_device", "hw",
            "-f", "lavfi",
            "-i", "color=size=64x64:duration=0.1:rate=1",
            # extra_hw_frames=16: a small surface pool for the upload — enough
            # to satisfy the QSV VPP/encoder without reserving GPU memory.
            "-vf", "format=nv12,hwupload=extra_hw_frames=16,scale_qsv=64:64",
            "-c:v", "h264_qsv", "-global_quality", "23",
            "-frames:v", "1",
            "-f", "null", "-",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=10)
            return result.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False
    # Exercise the REAL pipeline a filtered export uses: init the hw device
    # and run a filter (here a no-op format/hwupload for vaapi) before the
    # encoder. A bare encoder test passes for vaapi even though every real
    # export fails, because ffmpeg auto-inserts the upload only when there's
    # no explicit filter chain — a false positive we must not repeat.
    upload = _hw_upload_filter(encoder)
    vf = (["-vf", upload] if upload else [])
    cmd = [
        shutil.which("ffmpeg") or "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        *_hw_init_args(encoder),
        "-f", "lavfi",
        "-i", "color=size=64x64:duration=0.1:rate=1",
        *vf,
        *video_codec_args(encoder),
        "-frames:v", "1",
        "-f", "null", "-",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


async def probe_encoders() -> dict:
    """Probe which H.264 encoders this host can actually run.

    Two-pass: first a cheap ``ffmpeg -encoders`` to filter to
    what's compiled in, then a real 1-frame test encode for each
    candidate to weed out hardware encoders whose runtime is
    broken (no GPU passthrough, missing driver, container
    permissions). Result is cached on ``app.state.export_encoders``
    after boot, so the test encodes run exactly once.
    """
    if not ffmpeg_available():
        return {k: False for k in _ENCODER_NEEDLES}
    text = await asyncio.to_thread(_probe_encoders_sync)
    compiled_in = {
        name: (needle in text)
        for name, needle in _ENCODER_NEEDLES.items()
    }

    async def _check(name: str, present: bool) -> tuple[str, bool]:
        if not present:
            return name, False
        works = await asyncio.to_thread(_test_encoder_sync, name)
        if not works and name != "software":
            log.info(
                "encoder %s is compiled in but failed runtime test "
                "— skipping (likely needs GPU passthrough or driver)",
                name,
            )
        return name, works

    pairs = await asyncio.gather(
        *(_check(name, present) for name, present in compiled_in.items())
    )
    return dict(pairs)


# PiP overlay coordinates per corner. ffmpeg's overlay filter
# uses W/H = main video dimensions and w/h = overlay dimensions;
# 20px from the chosen edges keeps the inset clear of safe-area
# crop on most playback paths.
_PIP_OVERLAY_COORDS = {
    "top_right":    "W-w-20:20",
    "top_left":     "20:20",
    "bottom_right": "W-w-20:H-h-20",
    "bottom_left":  "20:H-h-20",
}


def _scale_filter(w: int, h: int, encoder: str) -> str:
    """Full-frame scale filter in the right dialect for ``encoder``.

    QSV runs the scaler on the GPU (``scale_qsv``) and omits ``setsar`` —
    that filter can't operate on QSV surfaces and SAR is carried by the
    encoder instead. Every other encoder uses the software ``scale`` plus
    ``setsar=1`` (VAAPI then appends hwupload via :func:`_with_upload`)."""
    if encoder == "qsv":
        return f"scale_qsv=w={w}:h={h}"
    return f"scale={w}:{h},setsar=1"


def _pip_filter_complex(
    position: str, main: str = "front", encoder: str = "software",
) -> str:
    """Build the -filter_complex argument for the PiP overlay.

    ffmpeg input 0 is the front clip, input 1 is the partner clip
    (rear, tele or interior). ``main`` chooses which is the
    fullscreen base layer; the other is scaled to 1/4 size and
    overlaid. ``main="front"`` (default) reproduces the original
    front-fullscreen behaviour; any other value (rear / tele /
    interior) makes the partner fullscreen with the front inset.
    Unknown ``position`` values fall back to ``top_right`` so a
    typo doesn't break ffmpeg invocation entirely.
    """
    coords = _PIP_OVERLAY_COORDS.get(
        position, _PIP_OVERLAY_COORDS["top_right"],
    )
    # ``main`` is derived from the job type (front / rear / tele /
    # interior); anything that isn't "front" means partner-main,
    # matching the lenient position handling above.
    base, inset = ("0", "1") if main == "front" else ("1", "0")
    if encoder == "qsv":
        # GPU composition: scale_qsv shrinks the inset, overlay_qsv composes
        # on the iGPU. overlay_qsv takes x=/y= (the legacy overlay's single
        # "x:y" positional form isn't accepted), so split the coord pair.
        x, y = coords.split(":")
        return (
            f"[{inset}:v]scale_qsv=w=iw/4:h=ih/4[pip];"
            f"[{base}:v][pip]overlay_qsv=x={x}:y={y}"
        )
    return (
        f"[{inset}:v]scale=iw/4:ih/4[pip];"
        f"[{base}:v][pip]overlay={coords}"
    )


def video_codec_args(encoder: str) -> List[str]:
    """ffmpeg ``-c:v`` arguments for a given encoder. Unknown
    values fall back to software libx264 so a typo can't brick
    an export."""
    if encoder == "videotoolbox":
        return ["-c:v", "h264_videotoolbox", "-b:v", "8M"]
    if encoder == "nvenc":
        return [
            "-c:v", "h264_nvenc", "-preset", "p5", "-cq", "23",
        ]
    if encoder == "qsv":
        # ICQ (intelligent constant quality): -global_quality acts as the
        # ICQ quality level when no bitrate is set — QSV's best quality-per-bit
        # mode on Gen 9.5. look_ahead is disabled: Gen 9.5's LA is weak and
        # only adds latency. QP 23 ≈ the VAAPI CQP 24 used above.
        return [
            "-c:v", "h264_qsv", "-global_quality", "23", "-look_ahead", "0",
        ]
    if encoder == "vaapi":
        # Constant-QP rate control; pairs with the format=nv12,hwupload the
        # filter chain adds and the -vaapi_device global arg.
        return ["-c:v", "h264_vaapi", "-rc_mode", "CQP", "-qp", "24"]
    # ``software`` (default / fallback) — widely compatible.
    return ["-c:v", "libx264", "-preset", "fast", "-crf", "23"]


# The DRM render node VAAPI uploads/encodes through. Standard on a
# single-iGPU NAS; setuid.sh already grants the app user access to it.
VAAPI_RENDER_NODE = "/dev/dri/renderD128"


def _hw_init_args(encoder: str) -> List[str]:
    """Global ffmpeg args to initialise a hardware device for ``encoder``.

    VAAPI needs an explicit render node bound before the inputs. The others
    we support (videotoolbox, nvenc) derive their device implicitly, and
    software needs nothing. QSV creates a shared "qsv=hw" device (used by
    decode, the VPP filters via -filter_hw_device, and the encoder) so
    frames stay on the GPU end to end.
    """
    if encoder == "qsv":
        # One QSV device shared by decode, the VPP filters (scale_qsv/
        # overlay_qsv via -filter_hw_device) and the encoder, so frames
        # never leave the GPU.
        return ["-init_hw_device", "qsv=hw", "-filter_hw_device", "hw"]
    if encoder == "vaapi":
        return ["-vaapi_device", VAAPI_RENDER_NODE]
    return []


def _hw_decode_args(encoder: str) -> List[str]:
    """Per-input flags that decode on the GPU and keep frames there.

    QSV exports run the whole chain on the iGPU: these go *before* each
    ``-i`` so ffmpeg decodes into QSV surfaces that scale_qsv/overlay_qsv
    and h264_qsv consume without a GPU->RAM round trip. Every other
    encoder decodes on the CPU (VAAPI uploads later via hwupload), so they
    get nothing here."""
    if encoder == "qsv":
        return ["-hwaccel", "qsv", "-hwaccel_output_format", "qsv"]
    return []


def _hw_upload_filter(encoder: str) -> str:
    """Filter that moves software frames onto the GPU before a hardware
    encoder that requires it. VAAPI does (``h264_vaapi`` only accepts VAAPI
    surfaces); videotoolbox/nvenc accept software frames directly, so they
    get ``""``. Append to the end of a ``-vf`` / ``-filter_complex`` chain."""
    if encoder == "vaapi":
        return "format=nv12,hwupload"
    return ""


def _with_upload(chain: str, encoder: str) -> str:
    """Append the hardware-upload filter to ``chain`` when ``encoder`` needs
    it, else return ``chain`` unchanged."""
    up = _hw_upload_filter(encoder)
    return f"{chain},{up}" if up else chain


class ExportWorker:
    def __init__(
        self,
        db: Database,
        provider: SettingsProvider,
        broadcast,  # callable(dict) -> awaitable — WebSocket hub
    ) -> None:
        self.db = db
        self._provider = provider
        self.broadcast = broadcast
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        # Control of the one job running right now. Only the worker loop and
        # the (same-event-loop) HTTP handlers touch these, so no locking.
        self._current_job_id: Optional[int] = None
        self._current_proc: Optional[asyncio.subprocess.Process] = None
        self._cancel_current = False
        self._paused = False
        self._resume = asyncio.Event()
        self._resume.set()   # not paused

    def _set_state(self, job_id: int, state: str) -> None:
        with self.db.write() as c:
            c.execute(
                "UPDATE export_jobs SET state=? WHERE id=?", (state, job_id)
            )

    async def pause(self, job_id: int) -> bool:
        """Freeze the running job's encoder (SIGSTOP) and mark it paused.
        Returns False if ``job_id`` isn't the job currently running."""
        if job_id != self._current_job_id:
            return False
        self._paused = True
        self._resume.clear()
        if self._current_proc is not None:
            with contextlib.suppress(Exception):
                self._current_proc.send_signal(signal.SIGSTOP)
        self._set_state(job_id, "paused")
        await self.broadcast(
            {"type": "export_state", "job_id": job_id, "state": "paused"}
        )
        return True

    async def resume(self, job_id: int) -> bool:
        """Resume a paused job (SIGCONT) and mark it running again."""
        if job_id != self._current_job_id:
            return False
        self._paused = False
        self._resume.set()
        if self._current_proc is not None:
            with contextlib.suppress(Exception):
                self._current_proc.send_signal(signal.SIGCONT)
        self._set_state(job_id, "running")
        await self.broadcast(
            {"type": "export_state", "job_id": job_id, "state": "running"}
        )
        return True

    async def cancel(self, job_id: int) -> bool:
        """Kill the running job's encoder so a delete-in-progress actually
        stops the ffmpeg work. Returns False if ``job_id`` isn't running.
        The worker unwinds via _ExportCancelled (no 'failed' row)."""
        if job_id != self._current_job_id:
            return False
        self._cancel_current = True
        self._paused = False
        self._resume.set()   # unblock a paused job so it can unwind
        if self._current_proc is not None:
            with contextlib.suppress(Exception):
                self._current_proc.send_signal(signal.SIGCONT)  # unfreeze first
            with contextlib.suppress(Exception):
                self._current_proc.kill()
        return True

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        # Unwind any in-flight job: a paused child is SIGSTOP'd and
        # must be resumed before the kill can take effect, and a
        # running encoder won't finish inside the shutdown timeout.
        # Without this the ffmpeg child outlives the server (forever,
        # if frozen). The job row is reconciled to 'failed' on next
        # boot by reconcile_orphan_jobs.
        self._cancel_current = True
        self._paused = False
        self._resume.set()
        proc = self._current_proc
        if proc is not None:
            with contextlib.suppress(Exception):
                proc.send_signal(signal.SIGCONT)
            with contextlib.suppress(Exception):
                proc.kill()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
                # Await the cancellation so the job's cleanup
                # (temp dirs, subprocess reaping) runs before the
                # loop tears down.
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task

    # ---- Job creation (called from the HTTP route) ----

    def enqueue(
        self,
        job_type: str,
        clip_ids: List[int],
        encoder: str = "software",
    ) -> int:
        if not ffmpeg_available():
            raise RuntimeError("ffmpeg not installed on this host")
        if job_type not in (
            "join_front", "join_rear", "join_tele", "join_interior",
            "pip", "pip_rear", "pip_tele", "pip_interior",
        ):
            raise ValueError(f"unknown job type: {job_type}")
        if not clip_ids:
            raise ValueError("no clips selected")

        # clip_ids + encoder share one JSON column to avoid a
        # schema migration. The reader accepts both this dict
        # form and legacy rows that stored a bare list.
        payload = json.dumps({
            "clip_ids": clip_ids,
            "encoder": encoder,
        })
        with self.db.write() as c:
            # Snapshot the footage date range now — the source clips
            # may be retention-pruned long before the export is.
            ph = ",".join("?" * len(clip_ids))
            rng = c.execute(
                f"SELECT MIN(timestamp) AS lo, MAX(timestamp) AS hi "
                f"FROM clip_index WHERE id IN ({ph})",
                clip_ids,
            ).fetchone()
            cur = c.execute(
                """
                INSERT INTO export_jobs
                    (type, clip_ids, state, created_at,
                     clip_start, clip_end)
                VALUES (?, ?, 'queued', ?, ?, ?)
                """,
                (job_type, payload, int(time.time()),
                 rng["lo"], rng["hi"]),
            )
            return cur.lastrowid

    def enqueue_timeline(self, segments: list, encoder: str = "software") -> int:
        if not ffmpeg_available():
            raise RuntimeError("ffmpeg not installed on this host")
        if not segments:
            raise ValueError("no segments")
        for s in segments:
            if "channel" not in s or "start_ts" not in s or "end_ts" not in s:
                raise ValueError("segment missing channel/start_ts/end_ts")
            if not (s["end_ts"] > s["start_ts"]):
                raise ValueError("segment end_ts must be after start_ts")

        payload = json.dumps({"segments": segments, "encoder": encoder})
        clip_start = int(min(s["start_ts"] for s in segments))
        clip_end = int(max(s["end_ts"] for s in segments))
        with self.db.write() as c:
            cur = c.execute(
                """
                INSERT INTO export_jobs
                    (type, clip_ids, state, created_at, clip_start, clip_end)
                VALUES ('timeline', ?, 'queued', ?, ?, ?)
                """,
                (payload, int(time.time()), clip_start, clip_end),
            )
            return cur.lastrowid

    # ---- Background loop ----

    async def _run(self) -> None:
        while not self._stop.is_set():
            job = await self._pop_next()
            if job is None:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=2.0
                    )
                except asyncio.TimeoutError:
                    pass
                continue
            await self._process(job)

    async def _process(self, job: dict) -> None:
        """Run one job, translating a cancellation (delete-in-progress) into
        a clean discard and any real error into a 'failed' row. Always resets
        the per-job control state afterwards."""
        try:
            await self._run_job(job)
        except _ExportCancelled:
            log.info("export job %d cancelled — discarded", job["id"])
            self._discard_partial(job["id"])
        except Exception as e:  # pragma: no cover
            log.exception("export job %d failed", job["id"])
            self._finish(job["id"], False, str(e), None)
        finally:
            self._current_job_id = None
            self._current_proc = None
            self._cancel_current = False
            self._paused = False
            self._resume.set()

    async def _pop_next(self) -> Optional[dict]:
        # The write transaction contends with worker threads for the
        # DB lock (scanner flush, retention) — wait off the loop.
        return await asyncio.to_thread(self._pop_next_sync)

    def _pop_next_sync(self) -> Optional[dict]:
        with self.db.write() as c:
            row = c.execute(
                "SELECT * FROM export_jobs "
                "WHERE state='queued' "
                "ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            c.execute(
                "UPDATE export_jobs "
                "SET state='running', started_at=? WHERE id=?",
                (int(time.time()), row["id"]),
            )
            self._current_job_id = row["id"]
            self._current_proc = None
            self._cancel_current = False
            self._paused = False
            self._resume.set()
            return dict(row)

    def _finish(
        self,
        job_id: int,
        ok: bool,
        err: Optional[str],
        output_path: Optional[str],
    ) -> None:
        state = "done" if ok else "failed"
        # output_path arrives as the staged .part name. On success
        # promote it to the canonical {job_id}.mp4; on failure (here
        # or via the explicit None callers pass) drop any partial so
        # .exports doesn't accumulate unreferenced, quota-counting
        # junk. Cleanup is also keyed by job_id so it works even when
        # the caller passed output_path=None.
        if ok and output_path:
            # Staged name is {id}.part.mp4 -> promote to {id}.mp4 by
            # dropping the ".part" marker that sits before the extension.
            root, ext = os.path.splitext(output_path)
            final = root[: -len(".part")] + ext if root.endswith(".part") \
                else output_path
            if final != output_path:
                try:
                    os.replace(output_path, final)
                except OSError:
                    log.exception("export %d: rename of %s failed",
                                  job_id, output_path)
                    ok = False
                    state = "failed"
                    err = err or "could not finalise output"
                    output_path = None
                else:
                    output_path = final
        if not ok:
            self._discard_partial(job_id)
        # progress is REAL NOT NULL — only force it to 1.0 on
        # success. On failure leave it alone so users can see
        # how far the job got. (Earlier we wrote NULL here, which
        # violated the constraint and crashed _finish, leaving
        # the job stuck at state='running' with no broadcast —
        # the UI sat at 0% with no failure indication.)
        # Snapshot the finished output's size + length onto the row. Size is a
        # cheap stat; duration reads just the MP4 mvhd box (~108 bytes, sync and
        # fast — our outputs always carry one). Both stay NULL on failure or if
        # the probe can't read them, and the UI shows "—".
        size = None
        duration = None
        if ok and output_path:
            try:
                size = os.path.getsize(output_path)
            except OSError:
                size = None
            try:
                duration = durations._probe_duration_mvhd(output_path)
            except Exception:  # pragma: no cover - best-effort metadata
                duration = None
        with self.db.write() as c:
            if ok:
                c.execute(
                    "UPDATE export_jobs SET state=?, error=?, "
                    "output_path=?, progress=1.0, finished_at=?, "
                    "output_size=?, output_duration_s=? "
                    "WHERE id=?",
                    (state, err, output_path,
                     int(time.time()), size, duration, job_id),
                )
            else:
                c.execute(
                    "UPDATE export_jobs SET state=?, error=?, "
                    "output_path=?, finished_at=? WHERE id=?",
                    (state, err, output_path,
                     int(time.time()), job_id),
                )
        _tasks.spawn(
            self.broadcast(
                {
                    "type": "export_finished",
                    "job_id": job_id,
                    "ok": ok,
                    "error": err,
                }
            ),
            name=f"export-finished-{job_id}",
        )
        if ok and output_path:
            _tasks.spawn(
                self._make_export_preview(job_id, output_path),
                name=f"export-preview-{job_id}",
            )

    def _discard_partial(self, job_id: int) -> None:
        """Remove a job's staged .part output (best-effort)."""
        rec = getattr(self._provider.get(), "recordings", None)
        if not isinstance(rec, str):
            return
        try:
            os.remove(_partial_path(rec, job_id))
        except OSError:
            pass

    async def _make_export_preview(self, job_id: int, output_path: str) -> None:
        """Generate the job's filmstrip preview once, after it finishes, so the
        HTTP endpoint only ever serves a cached file (no request-time ffmpeg).
        Best-effort: a failure here must never affect the export itself."""
        try:
            recordings = self._provider.get().recordings
            sp = await export_preview.ensure_export_preview(
                recordings, job_id, output_path, None,
            )
            # Tell the UI the strip is ready so it can swap the "generating"
            # placeholder for the real (hover-scrub) filmstrip. Only on success
            # — a None means the placeholder simply stays put.
            if sp:
                await self.broadcast(
                    {"type": "export_preview_ready", "job_id": job_id}
                )
        except Exception:  # pragma: no cover - best-effort
            log.exception("export preview generation failed for job %d", job_id)

    def _fetch_clips(self, clip_ids: List[int]) -> List[dict]:
        ph = ",".join("?" * len(clip_ids))
        with self.db.conn() as c:
            rows = c.execute(
                f"SELECT id, path, basename, camera, event_type, "
                f"timestamp, sequence FROM clip_index "
                f"WHERE id IN ({ph})",
                clip_ids,
            ).fetchall()
        return sorted(
            (dict(r) for r in rows),
            key=lambda r: (r["timestamp"], r["sequence"]),
        )

    async def _run_job(self, job: dict) -> None:
        snap = self._provider.get()
        raw = json.loads(job["clip_ids"])
        if isinstance(raw, list):
            # Legacy payload shape: bare list of ids.
            clip_ids = raw
            encoder = "software"
        else:
            clip_ids = raw.get("clip_ids", [])
            encoder = raw.get("encoder") or "software"
        # Stage to a .part name; _finish renames the verified result
        # onto the canonical path and removes the partial on failure.
        out = _partial_path(snap.recordings, job["id"])

        if job["type"] == "timeline":
            segments = raw.get("segments", []) if isinstance(raw, dict) else []
            await self._run_timeline(job, segments, encoder, out)
            return

        clips = self._fetch_clips(clip_ids)

        join_wanted = {
            "join_front": "F",
            "join_rear": "R",
            "join_tele": "T",
            "join_interior": "I",
        }
        if job["type"] in join_wanted:
            wanted = join_wanted[job["type"]]
            # ``camera`` may be ``F``, ``R``, ``PF``, ``PR``, etc.
            # The last letter identifies the lens.
            selected = [
                c for c in clips
                if (c["camera"] or "").upper().endswith(wanted)
            ]
            if not selected:
                self._finish(
                    job["id"], False,
                    f"no {wanted} clips selected", None,
                )
                return
            await self._concat(job["id"], selected, out)
        else:  # pip / pip_rear / pip_tele / pip_interior
            # The PiP partner is the non-front camera; ``main``
            # chooses which side is fullscreen. Front is always
            # ffmpeg input 0 (it carries the mic audio).
            partner = {
                "pip_tele": "tele",
                "pip_interior": "interior",
            }.get(job["type"], "rear")
            pairs = self._pair_clips(
                clips, required=("front", partner),
            )
            if not pairs:
                self._finish(
                    job["id"], False,
                    f"no front+{partner} pairs in selection", None,
                )
                return
            main = {
                "pip_rear": "rear",
                "pip_tele": "tele",
                "pip_interior": "interior",
            }.get(job["type"], "front")
            await self._pip(
                job["id"], pairs, out, encoder,
                snap.pip_position, main=main, partner=partner,
            )

    # ---- ffmpeg invocations ----

    @staticmethod
    def _pair_clips(
        clips: List[dict],
        required: tuple = ("front", "rear"),
    ):
        # Viofo gives same-capture clips identical timestamps but
        # consecutive sequences, so key on (timestamp, event_type)
        # and pick the slot from the trailing letter of ``camera``
        # (handles PF/PR/PT/PI too). ``required`` names the slots
        # a group must have to count as a complete pair.
        pairs: dict[tuple[int, str], dict] = {}
        for c in clips:
            cam = (c["camera"] or "").upper()
            kind = c.get("event_type") or "normal"
            key = (c["timestamp"], kind)
            slot = {"F": "front", "T": "tele", "I": "interior"}.get(
                cam[-1:], "rear"
            )
            pairs.setdefault(key, {})[slot] = c
        return [
            p for p in sorted(pairs.items())
            if all(s in p[1] for s in required)
        ]

    async def _concat(
        self, job_id: int, clips: List[dict], out: str
    ) -> None:
        """Use the concat demuxer — fast, no re-encode when
        codecs match (which they do for a single dashcam)."""
        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False
        ) as f:
            list_file = f.name
            for c in clips:
                # Absolute path: ffmpeg's concat demuxer resolves
                # relative entries against the list file's own
                # directory (the temp dir), not our CWD — so a
                # relative clip path (dev boxes with a relative
                # RECORDINGS) would send ffmpeg looking in /tmp.
                # Escape single quotes per the ffmpeg concat docs.
                safe = os.path.abspath(c["path"]).replace("'", "'\\''")
                f.write(f"file '{safe}'\n")

        try:
            total_duration = await self._probe_total(clips)
            rc, err = await self._run_ffmpeg(
                job_id,
                [
                    "-y", "-f", "concat", "-safe", "0",
                    "-i", list_file, "-c", "copy", out,
                ],
                total_duration,
            )
        finally:
            try:
                os.remove(list_file)
            except OSError:
                pass

        if rc == 0 and os.path.exists(out):
            self._finish(job_id, True, None, out)
        else:
            self._finish(
                job_id, False,
                f"ffmpeg exit {rc}: {err}" if err else f"ffmpeg exit {rc}",
                None,
            )

    async def _pip(
        self, job_id: int, pairs, out: str,
        encoder: str = "software",
        position: str = "top_right",
        main: str = "front",
        partner: str = "rear",
    ) -> None:
        """One ffmpeg per pair into a temp dir, then concat.

        ``partner`` names the non-front slot in each pair (rear,
        tele or interior). Front is always ffmpeg input 0 — it
        carries the mic audio, and ``-c:a copy`` with no explicit
        ``-map`` makes ffmpeg's default stream selection pick it.

        Broadcasts segment-level progress so the UI shows
        something meaningful even though each segment is a
        separate ffmpeg invocation."""
        tmp = tempfile.mkdtemp(prefix="vfs_pip_")
        parts: List[str] = []
        total_segments = len(pairs)
        filter_complex = _pip_filter_complex(position, main=main, encoder=encoder)
        try:
            for i, (_, p) in enumerate(pairs):
                # Probe this segment's duration so the inner
                # pump() can emit fine-grained progress instead
                # of sitting silent for minutes. All cameras of a
                # Viofo capture share a duration, so the front clip
                # is a fine reference even for partner-main jobs.
                seg_dur = await self._probe_total(
                    [p["front"]]
                )
                seg = os.path.join(tmp, f"seg_{i:04d}.mp4")
                # Coarse progress tick so the UI moves when a
                # new segment starts even before ffmpeg output
                # comes through.
                await self.broadcast({
                    "type": "export_progress",
                    "job_id": job_id,
                    "progress": i / max(1, total_segments),
                    "stage": f"segment {i + 1}/{total_segments}",
                })
                rc, err = await self._run_ffmpeg(
                    job_id,
                    [
                        *_hw_init_args(encoder),
                        "-y",
                        # decode flags are per-input: repeated before each -i
                        # so QSV decodes both clips straight onto the GPU.
                        *_hw_decode_args(encoder),
                        "-i", p["front"]["path"],
                        *_hw_decode_args(encoder),
                        "-i", p[partner]["path"],
                        # _with_upload appends hwupload for VAAPI only; for QSV
                        # the filter already yields GPU surfaces, so it's a no-op.
                        "-filter_complex",
                        _with_upload(filter_complex, encoder),
                        *video_codec_args(encoder),
                        "-c:a", "copy",
                        seg,
                    ],
                    seg_dur,
                    progress_base=i / max(1, total_segments),
                    progress_span=1.0 / max(1, total_segments),
                    stage=f"segment {i + 1}/{total_segments}",
                )
                if rc != 0:
                    self._finish(
                        job_id, False,
                        f"segment {i + 1} failed (ffmpeg exit {rc}): {err}",
                        None,
                    )
                    return
                parts.append(seg)

            await self.broadcast({
                "type": "export_progress",
                "job_id": job_id,
                "progress": 0.98,
                "stage": "concatenating",
            })

            # Final concat of PiP segments.
            list_file = os.path.join(tmp, "parts.txt")
            with open(list_file, "w") as f:
                for p in parts:
                    f.write(f"file '{p}'\n")
            rc, err = await self._run_ffmpeg(
                job_id,
                [
                    "-y", "-f", "concat", "-safe", "0",
                    "-i", list_file, "-c", "copy", out,
                ],
                None,
                stage="concatenating",
            )
            if rc == 0 and os.path.exists(out):
                self._finish(job_id, True, None, out)
            else:
                self._finish(
                    job_id, False,
                    f"concat failed (ffmpeg exit {rc}): {err}", None,
                )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    async def _run_timeline(self, job, segments, encoder, out) -> None:
        lo = min(s["start_ts"] for s in segments)
        hi = max(s["end_ts"] for s in segments)
        with self.db.conn() as c:
            rows = c.execute(
                """
                SELECT path, camera, timestamp, duration_s
                FROM clip_index
                WHERE timestamp < ?
                  AND timestamp + COALESCE(duration_s, 0) > ?
                """,
                (hi, lo),
            ).fetchall()
        clips = [
            {
                "path": r["path"],
                "channel": channel_of(r["camera"]),
                "start_ts": r["timestamp"],
                "duration_s": r["duration_s"],
            }
            for r in rows
        ]
        pieces = build_switch_pieces(segments, clips)
        if not pieces:
            self._finish(job["id"], False, "no footage in selection", None)
            return

        # Audio comes from ONE continuous front-camera track spanning the whole
        # export, not re-cut at each switch. This is why timeline exports no
        # longer click/jump at switch points: the picture switches cameras while
        # the audio is a single uninterrupted decode of the front channel. Built
        # by asking the same piece-builder for one synthetic front segment over
        # the full [lo, hi] window.
        audio_pieces = build_switch_pieces(
            [{"channel": "front", "start_ts": lo, "end_ts": hi}], clips,
        )

        res = await self._probe_resolution(pieces[0]["path"])
        w, h = res if res else (1920, 1080)
        vf = _with_upload(_scale_filter(w, h, encoder), encoder)

        tmp = tempfile.mkdtemp(prefix="vfs_timeline_")

        async def concat_parts(parts: List[str], dst: str, listname: str):
            """Join same-codec parts with the concat demuxer (no re-encode).
            Returns ``(rc, err)`` without finishing the job."""
            list_file = os.path.join(tmp, listname)
            with open(list_file, "w") as f:
                for p in parts:
                    safe = os.path.abspath(p).replace("'", "'\\''")
                    f.write(f"file '{safe}'\n")
            return await self._run_ffmpeg(
                job["id"],
                ["-y", "-f", "concat", "-safe", "0",
                 "-i", list_file, "-c", "copy", dst],
                None, stage="joining",
            )

        try:
            # --- Video: encode each timeline piece picture-only, then join. ---
            vparts: List[str] = []
            n = len(pieces)
            for i, pc in enumerate(pieces):
                seg = os.path.join(tmp, f"vid_{i:04d}.mp4")
                await self.broadcast({
                    "type": "export_progress", "job_id": job["id"],
                    "progress": 0.6 * i / max(1, n),
                    "stage": f"video {i + 1}/{n}",
                })
                rc, err = await self._run_ffmpeg(
                    job["id"],
                    [
                        *_hw_init_args(encoder),
                        *_hw_decode_args(encoder),
                        "-y",
                        "-ss", str(pc["ss"]),
                        "-i", pc["path"],
                        "-t", str(pc["t"]),
                        "-an",
                        "-vf", vf,
                        *video_codec_args(encoder),
                        seg,
                    ],
                    pc["t"],
                    progress_base=0.6 * i / max(1, n),
                    progress_span=0.6 / max(1, n),
                    stage=f"video {i + 1}/{n}",
                )
                if rc != 0:
                    self._finish(
                        job["id"], False,
                        f"segment {i + 1} failed (ffmpeg exit {rc}): {err}",
                        None,
                    )
                    return
                vparts.append(seg)

            silent = os.path.join(tmp, "video.mp4")
            rc, err = await concat_parts(vparts, silent, "video_parts.txt")
            if rc != 0 or not os.path.exists(silent):
                self._finish(
                    job["id"], False,
                    f"video concat failed (ffmpeg exit {rc}): {err}", None,
                )
                return

            # --- Audio: build the continuous front track (or none if no front). ---
            track: Optional[str] = None
            if audio_pieces:
                aparts: List[str] = []
                m = len(audio_pieces)
                for i, pc in enumerate(audio_pieces):
                    ap = os.path.join(tmp, f"aud_{i:04d}.m4a")
                    await self.broadcast({
                        "type": "export_progress", "job_id": job["id"],
                        "progress": 0.6 + 0.25 * i / max(1, m),
                        "stage": f"audio {i + 1}/{m}",
                    })
                    rc, err = await self._run_ffmpeg(
                        job["id"],
                        [
                            "-y",
                            "-ss", str(pc["ss"]),
                            "-i", pc["path"],
                            "-t", str(pc["t"]),
                            "-vn",
                            "-c:a", "aac",
                            ap,
                        ],
                        pc["t"],
                        progress_base=0.6 + 0.25 * i / max(1, m),
                        progress_span=0.25 / max(1, m),
                        stage=f"audio {i + 1}/{m}",
                    )
                    if rc != 0:
                        self._finish(
                            job["id"], False,
                            f"audio {i + 1} failed (ffmpeg exit {rc}): {err}",
                            None,
                        )
                        return
                    aparts.append(ap)
                track = os.path.join(tmp, "audio.m4a")
                rc, err = await concat_parts(aparts, track, "audio_parts.txt")
                if rc != 0 or not os.path.exists(track):
                    self._finish(
                        job["id"], False,
                        f"audio concat failed (ffmpeg exit {rc}): {err}", None,
                    )
                    return

            await self.broadcast({
                "type": "export_progress", "job_id": job["id"],
                "progress": 0.95, "stage": "muxing",
            })

            if track is None:
                # No front footage anywhere in the span -> silent timeline video.
                shutil.move(silent, out)
                ok = os.path.exists(out)
                self._finish(
                    job["id"], ok,
                    None if ok else "mux produced no output",
                    out if ok else None,
                )
                return

            rc, err = await self._run_ffmpeg(
                job["id"],
                [
                    "-y",
                    "-i", silent,
                    "-i", track,
                    # apad makes the audio effectively endless; -shortest then
                    # trims the output to the (finite) video length. Net effect:
                    # the front audio is padded with silence over any stretch the
                    # front camera didn't cover, keeping A/V locked end-to-end.
                    "-filter_complex", "[1:a]apad[aud]",
                    "-map", "0:v:0",
                    "-map", "[aud]",
                    "-c:v", "copy",
                    "-c:a", "aac",
                    "-shortest",
                    out,
                ],
                None, stage="muxing",
            )
            if rc == 0 and os.path.exists(out):
                self._finish(job["id"], True, None, out)
            else:
                self._finish(
                    job["id"], False,
                    f"mux failed (ffmpeg exit {rc}): {err}", None,
                )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    async def _probe_total(self, clips: List[dict]) -> Optional[float]:
        ffprobe = shutil.which("ffprobe")
        if ffprobe is None:
            return None
        total = 0.0
        for c in clips:
            proc = await asyncio.create_subprocess_exec(
                ffprobe,
                "-v", "error", "-show_entries",
                "format=duration", "-of",
                "default=noprint_wrappers=1:nokey=1",
                c["path"],
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            try:
                total += float(out.decode().strip())
            except ValueError:
                return None
        return total

    async def _probe_resolution(self, path: str):
        """(width, height) of the first video stream, or None."""
        ffprobe = shutil.which("ffprobe")
        if ffprobe is None:
            return None
        proc = await asyncio.create_subprocess_exec(
            ffprobe, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=s=x:p=0", path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        try:
            w, h = out.decode().strip().split("x")
            return int(w), int(h)
        except ValueError:
            return None

    async def _run_ffmpeg(
        self,
        job_id: int,
        args: List[str],
        total_duration: Optional[float],
        *,
        progress_base: float = 0.0,
        progress_span: float = 1.0,
        stage: Optional[str] = None,
    ) -> tuple[int, str]:
        """Run ffmpeg and return (returncode, last-stderr-lines).

        stderr is drained concurrently — without this, a verbose
        ffmpeg would block once the OS pipe buffer filled up,
        hanging the job. We also return the tail of stderr so
        the UI can surface the real error instead of a bare
        ``ffmpeg exit 1``."""
        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-progress", "pipe:1", "-nostats",
            *args,
        ]
        # A delete may have landed between segments — bail before spawning.
        if self._cancel_current:
            raise _ExportCancelled
        # Block here while the job is paused (e.g. paused between segments).
        await self._resume.wait()

        log.info("export job %d: %s", job_id, " ".join(cmd))
        # libva ignores ffmpeg's -loglevel and prints its init handshake
        # ("libva info: va_openDriver() returns 0", driver path, VA-API
        # version) straight to stderr on every QSV/VAAPI process. Our stderr
        # pump tags all stderr as WARNING, so a multi-segment export floods the
        # log with benign driver chatter. LIBVA_MESSAGING_LEVEL=1 silences the
        # info lines at the source while still letting real VA-API errors
        # through. https://github.com/intel/libva — message-level: 0=none,
        # 1=error, 2=info (default).
        env = {**os.environ, "LIBVA_MESSAGING_LEVEL": "1"}
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        self._current_proc = proc
        # If a pause raced in just before the spawn, stop the child now.
        if self._paused:
            with contextlib.suppress(Exception):
                proc.send_signal(signal.SIGSTOP)

        stderr_tail: list[str] = []

        async def pump_stdout():
            assert proc.stdout is not None
            last = 0.0
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode(errors="ignore")
                m = PROGRESS_TIME_RE.search(text)
                if m and total_duration:
                    done_s = int(m.group(1)) / 1_000_000
                    inner = min(1.0, done_s / total_duration)
                    frac = progress_base + inner * progress_span
                    now = time.monotonic()
                    if now - last > 0.25:
                        last = now
                        ev = {
                            "type": "export_progress",
                            "job_id": job_id,
                            "progress": frac,
                        }
                        if stage:
                            ev["stage"] = stage
                        await self.broadcast(ev)

        async def pump_stderr():
            assert proc.stderr is not None
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode(errors="ignore").rstrip()
                if text:
                    log.warning("ffmpeg[%d]: %s", job_id, text)
                    stderr_tail.append(text)
                    # Cap memory — we only need the last few
                    # lines for the error message.
                    if len(stderr_tail) > 20:
                        del stderr_tail[0]

        await asyncio.gather(
            pump_stdout(), pump_stderr(), proc.wait()
        )
        self._current_proc = None
        # If the job was cancelled (its child was killed), unwind cleanly
        # instead of reporting a spurious ffmpeg failure for a deleted row.
        if self._cancel_current:
            raise _ExportCancelled
        rc = proc.returncode or 0
        err = " | ".join(stderr_tail[-3:]) if stderr_tail else ""
        return rc, err
