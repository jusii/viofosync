"""scanner.scan must not wipe the index when recordings is unavailable.

Root cause of "the duration sweep re-runs across all clips after it
completed yesterday": scanner.scan rebuilds the index from a glob of the
recordings directory and prunes any DB row whose file it didn't see. When
that glob returns nothing — the volume not yet mounted at container start,
or a transient NAS glitch — the prune ran an unconditional
``DELETE FROM clip_index``, wiping every row. The next scan re-inserted
them via an INSERT that omits duration_s (→ NULL) and resets gps_examined,
so the duration sweep (and GPS re-exam, thumbs) re-ran across the whole
archive.
"""
from __future__ import annotations

import logging
from pathlib import Path

from web.db import Database
from web.services import scanner


def _insert(db: Database, path: str, *, duration_s: float = 42.0,
            gps_examined: int = 1) -> None:
    with db.write() as c:
        c.execute(
            "INSERT INTO clip_index "
            "(path, basename, group_name, timestamp, camera, sequence, "
            " event_type, size_bytes, has_gpx, gps_examined, duration_s, "
            " scanned_at) "
            "VALUES (?, ?, '2026-06-03', 0, 'F', 1, 'normal', 100, 0, ?, ?, 0)",
            (path, path.split("/")[-1], gps_examined, duration_s),
        )


def _counts(db: Database) -> tuple[int, int]:
    with db.conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n, "
            "       SUM(CASE WHEN duration_s > 0 THEN 1 ELSE 0 END) AS d "
            "FROM clip_index"
        ).fetchone()
    return row["n"], (row["d"] or 0)


def test_empty_scan_does_not_wipe_index(tmp_path: Path, caplog) -> None:
    """A scan that finds zero clips (recordings unavailable) must keep the
    existing rows and their durations, not delete the whole index."""
    db = Database(str(tmp_path / "t.db"))
    _insert(db, "/recordings/2026-06-03/2026_0603_082421_0001F.MP4")
    _insert(db, "/recordings/2026-06-03/2026_0603_082421_0001R.MP4")
    assert _counts(db) == (2, 2)

    empty = tmp_path / "recordings"
    empty.mkdir()  # exists but contains no clips -> glob yields nothing

    with caplog.at_level(logging.WARNING, logger="viofosync.scanner"):
        scanner.scan(db, str(empty), "daily")

    assert _counts(db) == (2, 2)  # index intact, durations preserved
    assert any("skip" in r.getMessage().lower() or "0 clip" in r.getMessage()
               for r in caplog.records), "expected a warning about the empty scan"


def test_scan_still_prunes_genuinely_vanished_file(tmp_path: Path) -> None:
    """The empty-scan guard must not disable legitimate pruning: when the
    scan DOES find clips, a row whose file is gone is still removed."""
    db = Database(str(tmp_path / "t.db"))
    day = tmp_path / "recordings" / "2026-06-03"
    day.mkdir(parents=True)
    present = day / "2026_0603_082421_0001F.MP4"
    present.write_bytes(b"\x00" * 16)

    _insert(db, str(present))                                   # on disk
    _insert(db, "/recordings/2026-06-03/2026_0603_090000_0002F.MP4")  # gone

    scanner.scan(db, str(tmp_path / "recordings"), "daily")

    with db.conn() as c:
        paths = [r["path"] for r in
                 c.execute("SELECT path FROM clip_index ORDER BY path")]
    assert str(present) in paths           # the real file kept
    assert "/recordings/2026-06-03/2026_0603_090000_0002F.MP4" not in paths
