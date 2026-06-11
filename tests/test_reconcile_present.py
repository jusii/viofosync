"""Reconcile must not leave on-disk clips stuck as pending/failed.

Regression: a clip the dashcam listed (queued pending), then placed on
disk by another path (bulk web-upload / manual copy), used to stay
pending because reconcile only inserted a 'done' row when the filename
was absent from the queue. On the next Wi-Fi cycle the worker re-tried
the download and the dashcam 404'd it.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from web.db import Database
from web.services import queue as q


class _Rec:
    def __init__(self, filename: str, *, filepath: str = "/DCIM/Movie",
                 size: int = 1000) -> None:
        self.filename = filename
        self.filepath = filepath
        self.size = size
        self.datetime = _dt.datetime(2026, 5, 19, 7, 47, 52)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / ".viofosync.db"))


def _states(db: Database) -> dict[str, str]:
    with db.conn() as c:
        return {r["filename"]: r["state"] for r in c.execute(
            "SELECT filename, state FROM download_queue").fetchall()}


def _seed(db: Database, filename: str, state: str) -> None:
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue (filename, source_dir, state, "
            "enqueued_at) VALUES (?,?,?,0)",
            (filename, "/DCIM/Movie", state),
        )


@pytest.mark.parametrize("state", ["pending", "failed"])
def test_reconcile_heals_on_disk_clip_stuck_in_queue(db: Database, state: str):
    name = "2026_0519_074752_022262PF.MP4"
    _seed(db, name, state)                      # camera listed it earlier
    # File is now on disk (web-upload) and the camera still lists it.
    q.reconcile(db, [_Rec(name)], present_filenames=[name])
    assert _states(db)[name] == "done"
