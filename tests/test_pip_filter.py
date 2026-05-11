"""Tests for the PiP overlay filter-string builder.

The filter string drives ffmpeg's -filter_complex. Wrong
coordinates land the rear-camera inset off-screen or
overlapping the wrong corner; pin each corner explicitly.
"""
from __future__ import annotations

from web.services.exporter import _pip_filter_complex

_SCALE = "[1:v]scale=iw/4:ih/4[pip];"


def test_top_right_is_default_corner() -> None:
    """20px from the top-right edge."""
    assert _pip_filter_complex("top_right") == (
        _SCALE + "[0:v][pip]overlay=W-w-20:20"
    )


def test_top_left_corner() -> None:
    assert _pip_filter_complex("top_left") == (
        _SCALE + "[0:v][pip]overlay=20:20"
    )


def test_bottom_right_corner() -> None:
    assert _pip_filter_complex("bottom_right") == (
        _SCALE + "[0:v][pip]overlay=W-w-20:H-h-20"
    )


def test_bottom_left_corner() -> None:
    assert _pip_filter_complex("bottom_left") == (
        _SCALE + "[0:v][pip]overlay=20:H-h-20"
    )


def test_unknown_position_falls_back_to_top_right() -> None:
    """A typo or stale value should not crash ffmpeg invocation;
    fall back to the default corner."""
    assert _pip_filter_complex("middle") == (
        _SCALE + "[0:v][pip]overlay=W-w-20:20"
    )
