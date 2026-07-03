"""Integration tests for the full critical memory pressure resolution system.

Tests the end-to-end flow: metrics → thresholds → state machine →
process collection → kill list → resolution → recovery/manual intervention.
"""

from unittest.mock import MagicMock, patch


class TestFullCriticalMemoryLifecycle:
    """Test the full memory pressure lifecycle with mocked components."""

    def _make_metrics(self, ram_avail_mb: float) -> dict:
        """Build mock metrics with configurable RAM."""
        return {
            "ram": {"available_mb": ram_avail_mb, "total_mb": 1024, "percent": 50.0},
            "cpu": {"percent": 20.0, "load_15m": 1.0},
            "swap": {"used_mb": 0, "total_mb": 1024, "percent": 0},
            "disk": {"partitions": [{"mountpoint": "/", "free_mb": 50000}]},
            "timestamp": 1000000000,
        }

    def _make_snap(self, pid, name, mem_mb, cpu=5.0):
        from sysstable.process_watch import ProcessSnapshot

        return ProcessSnapshot(
            pid=pid,
            name=name,
            cmdline=f"/usr/bin/{name}",
            create_time=1000.0,
            memory_rss_mb=mem_mb,
            memory_percent=mem_mb / 1024 * 100,
            cpu_percent=cpu,
            io_read_bytes=0,
            io_write_bytes=0,
            status="running",
            username="user",
        )

    @patch("sysstable.process_watch.psutil.process_iter")
    def test_full_lifecycle_with_resolution(self, mock_iter):
        """Run through the complete lifecycle with all components."""
        from sysstable.process_watch import (
            KillListGenerator,
            NoKillManager,
        )
        from sysstable.resolver import MemoryPressureResolver
        from sysstable.state_machine import PressureState, PressureStateMachine

        # ── Setup ────────────────────────────────────────────────────
        mock_iter.return_value = []

        config = {
            "memory_pressure": {
                "confirmation_intervals": 2,
                "countdown_seconds": 0.01,
                "critical_threshold_mb": 128,
                "process_snapshot_interval": 60,
                "normal_snapshot_interval": 300,
                "kill_list_persistence_interval": 5,
            },
            "resolution": {
                "max_resolution_cycles": 3,
                "min_freed_memory_mb": 64,
                "sigterm_timeout_seconds": 1,
                "pause_count": 2,
                "pause_duration_seconds": 5,
            },
            "process_scoring": {
                "memory_weight": 1.0,
                "cpu_weight": 0.0,
                "io_weight": 0.0,
                "history_weight": 0.0,
            },
            "never_kill": {"user_list": []},
            "thresholds": {
                "ram_available_mb": {"critical": 128, "red": 256},
            },
        }
        import time

        db = MagicMock(
            spec=[
                "save_process_snapshots",
                "save_kill_list_generation",
                "save_resolution_event",
            ]
        )
        db.save_process_snapshots.return_value = 3
        db.save_kill_list_generation.return_value = 1
        db.save_resolution_event.return_value = 1

        # ── Step 1: Normal state ────────────────────────────────────
        state_machine = PressureStateMachine(config)
        assert state_machine.get_state() == PressureState.NORMAL

        # ── Step 2: RAM drops below critical ────────────────────────
        state_machine.update(50.0, critical_threshold_mb=128)
        assert state_machine.get_state() == PressureState.CRITICAL_DETECTED

        # ── Step 3: Confirming (2 intervals) ────────────────────────
        state_machine.update(50.0, critical_threshold_mb=128)
        assert state_machine.get_state() == PressureState.CONFIRMING

        # ── Step 4: Countdown begins ────────────────────────────────
        state_machine.update(50.0, critical_threshold_mb=128)
        assert state_machine.get_state() == PressureState.COUNTDOWN

        # ── Step 5: Countdown expires ───────────────────────────────
        time.sleep(0.02)
        state_machine.update(50.0, critical_threshold_mb=128)
        assert state_machine.get_state() == PressureState.RESOLVING
        assert state_machine.should_fire_resolution() is True

        # ── Step 6: Generate kill list ──────────────────────────────
        no_kill = NoKillManager()
        gen = KillListGenerator(config, no_kill, db)
        snaps = [
            self._make_snap(1001, "firefox", 300.0),
            self._make_snap(1002, "chrome", 250.0),
            self._make_snap(1003, "sshd", 50.0),  # protected!
        ]
        kill_list = gen.regenerate(snaps)
        assert len(kill_list) == 2  # sshd filtered
        assert kill_list[0].pid == 1001  # firefox highest memory
        assert kill_list[1].pid == 1002  # chrome next

        # ── Step 7: Execute resolution ─────────────────────────────
        resolver = MemoryPressureResolver(config, db)
        # Mock resolution to succeed
        with (
            patch.object(resolver, "_kill_process", return_value=True),
            patch.object(resolver, "_pause_process", return_value=True),
            patch("sysstable.resolver.psutil.virtual_memory") as mock_ram,
        ):
            mock_ram.return_value.available = 500 * 1024 * 1024  # 500MB freed

            result = resolver.resolve(kill_list, 200.0)

        assert result.success is True
        assert result.kill_count == 1

        # ── Step 8: Report success to state machine ─────────────────
        state_machine.on_resolution_complete(success=True)
        assert state_machine.get_state() == PressureState.RECOVERED

        # ── Step 9: Recovery cooldown → back to normal ──────────────
        state_machine.update(500.0, critical_threshold_mb=128)
        assert state_machine.get_state() == PressureState.RECOVERED  # still in cooldown

        # Simulate cooldown expiry (60s) by directly calling update
        state_machine._recovered_cooldown_until = time.time_ns() - 1_000_000_000
        state_machine.update(500.0, critical_threshold_mb=128)
        assert state_machine.get_state() == PressureState.NORMAL

    @patch("sysstable.process_watch.psutil.process_iter")
    def test_manual_intervention_after_max_retries(self, mock_iter):
        """When resolution fails 3 times, system enters MANUAL_INTERVENTION."""
        from sysstable.process_watch import KillListGenerator, NoKillManager
        from sysstable.resolver import MemoryPressureResolver
        from sysstable.state_machine import PressureState, PressureStateMachine

        mock_iter.return_value = []

        config = {
            "memory_pressure": {"confirmation_intervals": 1, "countdown_seconds": 0.005},
            "resolution": {"max_resolution_cycles": 3, "min_freed_memory_mb": 64, "sigterm_timeout_seconds": 1},
            "process_scoring": {"memory_weight": 1.0, "cpu_weight": 0.0, "io_weight": 0.0, "history_weight": 0.0},
            "never_kill": {"user_list": []},
        }
        import time

        db = MagicMock(spec=["save_kill_list_generation", "save_resolution_event"])
        db.save_kill_list_generation.return_value = 1
        db.save_resolution_event.return_value = 1

        sm = PressureStateMachine(config)
        no_kill = NoKillManager()
        gen = KillListGenerator(config, no_kill, db)
        resolver = MemoryPressureResolver(config, db)

        snap = self._make_snap(2001, "leaky", 800.0)

        for cycle in range(3):
            # Cycle through to RESOLVING: CRITICAL_DETECTED → CONFIRMING → COUNTDOWN → RESOLVING
            sm.update(50.0, critical_threshold_mb=128)  # → CRITICAL_DETECTED
            sm.update(50.0, critical_threshold_mb=128)  # → CONFIRMING
            sm.update(50.0, critical_threshold_mb=128)  # → COUNTDOWN
            time.sleep(0.01)  # Let countdown expire
            sm.update(50.0, critical_threshold_mb=128)  # → RESOLVING
            assert sm.get_state() == PressureState.RESOLVING

            kill_list = gen.regenerate([snap])
            with (
                patch.object(resolver, "_kill_process", return_value=True),
                patch.object(resolver, "_pause_process", return_value=True),
                patch("sysstable.resolver.psutil.virtual_memory") as mock_ram,
            ):
                # Simulate insufficient memory freed
                mock_ram.return_value.available = 50 * 1024 * 1024
                result = resolver.resolve(kill_list, 80.0)

            assert result.success is False
            sm.on_resolution_complete(success=False)

            if cycle < 2:
                assert sm.get_state() != PressureState.MANUAL_INTERVENTION
            else:
                assert sm.get_state() == PressureState.MANUAL_INTERVENTION

        # Once in MANUAL_INTERVENTION, stays there forever
        sm.update(5000.0, critical_threshold_mb=128)
        assert sm.get_state() == PressureState.MANUAL_INTERVENTION
