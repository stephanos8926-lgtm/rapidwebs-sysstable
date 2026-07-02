"""Event dispatch — shell hooks, webhooks, python extensions."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger("sysstable.events")


def dispatch_events(
    severity: str,
    metric_name: str,
    value: float,
    config: dict[str, Any],
    metrics: dict[str, Any],
) -> list[str]:
    """Dispatch events based on severity.

    Returns list of dispatch results (for logging).
    """
    results: list[str] = []
    events_config = config.get("events", {})

    # Shell hooks
    hooks_dir = events_config.get("shell_hooks_dir", "")
    if hooks_dir:
        hook_path = Path(hooks_dir).expanduser()
        if hook_path.is_dir():
            for script in sorted(hook_path.glob("*")):
                if script.is_file() and script.stat().st_mode & 0o111:
                    try:
                        env = {
                            "SYSSTABLE_SEVERITY": severity,
                            "SYSSTABLE_METRIC": metric_name,
                            "SYSSTABLE_VALUE": str(value),
                            "SYSSTABLE_METRICS_JSON": json.dumps(metrics),
                        }
                        result = subprocess.run(  # noqa: S603 — user-installed hook scripts
                            [str(script)],
                            capture_output=True,
                            text=True,
                            timeout=10,
                            env=env,
                        )
                        results.append(f"hook:{script.name} exit={result.returncode}")
                    except subprocess.TimeoutExpired:
                        results.append(f"hook:{script.name} timeout")

    # Webhooks
    for webhook_url in events_config.get("webhooks", []):
        try:
            import urllib.request

            payload = json.dumps(
                {
                    "severity": severity,
                    "metric": metric_name,
                    "value": value,
                    "config": config.get("thresholds", {}),
                }
            ).encode()
            req = urllib.request.Request(  # noqa: S310 — user-configured webhook URL
                webhook_url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)  # noqa: S310 — user-configured webhook URL
            results.append(f"webhook:{webhook_url} ok")
        except Exception as e:
            results.append(f"webhook:{webhook_url} error:{e}")

    # Python extensions
    ext_dir = events_config.get("python_extensions_dir", "")
    if ext_dir:
        ext_path = Path(ext_dir).expanduser()
        if ext_path.is_dir():
            for pyfile in sorted(ext_path.glob("*.py")):
                try:
                    import importlib.util

                    spec = importlib.util.spec_from_file_location(pyfile.stem, pyfile)
                    if spec and spec.loader:
                        mod = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(mod)
                        if hasattr(mod, "on_event"):
                            mod.on_event(severity=severity, metric=metric_name, value=value, metrics=metrics)
                            results.append(f"ext:{pyfile.name} ok")
                except Exception as e:
                    results.append(f"ext:{pyfile.name} error:{e}")

    return results
