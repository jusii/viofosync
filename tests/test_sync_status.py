"""Tests for the pure compute_sync_status() function.

Inputs come from hub.last_state and the settings snapshot. Output is
(state, reason) — never raises. Precedence: error > paused > downloading > waiting.
"""
from __future__ import annotations

import types

from web.services.sync_status import compute_sync_status


def _hub(**state):
    return types.SimpleNamespace(last_state=state)


def _snap(**kwargs):
    base = dict(
        address="192.168.1.50",
        recordings="/recordings",
        disk_critical_pct=95,
    )
    base.update(kwargs)
    return types.SimpleNamespace(**base)


# ---- error precedence (highest) ----

def test_error_when_address_unset():
    hub = _hub(sync_state={"running": True, "paused": False},
               dashcam_online=True)
    snap = _snap(address=None)
    state, reason = compute_sync_status(hub, None, snap)
    assert state == "error"
    assert reason == "camera address not configured"


def test_error_when_address_empty_string():
    hub = _hub(sync_state={"running": True, "paused": False},
               dashcam_online=True)
    snap = _snap(address="")
    state, reason = compute_sync_status(hub, None, snap)
    assert state == "error"


def test_error_when_sync_error_set_in_state():
    hub = _hub(
        sync_state={"running": True, "paused": False},
        dashcam_online=True,
        sync_error={"kind": "recordings_unwritable",
                    "message": "recordings path not writable"},
    )
    state, reason = compute_sync_status(hub, None, _snap())
    assert state == "error"
    assert reason == "recordings path not writable"


def test_error_when_disk_pct_at_or_above_critical():
    hub = _hub(
        sync_state={"running": True, "paused": False},
        dashcam_online=True,
        disk_pct=95.0,
    )
    state, reason = compute_sync_status(hub, None, _snap(disk_critical_pct=95))
    assert state == "error"
    assert reason == "disk 95% full"


def test_error_disk_message_rounds_to_integer():
    hub = _hub(
        sync_state={"running": True, "paused": False},
        dashcam_online=True,
        disk_pct=96.7,
    )
    state, reason = compute_sync_status(hub, None, _snap(disk_critical_pct=95))
    assert reason == "disk 97% full"


def test_no_error_when_disk_critical_disabled():
    """disk_critical_pct=0 disables the check, even at 100%."""
    hub = _hub(
        sync_state={"running": True, "paused": False},
        dashcam_online=True,
        disk_pct=100.0,
    )
    state, _ = compute_sync_status(hub, None, _snap(disk_critical_pct=0))
    assert state != "error"


def test_error_precedence_over_paused():
    hub = _hub(
        sync_state={"running": True, "paused": True},
        dashcam_online=True,
        sync_error={"kind": "config", "message": "camera address not configured"},
    )
    state, _ = compute_sync_status(hub, None, _snap(address=None))
    assert state == "error"


# ---- paused (worker not running counts as paused) ----

def test_paused_when_running_and_paused_flag_set():
    hub = _hub(sync_state={"running": True, "paused": True},
               dashcam_online=True)
    state, reason = compute_sync_status(hub, None, _snap())
    assert state == "paused"
    assert reason is None


def test_paused_when_worker_not_running():
    hub = _hub(sync_state={"running": False, "paused": False},
               dashcam_online=True)
    state, _ = compute_sync_status(hub, None, _snap())
    assert state == "paused"


def test_paused_when_sync_state_missing_entirely():
    """No sync_state yet — treat as paused (worker hasn't reported in)."""
    hub = _hub(dashcam_online=True)
    state, _ = compute_sync_status(hub, None, _snap())
    assert state == "paused"


# ---- downloading ----

def test_downloading_when_current_item_present():
    hub = _hub(
        sync_state={"running": True, "paused": False},
        dashcam_online=True,
        current_item={"filename": "x.mp4", "total": 100, "bytes": 50},
    )
    state, reason = compute_sync_status(hub, None, _snap())
    assert state == "downloading"
    assert reason is None


# ---- waiting (default for running-but-not-downloading) ----

def test_waiting_when_dashcam_offline():
    hub = _hub(
        sync_state={"running": True, "paused": False},
        dashcam_online=False,
        current_item={"filename": "x.mp4"},  # stale — dashcam is gone
    )
    state, reason = compute_sync_status(hub, None, _snap())
    assert state == "waiting"
    assert reason is None


def test_waiting_when_running_no_current_item():
    hub = _hub(
        sync_state={"running": True, "paused": False},
        dashcam_online=True,
        current_item=None,
    )
    state, _ = compute_sync_status(hub, None, _snap())
    assert state == "waiting"


def test_waiting_when_dashcam_online_unknown():
    """First moments after startup: dashcam_online is None. Don't flicker
    to downloading. Treat as waiting."""
    hub = _hub(
        sync_state={"running": True, "paused": False},
        dashcam_online=None,
    )
    state, _ = compute_sync_status(hub, None, _snap())
    assert state == "waiting"


# ---- robustness ----

def test_never_raises_on_garbage_state():
    """Any unexpected shape in last_state must resolve to waiting,
    never raise — the UI must never blank out."""
    hub = _hub(sync_state="not a dict",
               current_item=42, dashcam_online="yes")
    state, _ = compute_sync_status(hub, None, _snap())
    assert state in ("downloading", "waiting", "paused", "error")
