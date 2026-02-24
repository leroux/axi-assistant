# Axi Assistant — Complete Architecture & Design Report

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
│  bot.py  (single asyncio process)                                               │
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
│  supervisor.py                                                                  │
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

**systemd** (`axi-bot.service`) runs `supervisor.py` as a user service. If the supervisor itself dies with a non-zero exit, systemd restarts it after 10 seconds.

**supervisor.py** runs `bot.py` in a loop. It classifies exits, performs rollbacks on startup crashes, and writes marker files for bot.py to consume on the next boot.

**bot.py** is the actual application — a single-process asyncio event loop running discord.py, the Claude Agent SDK, and a 10-second scheduler tick.

## 3. Supervisor (`supervisor.py`, 231 lines)

### 3.1 Purpose

The supervisor's only job is to keep bot.py running and to automatically recover from crashes caused by Axi's own code edits (since Axi can modify its own source).

### 3.2 Exit Code Protocol

| Exit Code | Meaning | Supervisor Action |
|-----------|---------|-------------------|
| `42` | Intentional restart (graceful) | Relaunch immediately, reset all crash counters (`supervisor.py:139-143`) |
| `0` | Clean shutdown | Stop entirely (`supervisor.py:145-147`) |
| Negative / `143` | Killed by signal (SIGTERM etc.) | Stop entirely (`supervisor.py:150-152`) |
| Any other non-zero | Crash | Classify and handle (see below) |

### 3.3 Crash Classification

The supervisor uses a **60-second threshold** (`CRASH_THRESHOLD`, `supervisor.py:14`) to distinguish crash types:

**Startup crash** (uptime < 60s, `supervisor.py:176-226`): Almost certainly caused by a bad code change. The supervisor:
1. Stashes any uncommitted changes via `git stash push --include-untracked` (`supervisor.py:200-206`)
2. Hard-resets to the pre-launch commit via `git reset --hard` (`supervisor.py:209-218`)
3. Writes a `.rollback_performed` JSON marker with full crash context (`supervisor.py:220-223`)
4. Relaunches — the bot reads the marker on startup and spawns a crash-handler agent
5. Sets `rollback_attempted = True` — if the rollback code _also_ crashes, the supervisor stops dead (`supervisor.py:181-183`)

**Runtime crash** (uptime >= 60s, `supervisor.py:157-174`): Could be a transient issue. The supervisor:
1. Increments `runtime_crash_count`
2. Writes a `.crash_analysis` JSON marker (`supervisor.py:170`)
3. Relaunches — the bot reads the marker and spawns a crash-handler agent
4. After 3 consecutive runtime crashes (`MAX_RUNTIME_CRASHES`), stops permanently (`supervisor.py:165-167`)
5. A successful exit-42 restart resets the counter (`supervisor.py:141-142`)

### 3.4 Output Tee

`run_bot()` (`supervisor.py:59-82`) launches `bot.py` via `uv run python bot.py`, merges stdout+stderr into a single pipe, and tees it to both the real stdout and `.bot_output.log`. A daemon thread handles the streaming. `tail_log()` (`supervisor.py:85-91`) reads the last 200 lines of this log for crash markers.

### 3.5 First-Run Bootstrapping

`ensure_default_files()` (`supervisor.py:23-31`) creates `USER_PROFILE.md`, `schedules.json`, and `schedule_history.json` with sensible defaults if they don't exist.

## 4. Bot Core (`bot.py`, ~2878 lines)

### 4.1 Initialization & Configuration

**Logging** (`bot.py:34-58`): Two-tier logging setup:
- Console handler at INFO level with timestamped format
- File handler at DEBUG level using `RotatingFileHandler` (10 MB × 3 backups) writing to `logs/orchestrator.log`
- All timestamps in UTC via `time.gmtime` converter

**Environment loading** (`bot.py:60-69`): Ten env vars control the system:
- `DISCORD_TOKEN` — Bot auth (required)
- `ALLOWED_USER_IDS` — Comma-separated Discord user IDs that can interact (required)
- `DEFAULT_CWD` — Master agent's working directory (default: cwd)
- `AXI_USER_DATA` — Default working directory for spawned agents (default: `~/axi-user-data`)
- `SCHEDULE_TIMEZONE` — IANA timezone for cron evaluation (default: UTC)
- `DISCORD_GUILD_ID` — Target server (required)
- `DAY_BOUNDARY_HOUR` — Hour (0-23) when a new "logical day" starts for the `get_date_and_time` MCP tool (default: 0, i.e. midnight). If set to e.g. `4`, then 3 AM is still "yesterday" for planning purposes (`bot.py:66`)
- `DISABLE_CRASH_HANDLER` — When `1`/`true`/`yes`, skips spawning crash-handler agents after crashes (`bot.py:67`)
- `EXTRA_ALLOWED_DIRS` — Comma-separated list of additional directories agents can write to, beyond their own cwd. Also gates the `"discord"` MCP server for the master (`bot.py:68`)
- `EXTRA_SYSTEM_PROMPT_FILE` — Path to a file whose contents are appended to the master's system prompt. Used for instance-specific instructions (e.g. Nova's management instructions) (`bot.py:69`)

**Discord bot setup** (`bot.py:73-79`): Minimal intents — guilds, guild messages, message content, DMs. Command prefix `!` (unused in practice; slash commands are the real interface).

**Constants** (`bot.py:93-98`):
- `MAX_AGENTS = 20` — Hard cap on total agent sessions (awake + sleeping)
- `MAX_AWAKE_AGENTS = 5` — Hard cap on concurrently awake agents (each ~280 MB); set based on available RAM
- `IDLE_REMINDER_THRESHOLDS = [30min, 3h, 48h]` — Escalating idle notifications (cumulative)
- `QUERY_TIMEOUT = 43200` — 12 hours per query before timeout handling
- `INTERRUPT_TIMEOUT = 15` — Seconds to wait after sending interrupt before force-kill

### 4.2 The AgentSession Dataclass

Defined at `bot.py:104-147`. This is the core unit of state for every agent:

| Field | Type | Purpose |
|-------|------|---------|
| `name` | `str` | Unique identifier (also the Discord channel name) |
| `client` | `ClaudeSDKClient \| None` | The live Claude process. `None` = sleeping |
| `cwd` | `str` | Agent's working directory (sandboxed) |
| `query_lock` | `asyncio.Lock` | **One query at a time** per agent |
| `stderr_buffer` / `stderr_lock` | `list[str]` / `threading.Lock` | Captures Claude CLI stderr (tool use output, warnings) |
| `last_activity` | `datetime` | Last query completion time (for idle detection) |
| `system_prompt` | `str \| None` | Custom prompt (only master has one) |
| `last_idle_notified` / `idle_reminder_count` | `datetime \| None` / `int` | Tracks escalating idle notifications |
| `session_id` | `str \| None` | Claude session UUID for resume capability |
| `discord_channel_id` | `int \| None` | Bound Discord channel |
| `mcp_servers` | `dict \| None` | MCP servers to attach on wake/reset (preserved across sleep cycles) |
| `message_queue` | `asyncio.Queue` | Messages received while agent is busy or rate-limited |
| `_log` | `logging.Logger \| None` | Per-agent rotating file logger (see below) |

**Per-agent logging** (`bot.py:122-146`): Each `AgentSession` creates its own logger in `__post_init__()`, writing to `logs/<agent-name>.log` (5 MB × 2 backups). This captures USER messages, ASSISTANT responses, TOOL_USE calls, SESSION_WAKE/SLEEP events, STREAM events, and QUEUED_MSG processing — all at DEBUG level. `close_log()` cleans up handlers when the session ends.

**Global state** (`bot.py:149-164`):
- `agents` dict (name → session)
- `_wake_lock` — `asyncio.Lock` that serializes `wake_agent()` calls to prevent TOCTOU races on the concurrency limit
- `_shutdown_requested` flag
- `_rate_limited_until` / `_rate_limit_retry_task` — Global rate limit state (all agents share the same API account)
- Guild/category references (`target_guild`, `active_category`, `killed_category`)
- `channel_to_agent` reverse mapping (channel_id → agent_name)
- `_bot_creating_channels` — Set of channel names the bot is currently creating (prevents `on_guild_channel_create` from racing with `spawn_agent`)

### 4.3 Agent Lifecycle — The Four States

An agent exists in one of four states:

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

Key difference from earlier design: agents are now always **created sleeping** and wake on demand. There is no `start_session()` that eagerly creates a client. The master agent itself starts sleeping on boot and wakes when the first message arrives.

**Waking** (`bot.py:1005-1088`): `wake_agent()` creates a `ClaudeSDKClient` with these options:
- Model: `opus` (hardcoded)
- Effort: `high`
- Thinking: `{"type": "enabled", "budget_tokens": 128000}` (extended thinking with explicit token budget)
- Beta: `context-1m-2025-08-07` (1M context window)
- Permission mode: `default` with a custom `can_use_tool` callback
- Sandbox: enabled, with auto-allow for bash commands
- Streaming: `include_partial_messages=True`
- Setting sources: `["user", "project", "local"]` (reads `.claude/` config hierarchy)
- Resume: stored `session_id` (if available)

The function uses `_wake_lock` (`bot.py:1017`) to serialize wake calls, preventing TOCTOU races where multiple concurrent wakes could exceed `MAX_AWAKE_AGENTS`. Before creating the client, `_ensure_awake_slot()` (`bot.py:969-981`) checks the concurrency limit and evicts the most-idle non-busy agent if needed. If all slots are busy (locked), a `ConcurrencyLimitError` is raised and the message is queued instead.

If resume fails (session expired, corrupted), the function falls back to creating a fresh session with context loss (`bot.py:1062-1087`).

**Sleeping** (`bot.py:989-1002`): Calls `_disconnect_client()` to shut down the `ClaudeSDKClient` (5-second timeout) but keeps the `AgentSession` entry in the `agents` dict. The channel stays in Active category. This is a resource optimization — sleeping agents consume no Claude API resources or RAM.

**Auto-sleep** (`bot.py:2292-2312`): The scheduler loop uses **pressure-based** idle thresholds:
- Under concurrency pressure (`awake_count >= MAX_AWAKE_AGENTS`): sleep idle agents immediately (0-second threshold)
- Normal conditions: sleep agents idle for >1 minute

The master agent is not excluded — it sleeps and wakes on demand just like spawned agents.

**Ending** (`bot.py:893-903`): `end_session()` disconnects the client, calls `close_log()` on the per-agent logger, and removes the session from `agents`. Called by kill operations.

**Resetting** (`bot.py:906-928`): `reset_session()` ends the old session and creates a new **sleeping** session (no client), preserving system prompt, channel mapping, and MCP servers. Used by `/reset-context`. The agent wakes on its next message.

### 4.4 SDK Client Lifecycle & Subprocess Leak Workaround

Three helper functions manage `ClaudeSDKClient` teardown (`bot.py:835-891`):

**`_get_subprocess_pid()`** (`bot.py:835-851`): Extracts the PID of the underlying CLI subprocess by traversing SDK internals (`_transport._process.pid`).

**`_ensure_process_dead()`** (`bot.py:854-872`): Sends SIGTERM to a PID if it's still alive. This works around a bug in the claude-agent-sdk where `Query.close()`'s anyio cancel-scope leaks a `CancelledError`, preventing `SubprocessCLITransport.close()` from calling `process.terminate()`.

**`_disconnect_client()`** (`bot.py:875-891`): The primary client shutdown function. Calls `client.__aexit__()` with a 5-second timeout, catches `RuntimeError("cancel scope")` from the SDK bug gracefully, then calls `_ensure_process_dead()` as a safety net.

### 4.5 Permission & Sandboxing Model

Two independent layers restrict what agents can do:

**Layer 1 — OS-level sandbox** (`bot.py:1051`): `sandbox={"enabled": True, "autoAllowBashIfSandboxed": True}`. This is Claude Code's built-in sandboxing — bash commands run in an isolated environment, and the `autoAllowBashIfSandboxed` flag means bash commands are auto-approved (no manual permission needed) since the sandbox contains blast radius.

**Layer 2 — `can_use_tool` callback** (`bot.py:234-255`): A closure over `allowed_cwd` that intercepts all tool calls. For file-writing tools (`Edit`, `Write`, `MultiEdit`, `NotebookEdit`), it checks that the target path resolves to within any of: the agent's `cwd`, `AXI_USER_DATA`, or any path in `EXTRA_ALLOWED_DIRS`. All other tools (reads, bash, web) are allowed everywhere. This prevents agents from writing to arbitrary filesystem locations.

**CWD restriction for spawn** (`bot.py:551-553`): The `axi_spawn_agent` MCP tool validates that the requested cwd is under `AXI_USER_DATA`, `BOT_DIR`, or any directory in `EXTRA_ALLOWED_DIRS`. You can't spawn an agent pointed at arbitrary filesystem paths.

### 4.6 Message Flow — User to Agent

```
User types in #agent-channel
         │
         ▼
on_message() [bot.py:2009]
         │
         ├─ Own message? → ignore [2010]
         ├─ Other bot (not in ALLOWED_USER_IDS)? → ignore [2012-2013]
         ├─ DM? → redirect to guild channel [2016-2026]
         ├─ Wrong guild? → ignore [2029-2030]
         ├─ Not allowed user? → ignore [2032-2033]
         ├─ Shutdown in progress? → reject [2037-2039]
         ├─ No agent owns channel? → ignore [2042-2044]
         ├─ Agent in Killed category? → reject [2054-2057]
         │
         ▼
    Agent found for channel
         │
         ├─ Rate limited & agent not busy?
         │    → Queue message, show wait time [2062-2072]
         │
         ├─ Agent BUSY (lock held)?
         │    → Queue message with position indicator [2074-2083]
         │
         └─ Agent NOT busy:
              │
              ├─ Acquire query_lock [2085]
              ├─ Sleeping? → wake_agent() [2087-2108]
              │    ├─ ConcurrencyLimitError? → queue, notify [2091-2101]
              │    └─ Other error? → suggest kill+respawn [2102-2108]
              ├─ Reset idle counters [2110-2112]
              ├─ drain_stderr() + drain_sdk_buffer() [2113-2114]
              ├─ Log to per-agent log [2115-2116]
              ├─ asyncio.timeout(43200s) [2119]
              │    ├─ client.query(_as_stream(content)) [2120]
              │    └─ stream_response_to_channel() [2121]
              ├─ TimeoutError? → _handle_query_timeout() [2122-2123]
              ├─ Release lock, _process_message_queue() [2135]
              └─ bot.process_commands(message) [2137]
```

### 4.7 Message Queuing

When a user sends a message while an agent is busy, it is **always queued** (`bot.py:2074-2083`). The message goes into the session's `asyncio.Queue` with a position indicator shown to the user. After the current query finishes and the lock releases, `_process_message_queue()` (`bot.py:1865-1915`) drains the queue one message at a time, waking the agent if needed.

Messages are also queued (rather than sent immediately) when:
- The agent is rate-limited (`bot.py:2062-2072`) — a `_rate_limit_retry_worker()` drains all queues when the limit expires
- The agent can't be woken due to concurrency limits (`bot.py:2091-2101`)

**`bot.process_commands(message)`** (`bot.py:2137`): Called at the end of `on_message()` to allow discord.py's prefix command system to process the message. The bot uses `!` as its command prefix (`bot.py:79`), though no prefix commands are defined — this is a discord.py convention to ensure commands aren't silently swallowed.

### 4.8 Response Streaming

`stream_response_to_channel()` (`bot.py:1513-1681`) is the bridge between Claude's streaming output and Discord messages:

1. Wraps everything in `channel.typing()` (shows "Bot is typing..." in Discord)
2. Iterates over `_receive_response_safe()` (`bot.py:1490-1510`), which wraps the SDK's raw message stream, parsing each message and catching `MessageParseError` for unknown types
3. `StreamEvent` with `content_block_delta` / `text_delta` → appends to `text_buffer`
4. `StreamEvent` with `message_delta` / `stop_reason == "end_turn"` → flush buffer, **stop typing indicator early** (before `ResultMessage` arrives, so the typing indicator doesn't linger during SDK bookkeeping)
5. `AssistantMessage` → flush buffer to Discord, stop typing. If `msg.error` is `"rate_limit"` or `"billing_error"` → delegate to `_handle_rate_limit()` and set `hit_rate_limit` flag to suppress further output
6. Buffer exceeds 1800 chars → flush at nearest newline boundary (`bot.py:1657-1663`)
7. `ResultMessage` → extract session_id via `_set_session_id()`, flush, done
8. Stderr is drained and sent as code blocks between text chunks
9. All stream events and assistant content are logged to the per-agent log file
10. After the stream ends, sends "Bot has finished responding and is awaiting input." (unless suppressed by `show_awaiting_input=False`)

`send_long()` (`bot.py:1322-1339`) uses `split_message()` (`bot.py:1305-1319`) to split any message >2000 chars (Discord's limit) at newline boundaries. If a channel has been deleted, it auto-recreates it via `ensure_agent_channel()`.

### 4.9 Rate Limit Handling

A global rate limit system (`bot.py:1347-1486`) handles Claude API rate limits:

**Detection** (`bot.py:1604-1614`): When `stream_response_to_channel()` receives an `AssistantMessage` with `error == "rate_limit"` or `error == "billing_error"`, it calls `_handle_rate_limit()`.

**State** (`bot.py:155-157`): `_rate_limited_until` (global `datetime | None`) and `_rate_limit_retry_task` (background task reference). All agents share the same API account, so rate limits are global.

**`_handle_rate_limit()`** (`bot.py:1419-1445`):
1. Parses wait duration from error text via `_parse_rate_limit_seconds()` — tries multiple patterns ("in X seconds/minutes", "retry after X", etc.), falls back to 5 minutes
2. Sets/extends `_rate_limited_until`
3. Notifies the user (once, not on every subsequent hit)
4. Starts `_rate_limit_retry_worker()` if not already running

**`_rate_limit_retry_worker()`** (`bot.py:1448-1485`): Background task that:
1. Sleeps until rate limit expires (polling every 30s to pick up extensions)
2. Drains all agent queues one by one
3. If re-rate-limited during processing, loops back and waits again

**Message flow during rate limit**: `on_message()` checks `_is_rate_limited()` before acquiring the query lock (`bot.py:2062`). If limited, messages are queued with a "⏳" notification showing estimated wait time. The retry worker processes them when the limit expires.

### 4.10 Query Timeout & Recovery

`_handle_query_timeout()` (`bot.py:1683-1728`) implements a two-phase recovery:

**Phase 1 — Graceful interrupt** (`bot.py:1688-1701`):
- Calls `session.client.interrupt()` (sends SIGINT to Claude process)
- Waits 15 seconds for a `ResultMessage` (captures session_id)
- If successful: context preserved, agent still alive

**Phase 2 — Kill and create sleeping session** (`bot.py:1706-1728`):
- If interrupt fails or times out
- Calls `end_session()` to fully terminate
- Creates a new sleeping `AgentSession` with the old `session_id` preserved (for resume on next wake)
- Notifies the user whether context was preserved or lost

### 4.11 SDK Buffer Drain

`drain_sdk_buffer()` (`bot.py:193-231`) is a defensive measure against a specific SDK behavior. The Claude SDK uses an internal message queue (`_message_receive`) that's shared across query/response cycles. If a previous response left unconsumed messages (post-`ResultMessage` system messages), they'd pollute the next query's response stream. This function does a non-blocking drain of stale messages before each new query, using `anyio.WouldBlock` to detect an empty queue. It accesses SDK internals (`session.client._query._message_receive`). Drained messages are logged as warnings.

## 5. MCP Tools

There are three MCP servers providing seven total tools. The master gets the `"axi"` server (agent management + restart) and conditionally the `"discord"` server (cross-channel messaging). All spawned/reconstructed agents get the `"utils"` server (date/time).

### 5.1 The `"axi"` MCP Server (Master Only)

Defined at `bot.py:706-710`. Contains three tools, passed to the master session (`bot.py:2653`).

#### `axi_spawn_agent` (`bot.py:529-580`)

Parameters: `name` (required), `cwd` (optional, defaults to `AXI_USER_DATA/agents/<name>/`), `prompt` (required), `resume` (optional session ID).

Validation (`bot.py:551-562`):
- CWD must be under `AXI_USER_DATA`, `BOT_DIR`, or any `EXTRA_ALLOWED_DIRS` path
- Name cannot be empty or `axi-master`
- Name must be unique (unless `resume` is set)
- Agent count must be under `MAX_AGENTS` (20)

The actual spawn runs as a background `asyncio.create_task()` (`bot.py:579`) — the MCP tool returns immediately while the agent boots.

#### `axi_kill_agent` (`bot.py:583-633`)

Parameter: `name` (required).

Validation: name not empty, not `axi-master`, agent exists. The agent is **immediately removed from the `agents` dict** (`bot.py:609`) so the name is freed for respawn. The actual sleep + channel archival runs as a background task (`bot.py:629`). Returns the session ID for potential future resume.

#### `axi_restart` (`bot.py:694-703`)

No parameters. Triggers a graceful shutdown via `_graceful_shutdown("MCP tool", skip_agent=MASTER_AGENT_NAME)` as a background task. The `skip_agent` parameter prevents the master from deadlocking on itself (waiting for its own query to finish). Returns immediately with a confirmation message. The system prompt instructs Axi to only use this when the user explicitly asks (`bot.py:408-409`).

### 5.2 The `"utils"` MCP Server (Spawned Agents)

Defined at `bot.py:688-692`. Contains one tool, passed to all spawned agents (`bot.py:1769`) and reconstructed agents (`bot.py:1220`). The master does NOT receive this server.

#### `get_date_and_time` (`bot.py:637-685`)

No parameters. Returns a JSON object with:
- `now` / `now_display` — Current wall-clock time in `SCHEDULE_TIMEZONE`
- `logical_date` / `logical_date_display` / `logical_day_of_week` — The "logical" date, shifted back by one day if the current hour is before `DAY_BOUNDARY_HOUR` (e.g., 3 AM with a 4 AM boundary is still "yesterday")
- `logical_week_start` / `logical_week_display` — Logical week (Sunday-based)
- `timezone` — Active timezone name
- `day_boundary` — Human-readable boundary display (e.g. "4:00 AM")

Uses the `arrow` library (imported inline at `bot.py:644`) for date math. The logical day concept supports users who stay up past midnight — their "today" doesn't flip until the configured boundary hour.

### 5.3 The `"discord"` MCP Server (Master, Conditional)

Defined at `bot.py:825-829`. Contains three tools using `httpx.AsyncClient` for Discord REST API calls. **Only attached to the master when `EXTRA_ALLOWED_DIRS` is set** (`bot.py:2654-2655`) — this gates cross-instance communication capability.

#### `discord_list_channels` (`bot.py:739-768`)

Parameter: `guild_id` (required). Returns JSON array of text channels with `id`, `name`, and `category`.

#### `discord_read_messages` (`bot.py:771-799`)

Parameters: `channel_id` (required), `limit` (optional, default 20, max 100). Returns chronological message history formatted as `[timestamp] author: content`.

#### `discord_send_message` (`bot.py:802-823`)

Parameters: `channel_id` (required), `content` (required). Sends a message to any accessible Discord channel. Returns the message ID.

All three tools use `_discord_request()` (`bot.py:724-736`) which handles rate limit retries (429 → sleep `retry_after`, up to 3 attempts).

## 6. Discord Guild Infrastructure

### 6.1 Category System

Axi organizes agent channels into two Discord categories:

- **Active** — Channels for live agents (awake or sleeping)
- **Killed** — Archived channels for terminated agents (chat history preserved)

`ensure_guild_infrastructure()` (`bot.py:1134-1182`) finds or creates both categories and syncs permissions on every startup. Permission comparison uses `_overwrites_match()` (`bot.py:1154-1161`) which compares by target ID to avoid key-type mismatches.

### 6.2 Permission Model

`_build_category_overwrites()` (`bot.py:1107-1131`):
- `@everyone` — Can view and read history, but **cannot send messages or react**
- Bot — Full permissions (send, manage channels/messages, view, read history)
- Each allowed user — Can send, react, view, read history

This means: anyone in the server can watch agent conversations, but only allowed users can interact.

### 6.3 Channel Lifecycle

- **Creation**: `ensure_agent_channel()` (`bot.py:1234-1262`) — searches Active, then Killed (moves back to Active if found), then creates new. Tracks in-progress creations via `_bot_creating_channels` set to prevent races with `on_guild_channel_create`.
- **Archival**: `move_channel_to_killed()` (`bot.py:1265-1279`) — moves from Active to Killed category. Never archives the master channel.
- **Naming**: `_normalize_channel_name()` (`bot.py:1098-1104`) — lowercases, replaces spaces with hyphens, strips non-alphanumeric, truncates to 100 chars
- **Metadata**: Channel topics store `cwd: /path | session: uuid` parsed by `_parse_channel_topic()` (`bot.py:349-361`). `_set_session_id()` (`bot.py:364-379`) updates the topic when a new session_id is received (skips master to avoid topic churn on every restart).
- **Auto-registration**: `on_guild_channel_create()` (`bot.py:2592-2632`) — when a user manually creates a channel in the Active category, automatically registers a sleeping agent for it with default cwd.

### 6.4 Agent Reconstruction on Restart

`reconstruct_agents_from_channels()` (`bot.py:1185-1231`) scans the **Active category only** on startup. For each channel with a valid topic (containing `cwd`), it creates a sleeping `AgentSession` (no client), attaches the `"utils"` MCP server (`bot.py:1220`), and registers the channel mapping. Skips the master channel and already-known agents. This means agents survive restarts — their Discord channels persist, and when a user messages the channel, the agent wakes up with its tools intact and resumes from its stored session_id.

## 7. Scheduling System

### 7.1 Scheduler Loop

`check_schedules()` (`bot.py:2142-2313`) runs as a `discord.ext.tasks.loop` every 10 seconds. It does five things per tick:

1. **Prune data** — Remove history entries >7 days old, remove expired skip entries
2. **Process schedule entries** — Fire recurring (cron) and one-off (at) events
3. **Idle agent detection** — Escalating notifications
4. **Stranded message safety net** — Wake sleeping agents that have queued messages (only if an awake slot is available)
5. **Auto-sleep** — Pressure-based sleep of idle agents

### 7.2 Schedule Entry Format

`schedules.json` is a flat JSON array. Each entry:

```json
{
  "name": "identifier",
  "prompt": "instructions",
  "schedule": "0 9 * * *",
  "at": "2026-02-21T03:00:00Z",
  "cwd": "/path",
  "session": "name"
}
```

- `name` (required): Unique identifier
- `prompt` (required): Message/instructions sent to agent
- `schedule`: Cron expression (recurring) — evaluated in SCHEDULE_TIMEZONE
- `at`: ISO datetime with timezone (one-off)
- `cwd`: Working directory for the spawned agent (default: `AXI_USER_DATA/agents/<agent_name>/`)
- `session`: Agent session name to use. If omitted, defaults to the event `name`. Multiple events sharing the same `session` value route to one persistent agent

**Note on `agent` and `reset_context` fields**: The system prompt documents these as valid fields (`bot.py:399-401`), and Axi may write them into `schedules.json`. However, the scheduler loop (`bot.py:2167-2225`) does not actually read or act on either field — all scheduled events spawn agents unconditionally (they never route through the master), and `reset_context` is never checked. These fields are effectively inert in the current implementation.

### 7.3 Recurring Event Firing Logic

`bot.py:2167-2200`:

1. Validate cron expression via `croniter.is_valid()`
2. Compute `last_occurrence` = most recent cron match relative to `now_local` (in `SCHEDULE_TIMEZONE`)
3. First encounter: seed `schedule_last_fired[name]` to `last_occurrence` (prevents firing on startup for events that should have fired earlier)
4. Fire if `last_occurrence > schedule_last_fired[name]`
5. Check one-off skip via `check_skip()` — if the event's name matches a skip entry for today's date, skip this firing
6. Determine agent name: `entry.get("session", name)` — uses the `session` field if set, otherwise falls back to the event's `name` (`bot.py:2187`)
7. If an agent with that name already exists → send prompt to it via `send_prompt_to_agent()` (`bot.py:2192-2193`)
8. If max agents reached → skip and notify master channel (`bot.py:2194-2197`)
9. Otherwise → `reclaim_agent_name()` (silently kills any existing agent with that name, `bot.py:1734-1744`) then spawn new agent (`bot.py:2199-2200`)

### 7.4 One-Off Event Firing Logic

`bot.py:2202-2225`:

1. Parse `fire_at` from ISO string
2. Fire if `fire_at <= now_utc`
3. Route or spawn (same as recurring)
4. Remove from `schedules.json`, append to `schedule_history.json` with timestamp

### 7.5 Schedule Skips

`schedule_skips.json` allows one-off cancellations of recurring events. Each entry has `name` (matching the event) and `skip_date` (YYYY-MM-DD in `SCHEDULE_TIMEZONE`). `check_skip()` (`bot.py:327-336`) checks and auto-removes matching entries. `prune_skips()` (`bot.py:318-324`) removes entries for past dates.

### 7.6 Session Routing

Every scheduled event resolves an agent name via `entry.get("session", name)` (`bot.py:2187, 2208`). If no `"session"` field, the event's own `name` is used as the agent name. The routing then works the same way for both recurring and one-off events:
- If an agent with that name already exists → send prompt to it via `send_prompt_to_agent()` (`bot.py:1788-1805`)
- Otherwise → `reclaim_agent_name()` (if needed) then `spawn_agent()`

This means multiple schedule entries sharing the same `"session"` value all route to one persistent agent. It also means an event named `"daily-report"` with no `session` field will always try to use/create an agent named `daily-report`.

### 7.7 Idle Detection

`bot.py:2233-2275`: For each non-master, non-sleeping, non-busy, non-killed agent:

- Compute cumulative threshold: `sum(IDLE_REMINDER_THRESHOLDS[:count + 1])`
  - Reminder 0: after 30 minutes idle
  - Reminder 1: after 3.5 hours cumulative (30m + 3h)
  - Reminder 2: after 51.5 hours cumulative (30m + 3h + 48h)
- Send notification to agent's channel AND master channel
- Increment `idle_reminder_count`

This is a passive notification system — it doesn't kill agents, just reminds the user to.

## 8. Graceful Shutdown

### 8.1 Trigger

`/restart` slash command (`bot.py:2571-2587`), the `axi_restart` MCP tool (`bot.py:700-703`), or `_graceful_shutdown()` called programmatically.

### 8.2 Flow

`_graceful_shutdown()` (`bot.py:1946-2003`):

1. Set `_shutdown_requested = True` (blocks new messages and scheduler)
2. Accept optional `skip_agent` parameter — excludes that agent from the busy-wait (used when master triggers its own restart to avoid deadlocking on itself)
3. Identify busy agents (locked `query_lock`, excluding `skip_agent`)
4. If none busy → sleep all agents, close bot, `_kill_supervisor()` immediately
5. Notify each busy agent's channel
6. Poll every 5 seconds, send status update every 30 seconds
7. Hard timeout at `QUERY_TIMEOUT` (12 hours) → force sleep all, `_kill_supervisor()`

**`_kill_supervisor()`** (`bot.py:1921-1933`): Sends SIGTERM to the parent process (supervisor) so systemd restarts the whole service cleanly. Falls back to `os._exit(42)` after a 1-second delay if the supervisor survives. This is preferred over `os._exit(42)` alone because it ensures the supervisor stops cleanly rather than seeing the bot as a crash.

`_sleep_all_agents()` (`bot.py:1936-1943`) shuts down all SDK clients, preserving session IDs in memory (and in channel topics for reconstruction).

### 8.3 Force Restart

`/restart force=True` (`bot.py:2578-2583`) skips the wait entirely — sleeps all agents, closes the bot, and calls `_kill_supervisor()` immediately.

## 9. Startup Sequence

`on_ready()` (`bot.py:2639-2857`):

1. Install global async exception handler (`bot.py:2645`)
2. Guard against duplicate `on_ready` (Discord gateway reconnects) (`bot.py:2647-2650`)
3. Register master agent as **sleeping** (`client=None`) with `"axi"` MCP server (and `"discord"` MCP if `EXTRA_ALLOWED_DIRS` is set) (`bot.py:2652-2664`)
4. Set up guild infrastructure (categories + permissions) (`bot.py:2667-2683`)
5. Create/find master channel, bind to session, set topic to "Axi master control channel" (`bot.py:2669-2680`)
6. Reconstruct sleeping agents from existing channels (`bot.py:2686-2689`)
7. Sync slash commands (`bot.py:2691`)
8. Start scheduler loop (`bot.py:2694`)
9. Check for rollback marker → parse, delete, notify master channel (`bot.py:2698-2756`)
10. Check for crash analysis marker → same pattern (`bot.py:2714-2765`)
11. If neither marker: send simple "Axi restarted" notification (`bot.py:2767`)
12. If `DISABLE_CRASH_HANDLER` is set, skip crash handler spawn (`bot.py:2771-2772`)
13. Otherwise, spawn `crash-handler` agent with crash analysis prompt (`bot.py:2773-2856`)

### 9.1 Crash Handler Agent

When either marker exists (and `DISABLE_CRASH_HANDLER` is not set), a `crash-handler` agent is spawned (`bot.py:2773-2856`) with a detailed prompt containing:
- Exit code, uptime, timestamp
- Rollback details (what was stashed/reverted, commit hashes)
- Last 200 lines of crash log
- Instructions to analyze root cause and produce a fix plan (but NOT apply fixes)

The crash handler is a standard spawned agent — it gets its own Discord channel where it writes its analysis.

## 10. Slash Commands

| Command | Handler | Description |
|---------|---------|-------------|
| `/list-agents` | `bot.py:2340-2378` | Lists all agents with status (busy/awake/sleeping), killed tag, channel link, cwd, idle time, session ID. Shows awake count vs `MAX_AWAKE_AGENTS` in header. |
| `/kill-agent [name]` | `bot.py:2381-2437` | Sleeps agent, moves channel to Killed, shows session ID for resume. Infers agent from current channel if not specified. |
| `/stop [agent_name]` | `bot.py:2440-2472` | Sends interrupt signal to a busy agent (like Ctrl+C). Infers agent from channel if not specified. |
| `/reset-context [name] [cwd]` | `bot.py:2475-2502` | Wipes conversation history (creates sleeping session), optionally changes working directory. |
| `/compact [agent_name]` | `bot.py:2557-2561` | Sends `/compact` to Claude to compress conversation context. Wakes agent if sleeping. |
| `/clear [agent_name]` | `bot.py:2564-2568` | Sends `/clear` to Claude to wipe conversation context. Wakes agent if sleeping. |
| `/restart [force]` | `bot.py:2571-2587` | Graceful or force restart. |

All commands check `ALLOWED_USER_IDS` and return ephemeral errors for unauthorized users. Agent name parameters have autocomplete callbacks (`bot.py:2322-2337`).

`/compact` and `/clear` both use `_run_agent_sdk_command()` (`bot.py:2505-2554`), a shared helper that acquires the query lock, wakes the agent if needed, sends a CLI slash command as a query, and streams the response.

## 11. System Prompt

The master agent's system prompt (`bot.py:382-520`, ~140 lines) defines Axi's identity and capabilities:

**Identity**: Personal assistant in Discord. Autonomous system, not just an LLM behind a bot. Each agent has its own channel.

**Key instructions**:
- Read `USER_PROFILE.md` at conversation start for personalization
- Edit `schedules.json` directly for scheduling (no API — Claude reads/writes the file)
- Cron times are in `SCHEDULE_TIMEZONE`, not UTC
- Use `axi_spawn_agent` / `axi_kill_agent` MCP tools for agent management
- Use `axi_restart` MCP tool for restarts (not the slash command) (`bot.py:408`)
- Use `discord_query.py` via bash for message history lookups
- Send progress updates every 30-60 seconds (Discord has no typing indicator for bots in the same way)
- Never use `AskUserQuestion`, `TodoWrite`, `EnterPlanMode`, `ExitPlanMode`, `Skill`, or `EnterWorktree` — these are Claude Code UI features invisible in Discord (`bot.py:501-519`)
- Never guess or fabricate answers — look things up or ask

**Interpolated values**: `%(axi_user_data)s` and `%(bot_dir)s` are substituted at runtime (`bot.py:520`).

**Extra system prompt**: If `EXTRA_SYSTEM_PROMPT_FILE` is set and the file exists, its contents are appended to the system prompt (`bot.py:522-524`). Used for instance-specific instructions (e.g. Nova management).

**Note on prompt-vs-reality divergence**: The system prompt documents `agent` and `reset_context` as valid schedule fields (`bot.py:399-401`), but the scheduler does not implement them (see Section 7.2).

## 12. Discord Query Tool (`discord_query.py`, 449 lines)

A standalone CLI that the master agent runs via bash to query Discord's REST API. It's independent of bot.py — it loads `.env` directly and creates its own synchronous `httpx.Client`.

### 12.1 Subcommands

**`guilds`**: Lists servers the bot is in. Output: JSONL with `id` and `name`.

**`channels <guild_id>`** (`discord_query.py:227-253`): Lists text channels. Output: JSONL with `id`, `name`, `type`, `category`, `position`. Validates bot guild membership first.

**`history <channel>`** (`discord_query.py:256-301`): Fetches message history with pagination.
- `--limit` (default 50, max 500)
- `--before` / `--after` (ISO datetime or snowflake ID)
- `--format text|jsonl`
- Channel can be ID or `guild_id:channel_name`
- Pagination cursor: `before` (newest-first) or `after` (oldest-first)

**`search <guild_id> <query>`** (`discord_query.py:303-368`): Client-side substring search.
- Scans up to `--max-scan` (default 500) messages per channel
- Case-insensitive
- Optional `--channel` and `--author` filters
- Not a full-text index — scans recent history sequentially

### 12.2 API Handling

`api_get()` (`discord_query.py:68-98`): Rate limit handling (429 → sleep `retry_after`), 5xx retry with exponential backoff, 4xx → exit.

`resolve_channel()` (`discord_query.py:124-172`): Accepts `guild_id:channel_name` syntax, resolves by fetching the guild's channel list and matching by name.

`datetime_to_snowflake()` / `resolve_snowflake()` (`discord_query.py:104-121`): Converts between ISO datetimes and Discord snowflake IDs using Discord's epoch (2015-01-01).

## 13. Data Files & Persistence

| File | Managed By | Persistence | Purpose |
|------|-----------|-------------|---------|
| `schedules.json` | Axi (master) + scheduler | Across restarts | Active schedule entries |
| `schedule_history.json` | Scheduler | 7-day rolling | Fired one-off events log |
| `schedule_skips.json` | Axi (master) + scheduler | Until date passes | One-off recurring event skips |
| `USER_PROFILE.md` | Axi (master) | Indefinite | User preferences |
| `logs/orchestrator.log` | bot.py (main logger) | Rolling 10 MB × 3 | Main orchestrator debug log |
| `logs/<agent-name>.log` | bot.py (per-agent logger) | Rolling 5 MB × 2 | Per-agent activity log |
| `.rollback_performed` | supervisor → bot.py | Single use (deleted after read) | Startup crash rollback context |
| `.crash_analysis` | supervisor → bot.py | Single use (deleted after read) | Runtime crash context |
| `.bot_output.log` | supervisor | Overwritten each run (append mode) | Bot stdout/stderr for crash analysis |

JSON **load** functions use graceful error recovery — `load_schedules()` etc. return `[]` on `FileNotFoundError` or `json.JSONDecodeError` (`bot.py:260-265`). JSON **save** functions do not have error recovery — a write failure propagates as an exception.

## 14. Concurrency Model

**asyncio.Lock per agent** (`query_lock`): Ensures exactly one query runs per agent at any time. This is the primary concurrency control. The lock is checked (not acquired) to detect busy state for queuing decisions.

**asyncio.Lock global** (`_wake_lock`): Serializes `wake_agent()` calls to prevent TOCTOU races where multiple concurrent wakes could exceed `MAX_AWAKE_AGENTS`. The pattern is: acquire lock → check count → evict if needed → create client → release lock.

**threading.Lock per agent** (`stderr_lock`): The Claude SDK calls stderr callbacks from its own thread. The threading lock protects the `stderr_buffer` list from concurrent access between the SDK thread and the asyncio event loop.

**asyncio.Queue per agent** (`message_queue`): Unbounded queue for messages received while agent is busy or rate-limited. Drained serially after each query completes, and by the `_rate_limit_retry_worker()` when rate limits expire.

**Concurrency limit** (`MAX_AWAKE_AGENTS = 5`): Each awake agent (with a live `ClaudeSDKClient`) consumes ~280 MB of RAM. The `_ensure_awake_slot()` / `_evict_idle_agent()` system enforces this limit by sleeping the most-idle non-busy agent when the cap is reached. If all 5 slots are busy (query_lock held), a `ConcurrencyLimitError` is raised and the message is queued.

**asyncio.create_task()**: Used for background operations — initial prompts (`bot.py:1785`), MCP tool spawns/kills (`bot.py:579, 629`), stranded message wakeups (`bot.py:2289`). A global exception handler (`bot.py:2859-2869`) catches unhandled exceptions in any asyncio task, with special suppression of expected `ProcessError` from SIGTERM'd subprocesses.

**tasks.loop(seconds=10)**: The scheduler tick. Single-threaded within the event loop — no concurrent scheduler ticks.

**Global `_shutdown_requested` flag**: Checked by `on_message()` and `check_schedules()` to reject new work during shutdown.

**Global rate limit state** (`_rate_limited_until`): Checked by `on_message()` to queue messages instead of sending them to the API. All agents share the same API account.

## 15. Self-Modification Capability

Axi can modify its own source code. The master agent's `cwd` is set by `DEFAULT_CWD` (`bot.py:62`), which defaults to `os.getcwd()` — in practice the bot directory, since `supervisor.py` does `os.chdir(DIR)` before launching (`supervisor.py:127`). The `can_use_tool` callback (`bot.py:234-255`) allows writes within `cwd`, `AXI_USER_DATA`, and any `EXTRA_ALLOWED_DIRS` paths — which includes `bot.py`, `schedules.json`, `USER_PROFILE.md`, etc. The master agent's system prompt explicitly tells it where its source code lives (`bot.py:391`).

The safety net for self-modification is the supervisor's rollback mechanism. If a code change crashes the bot on startup (within 60 seconds), the supervisor reverts the change and spawns a crash-handler agent to analyze what went wrong.

The system prompt instructs Axi to only restart when explicitly asked (`bot.py:408-409`) — not after every self-edit.

## 16. DM Handling

`on_message()` at `bot.py:2016-2026`: DMs from allowed users are not processed — instead, the bot sends a redirect message pointing them to the master channel in the server. DMs from non-allowed users are silently ignored.

## 17. Error Handling Philosophy

The codebase follows a consistent pattern: **log the error, notify the user in Discord, keep running**.

- All async operations are wrapped in try/except
- `discord.NotFound` on channel operations triggers channel recreation (`bot.py:1328-1339`)
- SDK `MessageParseError` is caught and skipped with warning log (`bot.py:1501-1507`)
- Failed agent wakes fall through to user notification with recovery suggestion (`bot.py:2102-2108`)
- `ConcurrencyLimitError` is caught and the message is queued instead of dropped (`bot.py:2091-2101`)
- Rate limit errors are caught, global state is set, and messages are queued for automatic retry (`bot.py:1604-1614`)
- The global exception handler (`bot.py:2859-2869`) catches unhandled exceptions in fire-and-forget tasks, with suppression of expected `ProcessError` from SIGTERM'd subprocesses (SDK bug workaround)
- `on_ready()` guards against duplicate firing from gateway reconnects (`bot.py:2647-2650`)
- SDK subprocess leak workaround (`bot.py:854-891`) ensures orphaned CLI processes are SIGTERM'd even when the SDK's own cleanup fails

## 18. Dependencies

From `pyproject.toml`:

| Package | Version | Purpose |
|---------|---------|---------|
| `discord.py` | Latest | Discord bot framework |
| `claude-agent-sdk` | Latest | Anthropic's Claude Code programmatic SDK |
| `python-dotenv` | Latest | `.env` file loading |
| `croniter` | Latest | Cron expression parsing |
| `httpx` | Latest | HTTP client for `discord_query.py` and Discord MCP tools |
| `tzdata` | >=2025.3 | IANA timezone database (for `ZoneInfo` DST handling) |
| `arrow` | Latest | Date/time math for `get_date_and_time` MCP tool (imported inline at `bot.py:644`) |

Runtime: Python >=3.12. Package manager: `uv`.

## 19. What's Not Here

- **No database** — All state is JSON files and Discord channels
- **No web server / API** — Discord is the only interface
- **No tests** — `TESTING_PLAN.md` exists but is unimplemented
- **No authentication layer beyond Discord** — `ALLOWED_USER_IDS` is the auth system
- **No multi-guild support** — Hardcoded to `DISCORD_GUILD_ID`
- **No encryption at rest** — `.env` has plaintext tokens, JSON files are plaintext
- **No agent-to-agent communication** — Agents are isolated; only the master can spawn/kill (though the `"discord"` MCP tools allow cross-channel messaging when enabled)
- **No persistent task queue** — `message_queue` is in-memory and lost on restart
