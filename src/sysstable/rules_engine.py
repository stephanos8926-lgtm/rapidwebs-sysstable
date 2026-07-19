"""Pattern-matching rules engine for system processes."""

from __future__ import annotations

import difflib
import fnmatch
import logging
import re
import subprocess
from pathlib import Path
from typing import Any

import psutil

logger = logging.getLogger(__name__)


def is_interactive_process(proc: psutil.Process, config: dict[str, Any]) -> float:
    """Determine the interactivity score (0.0 to 1.0) of a process.

    Uses a weighted heuristic based on:
    - Terminal presence
    - Current username (non-system / non-root)
    - Parent process environment/name
    """
    try:
        # Get configuration weights
        nr_cfg = config.get("nice_renice", {})
        w_term = float(nr_cfg.get("interactive_weight_terminal", 0.4))
        w_user = float(nr_cfg.get("interactive_weight_username", 0.3))
        w_parent = float(nr_cfg.get("interactive_weight_parent", 0.3))

        score = 0.0

        # 1. Terminal presence
        try:
            terminal = proc.terminal()
            if terminal:
                score += w_term
        except (psutil.AccessDenied, psutil.NoSuchProcess, AttributeError):
            pass

        # 2. Username check
        try:
            username = proc.username()
            # Non-system username check (generally not root, systemd, or bin)
            if username and username not in ("root", "systemd", "bin", "daemon", "messagebus", "nobody"):
                score += w_user
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass

        # 3. Parent check
        try:
            parent = proc.parent()
            if parent:
                parent_name = parent.name().lower()
                interactive_parents = (
                    "bash",
                    "zsh",
                    "sh",
                    "fish",
                    "tmux",
                    "screen",
                    "gnome-terminal",
                    "xterm",
                    "systemd-logind",
                )
                if any(p in parent_name for p in interactive_parents):
                    score += w_parent
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass

        return min(score, 1.0)

    except Exception:
        return 0.5


class RulesEngine:
    """Matches process snapshots against glob, regex, fuzzy, and plugin patterns."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.rules = config.get("rules", [])
        self.extensions_dir = Path(
            config.get("events", {}).get("python_extensions_dir", "~/.config/sysstable/extensions.d")
        ).expanduser()

    def match_process(self, pid: int, name: str, cmdline: str) -> dict[str, Any] | None:
        """Evaluate a process against rules.

        Returns the matched rule configuration dict, or None.
        Matches in order of preference:
        1. Glob patterns (e.g. firefox*)
        2. Regex patterns (e.g. r"^python(3)?$")
        3. Plugin patterns (e.g. plugin:custom_rule)
        4. Fuzzy matching (e.g. ratio > 0.85)
        """
        for rule in self.rules:
            pattern = rule.get("pattern")
            if not pattern:
                continue

            # Case 1: Plugin pattern
            if pattern.startswith("plugin:"):
                plugin_name = pattern[len("plugin:") :]
                if self._evaluate_plugin_pattern(plugin_name, pid, name, cmdline):
                    return rule

            # Case 2: Regex pattern
            elif pattern.startswith('r"') and pattern.endswith('"'):
                regex_str = pattern[2:-1]
                try:
                    if re.search(regex_str, name) or re.search(regex_str, cmdline):
                        return rule
                except re.error:
                    pass

            # Case 3: Glob patterns
            elif "*" in pattern or "?" in pattern:
                if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(cmdline, pattern):
                    return rule

            # Case 4: Exact or Fuzzy matching
            else:
                if name == pattern:
                    return rule
                # Fuzzy matching ratio > 0.85
                ratio = difflib.SequenceMatcher(None, pattern, name).ratio()
                if ratio > 0.85:
                    return rule

        return None

    def _evaluate_plugin_pattern(self, plugin_name: str, pid: int, name: str, cmdline: str) -> bool:
        """Run an external plugin script to determine if it's a match.

        The plugin receives:
        --pid <pid> --name <name> --cmdline <cmdline>
        If the plugin exits with code 0 or returns "match", it matches.
        """
        # Look in extensions_dir
        plugin_path = self.extensions_dir / plugin_name
        if not plugin_path.exists():
            # Fallback check
            plugin_path = Path("/etc/sysstable/extensions.d") / plugin_name

        if not plugin_path.exists():
            logger.debug("Plugin pattern %s not found at %s", plugin_name, plugin_path)
            return False

        try:
            res = subprocess.run(  # noqa: S603
                [str(plugin_path), "--pid", str(pid), "--name", name, "--cmdline", cmdline],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if res.returncode == 0:
                return True
            stdout = res.stdout.strip().lower()
            if "match" in stdout or "true" in stdout:
                return True
        except Exception as e:
            logger.error("Error executing plugin pattern %s: %s", plugin_name, e)

        return False
