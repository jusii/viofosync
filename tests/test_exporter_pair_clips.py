"""Tests for ExportWorker._pair_clips slot dispatch.

The pairer groups same-capture clips by (timestamp, event_type)
and assigns each clip a slot from its trailing camera letter:
F → front, T → tele, I → interior, everything else → rear.
``required`` names the slots a group must have to count — PiP
front+rear needs both classic slots; pip_tele / pip_interior
need front plus the third camera.
"""
from __future__ import annotations

from web.services.exporter import ExportWorker


def _clip(camera: str, ts: int = 1000, event: str = "normal") -> dict:
    return {
        "camera": camera,
        "timestamp": ts,
        "event_type": event,
        "path": f"/x/{ts}_{camera}.MP4",
    }


def test_front_rear_pairing_unchanged():
    pairs = ExportWorker._pair_clips(
        [_clip("F"), _clip("R")],
    )
    assert len(pairs) == 1
    (_, p), = pairs
    assert p["front"]["camera"] == "F"
    assert p["rear"]["camera"] == "R"


def test_triplet_keeps_all_three_slots():
    pairs = ExportWorker._pair_clips(
        [_clip("F"), _clip("R"), _clip("T")],
    )
    (_, p), = pairs
    assert set(p) == {"front", "rear", "tele"}


def test_front_tele_pair_for_pip_tele():
    # A front+tele selection has no rear — the default
    # front+rear requirement drops it, the pip_tele one keeps it.
    clips = [_clip("F"), _clip("T")]
    assert ExportWorker._pair_clips(clips) == []
    pairs = ExportWorker._pair_clips(
        clips, required=("front", "tele"),
    )
    assert len(pairs) == 1


def test_front_interior_pair_for_pip_interior():
    clips = [_clip("F"), _clip("I")]
    pairs = ExportWorker._pair_clips(
        clips, required=("front", "interior"),
    )
    assert len(pairs) == 1
    (_, p), = pairs
    assert p["interior"]["camera"] == "I"


def test_tele_only_never_pairs():
    assert ExportWorker._pair_clips(
        [_clip("T")], required=("front", "tele"),
    ) == []


def test_parking_prefixes_assign_correct_slots():
    pairs = ExportWorker._pair_clips(
        [_clip("PF", event="parking"), _clip("PT", event="parking")],
        required=("front", "tele"),
    )
    (_, p), = pairs
    assert p["front"]["camera"] == "PF"
    assert p["tele"]["camera"] == "PT"


def test_rear_tele_without_front_never_pairs():
    assert ExportWorker._pair_clips(
        [_clip("R"), _clip("T")], required=("front", "tele"),
    ) == []
