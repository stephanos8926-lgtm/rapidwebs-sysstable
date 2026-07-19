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

import psutil

from . import utils
from .collector import collect
from .config import load_config
from .database import MetricsDB
from .events import dispatch_events
from .pro_balance import ProBalanceScheduler, safe_set_ionice
from .process_watch import (
    KillListGenerator,
    NoKillManager,
    fetch_all_processes,
    snapshot_processes_to_db,
)
from .resolver import MemoryPressureResolver
from .rules_engine import RulesEngine
from .socketd import SocketServer
from .state_machine import PressureState, PressureStateMachine
from .systemd_notify import notify_ready, notify_stopping, notify_watchdog
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
    no_kill_mgr = NoKillManager.from_config(config)
    kill_list_gen = KillListGenerator(config, no_kill_mgr, db)
    state_machine = PressureStateMachine(config)
    resolver = MemoryPressureResolver(config, db)
    mem_cfg = config.get("memory_pressure", {})
    process_snap_interval = int(mem_cfg.get("process_snapshot_interval", 60))
    normal_snap_interval = int(mem_cfg.get("normal_snapshot_interval", 300))
    critical_threshold = float(mem_cfg.get("critical_threshold_mb", 128))
    _last_snap_time = 0  # track snapshot timing by cycle count
    _last_normal_snap_time = 0

    # Initialize new stability/fairness components
    rules_engine = RulesEngine(config)
    pro_balance = ProBalanceScheduler(config, db, no_kill_mgr)

    sock_server = SocketServer(str(socket_path))
    sock_server.start(str(db_path))

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Notify systemd that startup is complete
    notify_ready()

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

                # ── Evaluate Standard Thresholds ────────────────────────────
                threshold_configs = config.get("thresholds", {})
                violations = evaluate_thresholds(metrics_dict, threshold_configs)

                # ── PSI Monitoring (Pressure Stall Information) ─────────────
                psi_cfg = config.get("psi_monitoring", {})
                if psi_cfg.get("enabled", True):
                    psi_cpu_some = metrics_dict.get("psi", {}).get("cpu", {}).get("some", {}).get("avg10", 0.0)
                    psi_mem_some = metrics_dict.get("psi", {}).get("memory", {}).get("some", {}).get("avg10", 0.0)
                    psi_mem_full = metrics_dict.get("psi", {}).get("memory", {}).get("full", {}).get("avg10", 0.0)
                    psi_io_some = metrics_dict.get("psi", {}).get("io", {}).get("some", {}).get("avg10", 0.0)

                    if psi_cpu_some > float(psi_cfg.get("cpu_some_10s_threshold", 40.0)):
                        violations["psi_cpu_stall"] = Severity.ORANGE
                    if psi_mem_full > float(psi_cfg.get("memory_full_10s_threshold", 15.0)):
                        violations["psi_mem_stall"] = Severity.CRITICAL
                    elif psi_mem_some > float(psi_cfg.get("memory_some_10s_threshold", 30.0)):
                        violations["psi_mem_stall"] = Severity.RED
                    if psi_io_some > float(psi_cfg.get("io_some_10s_threshold", 40.0)):
                        violations["psi_io_stall"] = Severity.ORANGE

                # ── Process level monitoring, rules matching, ProBalance, FDs, I/O ──
                # Fetch snapshots (needed for active monitoring)
                snaps = fetch_all_processes(lightweight=False)

                # 1. Rules-Based nice/ionice adjustments
                nice_renice_cfg = config.get("nice_renice", {})
                if nice_renice_cfg.get("enabled", True):
                    for snap in snaps:
                        matched_rule = rules_engine.match_process(snap.pid, snap.name, snap.cmdline)
                        if matched_rule:
                            try:
                                proc = psutil.Process(snap.pid)
                                target_nice = matched_rule.get("nice")
                                if target_nice is not None:
                                    proc.nice(int(target_nice))
                                    snap.nice = int(target_nice)

                                target_ioclass = matched_rule.get("ionice_class")
                                target_ioval = matched_rule.get("ionice_value", 4)
                                if target_ioclass is not None:
                                    safe_set_ionice(proc, int(target_ioclass), int(target_ioval))
                            except Exception:  # noqa: S110
                                pass

                # 2. ProBalance Execution
                if pro_balance.enabled:
                    pb_actions = pro_balance.run_cycle(metrics_dict["cpu"]["percent"], snaps)
                    for act in pb_actions:
                        db.save_resolution_event(
                            action=act["action"],
                            pid=act["pid"],
                            name=act["name"],
                            success=True,
                            details=act["details"]
                        )

                # 3. FD Limit Monitoring
                fd_cfg = config.get("fd_monitoring", {})
                if fd_cfg.get("enabled", True):
                    warning_pct = float(fd_cfg.get("warning_threshold_percent", 80.0))
                    crit_pct = float(fd_cfg.get("critical_threshold_percent", 95.0))
                    default_max_fds = int(fd_cfg.get("default_max_fds_per_process", 1024))
                    action_on_crit = fd_cfg.get("action_on_critical", "kill")

                    for snap in snaps:
                        # Find any process-specific rule overrides for FDs
                        matched_rule = rules_engine.match_process(snap.pid, snap.name, snap.cmdline)
                        max_fds = matched_rule.get("fd_limit", default_max_fds) if matched_rule else default_max_fds

                        pct_used = (snap.num_fds / max_fds * 100.0) if max_fds > 0 else 0.0
                        if pct_used >= crit_pct:
                            violations[f"fd_exhaustion_{snap.name}"] = Severity.CRITICAL
                            logger.critical(
                                "Process %s (PID %d) approaching FD exhaustion: %d/%d open FDs",
                                snap.name, snap.pid, snap.num_fds, max_fds
                            )
                            if action_on_crit == "kill" and not no_kill_mgr.is_protected(snap.pid, snap.name, snap.cmdline):  # noqa: E501
                                try:
                                    p = psutil.Process(snap.pid)
                                    p.terminate()
                                    msg = (
                                        f"FD limits: terminated process {snap.name} (PID {snap.pid}) due to "
                                        f"critical FD usage ({snap.num_fds}/{max_fds})"
                                    )
                                    logger.warning(msg)
                                    db.save_resolution_event(
                                        action="kill_fd_limit",
                                        pid=snap.pid,
                                        name=snap.name,
                                        success=True,
                                        details=msg
                                    )
                                except Exception as err:
                                    logger.error("Failed to terminate process %d on FD limits: %s", snap.pid, err)
                        elif pct_used >= warning_pct:
                            violations[f"fd_leak_warning_{snap.name}"] = Severity.YELLOW
                            logger.warning(
                                "Process %s (PID %d) high FD usage warning: %d/%d open FDs",
                                snap.name, snap.pid, snap.num_fds, max_fds
                            )

                # 4. I/O Allocation Fairness & Starvation Prevention
                io_cfg = config.get("io_monitoring", {})
                if io_cfg.get("enabled", True):
                    default_max_read = float(io_cfg.get("default_max_read_mbps", 50.0))
                    default_max_write = float(io_cfg.get("default_max_write_mbps", 50.0))
                    action_on_crit_io = io_cfg.get("action_on_critical", "ionice")

                    # We assume 1s interval for rates in lightweight loops
                    for snap in snaps:
                        matched_rule = rules_engine.match_process(snap.pid, snap.name, snap.cmdline)
                        max_read = matched_rule.get("io_read_limit_mbps", default_max_read) if matched_rule else default_max_read  # noqa: E501
                        max_write = matched_rule.get("io_write_limit_mbps", default_max_write) if matched_rule else default_max_write  # noqa: E501

                        # Rough rate calculation (since snaps are lightweight or full, we look at bytes)
                        read_mb = snap.io_read_bytes / (1024 * 1024)
                        write_mb = snap.io_write_bytes / (1024 * 1024)

                        if read_mb > max_read or write_mb > max_write:
                            violations[f"io_saturation_{snap.name}"] = Severity.ORANGE
                            logger.warning(
                                "Process %s (PID %d) exceeded I/O limits — read: %.1f/%.1f MB/s, write: %.1f/%.1f MB/s",  # noqa: E501
                                snap.name, snap.pid, read_mb, max_read, write_mb, max_write
                            )
                            if action_on_crit_io == "ionice":
                                try:
                                    proc = psutil.Process(snap.pid)
                                    safe_set_ionice(proc, 3) # Set to IDLE
                                    logger.info("I/O fairness: throttled I/O priority to IDLE for process %s", snap.name)  # noqa: E501
                                except Exception:  # noqa: S110
                                    pass
                            elif action_on_crit_io == "kill" and not no_kill_mgr.is_protected(snap.pid, snap.name, snap.cmdline):  # noqa: E501
                                try:
                                    p = psutil.Process(snap.pid)
                                    p.terminate()
                                    db.save_resolution_event(
                                        action="kill_io_limit",
                                        pid=snap.pid,
                                        name=snap.name,
                                        success=True,
                                        details=f"I/O limit: killed process {snap.name} exceeding limits"
                                    )
                                except Exception:  # noqa: S110
                                    pass

                # Write State JSON (alerts Hermes plugin)
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

                # Dispatch shell hooks & alerts
                for metric_name, severity in violations.items():
                    value = utils.get_violation_value(metric_name, metrics_dict) or 1.0
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
                    elapsed = time.time() - _last_snap_time
                    if elapsed >= process_snap_interval:
                        count = snapshot_processes_to_db(snaps, db)
                        if count:
                            logger.info("Collected %d process snapshots (pressure)", count)
                        kill_list_gen.regenerate(snaps)
                        _last_snap_time = time.time()
                else:
                    elapsed = time.time() - _last_normal_snap_time
                    if normal_snap_interval > 0 and elapsed >= normal_snap_interval:
                        count = snapshot_processes_to_db(snaps, db)
                        if count:
                            logger.debug("Collected %d lightweight snapshots (normal)", count)
                        _last_normal_snap_time = time.time()

                if state_machine.should_fire_resolution():
                    logger.warning("Memory pressure countdown expired — executing resolution")
                    kill_list = kill_list_gen.get_kill_list()
                    result = resolver.resolve(kill_list, ram_avail)
                    logger.info("Resolution result: %s", result.action_summary)
                    state_machine.on_resolution_complete(result.success)
                    # Update state with resolution info
                    state["resolution_active"] = True
                    state["resolution_info"] = {
                        "result": result.action_summary,
                        "kill_count": result.kill_count,
                        "pause_count": result.pause_count,
                        "freed_mb": None,
                    }

                if not foreground:
                    time.sleep(interval)
                elif _RUNNING:
                    time.sleep(interval)

                # Send watchdog ping to systemd (if running under it)
                notify_watchdog()

            except Exception as e:
                logger.error("Collection cycle failed: %s", e, exc_info=True)
                if foreground:
                    time.sleep(interval)
    finally:
        notify_stopping()
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
