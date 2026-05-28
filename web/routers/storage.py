"""Storage status — current usage against quota or filesystem.

Read-only, session-authenticated, intentionally separate from
``/api/settings`` so a tree-walk doesn't piggy-back on every settings
read. The tree walk itself is cached for 60s by ``retention.py`` so
the cost amortises across the MQTT sensor poll, the UI poll, and the
retention sweep.
"""
from __future__ import annotations

import shutil

from fastapi import APIRouter, Depends, Request

from ..auth import require_session
from ..services import retention as _ret


router = APIRouter(prefix="/api/storage", tags=["storage"],
                   dependencies=[Depends(require_session)])


@router.get("/usage")
def get_usage(request: Request) -> dict:
    """Current used-% with the same logic the retention sweep applies.

    Quota mode (``RECORDINGS_QUOTA_GB > 0``) reports the slice viofosync
    is allowed to consume; filesystem mode reports the underlying
    volume. The UI shows whichever rule actually governs the install.
    """
    snap = request.app.state.settings_provider.get()
    quota_gb = snap.recordings_quota_gb or 0

    used_bytes = 0
    total_bytes = 0
    if quota_gb > 0:
        used_bytes = _ret._cached_used_bytes(snap.recordings)
        total_bytes = quota_gb * (1 << 30)
        mode = "quota"
    else:
        try:
            du = shutil.disk_usage(snap.recordings)
            used_bytes = du.used
            total_bytes = du.total
        except (OSError, FileNotFoundError):
            used_bytes = 0
            total_bytes = 0
        mode = "filesystem"

    if total_bytes > 0:
        used_pct = round(100.0 * used_bytes / total_bytes, 1)
    else:
        used_pct = None

    return {
        "mode": mode,
        "used_bytes": used_bytes,
        "total_bytes": total_bytes,
        "used_pct": used_pct,
        "threshold_pct": snap.retention_disk_pct or None,
        "max_days": snap.retention_max_days or None,
    }
