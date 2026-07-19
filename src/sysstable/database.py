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

CREATE TABLE IF NOT EXISTS kill_list_generations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_ns INTEGER NOT NULL,
    trigger TEXT NOT NULL,
    entries_json TEXT NOT NULL,
    mem_avail_mb REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS resolution_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_ns INTEGER NOT NULL,
    action TEXT NOT NULL,
    pid INTEGER NOT NULL,
    name TEXT NOT NULL,
    signal TEXT,
    success INTEGER DEFAULT 0,
    details TEXT
);

CREATE TABLE IF NOT EXISTS process_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_ns INTEGER NOT NULL,
    pid INTEGER NOT NULL,
    name TEXT NOT NULL,
    cmdline TEXT,
    memory_rss_mb REAL,
    memory_percent REAL,
    cpu_percent REAL,
    io_read_bytes INTEGER,
    io_write_bytes INTEGER,
    status TEXT,
    username TEXT,
    num_fds INTEGER DEFAULT 0,
    nice INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_snaps_pid_name ON process_snapshots(pid, name);
CREATE INDEX IF NOT EXISTS idx_snaps_timestamp ON process_snapshots(timestamp_ns);

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

        # Seamless schema upgrades for newer columns
        try:
            self.conn.execute("ALTER TABLE process_snapshots ADD COLUMN num_fds INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            self.conn.execute("ALTER TABLE process_snapshots ADD COLUMN nice INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        self.conn.commit()

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
        """Return the total number of metric records."""
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
        """Close the database connection after checkpointing WAL."""
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.conn.close()

    # ── Kill List Generations ────────────────────────────────────────────

    def save_kill_list_generation(self, trigger: str, entries_json: str, mem_avail_mb: float = 0.0) -> int:
        """Persist a kill list generation event. Returns row ID."""
        cursor = self.conn.execute(
            "INSERT INTO kill_list_generations (timestamp_ns, trigger, entries_json, mem_avail_mb) VALUES (?, ?, ?, ?)",
            (time.time_ns(), trigger, entries_json, mem_avail_mb),
        )
        self.conn.commit()
        return cursor.lastrowid or 0

    def query_kill_list_history(self, limit: int = 10) -> list[dict[str, Any]]:
        """Return the most recent kill list generations."""
        rows = self.conn.execute(
            "SELECT * FROM kill_list_generations ORDER BY timestamp_ns DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Resolution Events ────────────────────────────────────────────────

    def save_resolution_event(
        self,
        action: str,
        pid: int,
        name: str,
        signal: str | None = None,
        success: bool = False,
        details: str | None = None,
    ) -> int:
        """Record a resolution event (kill/pause/unpause). Returns row ID."""
        cursor = self.conn.execute(
            "INSERT INTO resolution_events (timestamp_ns, action, pid, name, signal, success, details) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (time.time_ns(), action, pid, name, signal, int(success), details),
        )
        self.conn.commit()
        return cursor.lastrowid or 0

    def query_resolution_history(self, limit: int = 10) -> list[dict[str, Any]]:
        """Return the most recent resolution events."""
        rows = self.conn.execute(
            "SELECT * FROM resolution_events ORDER BY timestamp_ns DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Process Snapshots ────────────────────────────────────────────────

    def save_process_snapshots(self, snapshots: list[Any]) -> int:
        """Batch-insert process snapshots. Returns number of rows inserted."""
        count = 0
        now = time.time_ns()
        for snap in snapshots:
            num_fds = getattr(snap, "num_fds", 0)
            nice = getattr(snap, "nice", 0)
            self.conn.execute(
                "INSERT INTO process_snapshots "
                "(timestamp_ns, pid, name, cmdline, memory_rss_mb, memory_percent, "
                " cpu_percent, io_read_bytes, io_write_bytes, status, username, num_fds, nice) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    now,
                    snap.pid,
                    snap.name,
                    snap.cmdline,
                    snap.memory_rss_mb,
                    snap.memory_percent,
                    snap.cpu_percent,
                    snap.io_read_bytes,
                    snap.io_write_bytes,
                    snap.status,
                    snap.username,
                    num_fds,
                    nice,
                ),
            )
            count += 1
        self.conn.commit()
        return count

    def query_process_snapshots(self, pid: int, name: str, hours: int = 1) -> list[dict[str, Any]]:
        """Return process snapshots for a given pid+name within the last N hours."""
        cutoff_ns = time.time_ns() - (hours * 3600 * 1_000_000_000)
        rows = self.conn.execute(
            "SELECT * FROM process_snapshots "
            "WHERE pid = ? AND name = ? AND timestamp_ns >= ? "
            "ORDER BY timestamp_ns DESC",
            (pid, name, cutoff_ns),
        ).fetchall()
        return [dict(r) for r in rows]

    def prune_process_snapshots(self, retain_hours: int = 24) -> int:
        """Delete process snapshots older than retain_hours. Returns count deleted."""
        cutoff_ns = time.time_ns() - (retain_hours * 3600 * 1_000_000_000)
        row = self.conn.execute(
            "SELECT COUNT(*) as c FROM process_snapshots WHERE timestamp_ns < ?",
            (cutoff_ns,),
        ).fetchone()
        before = row["c"] if row else 0
        self.conn.execute(
            "DELETE FROM process_snapshots WHERE timestamp_ns < ?",
            (cutoff_ns,),
        )
        self.conn.commit()
        return before


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    try:
        data = json.loads(row["data_json"])
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        data = {"error": str(e)}
    data["timestamp"] = row["timestamp_ns"]
    return data
