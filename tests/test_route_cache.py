"""Tests for the per-day route aggregation cache."""
from __future__ import annotations

import os
from pathlib import Path

from web.services import route_cache


def test_signature_is_order_independent(tmp_path: Path):
    a = tmp_path / "1.gpx"
    a.write_text("x")
    b = tmp_path / "2.gpx"
    b.write_text("yy")
    assert route_cache.signature([str(a), str(b)]) == \
        route_cache.signature([str(b), str(a)])


def test_signature_changes_when_a_file_changes(tmp_path: Path):
    a = tmp_path / "1.gpx"
    a.write_text("x")
    before = route_cache.signature([str(a)])
    a.write_text("xxxxxxxx")                      # size changes
    os.utime(a, (a.stat().st_atime, a.stat().st_mtime + 10))  # and mtime
    assert route_cache.signature([str(a)]) != before


def test_signature_changes_when_a_file_is_added(tmp_path: Path):
    a = tmp_path / "1.gpx"
    a.write_text("x")
    one = route_cache.signature([str(a)])
    b = tmp_path / "2.gpx"
    b.write_text("y")
    assert route_cache.signature([str(a), str(b)]) != one


def test_signature_ignores_missing_files(tmp_path: Path):
    a = tmp_path / "1.gpx"
    a.write_text("x")
    missing = str(tmp_path / "nope.gpx")
    assert route_cache.signature([str(a), missing]) == \
        route_cache.signature([str(a)])


def test_store_then_load_roundtrips(tmp_path: Path):
    rec = str(tmp_path)
    payload = {"date": "2026-06-02", "point_count": 3, "journeys": []}
    route_cache.store(rec, "2026-06-02", "sig1", payload)
    assert route_cache.load(rec, "2026-06-02", "sig1") == payload


def test_load_returns_none_on_signature_mismatch(tmp_path: Path):
    rec = str(tmp_path)
    route_cache.store(rec, "2026-06-02", "sig1", {"point_count": 1})
    assert route_cache.load(rec, "2026-06-02", "sig2") is None


def test_load_returns_none_when_absent(tmp_path: Path):
    assert route_cache.load(str(tmp_path), "2026-06-02", "sig") is None
