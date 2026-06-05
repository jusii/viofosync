"""Persistent application log.

Captures records from the standard ``logging`` framework into the
``app_log`` table and live-broadcasts each one over the WebSocket hub,
so the UI's Logs tab shows a durable, filterable history instead of the
old ephemeral in-DOM event log.

Capture is decoupled from I/O: ``DBLogHandler.emit`` only enqueues onto
the event loop; a single async ``run`` task batch-inserts and broadcasts.
This keeps ``emit`` non-blocking on whatever thread logged (sync worker,
export worker, uvicorn) and makes re-entrancy impossible — a DB error in
the drain task that itself logs just enqueues another record rather than
recursing into a synchronous write.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import sys
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional

# Keep newest this-many rows; older rows are pruned. A module-level
# constant (not a setting yet) so tests can monkeypatch it.
APP_LOG_MAX_ROWS = 50_000

# INFO+ is persisted from our own loggers; everything else only at
# WARNING+ (keeps third-party INFO chatter — httpx, uvicorn — out).
_APP_NAMESPACES = ("viofosync", "viofosync_lib")
_APP_PREFIXES = tuple(ns + "." for ns in _APP_NAMESPACES)

# Rows inserted between prune sweeps.
_PRUNE_EVERY = 200

# Cap the live queue and per-transaction batch so a burst of logging
# can't grow memory or transaction size without bound. Overflow drops
# the newest record (and notes the drop on stderr) rather than blocking
# the thread that logged.
_QUEUE_MAXSIZE = 10_000
_MAX_BATCH = 1_000

# Name -> numeric level, for the API's `level` filter.
LEVELS = {
    "DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50,
}

BroadcastFn = Callable[[Dict[str, Any]], Awaitable[None]]


def _should_capture(record: logging.LogRecord) -> bool:
    if record.levelno >= logging.WARNING:
        return True
    name = record.name
    return name in _APP_NAMESPACES or name.startswith(_APP_PREFIXES)


class _CaptureFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        return _should_capture(record)


class DBLogHandler(logging.Handler):
    """Root-logger handler that persists records to ``app_log``.

    ``emit`` is cheap and thread-safe: it formats the record and hands
    it to the event loop. Nothing touches the DB until the async
    ``run`` task drains the queue.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.addFilter(_CaptureFilter())
        # Records seen before the loop is bound buffer here and flush
        # when ``run`` starts. Startup contract: callers must invoke
        # ``bind()`` and start ``run()`` back-to-back on the loop thread;
        # a record logged from another thread in that tiny pre-bind window
        # buffers here, and the bounded startup flush may drop a record
        # logged in the microsecond between the flush and the clear.
        self._pending: Deque[Dict[str, Any]] = collections.deque(maxlen=1000)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: Optional[asyncio.Queue] = None
        self._db: Any = None
        self._broadcast: Optional[BroadcastFn] = None
        self._dropped = 0

    # -- logging.Handler API --

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload = self._to_payload(record)
        except Exception:  # never let logging raise into the caller
            return
        loop, queue = self._loop, self._queue
        if loop is None or queue is None:
            self._pending.append(payload)
            return
        try:
            loop.call_soon_threadsafe(self._enqueue, payload)
        except RuntimeError:
            # Loop is closed (shutdown) — drop the record.
            pass

    def _enqueue(self, payload: Dict[str, Any]) -> None:
        q = self._queue
        if q is None:  # pragma: no cover — bound before emit schedules this
            return
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            self._dropped += 1
            # Surface the overflow without re-entering the log path.
            if self._dropped % 1000 == 1:
                print(
                    f"app_log queue full; dropped {self._dropped} record(s)",
                    file=sys.stderr,
                )

    @staticmethod
    def _to_payload(record: logging.LogRecord) -> Dict[str, Any]:
        exc_text = None
        if record.exc_info:
            exc_text = logging.Formatter().formatException(record.exc_info)
        return {
            "ts": record.created,
            "levelno": record.levelno,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "exc_text": exc_text,
        }

    # -- wiring --

    def bind(
        self,
        db: Any,
        broadcast: BroadcastFn,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Attach the DB, hub broadcast coroutine, and loop. Must be
        called from inside the running loop (creates the asyncio.Queue)."""
        self._db = db
        self._broadcast = broadcast
        self._loop = loop
        self._queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)

    async def run(self) -> None:
        """Drain loop. Cancel the task to stop."""
        assert self._queue is not None, "bind() before run()"
        if self._pending:
            await self._drain_batch(list(self._pending))
            self._pending.clear()
            await asyncio.to_thread(self._prune)
        since_prune = 0
        while True:
            payload = await self._queue.get()
            batch = [payload]
            while len(batch) < _MAX_BATCH:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            await self._drain_batch(batch)
            since_prune += len(batch)
            if since_prune >= _PRUNE_EVERY:
                since_prune = 0
                await asyncio.to_thread(self._prune)

    # -- internals --

    async def _drain_batch(self, batch: List[Dict[str, Any]]) -> None:
        rows = await asyncio.to_thread(self._insert_batch, batch)
        if self._broadcast is None:
            return
        for log_id, p in rows:
            await self._broadcast({"type": "log", "id": log_id, **p})

    def _insert_batch(
        self, batch: List[Dict[str, Any]]
    ) -> List[tuple]:
        rows: List[tuple] = []
        try:
            with self._db.write() as c:
                for p in batch:
                    cur = c.execute(
                        "INSERT INTO app_log "
                        "(ts, levelno, level, logger, message, exc_text) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            p["ts"], p["levelno"], p["level"],
                            p["logger"], p["message"], p["exc_text"],
                        ),
                    )
                    rows.append((cur.lastrowid, p))
        except Exception as e:  # pragma: no cover — report off the log path
            print(f"app_log insert failed: {e}", file=sys.stderr)
            return []
        return rows

    def _prune(self) -> None:
        try:
            with self._db.write() as c:
                c.execute(
                    "DELETE FROM app_log WHERE id <= "
                    "(SELECT MAX(id) FROM app_log) - ?",
                    (APP_LOG_MAX_ROWS,),
                )
        except Exception as e:  # pragma: no cover
            print(f"app_log prune failed: {e}", file=sys.stderr)


def query_logs(
    db: Any,
    *,
    min_levelno: int = logging.WARNING,
    logger: Optional[str] = None,
    q: Optional[str] = None,
    before: Optional[int] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Return persisted log rows, newest-first.

    ``before`` (an id) and ``limit`` drive "load older" pagination.
    ``logger`` / ``q`` are case-insensitive substring matches.
    """
    limit = max(1, min(int(limit), 1000))
    clauses = ["levelno >= ?"]
    params: List[Any] = [int(min_levelno)]
    if logger:
        clauses.append("logger LIKE ?")
        params.append(f"%{logger}%")
    if q:
        clauses.append("message LIKE ?")
        params.append(f"%{q}%")
    if before is not None:
        clauses.append("id < ?")
        params.append(int(before))
    where = " AND ".join(clauses)
    params.append(limit)
    # NB: levelno is a range filter, so SQLite serves this by scanning id
    # DESC (the PK) rather than idx_app_log_levelno; fine within the row cap.
    sql = (
        "SELECT id, ts, levelno, level, logger, message, exc_text "
        f"FROM app_log WHERE {where} ORDER BY id DESC LIMIT ?"
    )
    with db.conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]
