"""Truncated downloads must not be archived when HEAD fails.

Some firmwares drop HEAD under load; verification used to be skipped
entirely then, so a connection closed cleanly mid-stream produced a
truncated file that was os.replace'd into the archive as a success.
The listing size (recording.size) is the fallback reference.
"""
from __future__ import annotations

import datetime
import os
from unittest.mock import patch

from viofosync_lib import _protocol
from viofosync_lib._archive import Recording


class _TruncatedResponse:
    """Streams fewer bytes than the listing promised, then EOF —
    exactly what a cleanly-closed truncated transfer looks like."""

    def __init__(self, payload: bytes):
        self._chunks = [payload]

    def read(self, n: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _recording(size: int) -> Recording:
    return Recording(
        "2026_0101_120000_0001F.MP4", "/DCIM/Movie/x.MP4",
        size, 0, datetime.datetime(2026, 1, 1, 12, 0, 0), 0,
    )


def test_truncated_download_rejected_when_head_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(_protocol, "max_download_attempts", 1)

    def fake_urlopen(url_or_req, *args, **kwargs):
        # HEAD probe (Request object with method) fails; the GET
        # (plain URL string) streams a truncated body.
        if getattr(url_or_req, "get_method", lambda: "GET")() == "HEAD":
            raise OSError("HEAD not supported under load")
        return _TruncatedResponse(b"x" * 100)

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        ok, _speed = _protocol.download_file(
            "http://192.0.2.1", _recording(size=1000), str(tmp_path), "",
        )

    assert ok is False, "truncated download was reported as success"
    dest = os.path.join(str(tmp_path), "2026_0101_120000_0001F.MP4")
    assert not os.path.exists(dest), \
        "truncated file was moved into the archive"


def test_complete_download_succeeds_when_head_fails(tmp_path):
    def fake_urlopen(url_or_req, *args, **kwargs):
        if getattr(url_or_req, "get_method", lambda: "GET")() == "HEAD":
            raise OSError("HEAD not supported under load")
        return _TruncatedResponse(b"x" * 1000)  # full size this time

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        ok, _speed = _protocol.download_file(
            "http://192.0.2.1", _recording(size=1000), str(tmp_path), "",
        )

    assert ok is True
    dest = os.path.join(str(tmp_path), "2026_0101_120000_0001F.MP4")
    assert os.path.getsize(dest) == 1000
