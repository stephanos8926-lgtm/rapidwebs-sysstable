# RapidWebs-SysStable 🛡️

**System stability monitor with Hermes integration — daemon, CLI, and plugin.**

A background daemon that collects real-time system metrics (RAM, CPU, disk, swap,
temperature, network, battery), thresholds them against configurable watermarks,
and feeds results into [Hermes Agent](https://hermes-agent.nousresearch.com) via
plugin hooks. When resources get tight, Hermes adapts — blocking delegation on
critical states, injecting context warnings on pressure.

---

## ✨ Features

| Capability | What It Does |
|------------|-------------|
| 🔍 **Metric Collection** | RAM, ZRAM, SWAP, CPU (per-core + load avg), disk, net I/O, battery, temperature, uptime — every 15s |
| 🎚️ **Threshold Engine** | Green/Yellow/Orange/CRITICAL watermarks per metric. Supports CRITICAL severity level and threshold watermark for immediate action. Reverse-aware (lower=worse for RAM/disk, higher=worse for CPU/temp) |
| 🤖 **Hermes Plugin** | `pre_tool_call` blocks delegation on RED/CRITICAL, warns on YELLOW/ORANGE. With v0.2.0, it now supports CRITICAL severity blocking. `pre_llm_call` injects `[SYSTEM STATUS]` context. |
| 🖥️ **CLI** | `sysstable status`, `history`, `trend`, `start`, `stop`, `init`, `uninstall`, `kill-list`, `processes`, `never-kill`, `resolution-history`. Supports starting with `--never-kill` flag. |
| 🔌 **Memory Pressure Resolution System** | System for automatically resolving memory pressure situations by intelligently killing non-critical processes when system RAM falls below a defined `critical` threshold. Includes `MemoryPressureResolver`, `PressureStateMachine`, `ProcessSnapshot`, `NoKillManager`, and `KillListGenerator`. |
| 🗄️ **SQLite Storage** | WAL mode, configurable retention (24h–14d), auto-pruning, unix socket IPC. New tables in DB schema for enhanced data management. |
| ⚙️ **Configurable** | YAML config — thresholds, intervals, retention, webhook URLs, hook directories. New config blocks: `memory_pressure`, `resolution`, `process_scoring`, `never_kill`. |
| 🐦 **Security Enhancements** | Triple (pid,name,cmdline) matching for process identification and security. |
| 🐚 **systemd Support** | `--user` service template with `Restart=on-failure` |

---

## 🏗️ Architecture

```
┌──────────────────────────────────────────────────┐
│  Daemon (sysstabled)                             │
│  - Python, systemd --user service                │
│  - Collects every 15s (configurable)             │
│  - Reads /proc + psutil                          │
│  - Writes metrics → SQLite (WAL, retention)      │
│  - Writes current state → state.json             │
│  - Evaluates thresholds → dispatches events      │
└────┬──────────────┬─────────────┬────────────────┘
     │              │             │
     │ state.json   │ unix socket │ Memory Pressure Resolution Layer
     ▼              ▼             ▼ (monitors state.json, acts on critical thresholds)
┌─────────────┐  ┌──────────────────────────────┐   ┌──────────────────────────┐
│ Hermes      │  │ CLI (sysstable)              │   │ Resolver (memory_pressure) │
│ Plugin      │  │ - status                     │   │ - Manages resolution lifecycle │
│ (pre_tool,  │  │ - history, trend             │   │ - Integrates with Process Watch modules │
│  pre_llm)   │  │ - daemon lifecycle           │   └──────────────────────────┘
└─────────────┘  │ - init, uninstall            │
                 │ - kill-list, processes, etc. │
                 └──────────────────────────────┘
```

---

## 📦 Installation

### Requirements

- Python ≥ 3.10
- Linux (reads `/proc`; psutil sensors)
- [Hermes Agent](https://hermes-agent.nousresearch.com) (for plugin integration)

### From PyPI

```bash
pip install rw-sysstable
```

### From Source

```bash
git clone https://github.com/stephanos8926-lgtm/rapidwebs-sysstable.git
cd rapidwebs-sysstable
uv venv
source .venv/bin/activate
uv pip install -e ."[dev]"
```

### Hermes Plugin

```bash
# Install the plugin from github repo via hermes plugins install
hermes plugins install https://github.com/stephanos8926-lgtm/rapidwebs-sysstable/hermes-plugin/rapidwebs-sysstable

# Enable it
hermes config set plugins.rapidwebs-sysstable.enabled true
```

---

## 🚀 Quick Start

```bash
# 1. Initialize directories + default config
sysstable init

# 2. Start the daemon (background)
sysstable start

# 3. Check system status
sysstable status

# 4. View metric history
sysstable history -n 10

# 5. See trends
sysstable trend -n 10

# 6. Stop the daemon
sysstable stop

# 7. View process list (for debugging memory pressure)
sysstable processes

# 8. Generate a kill list for non-critical processes
sysstable kill-list

# 9. Check resolution history
sysstable resolution-history

# Start the daemon, preventing it from killing processes initially
sysstable start --never-kill
```

### Systemd (persistent)

```bash
cp docs/sysstable.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now sysstable
```

---

## ⚙️ Configuration

`~/.config/sysstable/config.yaml` — auto-created on `sysstable init`.

```yaml
interval_seconds: 15           # Collection interval
retention_hours: 72            # Data retention (24 | 72 | 120 | 168 | 336)
db_path: ~/.cache/sysstable/metrics.db
socket_path: ~/.cache/sysstable/sysstable.sock
state_path: ~/.hermes/plugins/rapidwebs-sysstable/state.json

# Memory Pressure Resolution Configuration
memory_pressure:
  enabled: true                # Enable automatic memory pressure resolution
  check_interval_seconds: 30   # How often to check for memory pressure
  severity_threshold: 128      # Memory available in MB to trigger resolution (e.g., CRITICAL threshold < 128 MB)
  grace_period_seconds: 120    # Time to wait before killing processes after pressure detected

resolution:
  # Settings for the MemoryPressureResolver
  max_kill_attempts: 5         # Max processes to attempt killing in one cycle
  min_process_ram_mb: 100      # Minimum RAM usage to consider a process for termination
  max_cpu_usage_percent: 50    # Don't kill processes consuming high CPU (e.g., active services)

process_scoring:
  # Scoring parameters for prioritizing processes to kill
  ram_usage_weight: 0.6
  cpu_usage_weight: 0.1
  runtime_weight: 0.1
  importance_score_weight: 0.2 # Higher score = less important

never_kill:
  # List of processes (by pid, name, or cmdline) to never kill
  user_list: ["hermes-agent", "sysstabled"] # Process names to protect (from config file)

events:
  shell_hooks_dir: ~/.config/sysstable/hooks.d
  webhooks: []
  python_extensions_dir: ~/.config/sysstable/extensions.d
thresholds:
  ram_available_mb: { yellow: 1024, orange: 512, red: 256, critical: 128 } # CRITICAL added
  cpu_load_15m:    { yellow: 2.0, red: 4.0 }
  disk_root_free_mb: { yellow: 5120, red: 1024 }
  swap_percent:    { yellow: 50, red: 80 }
  temperature_celsius: { yellow: 80, red: 95 }
```

---

## 🧠 Memory Pressure Resolution System

This system proactively identifies and resolves memory pressure situations by intelligently terminating non-critical processes when system RAM falls below a defined `critical` threshold. It integrates seamlessly with the existing daemon and Hermes Agent, and is controlled via new CLI commands and configuration blocks.

### Lifecycle Overview:

1.  **Detection:** The `sysstabled` daemon continuously monitors system metrics. When `ram_available_mb` drops below the `critical` watermark (defined in `thresholds.critical`), the Memory Pressure Resolution system is triggered.
2.  **Initiation:** The `MemoryPressureResolver` (from `resolver.py`) is engaged. It checks the new `memory_pressure.enabled` configuration. If enabled, and after a `grace_period_seconds`, it begins the resolution process.
3.  **Process Assessment:** The `ProcessSnapshot` class (from `process_watch.py`) gathers detailed information about all running processes, including their PID, name, command line, RAM usage, CPU usage, and runtime.
4.  **Scoring & Prioritization:** The `resolution` configuration block defines parameters for scoring processes. Each process is assigned an importance score based on RAM usage, CPU usage, runtime, and an `importance_score_weight`. The `never_kill` configuration (pids, names, cmdlines) is consulted to exclude critical processes (e.g., `hermes-agent`, `sysstabled` itself).
5.  **Termination:** The `KillListGenerator` (from `process_watch.py`) uses scorers and the `never_kill` list to create a prioritized list of non-critical processes to terminate. The `MemoryPressureResolver` then signals the termination of processes from this list, respecting `max_kill_attempts` and `min_process_ram_mb`.
6.  **Monitoring & Feedback:** The `PressureStateMachine` (from `state_machine.py`) tracks the state of the memory pressure resolution. The CLI command `resolution-history` provides a log of these events. The Hermes plugin receives `CRITICAL` severity alerts, enabling immediate, system-wide actions.

### New Modules:
*   `src/sysstable/process_watch.py`: Contains `ProcessSnapshot`, `NoKillManager`, `KillListGenerator`.
*   `src/sysstable/state_machine.py`: Contains `PressureStateMachine`.
*   `src/sysstable/resolver.py`: Contains `MemoryPressureResolver`.

### New CLI Commands:
*   `sysstable processes`: List all running processes with key metrics.
*   `sysstable kill-list`: Generate and display a prioritized list of processes that *could* be killed.
*   `sysstable resolution-history`: View logs of memory pressure resolution events.
*   `sysstable start --never-kill`: Temporarily disable the auto-kill feature on startup.

---

## 🧪 Development

```bash
# Install with dev deps
uv pip install -e ."[dev]"

# Run tests (80 tests total)
pytest tests/ -v

# Lint
ruff check src/ tests/

# Format
ruff format src/ tests/

# Full check
make check
```

---

## 📊 Threshold Behavior

| Severity | Plugin Action |
|----------|---------------|
| 🟢 Green | Silence |
| 🟡 Yellow | Injects `[SYSTEM STATUS]` context warning via `pre_llm_call` |
| 🟠 Orange | Injects warning, blocks first delegation attempt, allows retry |
| 🔴 Red | Blocks `delegate_task` via `pre_tool_call`. Release manually. |
| 🚨 CRITICAL | Blocks `delegate_task` via `pre_tool_call`. Triggers Memory Pressure Resolution system. |

---

## 🗺️ Project Structure

```
rapidwebs-sysstable/
├── src/sysstable/           # Main package
│   ├── __init__.py          # Version info
│   ├── __main__.py          # python -m entry
│   ├── daemon.py            # Collection loop + state
│   ├── collector.py         # psutil wrappers
│   ├── thresholds.py        # Watermark engine
│   ├── events.py            # Hook/extension dispatch
│   ├── database.py          # SQLite store
│   ├── socketd.py           # Unix IPC
│   ├── cli.py               # Click CLI
│   ├── config.py            # YAML loader
│   ├── process_watch.py     # Process (snapshot, no-kill, kill-list) modules
│   ├── state_machine.py     # State machine for pressure resolution
│   └── resolver.py          # Memory pressure resolver logic
├── hermes-plugin/           # Hermes integration
│   └── rapidwebs-sysstable/
├── tests/                   # Pytest suite (80 tests)
├── docs/                    # systemd service
├── pyproject.toml           # Build config
├── Makefile                 # Dev commands
└── Dockerfile               # Container build
```

---

## 🔗 Related

- [Hermes Agent](https://hermes-agent.nousresearch.com) — AI agent platform
- [rapidwebs-sysstable Hermes Plugin](https://github.com/stephanos8926-lgtm/rapidwebs-sysstable/tree/main/hermes-plugin) — v0.2.0 with CRITICAL severity blocking
- [psutil](https://github.com/giampaolo/psutil) — System metrics library
- [RapidWebs Enterprise](https://rapidwebs.com) — Digital architecture studio

---

## 📄 License

MIT © 2026 RapidWebs Enterprise. See [LICENSE](LICENSE) for details.
