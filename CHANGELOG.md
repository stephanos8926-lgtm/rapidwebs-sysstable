# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-07-02

### Added (open-source package)

- **README**: full docs with badges, architecture diagram, quick start, config reference
- **LICENSE**: MIT
- **CONTRIBUTING.md**: dev workflow, branch strategy, commit conventions
- **CODE_OF_CONDUCT.md**: Contributor Covenant v2.1
- **SECURITY.md**: supported versions, reporting process, security considerations
- **CHANGELOG.md**: keepachangelog format
- **GitHub Actions**: tests (4 Python versions), lint (ruff), publish (PyPI), release (GitHub Releases)
- **Makefile**: install, test, lint, format, check, clean, build, publish, docker, systemd targets
- **MANIFEST.in**: include templates, plugins, docs in sdist
- **.pre-commit-config.yaml**: ruff lint + format, trailing whitespace, yaml/toml checks
- **Dockerfile**: multi-stage build, slim runtime, non-root user
- **docker-compose.yml**: volume mounts for /proc, /sys, persistence
- **.gitignore**: comprehensive ignores for Python, IDE, OS, build artifacts
- **pyproject.toml**: full metadata, classifiers, URLs, ruff config, pytest config
- **Bugfix**: config shallow copy → deep copy (nested dict isolation)
- **Bugfix**: orphan `main.py` placeholder removed
- **Bugfix**: events dispatch parameter naming (`thresholds_config` → `config`)
- **Bugfix**: webhook payload reference cleanup

### Added (original release)

- **Daemon** (`sysstabled`): background collection loop, signal handling,
  configurable interval (default 15s), SQLite WAL-mode storage with
  configurable retention (24h–14d).
- **Collector**: psutil-based metric gathering for RAM, ZRAM, SWAP, CPU,
  load averages, iowait, disk partitions, disk IO rates, network interfaces,
  battery, temperatures, and uptime.
- **Threshold Engine**: configurable yellow/orange/red watermarks with
  reverse-aware comparison (lower=worse for RAM/disk, higher=worse for CPU/temp).
  Evaluated each cycle; highest severity determines overall system state.
- **Event Dispatch**: shell hooks, webhooks (HTTP POST), and Python extension
  scripts — fired per-violation each cycle.
- **Unix Socket IPC**: daemon listens on `AF_UNIX`; CLI queries metrics
  without polling.
- **CLI** (`sysstable`): 7 commands — `init`, `start`, `stop`, `status`,
  `history`, `trend`, `uninstall`.
- **Hermes Plugin**: 2 hooks — `pre_tool_call` (blocks delegation on RED,
  warns on YELLOW/ORANGE) and `pre_llm_call` (injects `[SYSTEM STATUS]`
  context with violation details).
- **Systemd Service Template**: `--user` unit with `Restart=on-failure`.
- **Test Suite**: 20 tests covering collector, database, thresholds, config,
  events, and plugin logic.
- **Packaging**: hatchling-based `pyproject.toml`, `sysstable` CLI entry point,
  optional dev dependencies.
