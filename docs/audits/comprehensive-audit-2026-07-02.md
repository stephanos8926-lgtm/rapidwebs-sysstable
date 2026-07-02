# RapidWebs-SysStable — Comprehensive Codebase Audit

**Date:** 2026-07-02
**Version:** 0.1.0 (`a85f3c3`)
**Auditor:** Lucien (RapidWebs Lead Digital Architect)
**Scope:** Entire source tree — `src/sysstable/`, `tests/`, `hermes-plugin/`, config

---

## Executive Summary

**Overall Rating: B+** — Functionally complete for v0.1, architecturally sound, with a clean test suite and strong open-source package. Four medium-severity issues found (duplicate cross-module logic, incomplete iowait, missing SocketServer->daemon wiring, no CI runner caching). No critical bugs or security vulnerabilities.

| Review | Finding Count | Verdict |
|--------|--------------|---------|
| Forward Review | 3 flags | Mostly aligned with spec; 2 gaps |
| Reverse Review | 1 issue | Metric round-trip verified; timestamp provenance ambiguous |
| Adversarial Review | 5 issues | 2 medium, 3 low |
| Code Quality Review | 8 issues | 2 medium, 6 low |
| Feature Completion | 1 gap, 2 partials | 1 unimplemented metric (iowait) |

---

## 1️⃣ Forward Review — Does code match spec?

### Spec: `docs/spec.md` aligned against `src/sysstable/`

| Spec Claim | Code Evidence | Verdict |
|-----------|--------------|---------|
| "Collects every 15s (configurable)" | `config.py` default `interval_seconds: 15`, `daemon.py` uses `config.get("interval_seconds", 15)` | ✅ |
| "Writes metrics → SQLite (WAL, retention)" | `database.py` — WAL mode, `SCHEMA_SQL`, `write()`, `prune()` | ✅ |
| "Writes current state → state.json" | `daemon.py` line 82 — `state_path.write_text(json.dumps(...))` | ✅ |
| "Exposes unix socket for CLI comms" | `socketd.py` — `SocketServer` class, `query_daemon()` function | ✅ |
| "Event dispatch: shell hooks, webhooks, python" | `events.py` — all three present | ✅ |
| RAM: total, used, available, percent, zram | `collector.py` — all five present | ✅ |
| SWAP: total, used, percent | `collector.py` — all three present | ✅ |
| CPU: per-core %, load avg 1/5/15m, iowait | `collector.py` — per-core ✓, load ✓, **iowait → hardcoded 0.0** | ⚠️ **Incomplete** |
| DISK: per-partition, IO ops/s | `collector.py` — partitions ✓, IO rate ✓ | ✅ |
| NET: per-device bytes, errors | `collector.py` — bytes_sent/recv, errors, drops | ✅ |
| BATTERY: percent, plugged, secs | `collector.py` — all three | ✅ |
| TEMP: per-sensor | `collector.py` — sensors_temperatures, label/current/high/critical | ✅ |
| Orange severity blocks first attempt, allows retry | `hermes-plugin/__init__.py` — returns `context` (not `block: True`) on orange | ⚠️ **No retry tracking** |
| "24h | 72h | 120h | 168h | 336h retention" | `config.py` default 72h, DB prune accepts `retain_hours` | ✅ |
| "green = Silence" | Plugin returns `{}` on green | ✅ |
| "yellow = Inject warning via pre_llm_call" | Plugin `_pre_llm_call` injects `[SYSTEM STATUS]` on yellow+ | ✅ |
| "red = Block delegate_task via pre_tool_call" | Plugin `_pre_tool_call` returns `{"block": True, ...}` on red | ✅ |

### Forward Review Findings

**F1 — iowait_percent is hardcoded** (Medium)
- `collector.py` line 159: `metrics.iowait_percent = 0.0  # simplified`
- The `/proc/stat` parser is present but doesn't extract iowait. The iowait column is the 6th field in `/proc/stat` (`man proc`), but the code just sets 0.0.
- **Fix needed:** Parse `parts[5]` from `/proc/stat` cpu line.

**F2 — Orange retry tracking is declared but unused** (Medium)
- `hermes-plugin/__init__.py` line 36: `_ORANGE_RETRY_TRACKER: dict[str, bool] = {}` is declared but never read or written.
- The spec says "Orange blocks first attempt, allows retry", but the plugin only injects context — no state tracking between calls.
- **Fix needed:** Implement retry tracker state across Hermes hook invocations.

**F3 — Spec mentions `swap-in/out` metrics** (Low)
- `collector.py` doesn't capture `swap.sin` / `swap.sout` from `psutil.swap_memory()`. The daemon uses `percent` only. Spec claims "MB, %, ops" for swap.

---

## 2️⃣ Reverse Review — Tracing outputs backward

### Trace 1: `sysstable status` → unix socket → daemon → collect → psutil

```
CLI `status` command
  → query_daemon(socket_path, "metrics_latest")
    → unix socket connect → send JSON {"action": "metrics_latest"}
      → SocketServer._handle_request → MetricsDB.get_latest()
        → SQL query: SELECT ... ORDER BY timestamp_ns DESC LIMIT 1
        → _row_to_dict: deserializes data_json, injects timestamp
      → Response JSON → socket → CLI
    → _print_metrics(metrics["metrics"])
```

✅ **Round-trip verified.** Data flows cleanly through socket → DB → JSON → presentation.

### Trace 2: Daemon lifecycle → collect → write → state.json → plugin read

```
daemon.run_daemon()
  → collect() → SystemMetrics.to_dict()
    → db.write(metrics_dict) → SQLite INSERT
    → state_path.write_text(json.dumps(state))
      → Hermes plugin _read_state() → json.loads(state_path.read_text())
```

✅ **Verified.** Plugin reads the exact same state.json the daemon writes.

### Trace 3: Timestamp provenance

```
collect() → time.time_ns() → metrics.timestamp
  → db.write() stores as timestamp_ns column
  → get_latest() → _row_to_dict() → data["timestamp"] = row["timestamp_ns"]
  → CLI prints: ts / 1_000_000_000 → strftime
```

✅ **Verified.** Timestamps are in nanoseconds throughout; CLI converts to seconds for display.

### Reverse Review Finding

**R1 — `data_json` rollup ambiguity** (Low)
- `_row_to_dict()` renames the SQL `timestamp_ns` column to `data["timestamp"]`, overwriting the original `metrics["timestamp"]` that was stored inside `data_json`. Both should be the same value, but if they ever diverge (DB corruption, future schema change), the code silently uses `timestamp_ns` and loses the original.

---

## 3️⃣ Adversarial Review — What breaks it?

### A1 — Uncontrolled growth of `data_json` (Medium)

**File:** `database.py:36`
- `write()` serializes the **entire** metrics dict (including nested `temperatures`, `interfaces`, `partitions`) into `data_json TEXT`.
- At 15s intervals for 72h: ~17,280 rows. Each row is ~2–8KB of JSON.
- **Worst case:** ~138MB for the DB. This is acceptable but should be documented.
- **Better:** Store only changed metrics as deltas; keep full snapshots every N cycles.

**Risk:** OK for now. Monitor if DB exceeds 500MB.

### A2 — Daemon has no PID file / lock (Medium)

**File:** `daemon.py`
- `sysstable start` spawns `subprocess.Popen` with `start_new_session=True`.
- No PID file written, no lock check. Running `start` twice spawns two daemons writing to the same DB and socket.
- Second daemon's socket `bind()` will fail if `socket_path` already exists, but `SocketServer.start()` line 29 does `if self.socket_path.exists(): self.socket_path.unlink()` — so the **second daemon clobbers the first's socket**, and the first daemon's `_server.accept()` will get `OSError` on stale fd.

**Risk:** Two daemons = double writes, silent data duplication, broken socket communication.

### A3 — Background daemon orphan on terminal close (Low)

**File:** `cli.py:77`
- `subprocess.Popen(cmd, stdout=DEVNULL, stderr=DEVNULL, start_new_session=True)` detaches from the parent process group.
- If the terminal closes, the daemon survives — correct.
- But the daemon's logging goes to stderr (DEVNULL in background mode). **No log file by default.**

**Risk:** Silent failures in background mode. Users can't see errors.

### A4 — Shell hook scripts can block the daemon loop (Low)

**File:** `events.py:42`
- Each shell hook runs synchronously with a 10s timeout.
- If a hook hangs but doesn't time out (SIGKILL can't be blocked, but SIGTERM can be caught), the daemon's collection loop is paused.
- **Mitigation:** The `except Exception` in `daemon.py:104` catches this, but the loop iterates per-violation, not per-hook. A single hook timeout delays the next collection by up to 10s.

**Risk:** Acceptable for v0.1. Future: async dispatch or thread pool.

### A5 — Python extension modules leak reference on every dispatch (Low)

**File:** `events.py:83-92`
- Each dispatch calls `importlib.util.spec_from_file_location()` + `exec_module()` — this loads and executes the extension from scratch.
- It does NOT cache loaded modules. If the same extension fires N times per cycle × M cycles, the module is loaded N×M times.
- `importlib` caches by `sys.modules` key, but `spec_from_file_location` doesn't use a consistent key — runs may create fresh module objects.

**Risk:** Low for small extensions. If extensions hold state or register signal handlers, repeated loading will cause leaks.

---

## 4️⃣ Code Quality Review

### CQ1 — Duplicate `_get_violation_value` (Medium)

**Files:** `daemon.py:124-142` and `hermes-plugin/__init__.py:140-161`
- Both modules have identical `_get_violation_value()` functions with the same 5 metric branches.
- **Fix:** Extract to a shared utility module (`src/sysstable/utils.py` or `src/sysstable/thresholds.py`).

### CQ2 — `__import__("json")` antipattern in hot path (Medium)

**File:** `database.py:36`
- `__import__("json").dumps(metrics)` instead of `import json` at module level.
- `json` is stdlib — there's no reason to lazy-import it. This is slightly slower per call and obfuscates intent.
- **Fix:** Move `import json` to the top of the file.

### CQ3 — `_row_to_dict()` uses late import inside function (Low)

**File:** `database.py:87`
- `import json as _json` inside `_row_to_dict()` — same issue as CQ2.
- **Fix:** Module-level `import json`.

### CQ4 — `config.py` default paths computed at import time (Low)

**File:** `config.py:11, 13-17`
- `DEFAULT_CONFIG_PATH` and values inside `DEFAULT_CONFIG` use `Path.home()` at import time.
- If `$HOME` changes between import and use (rare but possible in Docker or sudo contexts), the defaults will point to the original home.
- **Fix:** Make `load_config()` resolve paths lazily.

### CQ5 — No context manager for `MetricsDB` (Low)

**File:** `database.py`
- `MetricsDB` has `close()` but does not implement `__enter__` / `__exit__`.
- Several callers must remember to call `.close()` explicitly (socketd lines 79, 87, 95, cli lines 119, 146, 169).
- **Fix:** Add `__enter__`/`__exit__` for context manager support.

### CQ6 — Test coverage gaps (Low)

**File:** `tests/test_core.py`
| Component | Lines | Tested? |
|-----------|-------|---------|
| `collector.collect()` | 249 | 2 tests — structure + RAM range |
| `database.MetricsDB` | 94 | 4 tests — write/read, query, prune, empty |
| `thresholds.evaluate_thresholds()` | 117 | 7 tests |
| `config.load_config()` | 53 | 2 tests |
| `events.dispatch_events()` | 96 | 2 tests — no-op and error path |
| `socketd.SocketServer` | 123 | **0 tests** |
| `socketd.query_daemon()` | — | 0 tests |
| `cli.*` | 298 | **0 tests (no Click runner)** |
| `daemon.run_daemon()` | 142 | **0 tests** |
| Hermes plugin | 175 | 2 tests — import + severity mapping |

**Gaps:** Socket layer, daemon lifecycle, CLI invocations, and event dispatch success path are untested. These are the integration-heavy components where bugs manifest.

### CQ7 — iowait is hardcoded but /proc/stat is already open (Low)

**File:** `collector.py:153-162`
- The file reads `/proc/stat`, splits `cpu` line, and... writes `0.0`. The iowait column (`proc` field 5) is available at `parts[5]` but not used.
- The comment `# simplified` acknowledges this.
- **Fix:** 4 lines of actual code needed (handle the case where `/proc/stat` doesn't have 6 fields on older kernels).

### CQ8 — No logging configuration for non-daemon components (Low)

**File:** Only `daemon.py` and `collector.py` configure loggers. The `SocketServer`, `events.py`, and CLI don't set up handlers. Events dispatched in background mode produce logs that go nowhere.

---

## 5️⃣ Feature Completion Review

### Features listed in README vs Implementation

| Feature | Status | Notes |
|---------|--------|-------|
| RAM collection (total, used, avail, %, zram) | ✅ Complete | |
| SWAP (total, used, percent) | ✅ Complete | Missing `swap.sin/sout` per spec |
| CPU (per-core %, load 1/5/15m) | ✅ Complete | |
| CPU iowait | ⚠️ Partial | Hardcoded 0.0 |
| Disk partitions per-mount | ✅ Complete | |
| Disk IO rate (read/write MB/s) | ✅ Complete | |
| Net per-device I/O rate | ✅ Complete | |
| Battery sensors | ✅ Complete | |
| Temperature sensors | ✅ Complete | |
| Uptime | ✅ Complete | |
| ZRAM from /proc/swaps | ✅ Complete | |
| Threshold engine (Y/O/R) | ✅ Complete | |
| Reverse comparison (lower=worse) | ✅ Complete | |
| Hermes pre_tool_call block | ✅ Complete | |
| Hermes pre_llm_call context | ✅ Complete | |
| Orange retry tracking | ❌ Missing | Declared but never implemented |
| Shell hook dispatch | ✅ Complete | |
| Webhook dispatch | ✅ Complete | |
| Python extension dispatch | ✅ Complete | |
| SQLite WAL storage | ✅ Complete | |
| Retention/pruning | ✅ Complete | |
| Unix socket IPC | ✅ Complete | |
| CLI: init, start, stop, status, history, trend, uninstall | ✅ Complete | |
| systemd service | ✅ Complete | Template in `docs/` |
| Docker support | ✅ Complete | `Dockerfile` + `docker-compose.yml` |
| PyPI publish workflow | ✅ Complete | |
| MIT License | ✅ Complete | |
| Full open-source docs | ✅ Complete | README, CONTRIBUTING, CHANGELOG, CODE_OF_CONDUCT, SECURITY |
| Pre-commit hooks | ✅ Complete | |
| Ruff lint + format | ✅ Complete | |

---

## 📋 Consolidated Action Items

### Must Fix (Medium Severity)

| # | Item | File | Effort |
|---|------|------|--------|
| 1 | Extract shared `_get_violation_value` to utility module | `daemon.py`, `hermes-plugin/__init__.py` | 15 min |
| 2 | Implement iowait parsing from `/proc/stat` (field 5) | `collector.py:159` | 10 min |
| 3 | Add PID file / lock to prevent double-daemon | `daemon.py`, `cli.py` | 30 min |
| 4 | Move `import json` to module level in database.py | `database.py:36,87` | 5 min |
| 5 | Implement orange retry tracking in Hermes plugin | `hermes-plugin/__init__.py` | 20 min |

### Should Fix (Low Severity)

| # | Item | File | Effort |
|---|------|------|--------|
| 6 | Add context manager support to MetricsDB | `database.py` | 10 min |
| 7 | Add swap sin/sout to collector | `collector.py` | 5 min |
| 8 | Resolve `$HOME` at config load time, not import time | `config.py` | 10 min |
| 9 | Add logging handler for background daemon mode | `cli.py`, `daemon.py` | 15 min |
| 10 | Add tests for socketd, daemon, CLI | `tests/` | 2h |
| 11 | Document DB size expectations (at 15s × 72h) | `README.md` | 5 min |
| 12 | Add `__init__.py` exports for public API | `src/sysstable/__init__.py` | 5 min |

### Future (Nice to Have)

| # | Item | Rationale |
|---|------|-----------|
| 13 | Async event dispatch for hooks/webhooks | Prevents daemon loop blocking |
| 14 | Metrics delta storage vs full snapshots | Reduces DB size 10× |
| 15 | CLI completions (shell autocomplete) | Better UX |
| 16 | Prometheus metrics endpoint | Integration with monitoring stacks |
| 17 | Alerting via desktop notification | User-facing alerts without Hermes |

---

## 🔢 Metrics Summary

| Category | Count |
|----------|-------|
| Source files | 11 `.py` + 2 test |
| Total LOC | **1,570** |
| Tests | 20 (1 framework module) |
| Test pass rate | 100% |
| Ruff lint | Clean (0 errors) |
| Ruff format | Clean |
| Type hints | Full coverage (good) |
| Functions | 36 |
| Classes | 5 |
| Docstrings | 36/36 (100%) |
| `# noqa` comments | 4 (all justified) |
| Code duplication | 1 module (6.8% — `_get_violation_value` ×2) |

---

*Audit completed by Lucien (RapidWebs Lead Digital Architect) — 2026-07-02*
