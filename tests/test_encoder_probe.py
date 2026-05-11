"""Tests for the boot-time encoder probe.

`ffmpeg -encoders` only reports what was compiled in. On a
Synology container without /dev/dri passthrough, h264_qsv shows
up as compiled-in but fails at runtime with `MFX session: -9`,
which makes `auto` mode pick a broken encoder. The 1-frame test
encode catches this so the dropdown only ever lists encoders
that actually work end-to-end.
"""
from __future__ import annotations

from unittest.mock import patch

from web.services import exporter

_ENCODERS_OUT_ALL = """\
V..... libx264              libx264 H.264 / AVC / MPEG-4 AVC
V..... h264_qsv             h264 (qsv)
V..... h264_nvenc           h264 (nvenc)
V..... h264_vaapi           h264 (vaapi)
V..... h264_videotoolbox    h264 (videotoolbox)
"""


_ENCODERS_OUT_SOFTWARE_ONLY = """\
V..... libx264              libx264 H.264 / AVC / MPEG-4 AVC
"""


async def test_probe_keeps_software_when_only_software_compiled() -> None:
    with patch.object(exporter, "ffmpeg_available", return_value=True), \
         patch.object(exporter, "_probe_encoders_sync",
                      return_value=_ENCODERS_OUT_SOFTWARE_ONLY), \
         patch.object(exporter, "_test_encoder_sync", return_value=True):
        result = await exporter.probe_encoders()
    assert result["software"] is True
    assert result["qsv"] is False
    assert result["nvenc"] is False


async def test_probe_drops_qsv_when_runtime_test_fails() -> None:
    """The bug this whole file exists for: h264_qsv compiled in
    but unusable on the host. Probe must mark it False."""
    def _fake_test(name: str) -> bool:
        # Software always works; QSV fails at runtime.
        if name == "qsv":
            return False
        return True

    with patch.object(exporter, "ffmpeg_available", return_value=True), \
         patch.object(exporter, "_probe_encoders_sync",
                      return_value=_ENCODERS_OUT_ALL), \
         patch.object(exporter, "_test_encoder_sync",
                      side_effect=_fake_test):
        result = await exporter.probe_encoders()
    assert result["qsv"] is False
    assert result["software"] is True
    # Other hardware encoders that pass the runtime test stay True.
    assert result["nvenc"] is True
    assert result["vaapi"] is True


async def test_probe_short_circuits_when_ffmpeg_missing() -> None:
    """No ffmpeg binary at all → every encoder is unavailable."""
    with patch.object(exporter, "ffmpeg_available", return_value=False):
        result = await exporter.probe_encoders()
    assert all(v is False for v in result.values())


async def test_probe_skips_runtime_test_for_uncompiled_encoders() -> None:
    """Encoders missing from the -encoders output should not
    trigger a runtime test (would just fail and waste seconds)."""
    test_calls: list[str] = []

    def _track(name: str) -> bool:
        test_calls.append(name)
        return True

    with patch.object(exporter, "ffmpeg_available", return_value=True), \
         patch.object(exporter, "_probe_encoders_sync",
                      return_value=_ENCODERS_OUT_SOFTWARE_ONLY), \
         patch.object(exporter, "_test_encoder_sync", side_effect=_track):
        await exporter.probe_encoders()
    # Only "software" was compiled in. None of the hardware
    # encoders should reach the runtime test — that would spawn
    # ffmpeg subprocesses for nothing on a software-only build.
    for name in ("qsv", "nvenc", "vaapi", "videotoolbox"):
        assert name not in test_calls, (
            f"{name} not compiled in but probe still ran a "
            f"runtime test for it"
        )


def test_test_encoder_software_always_returns_true() -> None:
    """libx264 ships with every ffmpeg build; no need to spawn
    a subprocess to verify."""
    # No subprocess patching needed — the function short-circuits
    # before exec-ing ffmpeg.
    assert exporter._test_encoder_sync("software") is True
