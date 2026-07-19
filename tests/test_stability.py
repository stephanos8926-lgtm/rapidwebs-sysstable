"""Unit and integration tests for system stability enhancements."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import psutil

from sysstable.database import MetricsDB
from sysstable.pro_balance import ProBalanceScheduler
from sysstable.process_watch import NoKillManager, ProcessSnapshot
from sysstable.rules_engine import RulesEngine, is_interactive_process
from sysstable.utils import blake3_hash


def test_blake3_hash():
    """Verify that Blake3 hashing (with blake2b fallback) works as expected."""
    h1 = blake3_hash("hello world")
    h2 = blake3_hash("hello world")
    h3 = blake3_hash("another test")

    assert len(h1) == 64
    assert h1 == h2
    assert h1 != h3


def test_interactivity_score():
    """Test the process interactivity score heuristic."""
    proc = MagicMock(spec=psutil.Process)
    proc.terminal.return_value = "/dev/pts/0"
    proc.username.return_value = "jules"
    proc.parent.return_value = MagicMock()
    proc.parent.return_value.name.return_value = "bash"

    config = {
        "nice_renice": {
            "interactive_weight_terminal": 0.4,
            "interactive_weight_username": 0.3,
            "interactive_weight_parent": 0.3,
        }
    }

    score = is_interactive_process(proc, config)
    assert score == 1.0


def test_rules_engine_matching():
    """Test pattern matching with Glob, Regex, Fuzzy, and exact matches."""
    config = {
        "rules": [
            {
                "pattern": "firefox*",
                "nice": 4,
            },
            {
                "pattern": "r\"^python(3)?$\"",
                "nice": 2,
            },
            {
                "pattern": "exact-match",
                "nice": 0,
            }
        ]
    }

    engine = RulesEngine(config)

    # 1. Glob Match
    res = engine.match_process(1234, "firefox-bin", "firefox --new-tab")
    assert res is not None
    assert res["nice"] == 4

    # 2. Regex Match
    res = engine.match_process(5678, "python3", "python3 -m unittest")
    assert res is not None
    assert res["nice"] == 2

    # 3. Exact Match
    res = engine.match_process(9012, "exact-match", "./exact-match")
    assert res is not None
    assert res["nice"] == 0

    # 4. No Match
    res = engine.match_process(1111, "other-proc", "./other-proc")
    assert res is None


def test_pro_balance_cycle(tmp_path):
    """Test ProBalance scheduler deprioritization and restoration."""
    db_file = tmp_path / "test_stability.db"
    db = MetricsDB(db_file)

    config = {
        "pro_balance": {
            "enabled": True,
            "system_cpu_threshold_percent": 80.0,
            "process_cpu_threshold_percent": 20.0,
            "renice_value": 10,
            "restore_delay_seconds": 0.1,
        },
        "nice_renice": {
            "interactive_weight_terminal": 0.4,
            "interactive_weight_username": 0.3,
            "interactive_weight_parent": 0.3,
        }
    }

    no_kill_mgr = NoKillManager()
    scheduler = ProBalanceScheduler(config, db, no_kill_mgr)

    # Mock running process list containing a runaway background process
    runaway = ProcessSnapshot(
        pid=99999,  # safe/dummy pid
        name="runaway_proc",
        cmdline="runaway_proc --hog",
        create_time=time.time(),
        memory_rss_mb=200.0,
        memory_percent=5.0,
        cpu_percent=45.0,  # exceeds 20.0% threshold
        io_read_bytes=1000,
        io_write_bytes=1000,
        status="running",
        username="daemon",
        num_fds=10,
        nice=0,
    )

    # Mock psutil.Process
    mock_proc = MagicMock(spec=psutil.Process)
    mock_proc.terminal.return_value = None  # background
    mock_proc.username.return_value = "daemon"
    mock_proc.parent.return_value = None
    mock_proc.nice.return_value = 0
    mock_proc.name.return_value = "runaway_proc"

    import unittest.mock as mock
    with mock.patch("psutil.Process", return_value=mock_proc):
        # 1. Run cycle under high system load
        actions = scheduler.run_cycle(85.0, [runaway])
        assert len(actions) == 1
        assert actions[0]["action"] == "pro_balance_deprioritize"
        assert actions[0]["pid"] == 99999

        # Verify that process is registered as adjusted
        assert 99999 in scheduler.adjusted_processes

        # 2. Wait and run cycle under normal load to test restore
        time.sleep(0.2)
        # Mock active_pids to include 99999
        actions_restore = scheduler.run_cycle(30.0, [runaway])
        assert len(actions_restore) == 1
        assert actions_restore[0]["action"] == "pro_balance_restore"
        assert 99999 not in scheduler.adjusted_processes

    db.close()
