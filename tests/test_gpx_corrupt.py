"""parse_moov must terminate on corrupt/truncated MP4 input.

Power-loss recordings (exactly what dashcams produce) can contain a
zero-size or short child atom inside ``moov``; the walker must treat
any atom smaller than its own 8-byte header as corrupt and stop,
rather than looping forever re-reading the same offset.
"""
from __future__ import annotations

import io
import struct
import threading

from viofosync_lib import parse_moov


def _run_with_timeout(fh, timeout: float = 2.0):
    """Run parse_moov in a thread so a regression fails fast instead
    of hanging the suite."""
    result: list = [None]
    done = threading.Event()

    def _target() -> None:
        result[0] = parse_moov(fh)
        done.set()

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    finished = done.wait(timeout=timeout)
    return finished, result[0]


def test_zero_size_child_atom_terminates():
    # moov atom (size 24) containing a child whose size field is 0 —
    # the inner walk used to do `sub_offset += 0` forever.
    data = (
        struct.pack(">I4s", 24, b"moov")
        + struct.pack(">I4s", 0, b"free")
        + b"\x00" * 8
    )
    finished, gps = _run_with_timeout(io.BytesIO(data))
    assert finished, "parse_moov hung on a zero-size child atom"
    assert gps == []


def test_undersized_child_atom_terminates():
    # Child size 4 < 8-byte header: corrupt; must not crawl/spin.
    data = (
        struct.pack(">I4s", 24, b"moov")
        + struct.pack(">I4s", 4, b"free")
        + b"\x00" * 8
    )
    finished, gps = _run_with_timeout(io.BytesIO(data))
    assert finished, "parse_moov hung on an undersized child atom"
    assert gps == []


def test_truncated_file_mid_child_header_terminates():
    # File ends in the middle of a child header — get_atom_info's
    # short read yields (0, '') and must end the walk.
    data = struct.pack(">I4s", 64, b"moov") + b"\x00" * 3
    finished, gps = _run_with_timeout(io.BytesIO(data))
    assert finished, "parse_moov hung on a truncated child header"
    assert gps == []
