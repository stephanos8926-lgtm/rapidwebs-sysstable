"""SQLite database — WAL mode, retention, write/query helpers."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_ns INTEGER NOT NULL,
    data_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_metrics_timestamp ON metrics(timestamp_ns);
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
"""


class MetricsDB:
    """SQLite-backed metrics store with WAL mode and retention."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_SQL)

    def __enter__(self) -> MetricsDB:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def write(self, metrics: dict[str, Any]) -> None:
        """Write a metrics snapshot."""
        self.conn.execute(
            "INSERT INTO metrics (timestamp_ns, data_json) VALUES (?, ?)",
            (metrics["timestamp"], json.dumps(metrics)),
        )
        self.conn.commit()

    def query_recent(self, limit: int = 10) -> list[dict[str, Any]]:
        """Return the most recent N metric snapshots."""
        rows = self.conn.execute(
            "SELECT timestamp_ns, data_json FROM metrics ORDER BY timestamp_ns DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def query_range(self, since_ns: int, until_ns: int | None = None) -> list[dict[str, Any]]:
        """Return metrics within a time range (nanoseconds)."""
        if until_ns is None:
            until_ns = time.time_ns()
        rows = self.conn.execute(
            "SELECT timestamp_ns, data_json FROM metrics "
            "WHERE timestamp_ns >= ? AND timestamp_ns <= ? "
            "ORDER BY timestamp_ns ASC",
            (since_ns, until_ns),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def get_latest(self) -> dict[str, Any] | None:
        """Return the single most recent metric snapshot, or None."""
        rows = self.conn.execute(
            "SELECT timestamp_ns, data_json FROM metrics ORDER BY timestamp_ns DESC LIMIT 1"
        ).fetchall()
        return _row_to_dict(rows[0]) if rows else None

    def count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) as c FROM metrics").fetchone()
        return row["c"] if row else 0

    def prune(self, retain_hours: int = 72) -> int:
        """Delete metrics older than retain_hours. Returns count deleted."""
        cutoff_ns = time.time_ns() - (retain_hours * 3600 * 1_000_000_000)
        row = self.conn.execute("SELECT COUNT(*) as c FROM metrics WHERE timestamp_ns < ?", (cutoff_ns,)).fetchone()
        before = row["c"] if row else 0
        self.conn.execute("DELETE FROM metrics WHERE timestamp_ns < ?", (cutoff_ns,))
        self.conn.commit()
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return before

    def close(self) -> None:
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.conn.close()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    try:
        data = json.loads(row["data_json"])
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        data = {"error": str(e)}
    data["timestamp"] = row["timestamp_ns"]
    return data
