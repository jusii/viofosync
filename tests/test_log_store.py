from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

import pytest

from web.db import Database
from web.services import log_store
from web.services.log_store import DBLogHandler


def test_app_log_table_created(tmp_path) -> None:
    db = Database(str(tmp_path / "t.db"))
    with db.conn() as c:
        cols = {
            row["name"]
            for row in c.execute("PRAGMA table_info(app_log)").fetchall()
        }
    assert cols == {
        "id", "ts", "levelno", "level", "logger", "message", "exc_text",
    }


def _record(name, level, msg, *, exc_info=None) -> logging.LogRecord:
    return logging.LogRecord(
        name=name, level=level, pathname=__file__, lineno=1,
        msg=msg, args=None, exc_info=exc_info,
    )


async def _drain_once(handler: DBLogHandler) -> None:
    """Let the bound drain task process the queued records."""
    for _ in range(50):
        await asyncio.sleep(0.01)
        if handler._queue is not None and handler._queue.empty():
            # one more tick so the in-flight batch finishes inserting
            await asyncio.sleep(0.02)
            return


@pytest.fixture
async def bound_handler(tmp_path):
    from web.db import Database
    db = Database(str(tmp_path / "t.db"))
    sent: list = []

    async def broadcast(ev):
        sent.append(ev)

    h = DBLogHandler()
    h.bind(db, broadcast, asyncio.get_running_loop())
    task = asyncio.create_task(h.run())
    try:
        yield h, db, sent
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_handler_persists_and_broadcasts(bound_handler) -> None:
    h, db, sent = bound_handler
    h.handle(_record("viofosync.test", logging.WARNING, "boom"))
    await _drain_once(h)
    with db.conn() as c:
        rows = [
            tuple(r) for r in c.execute(
                "SELECT level, logger, message FROM app_log"
            ).fetchall()
        ]
    assert rows == [("WARNING", "viofosync.test", "boom")]
    logs = [e for e in sent if e.get("type") == "log"]
    assert logs and logs[0]["message"] == "boom" and logs[0]["id"] >= 1


async def test_scope_filter(bound_handler) -> None:
    h, db, _ = bound_handler
    h.handle(_record("httpx", logging.INFO, "chatter"))      # dropped
    h.handle(_record("httpx", logging.WARNING, "uh oh"))     # kept (>=WARNING)
    h.handle(_record("viofosync.x", logging.INFO, "ours"))   # kept (our ns)
    await _drain_once(h)
    with db.conn() as c:
        msgs = {
            r["message"] for r in c.execute(
                "SELECT message FROM app_log"
            ).fetchall()
        }
    assert msgs == {"uh oh", "ours"}


async def test_exc_text_captured(bound_handler) -> None:
    h, db, _ = bound_handler
    try:
        raise ValueError("kaboom")
    except ValueError:
        import sys
        h.handle(_record(
            "viofosync.x", logging.ERROR, "failed", exc_info=sys.exc_info()
        ))
    await _drain_once(h)
    with db.conn() as c:
        exc = c.execute("SELECT exc_text FROM app_log").fetchone()["exc_text"]
    assert exc is not None and "ValueError: kaboom" in exc


async def test_prune_keeps_newest(bound_handler, monkeypatch) -> None:
    h, db, _ = bound_handler
    monkeypatch.setattr(log_store, "APP_LOG_MAX_ROWS", 5)
    with db.write() as c:
        for i in range(12):
            c.execute(
                "INSERT INTO app_log (ts, levelno, level, logger, message) "
                "VALUES (?, ?, ?, ?, ?)",
                (float(i), 30, "WARNING", "viofosync.x", f"m{i}"),
            )
    h._prune()
    with db.conn() as c:
        msgs = [
            r["message"] for r in c.execute(
                "SELECT message FROM app_log ORDER BY id"
            ).fetchall()
        ]
    assert msgs == [f"m{i}" for i in range(7, 12)]  # newest 5 kept


async def test_reentrant_log_during_broadcast_does_not_hang(tmp_path) -> None:
    """A log emitted *during* a broadcast (the recursion hazard) must be
    captured too, without deadlocking the drain task."""
    from web.db import Database
    db = Database(str(tmp_path / "t.db"))
    fired = {"once": False}

    async def broadcast(ev):
        if not fired["once"]:
            fired["once"] = True
            logging.getLogger("viofosync.during").warning("nested")

    h = DBLogHandler()
    logging.getLogger().addHandler(h)
    h.bind(db, broadcast, asyncio.get_running_loop())
    task = asyncio.create_task(h.run())
    try:
        logging.getLogger("viofosync.first").warning("outer")
        for _ in range(100):
            await asyncio.sleep(0.01)
            with db.conn() as c:
                n = c.execute("SELECT COUNT(*) AS n FROM app_log").fetchone()["n"]
            if n >= 2:
                break
        with db.conn() as c:
            msgs = {
                r["message"] for r in c.execute(
                    "SELECT message FROM app_log"
                ).fetchall()
            }
        assert {"outer", "nested"} <= msgs
    finally:
        logging.getLogger().removeHandler(h)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_overbroad_logger_name_not_captured(bound_handler) -> None:
    h, db, _ = bound_handler
    h.handle(_record("viofosyncx", logging.INFO, "no"))    # not our namespace
    h.handle(_record("viofosync.y", logging.INFO, "yes"))  # our namespace
    await _drain_once(h)
    with db.conn() as c:
        msgs = {
            r["message"] for r in c.execute(
                "SELECT message FROM app_log"
            ).fetchall()
        }
    assert msgs == {"yes"}


async def test_enqueue_drops_when_queue_full(tmp_path, monkeypatch) -> None:
    from web.db import Database
    monkeypatch.setattr(log_store, "_QUEUE_MAXSIZE", 3)
    db = Database(str(tmp_path / "t.db"))

    async def broadcast(ev):
        pass

    h = DBLogHandler()
    h.bind(db, broadcast, asyncio.get_running_loop())  # bound, but no run() task draining
    for i in range(5):
        h._enqueue({
            "ts": 0.0, "levelno": 30, "level": "WARNING",
            "logger": "viofosync.x", "message": f"m{i}", "exc_text": None,
        })
    assert h._queue.qsize() == 3
    assert h._dropped == 2


def _seed(db) -> None:
    rows = [
        (1.0, 20, "INFO", "viofosync.scanner", "scan start"),
        (2.0, 30, "WARNING", "viofosync.sync_worker", "retry 1"),
        (3.0, 40, "ERROR", "viofosync.sync_worker", "download failed"),
        (4.0, 20, "INFO", "viofosync.geocode", "cache hit"),
    ]
    with db.write() as c:
        for r in rows:
            c.execute(
                "INSERT INTO app_log (ts, levelno, level, logger, message) "
                "VALUES (?, ?, ?, ?, ?)",
                r,
            )


def test_query_defaults_to_warning_plus_newest_first(tmp_path) -> None:
    from web.db import Database
    db = Database(str(tmp_path / "t.db"))
    _seed(db)
    out = log_store.query_logs(db)  # min_levelno defaults to WARNING
    assert [e["message"] for e in out] == ["download failed", "retry 1"]


def test_query_info_includes_everything(tmp_path) -> None:
    from web.db import Database
    db = Database(str(tmp_path / "t.db"))
    _seed(db)
    out = log_store.query_logs(db, min_levelno=20)
    assert len(out) == 4


def test_query_filters_logger_and_message(tmp_path) -> None:
    from web.db import Database
    db = Database(str(tmp_path / "t.db"))
    _seed(db)
    by_logger = log_store.query_logs(db, min_levelno=20, logger="geocode")
    assert [e["message"] for e in by_logger] == ["cache hit"]
    by_msg = log_store.query_logs(db, min_levelno=20, q="failed")
    assert [e["message"] for e in by_msg] == ["download failed"]


def test_query_before_paginates(tmp_path) -> None:
    from web.db import Database
    db = Database(str(tmp_path / "t.db"))
    _seed(db)
    page1 = log_store.query_logs(db, min_levelno=20, limit=2)
    assert [e["id"] for e in page1] == [4, 3]
    page2 = log_store.query_logs(
        db, min_levelno=20, limit=2, before=page1[-1]["id"]
    )
    assert [e["id"] for e in page2] == [2, 1]


def test_query_clamps_limit(tmp_path) -> None:
    from web.db import Database
    db = Database(str(tmp_path / "t.db"))
    _seed(db)
    # limit below 1 clamps up to 1
    assert len(log_store.query_logs(db, min_levelno=20, limit=0)) == 1
    # absurd limit clamps down to 1000 (still returns all 4 seeded rows)
    assert len(log_store.query_logs(db, min_levelno=20, limit=10_000)) == 4
