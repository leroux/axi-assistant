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

`get_engine_binary()` resolves the flowcoder-engine binary:
1. First checks `shutil.which("flowcoder-engine")` on PATH
2. Falls back to `$FLOWCODER_HOME/packages/flowcoder-engine/.venv/bin/flowcoder-engine`

**Important**: The engine binary uses the Python interpreter from its venv. If the engine package is installed via `uv pip install`, the `uv run` command (used by `axi_test.py`) can re-sync packages and **overwrite editable installs** with the lock file version. Direct file copies (`cp`) to site-packages survive this.

## CLI Arg Construction

`build_engine_cli_args(options)` mirrors `SubprocessCLITransport._build_command()` from the SDK. It constructs the full command line for the engine binary, including:
- Engine-specific: `--search-path` for flowchart command directories
- Claude passthrough: `--output-format stream-json`, `--verbose`, `--system-prompt`, `--model`, `--fallback-model`, `--permission-mode`, `--resume`, `--disallowedTools`, `--mcp-config`, `--include-partial-messages`, `--max-thinking-tokens`, `--effort`, `--settings`, `--setting-sources`, extra args, `--input-format stream-json`

The engine uses `parse_known_args` to absorb its own flags and pass everything else through to inner Claude.

## Environment

`build_engine_env()` creates a clean environment by stripping `CLAUDECODE`, `CLAUDE_AGENT_SDK_VERSION`, and `CLAUDE_CODE_ENTRYPOINT` from `os.environ`. This prevents inner Claude from detecting it's running under the SDK.

## Configuration

- **`FLOWCODER_ENABLED`** (env var): Must be `1`/`true`/`yes` to enable flowcoder agents. When disabled, flowcoder agents fall back to standard Claude Code behavior (no engine, no flowcharts).
- **`FLOWCODER_HOME`** (env var): Root of the flowcoder-rewrite repo. Defaults to `~/flowcoder-rewrite`.

## Available Flowchart Commands

Located in `$FLOWCODER_HOME/examples/commands/`:
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
