"""Test the RO-only listing filter in sync_worker._cycle.

The listing comes from viofosync_lib.Recording tuples; the filter
is a pure function over an iterable, so we test it in isolation.
"""
from __future__ import annotations

from web.services.sync_worker import _filter_ro_only


def _r(filepath: str, filename: str = "X.MP4"):
    """A minimal Recording stand-in — only the fields the filter reads."""
    class _Rec:
        pass
    rec = _Rec()
    rec.filepath = filepath
    rec.filename = filename
    return rec


def test_filter_ro_only_keeps_ro_paths() -> None:
    listing = [
        _r("/DCIM/Movie", "A.MP4"),
        _r("/DCIM/Movie/RO", "B.MP4"),
        _r("/DCIM/Movie/Parking", "C.MP4"),
        _r("/DCIM/Movie/RO/", "D.MP4"),
    ]
    out = list(_filter_ro_only(listing))
    assert [r.filename for r in out] == ["B.MP4", "D.MP4"]


def test_filter_ro_only_handles_missing_filepath() -> None:
    rec = _r("", "A.MP4")
    rec.filepath = None
    out = list(_filter_ro_only([rec]))
    assert out == []
