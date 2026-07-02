"""YAML config loader + defaults for RapidWebs-SysStable."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "sysstable" / "config.yaml"

DEFAULT_CONFIG: dict[str, Any] = {
    "interval_seconds": 15,
    "retention_hours": 72,
    "db_path": str(Path.home() / ".cache" / "sysstable" / "metrics.db"),
    "state_path": str(Path.home() / ".hermes" / "plugins" / "rapidwebs-sysstable" / "state.json"),
    "socket_path": str(Path.home() / ".cache" / "sysstable" / "sysstable.sock"),
    "events": {
        "shell_hooks_dir": str(Path.home() / ".config" / "sysstable" / "hooks.d"),
        "webhooks": [],
        "python_extensions_dir": str(Path.home() / ".config" / "sysstable" / "extensions.d"),
    },
    "thresholds": {
        "ram_available_mb": {"yellow": 1024, "orange": 512, "red": 256},
        "cpu_load_15m": {"yellow": 2.0, "red": 4.0},
        "disk_root_free_mb": {"yellow": 5120, "red": 1024},
        "swap_percent": {"yellow": 50, "red": 80},
        "temperature_celsius": {"yellow": 80, "red": 95},
    },
}


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load YAML config, merging with defaults."""
    config = copy.deepcopy(DEFAULT_CONFIG)
    config_path = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH

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
