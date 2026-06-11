"""Tests for the direct mvhd-box duration parser.

Reading the MP4 ``moov/mvhd`` box gives clip duration without spawning an
ffprobe subprocess per clip — far cheaper for the duration sweep across a
multi-thousand-clip archive. The parser seeks past ``mdat`` rather than
reading it, so it's cheap even when ``moov`` sits at the end of a large
file (the usual dashcam layout). Anything it can't parse returns None so
the caller falls back to ffprobe.
"""
from __future__ import annotations

import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from web.services import durations

# --- ISO-BMFF box builders for deterministic fixtures ---

def _box(btype: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + btype + payload


def _box64(btype: bytes, payload: bytes) -> bytes:
    # size==1 sentinel, then a 64-bit largesize (how big mdat is encoded)
    return struct.pack(">I", 1) + btype + struct.pack(">Q", 16 + len(payload)) + payload


def _mvhd_v0(timescale: int, duration: int) -> bytes:
    p = bytes([0, 0, 0, 0])                       # version 0 + flags
    p += struct.pack(">I", 0)                     # creation_time
    p += struct.pack(">I", 0)                     # modification_time
    p += struct.pack(">I", timescale)
    p += struct.pack(">I", duration)
    p += b"\x00" * 80                             # trailing fields (unparsed)
    return _box(b"mvhd", p)


def _mvhd_v1(timescale: int, duration: int) -> bytes:
    p = bytes([1, 0, 0, 0])                       # version 1 + flags
    p += struct.pack(">Q", 0)                     # creation_time (64-bit)
    p += struct.pack(">Q", 0)                     # modification_time (64-bit)
    p += struct.pack(">I", timescale)
    p += struct.pack(">Q", duration)              # duration (64-bit)
    p += b"\x00" * 80
    return _box(b"mvhd", p)


def _w(tmp_path: Path, name: str, data: bytes) -> str:
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def test_mvhd_v0_moov_first(tmp_path: Path) -> None:
    data = _box(b"ftyp", b"isomiso2") + _box(b"moov", _mvhd_v0(1000, 5000))
    assert durations._probe_duration_mvhd(_w(tmp_path, "a.mp4", data)) == pytest.approx(5.0)


def test_mvhd_after_large_mdat(tmp_path: Path) -> None:
    """Dashcam layout: moov AFTER a big mdat. Parser must seek past mdat
    (not read it) and still find mvhd."""
    data = (_box(b"ftyp", b"isom")
            + _box(b"mdat", b"\x00" * 100_000)
            + _box(b"moov", _mvhd_v0(90000, 90000 * 12)))
    assert durations._probe_duration_mvhd(_w(tmp_path, "b.mp4", data)) == pytest.approx(12.0)


def test_mvhd_v1_64bit_duration(tmp_path: Path) -> None:
    data = _box(b"ftyp", b"isom") + _box(b"moov", _mvhd_v1(48000, 48000 * 7))
    assert durations._probe_duration_mvhd(_w(tmp_path, "c.mp4", data)) == pytest.approx(7.0)


def test_mvhd_64bit_mdat_size(tmp_path: Path) -> None:
    """Large mdat encoded with the 64-bit size form must be skipped correctly."""
    data = (_box(b"ftyp", b"isom")
            + _box64(b"mdat", b"\x00" * 5000)
            + _box(b"moov", _mvhd_v0(1000, 3000)))
    assert durations._probe_duration_mvhd(_w(tmp_path, "d.mp4", data)) == pytest.approx(3.0)


def test_mvhd_skips_empty_free_box(tmp_path: Path) -> None:
    """ffmpeg emits a zero-payload ``free`` box (size 8) before mdat; the
    scan must treat it as valid and keep going, not bail."""
    data = (_box(b"ftyp", b"isom")
            + struct.pack(">I", 8) + b"free"          # empty free box
            + _box(b"mdat", b"\x00" * 200)
            + _box(b"moov", _mvhd_v0(1000, 4000)))
    assert durations._probe_duration_mvhd(_w(tmp_path, "free.mp4", data)) == pytest.approx(4.0)


def test_mvhd_no_moov_returns_none(tmp_path: Path) -> None:
    data = _box(b"ftyp", b"isom") + _box(b"mdat", b"\x00" * 1000)
    assert durations._probe_duration_mvhd(_w(tmp_path, "e.mp4", data)) is None


def test_mvhd_unknown_duration_returns_none(tmp_path: Path) -> None:
    data = _box(b"ftyp", b"isom") + _box(b"moov", _mvhd_v0(1000, 0xFFFFFFFF))
    assert durations._probe_duration_mvhd(_w(tmp_path, "f.mp4", data)) is None


def test_mvhd_zero_timescale_returns_none(tmp_path: Path) -> None:
    data = _box(b"ftyp", b"isom") + _box(b"moov", _mvhd_v0(0, 5000))
    assert durations._probe_duration_mvhd(_w(tmp_path, "z.mp4", data)) is None


def test_mvhd_missing_file_returns_none(tmp_path: Path) -> None:
    assert durations._probe_duration_mvhd(str(tmp_path / "nope.mp4")) is None


def test_mvhd_truncated_returns_none(tmp_path: Path) -> None:
    # moov header claims a size the file doesn't contain
    data = _box(b"ftyp", b"isom") + struct.pack(">I", 0x1000) + b"moov\x00\x00"
    assert durations._probe_duration_mvhd(_w(tmp_path, "g.mp4", data)) is None


def test_mvhd_matches_real_ffmpeg_clip(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg not available")
    clip = tmp_path / "real.mp4"
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "testsrc=size=320x180:duration=5:rate=30", "-c:v", "libx264",
         str(clip)],
        check=True,
    )
    assert durations._probe_duration_mvhd(str(clip)) == pytest.approx(5.0, abs=0.3)


# --- orchestration: probe_duration prefers mvhd, falls back to ffprobe ---


async def test_probe_duration_prefers_mvhd_over_ffprobe(tmp_path: Path, monkeypatch) -> None:
    data = _box(b"ftyp", b"isom") + _box(b"moov", _mvhd_v0(1000, 8000))
    path = _w(tmp_path, "h.mp4", data)

    def _boom(*a, **k):
        raise AssertionError("ffprobe must not run when mvhd parses")
    monkeypatch.setattr(durations.asyncio, "create_subprocess_exec", _boom)

    assert await durations.probe_duration(path) == pytest.approx(8.0)


async def test_probe_duration_falls_back_to_ffprobe(tmp_path: Path, monkeypatch) -> None:
    # No parseable moov -> mvhd returns None -> ffprobe is used.
    path = _w(tmp_path, "i.mp4", _box(b"ftyp", b"isom"))

    class _P:
        async def communicate(self):
            return (b"33.3\n", b"")
    async def fake_exec(*a, **k):
        return _P()
    monkeypatch.setattr(durations.shutil, "which", lambda _n: "/usr/bin/ffprobe")
    monkeypatch.setattr(durations.asyncio, "create_subprocess_exec", fake_exec)

    assert await durations.probe_duration(path) == pytest.approx(33.3)
