"""Tests for the sysstable monitor daemon core logic."""
import pytest
import time
from unittest.mock import patch, MagicMock

from src.sysstable.state_machine import PressureStateMachine, PressureState
from src.sysstable.resolver import MemoryPressureResolver, ResolutionResult
from src.sysstable.process_watch import (
    NoKillManager,
    ProcessScorer,
    KillListGenerator,
    KillListEntry,
    ProcessSnapshot,
)
from src.sysstable.thresholds import evaluate_thresholds, Severity


class TestPressureStateMachine:
    """Tests for NORMAL→CRITICAL→CONFIRMING→COUNTDOWN→RESOLVING→RECOVERED flow."""

    def test_critical_pressure_lifecycle(self):
        """Drive through full lifecycle: NORMAL → ... → RESOLVING"""
        sm = PressureStateMachine({
            "memory_pressure": {
                "confirmation_intervals": 2,
                "countdown_seconds": 1,
            },
            "resolution": {"max_resolution_cycles": 2},
        })

        # CRITICAL_DETECTED on first critical reading
        assert sm.update(ram_available_mb=50, critical_threshold_mb=128) == PressureState.CRITICAL_DETECTED
        # CONFIRMING after confirmation_intervals reached (interval counts from 1)
        assert sm.update(ram_available_mb=50, critical_threshold_mb=128) == PressureState.CONFIRMING
        # COUNTDOWN
        assert sm.update(ram_available_mb=50, critical_threshold_mb=128) == PressureState.COUNTDOWN
        # Wait for countdown...
        time.sleep(1.1)
        # RESOLVING after countdown
        assert sm.update(ram_available_mb=50, critical_threshold_mb=128) == PressureState.RESOLVING

    def test_recovery_on_memory_free(self):
        """Memory recovers → state toggles back to NORMAL"""
        sm = PressureStateMachine({
            "memory_pressure": {"confirmation_intervals": 3, "countdown_seconds": 30},
            "resolution": {"max_resolution_cycles": 3},
        })

        # Push to critical
        sm.update(ram_available_mb=50, critical_threshold_mb=128)
        sm.update(ram_available_mb=50, critical_threshold_mb=128)
        # Recovery mid-cycle
        assert sm.update(ram_available_mb=200, critical_threshold_mb=128) == PressureState.NORMAL

    def test_resolution_success_transition(self):
        """After successful resolution → RECOVERED → cooldown → NORMAL"""
        sm = PressureStateMachine({
            "memory_pressure": {"confirmation_intervals": 1, "countdown_seconds": 0},
            "resolution": {"max_resolution_cycles": 3},
        })

        # Force to RESOLVING
        sm.update(ram_available_mb=50, critical_threshold_mb=128)
        sm.update(ram_available_mb=50, critical_threshold_mb=128)
        sm.update(ram_available_mb=50, critical_threshold_mb=128)
        sm.update(ram_available_mb=50, critical_threshold_mb=128)
        assert sm.should_fire_resolution()

        # Resolution succeeds
        sm.on_resolution_complete(success=True)
        assert sm.get_state() == PressureState.RECOVERED

    def test_manual_intervention_after_max_retries(self):
        """Max resolution cycles exhausted → MANUAL_INTERVENTION"""
        sm = PressureStateMachine({
            "memory_pressure": {"confirmation_intervals": 1, "countdown_seconds": 0},
            "resolution": {"max_resolution_cycles": 2},
        })

        for _ in range(3):
            # Drive to RESOLVING, report failure, should stay in NORMAL or escalate
            sm._state = PressureState.NORMAL
            sm._reset_counters()
            sm.update(ram_available_mb=50, critical_threshold_mb=128)
            sm.update(ram_available_mb=50, critical_threshold_mb=128)
            sm.update(ram_available_mb=50, critical_threshold_mb=128)
            sm.update(ram_available_mb=50, critical_threshold_mb=128)
            sm.on_resolution_complete(success=False)

        # State should be MANUAL_INTERVENTION
        assert sm.get_state() == PressureState.MANUAL_INTERVENTION


class TestNoKillManager:
    """Tests for 3-layer process protection."""

    def test_hardcoded_protection(self):
        mgr = NoKillManager()
        assert mgr.is_protected(1, "systemd-journald", "/usr/lib/systemd/systemd-journald")
        assert mgr.is_protected(1, "sshd", "/usr/sbin/sshd")

    def test_user_list_protection(self):
        mgr = NoKillManager(user_list=["my-service"])
        assert mgr.is_protected(999, "my-service", "/opt/my-service")

    def test_unprotected(self):
        mgr = NoKillManager()
        assert not mgr.is_protected(9999, "chrome", "/opt/google/chrome")

    def test_cli_pid_override(self):
        mgr = NoKillManager(cli_overrides=["42"])
        assert mgr.is_protected(42, "anything", "any-cmdline")


class TestProcessScorer:
    """Tests for weighted process scoring."""

    def test_memory_hog_scores_higher(self):
        scorer = ProcessScorer({"process_scoring": {}})
        fat = ProcessSnapshot(pid=1, name="chrome", cmdline="/opt/chrome",
                             memory_rss_mb=8192, memory_percent=80.0,
                             cpu_percent=5.0, create_time=0, io_read_bytes=0,
                             io_write_bytes=0, username="user", status="running")
        skinny = ProcessSnapshot(pid=2, name="vim", cmdline="/usr/bin/vim",
                                memory_rss_mb=64, memory_percent=1.0,
                                cpu_percent=1.0, create_time=0, io_read_bytes=0,
                                io_write_bytes=0, username="user", status="running")

        assert scorer.score_snapshot(fat) > scorer.score_snapshot(skinny)

    def test_pinned_process_scores_zero(self):
        scorer = ProcessScorer({"process_scoring": {"pinned_processes": ["my-daemon"]}})
        snap = ProcessSnapshot(pid=1, name="my-daemon", cmdline="/usr/bin/my-daemon",
                              memory_rss_mb=4096, memory_percent=50.0,
                              cpu_percent=80.0, create_time=0, io_read_bytes=0,
                              io_write_bytes=0, username="root", status="running")
        assert scorer.score_snapshot(snap) == 0.0


class TestKillListGenerator:
    """Tests for kill list generation."""

    def test_protected_processes_filtered(self):
        mgr = NoKillManager()
        gen = KillListGenerator({}, mgr, MagicMock())

        snapshots = [
            ProcessSnapshot(pid=1, name="sshd", cmdline="/usr/sbin/sshd",
                          memory_rss_mb=4096, memory_percent=50.0,
                          cpu_percent=0.0, create_time=0, io_read_bytes=0,
                          io_write_bytes=0, username="root", status="running"),
            ProcessSnapshot(pid=2, name="chrome", cmdline="/opt/chrome",
                          memory_rss_mb=2048, memory_percent=25.0,
                          cpu_percent=80.0, create_time=0, io_read_bytes=0,
                          io_write_bytes=0, username="user", status="running"),
        ]

        result = gen.regenerate(snapshots)
        assert len(result) == 1
        assert result[0].name == "chrome"


class TestResolutionResult:
    """Tests for the resolution result dataclass."""

    def test_successful_result(self):
        result = ResolutionResult(
            success=True, action_summary="killed chrome (2048MB)",
            kill_count=1, pause_count=0,
        )
        assert result.success
        assert result.kill_count == 1


class TestThresholdEvaluation:
    """Tests for threshold evaluation."""

    def test_ram_threshold_crossing(self):
        thresholds = {"ram_available_mb": {"yellow": 500.0, "red": 128.0}}
        metrics = {"ram": {"available_mb": 100}}

        result = evaluate_thresholds(metrics, thresholds)
        assert "ram_available_mb" in result
        assert result["ram_available_mb"] == Severity.RED

    def test_no_crossing_returns_empty(self):
        thresholds = {"ram_available_mb": {"yellow": 500.0, "red": 128.0}}
        metrics = {"ram": {"available_mb": 1000}}

        result = evaluate_thresholds(metrics, thresholds)
        assert "ram_available_mb" not in result
