"""Camera facade for the web layer + export filename derivation.

Pure functions — no DB or HTTP. Two roles:

1. The web app's view of the camera registry
   (viofosync_lib/cameras.py): re-exports plus the derived
   per-camera export job types (``join_<channel>``, ``pip``/
   ``pip_<channel>``) with their letter/partner/main tables, and
   the timeline channel order/labels.

2. Download filenames for joined/PiP exports: ``build_basename``
   turns a set of clips plus a camera label into a stem like
   ``2024-03-15_1430-1502_front_4clips``; ``export_download_name``
   maps an export job type to a label and appends ``.mp4``,
   falling back to the legacy ``viofosync_export_{id}.mp4`` when
   the source clips are gone (retention) or the type is unknown.
   (Original, un-joined clips keep their dashcam basenames — they
   don't go through this module.) Timestamps are unix seconds
   formatted in local time, matching how the archive UI renders
   clip times (web/routers/archive.py).
"""
from __future__ import annotations

import datetime as _dt
import json as _json
from typing import List

from viofosync_lib.cameras import (  # noqa: F401 — re-exported
    CAMERAS,
    channel_of,
    pair_slot_of,
)

# --- Per-camera export job types, derived from the registry ----------
#
# Join types exist for every camera (``join_front`` … ``join_interior``).
# PiP types pair the front camera with one partner: the legacy ``pip``
# is front-main + rear inset; ``pip_<channel>`` makes the partner
# fullscreen with the front inset. Adding a camera in
# viofosync_lib/cameras.py extends all of these automatically.

JOIN_LETTER_FOR_TYPE = {
    f"join_{c.channel}": c.letter for c in CAMERAS
}

_PARTNERS = [c.channel for c in CAMERAS if c.channel != "front"]

# job type -> the non-front slot it pairs with
PIP_PARTNER_FOR_TYPE = {"pip": "rear"} | {
    f"pip_{ch}": ch for ch in _PARTNERS
}

# job type -> which side is fullscreen
PIP_MAIN_FOR_TYPE = {"pip": "front"} | {
    f"pip_{ch}": ch for ch in _PARTNERS
}

# Everything enqueue()/the route accept, except "timeline" which has
# its own entry point.
EXPORT_JOB_TYPES = (*JOIN_LETTER_FOR_TYPE, *PIP_PARTNER_FOR_TYPE)

# Export job type -> camera label used in the filename.
LABEL_FOR_TYPE = {
    f"join_{c.channel}": c.channel for c in CAMERAS
} | {"pip": "pip-front"} | {
    f"pip_{ch}": f"pip-{ch}" for ch in _PARTNERS
}


def build_basename(clips: List[dict], label: str) -> str:
    """Stem (no extension) for a set of clips and a camera label.

    Same day  -> ``2024-03-15_1430-1502_front_4clips``
    One clip  -> ``2024-03-15_1430_front_1clip`` (range collapses)
    Spans days-> ``2024-03-15_to_2024-03-17_front_12clips`` (no times)
    """
    times = sorted(
        _dt.datetime.fromtimestamp(c["timestamp"]) for c in clips
    )
    start, end = times[0], times[-1]
    n = len(times)
    count = f"{n}clip" if n == 1 else f"{n}clips"

    if start.date() == end.date():
        day = start.strftime("%Y-%m-%d")
        if start.strftime("%H%M") == end.strftime("%H%M"):
            stamp = f"{day}_{start.strftime('%H%M')}"
        else:
            stamp = (
                f"{day}_{start.strftime('%H%M')}-{end.strftime('%H%M')}"
            )
    else:
        stamp = (
            f"{start.strftime('%Y-%m-%d')}_to_{end.strftime('%Y-%m-%d')}"
        )
    return f"{stamp}_{label}_{count}"


def parse_clip_ids(raw: str) -> List[int]:
    """Read the export_jobs.clip_ids JSON column, which is either a
    bare list (legacy) or ``{"clip_ids": [...], "encoder": ...}``.

    Best-effort: returns ``[]`` on bad JSON, an unexpected shape, or
    non-integer ids rather than raising — the download path that
    relies on it degrades to the legacy filename instead of 500ing.
    """
    try:
        data = _json.loads(raw)
        if isinstance(data, dict):
            data = data.get("clip_ids", [])
        if not isinstance(data, list):
            return []
        return [int(x) for x in data]
    except (ValueError, TypeError):
        return []


def export_download_name(
    job_type: str, clips: List[dict], job_id: int
) -> str:
    """Filename for an export download. Best-effort: falls back to
    the legacy name when there's nothing to derive from."""
    label = LABEL_FOR_TYPE.get(job_type)
    if not label or not clips:
        return f"viofosync_export_{job_id}.mp4"
    return f"{build_basename(clips, label)}.mp4"


# --- Timeline camera channels -------------------------------------------

# Channel keys/labels come straight from the registry; "other" is the
# fallback channel_of() uses for unrecognised codes. channel_of itself
# lives in viofosync_lib.cameras and is re-exported above.
CHANNEL_ORDER = [c.channel for c in CAMERAS] + ["other"]
CHANNEL_LABELS = {c.channel: c.label for c in CAMERAS} | {
    "other": "Other",
}
