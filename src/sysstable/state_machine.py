"""Pressure State Machine — tracks memory pressure and transitions between states.

States:
  NORMAL → CRITICAL_DETECTED → CONFIRMING → COUNTDOWN → RESOLVING → RECOVERED
                                                                      ↓ (on max retries)
                                                                  MANUAL_INTERVENTION
"""

from __future__ import annotations

import logging
import time
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class PressureState(str, Enum):
    NORMAL = "normal"
    CRITICAL_DETECTED = "critical_detected"
    CONFIRMING = "confirming"
    COUNTDOWN = "countdown"
    RESOLVING = "resolving"
    RECOVERED = "recovered"
    MANUAL_INTERVENTION = "manual_intervention"


class PressureStateMachine:
    """State machine for critical memory pressure lifecycle.

    Tracks counters for: consecutive critical readings, countdown timing,
    recovery cooldown, and resolution retry cycles.
    """

    def __init__(self, config: dict[str, Any]):
        mem_cfg = config.get("memory_pressure", {})
        self.confirmation_intervals = int(mem_cfg.get("confirmation_intervals", 5))
        self.countdown_seconds = int(mem_cfg.get("countdown_seconds", 90))
        self.max_resolution_cycles = int(config.get("resolution", {}).get("max_resolution_cycles", 3))

        self._state = PressureState.NORMAL
        self._critical_counter = 0
        self._countdown_start_ns: int | None = None
        self._recovered_cooldown_until: int | None = None
        self._resolution_cycle_count = 0

    def update(self, ram_available_mb: float,
               critical_threshold_mb: float = 128.0) -> PressureState:
        """Advance the state machine based on current RAM availability.

        Args:
            ram_available_mb: Current free RAM in MB.
            critical_threshold_mb: Below this = critical threshold.

        Returns:
            Current PressureState after transition.
        """
        now_ns = time.time_ns()

        # If in MANUAL_INTERVENTION, stay there
        if self._state == PressureState.MANUAL_INTERVENTION:
            return self._state

        # If in RECOVERED cooldown, wait it out
        if self._state == PressureState.RECOVERED:
            if self._recovered_cooldown_until and now_ns < self._recovered_cooldown_until:
                return self._state
            self._state = PressureState.NORMAL
            self._reset_counters()
            return self._state

        is_critical = ram_available_mb < critical_threshold_mb

        if not is_critical:
            # Recover
            if self._state in (PressureState.CRITICAL_DETECTED,
                               PressureState.CONFIRMING,
                               PressureState.COUNTDOWN):
                self._state = PressureState.NORMAL
                self._reset_counters()
            return self._state

        # Critical RAM detected
        if self._state == PressureState.NORMAL:
            self._state = PressureState.CRITICAL_DETECTED
            self._critical_counter = 1
        elif self._state == PressureState.CRITICAL_DETECTED:
            self._critical_counter += 1
            if self._critical_counter >= self.confirmation_intervals:
                self._state = PressureState.CONFIRMING
        elif self._state == PressureState.CONFIRMING:
            self._state = PressureState.COUNTDOWN
            self._countdown_start_ns = now_ns
        elif self._state == PressureState.COUNTDOWN:
            if self._countdown_start_ns is not None:
                elapsed = (now_ns - self._countdown_start_ns) / 1e9
                if elapsed >= self.countdown_seconds:
                    self._state = PressureState.RESOLVING
        elif self._state == PressureState.RESOLVING:
            pass  # Wait for external on_resolution_complete() call

        return self._state

    def should_fire_resolution(self) -> bool:
        """Returns True when the state machine has timed through the countdown."""
        return self._state == PressureState.RESOLVING

    def on_resolution_complete(self, success: bool) -> None:
        """Called by the resolver after a resolution attempt.

        Args:
            success: True if memory was freed, False if insufficient.
        """
        if success:
            self._state = PressureState.RECOVERED
            now_ns = time.time_ns()
            self._recovered_cooldown_until = now_ns + 60_000_000_000  # 60s cooldown
            self._reset_counters()
        else:
            self._resolution_cycle_count += 1
            if self._resolution_cycle_count >= self.max_resolution_cycles:
                self._state = PressureState.MANUAL_INTERVENTION
                logger.critical(
                    "Memory pressure unresolved after %d cycles — entering MANUAL_INTERVENTION",
                    self.max_resolution_cycles,
                )
            else:
                self._state = PressureState.NORMAL
                logger.warning(
                    "Resolution cycle %d/%d didn't free enough memory — retrying",
                    self._resolution_cycle_count,
                    self.max_resolution_cycles,
                )

    def cancel_resolution(self) -> None:
        """Cancel a pending resolution and return to NORMAL."""
        self._state = PressureState.NORMAL
        self._reset_counters()

    def get_state(self) -> PressureState:
        return self._state

    def get_state_value(self) -> str:
        return self._state.value

    def get_metrics(self) -> dict[str, Any]:
        """Return state info for display/logging."""
        return {
            "state": self._state.value,
            "critical_counter": self._critical_counter,
            "resolution_cycle": self._resolution_cycle_count,
        }

    def _reset_counters(self) -> None:
        self._critical_counter = 0
        self._countdown_start_ns = None