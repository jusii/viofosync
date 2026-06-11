"""Tests for the clip duration ffprobe sweep."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from web.db import Database
from web.services import durations


def _insert_clip(db, clip_id, path, duration_s=None):
    with db.write() as c:
        c.execute(
            "INSERT INTO clip_index "
            "(id, path, basename, group_name, timestamp, camera, "
            " sequence, event_type, has_gpx, gps_examined, scanned_at, duration_s) "
            "VALUES (?,?,?,?,?,?,?,?,0,0,?,?)",
            (clip_id, path, f"{clip_id}.MP4", "2026-06-02",
             1_717_312_440, "F", clip_id, "normal", 1_717_312_440, duration_s),
        )


async def test_probe_duration_parses_ffprobe(monkeypatch):
    class _P:
        async def communicate(self):
            return (b"60.05\n", b"")
    async def fake_exec(*a, **k):
        return _P()
    monkeypatch.setattr(durations.shutil, "which", lambda _n: "/usr/bin/ffprobe")
    monkeypatch.setattr(durations.asyncio, "create_subprocess_exec", fake_exec)
    assert await durations.probe_duration("/x.mp4") == pytest.approx(60.05)


async def test_probe_duration_none_without_ffprobe(monkeypatch):
    monkeypatch.setattr(durations.shutil, "which", lambda _n: None)
    assert await durations.probe_duration("/x.mp4") is None


async def test_sweep_updates_null_durations(tmp_path: Path, monkeypatch):
    db = Database(str(tmp_path / "t.db"))
    f1 = tmp_path / "clip1.mp4"
    f1.write_bytes(b"\0")
    f2 = tmp_path / "clip2.mp4"
    f2.write_bytes(b"\0")
    _insert_clip(db, 1, str(f1), duration_s=None)        # needs probe
    _insert_clip(db, 2, str(f2), duration_s=60.0)        # already has one -> skipped

    async def fake_probe(path):
        return 42.0, "mvhd"
    monkeypatch.setattr(durations, "_probe_with_method", fake_probe)

    updated = await durations.sweep_missing_durations(db)
    assert updated == 1
    with db.conn() as c:
        d1 = c.execute("SELECT duration_s FROM clip_index WHERE id=1").fetchone()["duration_s"]
        d2 = c.execute("SELECT duration_s FROM clip_index WHERE id=2").fetchone()["duration_s"]
    assert d1 == pytest.approx(42.0)
    assert d2 == pytest.approx(60.0)   # untouched


async def test_sweep_skips_missing_files(tmp_path: Path, monkeypatch):
    db = Database(str(tmp_path / "t.db"))
    _insert_clip(db, 1, str(tmp_path / "gone.mp4"), duration_s=None)  # file absent
    async def fake_probe(path):
        raise AssertionError("should not probe a missing file")
    monkeypatch.setattr(durations, "_probe_with_method", fake_probe)
    assert await durations.sweep_missing_durations(db) == 0


async def test_sweep_persists_incrementally_when_interrupted(
    tmp_path: Path, monkeypatch
):
    """A sweep cancelled partway (e.g. server shutdown) must have already
    persisted the clips it probed before the interruption — otherwise a
    restart loses all progress and the sweep can never finish."""
    db = Database(str(tmp_path / "t.db"))
    f1 = tmp_path / "clip1.mp4"
    f1.write_bytes(b"\0")
    f2 = tmp_path / "clip2.mp4"
    f2.write_bytes(b"\0")
    _insert_clip(db, 1, str(f1), duration_s=None)
    _insert_clip(db, 2, str(f2), duration_s=None)

    async def fake_probe(path):
        if path == str(f1):
            return 42.0, "mvhd"
        raise asyncio.CancelledError   # shutdown hits while probing clip 2

    monkeypatch.setattr(durations, "_probe_with_method", fake_probe)

    # concurrency=1 -> clip 1 is fully probed (and must be flushed) before
    # clip 2 runs; batch_size=1 -> each result is persisted as it lands.
    with pytest.raises(asyncio.CancelledError):
        await durations.sweep_missing_durations(db, concurrency=1, batch_size=1)

    with db.conn() as c:
        d1 = c.execute(
            "SELECT duration_s FROM clip_index WHERE id=1"
        ).fetchone()["duration_s"]
    assert d1 == pytest.approx(42.0)   # survived the interruption


async def test_sweep_logs_method_breakdown(tmp_path: Path, monkeypatch, caplog):
    """The sweep reports how many clips it resolved via the fast mvhd path
    vs the ffprobe fallback, so the fast path is visible in the Logs tab."""
    import logging

    db = Database(str(tmp_path / "t.db"))
    for i in (1, 2, 3):
        f = tmp_path / f"clip{i}.mp4"
        f.write_bytes(b"\0")
        _insert_clip(db, i, str(f), duration_s=None)

    async def fake(path):
        # clip3 needs the ffprobe fallback; the rest resolve via mvhd
        return (30.0, "ffprobe") if path.endswith("clip3.mp4") else (15.0, "mvhd")
    monkeypatch.setattr(durations, "_probe_with_method", fake)

    with caplog.at_level(logging.INFO, logger="viofosync.durations"):
        updated = await durations.sweep_missing_durations(db)

    assert updated == 3
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "2 via mvhd" in msgs
    assert "1 via ffprobe" in msgs


async def test_sweep_writes_all_with_small_batches(tmp_path: Path, monkeypatch):
    """Batched flushing must not drop rows: every probed clip is written
    even when the batch size is smaller than the number of clips."""
    db = Database(str(tmp_path / "t.db"))
    paths = []
    for i in range(1, 6):
        f = tmp_path / f"clip{i}.mp4"
        f.write_bytes(b"\0")
        paths.append(str(f))
        _insert_clip(db, i, str(f), duration_s=None)

    async def fake_probe(path):
        return 10.0, "mvhd"

    monkeypatch.setattr(durations, "_probe_with_method", fake_probe)
    updated = await durations.sweep_missing_durations(db, batch_size=2)
    assert updated == 5
    with db.conn() as c:
        vals = [
            r["duration_s"]
            for r in c.execute(
                "SELECT duration_s FROM clip_index ORDER BY id"
            ).fetchall()
        ]
    assert vals == [pytest.approx(10.0)] * 5
