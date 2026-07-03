# Spec: Critical Memory Pressure Resolution System

> **Feature:** Automated detection, confirmation, and resolution of critical low-memory conditions
> **Project:** RapidWebs-SysStable v0.2.0
> **Status:** Draft Spec
> **Date:** 2026-07-03

---

## 1. Overview

The system monitors available RAM and escalates through a multi-phase protocol when memory pressure reaches a CRITICAL level. It confirms the condition persists, then intelligently identifies, prioritizes, and resolves offending processes — terminating the worst offender and temporarily freezing secondary offenders using a graduated unpause schedule.

The goal is **system stability without unnecessary user disruption**: aggressive enough to prevent OOM, surgical enough to avoid killing the wrong process.

---

## 2. Severity Model — CRITICAL Level

### Current (v0.1.0)

| Severity | Meaning | Plugin Action |
|----------|---------|---------------|
| GREEN | All metrics healthy | Silence |
| YELLOW | Warning threshold breached | Injects context warning |
| ORANGE | Serious threshold breached | Blocks first delegation attempt |
| RED | Critical threshold breached | Blocks delegation, manual release |

### New (v0.2.0)

| Severity | Meaning | Plugin Action | System Action |
|----------|---------|---------------|---------------|
| GREEN | All healthy | Silence | — |
| YELLOW | Warning | Context injection | — |
| ORANGE | Serious | Soft delegation block | — |
| RED | Critical | Hard delegation block | — |
| **CRITICAL** | **Memory emergency** | **Hard block + alert** | **Triggers state machine → resolution** |

### Threshold Definition

```yaml
thresholds:
  ram_available_mb:
    yellow: 1024
    orange: 512
    red: 256
    critical: 128   # NEW — below this = CRITICAL severity
```

`critical` watermarks are **reverse-threshold** (lower = worse), same as existing red/orange/yellow for RAM.

---

## 3. Pressure State Machine

### States

```
NORMAL ──→ CRITICAL_DETECTED ──→ CONFIRMING ──→ COUNTDOWN ──→ RESOLVING ──→ RECOVERED
  ↑                                                                              │
  └──────────────────────────────────────────────────────────────────────────────┘
                                                                            
RESOLVING ──→ RESOLVING (retry, up to max_resolution_cycles)
         └──→ MANUAL_INTERVENTION (after max cycles with no improvement)
```

#### NORMAL
- System metrics all below CRITICAL watermark
- No timers or counters active
- **Transition:** First CRITICAL reading → `CRITICAL_DETECTED`

#### CRITICAL_DETECTED
- RAM below `critical` watermark detected
- Counter initialized to 0
- **Transition:** Counter < `confirmation_intervals` AND still critical → increment counter, remain in state
- **Transition:** Metrics improve above CRITICAL → reset counter, return to `NORMAL`
- **Transition:** Counter >= `confirmation_intervals` AND still critical → `COUNTDOWN`

#### CONFIRMING *(renamed from CRITICAL_DETECTED for clarity)*
- **Transition:** Metric improves → `NORMAL`
- **Transition:** Counter >= X → `COUNTDOWN`

#### COUNTDOWN
- Y-second timer started
- **Transition:** Timer expired → `RESOLVING`
- **Transition:** Metrics improve → cancel timer, `NORMAL`

#### RESOLVING
- Resolution event fires (see §6)
- After resolution completes → `RECOVERED`

#### RECOVERED
- Brief cooldown period (1 interval)
- **Transition:** → `NORMAL`

### Config Keys

```yaml
memory_pressure:
  confirmation_intervals: 5    # X — intervals of consecutive CRITICAL before countdown
  countdown_seconds: 90        # Y — seconds to wait before firing resolution
```

---

## 4. Process Intelligence Engine

**File:** `src/sysstable/process_watch.py`

### 4.1 ProcessSnapshot Dataclass

```python
@dataclass
class ProcessSnapshot:
    pid: int
    name: str
    cmdline: str
    create_time: float
    memory_rss_mb: float
    memory_percent: float
    cpu_percent: float
    io_read_bytes: int
    io_write_bytes: int
    status: str  # running, sleeping, zombie, stopped, etc.
    username: str
```

### 4.2 Collection

**Two-tier collection model:**

| Tier | What | Frequency | When |
|------|------|-----------|------|
| System metrics | RAM, CPU, disk, net, swap | Every `interval_seconds` (default 15s) | Always (existing) |
| Process snapshots (full) | Per-process: memory, CPU, IO | Every `process_snapshot_interval` (default 60s) | When state != NORMAL |
| Process snapshots (lightweight) | Top 20 by memory, name+PID only | Every `normal_snapshot_interval` (default 300s) | When state == NORMAL (for historical data) |

```yaml
memory_pressure:
  process_snapshot_interval: 60      # Seconds between FULL process snapshots during pressure
  normal_snapshot_interval: 300      # Seconds between LIGHTWEIGHT snapshots during NORMAL state
```

Function: `fetch_all_processes() -> list[ProcessSnapshot]`
- Wraps `psutil.process_iter(['pid','name','cmdline','cpu_percent','memory_info','memory_percent','io_counters','status','create_time','username'])`
- Catches zombie processes and permission errors per-PID (doesn't fail on single PID error)
- Returns sorted list by memory_rss_mb descending
- **Has a 5-second timeout** — if psutil takes longer, returns partial results and logs warning for diagnostics

### 4.3 Offender Scoring Algorithm

**Score =** `w_mem * memory_score + w_cpu * cpu_score + w_io * io_score + w_hist * history_score`

Where:
- `memory_score = min(memory_percent / max_memory_percent_threshold, 1.0)`
- `cpu_score = min(cpu_percent / max_cpu_percent_threshold, 1.0)`
- `io_score = min(io_total_bytes / max_io_bytes_threshold, 1.0)`
- `history_score = historical_persistence_factor` (see §4.5)
- `w_mem = 0.5` (primary — we're solving memory pressure)
- `w_cpu = 0.25`
- `w_io = 0.15`
- `w_hist = 0.10`

Configurable:
```yaml
process_scoring:
  memory_weight: 0.5
  cpu_weight: 0.25
  io_weight: 0.15
  history_weight: 0.10
  max_memory_percent: 50.0   # Anything above this = full memory score
  max_cpu_percent: 80.0       # Anything above this = full cpu score
  max_io_mbps: 100.0          # MB/s threshold for full IO score
```

### 4.4 Possible False-Positive Detection

**Heuristic:** A process is a "possible false positive" if:
```
memory_percent >= memory_weight * 0.5    (significant memory)
AND cpu_percent <= cpu_false_positive_threshold    (low CPU activity)
AND io_total_bps <= io_false_positive_threshold    (low disk IO)
```

These processes likely have large but **stale/cached** working sets — the kernel could reclaim this memory without process termination. They are:
- Added to a "possible_false_positive" list
- Scored with a penalty modifier (`score *= 0.5`)
- Still eligible for the kill list but much lower priority

```yaml
process_scoring:
  cpu_false_positive_threshold: 5.0       # % CPU below = possible false positive
  io_false_positive_threshold_mbps: 1.0   # MB/s IO below = possible false positive
  false_positive_penalty: 0.5             # Score multiplier for possible FPs
```

### 4.5 Historical Pattern Analysis

On event fire, for each candidate process:
1. Query `process_snapshots` table for snapshots of this PID/name over the last 24h
2. Calculate **persistence score**: `entries_with_high_memory / total_entries`
   - 1.0 = always high memory → chronic offender
   - 0.0 = never seen before → acute offender (less likely to recur)
3. This feeds into `history_score` in the scoring algorithm

```python
def calc_history_score(pid: int, name: str, db: MetricsDB) -> float:
    """0.0 = never seen in DB, 1.0 = persistent high consumer."""
    history = db.query_process_snapshots(pid, name, hours=24)
    if not history:
        return 0.5  # Neutral — unknown process
    high_memory_count = sum(1 for h in history if h.memory_percent > 10.0)
    return min(high_memory_count / max(len(history), 1), 1.0)
```

### 4.6 Process Pinning

**"Pinned"** processes are those identified as:
1. On the `possible_false_positive` list from heuristic analysis
2. Processes with a history score > 0.8 (persistent non-offender — always present, never the cause)
3. Any process the user adds to `pinned_processes` in config

Pinned processes are moved to the **absolute bottom** of the kill list — they'll only be killed if every other option is exhausted.

```yaml
process_scoring:
  pinned_processes: []  # User-specified pinned processes (names or PIDs)
```

---

## 5. Kill List Management

### 5.1 KillListGenerator

**File:** `src/sysstable/process_watch.py`

Maintains an in-memory sorted list that is **always up to date**:

```python
class KillListGenerator:
    """Maintains the in-memory curator's kill list."""
    
    def __init__(self, config, no_kill_mgr: NoKillManager):
        self._list: list[KillListEntry] = []
        self._generation_count: int = 0
    
    def regenerate(self, processes: list[ProcessSnapshot]) -> list[KillListEntry]:
        """Full regeneration of the kill list."""
        # 1. Filter: remove no-kill + pinned + kernel/system processes
        # 2. Score: apply scoring algorithm
        # 3. Sort: descending by score
        # 4. Persist: dump to DB every Z regenerations
        # 5. Return: the ordered kill list
```

**KillListEntry:**
```python
@dataclass
class KillListEntry:
    pid: int
    name: str
    cmdline: str
    score: float
    memory_mb: float
    cpu_percent: float
    reason: str  # Why this process is on the list
    is_false_positive: bool
```

### 5.2 No-Kill Lists

**Critical safety rule:** Process identification for no-kill matching uses a **triple of (pid, name, cmdline)**, never PID alone. This prevents PID-recycling attacks where a new malicious process inherits a protected PID.

Three layers, evaluated in order (first match wins):

#### Layer 1: Hard-Coded Built-in (immutable)
```python
HARD_CODED_NO_KILL = frozenset({
    # Kernel/init
    "init", "systemd", "kthreadd", "kworker/*", "ksoftirqd/*",
    "migration/*", "watchdog/*", "rcu*", "mm_percpu_wq",
    # System critical
    "systemd-journald", "systemd-logind", "systemd-udevd",
    "systemd-resolved", "systemd-timesyncd", "systemd-oomd",
    "dbus-daemon", "dbus-broker",
    # This daemon
    "sysstable", "sysstabled",
    # Security
    "sshd", "login", "sudo", "polkitd",
    # Container runtime
    "dockerd", "containerd", "runc",
})
```

#### Layer 2: User Config (`config.yaml`)
```yaml
never_kill:
  user_list:
    - "sshd"
    - "cron"
    - "NetworkManager"
    - "rsyslogd"
    - "polkitd"
    - "systemd-journald"
    - "login"
    - "dbus-daemon"
    - "systemd-logind"
    - "systemd-udevd"
```

#### Layer 3: CLI / ENV Override
- CLI: `sysstable start --never-kill firefox --never-kill 1234`
- ENV: `SYSTABLE_NEVER_KILL=firefox,1234,chrome`

These **append** to the config list, they do not replace it.

### 5.3 KillList Entry

```python
@dataclass
class KillListEntry:
    pid: int
    name: str
    cmdline: str
    score: float
    memory_mb: float
    reason: str
```

### 5.4 Database Persistence

**Table: `kill_list_generations`**

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| timestamp | REAL | Unix timestamp |
| trigger_reason | TEXT | "scheduled" or "pre-resolution" |
| entries_json | TEXT | JSON array of KillListEntry dicts |
| entry_count | INTEGER | Number of entries |
| system_memory_available_mb | REAL | Available RAM at generation time |

The generator dumps to DB every `kill_list_persistence_interval` regenerations (default: 5).
DB retains last `kill_list_history_max` generations (default: 50).

---

## 6. Resolution Executor

**File:** `src/sysstable/resolver.py`

### 6.1 Execution Flow

```
RESOLVING:
  1. Access in-memory KillListGenerator._list
  2. If list empty → log CRITICAL warning, transition to RECOVERED
  3. Pop #1 entry → KILL (see §6.2)
  4. Pop next U entries → PAUSE (see §6.3)
  5. Schedule UNPAUSE timers (see §6.4)
  6. Log all actions, record in resolution_events table
  7. Transition to RECOVERED
```

### 6.2 Process Termination (Position #1)

```
kill_process(entry):
  1. Send SIGTERM
  2. Log: "[AUDIT] Termination sent: PID={pid} NAME={name} SIGNAL=SIGTERM"
  3. Wait T seconds (sigterm_timeout_seconds, default 10)
  4. Check if process still alive
  5. If alive → send SIGKILL
     Log: "[AUDIT] Force kill: PID={pid} NAME={name} SIGNAL=SIGKILL"
  6. If dead from SIGTERM → log success
  7. If process already dead → log, skip gracefully
```

### 6.3 Process Pausing (Positions #2 to #2+U)

```
for i, entry in enumerate(kill_list[1:1+U], start=2):
    send SIGSTOP to entry.pid
    Log: "[AUDIT] Paused: PID={pid} NAME={name} POSITION={i}"
```

### 6.4 Reverse Exponential Unpause *(linear decreasing as specified)*

**Formula:** position `i` (1-indexed, `i >= 2`) is paused for:
```
pause_seconds_i = L * max(1, U - (i - 2))
```

Where:
- `L` = `pause_duration_seconds` (default 10)
- `U` = `pause_count` (default 3)

**Example with U=3, L=10:**
| Position | Entry | Pause Duration |
|----------|-------|----------------|
| 2 | 2nd biggest | L * 3 = 30 seconds |
| 3 | 3rd biggest | L * 2 = 20 seconds |
| 4 | 4th biggest | L * 1 = 10 seconds |

After pause expires → send SIGCONT:
```
Log: "[AUDIT] Unpaused: PID={pid} NAME={name} PAUSED_FOR={seconds}s"
```

### 6.5 Safety Guards

1. **Skip already-dead processes** — check `psutil.pid_exists()` before actions
2. **Handle EPERM gracefully** — skip processes we don't have permission to manage
3. **Don't kill the daemon** — NoKillManager must include "sysstable" / own PID
4. **Rate-limit process collection** — 5-second timeout on `fetch_all_processes`
5. **Handle systemd auto-restart** — log that killed process may restart automatically; optional `systemctl --user stop` for known services configured in `resolution.systemd_managed_services`
6. **Resolution re-entrance guard** — prevent a second resolution from starting while one is in progress using a `_resolving` flag
7. **Fork bomb mitigation** — if a resolution cycle does not free enough memory (memory_after < memory_before + threshold), repeat resolution up to 3 times. After 3 consecutive failures, transition to `MANUAL_INTERVENTION` state and stop automatic resolution

### 6.6 Config Keys

```yaml
resolution:
  auto_resolve: true                    # Enable automatic resolution
  sigterm_timeout_seconds: 10           # T — SIGTERM→SIGKILL grace period
  pause_count: 3                        # U — offenders to pause after killing #1
  pause_duration_seconds: 10            # L — base pause duration
  max_resolution_cycles: 3              # Max resolution attempts before manual intervention
  min_freed_memory_mb: 64               # Minimum MB freed to consider resolution successful
  systemd_managed_services: []          # Services to `systemctl --user stop` instead of SIGKILL
```

---

## 7. Database Schema — New Tables

### kill_list_generations
```sql
CREATE TABLE IF NOT EXISTS kill_list_generations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   REAL    NOT NULL,
    trigger     TEXT    NOT NULL,       -- 'scheduled' | 'pre-resolution'
    config_hash TEXT,                   -- Hash of current no-kill config
    entries     TEXT    NOT NULL,       -- JSON array of KillListEntry
    entry_count INTEGER NOT NULL,
    mem_avail_mb REAL                   -- System memory at generation time
);
```

### resolution_events
```sql
CREATE TABLE IF NOT EXISTS resolution_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       REAL    NOT NULL,
    memory_before   REAL,
    memory_after    REAL,
    killed_pid      INTEGER,
    killed_name     TEXT,
    killed_signal   TEXT,               -- SIGTERM or SIGKILL
    killed_oomed    INTEGER,            -- Did kill actually free memory?
    paused_pids     TEXT,               -- JSON array of paused PIDs
    success         INTEGER NOT NULL,   -- 1 = successful
    duration_ms     REAL
);
```

### process_snapshots
```sql
CREATE TABLE IF NOT EXISTS process_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       REAL    NOT NULL,
    pid             INTEGER NOT NULL,
    name            TEXT    NOT NULL,
    memory_rss_mb   REAL,
    memory_percent  REAL,
    cpu_percent     REAL,
    io_read_mb      REAL,
    io_write_mb     REAL,
    status          TEXT
);
CREATE INDEX IF NOT EXISTS idx_process_snapshots_pid_name ON process_snapshots(pid, name);
CREATE INDEX IF NOT EXISTS idx_process_snapshots_ts ON process_snapshots(timestamp);
```

---

## 8. CLI Additions

### New Commands

```bash
# View current kill list
sysstable kill-list [--format json|table] [--limit N]

# View live processes (one-shot)
sysstable processes [--sort memory|cpu|io] [--limit N]

# View never-kill configuration
sysstable never-kill

# View resolution history
sysstable resolution-history [--limit N]
```

### New Global Flags

```bash
sysstable start --never-kill firefox  # Append to never-kill list
```

### ENV Variables

```bash
SYSTABLE_NEVER_KILL="firefox,chrome,1234"  # Append to never-kill list
```

---

## 9. Hermes Plugin — CRITICAL Integration

The existing plugin (`hermes-plugin/rapidwebs-sysstable/`) needs a new severity handler:

| Severity | pre_tool_call | pre_llm_call |
|----------|---------------|--------------|
| CRITICAL | **Blocks ALL delegation** (not just first attempt) | Injects `[SYSTEM STATUS: CRITICAL MEMORY PRESSURE]` with active resolution info |

The plugin reads `state.json` which the daemon updates — the new `CRITICAL` severity and resolution status need to be written to state.json.

---

## 10. Implementation Phases

| Phase | What | Files | Est. Tests |
|-------|------|-------|------------|
| **1** | CRITICAL severity + thresholds | `thresholds.py`, `config.py` | 5 |
| **2** | ProcessSnapshot + fetch_all | `process_watch.py` | 5 |
| **3** | NoKillManager (3 layers) | `process_watch.py`, `config.py` | 8 |
| **4** | KillListGenerator + scoring | `process_watch.py`, `process_scoring.py` | 10 |
| **5** | DB schema (3 new tables) | `database.py` | 5 |
| **6** | Process snapshot persistence | `process_watch.py`, `daemon.py` | 5 |
| **7** | State machine | `state_machine.py`, `daemon.py` | 10 |
| **8** | Resolution executor | `resolver.py` | 10 |
| **9** | CLI commands | `cli.py` | 5 |
| **10** | Hermes plugin update | `hermes-plugin/` | 2 |
| **11** | Integration + e2e tests | `tests/` | 10 |
| **12** | Documentation + config template | `docs/`, `config.py` | — |
| **13** | Audit pass (forward + reverse) | All | — |

**Total estimate:** ~75 new tests across 7 new source files and 7 modified files.

---

## 11. Edge Cases & Safety

| Scenario | Handling |
|----------|----------|
| Kill list empty (all excluded) | Log CRITICAL warning, skip resolution |
| #1 process already dead | Skip gracefully, move to next |
| No permission to kill/stop a process | Catch PermissionError, skip, log |
| Systemd auto-restarts killed process | Log warning; optional `systemctl --user stop` for configured services |
| Fork bomb / rapid PID recycling | Rate-limit collection; match on (pid, name, cmdline) triple; max 3 resolution cycles |
| Config file has invalid never-kill entries | Validate at load, warn + skip invalid |
| Process snapshot collection too expensive | 5-second timeout; lightweight snapshots during NORMAL |
| SIGKILL doesn't work (kernel process) | NoKillManager already excludes these |
| Countdown timer interrupted | Timer is in-process, not interrupt-driven |
| Daemon itself on kill list | Hard-coded exclusion (Layer 1) |
| Resolution fires but memory stays critical | Max 3 retry cycles, then MANUAL_INTERVENTION state |
| Concurrent resolution attempts | Re-entrance guard flag prevents overlap |