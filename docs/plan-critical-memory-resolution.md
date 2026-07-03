# Implementation Plan: Critical Memory Pressure Resolution System

**Based on:** `docs/spec-critical-memory-resolution.md`
**Execution:** 13 phases, ~75 tests

---

## Phase 1: CRITICAL Severity + Thresholds

### Files
- `src/sysstable/thresholds.py` — Add CRITICAL to Severity enum
- `src/sysstable/config.py` — Add memory_pressure config section
- `src/sysstable/daemon.py` — Wire CRITICAL into severity aggregation
- `src/sysstable/events.py` — Add CRITICAL event dispatch type

### Tasks
1. Add `CRITICAL = "critical"` to `Severity` enum in thresholds.py
2. Add `critical` watermark parsing to threshold evaluation
3. Add new config block to `DEFAULT_CONFIG`:
   ```python
   "memory_pressure": {
       "critical_threshold_mb": 128,
       "confirmation_intervals": 5,
       "countdown_seconds": 90,
       "process_snapshot_interval": 60,
       "kill_list_persistence_interval": 5,
       "kill_list_history_max": 50,
   }
   ```
4. Add resolution config block:
   ```python
   "resolution": {
       "auto_resolve": True,
       "sigterm_timeout_seconds": 10,
       "pause_count": 3,
       "pause_duration_seconds": 10,
   }
   ```
5. Add process_scoring config block:
   ```python
   "process_scoring": {
       "memory_weight": 0.5,
       "cpu_weight": 0.25,
       "io_weight": 0.15,
       "history_weight": 0.10,
       "max_memory_percent": 50.0,
       "max_cpu_percent": 80.0,
       "max_io_mbps": 100.0,
       "cpu_false_positive_threshold": 5.0,
       "io_false_positive_threshold_mbps": 1.0,
       "false_positive_penalty": 0.5,
       "pinned_processes": [],
   }
   ```
6. Add never_kill config block:
   ```python
   "never_kill": {
       "user_list": [
           "sshd", "cron", "NetworkManager", "rsyslogd",
           "polkitd", "systemd-journald", "login", "dbus-daemon",
           "systemd-logind", "systemd-udevd",
       ],
   }
   ```
7. Wire config into daemon's severity aggregation

### Tests
- Verify CRITICAL is a valid Severity member
- Verify threshold evaluation returns CRITICAL when metric below critical watermark
- Verify config loads all new keys with defaults
- Verify config validates never_kill entries

---

## Phase 2: Process Intelligence Engine — ProcessSnapshot + Collection

### Files
- `src/sysstable/process_watch.py` — NEW

### Tasks
1. Define `ProcessSnapshot` dataclass
2. Define `KillListEntry` dataclass
3. Implement `fetch_all_processes() -> list[ProcessSnapshot]`:
   - `psutil.process_iter()` with full info
   - Handle zombies, permission errors per-PID (continue on single PID failure)
   - Calculate memory_rss_mb from rss bytes
   - Calculate IO totals from `io_counters`
   - Return sorted by memory_rss_mb descending
4. Implement `snapshot_processes_to_db(snapshots, db)` to batch-insert

### Audit Fixes Integrated in P2:
- **5-second timeout** on `fetch_all_processes()` — use `signal.alarm()` or threading with timeout
- **Lightweight normal-state snapshots** — top 20 by memory, at `normal_snapshot_interval` (default 300s)

### Tests
- Mock psutil, verify `fetch_all_processes` returns correctly structured data
- Verify error handling (permission denied on one PID doesn't crash)
- Verify sorting is by memory_rss_mb descending
- Verify empty list on no processes
- Verify 5-second timeout returns partial results
- Verify lightweight mode (NORMAL state) returns fewer fields

---

## Phase 3: No-Kill Manager

### File
- `src/sysstable/process_watch.py` — NoKillManager class

### Tasks
1. Implement `HARD_CODED_NO_KILL` frozen set with all kernel/system processes
2. Implement `NoKillManager` class:
   - Constructor: takes user_config_list, cli_overrides, env_overrides
   - `is_protected(pid: int, name: str, cmdline: str) -> bool` — checks all 3 layers
   - `get_protected_names() -> set[str]` — returns all protected names (for display)
   - `validate_config_entries(entries) -> tuple[valid, invalid]` — validates user config
3. Merge logic: user_config_list + CLI overrides + ENV overrides → combined set
4. Hard-coded list also catches kernel PIDs by matching name patterns (glob-like)
5. **Audit fix: Triple matching** — `is_protected()` matches on `(pid, name, cmdline)` not PID alone, preventing PID-recycling attacks
6. **Audit fix: ENV validation** — `SYSTABLE_NEVER_KILL` entries validated: PIDs must be valid integers, names must be non-empty

### Tests
- Verify hard-coded system processes return `is_protected=True`
- Verify user config processes return protected
- Verify CLI overrides append to config
- Verify invalid PID in overrides is handled gracefully
- Verify daemon's own PID/name is always protected

---

## Phase 4: Kill List Generator + Scoring

### File
- `src/sysstable/process_watch.py` — KillListGenerator + ProcessScorer

### Tasks
1. Implement `ProcessSnapshot.score()` method or `ProcessScorer.calculate_score()`:
   - Weighted combination of memory, cpu, io, history
   - False-positive heuristic: high memory + low cpu + low io → penalty modifier
2. Implement `KillListGenerator`:
   - `__init__(config, no_kill_mgr, db)`
   - `regenerate(snapshots: list[ProcessSnapshot]) -> list[KillListEntry]`
   - Filtering pipeline: remove no-kill → score → sort → return
   - Pinned processes moved to bottom (score *= 0 for ordering)
   - Track `_generation_count` for persistence interval
   - `get_kill_list() -> list[KillListEntry]` — returns current in-memory list
   - `get_current_list_for_action() -> list[KillListEntry]` — returns list for resolution (pop from working copy)
3. Implement `get_possible_false_positives(snapshots) -> list[ProcessSnapshot]`

### Tests
- Verify scoring weights work correctly
- Verify false-positive heuristic identifies cached processes
- Verify pinned processes are at bottom of list
- Verify no-kill processes are removed from list
- Verify empty list when all processes are protected

---

## Phase 5: Database Schema — New Tables

### File
- `src/sysstable/database.py`

### Tasks
1. Add `kill_list_generations` table creation to `MetricsDB.__init__`
2. Add `resolution_events` table creation
3. Add `process_snapshots` table creation + indexes
4. Add query methods:
   - `save_kill_list_generation(generation) -> int`
   - `save_resolution_event(event) -> int`
   - `save_process_snapshots(snapshots) -> int`
   - `query_process_snapshots(pid, name, hours) -> list`
   - `query_kill_list_history(limit) -> list`
   - `query_resolution_history(limit) -> list`
   - `prune_process_snapshots(retain_hours) -> int`
5. Add pruning for process_snapshots in existing prune cycle

### Tests
- Verify all new tables are created
- Verify insert and query round-trip
- Verify index on pid+name for history queries
- Verify pruning removes old snapshots

---

## Phase 6: Process Snapshot Collection in Daemon Loop

### Files
- `src/sysstable/daemon.py`
- `src/sysstable/process_watch.py`

### Tasks
1. In daemon's main collection loop:
   - After system metrics collection, check pressure state
   - If state != NORMAL: check if it's time for FULL process snapshot (based on `process_snapshot_interval`)
   - If state == NORMAL: check if it's time for LIGHTWEIGHT snapshot (based on `normal_snapshot_interval`, default 300s)
   - If yes: call `fetch_all_processes()` with appropriate mode and `snapshot_processes_to_db()`
2. After each system metrics collection:
   - Call `KillListGenerator.regenerate()` to keep in-memory list fresh
   - Check `kill_list_persistence_interval` for DB dump

### Tests
- Verify FULL snapshots collected during pressure states
- Verify LIGHTWEIGHT snapshots collected during NORMAL at 300s intervals
- Verify snapshot interval timing
- Verify kill list regeneration happens every collection cycle

---

## Phase 7: Pressure State Machine

### Files
- `src/sysstable/state_machine.py` — NEW
- `src/sysstable/daemon.py` — Integration

### Tasks
1. Define `PressureState` enum: `NORMAL, CRITICAL_DETECTED, CONFIRMING, COUNTDOWN, RESOLVING, RECOVERED, MANUAL_INTERVENTION`
2. Implement `PressureStateMachine`:
   - `__init__(config)`
   - `update(metrics: dict, config) -> PressureState` — core transition logic
   - Internal counters: `_critical_counter`, `_countdown_timer_remaining`, `_recovered_cooldown`, `_resolution_cycle_count`
   - `should_fire_resolution() -> bool` — returns True when countdown hits 0
   - `cancel_resolution()` — reset to NORMAL
   - `on_resolution_complete(success: bool)` — success → RECOVERED, failure → increment cycle count; if >= max_resolution_cycles → MANUAL_INTERVENTION
   - `get_state() -> PressureState`
   - `get_metrics() -> dict` — state info for display/logging
3. Wire into daemon's main loop:
   - After metrics collection, call `state_machine.update(metrics, config)`
   - If `should_fire_resolution()` → trigger resolver
   - If `cancel_resolution()` → cancel pending resolution
   - If state == `MANUAL_INTERVENTION` → log CRITICAL alert, stop automatic processing

### Tests
- Test full transition: NORMAL → CRITICAL_DETECTED → CONFIRMING → COUNTDOWN → RESOLVING → RECOVERED
- Test cancellation: COUNTDOWN → NORMAL (metrics improve before timer expires)
- Test cancellation from CONFIRMING
- Test counter/interval tracking
- Test RECOVERED cooldown

---

## Phase 8: Resolution Executor

### File
- `src/sysstable/resolver.py` — NEW

### Tasks
1. Implement `kill_process(entry, timeout) -> bool`:
   - Send SIGTERM, start timer
   - After T seconds, check `psutil.pid_exists(pid)`
   - If alive: send SIGKILL
   - Return success/failure
   - Log every step
2. Implement `pause_process(entry) -> bool`:
   - Send SIGSTOP
   - Return success/failure
   - Log
3. Implement `unpause_process(entry) -> bool`:
   - Send SIGCONT
   - Log with pause duration
4. Implement `process_pause_unpause_schedule(kill_list, config)`:
   - Kill #1 via `kill_process`
   - Pause next U via `pause_process`
   - Schedule unpause timers:
     ```
     position 2: wait L*U → SIGCONT
     position 3: wait L*(U-1) → SIGCONT
     position 4: wait L*(U-2) → SIGCONT
     ```
   - All unpauses happen via asyncio (daemon is async) or threading
5. Implement `MemoryPressureResolver.resolve(kill_list, config, db)`:
   - **Re-entrance guard** — check `_resolving` flag; if True, return immediately
   - Set `_resolving = True` at start, release on completion
   - Call process_pause_unpause_schedule
   - Check memory freed: if `memory_after < memory_before + min_freed_memory_mb`, return `success=False`
   - Record resolution_events in DB including `success` flag
   - Return `ResolutionResult` dataclass
6. Implement `systemd_service_stop(service_name)`:
   - Runs `systemctl --user stop <service>` for services in `resolution.systemd_managed_services`
   - Called before SIGKILL for matching processes

### Tests
- Mock psutil to verify kill/stop/continue signal calls
- Verify SIGTERM → wait → SIGKILL sequence
- Verify pause schedule timing
- Verify already-dead process handling
- Verify PermissionError handling

---

## Phase 9: CLI Commands

### File
- `src/sysstable/cli.py`

### Tasks
1. Add `sysstable kill-list` command:
   - Reads in-memory list via socket IPC
   - `--format json|table`
   - `--limit N`
2. Add `sysstable processes` command:
   - One-shot `fetch_all_processes()` call
   - `--sort memory|cpu|io`
   - `--limit N`
   - `--watch` (repeated snapshot)
3. Add `sysstable never-kill` command:
   - Display current protected process list
   - `--add PID,NAME` (runtime addition)
   - `--remove NAME` (runtime removal)
4. Add `sysstable resolution-history` command:
   - Reads from resolution_events table
   - `--limit N`
   - `--format json|table`
5. Add `--never-kill` to `sysstable start`

### Tests
- CLI invocation tests (click test runner)
- Verify output formats

---

## Phase 10: Hermes Plugin Update

### File
- `hermes-plugin/rapidwebs-sysstable/__init__.py`

### Tasks
1. Add CRITICAL severity check in `pre_tool_call` hook:
   - CRITICAL → BLOCK every delegation (not just first)
2. Add CRITICAL severity check in `pre_llm_call` hook:
   - CRITICAL → inject `[SYSTEM STATUS: CRITICAL MEMORY PRESSURE — active resolution in progress]`
3. Update state.json fields to include:
   - `severity` — include "critical"
   - `resolution_active` — boolean
   - `resolution_info` — dict with current action

### Tests
- Mock state.json, verify CRITICAL block behavior
- Verify pre_llm_call context injection

---

## Phase 11: Integration Tests

### File
- `tests/test_integration.py` — NEW

### Tasks
1. Full daemon mock integration:
   - Mock metrics → CRITICAL threshold
   - Verify state machine transitions
   - Verify process collection triggers
   - Verify kill list regeneration
   - Verify resolution execution
2. Mock resolution lifecycle:
   - Mock processes with known priorities
   - Verify #1 gets SIGTERM → SIGKILL
   - Verify #2-4 get SIGSTOP
   - Verify unpause schedule
3. Config validation integration:
   - Verify all new config keys load and apply
   - Verify invalid config produces warnings

### Tests
- 10 tests covering the above scenarios

---

## Phase 12: Documentation & Config Template

### Tasks
1. Update README with new features section
2. Update `src/sysstable/config.py` default config generation with all new keys
3. Update `docs/sysstable.service` if needed
4. Add CHANGELOG entry for v0.2.0

---

## Phase 13: Forward + Reverse + Adversarial Audit

### Tasks
1. **Forward audit** — Does plan cover every requirement from spec?
2. **Reverse audit** — What could go wrong? Missing edge cases?
3. **Adversarial audit** — What would an attacker exploit?
4. **Audit artifact** — `docs/audits/phase2-audit-2026-07-03.md`

---

## Summary

| Phase | Files | Tests | Dependencies |
|-------|-------|-------|-------------|
| P1 | 4 modified | 5 | — |
| P2 | 1 new | 5 | P1 (config) |
| P3 | 1 file | 8 | P1 (config) |
| P4 | 1 file | 10 | P2 + P3 |
| P5 | 1 modified | 5 | — |
| P6 | 2 files | 5 | P2 + P4 + P5 |
| P7 | 1 new + 1 mod | 12 | P1 + P6 |
| P8 | 1 new | 14 | P4 + P7 |
| P9 | 1 modified | 5 | P4 + P5 |
| P10 | 1 modified | 2 | P1 |
| P11 | 1 new | 12 | All above |
| P12 | 2 files | — | All above |
| P13 | 1 new | — | All above |

**Total:** 7 new source files, ~8 modified, 4 new test files, ~88 tests
