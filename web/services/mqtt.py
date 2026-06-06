"""MqttService — connection lifecycle, publish pipeline, command dispatch.

This module is built up across several tasks. Task 8 contributes the
PublishCoalescer used by MqttService; subsequent tasks add the
connection loop, Hub bridge, periodic refresh, and command handling.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
import ssl
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional


Sink = Callable[[str, bytes, bool, int], Awaitable[None]]


@dataclass
class _Pending:
    payload: bytes
    retain: bool
    qos: int
    min_interval: float


class PublishCoalescer:
    """Per-topic change-detection + minimum-interval coalescing.

    ``consider`` is called with each candidate publish. If the payload
    is unchanged from the last successful publish on this topic, the
    call is a no-op. If the topic's minimum interval has not yet
    elapsed, the call records the latest payload as pending; the
    eventual ``flush_due`` (driven by a tick task) emits one publish
    per topic with the most recent value.
    """

    def __init__(self, *, monotonic: Callable[[], float] | None = None) -> None:
        self._mono = monotonic or time.monotonic
        self._last_payload: dict[str, bytes] = {}
        self._last_publish: dict[str, float] = {}
        self._pending: dict[str, _Pending] = {}

    async def consider(
        self,
        topic: str,
        payload: bytes,
        *,
        min_interval: float,
        sink: Sink,
        retain: bool,
        qos: int,
    ) -> None:
        if self._last_payload.get(topic) == payload:
            # Already published this exact payload — also cancel any
            # stashed update so a flush_due doesn't re-emit it.
            self._pending.pop(topic, None)
            return
        now = self._mono()
        last = self._last_publish.get(topic)
        if last is None or (now - last) >= min_interval:
            # Snapshot the current pending entry (if any) so that, after
            # the sink await yields, we only clear an entry that's still
            # the same object. A concurrent flush_due or consider may
            # have installed a NEWER pending while we awaited — dropping
            # it here would lose data.
            pending_before = self._pending.get(topic)
            await sink(topic, payload, retain, qos)
            self._last_payload[topic] = payload
            self._last_publish[topic] = now
            if self._pending.get(topic) is pending_before:
                self._pending.pop(topic, None)
            return
        # Still inside the cooldown — stash the latest value.
        self._pending[topic] = _Pending(
            payload=payload, retain=retain, qos=qos,
            min_interval=min_interval,
        )

    async def flush_due(self, sink: Sink) -> None:
        """Called periodically (e.g. once a second) to emit any
        deadline-elapsed pending publishes."""
        now = self._mono()
        for topic, pend in list(self._pending.items()):
            last = self._last_publish.get(topic, 0.0)
            if (now - last) < pend.min_interval:
                continue
            await sink(topic, pend.payload, pend.retain, pend.qos)
            self._last_payload[topic] = pend.payload
            self._last_publish[topic] = now
            # Identity check: a concurrent consider() running during the
            # sink await may have popped this entry (payload matched
            # the not-yet-updated _last_payload) — KeyError on bare del —
            # or replaced it with a newer pending we must preserve.
            if self._pending.get(topic) is pend:
                del self._pending[topic]

    def forget(self, topic: str) -> None:
        self._last_payload.pop(topic, None)
        self._last_publish.pop(topic, None)
        self._pending.pop(topic, None)

    def reset(self) -> None:
        self._last_payload.clear()
        self._last_publish.clear()
        self._pending.clear()


log = logging.getLogger("viofosync.mqtt")


def entities_affected_by(hub_event_type: str):
    """Yield every TOPOLOGY entry that should re-publish in response
    to the given Hub event type."""
    from .mqtt_topology import TOPOLOGY
    for entity in TOPOLOGY:
        if hub_event_type in entity.affected_by_hub_events:
            yield entity


async def _publish_entity_attrs(client, cfg, entity, hub, db, snap,
                                  *, maybe_publish):
    """Publish the JSON attributes payload for an entity that defines
    ``attrs_fn``. No-op when attrs_fn is None or returns None."""
    if entity.attrs_fn is None:
        return
    try:
        attrs = entity.attrs_fn(hub, db, snap)
    except Exception:
        log.exception("mqtt: attrs_fn raised for %s", entity.object_id)
        return
    if attrs is None:
        return
    from .mqtt_topology import build_attrs_topic
    import json as _json
    topic = build_attrs_topic(entity.object_id, cfg)
    payload = _json.dumps(attrs).encode()
    qos = entity.qos if entity.qos is not None else cfg["qos"]
    await maybe_publish(client, topic, payload,
                        retain=True, qos=qos,
                        min_interval=entity.min_publish_interval_s)


class ConnState(enum.Enum):
    IDLE = "idle"                # service not started or settings incomplete
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    ERROR = "error"
    DISABLED = "disabled"        # MQTT_ENABLED is False


class MqttService:
    """Manages the MQTT connection lifecycle.

    Built up across several tasks. This task gives it status reporting
    and the start/stop methods (which are no-ops until Task 10 adds
    the real connection loop)."""

    def __init__(self, *, db, provider, hub, app) -> None:
        self._db = db
        self._provider = provider
        self._hub = hub
        self._app = app
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event() if hub is not None else None
        self._state: ConnState = ConnState.IDLE
        self._detail: Optional[str] = None
        self._last_published_at: Optional[float] = None
        self._coalescer = PublishCoalescer()

    # ---- status ----

    def _set_state(self, state: ConnState, *, detail: Optional[str] = None) -> None:
        self._state = state
        self._detail = detail
        log.info("mqtt: state=%s detail=%s", state.value, detail)

    def get_status(self) -> dict:
        return {
            "state": self._state.value,
            "detail": self._detail,
            "last_published_at": self._last_published_at,
        }

    BACKOFF_STEPS = (1.0, 2.0, 5.0, 15.0, 60.0)

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="mqtt-service")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None
        self._set_state(ConnState.IDLE, detail=None)

    # ---- main loop ----

    def _cfg(self) -> dict:
        snap = self._provider.get()
        client_id = snap.mqtt_client_id
        if not client_id:
            client_id = f"viofosync-{os.urandom(4).hex()}"
            try:
                self._provider.update({"MQTT_CLIENT_ID": client_id}, actor="mqtt-service")
            except Exception:
                log.warning("mqtt: failed to persist generated MQTT_CLIENT_ID")
        return {
            "host": snap.mqtt_host,
            "port": snap.mqtt_port,
            "username": snap.mqtt_username or None,
            "password": snap.mqtt_password or None,
            "tls": snap.mqtt_tls,
            "client_id": client_id,
            "discovery_prefix": snap.mqtt_discovery_prefix,
            "node_id": snap.mqtt_node_id,
            "discovery_enabled": snap.mqtt_discovery_enabled,
            "qos": snap.mqtt_qos,
            "version": self._app.version if self._app is not None else "0.0.0",
            "configuration_url": (
                f"http://{snap.host}:{snap.port}/"
                if (self._app is not None and snap.host not in ("0.0.0.0", "::", ""))
                else ""
            ),
        }

    async def _run(self) -> None:
        import aiomqtt  # imported lazily so tests that don't need it don't pay
        backoff_idx = 0
        while not self._stop.is_set():
            cfg = self._cfg()
            if not cfg["host"]:
                self._set_state(ConnState.IDLE, detail="MQTT_HOST not set")
                return
            try:
                self._set_state(ConnState.CONNECTING,
                                detail=f"{cfg['host']}:{cfg['port']}")
                await self._connect_and_loop(aiomqtt, cfg)
                backoff_idx = 0
            except asyncio.CancelledError:
                raise
            except aiomqtt.MqttError as e:
                log.warning("mqtt: connection lost (%s); reconnecting", e)
                self._set_state(ConnState.RECONNECTING, detail=str(e))
            except Exception as e:
                log.exception("mqtt: unexpected error")
                self._set_state(ConnState.ERROR, detail=str(e))
            if self._stop.is_set():
                break
            delay = self.BACKOFF_STEPS[min(backoff_idx, len(self.BACKOFF_STEPS) - 1)]
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                # stop() fired during backoff
                return
            except asyncio.TimeoutError:
                pass
            backoff_idx += 1

    async def _connect_and_loop(self, aiomqtt_mod, cfg: dict) -> None:
        will = aiomqtt_mod.Will(
            topic=f"{cfg['node_id']}/availability",
            payload=b"offline", qos=1, retain=True,
        )
        client_kwargs: dict[str, Any] = dict(
            hostname=cfg["host"], port=cfg["port"],
            username=cfg["username"], password=cfg["password"],
            identifier=cfg["client_id"], will=will, keepalive=30,
        )
        if cfg["tls"]:
            client_kwargs["tls_context"] = ssl.create_default_context()

        async with aiomqtt_mod.Client(**client_kwargs) as client:
            self._client = client
            await self._on_connected(client, cfg)
            self._set_state(ConnState.CONNECTED,
                            detail=f"{cfg['host']}:{cfg['port']}")
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._drain_publishes(client, cfg))
                tg.create_task(self._handle_commands(client, cfg))
                tg.create_task(self._tick(client, cfg))

    async def _on_connected(self, client, cfg: dict) -> None:
        # Subscribe FIRST so any retained command from a previous offline
        # window is delivered to us before we announce ourselves available.
        from .mqtt_topology import (
            TOPOLOGY, build_command_topic, build_discovery_topic,
            build_discovery_payload,
        )
        import json as _json

        for entity in TOPOLOGY:
            if entity.command_handler is not None:
                await client.subscribe(
                    build_command_topic(entity.object_id, cfg), qos=1,
                )
        await client.subscribe(
            f"{cfg['node_id']}/cmd/prioritize_recent", qos=1,
        )

        if cfg["discovery_enabled"]:
            for entity in TOPOLOGY:
                topic = build_discovery_topic(entity.component,
                                              entity.object_id, cfg)
                await client.publish(
                    topic,
                    _json.dumps(build_discovery_payload(entity, cfg)).encode(),
                    qos=1, retain=True,
                )
        await self._publish_full_state(client, cfg)

        # Announce availability LAST so HA never receives "online" before
        # discovery, state, and command subscriptions are in place.
        await client.publish(
            f"{cfg['node_id']}/availability", b"online",
            qos=1, retain=True,
        )

    async def _publish_full_state(self, client, cfg: dict) -> None:
        from .mqtt_topology import TOPOLOGY, build_state_topic
        snap = self._provider.get()
        for entity in TOPOLOGY:
            if entity.state_fn is None:
                continue
            try:
                value = entity.state_fn(self._hub, self._db, snap)
            except Exception:
                log.exception("mqtt: state_fn raised for %s", entity.object_id)
                continue
            if value is None:
                continue
            topic = build_state_topic(entity.object_id, cfg)
            qos = entity.qos if entity.qos is not None else cfg["qos"]
            await self._maybe_publish(client, topic, value.encode(),
                                       retain=True, qos=qos,
                                       min_interval=entity.min_publish_interval_s)
            await _publish_entity_attrs(
                client, cfg, entity, self._hub, self._db, snap,
                maybe_publish=self._maybe_publish,
            )

    async def _maybe_publish(
        self, client, topic: str, payload: bytes,
        *, retain: bool, qos: int, min_interval: float,
    ) -> None:
        async def _sink(t, p, r, q):
            await client.publish(t, p, qos=q, retain=r)
            self._last_published_at = time.time()
        await self._coalescer.consider(
            topic, payload, min_interval=min_interval,
            sink=_sink, retain=retain, qos=qos,
        )

    async def _drain_publishes(self, client, cfg: dict) -> None:
        """Subscribe to the Hub; translate each event into a candidate
        publish for every affected entity. Uses an asyncio.Queue to
        decouple Hub callbacks from broker I/O."""
        q: asyncio.Queue = asyncio.Queue(maxsize=1024)

        async def _hub_handler(event: dict) -> None:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("mqtt: hub bridge queue full; dropping event type=%s",
                             event.get("type"))

        # Hub is the existing Hub() instance; it doesn't expose a
        # subscribe API today. The simplest minimal-invasive bridge is
        # to wrap its broadcast method so we get a fan-out call before
        # the original. Replaceable with a proper subscribe model later.
        original_broadcast = self._hub.broadcast

        async def _intercepting_broadcast(event: dict) -> None:
            await original_broadcast(event)
            await _hub_handler(event)

        self._hub.broadcast = _intercepting_broadcast  # type: ignore[assignment]

        # Also intercept schedule_broadcast — that path is used from
        # worker threads, and currently runs original broadcast on the
        # loop. Need to ensure our fan-out fires there too.
        original_schedule = self._hub.schedule_broadcast

        def _intercepting_schedule(running_loop, event: dict) -> None:
            try:
                asyncio.run_coroutine_threadsafe(
                    _intercepting_broadcast(event), running_loop,
                )
            except RuntimeError:
                log.debug("event loop closed, dropping event %s", event)

        self._hub.schedule_broadcast = _intercepting_schedule  # type: ignore[assignment]

        try:
            from .mqtt_topology import build_state_topic
            while not self._stop.is_set():
                event = await q.get()
                etype = event.get("type")
                if not etype:
                    continue
                snap = self._provider.get()
                for entity in entities_affected_by(etype):
                    if entity.state_fn is None:
                        continue
                    try:
                        value = entity.state_fn(self._hub, self._db, snap)
                    except Exception:
                        log.exception("mqtt: state_fn raised for %s",
                                       entity.object_id)
                        continue
                    if value is None:
                        continue
                    qos = entity.qos if entity.qos is not None else cfg["qos"]
                    await self._maybe_publish(
                        client,
                        build_state_topic(entity.object_id, cfg),
                        value.encode(),
                        retain=True,
                        qos=qos,
                        min_interval=entity.min_publish_interval_s,
                    )
                    await _publish_entity_attrs(
                        client, cfg, entity, self._hub, self._db, snap,
                        maybe_publish=self._maybe_publish,
                    )
        finally:
            # Restore original broadcast methods so subsequent test
            # runs / restarts don't accumulate interceptors.
            self._hub.broadcast = original_broadcast  # type: ignore[assignment]
            self._hub.schedule_broadcast = original_schedule  # type: ignore[assignment]

    async def _handle_commands(self, client, cfg: dict) -> None:
        from .mqtt_topology import (
            TOPOLOGY, build_command_topic, build_command_handlers,
        )
        handlers = build_command_handlers(self._app)
        routes: dict[str, Any] = {}
        for entity in TOPOLOGY:
            if entity.command_handler is None:
                continue
            routes[build_command_topic(entity.object_id, cfg)] = (
                handlers[entity.object_id]
            )
        routes[f"{cfg['node_id']}/cmd/prioritize_recent"] = (
            handlers["prioritize_recent"]
        )

        async for message in client.messages:
            topic = message.topic.value
            handler = routes.get(topic)
            if handler is None:
                log.debug("mqtt: unrouted topic %s", topic)
                continue
            try:
                await handler(message.payload)
            except Exception:
                log.exception("mqtt: handler raised for %s", topic)

    CONNECTION_KEYS = frozenset({
        "MQTT_ENABLED", "MQTT_HOST", "MQTT_PORT", "MQTT_USERNAME",
        "MQTT_PASSWORD", "MQTT_TLS", "MQTT_CLIENT_ID",
        "MQTT_DISCOVERY_PREFIX", "MQTT_NODE_ID",
    })

    async def on_settings_changed(self, keys: set, snap) -> None:
        if not (keys & self.CONNECTION_KEYS):
            return
        # If node_id or discovery_prefix changed, send delete-payloads
        # to every old discovery topic before restarting.
        if (
            {"MQTT_NODE_ID", "MQTT_DISCOVERY_PREFIX"} & keys
            and getattr(self, "_last_node_id", None) is not None
        ):
            old_cfg = {
                "discovery_prefix": self._last_discovery_prefix,
                "node_id": self._last_node_id,
            }
            from .mqtt_topology import TOPOLOGY, build_discovery_topic
            for entity in TOPOLOGY:
                topic = build_discovery_topic(entity.component,
                                               entity.object_id, old_cfg)
                try:
                    await self._publish_now(topic, b"", True, 1)
                except Exception:
                    log.exception("mqtt: cleanup publish failed for %s", topic)

        self._last_node_id = getattr(snap, "mqtt_node_id", None)
        self._last_discovery_prefix = getattr(snap, "mqtt_discovery_prefix", None)

        await self.stop()
        if snap.mqtt_enabled and snap.mqtt_host:
            self.start()

    async def _publish_now(self, topic: str, payload: bytes,
                            retain: bool, qos: int) -> None:
        """One-shot publish using a fresh short-lived connection.
        Used by node-rename cleanup, which needs to publish to the
        *old* topology even after settings have already switched."""
        if self._provider is None:
            return
        snap = self._provider.get()
        if not snap.mqtt_host:
            return
        import aiomqtt
        kwargs = dict(
            hostname=snap.mqtt_host, port=snap.mqtt_port,
            username=snap.mqtt_username or None,
            password=snap.mqtt_password or None,
            keepalive=10,
        )
        if snap.mqtt_tls:
            kwargs["tls_context"] = ssl.create_default_context()
        try:
            async with aiomqtt.Client(**kwargs) as c:
                await c.publish(topic, payload, qos=qos, retain=retain)
        except Exception:
            log.exception("mqtt: _publish_now failed for %s", topic)

    async def _tick(self, client, cfg: dict) -> None:
        from .mqtt_topology import TOPOLOGY, build_state_topic

        async def _sink(t, p, r, q):
            await client.publish(t, p, qos=q, retain=r)
            self._last_published_at = time.time()

        next_refresh = time.monotonic()
        while True:
            await asyncio.sleep(1.0)
            await self._coalescer.flush_due(_sink)

            now = time.monotonic()
            if now < next_refresh:
                continue
            next_refresh = now + 60.0
            snap = self._provider.get()
            for entity in TOPOLOGY:
                # Only re-publish poll-sourced entities here. Entities
                # with hub-event sources are kept fresh by _drain_publishes.
                if entity.state_fn is None:
                    continue
                if entity.object_id not in ("disk_used", "dashcam"):
                    continue
                try:
                    value = entity.state_fn(self._hub, self._db, snap)
                except Exception:
                    log.exception("mqtt: state_fn raised for %s",
                                   entity.object_id)
                    continue
                if value is None:
                    continue
                qos = entity.qos if entity.qos is not None else cfg["qos"]
                await self._maybe_publish(
                    client,
                    build_state_topic(entity.object_id, cfg),
                    value.encode(),
                    retain=True, qos=qos,
                    min_interval=0.0,
                )
