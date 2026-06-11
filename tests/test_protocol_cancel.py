"""A cancellation (user pause / stop / lost reachability) is a deliberate
abort, NOT a transient download failure.

``download_file`` must raise :class:`DownloadCancelled` the moment
``cancel_check`` fires, without consuming any of its retry budget and
without logging an ERROR. Previously the cancel surfaced as a plain
``UserWarning`` that the generic retry handler swallowed, so a pause
burned all three attempts and ended in a misleading "Failed to download
… after 3 attempts" error.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

import viofosync_lib as vfs
from viofosync_lib import _protocol


class _FakeResp:
    """GET/HEAD response stand-in: a context manager whose ``read``
    yields the supplied chunks then EOF."""

    def __init__(self, chunks=()):
        self._chunks = list(chunks)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def read(self, n=-1):
        return self._chunks.pop(0) if self._chunks else b""

    def getheader(self, name):
        return None


def test_cancel_raises_without_consuming_retries(tmp_path, monkeypatch):
    # No real sleeping: the point is "no retry", independent of backoff.
    monkeypatch.setattr(_protocol, "RETRY_BACKOFF", 0)

    rec = vfs.Recording(
        filename="X.MP4",
        filepath="/DCIM/Movie/X.MP4",
        size=1000,
        timecode=None,
        datetime=None,
        attr=None,
    )

    get_opens = {"n": 0}

    def fake_urlopen(req, timeout=None):
        method = getattr(req, "get_method", lambda: "GET")()
        if method == "HEAD":
            return _FakeResp()  # Content-Length unknown
        get_opens["n"] += 1
        return _FakeResp([b"abc"])

    # False on the first poll so the GET opens and we enter the read
    # loop; True afterwards, simulating a pause mid-stream.
    polls = {"n": 0}

    def cancel_check():
        polls["n"] += 1
        return polls["n"] > 1

    with patch("urllib.request.urlopen", fake_urlopen):
        with pytest.raises(vfs.DownloadCancelled):
            vfs.download_file_with(
                "http://cam",
                rec,
                str(tmp_path),
                "",
                cancel_check=cancel_check,
                max_attempts=3,
            )

    # Opened exactly once: the cancel short-circuited the 3-attempt
    # budget instead of retrying.
    assert get_opens["n"] == 1
    # No half-written .part file left behind.
    assert list(tmp_path.glob("*.part")) == []


# ---- cancellation during retry backoff ----

def test_cancel_honoured_during_retry_backoff(tmp_path, monkeypatch):
    """A pause/stop/unreachable signal must interrupt the inter-attempt
    backoff sleep, not wait out the full 5-50s ladder."""
    import datetime
    import time
    from unittest.mock import patch

    from viofosync_lib import DownloadCancelled, _protocol
    from viofosync_lib._archive import Recording

    monkeypatch.setattr(_protocol, "max_download_attempts", 3)
    monkeypatch.setattr(_protocol, "RETRY_BACKOFF", 30)  # would be a long wait

    rec = Recording(
        "2026_0101_120000_0001F.MP4", "/DCIM/Movie/x.MP4", 1000, 0,
        datetime.datetime(2026, 1, 1, 12, 0), 0,
    )

    def fail(url_or_req, *a, **k):
        raise ConnectionRefusedError("transient")

    started = time.monotonic()
    with patch("urllib.request.urlopen", side_effect=fail):
        with __import__("pytest").raises(DownloadCancelled):
            _protocol.download_file(
                "http://192.0.2.1", rec, str(tmp_path), "",
                cancel_check=lambda: True,
            )
    assert time.monotonic() - started < 2.0, "backoff sleep ignored cancel_check"
