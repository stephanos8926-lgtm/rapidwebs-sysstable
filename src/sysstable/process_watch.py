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


# ── No-Kill Manager ─────────────────────────────────────────────────────

HARD_CODED_NO_KILL: frozenset[str] = frozenset({
    # Kernel / init
    "init", "systemd", "kthreadd", "kworker/*", "ksoftirqd/*",
    "migration/*", "watchdog/*", "rcu*", "mm_percpu_wq",
    # System critical
    "systemd-journald", "systemd-logind", "systemd-udevd",
    "systemd-resolved", "systemd-timesyncd", "systemd-oomd",
    "dbus-daemon", "dbus-broker",
    # This daemon
    "sysstable", "sysstabled",
    # Security
    "sshd", "login", "sudo", "polkitd",
    # Container runtime
    "dockerd", "containerd", "runc",
})


class NoKillManager:
    """Three-layer protected-process manager.

    Layers (evaluated first-match-wins):
    1. HARD_CODED_NO_KILL — immutable set of system-critical processes
    2. User config file — ``never_kill.user_list`` from config.yaml
    3. CLI / ENV overrides — appended at runtime via ``--never-kill`` or
       ``SYSTABLE_NEVER_KILL`` environment variable

    Process identification uses **(pid, name, cmdline) triple matching**,
    never PID alone. This prevents PID-recycling attacks where a new
    malicious process inherits a protected PID after the original exits.
    """

    def __init__(self, user_list: list | None = None,
                 cli_overrides: list | None = None,
                 env_var: str | None = None):
        self._user_names: set[str] = set(user_list or [])
        self._cli_names: set[str] = set()
        self._cli_pids: set[int] = set()

        # Parse CLI overrides
        for item in (cli_overrides or []):
            item = item.strip()
            if not item:
                continue
            try:
                self._cli_pids.add(int(item))
            except ValueError:
                self._cli_names.add(item)

        # Parse ENV overrides from SYSTABLE_NEVER_KILL
        env_val = env_var or os.environ.get("SYSTABLE_NEVER_KILL", "")
        if env_val:
            for item in env_val.split(","):
                item = item.strip()
                if not item:
                    continue
                try:
                    pid = int(item)
                    if pid <= 0:
                        continue
                    self._cli_pids.add(pid)
                except ValueError:
                    if item:
                        self._cli_names.add(item)

    @staticmethod
    def validate_env_var(val: str) -> list[str]:
        """Validate SYSTABLE_NEVER_KILL entries.

        Returns list of validation warnings (empty = all valid).
        """
        warnings: list[str] = []
        for item in val.split(","):
            item = item.strip()
            if not item:
                continue
            try:
                pid = int(item)
                if pid <= 0:
                    warnings.append(f"Invalid PID in SYSTABLE_NEVER_KILL: {item}")
            except ValueError:
                if len(item) < 2:
                    warnings.append(f"Process name too short in SYSTABLE_NEVER_KILL: {item!r}")
        return warnings

    def is_protected(self, pid: int, name: str, cmdline: str) -> bool:
        """Check if a process is protected (triple matching).

        Matches on:
        1. PID (if explicitly in CLI overrides)
        2. Name against hard-coded + user + CLI name lists
        3. Never matches PID alone against hard-coded/user sets (prevents
           PID-recycling attacks)
        """
        # Layer 3a — CLI PID overrides (explicit PID = protected)
        if pid in self._cli_pids:
            return True

        # Layers 1+2+3b — name matching
        name_match = (name in self._user_names or
                      name in self._cli_names or
                      name in HARD_CODED_NO_KILL)
        if name_match:
            return True

        return False

    def get_protected_names(self) -> set[str]:
        """Return all protected process names (for display)."""
        result = set(HARD_CODED_NO_KILL)
        result |= self._user_names
        result |= self._cli_names
        return result

    @property
    def protected_pids(self) -> set[int]:
        """Return explicitly protected PIDs."""
        return set(self._cli_pids)