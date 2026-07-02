# Contributing to RapidWebs-SysStable

Thank you for considering contributing! This document outlines the process.

## Code of Conduct

This project adheres to a [Code of Conduct](CODE_OF_CONDUCT.md). By participating
you agree to uphold its terms.

## Getting Started

1. **Fork** the repository.
2. **Clone** your fork:
   ```bash
   git clone https://github.com/your-username/rapidwebs-sysstable.git
   cd rapidwebs-sysstable
   ```
3. **Create a virtual environment**:
   ```bash
   uv venv
   source .venv/bin/activate
   ```
4. **Install dev dependencies**:
   ```bash
   uv pip install -e ".[dev]"
   ```

## Development Workflow

1. **Create a branch** off `main`:
   ```bash
   git checkout -b feat/your-feature-name
   ```

2. **Run tests** before and after your changes:
   ```bash
   pytest tests/ -v
   ```

3. **Lint your code**:
   ```bash
   ruff check src/ tests/
   ruff format src/ tests/ --check
   ```

4. **Run the full verification**:
   ```bash
   make check
   ```

5. **Commit** using conventional commits:
   - `feat: ...` — new feature
   - `fix: ...` — bug fix
   - `docs: ...` — documentation
   - `refactor: ...` — code restructure
   - `test: ...` — tests only
   - `ci: ...` — CI/CD changes

6. **Push and open a Pull Request** against `main`.

## Project Structure

```
src/sysstable/           # Main package
  ├── __init__.py        # Version + public API
  ├── __main__.py        # python -m sysstable
  ├── daemon.py          # Collection loop + state management
  ├── collector.py       # psutil metric wrappers
  ├── thresholds.py      # Watermark matching engine
  ├── events.py          # Shell hooks, webhooks, python extensions
  ├── database.py        # SQLite store (WAL mode)
  ├── socketd.py         # Unix socket IPC
  ├── cli.py             # Click CLI
  └── config.py          # YAML config loader
tests/                   # Pytest test suite
docs/                    # Systemd service, docs
hermes-plugin/           # Hermes integration plugin
```

## Testing Guidelines

- Write tests before fixing bugs (TDD preferred).
- Use `tempfile` for ephemeral SQLite databases.
- Tests must not require a running daemon.
- Test threshold logic with edge cases (None values, boundary conditions).
