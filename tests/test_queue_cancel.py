"""``mark_cancelled`` returns an interrupted download to ``pending``
without counting it as a failed attempt.

``mark_downloading`` bumps ``attempts`` on every pickup. When the user
pauses (or the camera drops) mid-download that increment must be handed
back, otherwise repeated pauses silently exhaust the retry budget and
flip the item to ``failed``. This mirrors ``reconcile_orphan_downloads``,
which already declines to penalise a crash-interrupted download.
"""
from __future__ import annotations

from web.db import Database
from web.services import queue as q


def _insert_pending(db: Database) -> int:
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, state, enqueued_at, attempts) "
            "VALUES (?, ?, ?, ?, ?)",
            ("X.MP4", "/DCIM", "pending", 0, 0),
        )
        return c.execute(
            "SELECT id FROM download_queue"
        ).fetchone()["id"]


def test_mark_cancelled_returns_to_pending_without_burning_attempt(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    item_id = _insert_pending(db)

    q.mark_downloading(db, item_id)        # attempts -> 1, state downloading
    q.mark_cancelled(db, item_id)

    with db.conn() as c:
        row = dict(
            c.execute(
                "SELECT * FROM download_queue WHERE id=?", (item_id,)
            ).fetchone()
        )
    assert row["state"] == "pending"
    assert row["attempts"] == 0            # the attempt was given back
    assert row["last_error"] is None
    assert row["started_at"] is None


def test_mark_cancelled_attempts_never_negative(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    item_id = _insert_pending(db)
    # Cancel without a preceding mark_downloading: attempts stays at 0,
    # never goes negative.
    q.mark_cancelled(db, item_id)
    with db.conn() as c:
        row = dict(
            c.execute(
                "SELECT attempts, state FROM download_queue WHERE id=?",
                (item_id,),
            ).fetchone()
        )
    assert row["attempts"] == 0
    assert row["state"] == "pending"
