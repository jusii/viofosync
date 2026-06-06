"""Single source of truth for the four-state sync status.

State precedence (highest wins): error > paused > downloading > waiting.

Inputs come from ``hub.last_state`` (populated by the Hub.broadcast
side-effects in ``hub.py``) and from the settings ``Snapshot``.

The function is pure and total — it never raises. Any unexpected
``last_state`` shape resolves to ``waiting`` so the UI cannot blank out.
"""
from __future__ import annotations

from typing import Any, Optional, Tuple


STATES = ("downloading", "waiting", "paused", "error")


def compute_sync_status(hub, db, snapshot) -> Tuple[str, Optional[str]]:
    """Return ``(state, reason)``.

    ``state`` is one of ``STATES``. ``reason`` is a short human-readable
    string when ``state == "error"``, else ``None``.

    ``db`` is accepted for signature symmetry with ``state_fn`` callers
    in mqtt_state.py; not used today.
    """
    try:
        return _compute(hub, snapshot)
    except Exception:
        # Defensive: any unexpected exception falls back to waiting.
        # The UI is rendered from this value; never let it crash.
        return "waiting", None


def _compute(hub, snapshot) -> Tuple[str, Optional[str]]:
    last = getattr(hub, "last_state", None) or {}

    # ---- error tier (highest precedence) ----

    # Missing required config — sticky, takes priority over everything.
    if not getattr(snapshot, "address", None):
        return "error", "camera address not configured"

    # Stateful sync_error captured by the worker (recordings unwritable,
    # auth failure, etc.). Use its message verbatim as the reason.
    sync_error = last.get("sync_error")
    if isinstance(sync_error, dict):
        msg = sync_error.get("message") or "sync error"
        return "error", str(msg)

    # Disk-pressure error. ``disk_critical_pct == 0`` disables the check.
    critical = int(getattr(snapshot, "disk_critical_pct", 0) or 0)
    disk_pct = last.get("disk_pct")
    if critical > 0 and isinstance(disk_pct, (int, float)) and disk_pct >= critical:
        return "error", f"disk {round(disk_pct)}% full"

    # ---- paused tier ----

    sync_state = last.get("sync_state")
    if not isinstance(sync_state, dict):
        # Worker hasn't reported in yet — show paused rather than waiting,
        # because nothing is happening from the user's perspective.
        return "paused", None
    if not sync_state.get("running"):
        return "paused", None
    if sync_state.get("paused"):
        return "paused", None

    # ---- downloading ----

    current_item = last.get("current_item")
    dashcam_online = last.get("dashcam_online")
    if current_item and dashcam_online is True:
        return "downloading", None

    # ---- waiting (default for running-but-not-downloading) ----

    return "waiting", None
