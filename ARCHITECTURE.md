# Axi Assistant — Complete Architecture & Design Report

## 1. What This Is

Axi is a **self-hosted, self-modifying personal assistant** that lives inside a Discord server. It wraps Claude Code (via Anthropic's Agent SDK) in a multi-agent orchestration layer, giving you persistent AI agent sessions that each get their own Discord channel. You talk to Axi by typing in Discord; Axi talks back by streaming Claude's output into the channel in real time.

The system is three Python files, a systemd service, and a handful of JSON data files. There is no database, no web server, no API layer. Everything is files on disk and Discord API calls.

## 2. Process Hierarchy

```
systemd (axi-bot.service)
  └── supervisor.py         ← Process manager, crash recovery, rollback
       └── bot.py           ← Discord bot + agent orchestration + scheduler
            ├── axi-master  ← Always-on primary Claude session (has MCP tools)
            ├── agent-1     ← Spawned Claude session (vanilla Claude Code)
            ├── agent-2     ← ...
            └── ...up to 20 agents
```

**systemd** (`axi-bot.service:1-16`) runs `supervisor.py` as a user service. If the supervisor itself dies with a non-zero exit, systemd restarts it after 10 seconds.

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

## 4. Bot Core (`bot.py`, 1984 lines)

### 4.1 Initialization & Configuration

**Environment loading** (`bot.py:29-39`): Six env vars control the system:
- `DISCORD_TOKEN` — Bot auth (required)
- `ALLOWED_USER_IDS` — Comma-separated Discord user IDs that can interact (required)
- `DISCORD_GUILD_ID` — Target server (required)
- `SCHEDULE_TIMEZONE` — IANA timezone for cron evaluation (default: UTC)
- `DEFAULT_CWD` — Master agent's working directory (default: cwd)
- `AXI_USER_DATA` — Default working directory for spawned agents

**Discord bot setup** (`bot.py:43-49`): Minimal intents — guilds, guild messages, message content, DMs. Command prefix `!` (unused in practice; slash commands are the real interface).

**Constants** (`bot.py:63-67`):
- `MAX_AGENTS = 20` — Hard cap on concurrent agent sessions
- `IDLE_REMINDER_THRESHOLDS = [30min, 3h, 48h]` — Escalating idle notifications (cumulative)
- `QUERY_TIMEOUT = 600` — 10 minutes per query before timeout handling
- `INTERRUPT_TIMEOUT = 15` — Seconds to wait after sending interrupt before force-kill

### 4.2 The AgentSession Dataclass

Defined at `bot.py:73-88`. This is the core unit of state for every agent:

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
| `message_queue` | `asyncio.Queue` | Messages received while agent is busy |
| `injected_count` | `int` | Messages injected mid-stream (query-while-busy) |

**Global state** (`bot.py:91-100`): `agents` dict (name -> session), `_shutdown_requested` flag, guild/category references, `channel_to_agent` reverse mapping, `schedule_last_fired` tracking.

### 4.3 Agent Lifecycle — The Five States

An agent exists in one of five states:

```
                ┌──────────┐
                │  (none)  │  Not in agents dict
                └────┬─────┘
                     │ spawn_agent() / start_session()
                     ▼
              ┌──────────────┐
              │    AWAKE     │  client != None, lock unlocked
              │   (idle)     │
              └──┬───────┬───┘
       query()   │       │  sleep_agent() [auto after 1min idle]
                 ▼       ▼
          ┌──────────┐  ┌──────────────┐
          │   BUSY   │  │   SLEEPING   │  client == None, still in agents dict
          │  (locked)│  └──────┬───────┘
          └──────────┘         │ wake_agent() [on message or scheduled event]
                 │             ▼
                 │      ┌──────────────┐
                 └─────▶│    AWAKE     │
                        └──────────────┘

          kill_agent() / end_session() from any state → removed from agents dict
```

**Starting** (`bot.py:562-590`): `start_session()` creates a `ClaudeSDKClient` with these options:
- Model: `opus` (hardcoded)
- Effort: `high`
- Thinking: `adaptive` (extended thinking, model decides when to use it)
- Beta: `context-1m-2025-08-07` (1M context window)
- Permission mode: `default` with a custom `can_use_tool` callback
- Sandbox: enabled, with auto-allow for bash commands
- Streaming: `include_partial_messages=True`
- Setting sources: `["user", "project", "local"]` (reads `.claude/` config hierarchy)

**Sleeping** (`bot.py:626-645`): Shuts down the `ClaudeSDKClient` (5-second timeout) but keeps the `AgentSession` entry in the `agents` dict. The channel stays in Active category. This is a resource optimization — sleeping agents consume no Claude API resources.

**Waking** (`bot.py:648-701`): Creates a fresh `ClaudeSDKClient` with the stored `session_id` for resume. If resume fails (session expired, corrupted), falls back to a fresh session with context loss (`bot.py:681-701`). The wake options are identical to start except MCP servers are NOT re-attached (only the master gets MCP tools).

**Auto-sleep** (`bot.py:1536-1549`): The scheduler loop sleeps any agent idle for >1 minute. This runs every 10 seconds.

**Ending** (`bot.py:593-610`): `end_session()` shuts down the client and removes the session from `agents`. Called by kill operations.

**Resetting** (`bot.py:613-623`): `reset_session()` ends and restarts, preserving system prompt, channel mapping, and MCP servers. Used by `/reset-context`.

### 4.4 Permission & Sandboxing Model

Two independent layers restrict what agents can do:

**Layer 1 — OS-level sandbox** (`bot.py:582`): `sandbox={"enabled": True, "autoAllowBashIfSandboxed": True}`. This is Claude Code's built-in sandboxing — bash commands run in an isolated environment, and the `autoAllowBashIfSandboxed` flag means bash commands are auto-approved (no manual permission needed) since the sandbox contains blast radius.

**Layer 2 — `can_use_tool` callback** (`bot.py:170-189`): A closure over `allowed_cwd` that intercepts all tool calls. For file-writing tools (`Edit`, `Write`, `MultiEdit`, `NotebookEdit`), it checks that the target path resolves to within the agent's `cwd`. All other tools (reads, bash, web) are allowed everywhere. This prevents Agent A from writing to Agent B's directory.

**CWD restriction for spawn** (`bot.py:477-479`): The `axi_spawn_agent` MCP tool validates that the requested cwd is under either `AXI_USER_DATA` or `BOT_DIR`. You can't spawn an agent pointed at arbitrary filesystem paths.

### 4.5 Message Flow — User to Agent

```
User types in #agent-channel
         │
         ▼
on_message() [bot.py:1293]
         │
         ├─ DM? → redirect to guild channel [1298-1308]
         ├─ Wrong guild? → ignore [1311-1312]
         ├─ Not allowed user? → ignore [1314-1315]
         ├─ Shutdown in progress? → reject [1319-1321]
         ├─ No agent owns channel? → ignore [1324-1326]
         │
         ▼
    Agent found for channel
         │
         ├─ Agent BUSY (lock held)?
         │    ├─ Client alive? → inject mid-stream [1334-1339]
         │    │   (increments injected_count, SDK processes after current turn)
         │    └─ Client dead? → queue message [1342-1348]
         │
         └─ Agent NOT busy:
              │
              ├─ Acquire query_lock [1351]
              ├─ Sleeping? → wake_agent() [1353-1361]
              ├─ Reset idle counters [1364-1366]
              ├─ drain_stderr() + drain_sdk_buffer() [1367-1368]
              ├─ asyncio.timeout(600s) [1370]
              │    ├─ client.query(_as_stream(content)) [1371]
              │    └─ stream_response_to_channel() [1372]
              ├─ TimeoutError? → _handle_query_timeout() [1373-1374]
              └─ Release lock, _process_message_queue() [1384]
```

### 4.6 Message Injection vs. Queuing

When a user sends a message while an agent is busy, two different mechanisms apply:

**Injection** (`bot.py:1334-1339`): If the SDK client is alive, the message is injected directly into the running conversation via `session.client.query()`. The `injected_count` is incremented. In `_receive_response_safe()` (`bot.py:950-966`), when a `ResultMessage` arrives and `injected_count > 0`, the generator doesn't terminate — it continues listening for the response to the injected query. This means the agent processes the new message immediately after finishing its current turn, without dropping context.

**Queuing** (`bot.py:1342-1348`): If the client is somehow dead while the lock is held (edge case), the message goes into an `asyncio.Queue`. After the current query finishes and the lock releases, `_process_message_queue()` (`bot.py:1175-1218`) drains the queue one message at a time, waking the agent if needed.

### 4.7 Response Streaming

`stream_response_to_channel()` (`bot.py:969-1028`) is the bridge between Claude's streaming output and Discord messages:

1. Wraps everything in `channel.typing()` (shows "Bot is typing..." in Discord)
2. Iterates over `_receive_response_safe()`, which wraps the SDK's raw message stream
3. `StreamEvent` with `content_block_delta` / `text_delta` → appends to `text_buffer`
4. `AssistantMessage` (tool use boundary) → flushes buffer to Discord
5. Buffer exceeds 1800 chars → flush at nearest newline boundary (`bot.py:1007-1013`)
6. `ResultMessage` → extract session_id, flush, done
7. Stderr is drained and sent as code blocks between text chunks
8. After the stream ends, sends "Bot has finished responding and is awaiting input."

`send_long()` (`bot.py:923-940`) splits any message >2000 chars (Discord's limit) at newline boundaries. If a channel has been deleted, it auto-recreates it (`bot.py:929-940`).

### 4.8 Query Timeout & Recovery

`_handle_query_timeout()` (`bot.py:1030-1067`) implements a two-phase recovery:

**Phase 1 — Graceful interrupt** (`bot.py:1035-1048`):
- Calls `session.client.interrupt()` (sends SIGINT to Claude process)
- Waits 15 seconds for a `ResultMessage` (captures session_id)
- If successful: context preserved, agent still alive

**Phase 2 — Kill and resume** (`bot.py:1052-1067`):
- If interrupt fails or times out
- Calls `end_session()` to fully terminate
- If a `session_id` exists: start new session with `resume=old_session_id` → context preserved
- If no `session_id`: start fresh → context lost
- Notifies the user which recovery path was taken

### 4.9 SDK Buffer Drain

`drain_sdk_buffer()` (`bot.py:129-167`) is a defensive measure against a specific SDK behavior. The Claude SDK uses an internal message queue (`_message_receive`) that's shared across query/response cycles. If a previous response left unconsumed messages (post-`ResultMessage` system messages), they'd pollute the next query's response stream. This function does a non-blocking drain of stale messages before each new query, using `anyio.WouldBlock` to detect an empty queue. It accesses SDK internals (`session.client._query._message_receive`).

## 5. MCP Tools (Master Agent Only)

The master agent gets two custom tools via Model Context Protocol, defined at `bot.py:456-557`:

### 5.1 `axi_spawn_agent`

Schema at `bot.py:460-469`. Parameters: `name` (required), `cwd` (optional, defaults to `AXI_USER_DATA`), `prompt` (required), `resume` (optional session ID).

Validation (`bot.py:477-488`):
- CWD must be under `AXI_USER_DATA` or `BOT_DIR`
- Name cannot be empty or `axi-master`
- Name must be unique (unless `resume` is set)
- Agent count must be under `MAX_AGENTS` (20)

The actual spawn runs as a fire-and-forget `asyncio.create_task()` (`bot.py:499`) — the MCP tool returns immediately with a success message while the agent boots in the background.

### 5.2 `axi_kill_agent`

Schema at `bot.py:508-513`. Parameter: `name` (required).

Validation: name not empty, not `axi-master`, agent exists. The kill also runs as background task (`bot.py:546`) — sleeps the agent client and moves the channel to the Killed category.

### 5.3 MCP Server Registration

`bot.py:553-557`: Both tools are bundled into a single MCP server named `"axi"` via `create_sdk_mcp_server()`. This server is passed to `start_session()` only for the master agent (`bot.py:1768`). Spawned agents get no MCP tools — they're vanilla Claude Code instances.

## 6. Discord Guild Infrastructure

### 6.1 Category System

Axi organizes agent channels into two Discord categories:

- **Active** — Channels for live agents (awake or sleeping)
- **Killed** — Archived channels for terminated agents (chat history preserved)

`ensure_guild_infrastructure()` (`bot.py:748-785`) finds or creates both categories and syncs permissions on every startup.

### 6.2 Permission Model

`_build_category_overwrites()` (`bot.py:721-745`):
- `@everyone` — Can view and read history, but **cannot send messages or react**
- Bot — Full permissions (send, manage channels/messages, view, read history)
- Each allowed user — Can send, react, view, read history

This means: anyone in the server can watch agent conversations, but only allowed users can interact.

### 6.3 Channel Lifecycle

- **Creation**: `ensure_agent_channel()` (`bot.py:839-863`) — searches Active, then Killed (moves back to Active if found), then creates new
- **Archival**: `move_channel_to_killed()` (`bot.py:866-880`) — moves from Active to Killed category
- **Naming**: `_normalize_channel_name()` (`bot.py:712-718`) — lowercases, replaces spaces with hyphens, strips non-alphanumeric, truncates to 100 chars
- **Metadata**: Channel topics store `cwd: /path | session: uuid` parsed by `_parse_channel_topic()` (`bot.py:283-295`)

### 6.4 Agent Reconstruction on Restart

`reconstruct_agents_from_channels()` (`bot.py:788-836`) scans both Active and Killed categories on startup. For each channel with a valid topic (containing `cwd`), it creates a sleeping `AgentSession` (no client) and registers the channel mapping. This means agents survive restarts — their Discord channels persist, and when a user messages the channel, the agent wakes up and resumes from its stored session_id.

## 7. Scheduling System

### 7.1 Scheduler Loop

`check_schedules()` (`bot.py:1391-1549`) runs as a `discord.ext.tasks.loop` every 10 seconds. It does five things per tick:

1. **Prune data** — Remove history entries >7 days old, remove expired skip entries
2. **Process schedule entries** — Fire recurring (cron) and one-off (at) events
3. **Idle agent detection** — Escalating notifications
4. **Auto-sleep** — Sleep agents idle >1 minute
5. **Stranded message safety net** — Wake sleeping agents that have queued messages

### 7.2 Schedule Entry Format

`schedules.json` is a flat JSON array. Each entry:

```json
{
  "name": "identifier",
  "prompt": "instructions",
  "schedule": "0 9 * * *",
  "at": "2026-02-21T03:00:00Z",
  "agent": true,
  "cwd": "/path",
  "session": "name",
  "reset_context": true
}
```

- `name` (required): Unique identifier
- `prompt` (required): Message/instructions sent to agent
- `schedule`: Cron expression (recurring) — evaluated in SCHEDULE_TIMEZONE
- `at`: ISO datetime with timezone (one-off)
- `agent`: Spawn dedicated agent vs. route through master
- `cwd`: Working directory for spawned agent
- `session`: Reuse named agent session across events
- `reset_context`: Wipe conversation before firing

### 7.3 Recurring Event Firing Logic

`bot.py:1414-1447`:

1. Validate cron expression via `croniter.is_valid()`
2. Compute `last_occurrence` = most recent cron match relative to `now_local` (in `SCHEDULE_TIMEZONE`)
3. First encounter: seed `schedule_last_fired[name]` to `last_occurrence` (prevents firing on startup for events that should have fired earlier)
4. Fire if `last_occurrence > schedule_last_fired[name]`
5. Check one-off skip via `check_skip()` — if the event's name matches a skip entry for today's date, skip this firing
6. Route to existing session (if `session` field matches a running agent) or spawn new

### 7.4 One-Off Event Firing Logic

`bot.py:1449-1472`:

1. Parse `fire_at` from ISO string
2. Fire if `fire_at <= now_utc`
3. Route or spawn (same as recurring)
4. Remove from `schedules.json`, append to `schedule_history.json` with timestamp

### 7.5 Schedule Skips

`schedule_skips.json` allows one-off cancellations of recurring events. Each entry has `name` (matching the event) and `skip_date` (YYYY-MM-DD in `SCHEDULE_TIMEZONE`). `check_skip()` (`bot.py:261-270`) checks and auto-removes matching entries. `prune_skips()` (`bot.py:252-258`) removes entries for past dates.

### 7.6 Session Routing

When a schedule entry has a `"session"` field (`bot.py:1434`), the scheduler uses that as the agent name instead of the event name. If that agent already exists, the prompt is sent to it via `send_prompt_to_agent()` (`bot.py:1119-1136`). If not, a new agent is spawned with that name. This allows multiple schedule entries to share one persistent agent session.

### 7.7 Idle Detection

`bot.py:1480-1522`: For each non-master, non-sleeping, non-busy, non-killed agent:

- Compute cumulative threshold: `sum(IDLE_REMINDER_THRESHOLDS[:count + 1])`
  - Reminder 0: after 30 minutes idle
  - Reminder 1: after 3.5 hours cumulative (30m + 3h)
  - Reminder 2: after 51.5 hours cumulative (30m + 3h + 48h)
- Send notification to agent's channel AND master channel
- Increment `idle_reminder_count`

This is a passive notification system — it doesn't kill agents, just reminds the user to.

## 8. Graceful Shutdown

### 8.1 Trigger

`/restart` slash command (`bot.py:1729-1745`) or `_graceful_shutdown()` called programmatically.

### 8.2 Flow

`_graceful_shutdown()` (`bot.py:1234-1287`):

1. Set `_shutdown_requested = True` (blocks new messages and scheduler)
2. Identify busy agents (locked `query_lock`)
3. If none busy → sleep all agents, close bot, `os._exit(42)` immediately
4. Notify each busy agent's channel
5. Poll every 5 seconds, send status update every 30 seconds
6. Hard timeout at 10 minutes (`QUERY_TIMEOUT`) → force sleep all, `os._exit(42)`

Exit code 42 tells the supervisor to relaunch. `_sleep_all_agents()` (`bot.py:1224-1231`) shuts down all SDK clients, preserving session IDs in memory (and in channel topics for reconstruction).

### 8.3 Force Restart

`/restart force=True` (`bot.py:1736-1741`) skips the wait entirely — sleeps all agents and exits immediately.

## 9. Startup Sequence

`on_ready()` (`bot.py:1752-1967`):

1. Install global async exception handler (`bot.py:1758`)
2. Guard against duplicate `on_ready` (Discord gateway reconnects) (`bot.py:1760-1763`)
3. Start master session with retry (up to 3 attempts, exponential backoff 5s/10s) (`bot.py:1765-1776`)
4. Set up guild infrastructure (categories + permissions) (`bot.py:1779-1795`)
5. Create/find master channel, bind to session (`bot.py:1781-1792`)
6. Reconstruct sleeping agents from existing channels (`bot.py:1798-1801`)
7. Sync slash commands (`bot.py:1803`)
8. Start scheduler loop (`bot.py:1806`)
9. Check for rollback marker → parse, delete, notify master channel, spawn crash-handler agent (`bot.py:1810-1931`)
10. Check for crash analysis marker → same pattern (`bot.py:1825-1966`)
11. If neither marker: send simple "Axi restarted" notification (`bot.py:1879`)

### 9.1 Crash Handler Agent

When either marker exists, a `crash-handler` agent is spawned (`bot.py:1882-1966`) with a detailed prompt containing:
- Exit code, uptime, timestamp
- Rollback details (what was stashed/reverted, commit hashes)
- Last 200 lines of crash log
- Instructions to analyze root cause and produce a fix plan (but NOT apply fixes)

The crash handler is a standard spawned agent — it gets its own Discord channel where it writes its analysis.

## 10. Slash Commands

| Command | Handler | Description |
|---------|---------|-------------|
| `/list-agents` | `bot.py:1577-1613` | Lists all agents with status (busy/awake/sleeping), killed tag, channel link, cwd, idle time, session ID |
| `/kill-agent <name>` | `bot.py:1616-1661` | Sleeps agent, moves channel to Killed, shows session ID for resume |
| `/stop [agent_name]` | `bot.py:1664-1696` | Sends interrupt signal to a busy agent (like Ctrl+C). Infers agent from channel if not specified |
| `/reset-context [name] [cwd]` | `bot.py:1699-1726` | Wipes conversation history, optionally changes working directory |
| `/restart [force]` | `bot.py:1729-1745` | Graceful or force restart |

All commands check `ALLOWED_USER_IDS` and return ephemeral errors for unauthorized users. Agent name parameters have autocomplete callbacks (`bot.py:1559-1574`).

## 11. System Prompt

The master agent's system prompt (`bot.py:314-451`, ~140 lines) defines Axi's identity and capabilities:

**Identity**: Personal assistant in Discord. Autonomous system, not just an LLM behind a bot. Each agent has its own channel.

**Key instructions**:
- Read `USER_PROFILE.md` at conversation start for personalization
- Edit `schedules.json` directly for scheduling (no API — Claude reads/writes the file)
- Cron times are in `SCHEDULE_TIMEZONE`, not UTC
- Use `axi_spawn_agent` / `axi_kill_agent` MCP tools for agent management
- Use `discord_query.py` via bash for message history lookups
- Send progress updates every 30-60 seconds (Discord has no typing indicator for bots in the same way)
- Never use `AskUserQuestion`, `TodoWrite`, `EnterPlanMode`, `ExitPlanMode`, `Skill`, or `EnterWorktree` — these are Claude Code UI features invisible in Discord (`bot.py:431-450`)
- Never guess or fabricate answers — look things up or ask

**Interpolated values**: `%(axi_user_data)s` and `%(bot_dir)s` are substituted at runtime (`bot.py:451`).

## 12. Discord Query Tool (`discord_query.py`, 450 lines)

A standalone CLI that the master agent runs via bash to query Discord's REST API. It's independent of bot.py — it loads `.env` directly and creates its own HTTP client.

### 12.1 Subcommands

**`guilds`** (system prompt line 389): Lists servers the bot is in. Output: JSONL with `id` and `name`.

**`channels <guild_id>`** (`discord_query.py:227-253`): Lists text channels. Output: JSONL with `id`, `name`, `type`, `category`, `position`.

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
| `agent_history.json` | bot.py (unused in current code) | Indefinite | Agent lifecycle log |
| `USER_PROFILE.md` | Axi (master) | Indefinite | User preferences |
| `.rollback_performed` | supervisor → bot.py | Single use (deleted after read) | Startup crash rollback context |
| `.crash_analysis` | supervisor → bot.py | Single use (deleted after read) | Runtime crash context |
| `.bot_output.log` | supervisor | Overwritten each run (append mode) | Bot stdout/stderr for crash analysis |

All JSON files use graceful error recovery — `load_schedules()` etc. return `[]` on `FileNotFoundError` or `json.JSONDecodeError` (`bot.py:194-199`).

## 14. Concurrency Model

**asyncio.Lock per agent** (`query_lock`): Ensures exactly one query runs per agent at any time. This is the primary concurrency control. The lock is checked (not acquired) to detect busy state for injection vs. queuing decisions.

**threading.Lock per agent** (`stderr_lock`): The Claude SDK calls stderr callbacks from its own thread. The threading lock protects the `stderr_buffer` list from concurrent access between the SDK thread and the asyncio event loop.

**asyncio.Queue per agent** (`message_queue`): Unbounded queue for messages received while agent is busy and client is dead. Drained serially after each query completes.

**asyncio.create_task()**: Used for fire-and-forget operations — initial prompts (`bot.py:1116`), MCP tool spawns/kills (`bot.py:499, 546`), stranded message wakeups (`bot.py:1534`).

**tasks.loop(seconds=10)**: The scheduler tick. Single-threaded within the event loop — no concurrent scheduler ticks.

**Global `_shutdown_requested` flag**: Checked by `on_message()` and `check_schedules()` to reject new work during shutdown.

## 15. Self-Modification Capability

Axi can modify its own source code. The master agent's `cwd` is the bot directory itself (`DEFAULT_CWD`). The `can_use_tool` callback (`bot.py:170-189`) allows writes within `cwd`, which includes `bot.py`, `schedules.json`, `USER_PROFILE.md`, etc. The master agent's system prompt explicitly tells it where its source code lives (`bot.py:322`).

The safety net for self-modification is the supervisor's rollback mechanism. If a code change crashes the bot on startup (within 60 seconds), the supervisor reverts the change and spawns a crash-handler agent to analyze what went wrong.

The system prompt instructs Axi to only restart when explicitly asked (`bot.py:339-340`) — not after every self-edit.

## 16. DM Handling

`on_message()` at `bot.py:1298-1308`: DMs from allowed users are not processed — instead, the bot sends a redirect message pointing them to the master channel in the server. DMs from non-allowed users are silently ignored.

## 17. Error Handling Philosophy

The codebase follows a consistent pattern: **log the error, notify the user in Discord, keep running**.

- All async operations are wrapped in try/except
- `discord.NotFound` on channel operations triggers channel recreation (`bot.py:929-940`)
- SDK `MessageParseError` is caught and skipped (`bot.py:957-960`)
- Failed agent wakes fall through to user notification with recovery suggestion (`bot.py:1357-1361`)
- The global exception handler (`bot.py:1969-1975`) catches unhandled exceptions in fire-and-forget tasks
- `on_ready()` guards against duplicate firing from gateway reconnects (`bot.py:1760-1763`)

## 18. Dependencies

From `pyproject.toml`:

| Package | Version | Purpose |
|---------|---------|---------|
| `discord.py` | Latest | Discord bot framework |
| `claude-agent-sdk` | Latest | Anthropic's Claude Code programmatic SDK |
| `python-dotenv` | Latest | `.env` file loading |
| `croniter` | Latest | Cron expression parsing |
| `httpx` | Latest | HTTP client for `discord_query.py` |
| `tzdata` | >=2025.3 | IANA timezone database (for `ZoneInfo` DST handling) |

Runtime: Python >=3.12. Package manager: `uv`.

## 19. What's Not Here

- **No database** — All state is JSON files and Discord channels
- **No web server / API** — Discord is the only interface
- **No tests** — `TESTING_PLAN.md` exists but is unimplemented
- **No authentication layer beyond Discord** — `ALLOWED_USER_IDS` is the auth system
- **No rate limiting on the bot side** — Relies on Discord's rate limits and Claude API limits
- **No multi-guild support** — Hardcoded to `DISCORD_GUILD_ID`
- **No encryption at rest** — `.env` has plaintext tokens, JSON files are plaintext
- **No agent-to-agent communication** — Agents are isolated; only the master can spawn/kill
- **No persistent task queue** — `message_queue` is in-memory and lost on restart
