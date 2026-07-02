<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://img.shields.io/badge/status-active-00d26a">
  <img alt="Status: Active" src="https://img.shields.io/badge/status-active-00d26a">
</picture>
<a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License"></a>
<a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-≥3.10-3776AB" alt="Python"></a>
<a href="https://github.com/psf/black"><img src="https://img.shields.io/badge/code%20style-ruff-000000" alt="Ruff"></a>
<a href="https://github.com/stephanos8926-lgtm/rapidwebs-sysstable/actions"><img src="https://img.shields.io/github/actions/workflow/status/stephanos8926-lgtm/rapidwebs-sysstable/tests.yml?branch=main" alt="CI"></a>
<br>

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
| 🎚️ **Threshold Engine** | Green/Yellow/Orange/Red watermarks per metric. Reverse-aware (lower=worse for RAM/disk, higher=worse for CPU/temp) |
| 🤖 **Hermes Plugin** | `pre_tool_call` blocks delegation on RED, warns on YELLOW/ORANGE. `pre_llm_call` injects `[SYSTEM STATUS]` context |
| 🖥️ **CLI** | `sysstable status`, `history`, `trend`, `start`, `stop`, `init`, `uninstall` |
| 🔌 **Event Dispatch** | Shell hooks, webhooks (HTTP POST), Python extension scripts — fire per-violation |
| 🗄️ **SQLite Storage** | WAL mode, configurable retention (24h–14d), auto-pruning, unix socket IPC |
| ⚙️ **Configurable** | YAML config — thresholds, intervals, retention, webhook URLs, hook directories |
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
│  - Exposes unix socket for CLI comms             │
└────┬──────────────┬──────────────────────────────┘
     │              │
     │ state.json   │ unix socket
     ▼              ▼
┌─────────────┐  ┌──────────────────────────────┐
│ Hermes      │  │ CLI (sysstable)              │
│ Plugin      │  │ - status                     │
│ (pre_tool,  │  │ - history, trend             │
│  pre_llm)   │  │ - daemon lifecycle           │
└─────────────┘  │ - init, uninstall            │
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
pip install rapidwebs-sysstable
```

### From Source
```bash
git clone https://github.com/stephanos8926-lgtm/rapidwebs-sysstable.git
cd rapidwebs-sysstable
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"
```

### Hermes Plugin
```bash
# Install the plugin
hermes plugins install ~/Workspaces/rapidwebs-sysstable/hermes-plugin/rapidwebs-sysstable

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
events:
  shell_hooks_dir: ~/.config/sysstable/hooks.d
  webhooks: []
  python_extensions_dir: ~/.config/sysstable/extensions.d
thresholds:
  ram_available_mb: { yellow: 1024, orange: 512, red: 256 }
  cpu_load_15m:    { yellow: 2.0, red: 4.0 }
  disk_root_free_mb: { yellow: 5120, red: 1024 }
  swap_percent:    { yellow: 50, red: 80 }
  temperature_celsius: { yellow: 80, red: 95 }
```

---

## 🧪 Development

```bash
# Install with dev deps
uv pip install -e ".[dev]"

# Run tests
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
|----------|--------------|
| 🟢 Green | Silence |
| 🟡 Yellow | Injects `[SYSTEM STATUS]` context warning via `pre_llm_call` |
| 🟠 Orange | Injects warning, blocks first delegation attempt, allows retry |
| 🔴 Red | Blocks `delegate_task` via `pre_tool_call`. Release manually |

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
│   └── config.py            # YAML loader
├── hermes-plugin/           # Hermes integration
├── tests/                   # Pytest suite
├── docs/                    # systemd service
├── pyproject.toml           # Build config
├── Makefile                 # Dev commands
└── Dockerfile               # Container build
```

---

## 🔗 Related

- [Hermes Agent](https://hermes-agent.nousresearch.com) — AI agent platform
- [psutil](https://github.com/giampaolo/psutil) — System metrics library
- [RapidWebs Enterprise](https://rapidwebs.com) — Digital architecture studio

---

## 📄 License

MIT © 2026 RapidWebs Enterprise. See [LICENSE](LICENSE) for details.
