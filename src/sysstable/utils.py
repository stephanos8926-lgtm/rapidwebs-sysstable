"""Shared utilities for sysstable."""

from __future__ import annotations

from typing import Any


def get_violation_value(metric_name: str, metrics: dict[str, Any]) -> float | None:
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
