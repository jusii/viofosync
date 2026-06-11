"""Thumbnail sweep must not re-attempt clips that can't produce a thumb.

Regression: ``ensure_thumb`` returned None on ffmpeg failure and left no
marker, so un-thumbable clips (short/corrupt/partial) were re-selected and
re-run through ffmpeg on every sweep. With a sweep after every working
cycle (and on pause) that was a recurring CPU storm.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from web.db import Database
from web.services import scanner, thumbs


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / ".viofosync.db"))


def _add_clip(db: Database, path: str, clip_id: int = 1) -> int:
    with db.write() as c:
        c.execute(
            "INSERT INTO clip_index (id, path, basename, timestamp, camera, "
            "sequence, scanned_at) VALUES (?,?,?,?,?,?,?)",
            (clip_id, path, os.path.basename(path), 0, "F", 0, 0),
        )
    return clip_id


def test_mark_failed_then_skipped(tmp_path: Path):
    rec = tmp_path / "rec"
    rec.mkdir()
    video = rec / "clip.MP4"
    video.write_bytes(b"not a real video")
    # A fresh failure marker (recorded after the video was written) means
    # "don't bother trying again until the file changes".
    thumbs.mark_failed(str(rec), 1)
    assert thumbs.failed_recently(str(rec), 1, str(video)) is True


def test_stale_marker_retried_after_file_changes(tmp_path: Path):
    rec = tmp_path / "rec"
    rec.mkdir()
    video = rec / "clip.MP4"
    video.write_bytes(b"old")
    thumbs.mark_failed(str(rec), 1)
    # The clip is later rewritten (e.g. a partial import got redone) — its
    # mtime moves past the marker, so the thumb is worth another attempt.
    time.sleep(0.01)
    os.utime(str(video), None)
    assert thumbs.failed_recently(str(rec), 1, str(video)) is False


async def test_sweep_skips_failed_clip_on_next_pass(tmp_path: Path, db: Database):
    rec = tmp_path / "rec"
    rec.mkdir()
    video = rec / "clip.MP4"
    video.write_bytes(b"not a real video")
    _add_clip(db, str(video))

    calls = {"n": 0}

    async def _fake_ensure(recordings, clip_id, path):
        calls["n"] += 1
        thumbs.mark_failed(recordings, clip_id)   # simulate ffmpeg failure
        return None

    import unittest.mock as _m
    with _m.patch.object(thumbs, "ensure_thumb", _fake_ensure):
        await scanner.sweep_missing_thumbs(db, str(rec))
        await scanner.sweep_missing_thumbs(db, str(rec))

    # First sweep attempts it once; the second must skip it.
    assert calls["n"] == 1
