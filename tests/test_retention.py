"""Retention sweep tests — eligibility logic + integration."""
from __future__ import annotations

import time as _time
from collections import namedtuple
from pathlib import Path

import pytest

from web.db import Database
from web.services.retention import _eligible_by_time, sweep


def _clip(*, ts: int, event_type: str = "normal") -> dict:
    return {
        "id": 1,
        "path": "/x.MP4",
        "timestamp": ts,
        "event_type": event_type,
    }


def test_eligible_by_time_max_days_zero_returns_false() -> None:
    clip = _clip(ts=0)  # ancient, but max_days disabled
    ok, reason = _eligible_by_time(
        clip, now=1_000_000, max_days=0, protect_ro=True,
    )
    assert ok is False
    assert reason == "kept"


def test_eligible_by_time_old_clip_returns_true() -> None:
    clip = _clip(ts=0)
    ok, reason = _eligible_by_time(
        clip, now=86400 * 31, max_days=30, protect_ro=False,
    )
    assert ok is True
    assert reason == "time"


def test_eligible_by_time_recent_clip_returns_false() -> None:
    now = 86400 * 31
    clip = _clip(ts=now - 86400 * 5)  # 5 days old, cap is 30
    ok, reason = _eligible_by_time(
        clip, now=now, max_days=30, protect_ro=False,
    )
    assert ok is False
    assert reason == "kept"


def test_eligible_by_time_ro_protected_blocks_deletion() -> None:
    clip = _clip(ts=0, event_type="ro")
    ok, reason = _eligible_by_time(
        clip, now=86400 * 365, max_days=30, protect_ro=True,
    )
    assert ok is False
    assert reason == "ro_protected"


def test_eligible_by_time_ro_unprotected_can_be_deleted() -> None:
    clip = _clip(ts=0, event_type="ro")
    ok, reason = _eligible_by_time(
        clip, now=86400 * 365, max_days=30, protect_ro=False,
    )
    assert ok is True
    assert reason == "time"


@pytest.fixture
def env(tmp_path: Path):
    """Recordings dir + db with a clean clip_index."""
    rec = tmp_path / "rec"
    rec.mkdir()
    db = Database(str(rec / ".viofosync.db"))
    return rec, db


def _make_clip(
    rec: Path, db: Database, *,
    basename: str, ts: int, event_type: str = "normal",
    with_gpx: bool = True,
) -> int:
    """Create a fake clip on disk + clip_index row. Returns id."""
    day = _time.strftime("%Y-%m-%d", _time.gmtime(ts))
    folder = rec / day
    folder.mkdir(exist_ok=True)
    path = folder / basename
    path.write_bytes(b"x" * 1024)
    if with_gpx:
        (folder / (basename + ".gpx")).write_text("<gpx/>")
    with db.write() as c:
        cur = c.execute(
            "INSERT INTO clip_index "
            "(path, basename, group_name, timestamp, camera, sequence, "
            " event_type, size_bytes, has_gpx, scanned_at) "
            "VALUES (?, ?, ?, ?, 'F', 1, ?, ?, ?, ?)",
            (
                str(path), basename, day, ts, event_type,
                1024, 1 if with_gpx else 0, int(_time.time()),
            ),
        )
        return cur.lastrowid


def _index_count(db: Database) -> int:
    with db.conn() as c:
        return c.execute(
            "SELECT COUNT(*) AS n FROM clip_index"
        ).fetchone()["n"]


def test_sweep_time_deletes_old_clip(env) -> None:
    rec, db = env
    now = 86400 * 365
    _make_clip(rec, db, basename="OLD.MP4", ts=0)
    _make_clip(rec, db, basename="NEW.MP4", ts=now - 3600)
    summary = sweep(
        db, str(rec), max_days=30, disk_pct=0,
        protect_ro=True, _now=now,
    )
    assert summary["deleted_time"] == 1
    assert summary["deleted_disk"] == 0
    assert _index_count(db) == 1
    assert not (rec / "1970-01-01" / "OLD.MP4").exists()
    assert not (rec / "1970-01-01" / "OLD.MP4.gpx").exists()


def test_sweep_time_protects_ro_when_enabled(env) -> None:
    rec, db = env
    now = 86400 * 365
    _make_clip(rec, db, basename="LOCK.MP4", ts=0, event_type="ro")
    summary = sweep(
        db, str(rec), max_days=30, disk_pct=0,
        protect_ro=True, _now=now,
    )
    assert summary["deleted_time"] == 0
    assert summary["protected"] == 1
    assert _index_count(db) == 1


def test_sweep_time_max_days_zero_is_noop(env) -> None:
    rec, db = env
    _make_clip(rec, db, basename="OLD.MP4", ts=0)
    summary = sweep(
        db, str(rec), max_days=0, disk_pct=0,
        protect_ro=True, _now=86400 * 365,
    )
    assert summary["deleted_time"] == 0
    assert _index_count(db) == 1


def test_sweep_time_removes_empty_group_dir(env) -> None:
    rec, db = env
    now = 86400 * 365
    _make_clip(rec, db, basename="OLD.MP4", ts=0)
    sweep(
        db, str(rec), max_days=30, disk_pct=0,
        protect_ro=False, _now=now,
    )
    assert not (rec / "1970-01-01").exists()


DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])


def test_sweep_disk_deletes_oldest_first_until_under(env, monkeypatch) -> None:
    rec, db = env
    # Three clips, oldest first.
    _make_clip(rec, db, basename="A.MP4", ts=100)
    _make_clip(rec, db, basename="B.MP4", ts=200)
    _make_clip(rec, db, basename="C.MP4", ts=300)

    # Fake disk usage: starts above threshold, drops as clips
    # disappear (one per delete in this stub).
    state = {"used_pct": 95}

    def fake_disk_usage(path):
        used = state["used_pct"]
        return DiskUsage(total=100, used=used, free=100 - used)

    def shrink_after_delete():
        state["used_pct"] -= 10

    # Patch shutil.disk_usage in the module under test.
    monkeypatch.setattr(
        "web.services.retention.shutil.disk_usage", fake_disk_usage,
    )
    # Wrap _delete_clip_files to also shrink the fake disk.
    import web.services.retention as ret
    orig = ret._delete_clip_files
    def wrapped(*a, **kw):
        out = orig(*a, **kw)
        shrink_after_delete()
        return out
    monkeypatch.setattr(ret, "_delete_clip_files", wrapped)

    summary = sweep(
        db, str(rec), max_days=0, disk_pct=80,
        protect_ro=True, _now=86400 * 365,
    )
    # 95 -> 85 -> 75 (under 80) — should stop after 2 deletes.
    assert summary["deleted_disk"] == 2
    assert _index_count(db) == 1
    # Confirm the remaining clip is the newest one.
    with db.conn() as c:
        remaining = c.execute(
            "SELECT basename FROM clip_index"
        ).fetchone()["basename"]
    assert remaining == "C.MP4"


def test_sweep_disk_below_threshold_is_noop(env, monkeypatch) -> None:
    rec, db = env
    _make_clip(rec, db, basename="A.MP4", ts=100)

    monkeypatch.setattr(
        "web.services.retention.shutil.disk_usage",
        lambda p: DiskUsage(total=100, used=50, free=50),
    )
    summary = sweep(
        db, str(rec), max_days=0, disk_pct=80,
        protect_ro=True, _now=86400 * 365,
    )
    assert summary["deleted_disk"] == 0
    assert _index_count(db) == 1


def test_sweep_disk_skips_ro_when_protected(env, monkeypatch) -> None:
    rec, db = env
    _make_clip(rec, db, basename="LOCK.MP4", ts=100, event_type="ro")
    _make_clip(rec, db, basename="DRIVE.MP4", ts=200)

    # Pretend disk stays full — the only deletable target is DRIVE.
    monkeypatch.setattr(
        "web.services.retention.shutil.disk_usage",
        lambda p: DiskUsage(total=100, used=99, free=1),
    )
    # Run with disk_pct=80 and protect_ro=True; loop should run
    # exactly once (deletes DRIVE) then bail out because the only
    # remaining row is RO and ineligible.
    summary = sweep(
        db, str(rec), max_days=0, disk_pct=80,
        protect_ro=True, _now=86400 * 365,
    )
    assert summary["deleted_disk"] == 1
    # Disk stayed full and one RO clip survived; report it.
    assert summary["protected"] == 1
    with db.conn() as c:
        remaining = c.execute(
            "SELECT basename FROM clip_index"
        ).fetchone()["basename"]
    assert remaining == "LOCK.MP4"


def test_sweep_logs_examining_header_when_rows_exist(env, caplog) -> None:
    """When there are eligible-by-time clips, sweep logs a single
    'retention sweep: N clip(s) older than D days — examining' header
    at INFO before starting deletions. Says 'examining' rather than
    'deleting' because RO-protected rows are filtered inside the
    loop, so N is the candidate count, not the eventual delete count."""
    import logging
    rec, db = env
    now = 86400 * 365
    _make_clip(rec, db, basename="A.MP4", ts=0)
    _make_clip(rec, db, basename="B.MP4", ts=100)
    _make_clip(rec, db, basename="C.MP4", ts=200)

    with caplog.at_level(logging.INFO, logger="viofosync.retention"):
        sweep(
            db, str(rec), max_days=30, disk_pct=0,
            protect_ro=False, _now=now,
        )

    headers = [
        r.message for r in caplog.records
        if "older than" in r.message and "examining" in r.message
    ]
    assert len(headers) == 1, f"expected one header log, got {headers}"
    assert "3 clip(s) older than 30 days" in headers[0]


def test_sweep_silent_when_no_eligible_clips(env, caplog) -> None:
    """Steady-state: no eligible clips, no log noise."""
    import logging
    rec, db = env
    now = 86400 * 365
    _make_clip(rec, db, basename="NEW.MP4", ts=now - 3600)

    with caplog.at_level(logging.INFO, logger="viofosync.retention"):
        sweep(
            db, str(rec), max_days=30, disk_pct=0,
            protect_ro=False, _now=now,
        )

    assert caplog.records == [], \
        f"sweep with no eligible clips must not log, got {[r.message for r in caplog.records]}"


def test_sweep_logs_progress_every_10_deletions(env, caplog) -> None:
    """A long time-phase sweep emits an INFO progress line at every
    10th deletion, formatted with running count and MB freed."""
    import logging
    rec, db = env
    now = 86400 * 365
    # 25 eligible-by-time clips. Expect progress lines at the 10th
    # and 20th deletions — two lines total.
    for i in range(25):
        _make_clip(rec, db, basename=f"OLD-{i:02d}.MP4", ts=i)

    with caplog.at_level(logging.INFO, logger="viofosync.retention"):
        sweep(
            db, str(rec), max_days=30, disk_pct=0,
            protect_ro=False, _now=now,
        )

    progress = [
        r.message for r in caplog.records
        if "/25 clip(s) deleted" in r.message
    ]
    assert len(progress) == 2, \
        f"expected two progress lines, got {progress}"
    assert progress[0].startswith("retention sweep: 10/25 clip(s) deleted")
    assert progress[1].startswith("retention sweep: 20/25 clip(s) deleted")
    assert "MB freed so far" in progress[0]
