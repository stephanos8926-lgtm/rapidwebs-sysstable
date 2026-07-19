"""Metric collector — psutil wrappers for all system metrics."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import psutil

logger = logging.getLogger(__name__)


def parse_psi_file(filepath: str) -> dict[str, Any]:
    """Parse a single PSI pressure file (e.g. /proc/pressure/memory)."""
    res = {
        "some": {"avg10": 0.0, "avg60": 0.0, "avg300": 0.0, "total": 0},
        "full": {"avg10": 0.0, "avg60": 0.0, "avg300": 0.0, "total": 0}
    }
    try:
        with open(filepath) as f:
            for line in f:
                parts = line.strip().split()
                if not parts:
                    continue
                kind = parts[0]  # "some" or "full"
                if kind not in ("some", "full"):
                    continue
                for item in parts[1:]:
                    if "=" in item:
                        k, v = item.split("=", 1)
                        if k in ("avg10", "avg60", "avg300"):
                            res[kind][k] = float(v)
                        elif k == "total":
                            res[kind][k] = int(v)
    except Exception:  # noqa: S110
        pass
    return res


@dataclass
class SystemMetrics:
    """Snapshot of all system metrics at a point in time."""

    timestamp: float  # unix ns

    # RAM
    ram_total_mb: float = 0.0
    ram_used_mb: float = 0.0
    ram_available_mb: float = 0.0
    ram_percent: float = 0.0
    zram_used_mb: float = 0.0
    zram_total_mb: float = 0.0

    # SWAP
    swap_total_mb: float = 0.0
    swap_used_mb: float = 0.0
    swap_percent: float = 0.0
    swap_in_mb: float = 0.0
    swap_out_mb: float = 0.0

    # CPU
    cpu_percent: float = 0.0
    cpu_per_core: list[float] = field(default_factory=list)
    load_1m: float = 0.0
    load_5m: float = 0.0
    load_15m: float = 0.0
    iowait_percent: float = 0.0

    # DISK
    partitions: list[dict[str, Any]] = field(default_factory=list)
    disk_read_mb_s: float = 0.0
    disk_write_mb_s: float = 0.0

    # NET
    interfaces: list[dict[str, Any]] = field(default_factory=list)

    # BATTERY
    battery_percent: float | None = None
    battery_power_plugged: bool | None = None
    battery_secs_left: float | None = None

    # TEMP
    temperatures: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    # UPTIME
    uptime_seconds: float = 0.0

    # PSI
    psi: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "timestamp": self.timestamp,
            "ram": {
                "total_mb": round(self.ram_total_mb, 1),
                "used_mb": round(self.ram_used_mb, 1),
                "available_mb": round(self.ram_available_mb, 1),
                "percent": round(self.ram_percent, 1),
                "zram_used_mb": round(self.zram_used_mb, 1),
                "zram_total_mb": round(self.zram_total_mb, 1),
            },
            "swap": {
                "total_mb": round(self.swap_total_mb, 1),
                "used_mb": round(self.swap_used_mb, 1),
                "percent": round(self.swap_percent, 1),
                "in_mb": round(self.swap_in_mb, 1),
                "out_mb": round(self.swap_out_mb, 1),
            },
            "cpu": {
                "percent": round(self.cpu_percent, 1),
                "per_core": [round(p, 1) for p in self.cpu_per_core],
                "load_1m": round(self.load_1m, 2),
                "load_5m": round(self.load_5m, 2),
                "load_15m": round(self.load_15m, 2),
                "iowait_percent": round(self.iowait_percent, 1),
            },
            "disk": {
                "partitions": self.partitions,
                "read_mb_s": round(self.disk_read_mb_s, 1),
                "write_mb_s": round(self.disk_write_mb_s, 1),
            },
            "net": {
                "interfaces": self.interfaces,
            },
            "battery": {
                "percent": self.battery_percent,
                "power_plugged": self.battery_power_plugged,
                "secs_left": self.battery_secs_left,
            }
            if self.battery_percent is not None
            else None,
            "temperatures": self.temperatures,
            "uptime_seconds": round(self.uptime_seconds, 0),
            "psi": self.psi,
        }
        return result


# ── Previous IO counters for rate calculation ──

_prev_disk_io: dict[str, Any] = {}
_prev_net_io: dict[str, Any] = {}
_prev_time: float = 0.0


def collect() -> SystemMetrics:
    """Collect a full snapshot of system metrics."""
    global _prev_disk_io, _prev_net_io, _prev_time

    now = time.time()
    dt = (now - _prev_time) if _prev_time > 0 else 1.0
    metrics = SystemMetrics(timestamp=time.time_ns())

    # ── RAM ──
    mem = psutil.virtual_memory()
    metrics.ram_total_mb = mem.total / (1024 * 1024)
    metrics.ram_used_mb = mem.used / (1024 * 1024)
    metrics.ram_available_mb = mem.available / (1024 * 1024)
    metrics.ram_percent = mem.percent

    # ZRAM from /proc
    try:
        with open("/proc/swaps") as f:
            for line in f:
                if "zram" in line:
                    parts = line.split()
                    if len(parts) >= 4:
                        metrics.zram_used_mb = int(parts[2]) / 1024
                        metrics.zram_total_mb = int(parts[3]) / 1024
                        break
    except (OSError, ValueError):
        pass

    # ── SWAP ──
    swap = psutil.swap_memory()
    metrics.swap_total_mb = swap.total / (1024 * 1024)
    metrics.swap_used_mb = swap.used / (1024 * 1024)
    metrics.swap_percent = swap.percent
    metrics.swap_in_mb = round(swap.sin / (1024 * 1024), 1) if swap.sin else 0.0
    metrics.swap_out_mb = round(swap.sout / (1024 * 1024), 1) if swap.sout else 0.0

    # ── CPU ──
    metrics.cpu_percent = psutil.cpu_percent(interval=0.1)
    metrics.cpu_per_core = psutil.cpu_percent(interval=0, percpu=True)
    load = psutil.getloadavg()
    metrics.load_1m, metrics.load_5m, metrics.load_15m = load

    try:
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("cpu "):
                    parts = line.split()
                    if len(parts) >= 6:
                        cpu_fields = [int(p) for p in parts[1:]]
                        total = sum(cpu_fields)
                        iowait = cpu_fields[4] if len(cpu_fields) > 4 else 0
                        metrics.iowait_percent = round((iowait / total * 100) if total > 0 else 0.0, 1)
                    break
    except (OSError, ValueError, IndexError):
        logger.debug("Failed to parse /proc/stat for iowait")

    # ── DISK partitions ──
    partitions = []
    for part in psutil.disk_partitions():
        try:
            usage = psutil.disk_usage(part.mountpoint)
            partitions.append(
                {
                    "device": part.device,
                    "mountpoint": part.mountpoint,
                    "fstype": part.fstype,
                    "total_mb": round(usage.total / (1024 * 1024), 1),
                    "used_mb": round(usage.used / (1024 * 1024), 1),
                    "free_mb": round(usage.free / (1024 * 1024), 1),
                    "percent": round(usage.percent, 1),
                }
            )
        except (PermissionError, OSError):
            pass
    metrics.partitions = partitions

    # DISK IO rate
    disk_io = psutil.disk_io_counters()
    if disk_io and _prev_disk_io:
        metrics.disk_read_mb_s = (disk_io.read_bytes - _prev_disk_io.get("read_bytes", 0)) / (1024 * 1024 * dt)
        metrics.disk_write_mb_s = (disk_io.write_bytes - _prev_disk_io.get("write_bytes", 0)) / (1024 * 1024 * dt)
    _prev_disk_io = {"read_bytes": disk_io.read_bytes, "write_bytes": disk_io.write_bytes} if disk_io else {}

    # ── NET interfaces ──
    interfaces = []
    net_io = psutil.net_io_counters(pernic=True)
    for name, counters in net_io.items():
        if_data: dict[str, Any] = {
            "name": name,
            "bytes_sent": 0,
            "bytes_recv": 0,
            "packets_sent": 0,
            "packets_recv": 0,
            "errors": 0,
            "drops": 0,
        }
        prev = _prev_net_io.get(name, {})
        if dt > 0:
            if_data["bytes_sent"] = round((counters.bytes_sent - prev.get("bytes_sent", counters.bytes_sent)) / dt)
            if_data["bytes_recv"] = round((counters.bytes_recv - prev.get("bytes_recv", counters.bytes_recv)) / dt)
            if_data["packets_sent"] = int(
                (counters.packets_sent - prev.get("packets_sent", counters.packets_sent)) / dt
            )
            if_data["packets_recv"] = int(
                (counters.packets_recv - prev.get("packets_recv", counters.packets_recv)) / dt
            )
        if_data["errors"] = counters.errin + counters.errout
        if_data["drops"] = counters.dropin + counters.dropout
        interfaces.append(if_data)
        _prev_net_io[name] = {
            "bytes_sent": counters.bytes_sent,
            "bytes_recv": counters.bytes_recv,
            "packets_sent": counters.packets_sent,
            "packets_recv": counters.packets_recv,
        }
    metrics.interfaces = interfaces
    _prev_time = now

    # ── BATTERY ──
    battery = psutil.sensors_battery()
    if battery is not None:
        metrics.battery_percent = battery.percent
        metrics.battery_power_plugged = battery.power_plugged
        metrics.battery_secs_left = battery.secsleft if battery.secsleft != psutil.POWER_TIME_UNLIMITED else None

    # ── TEMPERATURES ──
    try:
        temps = psutil.sensors_temperatures()
        if temps:
            metrics.temperatures = {}
            for sensor, entries in temps.items():
                metrics.temperatures[sensor] = [
                    {"label": e.label or "", "current": e.current, "high": e.high or 0.0, "critical": e.critical or 0.0}
                    for e in entries
                ]
    except Exception:
        logger.debug("sensors_temperatures unavailable")

    # ── UPTIME ──
    metrics.uptime_seconds = time.time() - psutil.boot_time()

    # ── PSI ──
    metrics.psi = {
        "cpu": parse_psi_file("/proc/pressure/cpu"),
        "memory": parse_psi_file("/proc/pressure/memory"),
        "io": parse_psi_file("/proc/pressure/io"),
    }

    return metrics
