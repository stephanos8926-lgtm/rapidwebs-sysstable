# Deployment Guide

> **Goal:** Install and run `rapidwebs-sysstable` on a fresh Linux server.

## Table of Contents

- [Quick Install (PyPI)](#quick-install-pypi)
- [Production Server Setup](#production-server-setup)
- [Docker Deployment](#docker-deployment)
- [Systemd Service](#systemd-service)
- [Configuration](#configuration)
- [Monitoring & Verification](#monitoring--verification)
- [Updating](#updating)
- [Uninstalling](#uninstalling)

---

## Quick Install (PyPI)

The fastest way to get running:

```bash
# Install the package
pip install rw-sysstable

# Initialize directories + default config
sysstable init

# Start the daemon
sysstable start

# Verify it's collecting metrics
sysstable status
```

---

## Production Server Setup

### Prerequisites

- **Python ≥ 3.10** (`python3 --version`)
- **Linux** with `/proc` filesystem (psutil requirement)
- **Hermes Agent** (optional, for plugin integration)
- **Systemd** (recommended for persistence)
- **pip** or **uv** (`pip install --upgrade pip`)

### Step-by-Step

#### 1. Install the Package

```bash
# From PyPI
pip install rw-sysstable

# Or from source for the latest
git clone https://github.com/stephanos8926-lgtm/rapidwebs-sysstable.git
cd rapidwebs-sysstable
./install.sh
```

#### 2. Initialize

```bash
sysstable init
```

This creates:
- `~/.config/sysstable/config.yaml` — default configuration
- `~/.cache/sysstable/` — database and socket location

#### 3. Configure

Edit `~/.config/sysstable/config.yaml`:

```yaml
interval_seconds: 15
retention_hours: 72
thresholds:
  ram_available_mb: { yellow: 1024, orange: 512, red: 256, critical: 128 }
```

Adjust thresholds to match your server's RAM. For a 4GB server (like rw-server-01), the defaults are appropriate. For servers with more RAM, scale proportionally.

#### 4. Test the Daemon

```bash
# Run in foreground first to verify it works
sysstable start --foreground
# Ctrl+C to stop
```

#### 5. Install as Systemd Service

```bash
make install-systemd
# Or manually:
cp docs/sysstable.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now sysstable
```

#### 6. Verify

```bash
systemctl --user status sysstable
sysstable status
sysstable history -n 3
```

### Example: Deploy to rw-server-01

```bash
# Quick deploy via Makefile
make deploy

# Or manually:
scp dist/rapidwebs_sysstable-*.whl rw-server-01:/tmp/
ssh rw-server-01
pip install /tmp/rapidwebs_sysstable-*.whl
sysstable init
systemctl --user enable --now sysstable
```

---

## Docker Deployment

### Build and Run

```bash
# Build the image
make docker-build

# Or use the docker-compose stack
make docker-run
```

### docker-compose.yml

The compose file mounts `/proc` and `/sys` for psutil access, uses `pid: host` for process monitoring, and configures persistent volumes for data and configuration.

```bash
# View logs
docker compose logs -f

# Stop
docker compose down
```

---

## Systemd Service

### User Service (Recommended)

The provided `docs/sysstable.service` is a **user service** (not system-wide).

```bash
cp docs/sysstable.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now sysstable
```

### System-Wide Service

For multi-user systems or services that start before user login:

```bash
sudo cp docs/sysstable.service /etc/systemd/system/
# Edit the ExecStart path to use absolute paths
sudo systemctl daemon-reload
sudo systemctl enable --now sysstable
```

---

## Monitoring & Verification

### Health Checks

```bash
# Daemon status
sysstable status

# Recent metrics
sysstable history -n 5

# Metric trends
sysstable trend -n 5

# Memory pressure resolution history
sysstable resolution-history
```

### Logs

```bash
# Systemd
journalctl --user -u sysstable -f

# Docker
docker compose logs -f
```

### Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| `sysstable status` fails | Daemon not running | `sysstable start` |
| `sysstable: command not found` | Package not installed | `pip install rw-sysstable` |
| "Permission denied" on socket | Socket path permissions | Check `socket_path` in config.yaml |
| "No metrics collected yet" | Daemon just started | Wait 15s for first collection |
| Hermes plugin not blocking | Plugin not enabled | `hermes config set plugins.rapidwebs-sysstable.enabled true` |

---

## Updating

```bash
# Via PyPI
pip install --upgrade rw-sysstable

# Via source
cd ~/Workspaces/rapidwebs-sysstable
git pull
./install.sh

# Restart daemon after update
systemctl --user restart sysstable
```

---

## Uninstalling

```bash
# Stop daemon
systemctl --user stop sysstable 2>/dev/null

# Remove systemd service
rm -f ~/.config/systemd/user/sysstable.service
systemctl --user daemon-reload

# Remove package
pip uninstall rapidwebs-sysstable -y

# Remove data and config (optional)
rm -rf ~/.config/sysstable ~/.cache/sysstable

# Or use the CLI
sysstable uninstall
```