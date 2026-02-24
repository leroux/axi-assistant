# Axi Assistant — Architecture Specification

## System Overview

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│  Discord Server                                                                 │
│                                                                                 │
│  ┌─────────────── Active ───────────────┐  ┌──────────── Killed ─────────┐     │
│  │ #axi-master  #agent-1  #agent-2  ... │  │ #old-task  #finished    ... │     │
│  └───────┬──────────┬──────────┬────────┘  └─────────────────────────────┘     │
│          │          │          │                                                 │
│      User messages (on_message)                                                 │
└──────────┬──────────┬──────────┬────────────────────────────────────────────────┘
           │          │          │
           ▼          ▼          ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│  bot.py  (single asyncio process, 2882 lines)                                   │
│                                                                                 │
│  ┌────────────────────────────────┐    ┌────────────────────────────────────┐   │
│  │  Discord Gateway (discord.py)  │    │  Scheduler (10s loop)              │   │
│  │  • on_message → route to agent │    │  • Reads schedules.json each tick  │   │
│  │  • on_guild_channel_create     │    │  • Cron / one-off event firing     │   │
│  │  • Slash commands              │    │  • Idle detection & reminders      │   │
│  │    /kill, /stop, /restart,     │    │  • Auto-sleep (pressure-based)     │   │
│  │    /compact, /clear, /list,    │    │  • Stranded message safety net     │   │
│  │    /reset-context              │    │                                    │   │
│  └──────────────┬─────────────────┘    └────────────────┬───────────────────┘   │
│                 │ user messages                          │ scheduled prompts     │
│                 │                                        │ (spawn/route agent)   │
│                 ▼                                        ▼                       │
│  ┌──────────────────────────────────────────────────────────────────────────┐   │
│  │  Agent Orchestrator                                                      │   │
│  │                                                                          │   │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐   MAX_AGENTS = 20   │   │
│  │  │ axi-master  │  │  agent-1    │  │  agent-2    │   MAX_AWAKE  = 5    │   │
│  │  │ [sleeping/  │  │ [sleeping/  │  │ [sleeping/  │                     │   │
│  │  │  awake/busy]│  │  awake/busy]│  │  awake/busy]│   Concurrency:      │   │
│  │  │             │  │             │  │             │   • query_lock/agent │   │
│  │  │ MCP: axi,   │  │ MCP: utils  │  │ MCP: utils  │   • _wake_lock      │   │
│  │  │   discord?  │  │             │  │             │   • message_queue    │   │
│  │  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘                     │   │
│  └─────────┼────────────────┼────────────────┼────────────────────────────┘   │
│            │                │                │                                 │
│  ┌─────────┴────────────────┴────────────────┴──────────────────────────────┐  │
│  │  Claude Agent SDK  (per-agent ClaudeSDKClient subprocess)                │  │
│  │  • Streaming responses → text_buffer → Discord messages                  │  │
│  │  • Tool use (Bash, Edit, Read, etc.) sandboxed per-agent cwd             │  │
│  │  • Session resume via session_id                                         │  │
│  └──────────────────────────────────────────────────────────────────────────┘  │
│                                                                                │
│  ┌─────────────────────┐ ┌───────────────────┐ ┌──────────────────────────┐   │
│  │  Rate Limit Manager │ │  Logging           │ │  Permission Layer        │   │
│  │  • Global state     │ │  • orchestrator.log│ │  • OS sandbox (bash)     │   │
│  │  • Auto-retry queue │ │  • <agent>.log each│ │  • can_use_tool (writes) │   │
│  │  • Shared API acct  │ │  • Rotating files  │ │  • cwd + EXTRA_ALLOWED   │   │
│  └─────────────────────┘ └───────────────────┘ └──────────────────────────┘   │
└────────────────────────────────────────────────────────────────────────────────┘
              │
              │  exit codes: 42=restart, 0=stop, crash=rollback
              ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│  supervisor.py  (230 lines)                                                     │
│  • Crash classification (startup <60s vs runtime)                               │
│  • Auto-rollback: git stash + git reset --hard                                  │
│  • Marker files: .rollback_performed, .crash_analysis                           │
│  • Output tee: .bot_output.log                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
              │
              │  systemd restart on non-zero exit
              ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│  systemd (axi-bot.service)                                                      │
└─────────────────────────────────────────────────────────────────────────────────┘

Data files (all on disk, no database):
┌──────────────────┬─────────────────────┬──────────────────┬─────────────────────┐
│ schedules.json   │ schedule_skips.json │ USER_PROFILE.md  │ .env                │
│ schedule_history │ logs/*.log          │ .rollback_marker │ .crash_analysis     │
└──────────────────┴─────────────────────┴──────────────────┴─────────────────────┘
```

## 1. What This Is

Axi is a **self-hosted, self-modifying personal assistant** that lives inside a Discord server. It wraps Claude Code (via Anthropic's Agent SDK) in a multi-agent orchestration layer, giving you persistent AI agent sessions that each get their own Discord channel. You talk to Axi by typing in Discord; Axi talks back by streaming Claude's output into the channel in real time.

The system is three Python files, a systemd service, and a handful of JSON data files. There is no database, no web server, no API layer. Everything is files on disk and Discord API calls.

## 2. Process Hierarchy

```
systemd (axi-bot.service)
  └── supervisor.py         ← Process manager, crash recovery, rollback
       └── bot.py           ← Discord bot + agent orchestration + scheduler
            ├── axi-master  ← Primary session, wakes on demand (axi MCP + optional discord MCP)
            ├── agent-1     ← Spawned Claude session (utils MCP: date/time)
            ├── agent-2     ← ...
            └── ...up to 20 agents (max 5 awake concurrently)
```

**systemd** (`axi-bot.service`) runs `supervisor.py` as a user service with `Restart=on-failure` and `RestartSec=10`. The `ExecStart` is `uv run python supervisor.py`. The service runs under the user's systemd scope (not root).

**supervisor.py** runs `bot.py` in a loop. It classifies exits, performs rollbacks on startup crashes, and writes marker files for bot.py to consume on the next boot.

**bot.py** is the actual application — a single-process asyncio event loop running discord.py, the Claude Agent SDK, and a 10-second scheduler tick.

## 3. Supervisor (`supervisor.py`)

### 3.1 Purpose

The supervisor's only job is to keep bot.py running and to automatically recover from crashes caused by Axi's own code edits (since Axi can modify its own source).

### 3.2 Exit Code Protocol

| Exit Code | Meaning | Supervisor Action |
|-----------|---------|-------------------|
| `42` | Intentional restart (graceful) | Relaunch immediately, reset `rollback_attempted=False` and `runtime_crash_count=0` |
| `0` | Clean shutdown | Stop entirely (exit 0) |
| Negative / `143` (128+15=SIGTERM) | Killed by signal | Stop entirely (exit 0) |
| Any other non-zero | Crash | Classify and handle (see below) |

### 3.3 Crash Classification

The supervisor uses a **60-second threshold** (`CRASH_THRESHOLD = 60`) to distinguish crash types:

**Startup crash** (uptime < 60s): Almost certainly caused by a bad code change. The supervisor:
1. Checks `rollback_attempted` — if already true, stops permanently to prevent infinite loops
2. Captures last 200 lines of `.bot_output.log` via `tail_log()`
3. Records `pre_launch_commit` (saved before each launch via `get_head()`)
4. Checks for uncommitted changes via `has_uncommitted_changes()`
5. Stashes any uncommitted changes via `git stash push --include-untracked`
6. Hard-resets to the pre-launch commit via `git reset --hard` if HEAD moved
7. Writes a `.rollback_performed` JSON marker with: `exit_code`, `uptime_seconds`, `timestamp`, `stash_output`, `rollback_details`, `pre_launch_commit`, `crashed_commit`, `crash_log`
8. Sets `rollback_attempted = True` and relaunches

**Runtime crash** (uptime >= 60s): Could be a transient issue. The supervisor:
1. Increments `runtime_crash_count`
2. After `MAX_RUNTIME_CRASHES = 3` consecutive runtime crashes, stops permanently
3. Writes a `.crash_analysis` JSON marker with: `exit_code`, `uptime_seconds`, `timestamp`, `crash_log`
4. Relaunches — the bot reads the marker and spawns a crash-handler agent
5. A successful exit-42 restart resets `runtime_crash_count` to 0

### 3.4 Helper Functions

- `git(*args)` — Generic git command wrapper using `subprocess.run()`
- `get_head()` — Returns current commit SHA via `git rev-parse HEAD`
- `has_uncommitted_changes()` — Runs `git diff --quiet HEAD` (unstaged) and `git diff --cached --quiet` (staged); returns `True` if either has non-zero exit
- `is_git_repo()` — Runs `git rev-parse --is-inside-work-tree`; works in worktrees where `.git` may be a file
- `tail_log(n=200)` — Reads last `n` lines of `.bot_output.log` for crash context
- `write_crash_marker(code, elapsed, crash_log)` — Writes `.crash_analysis` JSON
- `write_rollback_marker(code, elapsed, stash_output, rollback_details, pre_launch_commit, current_commit, crash_log)` — Writes `.rollback_performed` JSON

### 3.5 Output Tee

`run_bot()` launches bot.py via `uv run python bot.py`, merges stdout+stderr (`stderr=subprocess.STDOUT`) into a single pipe, and tees it to both real stdout and `.bot_output.log` (append mode). A daemon thread handles the streaming.

### 3.6 First-Run Bootstrapping

`ensure_default_files()` creates `USER_PROFILE.md`, `schedules.json`, and `schedule_history.json` with sensible defaults if they don't exist.

## 4. Bot Core (`bot.py`)

### 4.1 Initialization & Configuration

**Logging**: Two-tier logging setup:
- Console handler at INFO level with timestamped format, UTC via `time.gmtime`
- File handler at DEBUG level using `RotatingFileHandler` (10 MB × 3 backups) writing to `logs/orchestrator.log`, UTC timestamps
- Logger name: `__name__` (module-level)

**Environment variables** (loaded via `python-dotenv`):

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `DISCORD_TOKEN` | Yes | — | Bot authentication token. `KeyError` at import if missing. |
| `ALLOWED_USER_IDS` | Yes | — | Comma-separated Discord user IDs that can interact. `KeyError` at import if missing. Parsed via `{int(uid.strip()) for uid in ...split(",")}`. |
| `DEFAULT_CWD` | No | `os.getcwd()` | Master agent's working directory |
| `AXI_USER_DATA` | No | `~/axi-user-data` | Default working directory for spawned agents |
| `SCHEDULE_TIMEZONE` | No | `UTC` | IANA timezone for cron evaluation (via `ZoneInfo`) |
| `DISCORD_GUILD_ID` | Yes | — | Target Discord server ID. `KeyError` at import if missing. Cast to `int`. |
| `DAY_BOUNDARY_HOUR` | No | `0` | Hour (0-23) when a new "logical day" starts for `get_date_and_time` MCP tool. Cast to `int`. |
| `DISABLE_CRASH_HANDLER` | No | `""` | When `1`/`true`/`yes`, skips spawning crash-handler agents |
| `EXTRA_ALLOWED_DIRS` | No | `""` | Comma-separated additional writable directories. Parsed via `[os.path.realpath(d.strip()) for d in ...split(",") if d.strip()]`. Also gates the `"discord"` MCP server (non-empty → attach discord MCP to master). |
| `EXTRA_SYSTEM_PROMPT_FILE` | No | `""` | Path to file whose contents are appended to master's system prompt |

Required env vars (`DISCORD_TOKEN`, `ALLOWED_USER_IDS`, `DISCORD_GUILD_ID`) use `os.environ["KEY"]` — a missing key raises `KeyError` at module load time, crashing immediately (supervisor classifies as startup crash).

**Discord bot setup**: Minimal intents — `guilds`, `guild_messages`, `message_content`, `dm_messages`. Command prefix `"!"` (unused in practice; slash commands are the real interface). The bot is created as `discord.ext.commands.Bot`.

**Constants**:

| Constant | Value | Purpose |
|----------|-------|---------|
| `MASTER_AGENT_NAME` | `"axi-master"` | Reserved name for the primary agent |
| `MAX_AGENTS` | `20` | Hard cap on total agent sessions (awake + sleeping) |
| `MAX_AWAKE_AGENTS` | `5` | Hard cap on concurrently awake agents (each ~280 MB) |
| `IDLE_REMINDER_THRESHOLDS` | `[30min, 3h, 48h]` | Escalating idle notification intervals (cumulative) |
| `QUERY_TIMEOUT` | `43200` (12 hours) | Per-query timeout before interrupt+kill |
| `INTERRUPT_TIMEOUT` | `15` (seconds) | Time to wait after sending interrupt before force-kill |
| `ACTIVE_CATEGORY_NAME` | `"Active"` | Discord category name for live agents |
| `KILLED_CATEGORY_NAME` | `"Killed"` | Discord category name for archived agents |

**Scheduler state**: File paths for `schedules.json`, `schedule_history.json`, `schedule_skips.json`, `.rollback_performed`, `.crash_analysis` are computed relative to `BOT_DIR = os.path.dirname(os.path.abspath(__file__))`. A `schedule_last_fired` dict tracks the last fire time for each recurring event (in memory, reseeds on restart).

### 4.2 The AgentSession Dataclass

The core unit of state for every agent:

| Field | Type | Default | Purpose |
|-------|------|---------|---------|
| `name` | `str` | (required) | Unique identifier (also the Discord channel name) |
| `client` | `ClaudeSDKClient \| None` | `None` | The live Claude process. `None` = sleeping |
| `cwd` | `str` | `""` | Agent's working directory (sandboxed) |
| `query_lock` | `asyncio.Lock` | factory | **One query at a time** per agent |
| `stderr_buffer` | `list[str]` | factory | Captures Claude CLI stderr from SDK callbacks |
| `stderr_lock` | `threading.Lock` | factory | Protects `stderr_buffer` from cross-thread access |
| `last_activity` | `datetime` | `now(UTC)` | Last query completion time (for idle detection) |
| `system_prompt` | `str \| None` | `None` | Custom prompt (only master has one) |
| `last_idle_notified` | `datetime \| None` | `None` | When last idle notification was sent |
| `idle_reminder_count` | `int` | `0` | Number of idle reminders already sent |
| `session_id` | `str \| None` | `None` | Claude session UUID for resume capability |
| `discord_channel_id` | `int \| None` | `None` | Bound Discord channel |
| `message_queue` | `asyncio.Queue` | factory | Messages received while agent is busy or rate-limited |
| `mcp_servers` | `dict \| None` | `None` | MCP servers to attach on wake/reset (preserved across sleep cycles) |
| `_log` | `logging.Logger \| None` | `None` | Per-agent rotating file logger |

**Per-agent logging**: `__post_init__()` creates a dedicated logger (`agent.<name>`) writing to `logs/<agent-name>.log` (5 MB × 2 backups, UTC timestamps). Captures: USER messages, ASSISTANT responses, TOOL_USE calls, SESSION_WAKE/SLEEP events, STREAM events, QUEUED_MSG processing, RATE_LIMIT_EVENT, RESULT (cost/turns/duration). `close_log()` removes all handlers when the session ends. Uses `logger.propagate = False` to avoid duplicate output to the main orchestrator log. Guards against duplicate handlers with `if not logger.handlers`.

### 4.3 Global State

| Variable | Type | Purpose |
|----------|------|---------|
| `agents` | `dict[str, AgentSession]` | All known agent sessions (awake + sleeping) |
| `_wake_lock` | `asyncio.Lock` | Serializes `wake_agent()` calls to prevent TOCTOU races on concurrency limit |
| `_shutdown_requested` | `bool` | Set during graceful shutdown; blocks new messages and scheduler |
| `_rate_limited_until` | `datetime \| None` | Global rate limit expiry (all agents share one API account) |
| `_rate_limit_retry_task` | `asyncio.Task \| None` | Background task draining queues after rate limit expires |
| `target_guild` | `discord.Guild \| None` | Cached guild reference (set in `on_ready()`) |
| `active_category` | `CategoryChannel \| None` | The "Active" Discord category |
| `killed_category` | `CategoryChannel \| None` | The "Killed" Discord category |
| `channel_to_agent` | `dict[int, str]` | Reverse mapping: channel_id → agent_name |
| `_bot_creating_channels` | `set[str]` | Channel names currently being created (prevents race with `on_guild_channel_create`) |
| `_on_ready_fired` | `bool` | Guards against duplicate `on_ready` from gateway reconnects |

### 4.4 Helper Functions

**Stderr handling**:
- `make_stderr_callback(session)` — Returns a closure that appends stderr text to `session.stderr_buffer` under `session.stderr_lock`. Passed to ClaudeAgentOptions as the `stderr` callback. The SDK calls this from its own thread, hence the `threading.Lock`.
- `drain_stderr(session)` → `list[str]` — Thread-safe drain of `stderr_buffer`. Called before each query and during response streaming.

**Streaming adapter**:
- `_as_stream(text)` — Async generator that yields a single user-message dict: `{"type": "user", "session_id": "", "message": {"role": "user", "content": text}, "parent_tool_use_id": None}`. Required because `can_use_tool` callbacks only work when queries are sent as AsyncIterables, not plain strings.

**SDK buffer drain**:
- `drain_sdk_buffer(session)` → `int` — Non-blocking drain of stale messages from the SDK's internal `_message_receive` queue. The SDK shares this queue across query/response cycles; unconsumed post-`ResultMessage` system messages would pollute the next response stream. Uses `anyio.WouldBlock` to detect empty queue. Accesses SDK internals (`session.client._query._message_receive`). Called before every query. Returns number drained; any drained messages are logged as warnings.

**System messaging**:
- `send_system(channel, text)` — Sends `*System:* {text}` via `send_long()`.
- `send_long(channel, text)` — Splits text >2000 chars (Discord limit) at newline boundaries via `split_message()`. On `discord.NotFound` (deleted channel), auto-recreates via `ensure_agent_channel()` and updates the session's `discord_channel_id`.
- `split_message(text, limit=2000)` → `list[str]` — Splits at newline boundaries via `rfind("\n")`; falls back to hard split at `limit` if no newline found. After each split, leading newlines are stripped from the remainder via `.lstrip("\n")`. `send_long()` also calls `.strip()` on the input text before splitting.

### 4.5 Agent Lifecycle — The Four States

```
                ┌──────────┐
                │  (none)  │  Not in agents dict
                └────┬─────┘
                     │ spawn_agent() or on_ready() registration
                     ▼
              ┌──────────────┐
              │   SLEEPING   │  client == None, in agents dict
              └──┬───────┬───┘
    wake_agent() │       │ kill/end_session()
     (on msg or  │       │
      schedule)  │       ▼
                 │    ┌──────────┐
                 │    │  (none)  │  Removed from agents dict
                 │    └──────────┘
                 ▼
          ┌──────────────┐
          │    AWAKE     │  client != None, lock unlocked
          │   (idle)     │
          └──┬───────┬───┘
   query()   │       │  sleep_agent() [auto, pressure-based]
             ▼       ▼
      ┌──────────┐  ┌──────────────┐
      │   BUSY   │  │   SLEEPING   │
      │  (locked)│  └──────────────┘
      └──────────┘
             │
             └─────▶ AWAKE (lock released, idle timer reset)
```

Agents are always **created sleeping** and wake on demand. There is no eager client creation. The master agent itself starts sleeping on boot and wakes when the first message arrives.

### 4.6 Waking (`wake_agent()`)

Creates a `ClaudeSDKClient` with these options:

| Option | Value |
|--------|-------|
| `model` | `"opus"` |
| `effort` | `"high"` |
| `thinking` | `{"type": "enabled", "budget_tokens": 128000}` |
| `betas` | `["context-1m-2025-08-07"]` (1M context window) |
| `setting_sources` | `["user", "project", "local"]` (reads `.claude/` config hierarchy) |
| `permission_mode` | `"default"` with custom `can_use_tool` callback |
| `sandbox` | `{"enabled": True, "autoAllowBashIfSandboxed": True}` |
| `include_partial_messages` | `True` |
| `stderr` | `make_stderr_callback(session)` |
| `resume` | Stored `session_id` (if available) |
| `cwd` | `session.cwd` |
| `system_prompt` | `session.system_prompt` |
| `mcp_servers` | `session.mcp_servers or {}` |

**Concurrency enforcement**: Uses `_wake_lock` to serialize wake calls, preventing TOCTOU races where multiple concurrent wakes could exceed `MAX_AWAKE_AGENTS`. The flow is: acquire lock → double-check `client is not None` → `_ensure_awake_slot()` → create client → release lock.

**`_ensure_awake_slot(requesting_agent)`** → `bool`: Loops while `_count_awake_agents() >= MAX_AWAKE_AGENTS`, calling `_evict_idle_agent()` each iteration. Returns `True` if a slot is available. If no slot can be freed (all are busy), returns `False`.

**`_evict_idle_agent(exclude=None)`** → `bool`: Finds all awake, non-busy, non-excluded agents. Sorts by idle duration (longest first). Sleeps the most idle agent. Returns `True` if evicted, `False` if no candidates.

**`_count_awake_agents()`** → `int`: Counts agents where `client is not None`.

**`ConcurrencyLimitError`**: Custom exception raised when `_ensure_awake_slot()` returns `False`. Caught by callers to queue the message instead of dropping it.

**Resume fallback**: If resume fails (session expired, corrupted, SDK error), the function retries with `resume=None` and sets `session.session_id = None` (context lost). Both the primary and fallback paths assign `session.client` only after `__aenter__()` succeeds.

### 4.7 Sleeping (`sleep_agent()`)

No-op if `client is None`. Calls `_disconnect_client()` to shut down the `ClaudeSDKClient` (5-second timeout) but keeps the `AgentSession` entry in the `agents` dict. The channel stays in Active category. Sleeping agents consume no Claude API resources or RAM. Logs `SESSION_SLEEP` to per-agent log.

### 4.8 Auto-Sleep (Scheduler)

The scheduler loop uses **pressure-based** idle thresholds:
- Under concurrency pressure (`awake_count >= MAX_AWAKE_AGENTS`): sleep idle agents immediately (0-second threshold)
- Normal conditions: sleep agents idle for >1 minute

The master agent is not excluded — it sleeps and wakes on demand just like spawned agents.

### 4.9 Ending (`end_session()`)

Disconnects the client, calls `close_log()` on the per-agent logger, and removes the session from `agents` via `agents.pop()`. Called by kill operations and timeout recovery.

### 4.10 Resetting (`reset_session()`)

Ends the old session via `end_session()` and creates a new **sleeping** session (no client), preserving: `system_prompt`, `cwd` (or overridden), `discord_channel_id`, and `mcp_servers`. Clears `session_id` (forces fresh context). Used by `/reset-context`.

### 4.11 SDK Client Lifecycle & Subprocess Leak Workaround

Three helper functions manage `ClaudeSDKClient` teardown:

**`_get_subprocess_pid(client)`** → `int | None`: Extracts the PID of the underlying CLI subprocess by traversing SDK internals (`_transport._process.pid`). Falls back through multiple attribute paths. Returns `None` on failure.

**`_ensure_process_dead(pid, label)`**: Sends `SIGTERM` to a PID if `os.kill(pid, 0)` shows it's still alive. Workaround for a bug in claude-agent-sdk where `Query.close()`'s anyio cancel-scope leaks a `CancelledError`, preventing `SubprocessCLITransport.close()` from calling `process.terminate()`. See `test_process_leak.py` for a reproducer.

**`_disconnect_client(client, label)`**: The primary client shutdown function:
1. Captures PID via `_get_subprocess_pid()`
2. Calls `client.__aexit__()` with a 5-second `asyncio.wait_for()` timeout
3. Catches `asyncio.TimeoutError` and `asyncio.CancelledError` gracefully
4. Catches `RuntimeError("cancel scope")` from the SDK bug (logged at DEBUG, not raised)
5. Calls `_ensure_process_dead()` as a safety net

### 4.12 Permission & Sandboxing Model

Two independent layers restrict what agents can do:

**Layer 1 — OS-level sandbox**: `sandbox={"enabled": True, "autoAllowBashIfSandboxed": True}`. Claude Code's built-in sandboxing — bash commands run in an isolated environment, and `autoAllowBashIfSandboxed` means bash commands are auto-approved since the sandbox contains blast radius.

**Layer 2 — `make_cwd_permission_callback(allowed_cwd)`**: Returns an async `_check_permission(tool_name, tool_input, ctx)` closure. For file-writing tools (`Edit`, `Write`, `MultiEdit`, `NotebookEdit`), checks that the target path (from `file_path` or `notebook_path`) resolves (via `os.path.realpath()`) to within any of: the agent's `cwd`, `AXI_USER_DATA`, or any path in `EXTRA_ALLOWED_DIRS`. Returns `PermissionResultAllow()` or `PermissionResultDeny(message=...)`. All other tools (reads, bash, web) are allowed everywhere.

**CWD restriction for spawn**: `axi_spawn_agent` validates that the requested cwd is under `AXI_USER_DATA`, `BOT_DIR`, or any `EXTRA_ALLOWED_DIRS` path.

### 4.13 Message Flow — User to Agent

```
User types in #agent-channel
         │
         ▼
on_message()
         │
         ├─ Own message? → ignore
         ├─ Other bot (not in ALLOWED_USER_IDS)? → ignore
         ├─ DM? → not allowed user? ignore : redirect to guild channel
         ├─ Wrong guild? → ignore
         ├─ Not allowed user? → ignore
         ├─ Shutdown in progress? → reject
         ├─ No agent owns channel? → ignore
         ├─ Agent session is None & channel in Killed category? → reject
         ├─ Agent in Killed category? → reject
         │
         ▼
    Agent found for channel
         │
         ├─ Rate limited & agent not busy?
         │    → Queue message, show wait time
         │
         ├─ Agent BUSY (lock held)?
         │    → Queue message with position indicator
         │
         └─ Agent NOT busy:
              │
              ├─ Acquire query_lock
              ├─ Sleeping? → wake_agent()
              │    ├─ ConcurrencyLimitError? → queue, notify
              │    └─ Other error? → suggest kill+respawn
              ├─ Reset idle counters (last_activity, last_idle_notified, idle_reminder_count)
              ├─ drain_stderr() + drain_sdk_buffer()
              ├─ Log USER message to per-agent log
              ├─ asyncio.timeout(QUERY_TIMEOUT)
              │    ├─ client.query(_as_stream(content))
              │    └─ stream_response_to_channel()
              ├─ TimeoutError? → _handle_query_timeout()
              ├─ Other Exception? → notify user, suggest kill+respawn
              ├─ Release lock, _process_message_queue()
              └─ bot.process_commands(message)
```

`bot.process_commands(message)` is called at the end to allow discord.py's prefix command system to process the message. The bot uses `"!"` as its command prefix, though no prefix commands are defined — this is a discord.py convention.

### 4.14 Message Queuing

When a user sends a message while an agent is busy, it is **always queued**. The message goes into the session's `asyncio.Queue` with a position indicator shown to the user. After the current query finishes and the lock releases, `_process_message_queue()` drains the queue one message at a time, waking the agent if needed.

Messages are also queued (rather than sent immediately) when:
- The agent is rate-limited — a `_rate_limit_retry_worker()` drains all queues when the limit expires
- The agent can't be woken due to concurrency limits

**`_process_message_queue(session)`**: Loops while queue is non-empty. Checks for shutdown and rate limit at each iteration (breaks if either). For each message: acquires `query_lock`, wakes agent if sleeping, resets all three idle counters (`last_activity`, `last_idle_notified`, `idle_reminder_count`), drains stderr/SDK buffer, runs query with `QUERY_TIMEOUT`, streams response. On wake failure, clears entire queue with error notifications to each channel. Sends "Processing queued message (N more in queue)" progress messages.

**Note**: The wake failure handler catches generic `Exception`, which includes `ConcurrencyLimitError`. Unlike `on_message()` and `_run_initial_prompt()` which specifically catch `ConcurrencyLimitError` and queue/retry, `_process_message_queue()` drops all remaining queued messages on any wake failure. This means a temporary concurrency limit hit during queue processing causes message loss rather than re-queuing.

### 4.15 Response Streaming

`stream_response_to_channel(session, channel, show_awaiting_input=True)` is the bridge between Claude's streaming output and Discord messages:

1. Wraps everything in `channel.typing()` (shows "Bot is typing..." in Discord)
2. Iterates over `_receive_response_safe()`, which wraps the SDK's raw message stream:
   - Parses each message via `parse_message()` (imported from SDK internals)
   - Catches `MessageParseError`: recognizes `rate_limit_event` (logged at INFO to both orchestrator and per-agent log), other unknown types logged as warnings
   - Yields parsed messages until `ResultMessage`
3. **For each message in the stream:**
   - Drains and sends stderr as code blocks first
   - `StreamEvent` with `content_block_delta` / `text_delta` → appends to `text_buffer`
   - `StreamEvent` with `message_delta` / `stop_reason == "end_turn"` → flush buffer, **stop typing indicator early** (before `ResultMessage` arrives, so typing doesn't linger during SDK bookkeeping)
   - `AssistantMessage` with `error == "rate_limit"` or `"billing_error"` → delegate to `_handle_rate_limit()`, set `hit_rate_limit` flag to suppress further output
   - `AssistantMessage` with other `error` → log warning, flush, stop typing
   - `AssistantMessage` (normal) → flush buffer, stop typing (safety net for responses ending with `tool_use` which have no `end_turn`)
   - `ResultMessage` → extract `session_id` via `_set_session_id()`, flush, done. Logs RESULT with cost/turns/duration/session to per-agent log
   - Buffer exceeds 1800 chars → flush at nearest newline boundary (or hard split at 1800)
   - All stream events and assistant content are logged to the per-agent log file
4. After the stream ends: final stderr drain, flush remaining buffer
5. If `show_awaiting_input=True`: sends "Bot has finished responding and is awaiting input."
6. If `hit_rate_limit`: suppresses the awaiting-input message and final flush

**Typing indicator mechanism**: `async with channel.typing() as _typing_ctx` starts a background task that repeatedly sends the "typing" indicator to Discord. `stop_typing()` cancels this task via `_typing_ctx.task.cancel()` (accessing the private `.task` attribute of the context manager). It's called on `end_turn`, on `AssistantMessage` (safety net), and on `ResultMessage`. The `typing_stopped` flag prevents double-cancellation. Early stop avoids typing indicator lingering during SDK bookkeeping between the last content and the `ResultMessage`.

### 4.16 Rate Limit Handling

A global rate limit system handles Claude API rate limits:

**Detection**: When `stream_response_to_channel()` receives an `AssistantMessage` with `error == "rate_limit"` or `error == "billing_error"`, it calls `_handle_rate_limit()`.

**State**: `_rate_limited_until` (global `datetime | None`) and `_rate_limit_retry_task` (background task reference). All agents share the same API account, so rate limits are global.

**Helper functions**:
- `_parse_rate_limit_seconds(text)` → `int`: Tries patterns: "in X seconds/minutes/hours", "retry after X", "X seconds", "X minutes". Falls back to 300s (5 min).
- `_format_time_remaining(seconds)` → `str`: Human-readable duration ("5m 30s", "1h 20m").
- `_is_rate_limited()` → `bool`: Checks and auto-clears expired limits.
- `_rate_limit_remaining_seconds()` → `int`: Seconds until limit expires.

**`_handle_rate_limit(error_text, session, channel)`**:
1. Parses wait duration from error text
2. Sets/extends `_rate_limited_until` (uses maximum of current and new)
3. Notifies the user (once, not on every subsequent hit — checks `already_limited`)
4. Starts `_rate_limit_retry_worker()` if not already running

**`_rate_limit_retry_worker()`**: Background task that:
1. Polls every 1-30s until rate limit expires (shorter intervals as expiry approaches, picks up extensions)
2. Drains all agent queues one by one
3. If re-rate-limited during processing, loops back and waits again
4. Exits cleanly on `CancelledError`

**Message flow during rate limit**: `on_message()` checks `_is_rate_limited()` before acquiring the query lock. If limited and agent isn't already busy, messages are queued with a "⏳" notification showing estimated wait time.

### 4.17 Query Timeout & Recovery

`_handle_query_timeout(session, channel)` implements a two-phase recovery:

**Phase 1 — Graceful interrupt**:
- Calls `session.client.interrupt()` (sends SIGINT to Claude process)
- Waits `INTERRUPT_TIMEOUT` (15 seconds) for a `ResultMessage` (captures session_id)
- If successful: context preserved, agent still alive, `last_activity` reset

**Phase 2 — Kill and create sleeping session**:
- If interrupt fails or times out (`TimeoutError` or any `Exception`)
- Saves: `session_id`, `name`, `cwd`, `system_prompt`, `discord_channel_id`, `mcp_servers`
- Calls `end_session()` to fully terminate
- Creates a new sleeping `AgentSession` with the old `session_id` preserved (for resume on next wake)
- Notifies the user whether context was preserved ("recovered") or lost ("reset")

## 5. MCP Tools

Three MCP servers providing seven total tools. The master gets the `"axi"` server (agent management + restart) and conditionally the `"discord"` server (cross-channel messaging). All spawned/reconstructed agents get the `"utils"` server (date/time).

### 5.1 The `"axi"` MCP Server (Master Only)

Contains three tools: `axi_spawn_agent`, `axi_kill_agent`, `axi_restart`.

#### `axi_spawn_agent`

Parameters: `name` (string, required), `cwd` (string, optional — defaults to `AXI_USER_DATA/agents/<name>/`), `prompt` (string, required), `resume` (string, optional session ID).

Validation:
- CWD must be under `AXI_USER_DATA`, `BOT_DIR`, or any `EXTRA_ALLOWED_DIRS` path
- Name cannot be empty or `"axi-master"`
- Name must be unique (unless `resume` is set, in which case it calls `reclaim_agent_name()` first)
- Agent count must be under `MAX_AGENTS` (20)

Returns MCP result immediately. The actual spawn runs as `asyncio.create_task(_do_spawn())`. Errors in the background task are caught, logged, and reported to the agent's Discord channel.

#### `axi_kill_agent`

Parameter: `name` (string, required).

Validation: name not empty, not `"axi-master"`, agent exists. The agent is **immediately removed from the `agents` dict** so the name is freed for respawn. The actual sleep + channel archival runs as `asyncio.create_task(_do_kill())`. Returns the session ID for potential future resume.

#### `axi_restart`

No parameters. Triggers `_graceful_shutdown("MCP tool", skip_agent=MASTER_AGENT_NAME)` as a background task. The `skip_agent` parameter prevents the master from deadlocking on itself. Returns immediately with a confirmation message.

### 5.2 The `"utils"` MCP Server (Spawned Agents)

Contains one tool: `get_date_and_time`. Passed to all spawned agents and reconstructed agents. The master does NOT receive this server.

#### `get_date_and_time`

No parameters. Uses the `arrow` library (imported inline). Returns JSON with:
- `now` / `now_display` — Current wall-clock time in `SCHEDULE_TIMEZONE`
- `logical_date` / `logical_date_display` / `logical_day_of_week` — The "logical" date, shifted back by one day if the current hour is before `DAY_BOUNDARY_HOUR`
- `logical_week_start` / `logical_week_display` — Logical week (Sunday-based, via `arrow.weekday()`)
- `timezone` — Active timezone name
- `day_boundary` — Human-readable boundary display (12-hour AM/PM format)

### 5.3 The `"discord"` MCP Server (Master, Conditional)

Contains three tools using `httpx.AsyncClient` for Discord REST API calls (base URL: `https://discord.com/api/v10`, timeout 15s). **Only attached to the master when `EXTRA_ALLOWED_DIRS` is set** — this gates cross-instance communication capability.

#### `discord_list_channels`

Parameter: `guild_id` (string, required). Fetches `/guilds/{guild_id}/channels`. Filters to type 0 (GUILD_TEXT). Returns JSON array of `{id, name, category}` where category is resolved from type-4 channels.

#### `discord_read_messages`

Parameters: `channel_id` (string, required), `limit` (integer, optional, default 20, max 100). Fetches `/channels/{channel_id}/messages`, reverses for chronological order. Returns formatted text: `[timestamp] author: content`.

#### `discord_send_message`

Parameters: `channel_id` (string, required), `content` (string, required). POSTs to `/channels/{channel_id}/messages`. Returns `"Message sent (id: {id})"`.

**`_discord_request(method, path, **kwargs)`**: Shared helper. Retries up to 3 times on 429 (rate limit) with `retry_after` sleep. Raises on other errors.

## 6. Discord Guild Infrastructure

### 6.1 Category System

Axi organizes agent channels into two Discord categories:

- **Active** — Channels for live agents (awake or sleeping)
- **Killed** — Archived channels for terminated agents (chat history preserved)

`ensure_guild_infrastructure()` finds or creates both categories and syncs permissions on every startup. Permission comparison uses `_overwrites_match()` which compares by target ID to avoid key-type mismatches between `discord.Role`/`discord.Object`.

### 6.2 Permission Model

`_build_category_overwrites(guild)` returns permission overwrites:
- `@everyone` — Can view and read history, but **cannot send messages or react**
- Bot — Full permissions (send, manage channels/messages, view, read history)
- Each allowed user (from `ALLOWED_USER_IDS`) — Can send, react, view, read history

This means: anyone in the server can watch agent conversations, but only allowed users can interact.

### 6.3 Channel Lifecycle

- **Creation**: `ensure_agent_channel(name)` — normalizes name, searches Active category first, then Killed (moves back to Active via `ch.move(category=active_category, beginning=True, sync_permissions=True)` — positions at top of Active), then creates new. Tracks in-progress creations via `_bot_creating_channels` set.
- **Archival**: `move_channel_to_killed(name)` — moves from Active to Killed via `ch.move(category=killed_category, end=True, sync_permissions=True)` — positions at bottom of Killed. Never archives the master channel. Handles `discord.HTTPException` gracefully.
- **Lookup**: `get_agent_channel(name)` — checks `discord_channel_id` first, falls back to channel name search in Active category. `get_master_channel()` is a convenience wrapper.
- **Naming**: `_normalize_channel_name(name)` — lowercases, replaces spaces with hyphens, strips non-alphanumeric (regex `[^a-z0-9\-_]`), truncates to 100 chars.
- **Metadata**: Channel topics store `cwd: /path | session: uuid` parsed by `_parse_channel_topic()`. `_set_session_id()` updates the topic when a new session_id is received from `ResultMessage`, but **skips master** to avoid topic churn (master gets new session every restart). Uses `_format_channel_topic()` to build the topic string. **Note**: `_set_session_id()` unconditionally assigns `session.session_id = sid` in the else branch, which means a `ResultMessage` with no session_id (`sid=None`) will overwrite any previously stored session_id with `None`.
- **Auto-registration**: `on_guild_channel_create()` — when a user manually creates a channel in the Active category, automatically registers a sleeping agent for it with default cwd under `AXI_USER_DATA/agents/<name>/`, attaches `"utils"` MCP, creates the cwd directory, sets channel topic. Guards: text channel only, Active category only, not in `_bot_creating_channels`, not master, not already registered, under `MAX_AGENTS`.

### 6.4 Agent Reconstruction on Restart

`reconstruct_agents_from_channels()` scans the **Active category only** on startup. For each channel:
1. Master channel → register in `channel_to_agent` only (skip agent creation)
2. Already-known agent → register mapping only
3. Has valid topic with `cwd` → create sleeping `AgentSession` (no client) with `"utils"` MCP, register both `agents` and `channel_to_agent`
4. No valid topic → skip

This means agents survive restarts — their Discord channels persist, and when a user messages the channel, the agent wakes with tools intact and resumes from its stored session_id. Returns the count of reconstructed agents.

## 7. Scheduling System

### 7.1 Scheduler Loop

`check_schedules()` runs as a `discord.ext.tasks.loop(seconds=10)`. Uses `@check_schedules.before_loop` to wait for bot ready. It performs **six operations** per tick:

1. **Prune history** (`prune_history()`) — Remove history entries >7 days old
2. **Prune skips** (`prune_skips()`) — Remove skip entries with past dates
3. **Process schedule entries** — Fire recurring (cron) and one-off (at) events
4. **Idle agent detection** — Escalating notifications for non-master, non-sleeping, non-busy agents
5. **Stranded message safety net** — Wake sleeping agents that have queued messages (race condition fix)
6. **Auto-sleep** — Pressure-based sleep of idle agents

All operations are skipped when `_shutdown_requested` is true.

### 7.2 Schedule Entry Format

`schedules.json` is a flat JSON array. Each entry:

```json
{
  "name": "identifier",
  "prompt": "instructions",
  "schedule": "0 9 * * *",
  "at": "2026-02-21T03:00:00Z",
  "cwd": "/path",
  "session": "agent-name"
}
```

| Field | Required | Purpose |
|-------|----------|---------|
| `name` | Yes | Unique identifier |
| `prompt` | Yes | Message/instructions sent to agent |
| `schedule` | One of schedule/at | Cron expression (recurring) — evaluated in `SCHEDULE_TIMEZONE` |
| `at` | One of schedule/at | ISO datetime with timezone (one-off) |
| `cwd` | No | Working directory (default: `AXI_USER_DATA/agents/<agent_name>/`) |
| `session` | No | Agent session name. If omitted, uses event `name`. Multiple events with same `session` share one agent |

**Note on `agent` and `reset_context` fields**: The system prompt documents these as valid fields, and Axi may write them into `schedules.json`. However, the scheduler does **not** read or act on either field — all scheduled events spawn/route agents unconditionally, and `reset_context` is never checked. These fields are effectively inert. This is a known spec-vs-implementation divergence.

### 7.3 Recurring Event Firing Logic

1. Validate cron expression via `croniter.is_valid()`
2. Compute `last_occurrence` = most recent cron match relative to `now_local` (in `SCHEDULE_TIMEZONE`)
3. First encounter: seed `schedule_last_fired[name]` to `last_occurrence` (prevents firing on startup for events that should have fired earlier)
4. Fire if `last_occurrence > schedule_last_fired[name]`
5. Check one-off skip via `check_skip()` — if the event's name matches a skip entry for today's date, skip
6. Determine agent name: `entry.get("session", name)`
7. If agent exists → `send_prompt_to_agent()` (routes to existing session)
8. If max agents reached → skip and notify master channel
9. Otherwise → `reclaim_agent_name()` (silently kills any existing agent with that name, sends "Recycled" message) then `spawn_agent()`

### 7.4 One-Off Event Firing Logic

1. Parse `fire_at` from ISO string via `datetime.fromisoformat()`
2. Fire if `fire_at <= now_utc`
3. Route or spawn (same as recurring)
4. Remove from `schedules.json`, append to `schedule_history.json` with `fired_at` timestamp

### 7.5 Schedule Skips

`schedule_skips.json` allows one-off cancellations of recurring events. Each entry: `{"name": "event-name", "skip_date": "YYYY-MM-DD"}`. `check_skip(name)` checks if today (in `SCHEDULE_TIMEZONE`) matches, auto-removes the entry if matched. `prune_skips()` removes entries for past dates.

### 7.6 Session Routing

Every scheduled event resolves an agent name via `entry.get("session", name)`. The routing:
- Agent exists → `send_prompt_to_agent()` (creates a background `_run_initial_prompt` task)
- Agent doesn't exist → `reclaim_agent_name()` then `spawn_agent()`

Multiple events sharing the same `"session"` value all route to one persistent agent. An event named `"daily-report"` with no session field always uses/creates agent `daily-report`.

### 7.7 Idle Detection

For each non-master, non-sleeping, non-busy, non-killed agent (with `idle_reminder_count < len(IDLE_REMINDER_THRESHOLDS)`):

- Compute cumulative threshold: `sum(IDLE_REMINDER_THRESHOLDS[:count + 1])`
  - Reminder 0: after 30 minutes idle
  - Reminder 1: after 3.5 hours cumulative (30m + 3h)
  - Reminder 2: after 51.5 hours cumulative (30m + 3h + 48h)
- Send notification to agent's channel AND master channel
- Increment `idle_reminder_count`, set `last_idle_notified`

This is a passive notification system — it doesn't kill agents, just reminds the user.

### 7.8 Stranded Message Safety Net

Catches messages stranded by the race between queue-empty check and sleep. Only runs when `_count_awake_agents() < MAX_AWAKE_AGENTS` (avoids re-queuing loops). For each non-master sleeping agent with non-empty queue and unlocked lock: dequeues one message and creates a `_run_initial_prompt` task. Processes **one at a time** (breaks after first) to respect concurrency limits.

## 8. Agent Spawning

### 8.1 `spawn_agent(name, cwd, initial_prompt, resume=None)`

1. Auto-creates `cwd` directory if it doesn't exist
2. Finds or creates Discord channel via `ensure_agent_channel()`
3. Sends "Spawning agent..." or "Resuming agent..." system message
4. Creates sleeping `AgentSession` with `"utils"` MCP server, registers in `agents` and `channel_to_agent`
5. Sets channel topic with cwd and optional resume session ID
6. If `initial_prompt` is empty: sends "ready (sleeping)" message and returns
7. Otherwise: creates `asyncio.create_task(_run_initial_prompt(session, prompt, channel))`

### 8.2 `_run_initial_prompt(session, prompt, channel)`

Background task that:
1. Acquires `query_lock`
2. Wakes agent if sleeping (queues if `ConcurrencyLimitError`)
3. Sets `last_activity` (note: does NOT reset `last_idle_notified` or `idle_reminder_count` — unlike `on_message()` and `_process_message_queue()`)
4. Drains stderr/SDK buffer
5. Logs prompt to per-agent log (prefix `PROMPT:`, not `USER:`)
6. Runs query with `QUERY_TIMEOUT`, streams response with `show_awaiting_input=False`
7. Handles timeout via `_handle_query_timeout()`
8. Sends "finished initial task" message (unless timed out)
9. Processes message queue (outside `query_lock` scope)
10. **Sleeps agent after completing** — applies to ALL `_run_initial_prompt` invocations (spawned agents AND scheduler-routed prompts via `send_prompt_to_agent()`). This means a scheduled event firing on an active agent's session will sleep it after the prompt completes.

### 8.3 `send_prompt_to_agent(agent_name, prompt)`

Used by the scheduler to route prompts to existing agents. Validates agent and channel exist. Creates `asyncio.create_task(_run_initial_prompt(session, prompt, channel))`. **Note**: since `_run_initial_prompt` auto-sleeps the agent after completion, this will sleep any active agent receiving a scheduler-routed prompt.

### 8.4 `reclaim_agent_name(name)`

If agent exists: sleeps it, removes from `agents` dict, sends "Recycled previous **{name}** session for new scheduled run." to the agent's Discord channel.

## 9. Graceful Shutdown

### 9.1 Trigger

`/restart` slash command, the `axi_restart` MCP tool, or `_graceful_shutdown()` called programmatically.

### 9.2 Flow

`_graceful_shutdown(source, skip_agent=None)`:

1. Guard against duplicate calls (`_shutdown_requested` flag)
2. Set `_shutdown_requested = True`
3. Identify busy agents (locked `query_lock`, excluding `skip_agent`)
4. If none busy → `_sleep_all_agents()`, `bot.close()`, `_kill_supervisor()` immediately
5. Notify each busy agent's channel
6. Poll every 5 seconds, send status update every 30 seconds
7. Hard timeout at `QUERY_TIMEOUT` (12 hours) → force sleep all, `_kill_supervisor()`

**`_kill_supervisor()`**: Sends SIGTERM to parent process (supervisor) via `os.kill(os.getppid(), signal.SIGTERM)`. Sleeps 1 second (blocking `time.sleep()`) for supervisor to die. **Always ends with `os._exit(42)` — this function never returns.** Uses `os._exit()` (not `sys.exit()`) to skip Python cleanup, ensuring immediate termination. Since `_graceful_shutdown()` calls this, it also never returns normally.

**`_sleep_all_agents()`**: Iterates all agents, sleeps any with `client is not None`. Catches exceptions per-agent.

### 9.3 Force Restart

`/restart force=True` skips the wait entirely — `_sleep_all_agents()`, `bot.close()`, `_kill_supervisor()` immediately.

## 10. Startup Sequence

`on_ready()` performs these steps:

1. Install global async exception handler (`_handle_task_exception`)
2. Guard against duplicate `on_ready` (Discord gateway reconnects) via `_on_ready_fired`
3. Register master agent as **sleeping** (`client=None`) with `"axi"` MCP server (and `"discord"` MCP if `EXTRA_ALLOWED_DIRS` is set)
4. Set up guild infrastructure: find/create Active and Killed categories, sync permissions
5. Create/find master channel, bind to session, set topic to "Axi master control channel"
6. Reconstruct sleeping agents from existing Active category channels
7. Sync slash commands (`bot.tree.sync()`)
8. Start scheduler loop (`check_schedules.start()`)
9. Check for rollback marker → parse JSON, delete file, format notification
10. Check for crash analysis marker → parse JSON, delete file, format notification
11. Send startup notification to master channel: rollback details, crash details, or simple "Axi restarted"
12. If `DISABLE_CRASH_HANDLER` is set, skip crash handler spawn
13. Otherwise, if either marker exists: `reclaim_agent_name("crash-handler")` then `spawn_agent("crash-handler", BOT_DIR, crash_prompt)` with detailed analysis instructions

### 10.1 Crash Handler Agent

When either marker exists (and `DISABLE_CRASH_HANDLER` is not set), a `crash-handler` agent is spawned with a prompt containing:
- Exit code, uptime, timestamp
- Rollback details (what was stashed/reverted, commit hashes)
- Last 200 lines of crash log
- Instructions to analyze root cause and produce a fix plan (but NOT apply fixes)

### 10.2 Global Exception Handler

`_handle_task_exception(loop, context)`: Catches unhandled exceptions in fire-and-forget `asyncio.Task` instances. Suppresses expected `ProcessError` from SIGTERM'd subprocesses (checks `type(exception).__name__ == "ProcessError"` and `"-15"` in the string). All other exceptions logged as errors.

### 10.3 Main Entry Point

```python
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN, log_handler=None)
```

`log_handler=None` prevents discord.py from overriding the custom logging config. Wraps in try/except to log unhandled crashes.

## 11. Slash Commands

| Command | Description |
|---------|-------------|
| `/list-agents` | Lists all agents with status (busy/awake/sleeping), killed tag, protected tag (master), channel link, cwd, idle time, session ID. Shows awake count vs `MAX_AWAKE_AGENTS` in header. |
| `/kill-agent [name]` | Sleeps agent, moves channel to Killed, shows session ID for resume. Infers agent from current channel if not specified. Cannot kill master. Removes from `agents` dict, calls `sleep_agent()`, then `move_channel_to_killed()`. |
| `/stop [agent_name]` | Sends `client.interrupt()` to a busy agent (like Ctrl+C). Infers agent from channel if not specified. Checks agent is actually busy (lock held + client not None). |
| `/reset-context [name] [working_dir]` | Calls `reset_session()` — wipes conversation history (creates sleeping session), optionally changes working directory. |
| `/compact [agent_name]` | Sends `/compact` to Claude via `_run_agent_sdk_command()` to compress conversation context. Wakes agent if sleeping. |
| `/clear [agent_name]` | Sends `/clear` to Claude via `_run_agent_sdk_command()` to wipe conversation context. Wakes agent if sleeping. |
| `/restart [force]` | Graceful (waits for busy agents) or force restart. Force: immediate `_sleep_all_agents()` + `_kill_supervisor()`. |

All commands check `ALLOWED_USER_IDS` and return ephemeral errors for unauthorized users. Agent name parameters have autocomplete callbacks (`killable_agent_autocomplete` excludes master, `agent_autocomplete` includes all).

**`_run_agent_sdk_command(interaction, agent_name, command, label)`**: Shared helper that acquires the query lock, wakes the agent if needed, sends a CLI slash command as a query via `_as_stream(command)`, and streams the response to the agent's channel (falls back to `interaction.channel` if agent's channel can't be resolved) with `show_awaiting_input=False`.

## 12. System Prompt

The master agent's system prompt (~140 lines) defines Axi's identity and capabilities:

**Identity**: Personal assistant in Discord. Autonomous system, not just an LLM behind a bot. Each agent has its own channel.

**Key instructions**:
- Read `USER_PROFILE.md` at conversation start for personalization
- Edit `schedules.json` directly for scheduling (no API — Claude reads/writes the file)
- Cron times are in `SCHEDULE_TIMEZONE`, not UTC. DST handled automatically.
- Use `axi_spawn_agent` / `axi_kill_agent` MCP tools for agent management
- Use `axi_restart` MCP tool for restarts (not the slash command)
- Use `discord_query.py` via bash for message history lookups
- Send progress updates every 30-60 seconds (Discord has no streaming indicator from agents)
- Never use `AskUserQuestion`, `TodoWrite`, `EnterPlanMode`, `ExitPlanMode`, `Skill`, or `EnterWorktree` — these are Claude Code UI features invisible in Discord
- Never guess or fabricate answers — look things up or ask

**Interpolated values**: `%(axi_user_data)s` and `%(bot_dir)s` are substituted via `%` formatting at module load time.

**Extra system prompt**: If `EXTRA_SYSTEM_PROMPT_FILE` is set and the file exists, its contents are appended with `"\n\n"` separator. Used for instance-specific instructions (e.g. Nova management).

**Spec divergence**: The system prompt documents `agent` and `reset_context` as valid schedule fields, but the scheduler does not implement them (see Section 7.2).

## 13. Discord Query Tool (`discord_query.py`, 449 lines)

A standalone CLI that the master agent runs via bash to query Discord's REST API. Independent of bot.py — loads `.env` directly and creates its own synchronous `httpx.Client` (base URL: `https://discord.com/api/v10`, timeout 30s).

### 13.1 Subcommands

**`guilds`**: Lists servers the bot is in. Output: JSONL with `id` and `name`.

**`channels <guild_id>`**: Lists text channels. Output: JSONL with `id`, `name`, `type`, `category`, `position`. Validates bot guild membership first.

**`history <channel>`**: Fetches message history with pagination.
- `--limit` (default 50, max 500 via `MAX_LIMIT`)
- `--before` / `--after` (ISO datetime or snowflake ID, converted via `resolve_snowflake()`)
- `--format text|jsonl` (default jsonl)
- Channel can be ID or `guild_id:channel_name` (resolved via `resolve_channel()`)
- Pagination: `before` (newest-first) or `after` (oldest-first)

**`search <guild_id> <query>`**: Client-side substring search.
- Scans up to `--max-scan` (default 500, via `DEFAULT_MAX_SCAN`) messages per channel
- Case-insensitive (`query.lower()` matched against `content.lower()`)
- Optional `--channel` and `--author` filters
- Not a full-text index — scans recent history sequentially

### 13.2 API Handling

- `api_get(client, path, params)`: Rate limit handling (429 → sleep `retry_after`), 5xx retry with exponential backoff (`2**attempt`), 4xx → `sys.exit(1)`.
- `resolve_channel(client, channel_spec)`: Accepts raw ID or `guild_id:channel_name` syntax.
- `datetime_to_snowflake(dt)` / `resolve_snowflake(value)`: Converts between ISO datetimes and Discord snowflake IDs using Discord's epoch (2015-01-01, `1420070400000`).

## 14. Utility Scripts

### 14.1 `wait_for_message.py` (222 lines)

Polls the Discord API and returns as soon as new messages appear in a channel. Designed for fast cross-bot communication (e.g., Prime ↔ Nova).

**Usage**: `wait_for_message.py <channel_id> [--after ID] [--timeout 120] [--ignore-author-id ID] [--include-system] [--poll-interval 2.0] [--no-cursor]`

**Output**: Matching messages as JSONL (`{id, ts, author, author_id, content}`), followed by a cursor line (`{cursor: ID}`). Use cursor as `--after` on next call for gapless chaining.

**Key behavior**: Auto-detects latest message as baseline if `--after` not specified. Filters out system messages (`*System:*` prefix) by default. Advances baseline when all fetched messages are filtered (avoids re-fetching same filtered messages). Uses synchronous `httpx.Client`, exits with code 2 on timeout.

### 14.2 `nova_test.py` (141 lines)

Send a message to Nova's Discord server and wait for a response. Fast feedback loop for testing Nova.

**Usage**: `nova_test.py "message" [--channel ID] [--timeout 120] [--poll-after ID]`

**Key behavior**: Sends message via Discord REST API, then polls for response. Detects completion via "Bot has finished responding" sentinel. Prints messages as they arrive with elapsed time. Falls back to returning whatever messages exist on timeout.

### 14.3 `test_process_leak.py` (212 lines)

Reproducer for a ClaudeSDKClient subprocess leak bug in claude-agent-sdk. Documents the bug where `Query.close()`'s anyio cancel-scope leaks `CancelledError` when multiple clients share an event loop, preventing `SubprocessCLITransport.close()` from calling `process.terminate()`. Not part of the runtime system — used for bug reporting and verification.

## 15. Data Files & Persistence

| File | Managed By | Persistence | Purpose |
|------|-----------|-------------|---------|
| `schedules.json` | Axi (master) + scheduler | Across restarts | Active schedule entries |
| `schedule_history.json` | Scheduler (`append_history()`) | 7-day rolling (`prune_history()`) | Fired one-off events log |
| `schedule_skips.json` | Axi (master) + scheduler | Until date passes (`prune_skips()`) | One-off recurring event skips |
| `USER_PROFILE.md` | Axi (master) | Indefinite | User preferences and context |
| `logs/orchestrator.log` | bot.py (main logger) | Rolling 10 MB × 3 | Main orchestrator debug log |
| `logs/<agent-name>.log` | bot.py (per-agent logger) | Rolling 5 MB × 2 | Per-agent activity log |
| `.rollback_performed` | supervisor → bot.py | Single use (deleted after read) | Startup crash rollback context |
| `.crash_analysis` | supervisor → bot.py | Single use (deleted after read) | Runtime crash context |
| `.bot_output.log` | supervisor | Append mode (grows across runs) | Bot stdout/stderr for crash analysis |

**JSON load** functions use graceful error recovery — `load_schedules()`, `load_history()`, `load_skips()` return `[]` on `FileNotFoundError` or `json.JSONDecodeError`. **JSON save** functions do not have error recovery — a write failure propagates as an exception. All JSON writes include `indent=2` and a trailing newline.

## 16. Concurrency Model

**asyncio.Lock per agent** (`query_lock`): Ensures exactly one query runs per agent at any time. This is the primary concurrency control. The lock is checked (not acquired) via `.locked()` to detect busy state for queuing decisions.

**asyncio.Lock global** (`_wake_lock`): Serializes `wake_agent()` calls to prevent TOCTOU races where multiple concurrent wakes could exceed `MAX_AWAKE_AGENTS`. The pattern is: acquire lock → double-check client state → check count → evict if needed → create client → release lock.

**threading.Lock per agent** (`stderr_lock`): The Claude SDK calls stderr callbacks from its own thread. The threading lock protects the `stderr_buffer` list from concurrent access between the SDK thread and the asyncio event loop.

**asyncio.Queue per agent** (`message_queue`): Unbounded queue for messages received while agent is busy or rate-limited. Drained serially after each query completes via `_process_message_queue()`, and by `_rate_limit_retry_worker()` when rate limits expire.

**Concurrency limit** (`MAX_AWAKE_AGENTS = 5`): Each awake agent (with a live `ClaudeSDKClient`) consumes ~280 MB of RAM. The `_ensure_awake_slot()` / `_evict_idle_agent()` system enforces this limit by sleeping the most-idle non-busy agent when the cap is reached. If all 5 slots are busy, a `ConcurrencyLimitError` is raised and the message is queued.

**asyncio.create_task()**: Used for background operations — initial prompts, MCP tool spawns/kills, stranded message wakeups, graceful shutdown. A global exception handler (`_handle_task_exception`) catches unhandled exceptions, with suppression of expected `ProcessError` from SIGTERM'd subprocesses.

**tasks.loop(seconds=10)**: The scheduler tick. Single-threaded within the event loop — no concurrent scheduler ticks. `@before_loop` waits for bot ready.

**Global `_shutdown_requested` flag**: Checked by `on_message()`, `check_schedules()`, and `_process_message_queue()` to reject new work during shutdown.

**Global rate limit state** (`_rate_limited_until`): Checked by `on_message()` and `_process_message_queue()` to queue messages instead of sending them to the API.

## 17. Self-Modification Capability

Axi can modify its own source code. The master agent's `cwd` is set by `DEFAULT_CWD`, which defaults to `os.getcwd()` — in practice the bot directory, since `supervisor.py` does `os.chdir(DIR)` before launching. The `can_use_tool` callback allows writes within `cwd`, `AXI_USER_DATA`, and any `EXTRA_ALLOWED_DIRS` paths — which includes `bot.py`, `schedules.json`, `USER_PROFILE.md`, etc.

The safety net for self-modification is the supervisor's rollback mechanism. If a code change crashes the bot on startup (within 60 seconds), the supervisor reverts the change and spawns a crash-handler agent to analyze what went wrong.

The system prompt instructs Axi to only restart when explicitly asked — not after every self-edit.

## 18. DM Handling

`on_message()` DM handling: DMs from allowed users receive a redirect message pointing to the master channel (`<#{channel_id}>`). DMs from non-allowed users are silently ignored. The check is `message.channel.type == ChannelType.private`.

## 19. Error Handling Philosophy

The codebase follows a consistent pattern: **log the error, notify the user in Discord, keep running**.

- All async operations are wrapped in try/except
- `discord.NotFound` on channel operations triggers channel recreation via `ensure_agent_channel()`
- SDK `MessageParseError` is caught and skipped with warning log; `rate_limit_event` type is recognized and logged at INFO
- Failed agent wakes fall through to user notification with recovery suggestion ("Try `/kill-agent` and respawn")
- `ConcurrencyLimitError` is caught and the message is queued instead of dropped
- Rate limit errors are caught, global state is set, and messages are queued for automatic retry
- The global exception handler catches unhandled exceptions in fire-and-forget tasks
- `on_ready()` guards against duplicate firing from gateway reconnects
- SDK subprocess leak workaround ensures orphaned CLI processes are SIGTERM'd even when the SDK's own cleanup fails
- `_process_message_queue()` clears remaining queue on wake failure, notifying each channel (but catches generic `Exception` — see Section 4.14 note on `ConcurrencyLimitError` handling)

## 20. Dependencies

From `pyproject.toml`:

| Package | Version Constraint | Purpose |
|---------|-------------------|---------|
| `discord.py` | Latest | Discord bot framework (gateway + REST) |
| `claude-agent-sdk` | Latest | Anthropic's Claude Code programmatic SDK |
| `python-dotenv` | Latest | `.env` file loading |
| `croniter` | Latest | Cron expression parsing and evaluation |
| `httpx` | Latest | HTTP client for `discord_query.py`, `wait_for_message.py`, `nova_test.py`, and Discord MCP tools |
| `tzdata` | `>=2025.3` | IANA timezone database (for `ZoneInfo` DST handling) |
| `arrow` | Latest | Date/time math for `get_date_and_time` MCP tool (imported inline) |

**Transitive dependency**: `anyio` (via `claude-agent-sdk`) — used directly in `drain_sdk_buffer()` for `anyio.WouldBlock` exception handling when draining the SDK's internal message queue.

Runtime: Python `>=3.12`. Package manager: `uv`.

## 21. What's Not Here

- **No database** — All state is JSON files and Discord channels
- **No web server / API** — Discord is the only interface
- **No tests** — `test_process_leak.py` is a bug reproducer, not a test suite
- **No authentication layer beyond Discord** — `ALLOWED_USER_IDS` is the auth system
- **No multi-guild support** — Hardcoded to `DISCORD_GUILD_ID`
- **No encryption at rest** — `.env` has plaintext tokens, JSON files are plaintext
- **No agent-to-agent communication** — Agents are isolated; only the master can spawn/kill (though the `"discord"` MCP tools allow cross-channel messaging when enabled)
- **No persistent task queue** — `message_queue` is in-memory and lost on restart
- **No inert schedule fields implemented** — `agent` and `reset_context` exist in the system prompt spec but are not read by the scheduler
