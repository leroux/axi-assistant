# Flowcoder Integration — Behavior Specification

Documented through hands-on testing of the unified flowcoder/claude-code architecture.
Last updated: 2026-03-02.

---

## Architecture Overview

Each agent runs a **single process**: the flowcoder-engine, managed by procmux via BridgeTransport. The engine acts as a transparent proxy to an inner Claude Code CLI process. For normal messages, the engine passes them straight through to Claude. For slash commands matching known flowchart definitions, the engine takes over and orchestrates multi-block execution.

**Key components:**
- **flowcoder-engine**: Subprocess spawned in procmux. Wraps inner Claude CLI with `-p --input-format stream-json --output-format stream-json`.
- **BridgeTransport** (claudewire): SDK Transport interface wrapping procmux. Handles spawn, subscribe, stdin/stdout, and close.
- **ClaudeSDKClient**: Anthropic's official SDK. Accepts a custom `transport` parameter — when provided, uses BridgeTransport instead of spawning its own subprocess.
- **procmux**: Process multiplexer. Manages engine subprocesses, survives bot.py restarts, buffers output.

**Data flow:**
```
Discord message → on_message → wake_agent → _create_sdk_client
  → BridgeTransport.spawn(engine CLI args) → procmux spawns engine
  → SDK sends initialize control_request → engine handles it directly
  → SDK sends user message → engine proxies to inner Claude
  → inner Claude streams response → engine forwards → SDK parses → bot posts to Discord
```

## Agent Types

- **`flowcoder`** (default): Uses the engine. Supports both normal messages (proxied) and flowchart slash commands.
- **`claude_code`**: Uses the SDK's built-in SubprocessCLITransport (no engine, no flowchart support). Fallback when `FLOWCODER_ENABLED=false` or procmux unavailable.

The default agent type is `flowcoder`, set in `AgentSession.agent_type` (axi_types.py). All reconstructed agents from Discord channels default to flowcoder.

## Wake/Sleep Lifecycle

### Wake
1. `wake_agent()` acquires `_wake_lock` and checks concurrency limits.
2. `_create_sdk_client()` branches on agent_type:
   - **flowcoder**: Creates BridgeTransport, spawns engine via procmux, subscribes, creates SDK client with custom transport.
   - **claude_code**: Creates SDK client directly (SDK spawns its own subprocess).
3. SDK sends `initialize` control_request. Engine responds with synthetic success (inner Claude runs in `-p` mode and doesn't support the control protocol).
4. Agent is now awake. Session resumed with existing session_id.

### Sleep
1. `sleep_agent()` calls `disconnect_client()` which calls `BridgeTransport.close()`.
2. Close has a 5-second timeout. Currently, the engine's inner Claude process takes time to shut down, causing a **transport close timeout warning** on every sleep. This is non-critical — the agent goes to sleep correctly.
3. `session.client = None`. Agent is sleeping.

### Auto-sleep
- Agents auto-sleep after 60 seconds of idle time (configurable).
- Checked by the periodic idle agent scanner.

### Re-wake
- On next user message, `wake_agent()` spawns a new engine in procmux, resumes with the saved session_id.
- Session continuity is maintained across sleep/wake cycles.

## Normal Message Flow (Proxy Mode)

1. User sends a message in Discord.
2. `on_message()` → `process_message()` → `stream_with_retry()`.
3. SDK sends `{"type": "user", "message": {"role": "user", "content": "..."}}` to engine stdin.
4. Engine's main loop matches `msg_type == "user"`, checks for slash commands.
5. No slash command → `_proxy_turn()`: forwards message to inner Claude, reads all stdout until result.
6. Engine emits inner Claude's response messages on stdout.
7. SDK reads via BridgeTransport → parsed into stream events → streamed to Discord.

## Flowchart Execution

### Triggering
Flowcharts can be triggered via:
- **`//flowchart /name args`**: Text command in Discord. Handled by `_handle_double_slash_commands()` in main.py.
- **`/flowchart` slash command**: Discord application command with name and args parameters.

Both handlers:
1. Strip leading `/` from the command name (`.lstrip("/")`)
2. Construct `slash_content = f"/{fc_name} {fc_args}"`
3. Wake the agent if sleeping
4. Call `process_message(session, slash_content, channel)`

### Engine-Side Flowchart Processing
1. Engine receives the user message with content starting with `/`.
2. `_parse_slash_command()` extracts the command name.
3. `resolve_command()` looks up the command in `--search-path` directories.
4. If found: **flowchart takeover** — engine runs the flowchart blocks sequentially.
5. If not found: **proxy to Claude** — treated as a normal message.

### Flowchart Events
During flowchart execution, the engine emits structured system messages:
- `flowchart_start`: `{command, args, block_count}` — signals start of flowchart mode
- `block_start`: `{block_id, block_name, block_type}` — each block begins
- Inner Claude stream events (forwarded from per-block queries)
- `block_complete`: `{block_id, block_name, success}` — each block ends
- `flowchart_complete`: `{status, duration_ms, cost_usd, blocks_executed}` — flowchart done
- `result`: Final result with `session_id="flowchart"` and `duration_api_ms=0`

### Session ID Protection
During flowchart execution, inner Claude's stream events carry inner Claude's session_id. Without protection, `_handle_stream_event()` would overwrite the agent's real session_id.

**Solution**: `_StreamCtx.in_flowchart` tracks flowchart mode:
- Set `True` on `flowchart_start` system message
- Set `False` on `flowchart_complete` system message
- `_handle_stream_event()` skips `_set_session_id()` when `in_flowchart=True`

The engine does NOT forward inner Claude result messages during flowchart execution (the Session class consumes them internally). Only the final flowchart result (`session_id="flowchart"`) comes through.

### Flowchart Completion Display
When `flowchart_complete` is received, the bot posts a summary:
```
*System:* Flowchart **completed** in 30s | Cost: $0.3864 | Blocks: 5
```

## Control Protocol

### Initialize Handshake
The SDK sends `control_request(initialize)` on startup. The engine handles this **directly** (not forwarded to inner Claude) because inner Claude runs in `-p` mode and doesn't support the control protocol.

Engine responds with:
```json
{"type": "control_response", "response": {"subtype": "success", "request_id": "...", "response": {}}}
```

### Reconnect Initialize
When an agent reconnects (bridge `reconnecting=True`), BridgeTransport intercepts the initialize request and fakes a success response. The engine process may still be running in procmux from before.

### Other Control Requests
Non-initialize control requests (e.g., from inner Claude during execution) are forwarded to inner Claude via `process.write()` and the response is relayed back.

## Engine Binary Resolution

`get_engine_binary()` resolves the flowcoder-engine binary via `shutil.which("flowcoder-engine")` on PATH. The binary is installed as a package entry point from the vendored `packages/flowcoder_engine/` package.

## CLI Arg Construction

`build_engine_cli_args(options)` mirrors `SubprocessCLITransport._build_command()` from the SDK. It constructs the full command line for the engine binary, including:
- Engine-specific: `--search-path` for flowchart command directories
- Claude passthrough: `--output-format stream-json`, `--verbose`, `--system-prompt`, `--model`, `--fallback-model`, `--permission-mode`, `--resume`, `--disallowedTools`, `--mcp-config`, `--include-partial-messages`, `--max-thinking-tokens`, `--effort`, `--settings`, `--setting-sources`, extra args, `--input-format stream-json`

The engine uses `parse_known_args` to absorb its own flags and pass everything else through to inner Claude.

## Environment

`build_engine_env()` creates a clean environment by stripping `CLAUDECODE`, `CLAUDE_AGENT_SDK_VERSION`, and `CLAUDE_CODE_ENTRYPOINT` from `os.environ`. This prevents inner Claude from detecting it's running under the SDK.

## Configuration

- **`FLOWCODER_ENABLED`** (env var): Must be `1`/`true`/`yes` to enable flowcoder agents. When disabled, flowcoder agents fall back to standard Claude Code behavior (no engine, no flowcharts).

## Available Flowchart Commands

Located in `packages/flowcoder_engine/examples/commands/` (resolved at runtime from the installed package):
- `story.json` — Multi-block creative writing flowchart
- `explain.json` — Explanation flowchart
- `recast.json` — Text recasting flowchart

## Known Issues and Behaviors

### Transport Close Timeout (Non-Critical)
When sleeping an agent, `BridgeTransport.close()` times out after 5 seconds. This happens because the engine's inner Claude process takes time to shut down. The agent still goes to sleep correctly — the warning is cosmetic.

Log: `'axi-master' transport close timed out`

### Engine Package Installation
The engine package installed via `uv pip install -e` gets reverted by `uv run` (which re-syncs from the lock file). Workaround: copy source files directly to site-packages after any `uv run` invocation. This is a development-only issue.

### Flowchart Command Prefix
The `//flowchart` handler expects the command name with or without a leading `/`. It strips the prefix with `.lstrip("/")` to normalize. Users can type either `//flowchart /story` or `//flowchart story`.

### Bridge Connection Required
Flowcoder agents require a procmux bridge connection. If `wire_conn` is not available, `_create_transport()` returns `None` and `_create_sdk_client()` raises `RuntimeError`. The agent cannot wake as a flowcoder type without procmux.

### Message Format
The SDK wraps user messages in a standard envelope:
```json
{"type": "user", "session_id": "", "message": {"role": "user", "content": "..."}, "parent_tool_use_id": null}
```
The engine extracts content from `msg["message"]["content"]` for slash command parsing.

### Concurrent Agents
Awake agent count is tracked by `count_awake_agents()` which counts sessions where `client is not None`. The concurrency limit (`MAX_AWAKE_AGENTS`) applies. When at capacity, the least-idle agent is evicted (put to sleep) to make room.

---

# Multi-Frontend Architecture

Documented during the extraction and abstraction of Discord-specific code into a pluggable frontend system.
Last updated: 2026-03-06.

---

## Overview

Axi supports multiple simultaneous frontends (Discord, web UI, Slack) through a **Frontend Adapter Pattern**. AgentHub (the core orchestrator) emits events through a `FrontendRouter` that multiplexes to all registered frontends. Each frontend renders events in its own way.

```
              AgentHub
                 |
          FrontendCallbacks  (backward compat)
                 |
           FrontendRouter
          (multiplexes to N)
                 |
     +-----------+-----------+
     |           |           |
  Discord    WebFront.    Slack
  Frontend   (future)    (future)
```

**Key principle:** AgentHub and all core packages (`agenthub/`) have zero frontend imports. Frontends are leaf nodes that consume typed events.

---

## Layers

### 1. StreamOutput Types (`agenthub/stream_types.py`)

~20 dataclass types representing normalized streaming events. Frontend-agnostic — no Discord or web concepts.

| Type | Purpose |
|------|---------|
| `TextDelta` | Incremental text from model response |
| `TextFlush` | Accumulated text ready to render (with reason: end_turn, mid_turn_split, etc.) |
| `ThinkingStart` / `ThinkingEnd` | Extended thinking boundaries |
| `ToolUseStart` / `ToolInputDelta` / `ToolUseEnd` | Tool invocation lifecycle |
| `TodoUpdate` | Agent updated its todo list |
| `StreamStart` / `StreamEnd` | Response stream boundaries |
| `QueryResult` | Final turn result (session_id, cost, duration) |
| `RateLimitHit` / `TransientError` | API errors |
| `StreamKilled` | Stream ended without ResultMessage |
| `CompactStart` / `CompactComplete` | Context compaction |
| `FlowchartStart` / `FlowchartEnd` | Flowchart execution boundaries |
| `BlockStart` / `BlockComplete` | Flowchart block boundaries |
| `SystemNotification` | Catch-all for unhandled system messages |

Union type: `StreamOutput = TextDelta | TextFlush | ... | SystemNotification`

### 2. Streaming Engine (`agenthub/streaming.py`)

Async generator that transforms raw SDK messages into `StreamOutput` events:

```python
async for event in stream_response(session):
    # event is a StreamOutput — frontend renders it
```

Handles:
- **Text buffering**: Accumulates text, flushes at end-of-turn or mid-turn split (1800 char threshold)
- **Tool preview extraction**: Extracts human-readable preview from Bash commands, file paths, grep patterns
- **TodoWrite extraction**: Parses TodoWrite tool input into `TodoUpdate` event
- **Thinking lifecycle**: Emits ThinkingStart/End at correct content block boundaries
- **Session ID tracking**: Extracts from StreamEvent and ResultMessage, skips during flowchart mode
- **Error classification**: Rate limits vs transient errors vs stream kills

### 3. AgentLog (`agenthub/agent_log.py`)

Per-agent append-only event log — the shared source of truth for message parity.

```python
log = make_agent_log("master")
log.subscribe(my_callback)
await log.append(make_event("assistant", "master", text="Hello"))
# my_callback fires with the LogEvent
```

Features:
- `LogEvent` dataclass: ts, kind, agent, text, source, data
- Subscriber notifications on append (asyncio callbacks)
- Replay with `since` filter for late-connecting frontends
- Optional JSONL persistence
- Source tracking so frontends can skip self-originated events

### 4. Frontend Protocol (`agenthub/frontend.py`)

`@runtime_checkable` Protocol defining the full frontend interface:

- **Outbound messages**: `post_message`, `post_system`, `broadcast`
- **Lifecycle events**: `on_wake`, `on_sleep`, `on_spawn`, `on_kill`, `on_session_id`, `on_idle_reminder`, `on_reconnect`
- **Stream rendering**: `on_stream_event` (receives `StreamOutput`)
- **Interactive gates**: `request_plan_approval` → `PlanApprovalResult`, `ask_question` → answers dict
- **Channel management**: `ensure_channel`, `move_to_killed`, `get_channel`
- **Todo**: `update_todo`
- **Persistence**: `save_session_metadata`, `reconstruct_sessions`
- **Event log**: `on_log_event`
- **Lifecycle**: `start`, `stop`
- **Shutdown**: `send_goodbye`, `close_app`, `kill_process`

### 5. FrontendRouter (`agenthub/frontend_router.py`)

Multiplexer that broadcasts to all registered frontends:

```python
router = FrontendRouter()
router.add(discord_frontend)
router.add(web_frontend)

# Broadcast — all frontends receive
await router.post_message("master", "hello")

# Race — first response wins (plan approval, questions)
result = await router.request_plan_approval("agent-1", "plan text", session)

# Backward compat — generates FrontendCallbacks for AgentHub
callbacks = router.as_callbacks()
```

Error isolation: if one frontend throws, others still receive the event.

### 6. Discord Extractions (`axi/discord_stream.py`, `axi/discord_ui.py`)

Discord-specific code extracted from `agents.py`:

- **`discord_stream.py`** (981 lines): Live-edit streaming renderer, typing indicators, mid-turn splitting, inline duration timing, compaction handling
- **`discord_ui.py`** (433 lines): Plan approval via reactions, question UI via reactions, todo list formatting

Both use the `init()` dependency injection pattern to receive references to functions in `agents.py` without circular imports. All symbols are re-exported from `agents.py` for backward compatibility.

---

## Data Flow

### Outbound (hub → frontends)
```
AgentHub callback fires
  → FrontendRouter._broadcast(method, *args)
    → discord_frontend.method(*args)   # edits Discord message
    → web_frontend.method(*args)       # pushes WebSocket event
```

### Streaming (SDK → frontends)
```
SDK yields raw messages (StreamEvent, AssistantMessage, ResultMessage, SystemMessage)
  → streaming.stream_response(session) transforms to StreamOutput events
    → Frontend.on_stream_event(agent_name, event)
      → Discord: live-edits message, shows typing, etc.
      → Web: pushes WebSocket JSON event
```

### Interactive gates (frontends → hub)
```
Hub needs plan approval
  → FrontendRouter._first_response("request_plan_approval", ...)
    → asyncio.wait(FIRST_COMPLETED) across all frontends
      → Discord: posts plan, waits for reaction
      → Web: shows modal, waits for button click
    → First response wins, losers cancelled
    → Returns PlanApprovalResult to hub
```

### Inbound (user → hub)
```
User types in Discord/web/Slack
  → Frontend-specific message handler (on_message, WebSocket, etc.)
    → hub.receive_user_message(agent_name, content)
      → Wakes agent if sleeping
      → Queues if busy
      → Streams response via stream_response()
```

---

## Test Coverage

| Test file | Tests | What's covered |
|-----------|-------|----------------|
| `test_agent_log.py` | 15 | Append, subscribe, replay, persistence, source tracking |
| `test_stream_types.py` | 19 | All StreamOutput dataclass construction and fields |
| `test_streaming_engine.py` | 21 | SDK message → StreamOutput transformation, text buffering, tool previews, errors |
| `test_frontend_router.py` | 12 | Broadcast, error isolation, racing, as_callbacks(), lifecycle |

All tests are unit tests with mocked dependencies — no live bot or Discord needed.

---

## Migration Status

| Phase | Status | Description |
|-------|--------|-------------|
| 0a: Extract discord_stream.py | Done | Streaming renderer extracted from agents.py |
| 0b: Extract discord_ui.py | Done | Interactive UI handlers extracted from agents.py |
| 1a: AgentLog | Done | Per-agent event log with subscribers |
| 1b: StreamOutput types | Done | 20 normalized event dataclasses |
| 1c: Streaming engine | Done | Frontend-agnostic SDK message transformer |
| 1.5: Test harness | Done | 67 unit tests across 4 test files |
| 2a: Frontend protocol | Done | Protocol class with full interface |
| 2b: FrontendRouter | Done | Multiplexer with broadcast + racing |
| 2c: DiscordFrontend class | Pending | Wrap Discord code into Frontend impl |
| 2d: Wire hub_wiring.py | Pending | Replace direct callbacks with router |
| 3: Web MVP | Pending | WebSocket server + WebFrontend |
| 4: Web features | Pending | Plan approval, questions, todos in web |
| 5: Hub ingestion | Pending | hub.receive_user_message() |
| 6: Clean up | Pending | Parameterize "Discord" references |
