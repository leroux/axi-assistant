# Axi - Autonomous Personal Assistant

Axi is a Discord-based personal assistant powered by Claude Code. It runs as a persistent, self-modifying system that communicates through Discord guild channels. It features a multi-agent architecture with per-agent channels, a flowchart-driven behavior engine, a cron/one-off schedule system, automatic crash recovery with rollback, and a test instance system for safe self-modification.

## Table of Contents

- [Setup](#setup)
- [Runtime Configuration](#runtime-configuration)
- [Optional: ChatGPT/Codex via Anthropic Proxy](#optional-chatgptcodex-via-anthropic-proxy)
- [Architecture Overview](#architecture-overview)
- [Multi-Agent System](#multi-agent-system)
- [Extensions & Flowcharts](#extensions--flowcharts)
- [Schedule System](#schedule-system)
- [Crash Recovery & Rollback](#crash-recovery--rollback)
- [Discord Integration](#discord-integration)
- [Self-Modification](#self-modification)
- [Test Instances](#test-instances)
- [HTTP API](#http-api)
- [Configuration](#configuration)

---

## Setup

### 1. Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI installed and authenticated
- A Discord account

### 2. Create a Discord Bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and click **New Application**.
2. Go to the **Bot** tab:
   - Click **Reset Token** and save the token — you'll need it for `.env`.
   - Under **Privileged Gateway Intents**, enable **Message Content Intent**.
3. Go to the **OAuth2** tab:
   - Under **Scopes**, select `bot`.
   - Under **Bot Permissions**, select: `Manage Channels`, `Send Messages`, `Read Message History`, `Add Reactions`, `Manage Roles`, `View Channels`, `Attach Files`, `Embed Links`, `Use Slash Commands`.
   - Copy the generated URL and open it in your browser to invite the bot to your server.
4. Note your **guild (server) ID**: right-click the server name in Discord (with Developer Mode enabled in Settings > Advanced) and click **Copy Server ID**.
5. Note your **Discord user ID**: right-click your username and click **Copy User ID**.

### 3. Clone and Configure

```bash
git clone <repo-url> && cd axi-assistant

cp .env.template .env
```

Edit `.env` with your values:

```bash
DISCORD_TOKEN=<your bot token from step 2>
ALLOWED_USER_IDS=<your Discord user ID>
DISCORD_GUILD_ID=<your guild ID>
SCHEDULE_TIMEZONE=US/Pacific          # or your IANA timezone
DEFAULT_CWD=/path/to/axi-assistant    # absolute path to this repo
AXI_USER_DATA=/path/to/user-data      # where profile, schedules, etc. live
```

### Runtime Configuration

Axi separates the agent runtime from the model selection:

```bash
AXI_HARNESS=claude_code
AXI_MODEL=opus
```

`AXI_HARNESS` can be `claude_code` or `flowcoder`. `AXI_MODEL` can be a native
Claude alias such as `opus`, `sonnet`, or `haiku`, or a provider model ID such
as `gpt-5.4`. `gpt-*` models route through the local ChatGPT Anthropic proxy.
For FlowCoder, `AXI_FC_WRAP` controls the automatic wrapper flowchart; unset
defaults to `soul`, `prompt` uses the pass-through wrapper, and `off` disables
automatic wrapping.

See [docs/axi-runtime-configuration.md](docs/axi-runtime-configuration.md) for
examples and compatibility notes.

### Optional: ChatGPT/Codex via Anthropic Proxy

Axi normally uses Claude Code against Anthropic. To run the same Axi/Claude Code
integration through a ChatGPT/Codex-backed Anthropic-compatible endpoint, install
the helper scripts:

```bash
uv sync
scripts/anthropic-codex-proxy/install.sh
anthropic-proxy-codex-service start
```

Then add this to `.env`:

```bash
AXI_HARNESS=claude_code
AXI_MODEL=gpt-5.4
```

Axi will internally route Claude Code through the local Anthropic-compatible
proxy for `gpt-*` models. See
[docs/chatgpt-anthropic-proxy.md](docs/chatgpt-anthropic-proxy.md)
for the full setup, systemd option, and smoke tests.

### 4. Run

```bash
# Run directly
uv run python -m axi.supervisor

# -- OR --

# Install as a systemd user service (recommended for production)
cp axi-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now axi-bot.service

# Check status / logs
systemctl --user status axi-bot.service
journalctl --user -u axi-bot.service -f
```

On first startup, Axi will automatically create `#axi-master` and category channels (**Axi**, **Active**, **Killed**) in your guild, sync permissions, and create schedule data files in `AXI_USER_DATA`. You can now message Axi in `#axi-master`.

### 5. Build Your User Profile

Once Axi is running, use the `/build-user-profile` slash command in Discord to start a conversational interview. Axi will ask you about your preferences, context, and working style, then save the results to `AXI_USER_DATA/profile/`. This profile is injected into every agent's system prompt to personalize Axi's behavior.

The profile is optional — Axi works without it, but personalization improves response quality significantly.

---

## Architecture Overview

```
axi/supervisor.py (process supervisor — crash detection, rollback, hot restart)
  |
  +-- procmux bridge (Unix socket, persistent across bot restarts)
  |
  +-- axi/main.py (Discord bot + asyncio event loop)
       |
       +-- on_ready()        --> guild setup, master session, schedule loop, crash recovery
       +-- on_message()      --> routes messages by channel to the owning agent
       +-- slash commands    --> /list-agents, /kill-agent, /spawn, /restart, /stop, /skip, ...
       |
       +-- scheduler task loop (every 30s)
       |    +-- cron & one-off event firing
       |    +-- idle agent sleep (configurable timeout)
       |    +-- idle agent reminders (escalating)
       |
       +-- Agent sessions (ClaudeSDKClient via claudewire/procmux)
            +-- axi-master   (always present, full Axi personality)
            +-- spawned agents (extensions-based prompts, sandboxed to cwd)
            +-- crash-handler  (auto-spawned after crashes, if enabled)
```

**Key files:**

| File | Purpose |
|---|---|
| `axi/supervisor.py` | Process supervisor with crash recovery and optional auto-rollback |
| `axi/main.py` | Discord bot setup, slash commands, scheduling, idle management |
| `axi/agents.py` | Agent lifecycle — spawn, wake, sleep, kill, message routing |
| `axi/handlers.py` | Agent message handler, flowchart execution integration |
| `axi/channels.py` | Per-agent Discord channel management, category system |
| `axi/config.py` | Centralized configuration, env vars, paths, constants |
| `axi/tools.py` | MCP tools exposed to agents (spawn, kill, message, Discord API) |
| `axi/prompts.py` | System prompt assembly — SOUL + extensions + user profile |
| `axi/http_api.py` | HTTP trigger endpoint for external integrations |
| `axi/worktrees.py` | Git worktree creation, merge queue, cleanup |
| `axi_test.py` | CLI for managing isolated test instances |
| `prompts/SOUL.md` | Core personality and behavioral directives |
| `commands/` | FlowCoder flowchart definitions (soul.json, etc.) |
| `extensions/` | Modular prompt fragments and hooks |
| `.env` | Instance-specific configuration (gitignored) |

**Internal packages** (in `packages/`):

| Package | Purpose |
|---|---|
| `agenthub` | Multi-agent orchestration library — lifecycle, concurrency, rate limits |
| `claudewire` | Stream-JSON protocol wrapper for the Claude Agent SDK |
| `discordquery` | Lightweight Discord REST client (pure httpx, no discord.py dependency) |
| `procmux` | Process multiplexer over Unix socket for hot restart support |
| `flowcoder_engine` | FlowCoder execution engine for flowchart-driven agent behavior |
| `flowcoder_flowchart` | FlowCoder flowchart loader and tools |

---

## Multi-Agent System

Axi maintains a registry of named Claude Code sessions. Each agent gets its own Discord text channel. The master agent (`axi-master`) is always present and cannot be killed. Additional agents can be spawned to work on tasks autonomously.

### Core Concepts

- **Master agent (`axi-master`):** The primary session with the full Axi personality, dev context, and all admin tools. Always exists. Cannot be killed.
- **Spawned agents:** Independent sessions with extension-based prompts, sandboxed to their working directory.
- **Sleep/wake:** Idle agents are put to sleep (process suspended, no quota usage) and wake on the next message. This allows many agents to exist without hitting concurrency limits.
- **Per-agent channels:** Each agent has a dedicated Discord text channel. Messages are routed by channel, not by a global "active agent" concept.

### Agent Lifecycle

```
axi_spawn_agent (MCP tool)
    |
    +-- Create channel in guild (Active or Axi category)
    +-- Create git worktree if needed (auto-isolation)
    +-- Start ClaudeSDKClient session via procmux bridge
    +-- Run initial prompt in background
    |
    ... agent responds in its channel, sleeps when idle ...
    |
    +-- axi_kill_agent or /kill-agent
         +-- End session, move channel to Killed category
         +-- Merge worktree back to main if it has commits
```

### Spawning Agents

Agents are spawned via MCP tools — the master agent (or any admin agent) calls `axi_spawn_agent` with a name, working directory, prompt, and optional extensions/MCP servers. Schedule entries with `"agent": true` also auto-spawn dedicated agents.

### Slash Commands

| Command | Description |
|---|---|
| `/list-agents` | Show all sessions with status, idle time, cwd |
| `/kill-agent [name]` | Terminate a session (infers agent from current channel if no name given) |
| `/spawn <name> <cwd> <prompt>` | Spawn a new agent session |
| `/reset-context [name]` | Wipe an agent's conversation history |
| `/restart` | Restart the bot (exit code 42, supervisor relaunches) |
| `/stop` | Interrupt the current agent's response |
| `/skip` | Interrupt and discard the current response |
| `/model <model>` | Set the default model (opus/sonnet/haiku/gpt-5.4) |
| `/claude-usage` | Show API rate limit status |
| `/ping` | Check bot latency |
| `/verbose` | Toggle tool call visibility in the channel |
| `/debug` | Toggle full debug output (thinking, tool details) |
| `/toggle-plan-mode` | Enable/disable plan approval gate for the current agent |
| `/status` | Show bot status summary |

### Concurrency

- **`MAX_AWAKE_AGENTS`** (default 7): Maximum simultaneously awake agents. Under pressure, idle agents are aggressively slept.
- **Idle sleep:** Agents auto-sleep after `IDLE_SLEEP_SECONDS` (default 60, configurable).
- **Idle reminders:** Escalating notifications at 30 minutes, 3 hours, and 48 hours of inactivity.
- **Rate limits:** Tracked per-agent with automatic backoff and retry (up to 3 retries with exponential delay).

### Permissions & Sandboxing

| Layer | What it restricts | Scope |
|---|---|---|
| **OS sandbox** | Bash commands — filesystem and network access limited to `cwd` | All agents |
| **Tool callback** | Edit/Write/MultiEdit — file path must be within `cwd` (or worktrees dir for admin agents) | All agents |
| **MCP tool set** | Admin agents get all tools; spawned agents get spawn/kill/message only | By agent type |
| **Unrestricted** | Read/Grep/Glob — allowed everywhere for code exploration | All agents |

Symlink escapes are prevented by resolving paths with `os.path.realpath()`.

---

## Extensions & Flowcharts

### Extensions

Extensions are modular prompt fragments in `extensions/`. Each extension directory can contain:

- `prompt.md` — Prompt text injected into the agent's system prompt
- `meta.json` — Metadata: `audience` ("admin" or "all"), hooks, prompt_hooks
- `commands/` — FlowCoder flowchart commands specific to this extension

Extensions are loaded based on the `DEFAULT_PACKS` env var (comma-separated list). Agents can also receive extensions at spawn time.

### Flowcharts

Axi uses [FlowCoder](https://github.com/px-pride/flowcoder) for structured, multi-step agent behavior. Flowchart definitions in `commands/` drive the agent through classification, task execution, and record-keeping steps.

| Flowchart | Purpose |
|---|---|
| `prompt.json` | Pass-through wrapper that sends the message to the model as-is |
| `soul.json` | Core message handling — classify, route, execute, report |
| `soul-flow.json` | Soul flow variant |
| `mil.json` | Auto-execute deck cards with minimal human approval |
| `mill.json` | Auto-execute deck cards, stopping when human approval is needed |
| `algorithm.json` | Algorithm execution flow |
| `research-mode.json` | Research-focused execution flow |

---

## Schedule System

Axi has a built-in scheduler supporting both recurring (cron) and one-off events. Schedules are managed via MCP tools (`schedule_create`, `schedule_modify`, `schedule_delete`, `schedule_list`).

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
    "cwd": "/path/to/project"
  }
]
```

**Required fields:** `name`, `prompt`, plus one of `schedule` (cron) or `at` (ISO 8601 datetime).

**Optional fields:** `agent` (spawn dedicated session), `cwd` (working directory for spawned agent), `reset_context` (wipe history before firing), `session` (route to specific agent), `extensions`, `mcp_servers`.

### How It Works

The scheduler runs every 30 seconds. Recurring events use `croniter` for DST-aware cron evaluation. One-off events fire when their time arrives and are moved to history. If the target agent is busy, recurring events are skipped (not queued). Events that were due during downtime fire on the first cycle after startup.

---

## Crash Recovery & Rollback

The supervisor (`axi/supervisor.py`) manages bot lifecycle with crash classification and optional auto-rollback.

### Crash Classification

| Type | Condition | Response |
|---|---|---|
| **Intentional restart** | Exit code 42 | Relaunch immediately |
| **Clean stop** | Exit code 0 | Stop supervisor |
| **Startup crash** | Non-zero exit, uptime < 60s | Optional rollback, then relaunch |
| **Runtime crash** | Non-zero exit, uptime >= 60s | Relaunch (up to 3 consecutive crashes) |

### Auto-Rollback (opt-in)

When `ENABLE_ROLLBACK=1` and a startup crash occurs:

1. Stash uncommitted changes (`git stash push --include-untracked`)
2. Reset to pre-launch commit (`git reset --hard <pre_launch_commit>`)
3. Write `.rollback_performed` marker with crash details
4. Relaunch with the known-good code
5. On startup, read the marker and notify the user

### Crash Handler Agent (opt-in)

When `ENABLE_CRASH_HANDLER=1`, a `crash-handler` agent is auto-spawned after crashes to analyze the traceback and produce a fix plan (without auto-applying).

### Hot Restart

The procmux bridge persists across bot restarts. Sending SIGHUP to the supervisor kills only the bot process — the bridge stays alive, allowing agents to reconnect instantly without losing session state.

---

## Discord Integration

### Channel Architecture

Axi operates in a Discord guild (server), not DMs. Each agent gets its own text channel:

| Category | Contents |
|---|---|
| *(no category)* | `#axi-master` — pinned at position 0 |
| **Axi** | Admin agents (cwd is BOT_DIR or worktrees) |
| **Active** | All other spawned agents |
| **Killed** | Terminated agents (hidden from @everyone, preserved for history) |

Overflow categories (e.g., "Axi 2") are created when a category hits the 50-channel Discord limit.

### Channel Features

- **Status emoji prefix:** Channel names show agent state at a glance (configurable via `CHANNEL_STATUS_ENABLED`)
- **Recency sorting:** Most recently active channels float to the top (configurable via `CHANNEL_SORT_BY_RECENCY`)
- **Topic metadata:** Channel topics store `cwd`, `session_id`, `prompt_hash`, and `agent_type` for debugging and resume detection

### Response Streaming

Agent responses are streamed to Discord in real-time via live-editing messages. Tool calls and thinking indicators are shown based on the channel's verbose/debug settings. Long messages are split on newline boundaries to respect Discord's 2000-character limit.

### Plan Approval

When plan mode is enabled for an agent (`/toggle-plan-mode`), the agent pauses after producing a plan and posts it for review. Users approve or reject via checkmark/X reactions.

---

## Self-Modification

Axi is designed to modify its own source code. The master agent's working directory is the bot's repo, so it has write access to all source files. However, direct self-modification is dangerous — a bad edit can crash the running process.

### Safe Self-Modification Workflow

Instead of editing its own running code directly, Axi uses the test instance system:

1. Spawn a coding agent in a git worktree (auto-isolated copy of the repo)
2. Make changes in the worktree
3. Test via a disposable bot instance running the modified code
4. Squash-merge the worktree back to main
5. Restart to pick up the changes

The [rollback system](#crash-recovery--rollback) serves as a safety net — if a self-edit causes a startup crash, the changes are automatically reverted.

---

## Test Instances

The `axi_test.py` CLI manages disposable bot instances for testing code changes. Each instance runs in its own git worktree with a separate bot token, guild, virtualenv, data directory, and systemd service.

### Commands

```bash
uv run python axi_test.py up <name> [--wait]     # Reserve a slot and start instance
uv run python axi_test.py down <name>             # Stop and release slot
uv run python axi_test.py restart <name>          # Restart after code changes
uv run python axi_test.py list                    # Show all instances
uv run python axi_test.py merge [-m MSG]          # Squash-merge branch into main
uv run python axi_test.py queue [show|drop]       # Manage merge queue
uv run python axi_test.py msg <name> "<message>"  # Send message and wait for response
uv run python axi_test.py logs <name>             # Tail instance logs
```

### How It Works

- Bot tokens and test guilds are configured in `~/.config/axi/test-config.json`
- Slot reservation is atomic and file-locked to prevent races between concurrent agents
- `--wait` polls until a slot is available (useful when all tokens are in use)
- The merge queue serializes concurrent merges to prevent conflicts
- Worktrees and branches are cleaned up after successful merge

---

## HTTP API

An optional FastAPI server provides an HTTP endpoint for external integrations.

```
POST /v1/trigger
{
  "session": "agent-name",
  "prompt": "Do something",
  "cwd": "/optional/path",
  "extensions": ["optional-ext"],
  "mcp_servers": ["optional-server"]
}
```

Routes to an existing agent session or spawns a new one. Enabled by setting `HTTP_API_PORT` in the environment.

---

## Configuration

### Environment Variables

| Variable | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | Yes | Discord bot token |
| `ALLOWED_USER_IDS` | Yes | Comma-separated Discord user IDs authorized to interact |
| `DISCORD_GUILD_ID` | Yes | Discord server (guild) ID |
| `SCHEDULE_TIMEZONE` | No | IANA timezone for cron expressions (default: `UTC`) |
| `DEFAULT_CWD` | No | Default working directory for agent sessions |
| `AXI_USER_DATA` | No | Path to user data directory (profiles, schedules, MCP configs) |
| `AXI_HARNESS` | No | Agent runtime harness: `claude_code` or `flowcoder` |
| `AXI_MODEL` | No | Model name or alias (`haiku`, `sonnet`, `opus`, `gpt-5.4`, etc.) |
| `AXI_FC_WRAP` | No | FlowCoder auto-wrap command. Unset defaults to `soul`; use `prompt` for pass-through or `off` to disable |
| `AXI_CHATGPT_PROXY_BASE_URL` | No | Override for the ChatGPT Anthropic proxy URL when using `gpt-*` models |
| `AXI_CHATGPT_PROXY_API_KEY` | No | Override for the key sent to the ChatGPT Anthropic proxy |
| `DAY_BOUNDARY_HOUR` | No | Hour (0-23) when a new "day" starts for planning (default: `0`) |
| `IDLE_SLEEP_SECONDS` | No | Seconds before auto-sleeping idle agents (default: `60`) |
| `DEFAULT_PACKS` | No | Comma-separated list of extensions to load by default |
| `ENABLE_CRASH_HANDLER` | No | Set to `1` to auto-spawn crash analysis agent on recovery |
| `ENABLE_ROLLBACK` | No | Set to `1` to enable automatic git rollback on startup crashes |
| `HTTP_API_PORT` | No | Port for the HTTP trigger API (disabled if `0` or unset) |

### Feature Flags

| Variable | Default | Description |
|---|---|---|
| `FLOWCODER_ENABLED` | `1` | Legacy compatibility flag. Prefer `AXI_HARNESS=flowcoder` or `AXI_HARNESS=claude_code` |
| `STREAMING_DISCORD` | `1` | Stream agent responses to Discord in real-time |
| `CHANNEL_STATUS_ENABLED` | `1` | Show status emoji prefixes on channel names |
| `CHANNEL_SORT_BY_RECENCY` | `1` | Reorder channels by most recent activity |
| `CLEAN_TOOL_MESSAGES` | `0` | Clean up tool call indicator messages after completion |

### Key Constants

| Constant | Value | Location |
|---|---|---|
| `MASTER_AGENT_NAME` | `"axi-master"` | `axi/config.py` |
| `MAX_AWAKE_AGENTS` | `7` | `axi/config.py` |
| `RESTART_EXIT_CODE` | `42` | `axi/supervisor.py` |
| `CRASH_THRESHOLD` | `60s` | `axi/supervisor.py` |
| `MAX_RUNTIME_CRASHES` | `3` | `axi/supervisor.py` |
| `QUERY_TIMEOUT` | `43200s` (12h) | `axi/config.py` |
| `COMPACT_THRESHOLD` | `0.80` | `axi/config.py` |
