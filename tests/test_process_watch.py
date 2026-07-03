"""Tests for the Process Intelligence Engine (process_watch.py)."""

from unittest.mock import MagicMock, patch

from sysstable.process_watch import (
    ProcessSnapshot,
    KillListEntry,
    fetch_all_processes,
    snapshot_processes_to_db,
    NoKillManager,
    ProcessScorer,
    KillListGenerator,
)


class TestProcessSnapshot:
    def test_has_required_fields(self):
        snap = ProcessSnapshot(
            pid=1000, name="test", cmdline="/usr/bin/test",
            create_time=1000.0, memory_rss_mb=50.0, memory_percent=2.5,
            cpu_percent=10.0, io_read_bytes=1024, io_write_bytes=512,
            status="running", username="user",
        )
        assert snap.pid == 1000
        assert snap.name == "test"
        assert snap.memory_rss_mb == 50.0


class TestKillListEntry:
    def test_has_required_fields(self):
        entry = KillListEntry(
            pid=1000, name="test", cmdline="/usr/bin/test",
            score=0.85, memory_mb=50.0, cpu_percent=10.0,
            reason="High memory consumer",
        )
        assert entry.pid == 1000
        assert entry.score == 0.85
        assert entry.is_false_positive is False


class TestFetchAllProcesses:
    def _make_mock_proc(self, pid, name, rss_mb, mem_pct=5.0, cpu_pct=1.0,
                        io_read=100, io_write=50, status="running") -> MagicMock:
        proc = MagicMock()
        class FakeMem:
            rss = int(rss_mb * 1024 * 1024)
        class FakeIO:
            read_bytes = io_read
            write_bytes = io_write
        proc.info = {
            "pid": pid, "name": name, "cmdline": [name],
            "create_time": 1000.0, "memory_info": FakeMem(),
            "memory_percent": mem_pct, "cpu_percent": cpu_pct,
            "io_counters": FakeIO(), "status": status, "username": "user",
        }
        return proc

    @patch("sysstable.process_watch.psutil.process_iter")
    def test_returns_sorted_by_memory(self, mock_iter):
        small = self._make_mock_proc(1001, "firefox", 50.0, mem_pct=10.0)
        big = self._make_mock_proc(1002, "chrome", 200.0, mem_pct=40.0)
        mock_iter.return_value = [small, big]
        result = fetch_all_processes(lightweight=False)
        assert len(result) == 2
        assert result[0].pid == 1002
        assert result[1].pid == 1001

    @patch("sysstable.process_watch.psutil.process_iter")
    def test_handles_zombie_gracefully(self, mock_iter):
        import psutil
        good = self._make_mock_proc(1001, "good", 50.0, mem_pct=10.0)
        class ZombieInfo(dict):
            def get(self, key, default=None):
                if key in ("memory_info", "cpu_percent", "io_counters"):
                    raise psutil.NoSuchProcess(9999)
                return dict.get(self, key, default)
        bad = MagicMock()
        bad.info = ZombieInfo({"pid": 9999, "name": "zombie", "cmdline": ["zombie"],
                         "create_time": 1000.0, "memory_percent": 0,
                         "status": "zombie", "username": "?"})
        mock_iter.return_value = [good, bad]
        result = fetch_all_processes()
        assert len(result) == 1
        assert result[0].pid == 1001

    @patch("sysstable.process_watch.psutil.process_iter")
    def test_lightweight_returns_max_20(self, mock_iter):
        procs = [self._make_mock_proc(2000 + i, f"proc_{i}", float(30 - i), mem_pct=float(30 - i))
                 for i in range(30)]
        mock_iter.return_value = procs
        result = fetch_all_processes(lightweight=True)
        assert len(result) == 20

    @patch("sysstable.process_watch.psutil.process_iter")
    def test_skips_own_pid(self, mock_iter):
        import os
        own = self._make_mock_proc(os.getpid(), "pytest", 500.0, mem_pct=80.0)
        mock_iter.return_value = [own]
        result = fetch_all_processes()
        assert len(result) == 0


class TestSnapshotProcessesToDb:
    def test_delegates_to_db(self):
        snap = ProcessSnapshot(
            pid=1000, name="test", cmdline="/usr/bin/test",
            create_time=1000.0, memory_rss_mb=50.0, memory_percent=2.5,
            cpu_percent=10.0, io_read_bytes=1024, io_write_bytes=512,
            status="running", username="user",
        )
        mock_db = MagicMock()
        mock_db.save_process_snapshots.return_value = 1
        assert snapshot_processes_to_db([snap], mock_db) == 1
        mock_db.save_process_snapshots.assert_called_once_with([snap])

    def test_noop_without_save_method(self):
        snap = ProcessSnapshot(
            pid=1000, name="test", cmdline="/usr/bin/test",
            create_time=1000.0, memory_rss_mb=50.0, memory_percent=2.5,
            cpu_percent=10.0, io_read_bytes=1024, io_write_bytes=512,
            status="running", username="user",
        )
        assert snapshot_processes_to_db([snap], MagicMock(spec=[])) == 0


class TestNoKillManager:
    def test_hard_coded_system_processes(self):
        mgr = NoKillManager()
        assert mgr.is_protected(1, "systemd", "/lib/systemd")
        assert mgr.is_protected(42, "sshd", "/usr/sbin/sshd")
        assert not mgr.is_protected(500, "firefox", "/usr/bin/firefox")

    def test_user_config_processes(self):
        mgr = NoKillManager(user_list=["bash", "tmux"])
        assert mgr.is_protected(1, "systemd", "/lib/systemd")
        assert mgr.is_protected(100, "bash", "/usr/bin/bash")
        assert not mgr.is_protected(300, "python3", "/usr/bin/python3")

    def test_cli_override_by_pid(self):
        mgr = NoKillManager(cli_overrides=["1234"])
        assert mgr.is_protected(1234, "anything", "/bin/anything")
        assert 1234 in mgr.protected_pids

    def test_cli_override_by_name(self):
        mgr = NoKillManager(cli_overrides=["myapp"])
        assert mgr.is_protected(500, "myapp", "/usr/bin/myapp")

    def test_env_override_by_pid(self):
        mgr = NoKillManager(env_var="4321,5678")
        assert mgr.is_protected(4321, "x", "/bin/x")
        assert mgr.is_protected(5678, "y", "/bin/y")

    def test_env_override_by_name(self):
        mgr = NoKillManager(env_var="watchdog,agent")
        assert mgr.is_protected(100, "watchdog", "/bin/watchdog")
        assert mgr.is_protected(200, "agent", "/bin/agent")

    def test_pid_recycling_prevented(self):
        mgr = NoKillManager(user_list=["bash"])
        assert mgr.is_protected(1, "bash", "/usr/bin/bash")
        assert not mgr.is_protected(1, "evil", "/usr/bin/evil")

    def test_get_protected_names_includes_all_layers(self):
        mgr = NoKillManager(user_list=["myapp"], cli_overrides=["otherapp"])
        names = mgr.get_protected_names()
        assert "systemd" in names
        assert "myapp" in names
        assert "otherapp" in names

    def test_env_var_validation(self):
        assert NoKillManager.validate_env_var("1234,bash") == []
        warns = NoKillManager.validate_env_var("-1")
        assert len(warns) == 1
        assert "Invalid PID" in warns[0]

    def test_empty_env_var_no_crash(self):
        mgr = NoKillManager(env_var="")
        assert mgr.protected_pids == set()


class TestProcessScorer:
    def test_memory_score_normalization(self):
        scorer = ProcessScorer({"process_scoring": {"max_memory_percent": 50.0}})
        assert scorer._memory_score(0.0) == 0.0
        assert scorer._memory_score(25.0) == 0.5
        assert scorer._memory_score(50.0) == 1.0
        assert scorer._memory_score(100.0) == 1.0

    def test_cpu_score_normalization(self):
        scorer = ProcessScorer({"process_scoring": {"max_cpu_percent": 80.0}})
        assert scorer._cpu_score(0.0) == 0.0
        assert scorer._cpu_score(40.0) == 0.5
        assert scorer._cpu_score(80.0) == 1.0
        assert scorer._cpu_score(160.0) == 1.0

    def test_false_positive_detection(self):
        scorer = ProcessScorer({
            "process_scoring": {
                "cpu_false_positive_threshold": 5.0,
                "io_false_positive_threshold_mbps": 1.0,
            }
        })
        # High memory, low CPU, low IO = false positive (cached)
        fp = ProcessSnapshot(1, "cached", "cached", 0, 100, 30, 2, 100000, 50000, "sleeping", "user")
        assert scorer._is_false_positive(fp) is True
        # High CPU = not false positive
        not_fp = ProcessSnapshot(2, "active", "active", 0, 100, 30, 80, 100000, 50000, "running", "user")
        assert scorer._is_false_positive(not_fp) is False


class TestKillListGenerator:
    def test_filters_out_protected_processes(self):
        config = {"memory_pressure": {}, "process_scoring": {}, "never_kill": {"user_list": []}}
        gen = KillListGenerator(config, NoKillManager(), object())
        s1 = ProcessSnapshot(1, "firefox", "firefox", 0, 100, 10, 5, 0, 0, "running", "user")
        s2 = ProcessSnapshot(2, "systemd", "systemd", 0, 50, 5, 2, 0, 0, "running", "root")
        result = gen.regenerate([s1, s2])
        assert len(result) == 1
        assert result[0].pid == 1

    def test_sorts_by_score(self):
        config = {
            "memory_pressure": {},
            "process_scoring": {"memory_weight": 1.0, "cpu_weight": 0.0, "io_weight": 0.0, "history_weight": 0.0},
            "never_kill": {"user_list": []},
        }
        gen = KillListGenerator(config, NoKillManager(), object())
        low = ProcessSnapshot(1, "low", "low", 0, 50, 10, 5, 0, 0, "running", "user")
        high = ProcessSnapshot(2, "high", "high", 0, 200, 40, 5, 0, 0, "running", "user")
        result = gen.regenerate([low, high])
        assert result[0].pid == 2
        assert result[1].pid == 1
        assert result[0].score >= result[1].score

    def test_get_kill_list_returns_current(self):
        gen = KillListGenerator({}, NoKillManager(), object())
        s1 = ProcessSnapshot(1, "a", "a", 0, 100, 20, 5, 0, 0, "running", "user")
        s2 = ProcessSnapshot(2, "b", "b", 0, 200, 40, 5, 0, 0, "running", "user")
        gen.regenerate([s1, s2])
        result = gen.get_kill_list()
        assert len(result) == 2
        assert result[0].pid == 2
        assert result[1].pid == 1
