"""Process Intelligence Engine — snapshot, score, and analyze running processes.

Provides the data layer for the Critical Memory Pressure Resolution System:
- ProcessSnapshot and KillListEntry dataclasses
- fetch_all_processes() with timeout safety
- Lightweight (NORMAL state) and FULL (pressure state) snapshot modes
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import psutil

logger = logging.getLogger(__name__)


# ── Dataclasses ──────────────────────────────────────────────────────────


@dataclass
class ProcessSnapshot:
    """Snapshot of a single process at a moment in time."""

    pid: int
    name: str
    cmdline: str
    create_time: float
    memory_rss_mb: float
    memory_percent: float
    cpu_percent: float
    io_read_bytes: int
    io_write_bytes: int
    status: str
    username: str


@dataclass
class KillListEntry:
    """A process that has been scored and placed on the curator's kill list."""

    pid: int
    name: str
    cmdline: str
    score: float
    memory_mb: float
    cpu_percent: float
    reason: str
    is_false_positive: bool = False


# ── Process Collection ───────────────────────────────────────────────────


COLLECTION_TIMEOUT_SECONDS = 5


def fetch_all_processes(
    lightweight: bool = False,
    timeout: float = COLLECTION_TIMEOUT_SECONDS,
) -> list[ProcessSnapshot]:
    """Fetch all running processes with resource metrics.

    Args:
        lightweight: If True, return only top 20 by memory with basic fields.
            Used during NORMAL state to reduce overhead.
        timeout: Max seconds to spend collecting. Returns partial results
            on timeout (logs warning).

    Returns:
        List of ProcessSnapshot sorted by memory_rss_mb descending.
    """
    snapshots: list[ProcessSnapshot] = []
    deadline = time.monotonic() + timeout
    collected = 0
    errors = 0

    attrs = ["pid", "name", "cmdline", "cpu_percent", "memory_info",
             "memory_percent", "io_counters", "status", "create_time", "username"]

    for proc in psutil.process_iter(attrs):
        if time.monotonic() > deadline:
            logger.warning(
                "Process collection timed out after %.1fs — collected %d processes",
                timeout, collected,
            )
            break

        try:
            pinfo = proc.info
            pid = pinfo["pid"]
            if pid == os.getpid():
                continue  # Skip ourself

            mem = pinfo.get("memory_info")
            io = pinfo.get("io_counters")

            snap = ProcessSnapshot(
                pid=pid,
                name=str(pinfo.get("name", "?")),
                cmdline=" ".join(pinfo.get("cmdline") or ["?"]),
                create_time=float(pinfo.get("create_time", 0) or 0),
                memory_rss_mb=(mem.rss / (1024 * 1024)) if mem and mem.rss else 0.0,
                memory_percent=float(pinfo.get("memory_percent", 0) or 0),
                cpu_percent=float(pinfo.get("cpu_percent", 0) or 0),
                io_read_bytes=int(io.read_bytes) if io else 0,
                io_write_bytes=int(io.write_bytes) if io else 0,
                status=str(pinfo.get("status", "?")),
                username=str(pinfo.get("username", "?")),
            )
            snapshots.append(snap)
            collected += 1

        except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError):
            errors += 1
            continue

    if errors:
        logger.debug("Process collection: %d errors (zombies/permissions)", errors)

    # Sort by memory descending
    snapshots.sort(key=lambda s: s.memory_rss_mb, reverse=True)

    if lightweight:
        return snapshots[:20]

    return snapshots


def snapshot_processes_to_db(
    snapshots: list[ProcessSnapshot],
    db,
) -> int:
    """Batch-insert process snapshots into the metrics DB.

    Args:
        snapshots: Process snapshots to persist.
        db: MetricsDB instance.

    Returns:
        Number of rows inserted.
    """
    if not hasattr(db, "save_process_snapshots"):
        logger.warning("DB has no save_process_snapshots method — snapshots not saved")
        return 0

    return db.save_process_snapshots(snapshots)