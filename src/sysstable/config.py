"""YAML config loader + defaults for RapidWebs-SysStable."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml


def _config_dir() -> Path:
    return Path.home() / ".config" / "sysstable"


def default_config_path() -> Path:
    return _config_dir() / "config.yaml"


DEFAULT_CONFIG: dict[str, Any] = {
    "interval_seconds": 15,
    "retention_hours": 72,
    "db_path": str(Path.home() / ".cache" / "sysstable" / "metrics.db"),
    "state_path": str(Path.home() / ".hermes" / "plugins" / "rapidwebs-sysstable" / "state.json"),
    "socket_path": str(Path.home() / ".cache" / "sysstable" / "sysstable.sock"),
    "events": {
        "shell_hooks_dir": str(_config_dir() / "hooks.d"),
        "webhooks": [],
        "python_extensions_dir": str(_config_dir() / "extensions.d"),
    },
    "thresholds": {
        "ram_available_mb": {"yellow": 1024, "orange": 512, "red": 256, "critical": 128},
        "cpu_load_15m": {"yellow": 2.0, "red": 4.0},
        "disk_root_free_mb": {"yellow": 5120, "red": 1024},
        "swap_percent": {"yellow": 50, "red": 80},
        "temperature_celsius": {"yellow": 80, "red": 95},
    },
    "memory_pressure": {
        "critical_threshold_mb": 128,
        "confirmation_intervals": 5,
        "countdown_seconds": 90,
        "process_snapshot_interval": 60,
        "normal_snapshot_interval": 300,
        "kill_list_persistence_interval": 5,
        "kill_list_history_max": 50,
    },
    "resolution": {
        "auto_resolve": True,
        "sigterm_timeout_seconds": 10,
        "pause_count": 3,
        "pause_duration_seconds": 10,
        "max_resolution_cycles": 3,
        "min_freed_memory_mb": 64,
        "systemd_managed_services": [],
    },
    "process_scoring": {
        "memory_weight": 0.5,
        "cpu_weight": 0.25,
        "io_weight": 0.15,
        "history_weight": 0.10,
        "max_memory_percent": 50.0,
        "max_cpu_percent": 80.0,
        "max_io_mbps": 100.0,
        "cpu_false_positive_threshold": 5.0,
        "io_false_positive_threshold_mbps": 1.0,
        "false_positive_penalty": 0.5,
        "pinned_processes": [],
    },
    "never_kill": {
        "user_list": [
            "sshd", "cron", "NetworkManager", "rsyslogd",
            "polkitd", "systemd-journald", "login", "dbus-daemon",
            "systemd-logind", "systemd-udevd",
        ],
    },
}


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load YAML config, merging with defaults."""
    config = copy.deepcopy(DEFAULT_CONFIG)
    config_path = Path(path).expanduser() if path else default_config_path()

    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text())
        if raw:
            _deep_merge(config, raw)

    return config


def _deep_merge(base: dict, override: dict) -> None:
    """Recursively merge override into base."""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
