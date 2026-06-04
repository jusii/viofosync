"""Hub feeds the DownloadSession tracker and emits session_stats."""
from __future__ import annotations

from web.services.download_session import DownloadSession
from web.services.hub import Hub


class _Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


class _RecordingWS:
    def __init__(self):
        self.sent = []

    async def accept(self):
        pass

    async def send_json(self, payload):
        self.sent.append(payload)


def _hub_with_session(remaining=1000, clock=None):
    c = clock or _Clock()
    sess = DownloadSession(lambda: remaining, monotonic=c, window_s=30.0)
    return Hub(session=sess), c


async def test_initial_last_state_has_idle_session():
    hub = Hub()
    assert "session" in hub.last_state
    assert hub.last_state["session"]["active"] is False


async def test_progress_drives_session_stats_event():
    hub, c = _hub_with_session()
    ws = _RecordingWS()
    await hub.connect(ws)
    ws.sent.clear()
    await hub.broadcast({"type": "item_started", "filename": "a.mp4",
                         "total": 10_000})
    c.t = 3.0
    await hub.broadcast({"type": "item_progress", "filename": "a.mp4",
                         "bytes": 3000, "total": 10_000})
    c.t = 6.0
    await hub.broadcast({"type": "item_progress", "filename": "a.mp4",
                         "bytes": 6000, "total": 10_000})
    stats = [e for e in ws.sent if e.get("type") == "session_stats"]
    assert stats, "expected at least one session_stats event"
    assert stats[-1]["active"] is True
    assert stats[-1]["avg_speed_bps"] is not None
    assert hub.last_state["session"]["active"] is True


async def test_sync_done_emits_idle_session_stats():
    hub, c = _hub_with_session()
    ws = _RecordingWS()
    await hub.connect(ws)
    await hub.broadcast({"type": "item_started", "filename": "a.mp4",
                         "total": 10_000})
    c.t = 3.0
    await hub.broadcast({"type": "item_progress", "filename": "a.mp4",
                         "bytes": 3000, "total": 10_000})
    ws.sent.clear()
    await hub.broadcast({"type": "sync_done", "ok": True})
    assert hub.last_state["session"]["active"] is False
    idle = [e for e in ws.sent if e.get("type") == "session_stats"]
    assert idle and idle[-1]["active"] is False


async def test_running_sync_state_does_not_reset_session():
    """sync_state with running=True fires every time the worker picks an
    item — it must NOT idle an in-flight session."""
    hub, c = _hub_with_session()
    ws = _RecordingWS()
    await hub.connect(ws)
    await hub.broadcast({"type": "item_started", "filename": "a.mp4",
                         "total": 10_000})
    c.t = 3.0
    await hub.broadcast({"type": "item_progress", "filename": "a.mp4",
                         "bytes": 3000, "total": 10_000})
    await hub.broadcast({"type": "sync_state", "running": True,
                         "paused": False})
    assert hub.last_state["session"]["active"] is True


async def test_paused_sync_state_resets_session():
    hub, c = _hub_with_session()
    ws = _RecordingWS()
    await hub.connect(ws)
    await hub.broadcast({"type": "item_started", "filename": "a.mp4",
                         "total": 10_000})
    c.t = 3.0
    await hub.broadcast({"type": "item_progress", "filename": "a.mp4",
                         "bytes": 3000, "total": 10_000})
    await hub.broadcast({"type": "sync_state", "running": True,
                         "paused": True})
    assert hub.last_state["session"]["active"] is False


async def test_session_stats_deduped_when_rounded_view_unchanged():
    hub, c = _hub_with_session()
    ws = _RecordingWS()
    await hub.connect(ws)
    # Get into a steady active state with a computed speed.
    await hub.broadcast({"type": "item_started", "filename": "a.mp4",
                         "total": 10_000})
    c.t = 3.0
    await hub.broadcast({"type": "item_progress", "filename": "a.mp4",
                         "bytes": 3000, "total": 10_000})
    c.t = 6.0
    await hub.broadcast({"type": "item_progress", "filename": "a.mp4",
                         "bytes": 6000, "total": 10_000})
    ws.sent.clear()
    # An unrelated event that doesn't advance the clock or bytes: the
    # rounded session view is identical → no duplicate session_stats.
    await hub.broadcast({"type": "dashcam_online"})
    dup = [e for e in ws.sent if e.get("type") == "session_stats"]
    assert dup == []


async def test_snapshot_carries_session():
    hub, c = _hub_with_session()
    await hub.broadcast({"type": "item_started", "filename": "a.mp4",
                         "total": 10_000})
    c.t = 3.0
    await hub.broadcast({"type": "item_progress", "filename": "a.mp4",
                         "bytes": 3000, "total": 10_000})
    ws = _RecordingWS()
    await hub.connect(ws)
    snap = ws.sent[0]
    assert snap["type"] == "snapshot"
    assert snap["state"]["session"]["active"] is True


async def test_no_session_tracker_is_safe():
    """Hub with session=None must not emit session_stats or raise."""
    hub = Hub()  # no session
    ws = _RecordingWS()
    await hub.connect(ws)
    ws.sent.clear()
    await hub.broadcast({"type": "item_started", "filename": "a.mp4",
                         "total": 10_000})
    assert not any(e.get("type") == "session_stats" for e in ws.sent)


async def test_active_session_emits_heartbeat_on_elapsed_change():
    """Even with steady speed/eta, advancing the clock during an active
    session must still emit a session_stats (the ~1/s heartbeat that keeps
    MQTT triggered)."""
    c = _Clock()
    # Constant remaining + linear progress → speed stays flat.
    hub = Hub(session=DownloadSession(lambda: 1000, monotonic=c, window_s=30.0))
    ws = _RecordingWS()
    await hub.connect(ws)
    await hub.broadcast({"type": "item_started", "filename": "a.mp4",
                         "total": 1_000_000})
    c.t = 3.0
    await hub.broadcast({"type": "item_progress", "filename": "a.mp4",
                         "bytes": 3000, "total": 1_000_000})
    c.t = 6.0
    await hub.broadcast({"type": "item_progress", "filename": "a.mp4",
                         "bytes": 6000, "total": 1_000_000})
    ws.sent.clear()
    # Advance the clock by ~1s; broadcast an unrelated event. elapsed
    # changed → heartbeat session_stats expected.
    c.t = 7.0
    await hub.broadcast({"type": "dashcam_online"})
    beats = [e for e in ws.sent if e.get("type") == "session_stats"]
    assert beats, "expected a heartbeat session_stats after elapsed advanced"


async def test_dashcam_offline_resets_session():
    """A mid-download camera drop must idle the session so the UI line and
    HA sensor don't freeze on a stale speed."""
    hub, c = _hub_with_session()
    ws = _RecordingWS()
    await hub.connect(ws)
    await hub.broadcast({"type": "item_started", "filename": "a.mp4",
                         "total": 10_000})
    c.t = 3.0
    await hub.broadcast({"type": "item_progress", "filename": "a.mp4",
                         "bytes": 3000, "total": 10_000})
    assert hub.last_state["session"]["active"] is True
    await hub.broadcast({"type": "dashcam_offline"})
    assert hub.last_state["session"]["active"] is False
