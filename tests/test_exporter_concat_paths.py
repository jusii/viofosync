"""The join concat demuxer must list clips by ABSOLUTE path.

ffmpeg's concat demuxer resolves *relative* entries in the list file
against the directory of the list file itself — which lives in the
system temp dir. So when clip_index stores relative paths (a dev box
launched with a relative ``RECORDINGS``), a relative entry sends
ffmpeg looking under ``/tmp/.../recordings/...`` and the export dies
with "No such file or directory". Writing absolute paths makes the
concat robust regardless of how the path was stored, or where the
temp list file happens to live.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from web.db import Database
from web.services.exporter import ExportWorker


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / "test.db"))


async def _async_noop(_event):  # pragma: no cover — broadcast stub
    pass


async def test_concat_list_uses_absolute_paths(
    db: Database, tmp_path: Path, monkeypatch
) -> None:
    worker = ExportWorker(db=db, provider=MagicMock(), broadcast=_async_noop)
    captured: dict = {}

    async def fake_probe(_clips):
        return 1.0

    async def fake_ffmpeg(_job_id, args, _total):
        # Read the concat list while it still exists (-i <list_file>),
        # then touch the output so the rc==0 branch marks it done.
        list_file = args[args.index("-i") + 1]
        captured["lines"] = Path(list_file).read_text().splitlines()
        Path(args[-1]).write_bytes(b"\0")
        return 0, ""

    monkeypatch.setattr(worker, "_probe_total", fake_probe)
    monkeypatch.setattr(worker, "_run_ffmpeg", fake_ffmpeg)

    # A relative clip path, exactly as stored when RECORDINGS is given
    # relative on a local dev box.
    rel = "recordings/2026-06-04/clip_F.MP4"
    out = str(tmp_path / "out.mp4")
    await worker._concat(1, [{"path": rel}], out)

    assert captured.get("lines"), "ffmpeg was never invoked"
    for line in captured["lines"]:
        # Each entry is: file '<path>'
        assert line.startswith("file '/"), \
            f"concat entry is not absolute: {line!r}"
    assert f"file '{os.path.abspath(rel)}'" in captured["lines"]
