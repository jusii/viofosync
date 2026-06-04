"""pending_bytes() sums remote_size across pending rows only."""
from __future__ import annotations

import time


def _db_with_rows(tmp_path, rows):
    """rows: list of (filename, state, remote_size)."""
    from web.db import Database
    db = Database(str(tmp_path / "v.db"))
    now = int(time.time())
    with db.write() as c:
        for (filename, state, size) in rows:
            c.execute(
                "INSERT INTO download_queue "
                "(filename, source_dir, state, remote_size, enqueued_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (filename, "/DCIM/Movie", state, size, now),
            )
    return db


def test_pending_bytes_sums_only_pending(tmp_path):
    from web.services.queue import pending_bytes
    db = _db_with_rows(tmp_path, [
        ("a.MP4", "pending", 100),
        ("b.MP4", "pending", 200),
        ("c.MP4", "done", 999),
        ("d.MP4", "failed", 50),
        ("e.MP4", "downloading", 77),
    ])
    assert pending_bytes(db) == 300


def test_pending_bytes_zero_when_empty(tmp_path):
    from web.services.queue import pending_bytes
    db = _db_with_rows(tmp_path, [])
    assert pending_bytes(db) == 0
