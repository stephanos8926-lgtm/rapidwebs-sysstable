"""Daemon — main collection loop, state output, event dispatch."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from . import utils
from .collector import collect
from .config import load_config
from .database import MetricsDB
from .events import dispatch_events
from .process_watch import (
    KillListGenerator,
    NoKillManager,
    fetch_all_processes,
    snapshot_processes_to_db,
)
from .socketd import SocketServer
from .state_machine import PressureState, PressureStateMachine
from .thresholds import Severity, evaluate_thresholds

logger = logging.getLogger("sysstable.daemon")
_RUNNING = True
_PID_PATH = Path.home() / ".cache" / "sysstable" / "sysstabled.pid"


def _handle_signal(signum: int, _frame: Any) -> None:
    global _RUNNING
    _RUNNING = False
    logger.info("Signal %d received, shutting down", signum)


def _check_and_write_pid() -> bool:
    """Check if daemon is already running. Returns True if OK to start."""
    _PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    if _PID_PATH.exists():
        try:
            old_pid = int(_PID_PATH.read_text().strip())
            os.kill(old_pid, 0)
            logger.warning("Daemon already running (PID %d)", old_pid)
            return False
        except (ProcessLookupError, OSError):
            pass
        except (ValueError, OSError):
            pass
    _PID_PATH.write_text(str(os.getpid()))
    return True


def _cleanup_pid() -> None:
    """Remove PID file on shutdown."""
    try:
        if _PID_PATH.exists():
            _PID_PATH.unlink()
    except OSError:
        pass


def _setup_logging() -> None:
    """Set up rotating file logging for the daemon."""
    log_dir = Path.home() / ".cache" / "sysstable" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "sysstabled.log"

    file_handler = logging.handlers.RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=3)
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root_logger = logging.getLogger()
    root_logger.addHandler(file_handler)
    root_logger.setLevel(logging.INFO)
    logger.info("Logging to %s", log_path)


def run_daemon(config_path: str | None = None, foreground: bool = False) -> None:
    """Main daemon loop.

    Args:
        config_path: Override path to config YAML.
        foreground: If True, run in foreground (don't daemonize).
    """
    _setup_logging()

    if not _check_and_write_pid():
        logger.error("Another daemon instance is running. Exiting.")
        sys.exit(1)

    config = load_config(config_path)
    interval = config.get("interval_seconds", 15)
    retention = config.get("retention_hours", 72)

    db_path = Path(config.get("db_path", "")).expanduser()
    state_path = Path(config.get("state_path", "")).expanduser()

    db = MetricsDB(str(db_path))
    socket_path = Path(config.get("socket_path", "")).expanduser()

    # Initialize memory pressure subsystems
    no_kill_mgr = NoKillManager(user_list=config.get("never_kill", {}).get("user_list"))
    kill_list_gen = KillListGenerator(config, no_kill_mgr, db)
    state_machine = PressureStateMachine(config)
    mem_cfg = config.get("memory_pressure", {})
    process_snap_interval = int(mem_cfg.get("process_snapshot_interval", 60))
    normal_snap_interval = int(mem_cfg.get("normal_snapshot_interval", 300))
    critical_threshold = float(mem_cfg.get("critical_threshold_mb", 128))
    _last_snap_time = 0  # track snapshot timing by cycle count
    _last_normal_snap_time = 0

    sock_server = SocketServer(str(socket_path))
    sock_server.start(str(db_path))

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info(
        "sysstabled starting — interval=%ds, retention=%dh, db=%s, socket=%s",
        interval,
        retention,
        db_path,
        socket_path,
    )

    cycle = 0
    try:
        while _RUNNING:
            try:
                metrics = collect()
                metrics_dict = metrics.to_dict()

                db.write(metrics_dict)

                cycle += 1
                if cycle % 10 == 0:
                    pruned = db.prune(retain_hours=retention)
                    if pruned:
                        logger.info("Pruned %d old metric records", pruned)

                threshold_configs = config.get("thresholds", {})
                violations = evaluate_thresholds(metrics_dict, threshold_configs)

                state = {
                    "timestamp": metrics_dict["timestamp"],
                    "metrics": metrics_dict,
                    "violations": {k: v.value for k, v in violations.items()},
                    "severity": _overall_severity(violations),
                    "resolution_active": False,
                    "resolution_info": None,
                }
                state_path.parent.mkdir(parents=True, exist_ok=True)
                state_path.write_text(json.dumps(state, indent=2))

                for metric_name, severity in violations.items():
                    value = utils.get_violation_value(metric_name, metrics_dict)
                    if value is not None:
                        results = dispatch_events(
                            severity.value,
                            metric_name,
                            value,
                            config,
                            metrics_dict,
                        )
                        if results:
                            logger.info(
                                "Events dispatched for %s=%s: %s",
                                metric_name,
                                severity.value,
                                results,
                            )

                # ── Memory Pressure Resolution ────────────────────────────
                ram_avail = utils.get_violation_value("ram_available_mb", metrics_dict) or 9999.0
                current_state = state_machine.update(ram_avail, critical_threshold)

                # Snapshot collection (on timer, not every cycle)
                if current_state != PressureState.NORMAL:
                    elapsed = (time.time() - _last_snap_time) * 1000
                    if elapsed >= process_snap_interval:
                        snaps = fetch_all_processes(lightweight=False)
                        count = snapshot_processes_to_db(snaps, db)
                        if count:
                            logger.info("Collected %d process snapshots (pressure)", count)
                        kill_list_gen.regenerate(snaps)
                        _last_snap_time = time.time()
                else:
                    elapsed = (time.time() - _last_normal_snap_time) * 1000
                    rate = normal_snap_interval * 1000  # convert seconds to ms
                    if rate > 0 and elapsed >= rate:
                        snaps = fetch_all_processes(lightweight=True)
                        count = snapshot_processes_to_db(snaps, db)
                        if count:
                            logger.debug("Collected %d lightweight snapshots (normal)", count)
                        _last_normal_snap_time = time.time()

                if state_machine.should_fire_resolution():
                    logger.warning("Memory pressure countdown expired — ready for resolution")
                    # P8 resolver will be wired here when built

                if not foreground:
                    time.sleep(interval)
                elif _RUNNING:
                    time.sleep(interval)

            except Exception as e:
                logger.error("Collection cycle failed: %s", e, exc_info=True)
                if foreground:
                    time.sleep(interval)
    finally:
        sock_server.stop()
        _cleanup_pid()
        db.close()

    logger.info("sysstabled stopped")


def _overall_severity(violations: dict[str, Severity]) -> str:
    """Get the highest severity from all violations."""
    if any(v == Severity.CRITICAL for v in violations.values()):
        return Severity.CRITICAL.value
    if any(v == Severity.RED for v in violations.values()):
        return Severity.RED.value
    if any(v == Severity.ORANGE for v in violations.values()):
        return Severity.ORANGE.value
    if any(v == Severity.YELLOW for v in violations.values()):
        return Severity.YELLOW.value
    return Severity.GREEN.value
