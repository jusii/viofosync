"""Importer core tests."""
from __future__ import annotations

import os
import types
from pathlib import Path


def test_classify_event_type():
    from web.services import importer
    assert importer.classify_event_type("F", "DCIM/Movie/X.MP4") == "normal"
    assert importer.classify_event_type("PF", "DCIM/Parking/X.MP4") == "parking"
    assert importer.classify_event_type("F", "DCIM/Movie/RO/X.MP4") == "ro"
    # RO wins even for a parking-named clip living under /RO/.
    assert importer.classify_event_type("PF", "Movie/RO/X.MP4") == "ro"


def test_scan_source_recurses_and_sorts_newest_first(tmp_path: Path):
    from web.services import importer
    root = tmp_path / "card"
    (root / "DCIM" / "Movie").mkdir(parents=True)
    (root / "DCIM" / "Movie" / "RO").mkdir()
    # Recognised: one older normal front, one newer locked front.
    (root / "DCIM" / "Movie" / "2026_0101_080000_0001F.MP4").write_bytes(b"a" * 10)
    (root / "DCIM" / "Movie" / "RO" / "2026_0102_090000_0002F.MP4").write_bytes(b"b" * 20)
    # Junk: ignored + reported.
    (root / "DCIM" / "Movie" / "notes.txt").write_text("hi")
    # Matches the filename regex but has an impossible date -> bad_timestamp.
    (root / "DCIM" / "Movie" / "2026_1399_250000_0009F.MP4").write_bytes(b"c" * 5)

    manifest = importer.scan_source(str(root))
    assert manifest.total_bytes == 30
    assert [it.basename for it in manifest.items] == [
        "2026_0102_090000_0002F.MP4",   # newest first
        "2026_0101_080000_0001F.MP4",
    ]
    assert manifest.items[0].event_type == "ro"
    assert manifest.items[1].event_type == "normal"
    skipped = {s["name"]: s["reason"] for s in manifest.skipped}
    assert skipped["notes.txt"] == "not_recognised"
    assert skipped["2026_1399_250000_0009F.MP4"] == "bad_timestamp"


def _snap(rec: Path, **over):
    base = dict(
        recordings=str(rec), grouping="daily", gps_extract=False,
        retention_disk_pct=0, recordings_quota_gb=0, retention_protect_ro=True,
        retention_max_days=0, import_path="",
    )
    base.update(over)
    return types.SimpleNamespace(**base)


def _origin_rows(db):
    with db.conn() as c:
        return {r["filename"]: dict(r) for r in c.execute(
            "SELECT filename, source_dir, event_type, state, manual "
            "FROM download_queue").fetchall()}


def test_ingest_clip_places_file_and_records_origin(tmp_path: Path):
    from web.db import Database
    from web.services import importer
    rec = tmp_path / "rec"
    rec.mkdir()
    db = Database(str(rec / ".viofosync.db"))
    src = tmp_path / "card" / "RO"
    src.mkdir(parents=True)
    name = "2026_0102_090000_0002F.MP4"
    (src / name).write_bytes(b"b" * 20)

    man = importer.scan_source(str(tmp_path / "card"))
    item = man.items[0]
    res = importer.ingest_clip(db, _snap(rec), item, cross_volume=False)

    assert res.status == "imported"
    dest = rec / "2026-01-02" / name
    assert dest.exists()
    assert not (src / name).exists()              # same-volume move
    rows = _origin_rows(db)
    assert rows[name]["state"] == "done"
    assert rows[name]["manual"] == 1
    assert "/RO/" in rows[name]["source_dir"]     # RO survives via origin row
    assert rows[name]["event_type"] == "ro"


def test_ingest_clip_cross_volume_copies_and_keeps_source(tmp_path: Path):
    from web.db import Database
    from web.services import importer
    rec = tmp_path / "rec"
    rec.mkdir()
    db = Database(str(rec / ".viofosync.db"))
    name = "2026_0101_080000_0001F.MP4"
    src = tmp_path / "usb"
    src.mkdir()
    (src / name).write_bytes(b"a" * 10)
    man = importer.scan_source(str(src))
    res = importer.ingest_clip(db, _snap(rec), man.items[0], cross_volume=True)
    assert res.status == "imported"
    assert (rec / "2026-01-01" / name).exists()
    assert (src / name).exists()                  # original kept
    rows = _origin_rows(db)
    assert rows[name]["state"] == "done"
    assert rows[name]["manual"] == 1


def test_ingest_clip_skips_duplicate(tmp_path: Path):
    from web.db import Database
    from web.services import importer
    rec = tmp_path / "rec"
    rec.mkdir()
    db = Database(str(rec / ".viofosync.db"))
    name = "2026_0101_080000_0001F.MP4"
    (rec / "2026-01-01").mkdir()
    (rec / "2026-01-01" / name).write_bytes(b"existing")
    src = tmp_path / "usb"
    src.mkdir()
    (src / name).write_bytes(b"a" * 10)
    man = importer.scan_source(str(src))
    res = importer.ingest_clip(db, _snap(rec), man.items[0], cross_volume=True)
    assert res.status == "already_present"
    assert (rec / "2026-01-01" / name).read_bytes() == b"existing"


def test_ingest_clip_restores_source_when_final_rename_fails(tmp_path, monkeypatch):
    from web.db import Database
    from web.services import importer
    rec = tmp_path / "rec"
    rec.mkdir()
    db = Database(str(rec / ".viofosync.db"))
    src = tmp_path / "card"
    src.mkdir()
    name = "2026_0101_080000_0001F.MP4"
    (src / name).write_bytes(b"a" * 10)
    man = importer.scan_source(str(src))

    real_replace = os.replace
    calls = {"n": 0}
    def flaky_replace(a, b):
        calls["n"] += 1
        if calls["n"] == 2:          # 1st = src->staging, 2nd = staging->dest
            raise OSError("disk full")
        return real_replace(a, b)
    monkeypatch.setattr(importer.os, "replace", flaky_replace)

    res = importer.ingest_clip(db, _snap(rec), man.items[0], cross_volume=False)
    assert res.status == "error"
    # Source must be restored (no data loss); dest must not exist.
    assert (src / name).exists()
    assert not (rec / "2026-01-01" / name).exists()


class _FakeHub:
    def __init__(self):
        self.events = []

    def schedule_broadcast(self, loop, event):
        self.events.append(event)


def test_run_folder_ingest_imports_and_summarises(tmp_path: Path):
    from web.db import Database
    from web.services import importer
    rec = tmp_path / "rec"
    rec.mkdir()
    db = Database(str(rec / ".viofosync.db"))
    card = tmp_path / "card" / "DCIM" / "Movie"
    card.mkdir(parents=True)
    (card / "2026_0101_080000_0001F.MP4").write_bytes(b"a" * 10)
    (card / "2026_0101_080000_0001R.MP4").write_bytes(b"b" * 10)
    (card / "junk.bin").write_bytes(b"z")

    hub = _FakeHub()
    summary = importer.run_folder_ingest(
        db, _snap(rec), hub, loop=None, root=str(tmp_path / "card"),
    )
    assert summary["imported"] == 2
    assert summary["bytes_imported"] == 20  # two 10-byte clips
    assert summary["not_recognised"] == 1
    assert (rec / "2026-01-01" / "2026_0101_080000_0001F.MP4").exists()
    # clip_index was populated by the post-ingest scan.
    with db.conn() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM clip_index").fetchone()["n"]
    assert n == 2
    types_seen = {e["type"] for e in hub.events}
    assert {"import_started", "import_progress", "import_done"} <= types_seen


def test_ro_event_type_survives_rescan(tmp_path: Path):
    from web.db import Database
    from web.services import importer, scanner
    rec = tmp_path / "rec"
    rec.mkdir()
    db = Database(str(rec / ".viofosync.db"))
    card = tmp_path / "card" / "DCIM" / "Movie" / "RO"
    card.mkdir(parents=True)
    name = "2026_0101_080000_0001F.MP4"
    (card / name).write_bytes(b"a" * 10)

    importer.run_folder_ingest(
        db, _snap(rec), _FakeHub(), loop=None, root=str(tmp_path / "card"),
    )

    def _evt():
        with db.conn() as c:
            return c.execute(
                "SELECT event_type FROM clip_index WHERE basename=?",
                (name,)).fetchone()["event_type"]

    assert _evt() == "ro"
    # A second, independent full rescan must keep it 'ro'.
    scanner.scan(db, str(rec), "daily")
    assert _evt() == "ro"
