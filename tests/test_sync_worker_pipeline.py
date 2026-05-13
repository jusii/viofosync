"""Tests for the post-download pipeline refactor.

Background: v1 of ``_download_one`` ran the post-download tail
(GPS extract → dashcam delete → mark_done) inline on the same
executor thread as the download. With Wi-Fi already saturated at
N=1, the only way to compress cycle wall-clock is to hand the
tail to a separate executor and let the worker immediately start
the next file's download.

These tests pin:

* ``pipeline_post_download=True`` actually dispatches the tail to
  the named ``viofo-tail`` thread rather than running it inline.
* ``pipeline_post_download=False`` preserves legacy inline behaviour
  (used for A/B benchmarking against the camera).
* The cycle awaits all in-flight tails before returning so the
  post-cycle ``scanner.scan`` sees every sidecar on disk.
* Per-stage timing columns get populated for both paths.
"""
from __future__ import annotations

import datetime as _dt
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from web.db import Database
from web.services import sync_worker as sw
from web.services.hub import Hub
from web.services.sync_worker import SyncWorker

# ---------------------------------------------------------------- helpers


class _Rec:
    """Minimal Recording stand-in for listing reconcile."""

    def __init__(
        self, filename: str, *,
        filepath: str = "/DCIM/Movie",
        size: int = 1000,
    ) -> None:
        self.filename = filename
        self.filepath = filepath
        self.size = size
        self.datetime = _dt.datetime(2026, 5, 8, 12, 0, 0)


def _make_snap(
    *,
    pipeline_post_download: bool = True,
    gps_extract: bool = False,
    delete_after_download: bool = False,
    recordings: str = "/tmp",
):
    """Build a Snapshot stand-in covering only the fields the
    sync worker actually reads."""
    snap = MagicMock()
    snap.address = "192.168.1.230"
    snap.use_html_listing = True
    snap.grouping = "daily"
    snap.recordings = recordings
    snap.sync_ro_only = False
    snap.pipeline_post_download = pipeline_post_download
    snap.gps_extract = gps_extract
    snap.delete_after_download = delete_after_download
    snap.download_attempts = 1
    snap.max_attempts = 1
    snap.timeout = 5.0
    return snap


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / ".viofosync.db"))


def _build_worker(
    db: Database,
    snap,
    *,
    recordings_dir: Path,
    create_tail_executor: bool = True,
) -> SyncWorker:
    """Spin up a SyncWorker wired to a real DB but with the
    provider, hub broadcasts, and tail executor managed
    in-test (we don't want to run the asyncio loop)."""
    provider = MagicMock()
    provider.get.return_value = snap
    hub = Hub()
    worker = SyncWorker(db, provider, hub)
    snap.recordings = str(recordings_dir)
    if create_tail_executor:
        worker._tail_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="viofo-tail",
        )
    return worker


def _shutdown(worker: SyncWorker) -> None:
    if worker._tail_executor is not None:
        worker._tail_executor.shutdown(wait=True, cancel_futures=False)
        worker._tail_executor = None


def _queue_rows(db: Database):
    with db.conn() as c:
        return [
            dict(r) for r in c.execute(
                "SELECT * FROM download_queue ORDER BY filename"
            ).fetchall()
        ]


def _make_fakes(
    *,
    listings: list[list[_Rec]],
    download_payload: bytes = b"x" * 1024,
    gps_calls: list | None = None,
    delete_calls: list | None = None,
):
    """Build the four fakes the worker calls into during a cycle.
    The caller patches them onto ``vfs`` / ``SyncWorker`` itself."""
    listings_iter = iter(listings)

    def fake_fetch_listing(self):  # noqa: ARG001 — bound-method shape
        try:
            return next(listings_iter)
        except StopIteration:
            return []

    def fake_download_file_with(
        base, rec, dest_root, group_name,
        *,
        progress_sink=None,  # noqa: ARG001
        cancel_check=None,   # noqa: ARG001
        max_attempts=None,   # noqa: ARG001
        socket_timeout=None,  # noqa: ARG001
    ):
        from viofosync_lib import get_filepath

        dest = Path(get_filepath(dest_root, group_name, rec.filename))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(download_payload)
        return True, "1.0 MB/s"

    def fake_extract_gps_data(path):
        thread_name = threading.current_thread().name
        if gps_calls is not None:
            gps_calls.append({"path": path, "thread": thread_name})

    def fake_delete_dashcam_file(base_url, source_dir, filename, **kwargs):  # noqa: ARG001
        if delete_calls is not None:
            delete_calls.append(filename)
        return True

    return (
        fake_fetch_listing,
        fake_download_file_with,
        fake_extract_gps_data,
        fake_delete_dashcam_file,
    )


# ---------------------------------------------------------------- tests


async def test_pipeline_on_runs_tail_on_dedicated_thread(
    db: Database, tmp_path: Path,
) -> None:
    """With pipelining enabled, GPS extract + dashcam delete +
    mark_done execute on the ``viofo-tail`` thread, not on the
    asyncio event loop's default executor thread."""
    snap = _make_snap(
        pipeline_post_download=True,
        gps_extract=True,
        delete_after_download=True,
    )
    worker = _build_worker(db, snap, recordings_dir=tmp_path)

    gps_calls: list = []
    delete_calls: list = []
    (
        fake_fetch, fake_download, fake_gps, fake_delete,
    ) = _make_fakes(
        listings=[[_Rec("A.MP4")]],
        gps_calls=gps_calls,
        delete_calls=delete_calls,
    )

    try:
        with patch.object(SyncWorker, "_probe", return_value=True), \
             patch.object(
                 SyncWorker, "_fetch_listing", fake_fetch
             ), \
             patch.object(
                 SyncWorker, "_present_filenames", return_value=[]
             ), \
             patch.object(sw.vfs, "download_file_with", fake_download), \
             patch.object(sw.vfs, "extract_gps_data", fake_gps), \
             patch.object(sw.vfs, "delete_dashcam_file", fake_delete), \
             patch.object(sw.scanner, "scan", return_value=None), \
             patch.object(
                 sw.scanner, "sweep_missing_thumbs", return_value=None
             ), \
             patch("web.services.retention.sweep", return_value=None):
            did_any = await worker._cycle()
    finally:
        _shutdown(worker)

    assert did_any is True
    rows = _queue_rows(db)
    assert len(rows) == 1
    assert rows[0]["state"] == "done"
    # Tail work landed on the dedicated executor thread.
    assert len(gps_calls) == 1
    assert gps_calls[0]["thread"].startswith("viofo-tail"), \
        f"expected viofo-tail thread, got {gps_calls[0]['thread']!r}"
    assert delete_calls == ["A.MP4"]


async def test_pipeline_off_runs_tail_inline(
    db: Database, tmp_path: Path,
) -> None:
    """With pipelining off, the tail runs on the same thread as
    the download (the asyncio default executor) — used by the
    A/B benchmark to compare against the legacy timing."""
    snap = _make_snap(
        pipeline_post_download=False,
        gps_extract=True,
    )
    worker = _build_worker(db, snap, recordings_dir=tmp_path)

    gps_calls: list = []
    (
        fake_fetch, fake_download, fake_gps, fake_delete,
    ) = _make_fakes(
        listings=[[_Rec("B.MP4")]],
        gps_calls=gps_calls,
    )

    try:
        with patch.object(SyncWorker, "_probe", return_value=True), \
             patch.object(SyncWorker, "_fetch_listing", fake_fetch), \
             patch.object(
                 SyncWorker, "_present_filenames", return_value=[]
             ), \
             patch.object(sw.vfs, "download_file_with", fake_download), \
             patch.object(sw.vfs, "extract_gps_data", fake_gps), \
             patch.object(sw.vfs, "delete_dashcam_file", fake_delete), \
             patch.object(sw.scanner, "scan", return_value=None), \
             patch.object(
                 sw.scanner, "sweep_missing_thumbs", return_value=None
             ), \
             patch("web.services.retention.sweep", return_value=None):
            await worker._cycle()
    finally:
        _shutdown(worker)

    rows = _queue_rows(db)
    assert rows[0]["state"] == "done"
    assert len(gps_calls) == 1
    # Inline tail runs on whatever thread the asyncio default
    # executor used for the download — explicitly NOT on
    # viofo-tail.
    assert not gps_calls[0]["thread"].startswith("viofo-tail")


async def test_cycle_awaits_tail_before_returning(
    db: Database, tmp_path: Path,
) -> None:
    """The post-cycle ``scanner.scan`` relies on every sidecar
    landing on disk before it runs. If the cycle returned while
    a tail was still extracting GPS, ``clip_index.has_gpx``
    could miss the freshly-downloaded file. The cycle awaits."""
    snap = _make_snap(
        pipeline_post_download=True,
        gps_extract=True,
    )
    worker = _build_worker(db, snap, recordings_dir=tmp_path)

    gate = threading.Event()

    def slow_gps(path):
        # Block until released by the test, then mark done.
        gate.wait(timeout=5.0)

    (
        fake_fetch, fake_download, _, fake_delete,
    ) = _make_fakes(
        listings=[[_Rec("C.MP4")]],
    )

    async def run_cycle():
        with patch.object(SyncWorker, "_probe", return_value=True), \
             patch.object(SyncWorker, "_fetch_listing", fake_fetch), \
             patch.object(
                 SyncWorker, "_present_filenames", return_value=[]
             ), \
             patch.object(sw.vfs, "download_file_with", fake_download), \
             patch.object(sw.vfs, "extract_gps_data", slow_gps), \
             patch.object(sw.vfs, "delete_dashcam_file", fake_delete), \
             patch.object(sw.scanner, "scan", return_value=None), \
             patch.object(
                 sw.scanner, "sweep_missing_thumbs", return_value=None
             ), \
             patch("web.services.retention.sweep", return_value=None):
            return await worker._cycle()

    import asyncio

    try:
        cycle_task = asyncio.create_task(run_cycle())
        # The download finishes near-instantly, but the tail is
        # blocked on ``gate``. Give the worker a moment to submit
        # to the tail executor, then assert the cycle hasn't
        # returned yet.
        await asyncio.sleep(0.1)
        assert not cycle_task.done(), \
            "cycle returned before tail had a chance to start"
        # Release the tail and let the cycle finish.
        gate.set()
        await asyncio.wait_for(cycle_task, timeout=5.0)
        # mark_done must have happened — it lives at the end of
        # the tail, which only fires after the gate is released.
        assert _queue_rows(db)[0]["state"] == "done"
    finally:
        gate.set()
        _shutdown(worker)


async def test_timing_columns_populated(
    db: Database, tmp_path: Path,
) -> None:
    """The A/B benchmark reads four per-stage timestamps from the
    queue row. Verify each one gets a non-null value on the happy
    path."""
    snap = _make_snap(pipeline_post_download=True)
    worker = _build_worker(db, snap, recordings_dir=tmp_path)

    (
        fake_fetch, fake_download, fake_gps, fake_delete,
    ) = _make_fakes(
        listings=[[_Rec("D.MP4")]],
    )

    try:
        with patch.object(SyncWorker, "_probe", return_value=True), \
             patch.object(SyncWorker, "_fetch_listing", fake_fetch), \
             patch.object(
                 SyncWorker, "_present_filenames", return_value=[]
             ), \
             patch.object(sw.vfs, "download_file_with", fake_download), \
             patch.object(sw.vfs, "extract_gps_data", fake_gps), \
             patch.object(sw.vfs, "delete_dashcam_file", fake_delete), \
             patch.object(sw.scanner, "scan", return_value=None), \
             patch.object(
                 sw.scanner, "sweep_missing_thumbs", return_value=None
             ), \
             patch("web.services.retention.sweep", return_value=None):
            await worker._cycle()
    finally:
        _shutdown(worker)

    row = _queue_rows(db)[0]
    for col in (
        "download_started_at",
        "download_finished_at",
        "tail_submitted_at",
        "tail_finished_at",
    ):
        assert row[col] is not None, f"{col} should be set"
    assert row["download_started_at"] <= row["download_finished_at"]
    assert row["download_finished_at"] <= row["tail_submitted_at"]
    assert row["tail_submitted_at"] <= row["tail_finished_at"]


