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