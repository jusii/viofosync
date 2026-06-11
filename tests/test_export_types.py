"""Validation gates for the third-camera export job types.

Two layers must both accept a type before a job can run: the
route's pydantic pattern (web/routers/exports.py) and the
worker's enqueue() allowlist (web/services/exporter.py). A type
listed in one but not the other 422s or errors at runtime — pin
them together.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from web.db import Database
from web.routers.exports import CreateExport, Segment
from web.services.exporter import ExportWorker

NEW_TYPES = ("join_tele", "join_interior", "pip_tele", "pip_interior")
OLD_TYPES = ("join_front", "join_rear", "pip", "pip_rear")


def test_route_model_accepts_all_types():
    for t in OLD_TYPES + NEW_TYPES + ("timeline",):
        assert CreateExport(type=t).type == t


def test_route_model_rejects_unknown_type():
    with pytest.raises(ValidationError):
        CreateExport(type="join_bogus")


def test_segment_accepts_tele_and_interior_channels():
    for ch in ("front", "rear", "tele", "interior", "other"):
        seg = Segment(channel=ch, start_ts=0.0, end_ts=1.0)
        assert seg.channel == ch
    with pytest.raises(ValidationError):
        Segment(channel="bogus", start_ts=0.0, end_ts=1.0)


def test_enqueue_allowlist_accepts_new_types(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(
        "web.services.exporter.ffmpeg_available", lambda: True,
    )
    db = Database(str(tmp_path / "test.db"))
    worker = ExportWorker(
        db=db, provider=MagicMock(), broadcast=MagicMock(),
    )
    for t in OLD_TYPES + NEW_TYPES:
        job_id = worker.enqueue(t, [1, 2])
        assert isinstance(job_id, int)
    with pytest.raises(ValueError):
        worker.enqueue("join_bogus", [1])
