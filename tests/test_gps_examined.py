"""Tests for the ``gps_examined`` flag on clip_index.

Background: the manual "Extract GPS" button used to filter on
``has_gpx = 0``, which meant any clip whose moov atom yielded no
GPS data (parking, indoor, no satellite lock) was reprocessed on
every click — for a multi-thousand clip library that's many
minutes wasted per click. The fix tracks whether each clip has
been *examined* separately from whether it produced a sidecar.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from web.db import Database
from web.routers.archive import _process_extract_target, _select_extract_targets


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / "test.db"))


def _insert(
    db: Database, *,
    path: str, ts: int = 0,
    has_gpx: int = 0, gps_examined: int = 0,
) -> None:
    with db.write() as c:
        c.execute(
            "INSERT INTO clip_index "
            "(path, basename, group_name, timestamp, camera, "
            " sequence, event_type, size_bytes, has_gpx, "
            " gps_examined, scanned_at) "
            "VALUES (?, ?, '2026-01-01', ?, 'F', 1, 'normal', "
            "        100, ?, ?, 0)",
            (path, path.split("/")[-1], ts, has_gpx, gps_examined),
        )


# ---- migration ----

def test_migration_adds_gps_examined_to_existing_db(tmp_path: Path) -> None:
    """A pre-migration db.py would have created clip_index without
    gps_examined. Re-opening the database must not break and must
    add the column."""
    import sqlite3
    db_path = tmp_path / "old.db"
    # Build a clip_index without gps_examined to mimic the older
    # schema (the column doesn't exist yet on the user's
    # production volume).
    legacy = sqlite3.connect(str(db_path))
    legacy.executescript("""
        CREATE TABLE clip_index (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL UNIQUE,
            basename TEXT NOT NULL,
            group_name TEXT,
            timestamp INTEGER NOT NULL,
            camera TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            event_type TEXT,
            size_bytes INTEGER,
            has_gpx INTEGER NOT NULL DEFAULT 0,
            duration_s REAL,
            scanned_at INTEGER NOT NULL
        );
        INSERT INTO clip_index
            (path, basename, timestamp, camera, sequence,
             has_gpx, scanned_at)
        VALUES
            ('/x/A.MP4', 'A.MP4', 0, 'F', 1, 1, 0),
            ('/x/B.MP4', 'B.MP4', 0, 'F', 2, 0, 0);
    """)
    legacy.commit()
    legacy.close()

    # Re-opening through Database() must run the migration.
    Database(str(db_path))

    check = sqlite3.connect(str(db_path))
    cols = [r[1] for r in check.execute("PRAGMA table_info(clip_index)")]
    assert "gps_examined" in cols
    # Backfill: the row that already had a sidecar is now
    # marked examined.
    rows = check.execute(
        "SELECT path, gps_examined FROM clip_index ORDER BY path"
    ).fetchall()
    by_path = {p: e for p, e in rows}
    assert by_path["/x/A.MP4"] == 1
    assert by_path["/x/B.MP4"] == 0
    check.close()


def test_migration_is_idempotent(tmp_path: Path) -> None:
    """Opening a fresh-schema DB twice must not error on the
    duplicate-column ALTER."""
    db_path = tmp_path / "test.db"
    Database(str(db_path))
    Database(str(db_path))  # second open must not raise


# ---- _select_extract_targets ----

def test_force_returns_all_clips(db: Database) -> None:
    _insert(db, path="/x/A.MP4", has_gpx=1, gps_examined=1)
    _insert(db, path="/x/B.MP4", has_gpx=0, gps_examined=1)
    _insert(db, path="/x/C.MP4", has_gpx=0, gps_examined=0)
    targets = _select_extract_targets(db, force=True)
    assert len(targets) == 3


def test_default_skips_examined_clips(db: Database) -> None:
    """The whole point of the fix: empty-result clips marked
    ``gps_examined=1`` after the first run aren't picked up by
    subsequent clicks."""
    _insert(db, path="/x/HAS_GPX.MP4", has_gpx=1, gps_examined=1)
    _insert(db, path="/x/EMPTY.MP4", has_gpx=0, gps_examined=1)
    _insert(db, path="/x/NEW.MP4", has_gpx=0, gps_examined=0)
    targets = _select_extract_targets(db, force=False)
    assert [t[1] for t in targets] == ["/x/NEW.MP4"]


# ---- _process_extract_target ----


def _read_flags(db: Database, path: str) -> dict[str, int]:
    with db.conn() as c:
        row = c.execute(
            "SELECT has_gpx, gps_examined FROM clip_index "
            "WHERE path = ?",
            (path,),
        ).fetchone()
    return {
        "has_gpx": row["has_gpx"],
        "gps_examined": row["gps_examined"],
    }


def test_process_skips_moov_when_sidecar_already_present(
    tmp_path: Path, db: Database,
) -> None:
    """The first post-upgrade Extract GPS click on a library that
    has correct sidecars on disk but stale gps_examined=0 in the
    DB should NOT re-parse the moov atom — that's hours wasted on
    a multi-GB archive. The helper short-circuits."""
    clip = tmp_path / "OLD.MP4"
    clip.write_bytes(b"\x00" * 16)
    sidecar = clip.with_suffix(".MP4.gpx")
    sidecar.write_text("<gpx/>")

    _insert(db, path=str(clip), has_gpx=0, gps_examined=0)

    parse_calls: list = []
    def _parse(_):
        parse_calls.append(1)
        raise AssertionError("must not call parse_moov")
    def _gen(_, __):
        raise AssertionError("must not call generate_gpx")

    with db.conn() as c:
        cid = c.execute(
            "SELECT id FROM clip_index WHERE path=?", (str(clip),)
        ).fetchone()["id"]

    result = _process_extract_target(
        db, cid, str(clip),
        parse_moov=_parse, generate_gpx=_gen,
    )
    assert result == "sidecar_present"
    assert parse_calls == []
    flags = _read_flags(db, str(clip))
    assert flags == {"has_gpx": 1, "gps_examined": 1}


def test_process_extracts_when_no_sidecar(
    tmp_path: Path, db: Database,
) -> None:
    clip = tmp_path / "FRESH.MP4"
    clip.write_bytes(b"\x00" * 16)
    sidecar_path = str(clip) + ".gpx"

    _insert(db, path=str(clip), has_gpx=0, gps_examined=0)

    def _parse(_): return [{"lat": 0, "lon": 0, "t": 0}]
    def _gen(_, name): return f"<!-- {name} -->"

    with db.conn() as c:
        cid = c.execute(
            "SELECT id FROM clip_index WHERE path=?", (str(clip),)
        ).fetchone()["id"]

    result = _process_extract_target(
        db, cid, str(clip),
        parse_moov=_parse, generate_gpx=_gen,
    )
    assert result == "extracted"
    assert Path(sidecar_path).read_text().startswith("<!--")
    flags = _read_flags(db, str(clip))
    assert flags == {"has_gpx": 1, "gps_examined": 1}


def test_process_empty_marks_examined(
    tmp_path: Path, db: Database,
) -> None:
    """Clips with no GPS lock get gps_examined=1 so they're not
    re-parsed on the next click."""
    clip = tmp_path / "PARK.MP4"
    clip.write_bytes(b"\x00" * 16)

    _insert(db, path=str(clip), has_gpx=0, gps_examined=0)

    def _parse(_): return None
    def _gen(_, __): raise AssertionError("must not generate")

    with db.conn() as c:
        cid = c.execute(
            "SELECT id FROM clip_index WHERE path=?", (str(clip),)
        ).fetchone()["id"]

    result = _process_extract_target(
        db, cid, str(clip),
        parse_moov=_parse, generate_gpx=_gen,
    )
    assert result == "empty"
    assert not (Path(str(clip) + ".gpx")).exists()
    flags = _read_flags(db, str(clip))
    assert flags == {"has_gpx": 0, "gps_examined": 1}


def test_process_missing_file_marks_examined(
    tmp_path: Path, db: Database,
) -> None:
    """A row whose .MP4 vanished (retention, manual move) gets
    gps_examined=1 so we don't keep retrying it."""
    missing = str(tmp_path / "VANISHED.MP4")
    _insert(db, path=missing, has_gpx=0, gps_examined=0)

    def _parse(_): raise AssertionError("must not parse missing")
    def _gen(_, __): raise AssertionError("must not generate")

    with db.conn() as c:
        cid = c.execute(
            "SELECT id FROM clip_index WHERE path=?", (missing,)
        ).fetchone()["id"]

    result = _process_extract_target(
        db, cid, missing,
        parse_moov=_parse, generate_gpx=_gen,
    )
    assert result == "error"
    flags = _read_flags(db, missing)
    assert flags == {"has_gpx": 0, "gps_examined": 1}
