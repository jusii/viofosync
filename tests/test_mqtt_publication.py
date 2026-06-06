"""Publication logic: change detection + coalescing."""
from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_first_publish_emitted(monkeypatch):
    from web.services.mqtt import PublishCoalescer
    pc = PublishCoalescer(monotonic=lambda: 0.0)
    sent = []
    async def sink(topic, payload, retain, qos):
        sent.append((topic, payload, retain, qos))
    await pc.consider("a/state", b"42", min_interval=1.0, sink=sink,
                      retain=True, qos=1)
    assert sent == [("a/state", b"42", True, 1)]


@pytest.mark.asyncio
async def test_unchanged_payload_suppressed(monkeypatch):
    from web.services.mqtt import PublishCoalescer
    now = [0.0]
    pc = PublishCoalescer(monotonic=lambda: now[0])
    sent = []
    async def sink(topic, payload, retain, qos):
        sent.append((topic, payload))
    await pc.consider("a/state", b"42", min_interval=0.0,
                      sink=sink, retain=True, qos=1)
    now[0] = 10.0  # well past any interval
    await pc.consider("a/state", b"42", min_interval=0.0,
                      sink=sink, retain=True, qos=1)
    assert sent == [("a/state", b"42")]


@pytest.mark.asyncio
async def test_changed_payload_emitted_after_interval(monkeypatch):
    from web.services.mqtt import PublishCoalescer
    now = [0.0]
    pc = PublishCoalescer(monotonic=lambda: now[0])
    sent = []
    async def sink(topic, payload, retain, qos):
        sent.append((topic, payload))

    await pc.consider("a/state", b"1", min_interval=2.0,
                      sink=sink, retain=True, qos=1)
    now[0] = 0.5
    await pc.consider("a/state", b"2", min_interval=2.0,
                      sink=sink, retain=True, qos=1)
    # Within the interval — should NOT have fired the second publish yet.
    # But the value is now pending.
    assert sent == [("a/state", b"1")]

    # When the interval elapses, the deadline-flush yields the latest value.
    now[0] = 2.5
    await pc.flush_due(sink)
    assert sent == [("a/state", b"1"), ("a/state", b"2")]


@pytest.mark.asyncio
async def test_intermediate_frames_dropped(monkeypatch):
    from web.services.mqtt import PublishCoalescer
    now = [0.0]
    pc = PublishCoalescer(monotonic=lambda: now[0])
    sent = []
    async def sink(topic, payload, retain, qos):
        sent.append(payload)

    await pc.consider("a", b"1", min_interval=5.0,
                      sink=sink, retain=False, qos=1)
    now[0] = 1.0
    await pc.consider("a", b"2", min_interval=5.0,
                      sink=sink, retain=False, qos=1)
    now[0] = 2.0
    await pc.consider("a", b"3", min_interval=5.0,
                      sink=sink, retain=False, qos=1)
    now[0] = 6.0
    await pc.flush_due(sink)
    # Only the first and the final value should have been sent.
    assert sent == [b"1", b"3"]


@pytest.mark.asyncio
async def test_flush_due_does_nothing_when_no_pending(monkeypatch):
    from web.services.mqtt import PublishCoalescer
    pc = PublishCoalescer(monotonic=lambda: 0.0)
    sent = []
    async def sink(*a, **kw):
        sent.append(1)
    await pc.flush_due(sink)
    assert sent == []


@pytest.mark.asyncio
async def test_revert_to_published_value_cancels_pending():
    """If a stashed-pending value is overwritten by the originally-published
    value, the pending entry should be cancelled (no redundant publish on
    flush_due)."""
    from web.services.mqtt import PublishCoalescer
    now = [0.0]
    pc = PublishCoalescer(monotonic=lambda: now[0])
    sent = []
    async def sink(topic, payload, retain, qos):
        sent.append(payload)

    await pc.consider("a", b"1", min_interval=5.0,
                      sink=sink, retain=False, qos=1)
    now[0] = 1.0
    await pc.consider("a", b"2", min_interval=5.0,
                      sink=sink, retain=False, qos=1)
    now[0] = 2.0
    # Revert to the originally-published value while still inside the
    # cooldown — should cancel pending and emit nothing.
    await pc.consider("a", b"1", min_interval=5.0,
                      sink=sink, retain=False, qos=1)
    now[0] = 6.0
    await pc.flush_due(sink)
    assert sent == [b"1"]  # only the original publish


@pytest.mark.asyncio
async def test_flush_due_survives_concurrent_consider_popping_pending():
    """Race regression: flush_due was using `del self._pending[topic]`
    after `await sink(...)`. A concurrent `consider()` whose payload
    equals the still-unchanged `_last_payload[topic]` pops the same
    entry during the yield, and the trailing del raises KeyError —
    crashing the tick task and triggering an MQTT reconnect.
    """
    from web.services.mqtt import PublishCoalescer
    now = [0.0]
    pc = PublishCoalescer(monotonic=lambda: now[0])

    # Get topic X into "published once" state with last_payload = b"A".
    sent: list[tuple[str, bytes]] = []
    async def fast_sink(topic, payload, retain, qos):
        sent.append((topic, payload))
    await pc.consider("X", b"A", min_interval=5.0,
                      sink=fast_sink, retain=True, qos=1)

    # Within cooldown: stash a new pending value b"B".
    now[0] = 1.0
    await pc.consider("X", b"B", min_interval=5.0,
                      sink=fast_sink, retain=True, qos=1)

    # Cooldown elapsed. Trigger flush_due against a sink that blocks
    # so we can interleave another consider() call mid-publish.
    now[0] = 10.0
    publish_started = asyncio.Event()
    publish_release = asyncio.Event()

    async def slow_sink(topic, payload, retain, qos):
        publish_started.set()
        await publish_release.wait()
        sent.append((topic, payload))

    flush_task = asyncio.create_task(pc.flush_due(slow_sink))
    await publish_started.wait()  # flush_due is now mid-await

    # During the yield, _last_payload[X] is still b"A" (flush_due hasn't
    # updated it yet). A consider() with payload b"A" therefore matches
    # the first branch of consider and pops _pending[X].
    await pc.consider("X", b"A", min_interval=5.0,
                      sink=fast_sink, retain=True, qos=1)

    publish_release.set()
    # Pre-fix this raised KeyError: 'X' from the trailing `del`.
    await flush_task

    # The flushed b"B" must still have been recorded as the latest
    # published payload (so subsequent identical considers are suppressed).
    assert pc._last_payload["X"] == b"B"
    assert "X" not in pc._pending


@pytest.mark.asyncio
async def test_flush_due_preserves_newer_pending_added_during_await():
    """Latent data-loss bug paired with the KeyError race: if a concurrent
    consider() stashes a NEWER pending entry while flush_due is awaiting
    sink, the trailing `del self._pending[topic]` would drop that newer
    entry. The newer pending must survive so it can be flushed next tick.

    Forcing the stash path requires two sequential considers during the
    slow await: the first hits the immediate-publish branch and updates
    _last_publish[X] to `now`, so the second sees the cooldown as
    unelapsed and stashes.
    """
    from web.services.mqtt import PublishCoalescer
    now = [0.0]
    pc = PublishCoalescer(monotonic=lambda: now[0])

    sent: list[tuple[str, bytes]] = []
    async def fast_sink(topic, payload, retain, qos):
        sent.append((topic, payload))

    await pc.consider("X", b"A", min_interval=5.0,
                      sink=fast_sink, retain=True, qos=1)
    now[0] = 1.0
    await pc.consider("X", b"B", min_interval=5.0,
                      sink=fast_sink, retain=True, qos=1)

    now[0] = 10.0
    publish_started = asyncio.Event()
    publish_release = asyncio.Event()

    async def slow_sink(topic, payload, retain, qos):
        publish_started.set()
        await publish_release.wait()
        sent.append((topic, payload))

    flush_task = asyncio.create_task(pc.flush_due(slow_sink))
    await publish_started.wait()

    # First consider during the yield: cooldown is elapsed (last_publish
    # is still 0.0), takes immediate-publish path, sets _last_publish[X]
    # = 10.0 and pops the b"B" pending entry.
    await pc.consider("X", b"C", min_interval=5.0,
                      sink=fast_sink, retain=True, qos=1)
    # Second consider: cooldown now NOT elapsed (10 - 10 < 5), so stash
    # branch — installs b"D" as the new pending.
    await pc.consider("X", b"D", min_interval=5.0,
                      sink=fast_sink, retain=True, qos=1)

    publish_release.set()
    await flush_task

    # b"D" must survive — flush_due must not delete a pending entry it
    # didn't install. Pre-fix, `del self._pending[topic]` silently
    # dropped it.
    assert "X" in pc._pending, "newer pending entry was dropped by flush_due"
    assert pc._pending["X"].payload == b"D"
