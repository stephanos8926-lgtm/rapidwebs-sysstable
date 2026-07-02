"""CLI — sysstable command-line interface (click)."""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import click

from .config import default_config_path, load_config
from .daemon import run_daemon
from .database import MetricsDB
from .socketd import query_daemon


@click.group()
@click.option("--config", "-c", type=click.Path(), default=None, help="Config file path")
@click.pass_context
def cli(ctx: click.Context, config: str | None) -> None:
    """RapidWebs SysStable — System Stability Monitor.

    Monitors system metrics, thresholds them against configurable watermarks,
    and integrates with Hermes via plugin hooks.
    """
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config


@cli.command()
@click.pass_context
def init(ctx: click.Context) -> None:
    """Initialize directories and create default config."""
    config_path = Path(ctx.obj.get("config_path") or default_config_path())
    config = load_config(str(config_path) if ctx.obj.get("config_path") else None)

    dirs = [
        Path(config["db_path"]).parent,
        Path(config["state_path"]).parent,
        Path(config["socket_path"]).parent,
        Path(config.get("events", {}).get("shell_hooks_dir", "")),
        Path(config.get("events", {}).get("python_extensions_dir", "")),
    ]

    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        click.echo(f"  ✓ {d}")

    # Write default config if doesn't exist
    if not config_path.exists():
        import yaml

        from .config import DEFAULT_CONFIG

        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(yaml.dump(DEFAULT_CONFIG, default_flow_style=False))
        click.echo(f"  ✓ {config_path} (default config written)")

    click.echo("\n✅ sysstable initialized")


@cli.command()
@click.option("--foreground", "-f", is_flag=True, help="Run in foreground")
@click.pass_context
def start(ctx: click.Context, foreground: bool) -> None:
    """Start the sysstable daemon."""
    config_path = ctx.obj.get("config_path")

    if not foreground:
        # Start as background process
        cmd = [sys.executable, "-m", "sysstable", "start", "--foreground"]
        if config_path:
            cmd.extend(["--config", config_path])
        try:
            proc = subprocess.Popen(  # noqa: S603 — cmd is sys.executable + controlled args
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            click.echo(f"sysstabled started (PID {proc.pid})")
        except Exception as e:
            click.echo(f"Failed to start: {e}", err=True)
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )
        click.echo("sysstabled running in foreground (Ctrl+C to stop)...")
        run_daemon(config_path=config_path, foreground=True)


@cli.command()
@click.pass_context
def stop(ctx: click.Context) -> None:
    """Stop the sysstable daemon."""
    config = load_config(ctx.obj.get("config_path"))
    result = query_daemon(config["socket_path"], "stop")
    if "error" in result:
        click.echo(f"⚠️ {result['error']}")
    else:
        click.echo("✅ sysstabled stopped")


@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show daemon status and current metrics."""
    config = load_config(ctx.obj.get("config_path"))
    result = query_daemon(config["socket_path"], "metrics_latest")

    if "error" in result:
        click.echo(f"⚠️ Daemon: {result['error']}")
        # Try direct DB read
        db_path = config["db_path"]
        if Path(db_path).exists():
            db = MetricsDB(db_path)
            latest = db.get_latest()
            db.close()
            if latest:
                metrics = latest.get("metrics", latest)
                _print_metrics(metrics)
                return
        click.echo("No metrics available")
        return

    metrics = result.get("metrics", {})
    if isinstance(metrics, dict) and "metrics" in metrics:
        _print_metrics(metrics["metrics"])
    elif isinstance(metrics, dict):
        _print_metrics(metrics)


@cli.command()
@click.option("--count", "-n", default=5, help="Number of recent readings")
@click.pass_context
def history(ctx: click.Context, count: int) -> None:
    """Show recent metric history."""
    config = load_config(ctx.obj.get("config_path"))
    result = query_daemon(config["socket_path"], "metrics_recent", count=count)

    if "error" in result:
        click.echo(f"⚠️ Daemon: {result['error']}")
        db_path = config["db_path"]
        if Path(db_path).exists():
            db = MetricsDB(db_path)
            recent = db.query_recent(limit=count)
            db.close()
            _print_history(recent)
            return
        click.echo("No history available")
        return

    _print_history(result.get("metrics", []))


@cli.command()
@click.option("--count", "-n", default=5, help="Last N readings for trend")
@click.pass_context
def trend(ctx: click.Context, count: int) -> None:
    """Show metric trends (last N readings)."""
    config = load_config(ctx.obj.get("config_path"))
    db_path = config["db_path"]
    if not Path(db_path).exists():
        click.echo("No metrics database found")
        return

    db = MetricsDB(db_path)
    recent = db.query_recent(limit=count)
    db.close()

    if not recent:
        click.echo("No data")
        return

    for i, entry in enumerate(reversed(recent)):
        m = entry.get("metrics", entry)
        ts = entry.get("timestamp", m.get("timestamp", 0)) / 1_000_000_000
        t = time.strftime("%H:%M:%S", time.localtime(ts))
        ram = m.get("ram", {})
        cpu = m.get("cpu", {})
        click.echo(
            f"  {t}  RAM:{ram.get('available_mb', '?'):>6.0f}MB  "
            f"CPU:{cpu.get('percent', '?'):>5.1f}%  "
            f"Load:{cpu.get('load_15m', '?'):>5.2f}"
        )


@cli.command()
@click.pass_context
def uninstall(ctx: click.Context) -> None:
    """Remove all sysstable artifacts and stop daemon."""
    config = load_config(ctx.obj.get("config_path"))

    # Stop daemon
    query_daemon(config["socket_path"], "stop")

    # Clean up files
    paths_to_remove = [
        Path(config["db_path"]).parent,
        Path(config["socket_path"]),
        Path(config["socket_path"]).parent,
    ]
    for p in paths_to_remove:
        if p.exists():
            if p.is_dir():
                import shutil

                shutil.rmtree(p, ignore_errors=True)
                click.echo(f"  ✗ removed: {p}")
            else:
                p.unlink(missing_ok=True)
                click.echo(f"  ✗ removed: {p}")

    click.echo("\n✅ sysstable uninstalled")


def _print_metrics(metrics: dict[str, Any]) -> None:
    """Pretty-print current metrics."""
    ts = metrics.get("timestamp", 0) / 1_000_000_000
    t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    click.echo(f"📊 sysstable — {t}")
    click.echo("─" * 50)

    ram = metrics.get("ram", {})
    click.echo(
        f"RAM:     {ram.get('available_mb', '?'):>6.0f}MB avail / "
        f"{ram.get('total_mb', '?'):>6.0f}MB total ({ram.get('percent', '?')}%)"
    )
    if ram.get("zram_used_mb", 0) > 0:
        click.echo(f"ZRAM:    {ram.get('zram_used_mb', 0):>6.0f}MB used / {ram.get('zram_total_mb', 0):>6.0f}MB")

    swap = metrics.get("swap", {})
    click.echo(
        f"SWAP:    {swap.get('used_mb', '?'):>6.0f}MB / "
        f"{swap.get('total_mb', '?'):>6.0f}MB ({swap.get('percent', '?')}%)"
    )

    cpu = metrics.get("cpu", {})
    click.echo(
        f"CPU:     {cpu.get('percent', '?'):>5.1f}%  "
        f"load: {cpu.get('load_1m', '?'):.2f} / "
        f"{cpu.get('load_5m', '?'):.2f} / "
        f"{cpu.get('load_15m', '?'):.2f}"
    )

    disk = metrics.get("disk", {})
    for part in disk.get("partitions", []):
        click.echo(
            f"DISK:    {part.get('mountpoint', '?')}  "
            f"{part.get('free_mb', '?'):>6.0f}MB free / "
            f"{part.get('total_mb', '?'):>6.0f}MB ({part.get('percent', '?')}%)",
        )

    battery = metrics.get("battery")
    if battery:
        click.echo(f"BAT:     {battery.get('percent')}% {'🔌' if battery.get('power_plugged') else '🔋'}")

    temps = metrics.get("temperatures", {})
    if temps:
        for sensor, entries in temps.items():
            for entry in entries:
                if entry.get("current", 0) > 0:
                    click.echo(f"TEMP:    {sensor}: {entry['current']}°C")

    uptime = metrics.get("uptime_seconds", 0)
    days, rem = divmod(uptime, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    click.echo(f"UPTIME:  {int(days)}d {int(hours)}h {int(mins)}m")


def _print_history(entries: list[dict[str, Any]]) -> None:
    """Print historical metric table."""
    if not entries:
        click.echo("No data")
        return

    click.echo(f"{'Time':<20} {'RAM avail':>10} {'CPU%':>6} {'Load 5m':>8} {'Disk /':>10}")
    click.echo("─" * 60)
    for entry in entries:
        m = entry.get("metrics", entry)
        ts = entry.get("timestamp", m.get("timestamp", 0)) / 1_000_000_000
        t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        ram = m.get("ram", {})
        cpu = m.get("cpu", {})
        disk = m.get("disk", {})
        root_free = "?"
        for part in disk.get("partitions", []):
            if part.get("mountpoint") == "/":
                root_free = f"{part.get('free_mb', 0):.0f}MB"
                break
        click.echo(
            f"{t:<20} {ram.get('available_mb', 0):>8.0f}MB  "
            f"{cpu.get('percent', 0):>5.1f}% "
            f"{cpu.get('load_5m', 0):>7.2f}  "
            f"{root_free:>10}"
        )
