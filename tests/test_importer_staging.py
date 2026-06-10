"""Staging recovery: a crash must never cost the only copy of a clip.

Same-volume folder ingest *moves* the source into ``.import_tmp``
before renaming into the archive; the old ``_clean_staging`` deleted
everything there on the next run — destroying the only copy after a
crash between the two renames. Staged Viofo-named files are complete
by construction (atomic rename / post-verification rename), so they
are recovered. In-flight browser uploads stream to a ``.part`` name
so cleanup can tell them apart and never deletes a fresh one.
"""
from __future__ import annotations

import os
import time
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from web.db import Database
from web.services import importer

NAME = "2026_0102_090000_0002F.MP4"


def _snap(rec: Path):
    return types.SimpleNamespace(
        recordings=str(rec), grouping="daily", gps_extract=False,
        retention_disk_pct=0, recordings_quota_gb=0,
        retention_protect_ro=True, retention_max_days=0, import_path="",
    )


@pytest.fixture
def env(tmp_path: Path):
    rec = tmp_path / "rec"
    rec.mkdir()
    db = Database(str(rec / ".viofosync.db"))
    staging = rec / importer.STAGING_DIRNAME
    staging.mkdir()
    return rec, db, staging


def test_recover_staging_reingests_complete_clip(env):
    rec, db, staging = env
    (staging / NAME).write_bytes(b"b" * 20)  # crash left this here

    summary = importer.recover_staging(db, _snap(rec))

    assert summary["recovered"] == 1
    assert not (staging / NAME).exists()
    # The clip landed in the archive at its grouped destination.
    dests = list(rec.rglob(NAME))
    assert len(dests) == 1 and dests[0].read_bytes() == b"b" * 20


def test_recover_staging_keeps_fresh_part_removes_stale(env):
    rec, db, staging = env
    fresh = staging / (NAME + ".part")
    fresh.write_bytes(b"streaming")
    stale = staging / ("2026_0101_080000_0001F.MP4.part")
    stale.write_bytes(b"dead upload")
    old = time.time() - 7200
    os.utime(stale, (old, old))

    importer.recover_staging(db, _snap(rec))

    assert fresh.exists(), "recovery deleted an in-flight upload's .part"
    assert not stale.exists(), "stale crashed-upload debris kept forever"


def test_folder_ingest_recovers_instead_of_deleting(env):
    rec, db, staging = env
    (staging / NAME).write_bytes(b"b" * 20)
    root = rec.parent / "empty-import"
    root.mkdir()

    importer.run_folder_ingest(db, _snap(rec), hub=None, loop=None,
                               root=str(root))

    dests = list(rec.rglob(NAME))
    assert len(dests) == 1, \
        "folder ingest destroyed a staged clip instead of recovering it"


async def test_upload_streams_to_part_name(tmp_path, monkeypatch):
    """While bytes are in flight the staging file must carry a .part
    suffix — only a verified complete upload gets the plain name that
    recovery treats as safe to archive."""
    from starlette.requests import Request

    from web.routers import imports as imports_router

    rec = tmp_path / "rec"
    rec.mkdir()
    snap = _snap(rec)
    provider = MagicMock()
    provider.get.return_value = snap
    db = Database(str(tmp_path / "t.db"))
    app = SimpleNamespace(
        state=SimpleNamespace(settings_provider=provider, db=db))

    staging = rec / importer.STAGING_DIRNAME
    seen_during_stream: list[set] = []
    body = b"x" * 64
    messages = [
        {"type": "http.request", "body": body[:32], "more_body": True},
        {"type": "http.request", "body": body[32:], "more_body": False},
    ]

    async def receive():
        if staging.is_dir():
            seen_during_stream.append({p.name for p in staging.iterdir()})
        return messages.pop(0)

    scope = {
        "type": "http", "method": "POST", "path": "/api/import/upload",
        "query_string": b"",
        "headers": [
            (b"x-import-path", NAME.encode()),
            (b"x-import-size", str(len(body)).encode()),
        ],
        "app": app,
    }
    res = await imports_router.upload(Request(scope, receive))

    assert res["status"] == "imported"
    streamed_names = set().union(*seen_during_stream) if seen_during_stream else set()
    plain = {n for n in streamed_names if not n.endswith(".part")}
    assert plain == set(), \
        f"upload streamed to a plain staging name: {plain}"
