# RapidWebs-SysStable — System Stability Plugin

**Version:** v0.1-draft
**Author:** Lucien (RapidWebs Lead Digital Architect)
**Status:** Draft spec

---

## Overview

A Hermes-integrated system stability monitor that collects key machine metrics in a background daemon, thresholds them against configurable watermarks, and feeds results into Hermes via plugin hooks — providing real-time resource awareness and autonomous system pressure management.

## Architecture

```
┌──────────────────────────────────────────────────┐
│  Daemon (sysstabled)                             │
│  - Python, systemd --user timer/service          │
│  - Runs every 15s (configurable)                 │
│  - Reads /proc + psutil sensors                  │
│  - Writes metrics → SQLite (WAL, retention)      │
│  - Writes current state → state.json             │
│  - Exposes unix socket for CLI comms             │
│  - Event dispatch: shell hooks, webhooks, python  │
└────┬──────────────┬──────────────────────────────┘
     │              │
     │ state.json   │ unix socket
     ▼              ▼
┌─────────────┐  ┌──────────────────────────────┐
│ Hermes      │  │ CLI (sysstable)              │
│ Plugin      │  │ - status, metrics, history   │
│ (pre_tool,  │  │ - daemon lifecycle           │
│  pre_llm)   │  │ - threshold config           │
└─────────────┘  └──────────────────────────────┘
```

## Monitored Metrics

| Category | Metrics | Source | Unit |
|----------|---------|--------|------|
| RAM | total, used, available, percent, zram stats | `/proc/meminfo`, psutil | MB, % |
| SWAP | total, used, percent, swap-in/out | psutil.swap_memory() | MB, %, ops |
| CPU | per-core %, load avg 1/5/15m, iowait | psutil, /proc/loadavg | %, float |
| DISK | per-partition: total/used/free, IO ops/s | psutil.disk_usage/io_counters | MB, ops/s |
| NET | per-device: bytes sent/recv/s, errors | psutil.net_io_counters | bytes/s |
| BATTERY | percent, plugged, secs left | psutil.sensors_battery | %, bool, s |
| TEMP | per-sensor: CPU, GPU, NVMe | psutil.sensors_temperatures | °C |
| UPTIME | boot time, uptime seconds | psutil.boot_time | s |

## Thresholds → Behavior

| Severity | Hermes Plugin Action |
|----------|----------------------|
| **green** | Silence |
| **yellow** | Injects warning via `pre_llm_call`: "⚠️ RAM at 900MB — consider inline work" |
| **orange** | Injects stronger warning. Blocks first attempt, allows retry |
| **red** | Injects CRITICAL. Blocks `delegate_task` via `pre_tool_call` — must release manually |

Each metric's thresholds are fully configurable per-severity in YAML.

## Retention

Configurable: 24h | 72h (default) | 120h | 168h | 336h
Daemon prunes old data on each write cycle.

## Project Structure

```
~/Workspaces/rapidwebs-sysstable/
├── src/sysstable/
│   ├── __init__.py
│   ├── __main__.py
│   ├── daemon.py
│   ├── collector.py
│   ├── thresholds.py
│   ├── events.py
│   ├── database.py
│   ├── socketd.py
│   ├── cli.py
│   └── config.py
├── hermes-plugin/rapidwebs-sysstable/
│   ├── __init__.py
│   └── plugin.yaml
├── tests/
├── docs/
├── pyproject.toml
└── README.md
```

## Phases

| Phase | What | Est |
|-------|------|-----|
| **1** | Daemon core + collector + database + state.json output | 6h |
| **2** | Event dispatch: shell hooks, webhooks, python extensions | 4h |
| **3** | Hermes plugin (pre_tool_call + pre_llm_call hooks, threshold eval, context injection, block logic) | 3h |
| **4** | CLI (status, metrics, history, daemon lifecycle, init, uninstall) | 4h |
| **5** | Unix socket daemon↔CLI communication | 3h |
| **6** | Packaging, docs, systemd service, test suite | 4h |
