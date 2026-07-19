"""Process Lasso / ProBalance & Nice-Renice Algorithms."""

from __future__ import annotations

import logging
import time
from typing import Any

import psutil

from .database import MetricsDB
from .process_watch import NoKillManager
from .rules_engine import is_interactive_process

logger = logging.getLogger(__name__)


def safe_set_ionice(proc: psutil.Process, ioclass: int, value: int = 4) -> bool:
    """Set process I/O priority safely."""
    try:
        if hasattr(proc, "ionice"):
            # IOPRIO_CLASS_IDLE does not take a value parameter in psutil/Linux
            if ioclass == 3:
                proc.ionice(3)
            else:
                proc.ionice(ioclass, value)
            return True
    except Exception:  # noqa: S110
        pass
    return False


def safe_get_ionice(proc: psutil.Process) -> tuple[int, int]:
    """Get process I/O priority safely."""
    try:
        if hasattr(proc, "ionice"):
            io = proc.ionice()
            if isinstance(io, tuple):
                return io[0], io[1]
            if hasattr(io, "ioclass"):
                return getattr(io, "ioclass"), getattr(io, "value", 0)
            return int(io), 0
    except Exception:  # noqa: S110
        pass
    return 0, 0


class ProBalanceScheduler:
    """ProBalance priority adjuster for background resource hoggers.

    Tracks adjusted processes to restore their priority once load settles down
    or the process is no longer behaving as a bad actor.
    """

    def __init__(self, config: dict[str, Any], db: MetricsDB, no_kill_mgr: NoKillManager):
        self.config = config
        self.db = db
        self.no_kill_mgr = no_kill_mgr
        self.pb_cfg = config.get("pro_balance", {})
        self.enabled = self.pb_cfg.get("enabled", True)

        self.system_cpu_threshold = float(self.pb_cfg.get("system_cpu_threshold_percent", 85.0))
        self.process_cpu_threshold = float(self.pb_cfg.get("process_cpu_threshold_percent", 20.0))
        self.renice_value = int(self.pb_cfg.get("renice_value", 10))
        self.restore_delay = float(self.pb_cfg.get("restore_delay_seconds", 15))

        # adjusted_processes maps pid -> {
        #   "original_nice": int,
        #   "original_ionice": (ioclass, value),
        #   "last_seen_hogging": float,
        #   "name": str,
        #   "cmdline": str
        # }
        self.adjusted_processes: dict[int, dict[str, Any]] = {}

    def get_process_history_weight(self, name: str, pid: int, db: MetricsDB) -> float:
        """Calculate history weight for a process based on previous kills and CPU usage.

        Returns a score between 0.0 and 1.0.
        """
        weight = 0.0
        try:
            # 1. Check previous kills/pauses in resolution_events
            if hasattr(db, "conn"):
                row = db.conn.execute(
                    "SELECT COUNT(*) as c FROM resolution_events WHERE name = ? AND action IN ('kill', 'pause')",
                    (name,),
                ).fetchone()
                if row and row["c"] > 0:
                    # Each previous intervention adds to bad-actor rating
                    weight += min(row["c"] * 0.15, 0.4)

            # 2. Check historical CPU usage across multiple time windows (e.g. 5m, 1h)
            # Use process_snapshots table
            if hasattr(db, "conn"):
                now_ns = time.time_ns()
                five_min_ago = now_ns - (300 * 1_000_000_000)
                row_5m = db.conn.execute(
                    "SELECT AVG(cpu_percent) as avg_cpu FROM process_snapshots WHERE name = ? AND timestamp_ns >= ?",
                    (name, five_min_ago),
                ).fetchone()
                if row_5m and row_5m["avg_cpu"] is not None:
                    if row_5m["avg_cpu"] > 40.0:
                        weight += 0.3
                    elif row_5m["avg_cpu"] > 20.0:
                        weight += 0.15

                one_hour_ago = now_ns - (3600 * 1_000_000_000)
                row_1h = db.conn.execute(
                    "SELECT AVG(cpu_percent) as avg_cpu FROM process_snapshots WHERE name = ? AND timestamp_ns >= ?",
                    (name, one_hour_ago),
                ).fetchone()
                if row_1h and row_1h["avg_cpu"] is not None:
                    if row_1h["avg_cpu"] > 30.0:
                        weight += 0.3
                    elif row_1h["avg_cpu"] > 15.0:
                        weight += 0.15

        except Exception as e:
            logger.debug("Error computing process history weight for %s: %s", name, e)

        return min(weight, 1.0)

    def run_cycle(self, system_cpu_percent: float, running_processes: list[Any]) -> list[dict[str, Any]]:
        """Run one ProBalance adjustment and restoration cycle.

        Returns a list of logged action details.
        """
        actions = []
        if not self.enabled:
            return actions

        now = time.time()
        active_pids = set()

        # ── Step 1: Detect and Renice Bad Actors under High CPU Load ───────────────────
        is_system_hogged = system_cpu_percent >= self.system_cpu_threshold

        for proc_snap in running_processes:
            pid = proc_snap.pid
            name = proc_snap.name
            cmdline = proc_snap.cmdline
            cpu_percent = proc_snap.cpu_percent

            # Skip protected / system processes
            if self.no_kill_mgr.is_protected(pid, name, cmdline):
                continue

            try:
                proc = psutil.Process(pid)
                active_pids.add(pid)

                # Skip interactive / foreground processes (heuristic score > 0.6)
                interactivity = is_interactive_process(proc, self.config)
                if interactivity > 0.6:
                    continue

                # Factor in the history score of this process
                hist_weight = self.get_process_history_weight(name, pid, self.db)

                # Condition for ProBalance triggering:
                # Process CPU > threshold OR (process CPU > half of
                # threshold AND process is a known historical bad actor)
                is_process_hogging = (cpu_percent >= self.process_cpu_threshold) or (
                    cpu_percent >= (self.process_cpu_threshold * 0.5) and hist_weight > 0.3
                )

                if is_system_hogged and is_process_hogging:
                    # Deprioritize!
                    if pid not in self.adjusted_processes:
                        orig_nice = proc.nice()
                        orig_ioclass, orig_ioval = safe_get_ionice(proc)

                        # Calculate new priority target
                        target_nice = min(orig_nice + self.renice_value, 19)
                        # More aggressive nice for historical bad-actors
                        if hist_weight > 0.5:
                            target_nice = min(target_nice + 2, 19)

                        # Set Nice
                        proc.nice(target_nice)
                        # Set I/O Priority to IDLE
                        safe_set_ionice(proc, 3)

                        self.adjusted_processes[pid] = {
                            "original_nice": orig_nice,
                            "original_ionice": (orig_ioclass, orig_ioval),
                            "last_seen_hogging": now,
                            "name": name,
                            "cmdline": cmdline,
                        }

                        msg = (
                            f"ProBalance: deprioritized runaway background process {name} (PID {pid}) — "
                            f"CPU: {cpu_percent:.1f}%, nice: {orig_nice}->{target_nice}"
                        )
                        logger.info(msg)
                        actions.append({"action": "pro_balance_deprioritize", "pid": pid, "name": name, "details": msg})
                    else:
                        # Update last seen hogging time
                        self.adjusted_processes[pid]["last_seen_hogging"] = now

            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        # ── Step 2: Restore priority of processes that settled down ────────────────────
        pids_to_restore = []
        for pid, info in self.adjusted_processes.items():
            # If the process is dead, just clean up tracker
            if pid not in active_pids:
                pids_to_restore.append(pid)
                continue

            # If it has not been seen hogging within the restore delay, restore it
            if now - info["last_seen_hogging"] >= self.restore_delay:
                try:
                    proc = psutil.Process(pid)
                    # Verify name/cmdline to avoid PID recycling
                    if proc.name() == info["name"]:
                        proc.nice(info["original_nice"])
                        ioclass, ioval = info["original_ionice"]
                        safe_set_ionice(proc, ioclass, ioval)

                        msg = (
                            f"ProBalance: restored priority of settled process {info['name']} (PID {pid}) — "
                            f"nice: {info['original_nice']}"
                        )
                        logger.info(msg)
                        actions.append(
                            {"action": "pro_balance_restore", "pid": pid, "name": info["name"], "details": msg}
                        )
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
                pids_to_restore.append(pid)

        for pid in pids_to_restore:
            self.adjusted_processes.pop(pid, None)

        return actions
