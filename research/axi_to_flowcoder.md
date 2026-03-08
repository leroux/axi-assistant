# Flowcoder Integration Plan

## Context

Connect the flowchart execution engine to axi so users and agents can invoke flowcharts within a Claude session. Key requirements:

1. **Session continuity** — flowcharts run in the same Claude process, preserving conversation context
2. **Persistence** — engine survives axi crashes, managed by procmux with reconnection
3. **Two invocation modes** — user-invoked (`/flowchart <name> <args>`) and agent-invoked (MCP tool)
4. **Multi-frontend ready** — architecture supports adding Discord, web, TUI frontends without redesign
5. **Multi-agent future** — translation layer to support Codex, OpenCode, custom LLM agents
6. **Control** — pause (between blocks), interrupt (mid-block SIGINT), cancel (abort flowchart)

## Architecture

```
                    ┌────────────┐
                    │  axi bot   │ (or TUI, or web backend)
                    │            │
                    │ stream_response() reads events,
                    │ dispatches to Discord/TUI/web
                    └─────┬──────┘
                          │ procmux (persistence, reconnect, buffered replay)
                    ┌─────┴──────┐
                    │ flowcoder  │ headless proxy binary
                    │  -engine   │ transparent proxy + flowchart interception
                    └─────┬──────┘
                          │ stdin/stdout pipes
                    ┌─────┴──────┐
                    │ claude CLI │ inner subprocess (one continuous session)
                    └────────────┘
```

**Unified protocol — engine as bidirectional adapter.** Every engine binary (flowcoder-engine, future codex-engine) speaks the same stdin/stdout protocol regardless of the inner agent backend. The bot always writes the same message format and reads the same event format. The engine translates internally in both directions:

- **Write path** (bot → engine → agent): Bot sends claudewire-style JSON on stdin. Engine translates to backend-specific format (Claude CLI stream-json, future OpenAI format, etc.)
- **Read path** (agent → engine → bot): Agent emits backend-specific events. Engine translates to claudewire-style JSON + engine-specific events on stdout.

This means axi never branches on agent type for message formatting. Adding a new backend (Codex, OpenCode) means writing a new engine binary, not modifying the bot.

**No separate event layer crate.** Each consumer (axi, TUI, web) has its own stream handler that reads engine stdout and renders appropriately. This follows the existing `StreamHandlerFn` pattern — a boxed async closure that owns mutable render state. A sync EventHandler trait can't work because Discord ops are async and streaming requires mutable state across awaits.

## Crate Structure

### New: `axi-rs/crates/flowcoder-engine/`
Headless proxy binary. Wraps Claude CLI, intercepts flowchart commands.

```
flowcoder-engine/
  Cargo.toml
  src/
    main.rs             — CLI args, spawn inner Claude, main loop
    proxy.rs            — Transparent proxy mode (forward client ↔ inner Claude)
    router.rs           — Stdin message router (2 channels)
    engine_session.rs   — Session trait impl wrapping CliSession
    engine_protocol.rs  — Protocol trait impl emitting JSON events to stdout
    control.rs          — Background task: reads engine_control, sets pause/cancel flags
    events.rs           — Serde event type definitions for stdout JSON protocol
```

Dependencies: `flowchart`, `flowchart-runner`, `claudewire`, `tokio`, `tokio-util`, `clap`, `serde_json`, `nix`

### Modified: `axi-rs/crates/axi/src/`
- `flowcoder.rs` — rewrite: engine binary resolution, search paths, CLI arg building for Rust engine
- `claude_process.rs:stream_response()` — add match arms for engine-specific events (flowchart_start, block_start, etc.)
- `mcp_tools.rs` — add `run_flowchart` tool for agent-invoked flowcharts
- `state.rs` — add `pending_flowchart` field to `AgentSession`
- `messaging.rs` — check `pending_flowchart` after stream_response returns, inject into message queue

### Unchanged
- `flowchart` — pure data layer (parse, validate, walk)
- `flowchart-runner` — execution engine (Session + Protocol traits, run_flowchart)
- `flowcoder` — standalone TUI binary (keep as-is, separate use case)
- `procmux` — process multiplexer
- `claudewire` — wire protocol types and CliSession

## Key Components

### 1. Message Router (`router.rs`)

Spawns a background task reading stdin. Classifies each JSON message into one of two channels:

| Message type | Channel | Purpose |
|---|---|---|
| `control_response` | `control_response_tx` | Must reach inner Claude immediately during EngineSession::query() |
| Everything else | `message_tx` | Main loop: user messages + engine_control commands |

Why 2 channels: During `EngineSession::query()`, the engine reads inner Claude's stdout. Inner Claude may emit a `control_request` (permission prompt) that the engine relays to its own stdout. The outer client sends a `control_response` on stdin. Without a separate channel, control_responses pile up in the message queue and never reach inner Claude → deadlock.

### 2. Engine Session (`engine_session.rs`)

Implements `flowchart_runner::Session` around `claudewire::CliSession`.

**Ownership model**: Sequential exclusive access (not shared). The main loop alternates between proxy mode and flowchart mode — never both. `&mut CliSession` is passed down the call chain. No Arc<Mutex<>> needed. Same pattern as the existing `flowcoder` TUI REPL.

- `query()` — sends user message to inner Claude, reads stdout until `result`. Forwards `stream_event`/`assistant` to Protocol. On `control_request`: writes to engine stdout, reads from `control_response_rx`, forwards to inner Claude. Uses `tokio::select!` with CancellationToken to support mid-block cancel.
- `clear()` — kills inner Claude, respawns with same config. Cost survives. Session_id is intentionally lost (REFRESH blocks reset context).
- `interrupt()` — SIGINT to inner Claude process group. Aborts current API call.
- `stop()` — no-op. Process persists across flowcharts for session continuity.

Reference: `flowcoder/src/claude_session.rs` — mirror this, but relay control_requests to stdout instead of callback.

### 3. Engine Event Protocol (`events.rs`)

Stdout JSON events emitted by the engine (superset of claudewire):

| Event type | Fields | When |
|---|---|---|
| `flowchart_start` | command, args, block_count | Flowchart execution begins |
| `block_start` | block_id, block_name, block_type, block_index, total_blocks | Block begins |
| `block_complete` | block_id, block_name, success, duration_ms | Block ends |
| `forwarded` | message (claudewire JSON), block_id, block_name | Claude stream event during a block |
| `flowchart_complete` | status, duration_ms, blocks_executed, cost_usd, variables | Flowchart ends |
| `engine_status` | mode, current_block, blocks_done, total_blocks, paused | Response to status query |
| `engine_log` | message | Diagnostic |

In proxy mode: inner Claude's stdout passes through unchanged (standard claudewire).

### 4. Control System (`control.rs`)

**Two interrupt paths — both forward to inner Claude:**

1. **SIGINT from procmux**: axi calls `conn.interrupt("agent")` → procmux sends SIGINT to engine process. Engine traps SIGINT (does NOT die), forwards SIGINT to inner Claude's process group. Claude aborts current API call, emits partial result. Engine stays alive.

2. **engine_control:interrupt on stdin**: Same effect — engine sends SIGINT to inner Claude. Used when procmux interrupt isn't available (e.g., TUI direct connection).

The engine only terminates on SIGTERM (graceful shutdown) or explicit kill. SIGINT is always forwarded.

**Control during flowchart execution**: `run_flowchart()` blocks the main loop. A background task spawned at flowchart start reads from `message_tx` and:
- On `engine_control:pause` → sets `pause_flag` (AtomicBool)
- On `engine_control:resume` → clears `pause_flag`, signals `pause_notify` (tokio::sync::Notify)
- On `engine_control:cancel` → calls `cancel_token.cancel()` + forwards SIGINT to Claude if mid-block
- On `engine_control:interrupt` → forwards SIGINT to inner Claude (block aborts, flowchart may continue)
- On `engine_control:status` → emits `engine_status` event to stdout
- Other messages → buffer for after flowchart completes

The executor checks `pause_flag` and `cancel_token` between every block (`executor.rs:115-133`). For mid-block cancel: `EngineSession::query()` uses `tokio::select!` to race the cancel token against the Claude stdout read loop.

Stdin format: `{"type": "engine_control", "command": "pause"}`

| Command | Mode | Effect |
|---|---|---|
| `pause` | Flowchart only | Sets `pause_flag`, execution pauses before next block |
| `resume` | Flowchart only | Clears `pause_flag`, signals `pause_notify` |
| `cancel` | Flowchart only | Cancels token + interrupt if mid-block. Flowchart aborts. |
| `interrupt` | Any mode | SIGINT to inner Claude. Aborts current API call. Engine stays alive. |
| `status` | Any mode | Emits `engine_status` event |
| SIGINT (signal) | Any mode | Trapped by engine, forwarded to inner Claude. Engine does NOT die. |

### 5. Unified Stdin Protocol (Write Path)

The bot writes these message types to the engine's stdin (via procmux). Same format regardless of backend:

| Message type | Format | Purpose |
|---|---|---|
| User message | `{"type": "user", "message": {"role": "user", "content": "..."}}` | Normal chat, flowchart invocation (`/name args`) |
| Control response | `{"type": "control_response", "response": {...}}` | Permission grant/deny (relayed to inner agent) |
| Engine control | `{"type": "engine_control", "command": "pause"}` | Pause/resume/cancel/status |

The engine translates user messages to the backend-specific format:
- flowcoder-engine → claudewire stream-json (passthrough, already compatible)
- Future codex-engine → OpenAI API format (engine handles translation)

This means `send_query()` in axi never needs to branch on agent type. It always writes the same claudewire-style JSON. The engine is the adapter.

### 6. Main Loop States

**Proxy mode**: Read from `message_tx`. If user message content starts with `/` and matches a known flowchart command (resolved dynamically from search paths), switch to flowchart mode. Otherwise forward to inner Claude, read stdout until `result`, emit to stdout.

**Flowchart mode**:
1. Resolve command from search paths
2. Spawn background control reader task (reads engine_control from message_tx)
3. Call `run_flowchart()` with `EngineSession` + `EngineProtocol`
4. On completion: emit `flowchart_complete`, cancel control reader, drain buffered messages
5. Return to proxy mode

### 7. Agent-Invoked Flowcharts

1. Axi registers `run_flowchart` MCP tool on flowcoder-type agents
2. Agent calls `run_flowchart(command: "story", args: "dragons")`
3. Tool handler sets `session.pending_flowchart = Some(("story", "dragons"))`
4. Returns `{"status": "queued", "message": "Will run after this turn"}`
5. After `stream_response()` returns (result event), check `pending_flowchart` flag
6. If set: push synthetic message `"/story dragons"` to front of `session.message_queue`
7. `process_message_queue()` picks it up, sends to engine as user message
8. Engine intercepts as flowchart command, executes, emits events through a new `stream_response` cycle

Injection point: `events.rs:235` (after process_message, before process_message_queue) or inside process_message_queue itself.

## Build Order

### Phase 1: flowcoder-engine binary (standalone, testable from terminal)

1. Create crate: `Cargo.toml`, workspace member
2. `events.rs` — serde event type definitions
3. `router.rs` — stdin message router (2 channels: control_response + everything_else)
4. `engine_session.rs` — Session trait wrapping CliSession. Mirror `flowcoder/src/claude_session.rs`, relay control_requests to stdout, read responses from control_response_rx
5. `engine_protocol.rs` — Protocol trait emitting JSON events to stdout
6. `control.rs` — background task reading engine_control commands, setting pause/cancel flags
7. `proxy.rs` — transparent proxy mode (forward user↔claude, relay control_requests)
8. `main.rs` — CLI args (clap), spawn inner Claude via CliSession, main loop with proxy/flowchart switching

**Verification**: `flowcoder-engine --model sonnet` from terminal → chat → `/story dragons` → flowchart runs → chat continues with same context.

### Phase 2: E2E tests

1. Minimal test flowchart fixture (1 PROMPT block asking Claude to repeat context)
2. Session continuity test: chat → give secret → flowchart → ask for secret → verify recall
3. Control test: multi-block flowchart → pause → resume → cancel
4. Control_request relay test: flowchart with tool use that triggers permission prompt

### Phase 3: Axi integration

5. Rewrite `axi/src/flowcoder.rs` — resolve Rust `flowcoder-engine` binary, build CLI args
6. Add engine event match arms to `stream_response()` (flowchart_start, block_start, block_complete, forwarded, flowchart_complete)
7. Add `/flowchart` user command, `// pause`, `// resume`, `// cancel` control commands
8. Reconnection: existing procmux replay handles buffered events

### Phase 4: Agent-invoked flowcharts

9. `run_flowchart` MCP tool in `mcp_tools.rs`
10. `pending_flowchart: Option<(String, String)>` in `AgentSession`
11. Injection hook after stream_response in messaging flow

### Phase 5: Future (not in this plan)
- Web frontend stream handler
- TUI connecting via procmux subscription
- Codex/OpenCode session implementations

## Design Review

### Review Checklist
1. **Premature abstraction?** — ~~agent-events crate~~ **DROPPED.** Sync EventHandler trait can't work (Discord ops are async, StreamContext needs mutable state across awaits). The existing StreamHandlerFn pattern is sufficient.
2. **EventHandler async gap** — **N/A** (dropped). Each consumer writes its own stream handler with its own render state.
3. **EventTranslator vs stream_response()** — **N/A** (dropped). Translation stays in stream_response() as match arms.
4. **CliSession ownership** — **Sequential exclusive access.** Proxy mode and flowchart mode never overlap. &mut passed down call chain. Same as existing flowcoder REPL. No Arc/Mutex.
5. **Control during flowchart** — **Background task** reads engine_control from message_tx, sets pause/cancel flags. Executor checks between blocks. Mid-block cancel via tokio::select! in EngineSession::query().
6. **Proxy mode control_response routing** — In proxy mode, control_responses from stdin are forwarded directly to inner Claude via the main loop (no separate channel needed). The 2-channel split is only required during flowchart mode when EngineSession::query() owns the Claude read loop.
7. **pending_flowchart timing** — After stream_response returns, before process_message_queue. Push to front of message_queue. Natural flow.
8. **Command discovery** — Dynamic per-invocation resolution from search paths. No startup scan needed for execution. Axi's list_flowchart_commands() is for discovery UI only.
9. **Stdout flushing** — Use `println!` (auto-flushes on newline) or explicit `io::stdout().flush()` after each event. Critical for procmux latency.
10. **REFRESH/clear()** — Kill + respawn is correct. Session_id is intentionally lost. Cost survives. This is the intended REFRESH block semantics.
11. **Write path (bot → agent)** — Unified stdin protocol. Engine binary is the bidirectional adapter. Bot always writes claudewire-style JSON. Engine translates to backend format internally. No agent_type branching in bot code. Adding new backends = new engine binary, not bot changes.

## Critical Files

| File | Role |
|---|---|
| `flowcoder/src/claude_session.rs` | Reference Session impl to mirror |
| `flowcoder/src/repl.rs` | Reference for proxy/flowchart mode switching |
| `flowchart-runner/src/executor.rs` | `run_flowchart()`, cancel/pause checks at L115-133 |
| `flowchart-runner/src/session.rs` | Session trait definition |
| `flowchart-runner/src/protocol.rs` | Protocol trait definition |
| `claudewire/src/session.rs` | `CliSession` — inner Claude subprocess, &mut self methods |
| `axi/src/claude_process.rs` | stream_response() event dispatch (add engine event arms) |
| `axi/src/messaging.rs` | StreamHandlerFn, process_message_queue (injection point) |
| `axi/src/flowcoder.rs` | Rewrite: binary resolution for Rust engine |
| `axi/src/mcp_tools.rs` | Add run_flowchart tool |
| `axi/src/state.rs` | Add pending_flowchart to AgentSession |

## Verification

### Session continuity test
1. Start engine, send: "Remember this secret: BANANA-42"
2. Run `/test-echo` (1-block flowchart asking Claude to repeat context)
3. Send: "What was the secret?"
4. Assert response contains "BANANA-42"

### Control test
1. Start engine, run multi-block flowchart
2. Send `pause` → verify execution stops between blocks
3. Send `resume` → verify execution continues
4. Send `cancel` → verify flowchart aborts cleanly

### Reconnection test
1. Start engine via procmux, start flowchart
2. Kill axi (simulate crash)
3. Restart axi, reconnect to procmux
4. Verify buffered events replay, rendering resumes
