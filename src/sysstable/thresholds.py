"""Threshold/watermark matching engine."""

from __future__ import annotations

from enum import Enum
from typing import Any


class Severity(str, Enum):
    GREEN = "green"
    YELLOW = "yellow"
    ORANGE = "orange"
    RED = "red"


def evaluate_thresholds(
    metrics: dict[str, Any],
    thresholds_config: dict[str, dict[str, float]],
) -> dict[str, Severity]:
    """Evaluate all metrics against configured thresholds.

    Returns a dict of metric_name → Severity for any thresholds
    that were crossed (green = no crossing, omitted).
    """
    results: dict[str, Severity] = {}

    # RAM available
    if "ram_available_mb" in thresholds_config:
        ram_avail = _get_nested(metrics, "ram", "available_mb")
        sev = _check(ram_avail, thresholds_config["ram_available_mb"], reverse=True)
        if sev:
            results["ram_available_mb"] = sev

    # CPU load 15m
    if "cpu_load_15m" in thresholds_config:
        load = _get_nested(metrics, "cpu", "load_15m")
        sev = _check(load, thresholds_config["cpu_load_15m"])
        if sev:
            results["cpu_load_15m"] = sev

    # Disk root free
    if "disk_root_free_mb" in thresholds_config:
        for part in metrics.get("disk", {}).get("partitions", []):
            if part.get("mountpoint") == "/":
                free = part.get("free_mb", 0)
                sev = _check(free, thresholds_config["disk_root_free_mb"], reverse=True)
                if sev:
                    results["disk_root_free_mb"] = sev
                break

    # SWAP percent
    if "swap_percent" in thresholds_config:
        pct = _get_nested(metrics, "swap", "percent")
        sev = _check(pct, thresholds_config["swap_percent"])
        if sev:
            results["swap_percent"] = sev

    # Temperature
    if "temperature_celsius" in thresholds_config:
        max_temp = 0.0
        for sensor_entries in metrics.get("temperatures", {}).values():
            for entry in sensor_entries:
                max_temp = max(max_temp, entry.get("current", 0))
        sev = _check(max_temp, thresholds_config["temperature_celsius"])
        if sev:
            results["temperature_celsius"] = sev

    # RAM percent (derived, not in defaults but user can add)
    if "ram_percent" in thresholds_config:
        pct = _get_nested(metrics, "ram", "percent")
        sev = _check(pct, thresholds_config["ram_percent"])
        if sev:
            results["ram_percent"] = sev

    return results


def _check(value: float | None, levels: dict[str, float], reverse: bool = False) -> Severity | None:
    """Check a single value against yellow/orange/red thresholds.

    reverse=True means lower is worse (e.g., available RAM, free disk).
    reverse=False means higher is worse (e.g., CPU load, swap %).
    """
    if value is None:
        return None

    red = levels.get("red")
    orange = levels.get("orange")
    yellow = levels.get("yellow")

    if reverse:
        if red is not None and value <= red:
            return Severity.RED
        if orange is not None and value <= orange:
            return Severity.ORANGE
        if yellow is not None and value <= yellow:
            return Severity.YELLOW
    else:
        if red is not None and value >= red:
            return Severity.RED
        if orange is not None and value >= orange:
            return Severity.ORANGE
        if yellow is not None and value >= yellow:
            return Severity.YELLOW

    return None


def _get_nested(d: dict[str, Any], *keys: str) -> float | None:
    """Safely traverse nested dict."""
    current: Any = d
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return None
    return float(current) if current is not None else None
