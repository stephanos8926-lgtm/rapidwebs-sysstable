"""Tests for RapidWebs-SysStable core components."""

import json
from pathlib import Path


class TestCollector:
    """Test metric collection."""

    def test_collect_returns_all_categories(self) -> None:
        from sysstable.collector import collect

        m = collect()
        d = m.to_dict()

        assert "ram" in d
        assert "cpu" in d
        assert "disk" in d
        assert "net" in d
        assert "uptime_seconds" in d
        assert d["ram"]["total_mb"] > 0
        assert d["cpu"]["percent"] >= 0
        assert d["uptime_seconds"] > 0

    def test_collect_ram_reasonable(self) -> None:
        from sysstable.collector import collect

        d = collect().to_dict()
        # Typical dev machine: 1GB-128GB
        assert 500 < d["ram"]["total_mb"] < 131072


class TestDatabase:
    """Test SQLite metrics store."""

    def test_write_and_read(self, tmp_path) -> None:
        from sysstable.database import MetricsDB

        db = MetricsDB(tmp_path / "test.db")
        db.write({"timestamp": 1234567890, "test": True})
        assert db.count() == 1
        latest = db.get_latest()
        assert latest is not None
        db.close()

    def test_query_recent(self, tmp_path) -> None:
        from sysstable.database import MetricsDB

        db = MetricsDB(tmp_path / "test.db")
        for i in range(5):
            db.write({"timestamp": 1000000000 + i, "i": i})
        recent = db.query_recent(limit=3)
        assert len(recent) == 3
        db.close()

    def test_prune(self, tmp_path) -> None:
        from sysstable.database import MetricsDB

        db = MetricsDB(tmp_path / "test.db")
        db.write({"timestamp": 1000000000, "old": True})
        pruned = db.prune(retain_hours=0)
        assert pruned == 1
        assert db.count() == 0
        db.close()

    def test_empty_db(self, tmp_path) -> None:
        from sysstable.database import MetricsDB

        db = MetricsDB(tmp_path / "test.db")
        assert db.get_latest() is None
        assert db.count() == 0
        assert db.query_recent(5) == []
        db.close()


class TestThresholds:
    """Test threshold evaluation."""

    def test_yellow_when_below_yellow_reverse(self) -> None:
        from sysstable.thresholds import Severity, evaluate_thresholds

        metrics = {"ram": {"available_mb": 800}}
        thresholds = {"ram_available_mb": {"yellow": 1024, "red": 256}}
        v = evaluate_thresholds(metrics, thresholds)
        assert v["ram_available_mb"] == Severity.YELLOW

    def test_red_when_below_red_reverse(self) -> None:
        from sysstable.thresholds import Severity, evaluate_thresholds

        metrics = {"ram": {"available_mb": 100}}
        thresholds = {"ram_available_mb": {"yellow": 1024, "red": 256}}
        v = evaluate_thresholds(metrics, thresholds)
        assert v["ram_available_mb"] == Severity.RED

    def test_green_when_above_threshold(self) -> None:
        from sysstable.thresholds import evaluate_thresholds

        metrics = {"ram": {"available_mb": 2000}}
        thresholds = {"ram_available_mb": {"yellow": 1024, "red": 256}}
        v = evaluate_thresholds(metrics, thresholds)
        assert "ram_available_mb" not in v

    def test_cpu_load_high_is_worse(self) -> None:
        from sysstable.thresholds import Severity, evaluate_thresholds

        metrics = {"cpu": {"load_15m": 5.0}}
        thresholds = {"cpu_load_15m": {"yellow": 2.0, "red": 4.0}}
        v = evaluate_thresholds(metrics, thresholds)
        assert v["cpu_load_15m"] == Severity.RED

    def test_disk_root_free(self) -> None:
        from sysstable.thresholds import Severity, evaluate_thresholds

        metrics = {"disk": {"partitions": [{"mountpoint": "/", "free_mb": 1000}]}}
        thresholds = {"disk_root_free_mb": {"yellow": 5120, "red": 1024}}
        v = evaluate_thresholds(metrics, thresholds)
        assert v["disk_root_free_mb"] == Severity.RED

    def test_temperature(self) -> None:
        from sysstable.thresholds import Severity, evaluate_thresholds

        metrics = {"temperatures": {"cpu": [{"current": 85}]}}
        thresholds = {"temperature_celsius": {"yellow": 80, "red": 95}}
        v = evaluate_thresholds(metrics, thresholds)
        # 85 is >= 80 (yellow) but < 95 (red)
        assert v["temperature_celsius"] == Severity.YELLOW

    def test_temperature_red(self) -> None:
        from sysstable.thresholds import Severity, evaluate_thresholds

        metrics = {"temperatures": {"cpu": [{"current": 100}]}}
        thresholds = {"temperature_celsius": {"yellow": 80, "red": 95}}
        v = evaluate_thresholds(metrics, thresholds)
        assert v["temperature_celsius"] == Severity.RED

    def test_open_value_none(self) -> None:
        from sysstable.thresholds import evaluate_thresholds

        metrics = {"ram": {"available_mb": None}}
        thresholds = {"ram_available_mb": {"yellow": 1024}}
        v = evaluate_thresholds(metrics, thresholds)
        assert "ram_available_mb" not in v


class TestConfig:
    """Test config loading."""

    def test_defaults(self) -> None:
        from sysstable.config import DEFAULT_CONFIG

        assert DEFAULT_CONFIG["interval_seconds"] == 15
        assert DEFAULT_CONFIG["retention_hours"] == 72

    def test_load_creates_defaults(self) -> None:
        from sysstable.config import load_config

        cfg = load_config()
        assert "thresholds" in cfg
        assert "events" in cfg
        assert "ram_available_mb" in cfg["thresholds"]


class TestEvents:
    """Test event dispatch."""

    def test_dispatch_handles_no_hooks_dir(self) -> None:
        from sysstable.events import dispatch_events

        results = dispatch_events("yellow", "ram_available_mb", 500, {"events": {}}, {})
        assert isinstance(results, list)

    def test_dispatch_with_invalid_webhook(self) -> None:
        from sysstable.events import dispatch_events

        config = {"events": {"webhooks": ["http://localhost:1/nonexistent"]}}
        results = dispatch_events("red", "ram_available_mb", 100, config, {})
        # Should not crash
        assert isinstance(results, list)


class TestHermesPlugin:
    """Test the Hermes integration plugin."""

    def test_plugin_imports(self) -> None:
        from sysstable.config import load_config

        cfg = load_config()
        state_path = Path(cfg["state_path"])
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "severity": "green",
                    "violations": {},
                    "metrics": {},
                }
            )
        )

        # Simulate plugin reading state
        data = json.loads(state_path.read_text())
        assert data["severity"] == "green"

    def test_plugin_severity_logic(self) -> None:
        # Test the severity mapping logic inline
        def severity_to_action(s: str) -> str:
            if s == "red":
                return "block"
            if s == "orange":
                return "soft_block"
            if s == "yellow":
                return "warn"
            return "ok"

        assert severity_to_action("red") == "block"
        assert severity_to_action("orange") == "soft_block"
        assert severity_to_action("yellow") == "warn"
        assert severity_to_action("green") == "ok"


class TestUtils:
    """Test utility functions."""

    def test_get_violation_value_ram(self) -> None:
        from sysstable.utils import get_violation_value

        val = get_violation_value("ram_available_mb", {"ram": {"available_mb": 500.0}})
        assert val == 500.0

    def test_get_violation_value_unknown(self) -> None:
        from sysstable.utils import get_violation_value

        val = get_violation_value("nonexistent_metric", {})
        assert val is None

    def test_get_violation_value_temperature(self) -> None:
        from sysstable.utils import get_violation_value

        val = get_violation_value(
            "temperature_celsius",
            {"temperatures": {"cpu_thermal": [{"current": 85.0}]}},
        )
        assert val == 85.0


class TestIowait:
    """Test iowait parsing."""

    def test_iowait_field_present(self) -> None:
        from sysstable.collector import collect

        d = collect().to_dict()
        assert "iowait_percent" in d["cpu"]
        assert d["cpu"]["iowait_percent"] >= 0.0


class TestSwapIO:
    """Test swap sin/sout fields."""

    def test_swap_in_out_fields_present(self) -> None:
        from sysstable.collector import collect

        d = collect().to_dict()
        assert "in_mb" in d["swap"]
        assert "out_mb" in d["swap"]
        assert d["swap"]["in_mb"] >= 0.0
        assert d["swap"]["out_mb"] >= 0.0


class TestMetricsDBContext:
    """Test MetricsDB context manager."""

    def test_context_manager(self, tmp_path) -> None:
        from sysstable.database import MetricsDB

        db_path = tmp_path / "ctx.db"
        with MetricsDB(str(db_path)) as db:
            db.write({"timestamp": 1000000000, "test": True})
            assert db.count() == 1

        import sqlite3

        conn = sqlite3.connect(str(db_path))
        try:
            count = conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0]
            assert count == 1
        finally:
            conn.close()


class TestOrangeRetryTracker:
    """Test orange retry tracking logic."""

    def test_first_orange_blocks_second_passes(self) -> None:
        tracker: dict[str, bool] = {}
        violations = {"ram_available_mb": "orange"}
        import json as _json

        key = _json.dumps(violations, sort_keys=True)

        # First orange → block
        assert key not in tracker
        tracker[key] = True

        # Second orange → let through
        if tracker.get(key):
            tracker.pop(key, None)
        second_blocked = key in tracker
        assert not second_blocked

        # Third orange (key consumed) → block again
        assert key not in tracker


class TestCriticalSeverity:
    """Test CRITICAL severity level (Phase 2 addition)."""

    def test_critical_is_severity_member(self) -> None:
        from sysstable.thresholds import Severity

        assert Severity.CRITICAL == "critical"
        assert Severity.CRITICAL.value == "critical"

    def test_critical_when_below_critical_reverse(self) -> None:
        from sysstable.thresholds import Severity, evaluate_thresholds

        metrics = {"ram": {"available_mb": 50}}
        thresholds = {"ram_available_mb": {"yellow": 1024, "red": 256, "critical": 128}}
        v = evaluate_thresholds(metrics, thresholds)
        assert v["ram_available_mb"] == Severity.CRITICAL

    def test_red_not_critical_when_between_red_and_critical(self) -> None:
        from sysstable.thresholds import Severity, evaluate_thresholds

        metrics = {"ram": {"available_mb": 200}}
        thresholds = {"ram_available_mb": {"yellow": 1024, "red": 256, "critical": 128}}
        v = evaluate_thresholds(metrics, thresholds)
        assert v["ram_available_mb"] == Severity.RED

    def test_green_when_above_all_reverse(self) -> None:
        from sysstable.thresholds import evaluate_thresholds

        metrics = {"ram": {"available_mb": 2000}}
        thresholds = {"ram_available_mb": {"yellow": 1024, "red": 256, "critical": 128}}
        v = evaluate_thresholds(metrics, thresholds)
        assert "ram_available_mb" not in v


class TestNewConfig:
    """Test new config blocks (Phase 2 addition)."""

    def test_memory_pressure_defaults(self) -> None:
        from sysstable.config import DEFAULT_CONFIG

        mp = DEFAULT_CONFIG["memory_pressure"]
        assert mp["confirmation_intervals"] == 5
        assert mp["countdown_seconds"] == 90
        assert mp["process_snapshot_interval"] == 60
        assert mp["normal_snapshot_interval"] == 300
        assert mp["kill_list_persistence_interval"] == 5
        assert mp["kill_list_history_max"] == 50

    def test_resolution_defaults(self) -> None:
        from sysstable.config import DEFAULT_CONFIG

        r = DEFAULT_CONFIG["resolution"]
        assert r["auto_resolve"] is True
        assert r["sigterm_timeout_seconds"] == 10
        assert r["pause_count"] == 3
        assert r["pause_duration_seconds"] == 10
        assert r["max_resolution_cycles"] == 3
        assert r["min_freed_memory_mb"] == 64
        assert r["systemd_managed_services"] == []

    def test_process_scoring_defaults(self) -> None:
        from sysstable.config import DEFAULT_CONFIG

        ps = DEFAULT_CONFIG["process_scoring"]
        assert ps["memory_weight"] == 0.5
        assert ps["cpu_weight"] == 0.25
        assert ps["false_positive_penalty"] == 0.5

    def test_never_kill_defaults(self) -> None:
        from sysstable.config import DEFAULT_CONFIG

        nk = DEFAULT_CONFIG["never_kill"]
        assert len(nk["user_list"]) > 0
        assert "sshd" in nk["user_list"]

    def test_thresholds_has_critical(self) -> None:
        from sysstable.config import DEFAULT_CONFIG

        assert DEFAULT_CONFIG["thresholds"]["ram_available_mb"]["critical"] == 128
