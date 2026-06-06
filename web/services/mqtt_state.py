"""Pure state-extraction functions for MQTT entity values.

Every function has the signature ``(hub, db, snapshot) -> Optional[str]``
where the string is the exact MQTT payload to publish, or ``None`` to
skip publishing (the entity will appear as Unknown to HA, distinct from
Unavailable which is the LWT-driven state).

Functions read from ``hub.last_state`` (a dict updated by Hub.broadcast),
the SQLite ``Database``, and the settings ``Snapshot``. No I/O beyond
SQLite, plus whatever the retention service does to compute the
quota-aware used-% for the disk gauge.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Optional

from .sync_status import compute_sync_status


def _iso_z(ts: int) -> str:
    """ISO 8601 with explicit UTC marker — what HA's timestamp
    device_class expects."""
    return (
        _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


# ---- binary sensors

def state_dashcam(hub, db, snapshot) -> Optional[str]:
    if not snapshot.address:
        return "OFF"
    val = hub.last_state.get("dashcam_online")
    if val is None:
        return None
    return "ON" if val else "OFF"


def state_dashcam_connection(hub, db, snapshot) -> Optional[str]:
    """Which address the dashcam is reached through: ``primary`` /
    ``alternative`` / ``offline``. ``None`` (Unknown) when no address is
    configured or the camera has never been probed this run."""
    if not (snapshot.address or getattr(snapshot, "address_fallback", None)):
        return None
    online = hub.last_state.get("dashcam_online")
    if online is None:
        return None
    if not online:
        return "offline"
    return hub.last_state.get("dashcam_source") or "primary"


def attrs_dashcam_connection(hub, db, snapshot) -> Optional[dict]:
    """JSON attributes for the connection sensor — the live address."""
    return {"address": hub.last_state.get("dashcam_address")}


def state_sync_status(hub, db, snapshot) -> Optional[str]:
    """The four-state unified status string. See sync_status.py."""
    state, _reason = compute_sync_status(hub, db, snapshot)
    return state


def attrs_sync_status(hub, db, snapshot) -> Optional[dict]:
    """JSON attributes payload for the sync_status sensor. Always
    returns a dict with a ``reason`` key so HA templating doesn't have
    to guard for a missing attribute."""
    _state, reason = compute_sync_status(hub, db, snapshot)
    return {"reason": reason}


# ---- queue counts

def _queue_count(db, state: str) -> int:
    with db.conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM download_queue WHERE state=?",
            (state,),
        ).fetchone()
    return row["n"]


def state_queue_pending(hub, db, snapshot) -> Optional[str]:
    return str(_queue_count(db, "pending"))


def state_queue_failed(hub, db, snapshot) -> Optional[str]:
    return str(_queue_count(db, "failed"))


def state_queue_downloading(hub, db, snapshot) -> Optional[str]:
    return str(_queue_count(db, "downloading"))


# ---- archive

def state_last_downloaded_clip(hub, db, snapshot) -> Optional[str]:
    with db.conn() as c:
        row = c.execute(
            "SELECT MAX(timestamp) AS m FROM clip_index"
        ).fetchone()
    ts = row["m"]
    if not ts:
        return None
    return _iso_z(int(ts))


def state_total_clips(hub, db, snapshot) -> Optional[str]:
    with db.conn() as c:
        row = c.execute("SELECT COUNT(*) AS n FROM clip_index").fetchone()
    return str(row["n"])


# ---- current download

def state_current_filename(hub, db, snapshot) -> Optional[str]:
    ci = hub.last_state.get("current_item")
    if not ci:
        return None
    return ci.get("filename")


def state_current_progress(hub, db, snapshot) -> Optional[str]:
    ci = hub.last_state.get("current_item") or {}
    total = ci.get("total")
    done = ci.get("bytes")
    if not total or done is None:
        return None
    pct = round(100 * done / total, 1)
    return f"{pct}"


# Suppress publishing the session speed until the window has filled and
# the average has stabilised.
SPEED_PUBLISH_DELAY_S = 30.0


def state_download_speed(hub, db, snapshot) -> Optional[str]:
    """Session moving-average download speed in MB/s.

    Returns ``None`` (no publish) for the first ``SPEED_PUBLISH_DELAY_S``
    of a session or before the average is computable; ``"0"`` when idle.
    Combined with the entity's 60 s ``min_publish_interval_s`` this yields
    a first publish at ~30 s then at most once per 60 s.
    """
    sess = hub.last_state.get("session") or {}
    if not sess.get("active"):
        return "0"
    if (sess.get("elapsed_s") or 0) < SPEED_PUBLISH_DELAY_S:
        return None
    bps = sess.get("avg_speed_bps")
    if bps is None:
        return None
    return f"{bps / (1024 * 1024):.1f}"


# ---- disk

def state_disk_used(hub, db, snapshot) -> Optional[str]:
    """Report the higher of (filesystem %, quota %) — that's the
    rule closest to triggering retention cleanup. Reusing the
    retention service's cache means the sweep and the sensor see
    identical numbers.

    Filesystem mode (no quota set): the rule reports the underlying
    volume's used %. Quota mode (RECORDINGS_QUOTA_GB > 0): the rule
    reports bytes-under-recordings ÷ quota. Independent triggers
    (post-cherry-pick) mean both rules can be active at once; we
    publish the max so a single HA threshold alerts on either.
    """
    from . import retention as _ret
    quota = getattr(snapshot, "recordings_quota_gb", 0) or 0

    # Filesystem rule is always queryable (there's always a mounted
    # volume under recordings). Quota rule is opt-in via the setting.
    candidates = []
    pct_fs = _ret.disk_used_pct(snapshot.recordings, quota_gb=0)
    if pct_fs is not None:
        candidates.append(pct_fs)
    if quota > 0:
        pct_quota = _ret.disk_used_pct(snapshot.recordings, quota_gb=quota)
        if pct_quota is not None:
            candidates.append(pct_quota)
    if not candidates:
        return None
    return str(int(round(max(candidates))))


