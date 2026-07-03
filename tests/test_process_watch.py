"""Tests for the Process Intelligence Engine (process_watch.py)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch, PropertyMock


class TestProcessSnapshot:
    """Test ProcessSnapshot dataclass."""

    def test_has_required_fields(self) -> None:
        from sysstable.process_watch import ProcessSnapshot

        snap = ProcessSnapshot(
            pid=1000, name="test", cmdline="/usr/bin/test",
            create_time=1000.0, memory_rss_mb=50.0, memory_percent=2.5,
            cpu_percent=10.0, io_read_bytes=1024, io_write_bytes=512,
            status="running", username="user",
        )
        assert snap.pid == 1000
        assert snap.name == "test"
        assert snap.memory_rss_mb == 50.0
        assert snap.cpu_percent == 10.0
        assert snap.memory_percent == 2.5


class TestKillListEntry:
    """Test KillListEntry dataclass."""

    def test_has_required_fields(self) -> None:
        from sysstable.process_watch import KillListEntry

        entry = KillListEntry(
            pid=1000, name="test", cmdline="/usr/bin/test",
            score=0.85, memory_mb=50.0, cpu_percent=10.0,
            reason="High memory consumer",
        )
        assert entry.pid == 1000
        assert entry.score == 0.85
        assert entry.is_false_positive is False


class TestFetchAllProcesses:
    """Test fetch_all_processes()."""

    def _make_mock_proc(self, pid: int, name: str, rss_mb: float,
                        mem_pct: float = 5.0, cpu_pct: float = 1.0,
                        io_read: int = 100, io_write: int = 50,
                        status: str = "running") -> MagicMock:
        """Helper to create a mock psutil process with plain-type info dict."""
        proc = MagicMock()

        class FakeMem:
            rss = int(rss_mb * 1024 * 1024)
            vms = 0

        class FakeIO:
            read_bytes = io_read
            write_bytes = io_write

        # Build info dict with PLAIN values — no PropertyMock wrappers
        proc.info = {
            "pid": pid,
            "name": name,
            "cmdline": [name],
            "create_time": 1000.0,
            "memory_info": FakeMem(),
            "memory_percent": mem_pct,
            "cpu_percent": cpu_pct,
            "io_counters": FakeIO(),
            "status": status,
            "username": "user",
        }
        return proc

    @patch("sysstable.process_watch.psutil.process_iter")
    def test_returns_sorted_by_memory(self, mock_iter) -> None:
        from sysstable.process_watch import fetch_all_processes

        small = self._make_mock_proc(1001, "firefox", 200.0)
        big = self._make_mock_proc(1002, "chrome", 500.0)
        mock_iter.return_value = [small, big]

        result = fetch_all_processes(lightweight=False)

        assert len(result) == 2
        # Sorted by memory descending
        assert result[0].pid == 1002  # chrome — 500MB
        assert result[0].name == "chrome"
        assert result[1].pid == 1001  # firefox — 200MB

    @patch("sysstable.process_watch.psutil.process_iter")
    def test_handles_zombie_gracefully(self, mock_iter) -> None:
        from sysstable.process_watch import fetch_all_processes

        import psutil

        good = self._make_mock_proc(1001, "good", 50.0)

        bad = MagicMock()
        # Simulate zombie: info dict throws on specific key access via .get()
        class ZombieInfo(dict):
            def get(self, key, default=None):
                if key in ("memory_info", "cpu_percent", "io_counters"):
                    raise psutil.NoSuchProcess(9999)
                return dict.get(self, key, default)
        bad.info = ZombieInfo({
            "pid": 9999, "name": "zombie", "cmdline": ["zombie"],
            "create_time": 1000.0, "memory_percent": 0,
            "status": "zombie", "username": "?",
        })

        mock_iter.return_value = [good, bad]
        result = fetch_all_processes()

        assert len(result) == 1
        assert result[0].pid == 1001

    @patch("sysstable.process_watch.psutil.process_iter")
    def test_lightweight_returns_max_20(self, mock_iter) -> None:
        from sysstable.process_watch import fetch_all_processes

        procs = [self._make_mock_proc(2000 + i, f"proc_{i}", float(30 - i))
                 for i in range(30)]
        mock_iter.return_value = procs

        result = fetch_all_processes(lightweight=True)

        assert len(result) == 20

    @patch("sysstable.process_watch.psutil.process_iter")
    def test_skips_own_pid(self, mock_iter) -> None:
        from sysstable.process_watch import fetch_all_processes

        import os

        own = self._make_mock_proc(os.getpid(), "pytest", 500.0)
        mock_iter.return_value = [own]

        result = fetch_all_processes()
        assert len(result) == 0


class TestSnapshotProcessesToDb:
    """Test snapshot_processes_to_db()."""

    def test_delegates_to_db(self) -> None:
        from sysstable.process_watch import ProcessSnapshot, snapshot_processes_to_db

        snap = ProcessSnapshot(
            pid=1000, name="test", cmdline="/usr/bin/test",
            create_time=1000.0, memory_rss_mb=50.0, memory_percent=2.5,
            cpu_percent=10.0, io_read_bytes=1024, io_write_bytes=512,
            status="running", username="user",
        )

        mock_db = MagicMock()
        mock_db.save_process_snapshots.return_value = 1
        result = snapshot_processes_to_db([snap], mock_db)

        assert result == 1
        mock_db.save_process_snapshots.assert_called_once_with([snap])

    def test_noop_without_save_method(self) -> None:
        from sysstable.process_watch import ProcessSnapshot, snapshot_processes_to_db

        snap = ProcessSnapshot(
            pid=1000, name="test", cmdline="/usr/bin/test",
            create_time=1000.0, memory_rss_mb=50.0, memory_percent=2.5,
            cpu_percent=10.0, io_read_bytes=1024, io_write_bytes=512,
            status="running", username="user",
        )

        mock_db = MagicMock(spec=[])
        result = snapshot_processes_to_db([snap], mock_db)
        assert result == 0


class TestNoKillManager:
    """Test NoKillManager 3-layer protection."""

    def test_hard_coded_system_processes(self) -> None:
        from sysstable.process_watch import NoKillManager

        mgr = NoKillManager()
        assert mgr.is_protected(1, "systemd", "/lib/systemd")
        assert mgr.is_protected(42, "sshd", "/usr/sbin/sshd")
        assert mgr.is_protected(100, "dockerd", "/usr/bin/dockerd")
        # Unknown process — not protected
        assert not mgr.is_protected(500, "firefox", "/usr/bin/firefox")

    def test_user_config_processes(self) -> None:
        from sysstable.process_watch import NoKillManager

        mgr = NoKillManager(user_list=["bash", "tmux"])
        # Hard-coded still protected
        assert mgr.is_protected(1, "systemd", "/lib/systemd")
        # User config protected
        assert mgr.is_protected(100, "bash", "/usr/bin/bash")
        assert mgr.is_protected(200, "tmux", "/usr/bin/tmux")
        # Not protected
        assert not mgr.is_protected(300, "python3", "/usr/bin/python3")

    def test_cli_override_by_pid(self) -> None:
        from sysstable.process_watch import NoKillManager

        mgr = NoKillManager(cli_overrides=["1234"])
        assert mgr.is_protected(1234, "anything", "/bin/anything")
        assert 1234 in mgr.protected_pids

    def test_cli_override_by_name(self) -> None:
        from sysstable.process_watch import NoKillManager

        mgr = NoKillManager(cli_overrides=["myapp"])
        assert mgr.is_protected(500, "myapp", "/usr/bin/myapp")

    def test_env_override_by_pid(self) -> None:
        from sysstable.process_watch import NoKillManager

        mgr = NoKillManager(env_var="4321,5678")
        assert mgr.is_protected(4321, "x", "/bin/x")
        assert mgr.is_protected(5678, "y", "/bin/y")

    def test_env_override_by_name(self) -> None:
        from sysstable.process_watch import NoKillManager

        mgr = NoKillManager(env_var="watchdog,agent")
        assert mgr.is_protected(100, "watchdog", "/bin/watchdog")
        assert mgr.is_protected(200, "agent", "/bin/agent")

    def test_pid_recycling_prevented(self) -> None:
        """A recycled PID should NOT match hard-coded/user name lists."""
        from sysstable.process_watch import NoKillManager

        mgr = NoKillManager(user_list=["bash"])
        # PID 1 with name "bash" matches the user list entry
        assert mgr.is_protected(1, "bash", "/usr/bin/bash")
        # But PID 1 alone (name="evil") does NOT match — triple check prevents it
        assert not mgr.is_protected(1, "evil", "/usr/bin/evil")

    def test_get_protected_names_includes_all_layers(self) -> None:
        from sysstable.process_watch import NoKillManager, HARD_CODED_NO_KILL

        mgr = NoKillManager(user_list=["myapp"], cli_overrides=["otherapp"])
        names = mgr.get_protected_names()
        assert "systemd" in names  # hard-coded
        assert "myapp" in names     # user config
        assert "otherapp" in names  # CLI

    def test_env_var_validation(self) -> None:
        from sysstable.process_watch import NoKillManager

        # Valid
        assert NoKillManager.validate_env_var("1234,bash") == []
        # Invalid PID
        warns = NoKillManager.validate_env_var("-1")
        assert len(warns) == 1
        assert "Invalid PID" in warns[0]
        # Too-short name
        warns = NoKillManager.validate_env_var("a")
        assert len(warns) == 1
        assert "too short" in warns[0]

    def test_empty_env_var_no_crash(self) -> None:
        from sysstable.process_watch import NoKillManager

        mgr = NoKillManager(env_var="")
        assert mgr.protected_pids == set()

    def test_env_and_cli_and_config_stack(self) -> None:
        from sysstable.process_watch import NoKillManager

        mgr = NoKillManager(
            user_list=["bash"],
            cli_overrides=["chrome"],
            env_var="firefox",
        )
        assert mgr.is_protected(1, "systemd", "")    # hard-coded
        assert mgr.is_protected(10, "bash", "")       # user config
        assert mgr.is_protected(20, "chrome", "")     # CLI
        assert mgr.is_protected(30, "firefox", "")    # ENV
        assert not mgr.is_protected(99, "unknown", "")  # not protected