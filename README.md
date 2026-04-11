# Axi - Autonomous Personal Assistant

Axi is a Discord-based personal assistant powered by Claude Code. It runs as a persistent, self-modifying system that communicates through Discord DMs. It features a multi-agent architecture, a cron/one-off schedule system, an automatic restart-and-rollback mechanism that recovers from bad self-edits, and runtime crash recovery with automatic crash analysis.

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Quick Start](#quick-start)
- [Multi-Agent System](#multi-agent-system)
- [Schedule System](#schedule-system)
- [Restart & Rollback System](#restart--rollback-system)
- [Discord Integration](#discord-integration)
- [Discord Query Tool](#discord-query-tool)
- [Permissions & Sandboxing](#permissions--sandboxing)
- [Self-Modification](#self-modification)
- [Voice Interface](#voice-interface)
- [Configuration](#configuration)

---

## Architecture Overview

```
run.sh (process supervisor — output capture, crash detection, rollback)
  |
  +-- bot.py (Discord bot + asyncio event loop)
       |
       +-- on_ready()        --> starts master session, schedule loop, crash recovery
       +-- on_message()      --> routes DMs to the active agent
       +-- slash commands    --> /switch-agent, /list-agents, /kill-agent, /reset-context, /config
       |
       +-- check_schedules() task loop (every 30s)
       |    +-- restart signal detection    (.restart_requested)
       |    +-- spawn signal detection      (.spawn_agent)
       |    +-- cron & one-off event firing
       |    +-- idle agent detection & notifications
       |
       +-- Agent sessions (ClaudeSDKClient instances, sandboxed to cwd)
            +-- axi-master   (always present, has the Axi personality)
            +-- spawned agents (vanilla Claude Code, no custom prompt)
            +-- crash-handler  (auto-spawned after runtime crashes)
```

**Key files:**

| File | Purpose |
|---|---|
| `run.sh` | Process supervisor with crash recovery and auto-rollback (~150 lines) |
| `bot.py` | The entire application (~1130 lines) |
| `discord_query.py` | Standalone CLI tool for querying Discord server message history (~450 lines) |
| `schedules.json` | User-defined schedule entries (gitignored, auto-created) |
| `schedule_history.json` | Log of fired one-off events, pruned to 7 days (gitignored) |
| `profile/` | User-customizable profile files — USER_PROFILE.md and refs/ subdirectory (gitignored, auto-created) |
| `.env` | Environment variables (gitignored) |

**Dependencies:** `discord.py`, `claude-agent-sdk`, `python-dotenv`, `croniter`, `httpx`

---

## Quick Start

```bash
# 1. Clone the repo
git clone <repo-url> && cd personal-assistant

# 2. Configure environment
cp .env.template .env
# Edit .env with your Discord bot token and user IDs

# 3. Run directly (creates profile/, schedules.json, schedule_history.json on first start)
uv run python supervisor.py

# -- OR --

# 3. Install as a systemd user service (recommended for production)
cp axi-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable axi-bot.service
systemctl --user start axi-bot.service

# Check status / logs
systemctl --user status axi-bot.service
journalctl --user -u axi-bot.service -f
```

Axi will message the `#axi-master` channel when it comes online.

---

## Multi-Agent System

Axi maintains a registry of named Claude Code sessions. One session is always active and receives your DM messages. The master agent (`axi-master`) is always present and cannot be killed. Additional agents can be spawned to work on tasks autonomously without polluting the master's conversation context.

### Core Concepts

- **Master agent (`axi-master`):** The primary session with the full Axi personality and system prompt. Always exists. Cannot be killed. Default recipient of all messages.
- **Spawned agents:** Independent Claude Code sessions with no custom personality. They work in a specified directory and are sandboxed to it (see [Permissions & Sandboxing](#permissions--sandboxing)).
- **Active agent:** The session that currently receives your DM messages. Only one agent is active at a time.
- **Concurrency:** Up to 5 agents can be awake simultaneously (`MAX_AWAKE_AGENTS`). Sleeping agents are unlimited.

### Session Configuration

All agent sessions are created with the following Claude SDK options:

| Option | Value | Description |
|---|---|---|
| `model` | `opus` | Uses Claude Opus for all sessions |
| `effort` | `high` | High reasoning effort |
| `thinking` | `adaptive` | Enables adaptive extended thinking |
| `betas` | `context-1m-2025-08-07` | 1M context window beta |
| `setting_sources` | `user, project, local` | Reads settings from all config layers |
| `permission_mode` | `default` | Standard permission handling |

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
| `/config [auto_switch] [visibility]` | View or update agent configuration (see below) |
| `/restart` | Immediately restart the bot (exit code 42, triggers run.sh relaunch) |

### Idle Agent Detection

The system monitors spawned agents for inactivity and sends escalating reminders through the master agent:

| Reminder | Fires after idle for |
|---|---|
| 1st | 30 minutes |
| 2nd | 3.5 hours (cumulative: 30m + 3h) |
| 3rd (final) | ~51.5 hours (cumulative: 30m + 3h + 48h) |

When idle agents are detected, the master agent is prompted to notify you and suggest using `/kill-agent` or `/switch-agent`.

### Agent Configuration (`/config`)

The `/config` command controls two behaviors:

| Setting | Values | Default | Description |
|---|---|---|---|
| `auto_switch` | `on` / `off` | `on` | When a new agent is spawned, automatically switch to it as the active agent |
| `visibility` | `active` / `all` | `active` | Which agents' output is streamed to Discord |

**Visibility modes:**
- **`active`** — Only the currently active agent's output appears in Discord. Other agents run silently in the background (their output is still stored in `last_response` for `/last-response`).
- **`all`** — All agents' output is streamed to Discord in real-time, regardless of which is active.

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
| `schedule` | Cron expression for recurring events in `SCHEDULE_TIMEZONE` (parsed by `croniter`, DST-aware) |
| `at` | ISO 8601 datetime with timezone for one-off events |

#### Optional Fields

| Field | Type | Default | Description |
|---|---|---|---|
| `reset_context` | boolean | `false` | Wipe the master agent's conversation history before firing |
| `agent` | boolean | `false` | Spawn a dedicated agent session instead of routing through the master |
| `cwd` | string | `DEFAULT_CWD` | Working directory for the spawned agent (required when `agent` is `true`) |

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

On startup, recurring events that were due during downtime will fire on the first scheduler cycle (within 30 seconds of boot). The scheduler initializes `schedule_last_fired` on first encounter, so a newly added schedule will fire immediately if its most recent cron occurrence is in the past.

---

## Restart & Rollback System

The restart and rollback system is split across two files: `run.sh` (process supervisor) and `bot.py` (signal detection and notification). It handles two categories of crashes: **startup crashes** (bad self-edits that break on import) and **runtime crashes** (errors that occur after the bot has been running). All bot output is captured to `.bot_output.log` via `tee` for crash analysis.

### Restart Flow

```
1. User asks Axi to restart (or uses /restart, or Axi decides to after a self-edit)
2. Via /restart: bot exits immediately with code 42
   Via signal file: Axi runs touch .restart_requested, check_schedules() detects it within 30s
3. run.sh sees exit code 42, treats it as intentional restart
4. run.sh re-launches: uv run python bot.py
5. on_ready() starts the master session (retries up to 3 times with exponential backoff)
6. on_ready() sends "Axi restarted." DM to all authorized users
```

### Auto-Rollback Flow (Startup Crashes)

The rollback system handles both **uncommitted changes** and **committed changes** made since the last launch. It triggers when the bot crashes within 60 seconds of startup (`CRASH_THRESHOLD`).

Before each launch, `run.sh` records the current commit hash (`pre_launch_commit`). If a quick crash occurs, it compares `HEAD` against this snapshot to detect new commits.

```
1. Axi edits bot.py with a bug (committed or uncommitted)
2. Axi restarts (exit code 42)
3. bot.py crashes on startup (non-zero exit, <60s uptime)
4. run.sh detects "quick crash" (uptime < CRASH_THRESHOLD of 60s)
5. run.sh snapshots the last 200 lines of .bot_output.log for crash analysis
6. run.sh checks for rollback-able changes:
   a. Uncommitted changes? --> git stash push --include-untracked
   b. HEAD moved since pre-launch? --> git reset --hard <pre_launch_commit>
7. run.sh writes .rollback_performed marker with crash details (including crash log)
8. run.sh sets rollback_attempted=1 (prevents infinite loops)
9. run.sh re-launches bot.py with the pre-launch code
10. on_ready() reads .rollback_performed, sends detailed notification
11. on_ready() spawns a "crash-handler" agent to analyze the crash
```

Both rollback types can happen simultaneously — if Axi made some commits *and* left uncommitted changes, the stash happens first, then the reset.

### Runtime Crash Recovery

When the bot crashes **after** 60 seconds of uptime (a runtime crash, not caused by a bad self-edit), the system restarts the bot and spawns a crash analysis agent:

```
1. bot.py crashes after running for >60s
2. run.sh detects runtime crash (uptime >= CRASH_THRESHOLD)
3. run.sh increments runtime_crash_count (stops after MAX_RUNTIME_CRASHES=3)
4. run.sh snapshots the last 200 lines of .bot_output.log
5. run.sh writes .crash_analysis marker with: exit code, uptime, timestamp, crash log
6. run.sh re-launches bot.py
7. on_ready() reads .crash_analysis, sends "Runtime crash detected" DM
8. on_ready() spawns a "crash-handler" agent in the bot's project directory
9. The crash handler agent analyzes the traceback and creates a fix plan (no auto-apply)
```

### Crash Handler Agent

Both startup crashes (after rollback) and runtime crashes spawn a `crash-handler` agent. The agent:
- Appears in `/list-agents` and can be switched to with `/switch-agent crash-handler`
- Gets the full crash log embedded in its initial prompt
- For startup crashes, also receives rollback context (reverted commits, stashed changes)
- Is instructed to analyze the root cause and produce a plan, **not** to apply fixes automatically
- Is recycled if a previous crash-handler session still exists

### run.sh Decision Tree

```
bot.py exits
  |
  +-- exit code 42? --> restart (reset counters, update pre_launch_commit, loop)
  +-- exit code 0?  --> clean stop, exit supervisor
  +-- uptime >= 60s (runtime crash):
  |    +-- runtime_crash_count >= 3? --> stop (prevent infinite loop)
  |    +-- snapshot last 200 lines of log
  |    +-- write .crash_analysis marker (JSON with crash log)
  |    +-- restart bot
  |
  +-- uptime < 60s (startup crash):
       |
       +-- rollback already attempted? --> stop (prevent infinite loop)
       +-- not in a git repo? --> stop
       +-- no uncommitted changes AND HEAD unchanged? --> stop (nothing to roll back)
       +-- changes exist:
            +-- if uncommitted changes: git stash push --include-untracked
            +-- if HEAD != pre_launch_commit: git reset --hard <pre_launch_commit>
            +-- write .rollback_performed marker (JSON)
            +-- set rollback_attempted=1
            +-- re-launch bot.py
```

### Signal Files

| File | Created by | Purpose |
|---|---|---|
| `.restart_requested` | Axi (bot.py) | Signals the bot to exit with code 42 for a clean restart |
| `.spawn_agent` | Axi (bot.py) | Signals the scheduler to spawn a new agent session |
| `.rollback_performed` | run.sh | Communicates startup crash rollback details to bot.py, including crash log snapshot |
| `.crash_analysis` | run.sh | Communicates runtime crash details to bot.py for crash handler agent |
| `.bot_output.log` | run.sh | Captures bot stdout/stderr via `tee` for crash log snapshots |

All signal and log files are gitignored to prevent accidental commits.

### Default File Creation

On first run, `run.sh` creates default versions of user data files if they don't exist:
- `profile/USER_PROFILE.md` - blank profile template
- `schedules.json` - empty array `[]`
- `schedule_history.json` - empty array `[]`

---

## Discord Integration

### Architecture

Axi communicates interactively through Discord DMs. The bot requests two intents: `dm_messages` and `message_content`. Server message history is queried on-demand via the [Discord Query Tool](#discord-query-tool) rather than passively logged.

### Communication Style

The system prompt instructs Axi to send short progress updates during long-running tasks (e.g., "Reading the file now...", "Found the issue, fixing it") so the user doesn't experience long silences in DMs. Final answers are still thorough and well-formatted.

### Authentication

All interactions are gated by `ALLOWED_USER_IDS`. Unauthorized users are silently ignored for DMs and receive an ephemeral "Not authorized." for slash commands.

### Message Flow

```
User sends message
  |
  +-- Ignore if: bot message or guild message
  +-- DM message?
       +-- Unauthorized user? --> ignore
       +-- Get active session
       |    +-- Not ready? --> "Claude session not ready yet."
       |    +-- Query lock held? --> "Agent is busy."
       |
       +-- Acquire query_lock
       +-- Update last_activity, reset idle state
       +-- Send query to Claude SDK
       +-- Stream response to Discord (respects visibility mode)
       +-- Process any pending .spawn_agent signal
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

## Discord Query Tool

Axi can query Discord server message history on-demand using `discord_query.py`, a standalone CLI tool that calls the Discord REST API. This replaced the earlier passive logging approach — instead of continuously logging server messages, Axi fetches history only when needed.

### Commands

```bash
# List servers the bot is in
python discord_query.py guilds

# List channels in a server
python discord_query.py channels <guild_id>

# Fetch recent messages from a channel
python discord_query.py history <channel_id> [--limit 50] [--before SNOWFLAKE] [--after SNOWFLAKE] [--format text]

# Search messages across a server
python discord_query.py search <guild_id> "search term" [--channel CHANNEL] [--author USERNAME] [--limit 50] [--format text]
```

### How It Works

1. The tool uses the bot's `DISCORD_TOKEN` from `.env` to authenticate with the Discord REST API
2. It fetches message history directly from Discord's servers (no local log file)
3. Search scans recent history (~500 messages per channel) with case-insensitive substring matching
4. Output defaults to JSON but supports `--format text` for human-readable output
5. Rate limiting is handled automatically with retries

### Integration with Axi

Axi's system prompt documents the tool's usage. When asked about server activity, Axi runs `discord_query.py` via bash to fetch and analyze messages. The bot does **not** respond in server channels — it only observes via the API and reports via DMs.

---

## Permissions & Sandboxing

All agents (including the master) are restricted to their working directory for writes and bash execution:

| Layer | What it restricts | Enforced by |
|---|---|---|
| **OS sandbox** | Bash commands — filesystem and network access limited to `cwd` | `sandbox={"enabled": True, "autoAllowBashIfSandboxed": True}` |
| **`can_use_tool` callback** | Edit/Write/MultiEdit/NotebookEdit — file path must be within `cwd` | `make_cwd_permission_callback(cwd)` |
| **Unrestricted** | Read/Grep/Glob — allowed everywhere so agents can explore code for context | No restriction |

Each agent session gets its own callback bound to its `cwd`, so agents spawned in different directories are isolated from each other. Symlink escapes are prevented by resolving paths with `os.path.realpath()`.

## Self-Modification

Axi is explicitly designed to modify its own source code. The system prompt tells it that `~/coding-projects/personal-assistant` is its own codebase. Since the master agent's `cwd` is the bot's project directory, it has write access to its own source files.

This means Axi can:
- Edit `bot.py` to add features or fix bugs
- Modify `schedules.json` to create/edit/remove scheduled events
- Update `profile/USER_PROFILE.md` with learned preferences
- Edit `run.sh` to change supervisor behavior
- Trigger a restart via `touch .restart_requested`

The [rollback system](#restart--rollback-system) exists specifically as a safety net for this capability. If a self-edit introduces a startup crash, the changes are automatically stashed and the bot reverts to the last committed version. If a runtime crash occurs, a [crash analysis agent](#runtime-crash-recovery) is spawned to diagnose the issue.

---

## Voice Interface

Axi can join a Discord voice channel, listen to speech via Deepgram STT, route transcripts to the active agent, and speak responses back using TTS. The voice pipeline runs in the Rust rewrite (`axi-rs/`).

### Architecture

```
Discord Voice Channel
    │ Opus → PCM (Songbird + DAVE E2EE)
    ▼
VoiceReceiveHandler (VoiceTick events)
    │ filter to authorized user's SSRC
    │ downsample 48kHz stereo → 16kHz mono
    ▼
Deepgram Nova-3 (WebSocket streaming STT)
    │ speech_final transcript
    ▼
Agent (queue_and_wake → Claude)
    │ response text
    ▼
TTS Provider (Piper / OpenAI / espeak-ng)
    │ PCM → upsample to 48kHz stereo f32
    ▼
Songbird playback (RawAdapter → Opus → Discord)
```

### Prerequisites

**System packages:**

```bash
sudo apt install libopus-dev
```

**Piper TTS (recommended for natural-sounding local voice):**

```bash
# Download and install the Piper binary
cd /tmp
curl -sL "https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_x86_64.tar.gz" -o piper.tar.gz
tar xzf piper.tar.gz
sudo cp -r piper /usr/local/lib/piper
sudo ln -sf /usr/local/lib/piper/piper /usr/local/bin/piper

# Download the lessac-high voice model (~109MB)
sudo mkdir -p /usr/local/share/piper-voices
sudo curl -sL "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/high/en_US-lessac-high.onnx" \
  -o /usr/local/share/piper-voices/en_US-lessac-high.onnx
sudo curl -sL "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/high/en_US-lessac-high.onnx.json" \
  -o /usr/local/share/piper-voices/en_US-lessac-high.onnx.json

# Verify
echo "Hello world" | LD_LIBRARY_PATH=/usr/local/lib/piper piper \
  --model /usr/local/share/piper-voices/en_US-lessac-high.onnx \
  --output_raw --espeak_data /usr/local/lib/piper/espeak-ng-data 2>/dev/null | wc -c
```

### Environment Variables

| Variable | Required | Description |
|---|---|---|
| `DEEPGRAM_API_KEY` | Yes | Deepgram API key for speech-to-text |
| `PIPER_MODEL` | No | Path to Piper `.onnx` model file (e.g., `/usr/local/share/piper-voices/en_US-lessac-high.onnx`) |
| `OPENAI_API_KEY` | No | OpenAI API key (used for TTS if set; takes priority over Piper) |
| `VOICE_AUTO_JOIN_CHANNEL` | No | Voice channel ID to auto-join on startup (for testing) |
| `VOICE_AUTHORIZED_USER` | No | Discord user ID to transcribe (defaults to `275841062327549953`) |

### TTS Provider Selection

TTS providers are selected in priority order:

1. **OpenAI TTS** — if `OPENAI_API_KEY` is set. Cloud-based, highest quality, costs ~$0.015/1K chars.
2. **Piper TTS** — if `PIPER_MODEL` is set. Local neural TTS, natural-sounding, free. The `en_US-lessac-high` voice is recommended.
3. **espeak-ng** — fallback. Local formant synthesis, robotic but always available.

### Usage

**Slash command:**
```
/voice-join [channel]    Join your current voice channel (or specify one)
/voice-leave             Leave the voice channel
```

**Text command (in any text channel the bot can see):**
```
!voice-join [channel_id]
!voice-leave
```

**Auto-join (for testing):** Set `VOICE_AUTO_JOIN_CHANNEL=<channel_id>` in `.env` and the bot joins on startup.

### How It Works

1. Bot joins the voice channel via Songbird with DAVE E2EE support
2. Incoming audio is decoded, filtered to the authorized user's SSRC, downsampled to 16kHz mono, and streamed to Deepgram via WebSocket
3. When Deepgram signals `speech_final` (end of utterance), the transcript is sent to the active agent with a voice-mode prefix requesting concise spoken responses
4. The agent's text response is synthesized through the configured TTS provider, resampled to 48kHz stereo f32, and played back through Songbird
5. Playback waits for each track to finish before starting the next, with a 250ms gap between utterances

### Key Files

| File | Purpose |
|---|---|
| `axi-rs/crates/axi-voice/src/gateway.rs` | `VoiceSession` lifecycle, TTS queue consumer, transcript loop |
| `axi-rs/crates/axi-voice/src/stt.rs` | `SttProvider` trait + Deepgram Nova-3 WebSocket client |
| `axi-rs/crates/axi-voice/src/tts.rs` | `TtsProvider` trait + OpenAI / Piper / espeak-ng implementations |
| `axi-rs/crates/axi-voice/src/receive.rs` | Songbird event handler — SSRC filtering + audio forwarding to STT |
| `axi-rs/crates/axi-voice/src/playback.rs` | TTS audio → Songbird playback with track-end waiting |
| `axi-rs/crates/axi-voice/src/resample.rs` | Audio format conversions (48k↔16k, 22k→48k, 24k→48k) |
| `axi-rs/vendor/songbird/` | Vendored Songbird 0.5 fork with DAVE E2EE support |

---

## Configuration

### Environment Variables

| Variable | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | Yes | Discord bot token (used by both `bot.py` and `discord_query.py`) |
| `ALLOWED_USER_IDS` | Yes | Comma-separated Discord user IDs authorized to interact |
| `SCHEDULE_TIMEZONE` | No | IANA timezone for cron expressions (e.g., `US/Pacific`). Defaults to `UTC`. Handles DST automatically. |
| `DEFAULT_CWD` | No | Default working directory for agent sessions (defaults to the bot's directory) |
| `DEEPGRAM_API_KEY` | No* | Deepgram API key for voice STT (*required if using voice) |
| `PIPER_MODEL` | No | Path to Piper TTS `.onnx` model file for local neural voice synthesis |
| `OPENAI_API_KEY` | No | OpenAI API key for cloud TTS (takes priority over Piper if set) |
| `VOICE_AUTO_JOIN_CHANNEL` | No | Voice channel ID to auto-join on startup |
| `VOICE_AUTHORIZED_USER` | No | Discord user ID whose speech is transcribed (defaults to bot owner) |

### Constants (in `bot.py`)

| Constant | Value | Description |
|---|---|---|
| `MASTER_AGENT_NAME` | `"axi-master"` | Reserved name for the primary agent |
| `MAX_AWAKE_AGENTS` | `5` | Maximum concurrently awake agents |
| `IDLE_REMINDER_THRESHOLDS` | `[30m, 3h, 48h]` | Escalating idle notification intervals (cumulative) |

### Constants (in `run.sh`)

| Constant | Value | Description |
|---|---|---|
| `RESTART_EXIT_CODE` | `42` | Exit code that signals an intentional restart |
| `CRASH_THRESHOLD` | `60` | Seconds — crashes faster than this trigger rollback; slower trigger runtime recovery |
| `ROLLBACK_MARKER` | `.rollback_performed` | Filename for the startup crash rollback info marker |
| `CRASH_ANALYSIS_MARKER` | `.crash_analysis` | Filename for the runtime crash analysis marker |
| `LOG_FILE` | `.bot_output.log` | Bot stdout/stderr capture file |
| `MAX_RUNTIME_CRASHES` | `3` | Consecutive runtime crashes before the supervisor stops |
