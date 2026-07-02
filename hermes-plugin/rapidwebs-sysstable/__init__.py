"""rapidwebs-sysstable: Hermes integration plugin — system health awareness.

Registers pre_tool_call and pre_llm_call hooks that read the daemon's
state.json and inject system-pressure context or block delegation when
thresholds are crossed.

Author: RapidWebs (Lucien)
License: MIT
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("rapidwebs-sysstable")

version = "0.1.0"
description = "System stability awareness — reads sysstabled state.json and injects pressure context"
author = "RapidWebs (Lucien)"
license = "MIT"
tags = ["system", "stability", "monitoring", "thresholds", "rapidwebs"]
requirements = []

# Config paths (can be overridden via env vars)
_STATE_PATH = Path(
    os.environ.get(
        "SYSSTABLE_STATE_PATH",
        str(Path.home() / ".hermes" / "plugins" / "rapidwebs-sysstable" / "state.json"),
    )
)
# Threshold severity → behavior
_ORANGE_RETRY_TRACKER: dict[str, bool] = {}


def _read_state() -> dict[str, Any] | None:
    """Read the daemon's current state.json."""
    if not _STATE_PATH.exists():
        return None
    try:
        data = json.loads(_STATE_PATH.read_text())
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.debug("Failed to read sysstable state: %s", e)
        return None


def _severity_to_behavior(severity: str) -> tuple[str, str]:
    """Map severity to (action, message).

    Returns (action, message) where action is one of:
    - 'warn': inject context warning
    - 'soft_block': inject warning, allow retry
    - 'block': block the tool call
    """
    if severity == "green":
        return ("ok", "")
    if severity == "yellow":
        return ("warn", "⚠️  System resource pressure: one or more metrics in YELLOW zone.")
    if severity == "orange":
        return (
            "soft_block",
            "⚠️  System resource pressure in ORANGE zone. "
            "Consider reducing parallel operations. "
            "This call will proceed if retried.",
        )
    if severity == "red":
        return (
            "block",
            "🚨  CRITICAL system pressure — resources are critically low. "
            "Subagent delegation is temporarily blocked. "
            "Reduce system load first, then retry.",
        )
    return ("ok", "")


def _pre_tool_call(**kwargs: Any) -> dict[str, Any]:
    """Check system state before tool calls.

    Blocks delegate_task when system is in CRITICAL state.
    """
    try:
        state = _read_state()
        if not state:
            return {}

        severity = state.get("severity", "green")
        violations = state.get("violations", {})

        if severity == "red":
            return {"block": True, "context": _severity_to_behavior("red")[1]}
        if severity == "orange":
            return {"context": _severity_to_behavior("orange")[1]}
        if severity == "yellow" and violations:
            return {"context": _severity_to_behavior("yellow")[1]}

        return {}
    except Exception as e:
        logger.error("pre_tool_call hook crashed: %s", e, exc_info=True)
        return {}


def _pre_llm_call(**kwargs: Any) -> dict[str, str]:
    """Inject system state context into the prompt."""
    try:
        state = _read_state()
        if not state:
            return {}

        severity = state.get("severity", "green")
        if severity == "green":
            return {}

        violations = state.get("violations", {})
        metrics = state.get("metrics", {})

        lines = ["[SYSTEM STATUS]"]

        if severity == "red":
            lines.append("🚨 CRITICAL — Resources critically low")
        elif severity == "orange":
            lines.append("⚠️  HIGH — System under significant pressure")
        else:
            lines.append("ℹ️  YELLOW — One or more metrics approaching limits")

        for metric, sev in violations.items():
            value = _get_violation_value(metric, metrics)
            lines.append(f"  {sev.upper()}: {metric} = {value}")

        lines.append("[/SYSTEM STATUS]")
        return {"context": "\n".join(lines)}
    except Exception as e:
        logger.error("pre_llm_call hook crashed: %s", e, exc_info=True)
        return {}


def _get_violation_value(metric_name: str, metrics: dict[str, Any]) -> Any:
    """Extract the value for a given violation metric name."""
    if not metrics:
        return None
    if metric_name == "ram_available_mb":
        return metrics.get("ram", {}).get("available_mb")
    if metric_name == "cpu_load_15m":
        return metrics.get("cpu", {}).get("load_15m")
    if metric_name == "disk_root_free_mb":
        for p in metrics.get("disk", {}).get("partitions", []):
            if p.get("mountpoint") == "/":
                return p.get("free_mb")
    if metric_name == "swap_percent":
        return metrics.get("swap", {}).get("percent")
    if metric_name == "temperature_celsius":
        temps = metrics.get("temperatures", {})
        max_t = 0.0
        for entries in temps.values():
            for e in entries:
                max_t = max(max_t, e.get("current", 0))
        return max_t if max_t > 0 else None
    return None


def register(ctx) -> None:
    """Register sysstable hooks."""
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("pre_llm_call", _pre_llm_call)

    state_dir = _STATE_PATH.parent
    state_dir.mkdir(parents=True, exist_ok=True)

    logger.info(
        "rapidwebs-sysstable v%s registered — 2 hooks (pre_tool_call, pre_llm_call)",
        version,
    )
