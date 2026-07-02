"""Daemon — main collection loop, state output, event dispatch."""

from __future__ import annotations

import json
import logging
import signal
import time
from pathlib import Path
from typing import Any

from .collector import collect
from .config import load_config
from .database import MetricsDB
from .events import dispatch_events
from .thresholds import Severity, evaluate_thresholds

logger = logging.getLogger("sysstable.daemon")
_RUNNING = True


def _handle_signal(signum: int, _frame: Any) -> None:
    global _RUNNING
    _RUNNING = False
    logger.info("Signal %d received, shutting down", signum)


def run_daemon(config_path: str | None = None, foreground: bool = False) -> None:
    """Main daemon loop.

    Args:
        config_path: Override path to config YAML.
        foreground: If True, run in foreground (don't daemonize).
    """
    config = load_config(config_path)
    interval = config.get("interval_seconds", 15)
    retention = config.get("retention_hours", 72)

    db_path = Path(config.get("db_path", "")).expanduser()
    state_path = Path(config.get("state_path", "")).expanduser()

    db = MetricsDB(str(db_path))

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info(
        "sysstabled starting — interval=%ds, retention=%dh, db=%s",
        interval,
        retention,
        db_path,
    )

    cycle = 0
    while _RUNNING:
        try:
            metrics = collect()
            metrics_dict = metrics.to_dict()

            # Write to SQLite
            db.write(metrics_dict)

            # Prune old data (every 10 cycles)
            cycle += 1
            if cycle % 10 == 0:
                pruned = db.prune(retain_hours=retention)
                if pruned:
                    logger.info("Pruned %d old metric records", pruned)

            # Evaluate thresholds
            threshold_configs = config.get("thresholds", {})
            violations = evaluate_thresholds(metrics_dict, threshold_configs)

            # Write state.json
            state = {
                "timestamp": metrics_dict["timestamp"],
                "metrics": metrics_dict,
                "violations": {k: v.value for k, v in violations.items()},
                "severity": _overall_severity(violations),
            }
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps(state, indent=2))

            # Dispatch events for violations
            for metric_name, severity in violations.items():
                value = _get_violation_value(metric_name, metrics_dict)
                if value is not None:
                    results = dispatch_events(
                        severity.value,
                        metric_name,
                        value,
                        config,
                        metrics_dict,
                    )
                    if results:
                        logger.info("Events dispatched for %s=%s: %s", metric_name, severity.value, results)

            if not foreground:
                time.sleep(interval)
            elif _RUNNING:
                # In foreground mode, loop doesn't sleep — runs continuously
                time.sleep(interval)

        except Exception as e:
            logger.error("Collection cycle failed: %s", e, exc_info=True)
            if foreground:
                time.sleep(interval)

    db.close()
    logger.info("sysstabled stopped")


def _overall_severity(violations: dict[str, Severity]) -> str:
    """Get the highest severity from all violations."""
    if any(v == Severity.RED for v in violations.values()):
        return Severity.RED.value
    if any(v == Severity.ORANGE for v in violations.values()):
        return Severity.ORANGE.value
    if any(v == Severity.YELLOW for v in violations.values()):
        return Severity.YELLOW.value
    return Severity.GREEN.value


def _get_violation_value(metric_name: str, metrics: dict[str, Any]) -> float | None:
    """Extract the numeric value that triggered a violation."""
    if metric_name == "ram_available_mb":
        return metrics.get("ram", {}).get("available_mb")
    if metric_name == "cpu_load_15m":
        return metrics.get("cpu", {}).get("load_15m")
    if metric_name == "disk_root_free_mb":
        for part in metrics.get("disk", {}).get("partitions", []):
            if part.get("mountpoint") == "/":
                return part.get("free_mb")
    if metric_name == "swap_percent":
        return metrics.get("swap", {}).get("percent")
    if metric_name == "temperature_celsius":
        max_temp = 0.0
        for entries in metrics.get("temperatures", {}).values():
            for entry in entries:
                max_temp = max(max_temp, entry.get("current", 0))
        return max_temp if max_temp > 0 else None
    return None
