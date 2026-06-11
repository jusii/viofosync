"""download_file_with must pass per-call attempts/timeout as
parameters, not by mutating module globals (which would let two
concurrent downloads clobber each other's settings)."""
from __future__ import annotations

import datetime
from unittest.mock import patch

import viofosync_lib as vfs
from viofosync_lib import _protocol
from viofosync_lib._archive import Recording


def _recording() -> Recording:
    return Recording(
        "2026_0101_120000_0001F.MP4", "/DCIM/Movie/x.MP4", 4, 0,
        datetime.datetime(2026, 1, 1, 12, 0), 0,
    )


class _Resp:
    def __init__(self, payload: bytes):
        self._chunks = [payload]

    def read(self, n):
        return self._chunks.pop(0) if self._chunks else b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_download_file_accepts_per_call_params(tmp_path):
    seen = {}

    def fake_urlopen(url_or_req, *args, **kwargs):
        if getattr(url_or_req, "get_method", lambda: "GET")() == "HEAD":
            raise OSError("no HEAD")
        seen["timeout"] = kwargs.get("timeout")
        return _Resp(b"abcd")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        ok, _ = _protocol.download_file(
            "http://192.0.2.1", _recording(), str(tmp_path), "",
            max_attempts=4, socket_timeout=42.0,
        )
    assert ok is True
    assert seen["timeout"] == 42.0


def test_download_file_with_does_not_mutate_globals_during_call(tmp_path):
    """The override must reach urlopen by parameter while the module
    globals stay at their defaults throughout the call."""
    default_timeout = _protocol.socket_timeout
    default_attempts = _protocol.max_download_attempts
    observed = {}

    def fake_urlopen(url_or_req, *args, **kwargs):
        if getattr(url_or_req, "get_method", lambda: "GET")() == "HEAD":
            raise OSError("no HEAD")
        observed["call_timeout"] = kwargs.get("timeout")
        observed["global_timeout_mid_call"] = _protocol.socket_timeout
        observed["global_attempts_mid_call"] = _protocol.max_download_attempts
        return _Resp(b"abcd")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        vfs.download_file_with(
            "http://192.0.2.1", _recording(), str(tmp_path), "",
            max_attempts=9, socket_timeout=99.0,
        )

    assert observed["call_timeout"] == 99.0
    assert observed["global_timeout_mid_call"] == default_timeout
    assert observed["global_attempts_mid_call"] == default_attempts
    assert _protocol.socket_timeout == default_timeout
    assert _protocol.max_download_attempts == default_attempts
