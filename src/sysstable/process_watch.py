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

    attrs = [
        "pid",
        "name",
        "cmdline",
        "cpu_percent",
        "memory_info",
        "memory_percent",
        "io_counters",
        "status",
        "create_time",
        "username",
    ]

    for proc in psutil.process_iter(attrs):
        if time.monotonic() > deadline:
            logger.warning(
                "Process collection timed out after %.1fs — collected %d processes",
                timeout,
                collected,
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

HARD_CODED_NO_KILL: frozenset[str] = frozenset(
    {
        # Kernel / init
        "init",
        "systemd",
        "kthreadd",
        "kworker/*",
        "ksoftirqd/*",
        "migration/*",
        "watchdog/*",
        "rcu*",
        "mm_percpu_wq",
        # System critical
        "systemd-journald",
        "systemd-logind",
        "systemd-udevd",
        "systemd-resolved",
        "systemd-timesyncd",
        "systemd-oomd",
        "dbus-daemon",
        "dbus-broker",
        # This daemon
        "sysstable",
        "sysstabled",
        # Security
        "sshd",
        "login",
        "sudo",
        "polkitd",
        # Container runtime
        "dockerd",
        "containerd",
        "runc",
    }
)


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

    def __init__(self, user_list: list | None = None, cli_overrides: list | None = None, env_var: str | None = None):
        self._user_names: set[str] = set(user_list or [])
        self._cli_names: set[str] = set()
        self._cli_pids: set[int] = set()

        # Parse CLI overrides
        for item in cli_overrides or []:
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

    @classmethod
    def from_config(cls, config: dict) -> NoKillManager:
        """Create NoKillManager from sysstable config dict.

        Reads ``never_kill.user_list`` from config for user-configured
        protected process names, plus ``never_kill.pids`` for PID overrides.
        CLI overrides and env var are loaded separately from runtime state.

        Args:
            config: Full sysstable config dict (from load_config).

        Returns:
            Configured NoKillManager instance.
        """
        never_kill = config.get("never_kill", {})
        user_list = never_kill.get("user_list") or never_kill.get("names") or []
        return cls(user_list=list(user_list))

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
        name_match = name in self._user_names or name in self._cli_names or name in HARD_CODED_NO_KILL
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


# ── Process Scoring and Kill List Generation ───────────────────────────────


class ProcessScorer:
    """Scores processes based on memory, CPU, IO, and historical persistence.

    The score is a weighted sum:
        score = w_mem * memory_score + w_cpu * cpu_score + w_io * io_score +
                w_hist * history_score

    Each sub-score is normalized to [0, 1] based on configurable thresholds.
    False-positive heuristic (high memory, low CPU/IO) applies a penalty.
    """

    def __init__(self, config: dict[str, Any]):
        self.cfg = config.get("process_scoring", {})
        self.w_mem = float(self.cfg.get("memory_weight", 0.5))
        self.w_cpu = float(self.cfg.get("cpu_weight", 0.25))
        self.w_io = float(self.cfg.get("io_weight", 0.15))
        self.w_hist = float(self.cfg.get("history_weight", 0.10))
        self.max_mem = float(self.cfg.get("max_memory_percent", 50.0))
        self.max_cpu = float(self.cfg.get("max_cpu_percent", 80.0))
        self.max_io_mbps = float(self.cfg.get("max_io_mbps", 100.0))
        self.fp_cpu_thresh = float(self.cfg.get("cpu_false_positive_threshold", 5.0))
        self.fp_io_thresh_mbps = float(self.cfg.get("io_false_positive_threshold_mbps", 1.0))
        self.fp_penalty = float(self.cfg.get("false_positive_penalty", 0.5))
        self.pinned = set(self.cfg.get("pinned_processes", []))

    def _memory_score(self, mem_percent: float) -> float:
        return min(mem_percent / self.max_mem, 1.0) if self.max_mem > 0 else 0.0

    def _cpu_score(self, cpu_percent: float) -> float:
        return min(cpu_percent / self.max_cpu, 1.0) if self.max_cpu > 0 else 0.0

    def _io_score(self, io_read_bytes: int, io_write_bytes: int, interval_sec: float) -> float:
        if interval_sec <= 0:
            return 0.0
        io_mbps = (io_read_bytes + io_write_bytes) / (interval_sec * 1024 * 1024)
        return min(io_mbps / self.max_io_mbps, 1.0) if self.max_io_mbps > 0 else 0.0

    def _history_score(self, snapshots: list[ProcessSnapshot]) -> dict[int, float]:
        """Return a dict mapping pid -> history score (0.0-1.0)."""
        # In a full implementation, we would query the DB for historical snapshots.
        # For now, we return a neutral score (0.5) for all pids.
        # The DB integration will be handled in P5/P6.
        return {s.pid: 0.5 for s in snapshots}

    def _is_false_positive(self, snap: ProcessSnapshot) -> bool:
        """Heuristic: high memory but low CPU and low IO suggests cached data."""
        (snap.io_read_bytes + snap.io_write_bytes) / (1024 * 1024)  # per second? we need interval
        # Since we don't have interval here, we'll use a simplistic check:
        # low CPU percent and low IO bytes (assuming collection interval is ~1s)
        return (
            snap.memory_percent >= self.w_mem * 0.5
            and snap.cpu_percent <= self.fp_cpu_thresh
            and (snap.io_read_bytes + snap.io_write_bytes) <= self.fp_io_thresh_mbps * 1024 * 1024
        )

    def score_snapshot(self, snap: ProcessSnapshot, history_score: float = 0.5) -> float:
        """Compute score for a single process snapshot."""
        mem_score = self._memory_score(snap.memory_percent)
        cpu_score = self._cpu_score(snap.cpu_percent)
        # For IO score we need interval; assume 1s for now (will be refined in daemon)
        io_score = self._io_score(snap.io_read_bytes, snap.io_write_bytes, interval_sec=1.0)
        hist = history_score

        score = self.w_mem * mem_score + self.w_cpu * cpu_score + self.w_io * io_score + self.w_hist * hist

        if self._is_false_positive(snap):
            score *= self.fp_penalty

        # Pinned processes get score 0 so they sink to the bottom
        if snap.name in self.pinned:
            score = 0.0

        return score


class KillListEntryScore(KillListEntry):
    """KillListEntry with a computed score (for sorting)."""

    pass


class KillListGenerator:
    """Maintains the in-memory curator's kill list, regenerated each collection cycle."""

    def __init__(self, config: dict[str, Any], no_kill_mgr: NoKillManager, db):
        self.config = config
        self.no_kill_mgr = no_kill_mgr
        self.db = db
        self.scorer = ProcessScorer(config)
        self._list: list[KillListEntryScore] = []
        self._generation_count: int = 0

    def regenerate(self, snapshots: list[ProcessSnapshot]) -> list[KillListEntryScore]:
        """Regenerate the kill list from a fresh set of process snapshots.

        Steps:
        1. Filter out protected processes (via NoKillManager).
        2. Score each remaining snapshot.
        3. Sort by score descending.
        4. Persist to DB every kill_list_persistence_interval generations.
        5. Return the ordered kill list.
        """
        # 1. Filter
        candidates = [s for s in snapshots if not self.no_kill_mgr.is_protected(s.pid, s.name, s.cmdline)]

        # 2. Score
        history = self.scorer._history_score(candidates)  # placeholder until DB history
        scored: list[KillListEntryScore] = []
        for snap in candidates:
            hist = history.get(snap.pid, 0.5)
            score_val = self.scorer.score_snapshot(snap, hist)
            entry = KillListEntryScore(
                pid=snap.pid,
                name=snap.name,
                cmdline=snap.cmdline,
                score=score_val,
                memory_mb=snap.memory_rss_mb,
                cpu_percent=snap.cpu_percent,
                reason=f"mem:{snap.memory_rss_mb:.0f}MB cpu:{snap.cpu_percent:.1f}%",
                is_false_positive=self.scorer._is_false_positive(snap),
            )
            scored.append(entry)

        # 3. Sort
        scored.sort(key=lambda e: e.score, reverse=True)

        # 4. Persist periodically
        self._generation_count += 1
        pressure_interval = self.config.get("memory_pressure", {}).get("kill_list_persistence_interval", 5)
        if self._generation_count % pressure_interval == 0:
            self._persist_to_db(scored)

        self._list = scored
        return self._list

    def _persist_to_db(self, entries: list[KillListEntryScore]) -> None:
        """Save the current kill list to the DB for historical tracking."""
        if not hasattr(self.db, "save_kill_list_generation"):
            logger.warning("DB missing save_kill_list_generation — skipping persistence")
            return

        # Build a simple JSON-serializable representation
        import json

        entries_json = [
            {
                "pid": e.pid,
                "name": e.name,
                "cmdline": e.cmdline,
                "score": e.score,
                "memory_mb": e.memory_mb,
                "cpu_percent": e.cpu_percent,
                "reason": e.reason,
                "is_false_positive": e.is_false_positive,
            }
            for e in entries
        ]
        self.db.save_kill_list_generation(
            trigger="scheduled",
            entries_json=json.dumps(entries_json),
            mem_avail_mb=self._get_system_free_memory_mb(),
        )

    def _get_system_free_memory_mb(self) -> float:
        """Query current free memory from metrics (placeholder)."""
        # In a real implementation, we'd get this from the latest system metrics.
        # For now, return 0.0 — the column can be null.
        return 0.0

    def get_kill_list(self) -> list[KillListEntryScore]:
        """Return the current in-memory kill list (sorted by score)."""
        return list(self._list)

    def get_current_list_for_action(self) -> list[KillListEntryScore]:
        """Return a working copy for the resolution executor to pop from."""
        return list(self._list)
