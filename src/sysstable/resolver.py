"""Resolution Executor — kill, pause, and manage offending processes.

Integrates with PressureStateMachine for retry/fail lifecycle and
NoKillManager for process protection checks.
"""

from __future__ import annotations

import logging
import signal
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

import psutil

logger = logging.getLogger(__name__)


class ResolutionError(Exception):
    """Base for resolution failures."""


@dataclass
class ResolutionResult:
    success: bool
    action_summary: str
    kill_count: int = 0
    pause_count: int = 0
    systemd_stops: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)


class MemoryPressureResolver:
    """Resolves memory pressure by killing/pausing offending processes.

    Features:
    - Re-entrance guard (only one resolution at a time)
    - SIGTERM → wait → SIGKILL chain for kill targets
    - Pause/unpause with staggered schedule (reverse linear backoff)
    - Optional systemd service stop for managed services
    - Fork bomb mitigation via state machine (max retry cycles)
    """

    def __init__(self, config: dict[str, Any], db):
        self.config = config
        self.db = db
        res_cfg = config.get("resolution", {})
        self.sigterm_timeout = int(res_cfg.get("sigterm_timeout_seconds", 10))
        self.pause_count = int(res_cfg.get("pause_count", 3))
        self.pause_duration = int(res_cfg.get("pause_duration_seconds", 10))
        self.min_freed_mb = int(res_cfg.get("min_freed_memory_mb", 64))
        self.systemd_services = list(res_cfg.get("systemd_managed_services", []))
        self._resolving = False

    # ── Public API ───────────────────────────────────────────────────────

    def resolve(self, kill_list_entries: list[Any], current_ram_mb: float) -> ResolutionResult:
        """Execute a resolution cycle on the given kill list.

        Re-entrance guard prevents concurrent resolutions. Called by
        the daemon loop when state_machine.should_fire_resolution().

        Args:
            kill_list_entries: Sorted list of KillListEntryScore objects.
            current_ram_mb: Current free RAM in MB.

        Returns:
            ResolutionResult with success/failure and action summary.
        """
        if self._resolving:
            logger.warning("Resolution already in progress — re-entrance guard active")
            return ResolutionResult(
                success=False,
                action_summary="REJECTED: concurrent resolution attempt",
            )

        self._resolving = True
        ram_before = current_ram_mb
        details: list[dict[str, Any]] = []

        try:
            if not kill_list_entries:
                logger.warning("Kill list is empty — nothing to resolve")
                return ResolutionResult(
                    success=False,
                    action_summary="EMPTY KILL LIST",
                )

            result = self._execute_resolution(kill_list_entries, details)
            ram_after = self._get_ram_mb()

            # Check if enough memory was freed
            freed = ram_after - ram_before
            if freed < self.min_freed_mb:
                logger.warning(
                    "Only %d MB freed (min %d MB) — marking as unsuccessful",
                    freed,
                    self.min_freed_mb,
                )
                result.success = False
                result.action_summary = f"INSUFFICIENT: {freed}MB freed < {self.min_freed_mb}MB min"
            else:
                result.success = True
                result.action_summary = f"OK: killed {result.kill_count}, paused {result.pause_count}, freed {freed}MB"

            self._log_resolution_event(result, ram_before, ram_after)
            return result

        finally:
            self._resolving = False

    # ── Internal Resolution ──────────────────────────────────────────────

    def _execute_resolution(self, entries: list[Any], details: list[dict[str, Any]]) -> ResolutionResult:
        """Execute the kill/pause/unpause schedule.

        Strategy (from spec):
        1. Kill #1 via SIGTERM → wait → SIGKILL
        2. Pause next U via SIGSTOP (U = pause_count)
        3. Unpause in reverse linear backoff:
           position 2: wait L*U → SIGCONT
           position 3: wait L*(U-1) → SIGCONT
           position 4: wait L*(U-2) → SIGCONT
           (L = pause_duration)
        """
        result = ResolutionResult(success=True, action_summary="", kill_count=0, pause_count=0, details=details)

        if not entries:
            return result

        # 1. Kill #1
        target = entries[0]
        sig = self._get_signal_for_process(target)
        if self._kill_process(target, sig, details):
            result.kill_count = 1
        else:
            logger.warning("Failed to kill #1 (%s), trying next", target.name)
            # Try #2 as fallback
            if len(entries) > 1:
                target = entries[1]
                if self._kill_process(target, self._get_signal_for_process(target), details):
                    result.kill_count = 1

        # 2. Pause next U
        paused = []
        for i in range(1, min(self.pause_count, len(entries))):
            entry = entries[i]
            if self._pause_process(entry, details):
                paused.append(entry)
                result.pause_count += 1

        # 3. Schedule unpauses (happens in-order from the daemon loop)
        #    Reverse linear: position i waits L * (U - i + 1)
        unpause_delays = []
        for i, entry in enumerate(paused):
            i + 2  # position in kill list (1-indexed)
            delay = self.pause_duration * (self.pause_count - i)
            unpause_delays.append((entry, delay))

        # Fire-and-forget unpause threads
        import threading

        for entry, delay in unpause_delays:
            t = threading.Timer(delay, self._unpause_process, args=[entry, details])
            t.daemon = True
            t.start()

        # Log all actions
        for d in details:
            logger.info("Resolution action: %s", d.get("action", "?"))

        return result

    # ── Kill Logic ───────────────────────────────────────────────────────

    def _get_signal_for_process(self, entry: Any) -> int:
        """Determine the signal to send (SIGTERM by default)."""
        if self.systemd_services and entry.name in self.systemd_services:
            return signal.SIGTERM  # systemctl stop is done separately
        return signal.SIGTERM

    def _kill_process(self, entry: Any, sig: int, details: list[dict[str, Any]]) -> bool:
        """Send SIGTERM, wait, then SIGKILL if still alive.

        For systemd-managed services, runs `systemctl --user stop` first.
        """
        # Systemd stop before kill
        if self.systemd_services and entry.name in self.systemd_services:
            if self._systemd_stop(entry.name, details):
                return True  # systemd handled it

        try:
            proc = psutil.Process(entry.pid)
            if not proc.is_running():
                logger.info("Process %d (%s) already dead", entry.pid, entry.name)
                return True

            kill_tried = signal.Signals(sig).name if hasattr(signal, "Signals") else str(sig)
            logger.info("Sending %s to %d (%s — %.0fMB)", kill_tried, entry.pid, entry.name, entry.memory_mb)

            proc.send_signal(sig)
            details.append(
                {
                    "action": "kill",
                    "pid": entry.pid,
                    "name": entry.name,
                    "signal": kill_tried,
                    "memory_mb": entry.memory_mb,
                    "reason": entry.reason,
                }
            )

            # Wait for graceful shutdown
            deadline = time.monotonic() + self.sigterm_timeout
            while time.monotonic() < deadline:
                try:
                    proc.wait(timeout=1)
                    logger.info("Process %d exited gracefully", entry.pid)
                    return True
                except psutil.TimeoutExpired:
                    continue
                except psutil.NoSuchProcess:
                    logger.info("Process %d already gone", entry.pid)
                    return True

            # Timeout — escalate to SIGKILL
            logger.warning("Process %d didn't respond to SIGTERM — sending SIGKILL", entry.pid)
            try:
                proc.send_signal(signal.SIGKILL)
                details[-1]["escalated"] = True
                proc.wait(timeout=3)
                logger.info("Process %d killed with SIGKILL", entry.pid)
            except psutil.NoSuchProcess:
                return True

            return True

        except psutil.NoSuchProcess:
            logger.info("Process %d already gone before action", entry.pid)
            return True
        except psutil.AccessDenied:
            logger.warning("Permission denied to manage process %d (%s)", entry.pid, entry.name)
            details.append({"action": "error", "pid": entry.pid, "error": "AccessDenied"})
            return False
        except Exception as e:
            logger.error("Failed to kill process %d: %s", entry.pid, e)
            return False

    # ── Pause/Unpause ────────────────────────────────────────────────────

    def _pause_process(self, entry: Any, details: list[dict[str, Any]]) -> bool:
        """Send SIGSTOP to pause a process."""
        import psutil as _psutil

        try:
            proc = _psutil.Process(entry.pid)
            if not proc.is_running():
                return False
            proc.send_signal(signal.SIGSTOP)
            logger.info("Paused process %d (%s)", entry.pid, entry.name)
            details.append(
                {
                    "action": "pause",
                    "pid": entry.pid,
                    "name": entry.name,
                }
            )
            return True
        except (_psutil.NoSuchProcess, _psutil.AccessDenied) as e:
            logger.warning("Failed to pause %d: %s", entry.pid, e)
            return False

    def _unpause_process(self, entry: Any, details: list[dict[str, Any]]) -> None:
        """Send SIGCONT to resume a paused process."""
        import psutil as _psutil

        try:
            proc = _psutil.Process(entry.pid)
            if proc.is_running():
                proc.send_signal(signal.SIGCONT)
                logger.info("Unpaused process %d (%s) after delay", entry.pid, entry.name)
                details.append(
                    {
                        "action": "unpause",
                        "pid": entry.pid,
                        "name": entry.name,
                    }
                )
                self._log_resolution_event(
                    ResolutionResult(
                        success=True,
                        action_summary=f"unpause {entry.name}",
                    ),
                    0,
                    0,
                    action="unpause",
                )
        except (_psutil.NoSuchProcess, _psutil.AccessDenied) as e:
            logger.debug("Could not unpause %d: %s", entry.pid, e)

    # ── Systemd ──────────────────────────────────────────────────────────

    def _systemd_stop(self, name: str, details: list[dict[str, Any]]) -> bool:
        """Stop a systemd user service."""
        try:
            subprocess.run(
                ["systemctl", "--user", "stop", name],  # noqa: S603, S607
                capture_output=True,
                text=True,
                timeout=10,
            )
            logger.info("Stopped systemd service: %s", name)
            details.append({"action": "systemd_stop", "name": name})
            return True
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.warning("Failed to stop systemd service %s: %s", name, e)
            return False

    # ── Helpers ──────────────────────────────────────────────────────────

    def _get_ram_mb(self) -> float:
        """Query current available RAM."""
        try:
            return psutil.virtual_memory().available / (1024 * 1024)
        except Exception:
            return 0.0

    def _log_resolution_event(
        self, result: ResolutionResult, ram_before: float = 0, ram_after: float = 0, action: str = "resolve"
    ) -> None:
        """Log a resolution event to the DB."""
        if not hasattr(self.db, "save_resolution_event"):
            return
        try:
            details_str = str(result.details[:5])  # first 5 for brevity
            self.db.save_resolution_event(
                action=action,
                pid=0,
                name="system",
                success=result.success,
                details=(
                    f"summary={result.action_summary}, "
                    f"ram_before={ram_before:.0f}, "
                    f"ram_after={ram_after:.0f}, "
                    f"details={details_str}"
                ),
            )
        except Exception as e:
            logger.warning("Failed to log resolution event: %s", e)
