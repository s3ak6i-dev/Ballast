"""SQLite event log (TechSpec §4): audit trail behind the dashboard's Event Log.

Attaches to a runtime as a bus subscriber; every event becomes a row. Writes
are serialized with a lock (events arrive from worker threads), and the
subscriber never raises — the bus swallows failures, but we still guard so a
full disk can't spam the log.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from .events import Event
from .runtime import Runtime

logger = logging.getLogger("ballast.eventlog")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    dependency TEXT,
    detail TEXT,
    session_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_type ON events (event_type);
CREATE INDEX IF NOT EXISTS idx_events_dependency ON events (dependency);
"""


class SQLiteEventLog:
    """File-backed event sink + query API. Use ``:memory:`` for tests."""

    def __init__(self, path: str = "ballast_events.db") -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._unsubscribe: Callable[[], None] | None = None

    def attach(self, runtime: Runtime) -> None:
        """Subscribe to the runtime's bus; every event is persisted."""
        self._unsubscribe = runtime.bus.subscribe(self.handle)

    def handle(self, event: Event) -> None:
        row = (
            datetime.fromtimestamp(event.timestamp, tz=timezone.utc).isoformat(),
            str(event.event_type),
            event.dependency,
            json.dumps(event.detail),
            event.session_id,
        )
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO events (timestamp, event_type, dependency, detail, session_id)"
                    " VALUES (?, ?, ?, ?, ?)",
                    row,
                )
                self._conn.commit()
        except sqlite3.Error:
            logger.exception("failed to persist event %s", event.event_type)

    def query(
        self,
        *,
        limit: int = 200,
        event_type: str | None = None,
        dependency: str | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Most recent events first, with optional exact-match filters."""
        clauses, params = [], []
        for column, value in (
            ("event_type", event_type),
            ("dependency", dependency),
            ("session_id", session_id),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            "SELECT id, timestamp, event_type, dependency, detail, session_id"
            f" FROM events{where} ORDER BY id DESC LIMIT ?"
        )
        params.append(max(1, min(limit, 5000)))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "id": row[0],
                "timestamp": row[1],
                "event_type": row[2],
                "dependency": row[3],
                "detail": json.loads(row[4]) if row[4] else {},
                "session_id": row[5],
            }
            for row in rows
        ]

    def close(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        with self._lock:
            self._conn.close()
