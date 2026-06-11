"""The XML listing request must carry a socket timeout.

Without one, a half-open dashcam connection (Wi-Fi drop mid-handshake)
blocks the sync worker's executor thread forever — the HTML-scrape
path already passes ``socket_timeout``; the XML path must too.
"""
from __future__ import annotations

from unittest.mock import patch

from viofosync_lib import _protocol


class _FakeResponse:
    def getcode(self) -> int:
        return 200

    def read(self) -> bytes:
        return b"<?xml version='1.0'?><LIST></LIST>"


def test_xml_listing_passes_socket_timeout():
    captured: dict = {}

    def fake_urlopen(request, *args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return _FakeResponse()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        recs = _protocol.get_dashcam_filenames("http://192.0.2.1")

    assert recs == []
    assert captured["timeout"] == _protocol.socket_timeout


def test_xml_listing_timeout_tracks_module_setting():
    """download_file_with temporarily overrides the module global —
    the listing must honour the value at call time, not import time."""
    captured: dict = {}

    def fake_urlopen(request, *args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return _FakeResponse()

    saved = _protocol.socket_timeout
    try:
        _protocol.socket_timeout = 7.5
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            _protocol.get_dashcam_filenames("http://192.0.2.1")
    finally:
        _protocol.socket_timeout = saved

    assert captured["timeout"] == 7.5
