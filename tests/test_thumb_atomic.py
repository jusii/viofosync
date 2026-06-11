"""Partial ffmpeg output must never become a permanent cache hit.

thumbs/export-preview wrote ffmpeg output straight to the final cache
path; a killed or failed job left a truncated file that the
``exists() and size > 0`` cache check then served forever. Output must
land at the final path only on success. ensure_thumb also needs a
concurrency cap — a 100-clip day view used to spawn 100 ffmpegs.
"""
from __future__ import annotations

import asyncio
import glob
import os
import shutil
from pathlib import Path

from web.services import filmstrip, thumbs


class _FakeProc:
    def __init__(self, rc: int, hang: bool):
        self.returncode = rc
        self._hang = hang

    async def wait(self) -> int:
        if self._hang:
            await asyncio.sleep(60)
        return self.returncode

    def kill(self) -> None:
        self._hang = False


def _fake_ffmpeg(monkeypatch, *, rc: int = 1, hang: bool = False,
                 write_partial: bool = True, track: dict | None = None):
    """Replace subprocess spawning with a fake that (optionally) writes
    a partial file at the output path (ffmpeg's last argv) and then
    fails or hangs."""
    async def _exec(*argv, **kwargs):
        if track is not None:
            track["active"] += 1
            track["max"] = max(track["max"], track["active"])
            await asyncio.sleep(0.05)
            track["active"] -= 1
        if write_partial:
            Path(argv[-1]).write_bytes(b"PARTIAL")
        return _FakeProc(rc, hang)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    monkeypatch.setattr(shutil, "which", lambda name: "/bin/fake-ffmpeg")


async def test_failed_thumb_leaves_no_partial_cache(tmp_path, monkeypatch):
    _fake_ffmpeg(monkeypatch, rc=1)
    rec = tmp_path / "rec"
    rec.mkdir()
    video = rec / "clip.MP4"
    video.write_bytes(b"video")

    got = await thumbs.ensure_thumb(str(rec), 1, str(video))

    assert got is None
    final = thumbs.thumb_path(str(rec), 1)
    assert not os.path.exists(final), \
        "failed ffmpeg left a partial thumb that becomes a cache hit"
    leftovers = [p for p in glob.glob(final + "*") if not p.endswith(".fail")]
    assert leftovers == [], f"temp debris left behind: {leftovers}"


async def test_timed_out_thumb_leaves_no_partial_cache(tmp_path, monkeypatch):
    _fake_ffmpeg(monkeypatch, rc=0, hang=True)
    monkeypatch.setattr(thumbs, "_TIMEOUT_S", 0.1, raising=False)
    rec = tmp_path / "rec"
    rec.mkdir()
    video = rec / "clip.MP4"
    video.write_bytes(b"video")

    got = await thumbs.ensure_thumb(str(rec), 1, str(video))

    assert got is None
    assert not os.path.exists(thumbs.thumb_path(str(rec), 1))


async def test_thumb_generation_is_concurrency_capped(tmp_path, monkeypatch):
    track = {"active": 0, "max": 0}
    _fake_ffmpeg(monkeypatch, rc=1, write_partial=False, track=track)
    rec = tmp_path / "rec"
    rec.mkdir()
    videos = []
    for i in range(10):
        v = rec / f"clip{i}.MP4"
        v.write_bytes(b"video")
        videos.append(v)

    await asyncio.gather(*(
        thumbs.ensure_thumb(str(rec), i, str(v))
        for i, v in enumerate(videos)
    ))

    assert track["max"] <= 3, \
        f"{track['max']} concurrent ffmpeg thumb jobs (want <= 3)"


async def test_failed_sprite_montage_leaves_no_partial(tmp_path, monkeypatch):
    sprite = str(tmp_path / "42.jpg")

    async def _fake_run(cmd, timeout):
        dest = cmd[-1]
        Path(dest).write_bytes(b"X")
        # Tile extractions (inside the temp .tiles_ dir) succeed; the
        # final montage fails after writing partial output.
        return 0 if ".tiles_" in dest else 1

    monkeypatch.setattr(filmstrip, "_run_ffmpeg", _fake_run)

    ok = await filmstrip.generate_sprite_at(
        "/bin/fake-ffmpeg", str(tmp_path / "in.mp4"), sprite, [0.5, 1.5],
    )

    assert ok is False
    assert not os.path.exists(sprite), \
        "failed montage left a partial sprite at the cache path"
    assert glob.glob(sprite + "*") == [], "partial temp sprite left behind"
