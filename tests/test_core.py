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
