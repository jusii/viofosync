"""Tests for the one-shot legacy-DB migration helper."""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from web.db import Database, migrate_legacy_db_path


def _seed_legacy_db(path: Path) -> None:
    """Write a sentinel row into a SQLite file at `path` so we can
    confirm the migrated DB carries the same data."""
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path))
    c.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    c.execute("INSERT INTO kv (key, value) VALUES ('marker', 'legacy-data')")
    c.commit()
    c.close()


def test_migrate_copies_legacy_db_and_renames_source(
    tmp_path: Path, monkeypatch,
) -> None:
    """When legacy DB exists and new path is empty, migrate copies the
    DB across, renames the legacy file to .viofosync.db.migrated, and
    the migrated DB returns the original row."""
    rec = tmp_path / "rec"
    cfg = tmp_path / "cfg"
    rec.mkdir()
    monkeypatch.setenv("RECORDINGS", str(rec))

    legacy = rec / ".viofosync.db"
    _seed_legacy_db(legacy)

    new_path = str(cfg / "viofosync.db")
    migrate_legacy_db_path(new_path)

    assert Path(new_path).exists(), "new DB should exist after migration"
    assert not legacy.exists(), "legacy file should be renamed"
    assert (rec / ".viofosync.db.migrated").exists(), \
        "legacy file should be renamed to .viofosync.db.migrated"

    # Sentinel data survived the copy.
    db = Database(new_path)
    with db.conn() as c:
        row = c.execute(
            "SELECT value FROM kv WHERE key = 'marker'"
        ).fetchone()
    assert row is not None
    assert row["value"] == "legacy-data"


def test_migrate_skips_when_new_path_already_exists(
    tmp_path: Path, monkeypatch,
) -> None:
    """If a DB exists at the new path already, don't overwrite it and
    don't rename the legacy file (the migration short-circuits at the
    top)."""
    rec = tmp_path / "rec"
    cfg = tmp_path / "cfg"
    rec.mkdir()
    cfg.mkdir()
    monkeypatch.setenv("RECORDINGS", str(rec))

    legacy = rec / ".viofosync.db"
    _seed_legacy_db(legacy)

    new_path = cfg / "viofosync.db"
    new_path.write_bytes(b"pre-existing content")

    migrate_legacy_db_path(str(new_path))

    assert new_path.read_bytes() == b"pre-existing content", \
        "existing new-path file must not be overwritten"
    assert legacy.exists(), \
        "legacy file should remain untouched when migration short-circuits"


def test_migrate_noop_when_legacy_missing(
    tmp_path: Path, monkeypatch,
) -> None:
    """No legacy DB → helper returns without creating anything."""
    rec = tmp_path / "rec"
    cfg = tmp_path / "cfg"
    rec.mkdir()
    monkeypatch.setenv("RECORDINGS", str(rec))

    new_path = cfg / "viofosync.db"
    migrate_legacy_db_path(str(new_path))

    assert not new_path.exists(), "no new DB should be created"
    assert not (rec / ".viofosync.db.migrated").exists()


def test_migrate_sidecar_copy_failure_is_non_fatal(
    tmp_path: Path, monkeypatch, caplog,
) -> None:
    """Forcing a sidecar copy to fail must not abort the migration —
    the main DB is still copied and the legacy file is still renamed."""
    rec = tmp_path / "rec"
    cfg = tmp_path / "cfg"
    rec.mkdir()
    monkeypatch.setenv("RECORDINGS", str(rec))

    legacy = rec / ".viofosync.db"
    _seed_legacy_db(legacy)
    # Make the -wal path a directory so shutil.copy2 raises
    # IsADirectoryError instead of copying a real WAL file.
    (rec / ".viofosync.db-wal").mkdir()

    new_path = cfg / "viofosync.db"
    with caplog.at_level(logging.WARNING, logger="viofosync.db"):
        migrate_legacy_db_path(str(new_path))

    assert new_path.exists(), "main DB must still be copied"
    assert (rec / ".viofosync.db.migrated").exists(), \
        "legacy file must still be renamed"
    assert any(
        "could not copy" in lr.message for lr in caplog.records
    ), "sidecar failure must be logged at WARNING"


def test_default_db_path_uses_config_dir_env(monkeypatch) -> None:
    """default_db_path resolves under CONFIG_DIR, with no leading dot."""
    from web.db import default_db_path

    monkeypatch.setenv("CONFIG_DIR", "/custom/cfg")
    assert default_db_path() == "/custom/cfg/viofosync.db"


def test_default_db_path_falls_back_to_default(monkeypatch) -> None:
    """Without CONFIG_DIR set, default_db_path returns /config/viofosync.db."""
    from web.db import default_db_path

    monkeypatch.delenv("CONFIG_DIR", raising=False)
    assert default_db_path() == "/config/viofosync.db"


def test_database_init_does_not_migrate(
    tmp_path: Path, monkeypatch,
) -> None:
    """Database() must not trigger migration even when a legacy file
    exists in RECORDINGS. This is the test-isolation guarantee that
    lets the rest of the suite use `Database(tmp_path / "x.db")`
    without risk of clobbering the host's real DB."""
    rec = tmp_path / "rec"
    rec.mkdir()
    monkeypatch.setenv("RECORDINGS", str(rec))

    legacy = rec / ".viofosync.db"
    _seed_legacy_db(legacy)
    legacy_bytes = legacy.read_bytes()

    # Construct a fresh Database somewhere completely different. The
    # legacy file must remain identical — no rename to .migrated, no
    # truncation, no reads.
    fresh = tmp_path / "fresh.db"
    Database(str(fresh))

    assert legacy.exists(), "legacy file must remain present"
    assert legacy.read_bytes() == legacy_bytes, \
        "legacy file contents must be untouched"
    assert not (rec / ".viofosync.db.migrated").exists(), \
        "legacy file must NOT have been renamed by Database()"
