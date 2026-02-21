# Axi - Autonomous Personal Assistant

Axi is a Discord-based personal assistant powered by Claude Code. It runs as a persistent, self-modifying system that communicates exclusively through Discord DMs. It features a multi-agent architecture, a cron/one-off schedule system, and an automatic restart-and-rollback mechanism that recovers from bad self-edits.

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Quick Start](#quick-start)
- [Multi-Agent System](#multi-agent-system)
- [Schedule System](#schedule-system)
- [Restart & Rollback System](#restart--rollback-system)
- [Discord Integration](#discord-integration)
- [Self-Modification](#self-modification)
- [Configuration](#configuration)

---

## Architecture Overview

```
run.sh (process supervisor)
  |
  +-- bot.py (Discord bot + asyncio event loop)
       |
       +-- on_ready()        --> starts master session, schedule loop
       +-- on_message()      --> routes DMs to the active agent
       +-- slash commands    --> /switch-agent, /list-agents, /kill-agent, /reset-context
       |
       +-- check_schedules() task loop (every 30s)
       |    +-- restart signal detection    (.restart_requested)
       |    +-- spawn signal detection      (.spawn_agent)
       |    +-- cron & one-off event firing
       |    +-- idle agent detection & notifications
       |
       +-- Agent sessions (ClaudeSDKClient instances)
            +-- axi-master   (always present, has the Axi personality)
            +-- spawned agents (vanilla Claude Code, no custom prompt)
```

**Key files:**

| File | Purpose |
|---|---|
| `run.sh` | Process supervisor with auto-rollback (87 lines) |
| `bot.py` | The entire application (~835 lines) |
| `schedules.json` | User-defined schedule entries (gitignored, auto-created) |
| `schedule_history.json` | Log of fired one-off events, pruned to 7 days (gitignored) |
| `USER_PROFILE.md` | User preferences, read by Axi for personalization (gitignored) |
| `.env` | Environment variables (gitignored) |

**Dependencies:** `discord.py`, `claude-agent-sdk`, `python-dotenv`, `croniter`

---

## Quick Start

```bash
# 1. Clone the repo
git clone <repo-url> && cd personal-assistant

# 2. Configure environment
cp .env.template .env
# Edit .env with your Discord bot token and user IDs

# 3. Run (creates USER_PROFILE.md, schedules.json, schedule_history.json on first start)
./run.sh
```

Axi will DM you "Axi restarted." when it comes online.

---

## Multi-Agent System

Axi maintains a registry of named Claude Code sessions. One session is always active and receives your DM messages. The master agent (`axi-master`) is always present and cannot be killed. Additional agents can be spawned to work on tasks autonomously without polluting the master's conversation context.

### Core Concepts

- **Master agent (`axi-master`):** The primary session with the full Axi personality and system prompt. Always exists. Cannot be killed. Default recipient of all messages.
- **Spawned agents:** Independent Claude Code sessions with no custom personality. They run with `bypassPermissions` mode and work in a specified directory.
- **Active agent:** The session that currently receives your DM messages. Only one agent is active at a time.
- **Hard limit:** Maximum 20 concurrent agent sessions (`MAX_AGENTS`).

### Agent Lifecycle

```
spawn_agent()
    |
    +-- start_session(name, cwd)      # Creates ClaudeSDKClient, enters async context
    |
    +-- _run_initial_prompt()          # Runs in background via asyncio.create_task
    |   +-- Acquires query_lock
    |   +-- Sends prompt, consumes response silently
    |   +-- Notifies user when done
    |
    ... agent is idle, user can /switch-agent to interact ...
    |
    +-- end_session(name)              # Shuts down client with 5s timeout, removes from registry
```

Each session tracks:
- `query_lock` - asyncio lock preventing concurrent queries
- `last_activity` - timestamp for idle detection
- `stderr_buffer` - thread-safe buffer for tool execution output
- `idle_reminder_count` - escalating notification state

### Spawning Agents

There are two ways to spawn an agent:

#### 1. File-based spawning (from the master agent)

Axi can spawn agents by creating a `.spawn_agent` file in its project directory:

```json
{
  "name": "feature-auth",
  "cwd": "/home/pride/coding-projects/my-app",
  "prompt": "Implement JWT authentication for the API"
}
```

The scheduler loop picks this up within 30 seconds. Validation rules:
- `name` must be non-empty, unique, and not `axi-master`
- Total agents must be under the 20-session limit
- On failure, the user receives a DM explaining why

#### 2. Schedule-based spawning

Schedule entries with `"agent": true` automatically spawn a dedicated agent when they fire. Agent names are auto-generated with a timestamp suffix (e.g., `weekly-cleanup-20260220-0900`).

### Slash Commands

| Command | Description |
|---|---|
| `/switch-agent <name>` | Switch which agent receives your messages (autocomplete-enabled) |
| `/list-agents` | Show all sessions with status: active, busy, protected, idle time, cwd |
| `/kill-agent <name>` | Terminate a session (cannot kill `axi-master`; auto-switches to master if killing the active agent) |
| `/reset-context [working_dir]` | Wipe the active agent's conversation history, optionally change its working directory |

### Idle Agent Detection

The system monitors spawned agents for inactivity and sends escalating reminders through the master agent:

| Reminder | Fires after idle for |
|---|---|
| 1st | 30 minutes |
| 2nd | 3.5 hours (cumulative: 30m + 3h) |
| 3rd (final) | ~51.5 hours (cumulative: 30m + 3h + 48h) |

When idle agents are detected, the master agent is prompted to notify you and suggest using `/kill-agent` or `/switch-agent`.

### Concurrency

- Each agent has its own `asyncio.Lock`. If you message a busy agent, you get: *"Agent **name** is busy. Please wait or `/switch-agent` to another."*
- Stderr buffers use `threading.Lock` because the Claude SDK callback may fire from a different thread.
- Initial prompts for spawned agents run as background tasks (`asyncio.create_task`) so they don't block the event loop.

---

## Schedule System

Axi has a built-in scheduler that supports both recurring (cron) and one-off events. The master agent can create and edit schedule entries by modifying `schedules.json` directly.

### Schedule Entry Format

```json
[
  {
    "name": "daily-standup",
    "prompt": "Ask me what I'm working on today",
    "schedule": "0 9 * * *"
  },
  {
    "name": "reminder",
    "prompt": "Remind me to review the PR",
    "at": "2026-02-21T03:00:00+00:00"
  },
  {
    "name": "weekly-cleanup",
    "prompt": "Clean up unused imports across the project",
    "schedule": "0 9 * * 1",
    "agent": true,
    "cwd": "/home/pride/coding-projects/my-app"
  }
]
```

#### Required Fields

| Field | Description |
|---|---|
| `name` | Short identifier for the event |
| `prompt` | The message or instructions sent to Claude when the event fires |

Plus **one of:**

| Field | Description |
|---|---|
| `schedule` | Cron expression for recurring events (parsed by `croniter`) |
| `at` | ISO 8601 datetime with timezone for one-off events |

#### Optional Fields

| Field | Type | Default | Description |
|---|---|---|---|
| `reset_context` | boolean | `false` | Wipe the master agent's conversation history before firing |
| `agent` | boolean | `false` | Spawn a dedicated agent session instead of routing through the master |
| `cwd` | string | `DEFAULT_CWD` | Working directory for the spawned agent (required when `agent` is `true`) |
| `catch_up` | boolean | `false` | If `true`, fire missed recurring events on startup (by default, missed events are skipped) |

### How the Scheduler Works

The `check_schedules()` function runs as a `discord.ext.tasks.loop` every **30 seconds**. Each cycle:

1. **Signal checks** - looks for `.restart_requested` and `.spawn_agent` files
2. **History pruning** - removes fired event records older than 7 days
3. **Channel resolution** - fetches DM channels for all authorized users
4. **Event processing:**

**Recurring events:**
- Computes the most recent cron occurrence using `croniter.get_prev()`
- On first encounter, **seeds** the last-fired timestamp to prevent immediate firing on startup
- If the event has fired since the last check, it runs
- If the master is busy (query lock held), the event is **skipped** (not queued)

**One-off events:**
- If `fire_at <= now`, the event fires
- After firing, the entry is **removed** from `schedules.json` and appended to `schedule_history.json`

5. **Idle agent detection** - checks all spawned agents for inactivity

### Startup Behavior

On startup, all recurring events (without `catch_up: true`) have their `schedule_last_fired` pre-seeded to the most recent cron occurrence. This prevents every recurring event from firing immediately when the bot starts.

Events with `catch_up: true` skip this seeding, so they **will** fire on startup if they were missed during downtime.

---

## Restart & Rollback System

The restart and rollback system is split across two files: `run.sh` (process supervisor) and `bot.py` (signal detection and notification). It's specifically designed to handle the case where Axi modifies its own source code and introduces a crash.

### Restart Flow

```
1. User asks Axi to restart (or Axi decides to after a self-edit)
2. Axi runs: touch .restart_requested
3. check_schedules() detects the file within 30 seconds
4. File is deleted, bot exits with code 42
5. run.sh sees exit code 42, treats it as intentional restart
6. run.sh re-launches: uv run python bot.py
7. on_ready() sends "Axi restarted." DM to all authorized users
```

### Auto-Rollback Flow

```
1. Axi edits bot.py with a bug
2. Axi restarts (exit code 42)
3. bot.py crashes on startup (non-zero exit, <30s uptime)
4. run.sh detects "quick crash" (uptime < CRASH_THRESHOLD of 30s)
5. run.sh checks for uncommitted git changes
6. run.sh stashes all changes: git stash push --include-untracked -m "auto-rollback: ..."
7. run.sh writes .rollback_performed marker with crash details
8. run.sh sets rollback_attempted=1 (prevents infinite loops)
9. run.sh re-launches bot.py with the last committed code
10. on_ready() reads .rollback_performed, sends detailed notification:

    "Automatic rollback performed.
     Axi crashed on startup (exit code 1 after 2s) at 2026-02-20T...
     Uncommitted changes were stashed and reverted to the last committed version.
     To inspect: git stash list and git stash show -p
     To restore: git stash pop"
```

### run.sh Decision Tree

```
bot.py exits
  |
  +-- exit code 42? --> restart (reset rollback flag, loop)
  +-- exit code 0?  --> clean stop, exit supervisor
  +-- uptime >= 30s? --> not a startup crash, exit supervisor
  +-- uptime < 30s (quick crash):
       |
       +-- rollback already attempted? --> stop (prevent infinite loop)
       +-- not in a git repo? --> stop
       +-- no uncommitted changes? --> stop
       +-- uncommitted changes exist:
            +-- git stash push --include-untracked
            +-- write .rollback_performed marker (JSON with exit_code, uptime, stash_output, timestamp)
            +-- set rollback_attempted=1
            +-- re-launch bot.py
```

### Signal Files

| File | Created by | Purpose |
|---|---|---|
| `.restart_requested` | Axi (bot.py) | Signals the bot to exit with code 42 for a clean restart |
| `.spawn_agent` | Axi (bot.py) | Signals the scheduler to spawn a new agent session |
| `.rollback_performed` | run.sh | Communicates rollback details to bot.py on next startup |

All signal files are gitignored to prevent accidental commits.

### Default File Creation

On first run, `run.sh` creates default versions of user data files if they don't exist:
- `USER_PROFILE.md` - blank profile template
- `schedules.json` - empty array `[]`
- `schedule_history.json` - empty array `[]`

---

## Discord Integration

### DM-Only Architecture

Axi operates exclusively through Discord DMs. The bot requests only two intents: `dm_messages` and `message_content`. There is no server/guild functionality.

### Authentication

All interactions are gated by `ALLOWED_USER_IDS`. Unauthorized users are silently ignored for DMs and receive an ephemeral "Not authorized." for slash commands.

### Message Flow

```
User sends DM
  |
  +-- Ignore if: bot message, non-DM channel, unauthorized user
  +-- Get active session
  |    +-- Not ready? --> "Claude session not ready yet."
  |    +-- Query lock held? --> "Agent is busy."
  |
  +-- Acquire query_lock
  +-- Update last_activity, reset idle state
  +-- Send query to Claude SDK
  +-- Stream response to Discord
```

### Response Streaming

Responses are streamed to Discord in real-time using the Claude SDK's `include_partial_messages=True` option:

- **StreamEvent** messages accumulate text deltas in a buffer
- **AssistantMessage** boundaries trigger a flush (sends the buffered text)
- Buffers exceeding 1800 characters are flushed mid-turn, split on newline boundaries
- All messages are prefixed with the session name in italics (e.g., `*axi-master:*`)
- Discord's 2000-character limit is handled by `split_message()`, which splits on newline boundaries where possible
- Stderr output from tool executions is rendered in code blocks
- After the response completes: *"Bot has finished responding and is awaiting input."*

### Unknown Message Type Handling

The SDK's message stream can emit unknown message types (e.g., `rate_limit_event`) that would normally crash the parser. `_receive_response_safe()` wraps the raw message stream and silently skips unrecognized types instead of crashing.

---

## Self-Modification

Axi is explicitly designed to modify its own source code. The system prompt tells it that `~/coding-projects/personal-assistant` is its own codebase, and it runs with `permission_mode="bypassPermissions"` (fully autonomous, no approval prompts).

This means Axi can:
- Edit `bot.py` to add features or fix bugs
- Modify `schedules.json` to create/edit/remove scheduled events
- Update `USER_PROFILE.md` with learned preferences
- Edit `run.sh` to change supervisor behavior
- Trigger a restart via `touch .restart_requested`

The [rollback system](#restart--rollback-system) exists specifically as a safety net for this capability. If a self-edit introduces a startup crash, the changes are automatically stashed and the bot reverts to the last committed version.

---

## Configuration

### Environment Variables

| Variable | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | Yes | Discord bot token |
| `ALLOWED_USER_IDS` | Yes | Comma-separated Discord user IDs authorized to interact |
| `DEFAULT_CWD` | No | Default working directory for agent sessions (defaults to the bot's directory) |

### Constants (in `bot.py`)

| Constant | Value | Description |
|---|---|---|
| `MASTER_AGENT_NAME` | `"axi-master"` | Reserved name for the primary agent |
| `MAX_AGENTS` | `20` | Maximum concurrent agent sessions |
| `IDLE_REMINDER_THRESHOLDS` | `[30m, 3h, 48h]` | Escalating idle notification intervals (cumulative) |

### Constants (in `run.sh`)

| Constant | Value | Description |
|---|---|---|
| `RESTART_EXIT_CODE` | `42` | Exit code that signals an intentional restart |
| `CRASH_THRESHOLD` | `30` | Seconds — crashes faster than this trigger rollback |
| `ROLLBACK_MARKER` | `.rollback_performed` | Filename for the rollback info marker |
