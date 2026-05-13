"""SyncWorker — the dashcam download background task.

The car can drive away at any moment, so this worker is built
around the assumption that the dashcam is flaky:

* every cycle starts with a short reachability probe; failure
  is a no-op (not an error) and we back off exponentially;
* a single in-flight download holds the worker; the download's
  HTTP read loop polls ``cancel_check`` so we can abort within
  one chunk when the probe fails or the user hits Stop;
* transient socket errors return the item to ``pending`` with
  an incremented attempt counter, not ``failed``;
* every event (reachability change, queue mutation, per-item
  progress) is pushed to the WebSocket hub so the UI sees live
  state without polling.

The actual downloading is delegated to
``viofosync_lib.download_file`` so we share the retry/verify
logic with the CLI.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Optional

import viofosync_lib as vfs

from ..db import Database
from ..settings import SettingsProvider
from . import queue as q
from . import scanner
from .hub import Hub

log = logging.getLogger("viofosync.sync_worker")

BACKOFF_STEPS = [10, 30, 120, 600]  # seconds


def _now_ms() -> int:
    """Current time in milliseconds since the epoch. Used for
    the per-stage timing columns the A/B benchmarking reads."""
    return int(time.time() * 1000)


def _filter_ro_only(listing):
    """Yield only Recordings whose dashcam source path lies under
    /RO/. Used when the user has 'Sync read-only files only' on."""
    for r in listing:
        fp = (getattr(r, "filepath", None) or "").upper()
        if "/RO/" in fp or fp.endswith("/RO"):
            yield r


def _should_delete_after_download(
    item,
    *,
    dest_path: str,
    delete_enabled: bool,
    local_size: int,
    local_exists: bool,
) -> tuple[bool, str]:
    """The three-guard decision. Pure function — no side effects.
    Returns ``(ok_to_delete, reason)`` where reason is one of
    ``setting_off`` / ``locked`` / ``local_missing`` /
    ``size_mismatch`` / ``ok``.
    """
    if not delete_enabled:
        return False, "setting_off"
    src = item.source_dir or ""
    if "/RO/" in src or src.endswith("/RO"):
        return False, "locked"
    if not local_exists:
        return False, "local_missing"
    if not item.remote_size or local_size != item.remote_size:
        return False, "size_mismatch"
    return True, "ok"


def _maybe_delete_from_dashcam(
    *,
    item,
    dest_path: str,
    delete_enabled: bool,
    base_url: str,
    sink: "WebSink | None" = None,
) -> None:
    """Apply the three-guard check and, if all pass, ask the
    dashcam to delete the clip. Failure-to-delete is logged at
    WARNING and never raised — the caller has already marked the
    queue row done.

    When ``sink`` is provided, broadcasts a ``dashcam_delete``
    event with the outcome. Only fires when delete is enabled
    (``setting_off`` stays silent so we don't spam the log for
    users who never opted in)."""
    try:
        local_exists = os.path.exists(dest_path)
        local_size = os.path.getsize(dest_path) if local_exists else 0
    except OSError:
        local_exists, local_size = False, 0
    ok, reason = _should_delete_after_download(
        item,
        dest_path=dest_path,
        delete_enabled=delete_enabled,
        local_size=local_size,
        local_exists=local_exists,
    )
    if not ok:
        if reason == "locked":
            log.info("not deleting %s from dashcam: locked", item.filename)
        elif reason == "local_missing":
            log.warning(
                "not deleting %s from dashcam: local file missing",
                item.filename,
            )
        elif reason == "size_mismatch":
            log.warning(
                "not deleting %s from dashcam: size mismatch "
                "(local=%s, dashcam=%s)",
                item.filename, local_size, item.remote_size,
            )
        if sink is not None and reason != "setting_off":
            sink.dashcam_delete(
                item.filename, ok=False, reason=reason,
                local_size=local_size,
                remote_size=item.remote_size,
            )
        return

    success = vfs.delete_dashcam_file(
        base_url, item.source_dir, item.filename
    )
    if success:
        log.info("deleted %s from dashcam", item.filename)
    else:
        log.warning("dashcam delete failed for %s", item.filename)
    if sink is not None:
        sink.dashcam_delete(
            item.filename,
            ok=success,
            reason="ok" if success else "request_failed",
            local_size=local_size,
            remote_size=item.remote_size,
        )


def _refresh_queue_size(db, item, dest_path: str) -> None:
    """After a successful download, replace the queue row's
    ``remote_size`` with the actual on-disk size.

    The HTML directory listing reports sizes rounded to MB
    precision (`"102.00 MB"` → 102 << 20 bytes), which makes
    strict equality against the byte-precise local file useless.
    The download path itself uses HEAD to fetch the exact size
    and verifies it during streaming, so the on-disk file is the
    authoritative size — adopt it as truth for downstream checks
    (the auto-delete guard, the queue's MB stats) and to fix the
    persisted row for the next cycle.
    """
    try:
        actual = os.path.getsize(dest_path)
    except OSError:
        return
    with db.write() as c:
        c.execute(
            "UPDATE download_queue SET remote_size=? WHERE id=?",
            (actual, item.id),
        )
    item.remote_size = actual


class WebSink(vfs.ProgressSink):
    """Bridges the downloader (running on a worker thread) to
    the asyncio hub on the main loop.

    All calls are synchronous and thread-safe because they use
    :meth:`Hub.schedule_broadcast` which goes through
    ``asyncio.run_coroutine_threadsafe``.
    """

    def __init__(
        self,
        hub: Hub,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.hub = hub
        self.loop = loop

    def _send(self, event: dict) -> None:
        self.hub.schedule_broadcast(self.loop, event)

    def queue_set(self, filenames):
        self._send({
            "type": "queue_set",
            "filenames": list(filenames),
        })

    def item_started(self, filename, total_bytes):
        self._send({
            "type": "item_started",
            "filename": filename,
            "total": total_bytes,
        })

    def item_progress(self, filename, bytes_done, total, speed):
        self._send({
            "type": "item_progress",
            "filename": filename,
            "bytes": bytes_done,
            "total": total,
            "speed": speed,
        })

    def item_finished(self, filename, ok, err, bytes_written):
        self._send({
            "type": "item_finished",
            "filename": filename,
            "ok": ok,
            "error": err,
            "bytes": bytes_written,
        })

    def sync_done(self, ok, err):
        self._send({"type": "sync_done", "ok": ok, "error": err})

    def dashcam_delete(
        self, filename, *, ok, reason,
        local_size=None, remote_size=None,
    ):
        ev = {
            "type": "dashcam_delete",
            "filename": filename,
            "ok": bool(ok),
            "reason": reason,
        }
        if local_size is not None:
            ev["local_size"] = local_size
        if remote_size is not None:
            ev["remote_size"] = remote_size
        self._send(ev)

    def retention_deleted(self, filename, *, reason):
        self._send({
            "type": "retention_deleted",
            "filename": filename,
            "reason": reason,
        })


class _ArgsShim:
    """Tiny duck-typed stand-in for argparse.Namespace that
    ``viofosync.sync()`` expects. Saves us from importing
    argparse just to synthesise an object."""

    def __init__(self, use_html: bool, gps_extract: bool) -> None:
        self.html = use_html
        self.gps_extract = gps_extract


class SyncWorker:
    def __init__(
        self,
        db: Database,
        provider: SettingsProvider,
        hub: Hub,
    ) -> None:
        self.db = db
        self._provider = provider
        self.hub = hub
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._cancel_current = threading.Event()
        self._kick = asyncio.Event()
        self._paused = threading.Event()    # set = paused
        self._backoff_idx = 0
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._running_cycle = False
        self._current_filename: Optional[str] = None
        # Single-thread executor for post-download work (GPS
        # extract, dashcam delete, mark_done). Strict FIFO keeps
        # write ordering simple and avoids disk contention from
        # parallel MP4 atom parses. Lazily created in start().
        self._tail_executor: Optional[ThreadPoolExecutor] = None
        self._tail_futures: set[Future] = set()

    # ---- lifecycle ----

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Called once from the lifespan hook to capture the
        main event loop. ``start()`` is safe to call from a
        threadpool handler after this."""
        self._loop = loop

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                raise RuntimeError(
                    "SyncWorker.start called without an event loop; "
                    "call bind_loop() during app startup"
                )
        self._stop.clear()
        if self._tail_executor is None:
            self._tail_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="viofo-tail",
            )
        # Schedule the coroutine onto the captured loop — works
        # both from the loop thread and from threadpool handlers.
        self._task = asyncio.run_coroutine_threadsafe(
            self._run(), self._loop
        )

    def _is_running(self) -> bool:
        if self._task is None:
            return False
        # asyncio.Task has .done(); concurrent.futures.Future also
        # exposes .done(), so the check works for both.
        return not self._task.done()

    async def stop(self) -> None:
        self._stop.set()
        self._cancel_current.set()
        self._kick.set()
        if self._task is not None:
            # self._task may be an asyncio.Task or a
            # concurrent.futures.Future — wrap uniformly.
            try:
                if isinstance(self._task, concurrent.futures.Future):
                    await asyncio.wrap_future(self._task)
                else:
                    await asyncio.wait_for(self._task, timeout=10.0)
            except (asyncio.TimeoutError, Exception):
                try:
                    self._task.cancel()
                except Exception:
                    pass
        # Drain the tail executor: let in-flight GPS extracts /
        # dashcam deletes finish so we don't leave half-written
        # .gpx sidecars on disk. The executor is short-lived
        # work; this should complete in <2 s typically.
        if self._tail_executor is not None:
            ex = self._tail_executor
            self._tail_executor = None
            try:
                ex.shutdown(wait=True, cancel_futures=False)
            except Exception:  # pragma: no cover
                log.exception("tail executor shutdown failed")

    def kick(self) -> None:
        """Trigger an immediate cycle (e.g. user clicked Start
        sync or changed priorities). Thread-safe."""
        self._backoff_idx = 0
        if self._loop is not None and self._loop.is_running():
            # asyncio.Event.set() is not thread-safe; hop onto
            # the loop thread first.
            self._loop.call_soon_threadsafe(self._kick.set)
        else:
            self._kick.set()

    def cancel_current(self) -> None:
        """Abort the in-flight download ASAP. Used when the
        reachability probe fails mid-download or the user
        presses Stop."""
        self._cancel_current.set()

    def skip_current(self) -> None:
        """Cancel the in-flight download and move on to the
        next queue item. Unlike pause, the worker keeps running."""
        self._cancel_current.set()

    def pause(self) -> None:
        """Pause the worker: finish the current chunk then stop
        picking new items. The current download is cancelled."""
        self._paused.set()
        self._cancel_current.set()
        self._broadcast_sync_state()

    def resume(self) -> None:
        """Unpause and kick the worker to pick up immediately."""
        self._paused.clear()
        self._broadcast_sync_state()
        self.kick()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    @property
    def current_filename(self) -> Optional[str]:
        return self._current_filename

    def get_status(self) -> dict:
        return {
            "running": self._is_running(),
            "paused": self.paused,
            "current_filename": self._current_filename,
        }

    def _broadcast_sync_state(self) -> None:
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(
                    self.hub.broadcast({
                        "type": "sync_state",
                        "running": self._is_running(),
                        "paused": self.paused,
                        "current_filename": self._current_filename,
                    })
                )
            )

    # ---- main loop ----

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                did_work = await self._cycle()
            except Exception:  # pragma: no cover
                log.exception("sync cycle crashed")
                did_work = False

            if did_work:
                wait = 1.0   # immediately look for more
                self._backoff_idx = 0
            else:
                wait = float(
                    BACKOFF_STEPS[
                        min(self._backoff_idx, len(BACKOFF_STEPS) - 1)
                    ]
                )
                self._backoff_idx = min(
                    self._backoff_idx + 1, len(BACKOFF_STEPS) - 1
                )

            # Sleep, but wake up early on kick or stop.
            try:
                await asyncio.wait_for(
                    self._kick.wait(), timeout=wait
                )
            except asyncio.TimeoutError:
                pass
            self._kick.clear()

    # ---- probe ----

    async def _probe(self) -> bool:
        """3-second TCP probe to the dashcam. True = reachable."""
        snap = self._provider.get()
        if not snap.address:
            return False
        loop = asyncio.get_running_loop()
        address = snap.address

        def _sync():
            try:
                with socket.create_connection(
                    (address, 80), timeout=3.0
                ):
                    return True
            except OSError:
                return False

        return await loop.run_in_executor(None, _sync)

    # ---- one cycle ----

    async def _refresh_listing_and_reconcile(self) -> bool:
        """Pull a fresh listing from the dashcam and reconcile it
        with the queue. Returns True on success, False on
        listing failure (logged at warning, no broadcast).

        Used at the top of every cycle and again after each
        successful download mid-drain — the latter is what gets
        clips recorded *during* a long sync into the queue
        without waiting for the cycle to end.
        """
        try:
            listing = await asyncio.get_running_loop().run_in_executor(
                None, self._fetch_listing
            )
        except Exception as e:
            log.warning("listing fetch failed: %s", e)
            return False
        if self._provider.get().sync_ro_only:
            listing = list(_filter_ro_only(listing))
        present = self._present_filenames()
        summary = q.reconcile(self.db, listing, present)
        await self.hub.broadcast({
            "type": "queue_reconciled",
            "summary": summary,
            "queue": q.list_all(self.db, limit=200),
        })
        return True

    async def _cycle(self) -> bool:
        cycle_start = time.monotonic()
        reachable = await self._probe()
        await self.hub.broadcast({
            "type": "dashcam_online" if reachable else "dashcam_offline",
        })
        if not reachable:
            return False

        # Initial listing — failure here aborts the cycle so
        # back-off can kick in. Mid-cycle re-listings (below)
        # are best-effort instead.
        if not await self._refresh_listing_and_reconcile():
            await self.hub.broadcast({
                "type": "sync_error",
                "error": "listing failed",
            })
            return False

        # Drain the queue. After each successful download the
        # loop re-checks ``next_pending`` so a priority update
        # mid-cycle takes effect immediately.
        did_any = False
        drained = 0
        while not self._stop.is_set():
            if self._paused.is_set():
                break
            item = q.next_pending(
                self.db,
                ro_only=self._provider.get().sync_ro_only,
            )
            if item is None:
                break
            # Re-probe occasionally so we don't burn a whole
            # retry budget on a dashcam that's already gone.
            if did_any and not await self._probe():
                await self.hub.broadcast({
                    "type": "dashcam_offline",
                })
                return True
            self._current_filename = item.filename
            self._broadcast_sync_state()
            ok = await self._download_one(item)
            self._current_filename = None
            did_any = True
            if not ok:
                # Transient failure. Loop continues with next
                # pending item, which may well succeed.
                continue
            drained += 1
            # Refresh listing between downloads so clips the
            # dashcam recorded during this transfer show up in
            # the queue before we pick the next pending one.
            # Best-effort: a transient listing failure here
            # leaves the existing queue intact.
            await self._refresh_listing_and_reconcile()

        # Let the tail executor finish before the post-cycle scan:
        # ``scanner.scan`` reads has_gpx off disk to set clip_index
        # flags, and the dashcam-delete sink events shouldn't lag
        # past sync_done.
        await self._await_tails()

        # Re-index + sweep thumbs so new clips appear in the UI.
        # Both calls are idempotent; the did_any gate is just to
        # skip the directory walk when nothing changed.
        if did_any:
            snap = self._provider.get()
            try:
                await asyncio.to_thread(
                    scanner.scan,
                    self.db, snap.recordings, snap.grouping,
                )
                await scanner.sweep_missing_thumbs(
                    self.db, snap.recordings,
                )
                from . import retention as _retention
                sink = WebSink(self.hub, asyncio.get_running_loop())
                await asyncio.to_thread(
                    _retention.sweep,
                    self.db, snap.recordings,
                    max_days=snap.retention_max_days,
                    disk_pct=snap.retention_disk_pct,
                    protect_ro=snap.retention_protect_ro,
                    sink=sink,
                )
            except Exception:  # pragma: no cover — non-fatal
                log.exception("post-cycle scan/thumb sweep failed")

        await self.hub.broadcast({
            "type": "sync_done",
            "ok": True,
            "queue": q.list_all(self.db, limit=200),
        })
        cycle_duration = time.monotonic() - cycle_start
        log.info(
            "cycle done: drained=%d duration=%.1fs pipeline=%s",
            drained, cycle_duration,
            self._provider.get().pipeline_post_download,
        )
        return did_any

    def _fetch_listing(self):
        snap = self._provider.get()
        base = f"http://{snap.address}"
        if snap.use_html_listing:
            return vfs.get_dashcam_filenames_html(base)
        return vfs.get_dashcam_filenames(base)

    def _present_filenames(self):
        snap = self._provider.get()
        out = []
        for filename, _ in vfs.get_downloaded_recordings(
            snap.recordings, snap.grouping
        ):
            out.append(filename)
        return out

    # ---- single item download ----

    async def _download_one(self, item: q.QueueItem) -> bool:
        """Download one queued file and hand its post-download
        tail (GPS extract → dashcam delete → mark_done) to the
        tail executor when ``pipeline_post_download`` is on.

        The download itself stays N=1 because the dashcam's Wi-Fi
        is the wall — but the tail used to block the worker from
        starting the next download. Moving it off the critical
        path means file N+1's bytes start flowing while file N's
        sidecar is still being parsed.

        When ``pipeline_post_download`` is off, the tail runs
        inline on the same executor thread, restoring legacy
        behaviour for A/B benchmarking.
        """
        snap = self._provider.get()
        q.mark_downloading(self.db, item.id)
        self._cancel_current.clear()
        loop = asyncio.get_running_loop()
        sink = WebSink(self.hub, loop)
        base = f"http://{snap.address}"

        import datetime as _dt
        # get_group_name wants a datetime; if the queue row
        # didn't capture one, now() is a safe fallback.
        recorded = (
            _dt.datetime.fromtimestamp(item.recorded_at)
            if item.recorded_at
            else _dt.datetime.now()
        )
        group_name = vfs.get_group_name(recorded, snap.grouping)
        rec = vfs.Recording(
            filename=item.filename,
            filepath=item.source_dir,
            size=item.remote_size,
            timecode=None,
            datetime=recorded,
            attr=None,
        )

        def _blocking_download():
            """Run on the default executor thread. Returns
            (ok, dest_path, err). ``dest_path`` is only valid
            when ``ok`` is True."""
            try:
                ok, _ = vfs.download_file_with(
                    base, rec, snap.recordings,
                    group_name,
                    progress_sink=sink,
                    cancel_check=self._cancel_current.is_set,
                    max_attempts=snap.download_attempts,
                    socket_timeout=snap.timeout,
                )
                if not ok:
                    return False, None, None
                dest_path = vfs.get_filepath(
                    snap.recordings, group_name, item.filename,
                )
                return True, dest_path, None
            except Exception as e:
                return False, None, str(e)

        self._set_timing(item.id, download_started_at=_now_ms())
        ok, dest_path, err = await loop.run_in_executor(
            None, _blocking_download
        )
        self._set_timing(item.id, download_finished_at=_now_ms())

        if not ok:
            new_state = q.mark_transient_failure(
                self.db,
                item.id,
                err or "unknown",
                snap.max_attempts,
            )
            await self.hub.broadcast({
                "type": "item_state_change",
                "filename": item.filename,
                "state": new_state,
                "error": err,
            })
            return False

        # Download succeeded. Run the post-download tail either
        # on a dedicated executor (pipelined: worker returns now
        # and starts the next download immediately) or inline
        # (legacy timing for the A/B comparison).
        if (snap.pipeline_post_download
                and self._tail_executor is not None):
            self._set_timing(item.id, tail_submitted_at=_now_ms())
            fut = self._tail_executor.submit(
                self._run_tail, item, dest_path, snap, sink,
            )
            self._tail_futures.add(fut)
            fut.add_done_callback(self._tail_futures.discard)
        else:
            self._set_timing(item.id, tail_submitted_at=_now_ms())
            self._run_tail(item, dest_path, snap, sink)
        return True

    # ---- tail stage ----

    def _run_tail(
        self,
        item: q.QueueItem,
        dest_path: str,
        snap,
        sink: "WebSink",
    ) -> None:
        """Post-download work for one file. Runs either on the
        tail executor or inline; either way, the download itself
        has already succeeded by the time we get here, so a
        failure in any step here logs but never re-queues the
        download (it's on disk; a re-download would waste Wi-Fi).
        """
        t_start = time.perf_counter()
        log.debug("tail begin: %s", item.filename)
        try:
            try:
                _refresh_queue_size(self.db, item, dest_path)
            except Exception:  # pragma: no cover — DB hiccup
                log.exception(
                    "refresh_queue_size failed for %s",
                    item.filename,
                )
            t_rqs = time.perf_counter()
            log.debug(
                "tail: refresh_queue_size done in %.2fs (%s)",
                t_rqs - t_start, item.filename,
            )
            if snap.gps_extract:
                try:
                    vfs.extract_gps_data(dest_path)
                except Exception as e:
                    # Clips recorded without GPS lock have no
                    # track to extract; not a download failure.
                    log.info(
                        "gpx extract failed for %s: %s",
                        item.filename, e,
                    )
            t_gps = time.perf_counter()
            log.debug(
                "tail: gps done in %.2fs (%s)",
                t_gps - t_rqs, item.filename,
            )
            _maybe_delete_from_dashcam(
                item=item,
                dest_path=dest_path,
                delete_enabled=snap.delete_after_download,
                base_url=f"http://{snap.address}",
                sink=sink,
            )
            t_del = time.perf_counter()
            log.debug(
                "tail: dashcam_delete done in %.2fs (%s)",
                t_del - t_gps, item.filename,
            )
            q.mark_done(self.db, item.id)
            log.debug(
                "tail: mark_done done in %.2fs (%s)",
                time.perf_counter() - t_del, item.filename,
            )
        except Exception:
            log.exception(
                "post-download tail unexpected failure for %s",
                item.filename,
            )
        finally:
            try:
                self._set_timing(
                    item.id,
                    tail_finished_at=_now_ms(),
                )
            except Exception:  # pragma: no cover
                pass
            log.debug(
                "tail end: %s total=%.2fs",
                item.filename, time.perf_counter() - t_start,
            )

    async def _await_tails(self) -> None:
        """Block until every tail submitted during this cycle has
        completed. Called at end-of-cycle so post-cycle scans see
        every sidecar and queue row updated."""
        if not self._tail_futures:
            return
        pending = list(self._tail_futures)
        await asyncio.gather(
            *(asyncio.wrap_future(f) for f in pending),
            return_exceptions=True,
        )

    def _set_timing(self, item_id: int, **fields: int) -> None:
        """Update timing columns on a download_queue row.

        ``fields`` keys must match nullable INTEGER columns added
        by the db migration (``download_started_at`` etc.); values
        are ms-since-epoch. Used by the A/B benchmarking — failure
        here must never escape into the pipeline."""
        if not fields:
            return
        try:
            cols = ", ".join(f"{k}=?" for k in fields)
            values = list(fields.values()) + [item_id]
            with self.db.write() as c:
                c.execute(
                    f"UPDATE download_queue SET {cols} WHERE id=?",
                    values,
                )
        except Exception:  # pragma: no cover — DB hiccup
            log.exception("_set_timing failed for %s", item_id)
