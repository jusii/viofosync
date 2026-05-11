"""Progress sink protocol for observing downloader events.

A :class:`ProgressSink` is an object passed into
``viofosync.sync()`` and ``viofosync.download_file()`` so callers
(the web UI's SyncWorker, or tests) can observe what the
downloader is doing in real time without parsing stdout.

The CLI does **not** use a sink — it relies on logger output —
so the existing behaviour of ``python viofosync.py`` is unchanged.

Event order for a normal run::

    queue_set([f1, f2, f3])
    item_started(f1, size)
      item_progress(f1, bytes, total, speed)   # repeated
    item_finished(f1, ok=True,  err=None, bytes_written=...)
    item_started(f2, size)
      item_progress(...)
    item_finished(f2, ok=False, err="...",    bytes_written=None)
    ...
    sync_done(ok=True, err=None)
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence


class ProgressSink:
    """Base class — override any subset of methods you care about.

    Every method is a no-op by default so subclasses only
    implement what they need.
    """

    def queue_set(self, filenames: Sequence[str]) -> None:
        """Called once per sync cycle after the remote listing
        has been fetched, with the ordered list of filenames the
        downloader intends to process."""

    def item_started(
        self, filename: str, total_bytes: Optional[int]
    ) -> None:
        """A new file is about to be downloaded. ``total_bytes``
        may be ``None`` if the dashcam didn't honour HEAD."""

    def item_progress(
        self,
        filename: str,
        bytes_done: int,
        total_bytes: Optional[int],
        speed_bps: float,
    ) -> None:
        """Throttled progress tick (roughly every 250 ms)."""

    def item_finished(
        self,
        filename: str,
        ok: bool,
        err: Optional[str],
        bytes_written: Optional[int],
    ) -> None:
        """A file download has completed, failed, or been skipped."""

    def sync_done(self, ok: bool, err: Optional[str]) -> None:
        """The current sync cycle has finished."""


class NullSink(ProgressSink):
    """Explicit no-op sink. Handy as a default in tests."""


class LoggingSink(ProgressSink):
    """Emits every event via :mod:`logging` at DEBUG level.

    Useful for diagnosing the web UI's event stream without
    needing a WebSocket client attached.
    """

    def __init__(
        self, logger: Optional[logging.Logger] = None
    ) -> None:
        self._log = logger or logging.getLogger("viofosync.sink")

    def queue_set(self, filenames):
        self._log.debug("queue_set: %d items", len(filenames))

    def item_started(self, filename, total_bytes):
        self._log.debug(
            "item_started: %s (%s bytes)",
            filename, total_bytes,
        )

    def item_progress(
        self, filename, bytes_done, total_bytes, speed_bps
    ):
        self._log.debug(
            "item_progress: %s %d/%s @ %.1f B/s",
            filename, bytes_done, total_bytes, speed_bps,
        )

    def item_finished(self, filename, ok, err, bytes_written):
        self._log.debug(
            "item_finished: %s ok=%s err=%s bytes=%s",
            filename, ok, err, bytes_written,
        )

    def sync_done(self, ok, err):
        self._log.debug("sync_done: ok=%s err=%s", ok, err)
