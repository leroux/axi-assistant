# Axi Rust Rewrite Plan

Full rewrite of Axi from Python to Rust. Cargo workspace with multiple crates
mirroring the existing package structure. Test each phase with a test instance.

## Architecture

```
axi-rs/                          (Cargo workspace root)
├── crates/
│   ├── procmux/                 Process multiplexer (standalone binary + library)
│   ├── claudewire/              Claude CLI stream-json protocol (library)
│   ├── axi-hub/                 Multi-agent orchestrator (library)
│   ├── axi-bot/                 Discord bot (binary)
│   ├── axi-supervisor/          Process supervisor (binary)
│   └── discordquery/            Discord message history CLI (binary)
├── Cargo.toml                   Workspace manifest
└── RUST_REWRITE_PLAN.md         This file
```

## Key Crate Dependencies

| Crate | Key deps |
|-------|----------|
| procmux | tokio, serde, serde_json, nix, tracing |
| claudewire | tokio, serde, serde_json, tracing |
| axi-hub | tokio, claudewire, procmux, tracing |
| axi-bot | tokio, serenity, reqwest, axi-hub, claudewire, procmux, chrono, tracing |
| axi-supervisor | tokio, nix, tracing |
| discordquery | tokio, reqwest, serde_json, clap |

## Phases

### Phase 1: Procmux [x]
**Goal**: Standalone Rust process multiplexer, drop-in replacement for Python procmux.

Tasks:
- [x] Set up Cargo workspace with procmux crate
- [x] Wire protocol types (CmdMsg, StdinMsg, ResultMsg, StdoutMsg, StderrMsg, ExitMsg)
- [x] ProcmuxServer: Unix socket listener, single-client model
- [x] Process spawning with start_new_session, stdin/stdout/stderr relay
- [x] Output buffering when client is disconnected
- [x] Subscribe/unsubscribe with buffer replay
- [x] Process kill with SIGTERM→SIGKILL escalation via process groups
- [x] Interrupt command (SIGINT to process group)
- [x] List and status commands
- [x] Per-process stdio logging (rotating files)
- [x] Signal handling (SIGTERM/SIGINT → graceful shutdown)
- [x] ProcmuxClient: connect, demux loop, command/stdin sending
- [ ] Test: run procmux-rs, connect Python bot to it via Unix socket

### Phase 2: Claudewire [x]
**Goal**: Rust library implementing the Claude CLI stream-json protocol.

Tasks:
- [x] Protocol types: all inbound/outbound message types (serde structs)
- [x] Stream event types: MessageStart, ContentBlockDelta, etc.
- [x] Content block types: TextBlock, ToolUseBlock, ThinkingBlock
- [x] Delta types: TextDelta, InputJsonDelta, ThinkingDelta, SignatureDelta
- [x] Control protocol: ControlRequest, ControlResponse
- [x] Rate limit event parsing
- [x] Schema validation (serde does this at deserialize time)
- [x] BridgeTransport: write, read_messages, stop, close
- [x] ActivityState tracking from stream events
- [x] Bare stream event deduplication
- [x] Unit tests for serde round-trips and event parsing
- [x] Initialize interception for reconnecting agents
- [x] OTel tracing export (OTLP/gRPC, conditional on OTEL_ENDPOINT env var)
- [N/A] DirectProcessConnection: PTY subprocess management — not needed; procmux replaces this entirely
- [N/A] OTel trace context injection into CLI processes — Claude CLI doesn't support receiving trace context

### Phase 3: Supervisor [x]
**Goal**: Rust process supervisor replacing supervisor.py.

Tasks:
- [x] Signal handling: SIGTERM/SIGINT (full stop), SIGHUP (hot restart)
- [x] Bot process spawning and monitoring
- [x] Bridge (procmux) process spawning and monitoring
- [x] Crash detection with threshold and max runtime crashes
- [x] Optional git rollback on crash (ENABLE_ROLLBACK)
- [x] Exit code 42 = restart, other = crash
- [x] Log capture and ANSI stripping
- [x] ~~Crash analysis marker file writing~~ (removed — crash handler deleted)

### Phase 4: Config & Tracing [x]
**Goal**: Centralized configuration and OpenTelemetry setup.

Tasks:
- [x] Environment variable loading (dotenvy)
- [x] All config constants (paths, timeouts, feature flags)
- [x] Discord REST client (reqwest-based)
- [x] MCP server config loading
- [x] Model management (get/set current model)
- [x] OpenTelemetry tracing setup (tracing + tracing-subscriber with env-filter)

### Phase 5: AgentHub [x]
**Goal**: Multi-agent session orchestrator library.

Tasks:
- [x] AgentSession struct (name, client, cwd, queue, activity, etc.)
- [x] FrontendCallbacks trait (post_message, on_wake, on_sleep, etc.)
- [x] Scheduler: slot management, eviction (protected/interactive/background)
- [x] Lifecycle: wake_agent, sleep_agent, wake_or_queue
- [x] Registry: session dict, spawn, kill, end_session
- [x] Messaging: process_message, stream_with_retry, message queue drain
- [x] Inter-agent messaging
- [x] Rate limit tracking (per-session usage, quota management)
- [x] ShutdownCoordinator: graceful (wait for busy agents) + force
- [x] ReconnectManager: procmux reconnect, buffer replay, session reconstruction
- [x] Background task management (fire-and-forget with cleanup)

### Phase 6: Discord Bot [x]
**Goal**: Full Discord bot replacing main.py, agents.py, channels.py.

Tasks:
- [x] Serenity bot setup with intents
- [x] on_ready: channel reconstruction, master agent setup
- [x] on_message: message routing to agents, bot filtering
- [x] Channel management: create/move/archive agent channels, categories
- [x] Slash commands: /spawn, /kill, /list-agents, /stop, /skip, /restart, /model, /debug, /send
- [x] Message content extraction (text, attachments, embeds)
- [x] Response streaming to Discord (live-edit messages)
- [x] Reaction-based interactions (plan approval, user questions)
- [x] Todo list rendering and tracking
- [x] Channel topic updates with session state
- [x] Idle agent reminders
- [x] System prompt generation (SOUL.md, dev_context.md, packs)
- [x] Channel status prefixes

### Phase 7: MCP Tools [x]
**Goal**: MCP server implementations for agent tooling.

Tasks:
- [x] MCP server framework integration (rmcp or manual JSON-RPC)
- [x] Master tools: axi_spawn_agent, axi_kill_agent, axi_send_message, axi_restart
- [x] Discord tools: discord_send_message, discord_read_messages, discord_list_channels, discord_send_file
- [x] Schedule tools: schedule_create, schedule_list, schedule_delete
- [x] Utility tools: get_date_and_time, set_agent_status, clear_agent_status, discord_send_file
- [x] Permission callback for cwd-based tool access control

### Phase 8: Scheduler & Advanced Features [x]
**Goal**: Cron scheduler, flowcoder integration, remaining features.

Tasks:
- [x] Cron-based event scheduler (recurring + one-off)
- [x] Schedule persistence (JSON file)
- [DEFERRED] Flowcoder agent type support — will be ported separately after Rust bot is in production
- [ ] Test instance system (axi_test.py equivalent or keep Python version)
- [x] ~~Crash handler and analysis~~ (removed — crash handler deleted)
- [ ] Hot restart end-to-end validation

### Phase 9: Integration Testing & Deployment [x]
**Goal**: Full system test, service files, deployment.

Tasks:
- [x] Build release binary
- [x] Update systemd service files for Rust binary
- [ ] End-to-end test with test instance (full bot lifecycle)
- [ ] Hot restart test (SIGHUP → reconnect → verify agents survive)
- [ ] Multi-agent stress test
- [ ] Performance comparison (CPU, memory, startup time)
- [x] Migration plan for production cutover

### Phase 10: Hub Wiring [x]
**Goal**: Connect all modules — event handlers route to AgentHub, slash commands talk to sessions.

Tasks:
- [x] BotState holds AgentHub, bidirectional channel-agent mapping, guild infrastructure
- [x] DiscordFrontend implements FrontendCallbacks (messages, status, lifecycle)
- [x] Discord REST: edit_channel_name, edit_channel_topic, edit_channel_category
- [x] handle_message routes to agents via hub (wake-or-queue, background task, timeout)
- [x] handle_reaction_add routes plan approval / rejection
- [x] Slash commands wired: list-agents, status, kill-agent, stop, skip, reset-context, send, restart
- [x] on_ready startup: guild infra, channel reconstruction, hub init, master agent, crash check, scheduler
- [x] Scheduler loop connected to hub (fired schedules wake agents)
- [x] Stream handler (bridge → live-edit rendering) — reads claudewire events, renders via live-edit to Discord
- [x] Client factories (create/disconnect/send) — wired to procmux bridge via transport manager

### Phase 11: Feature Parity [ ]
**Goal**: Close every gap between the Python and Rust implementations.

Phases 1-10 built the structural skeleton. A comprehensive Python↔Rust comparison revealed many features are TODO stubs, unwired, or entirely missing. This phase covers closing every gap.

#### A. MCP Tool Stubs (tools.rs — all return fake success)

| Tool | Status | What's needed |
|------|--------|---------------|
| `axi_spawn_agent` | TODO stub | Delegate to hub: validate CWD, build prompt, register session, create channel, run initial prompt |
| `axi_kill_agent` | TODO stub | Delegate to hub: `end_session`, move channel to Killed |
| `axi_restart` | TODO stub | Trigger `ShutdownCoordinator` graceful restart (exit code 42) |
| `axi_restart_agent` | TODO stub | Disconnect + rebuild session with fresh prompt |
| `axi_send_message` | TODO stub | Delegate to `deliver_inter_agent_message` |
| `set_agent_status` | TODO stub | Call `channels::set_channel_status` for the agent |
| `clear_agent_status` | TODO stub | Reset channel name to normalized (no prefix) |

Fix: MCP tool handlers need `Arc<BotState>` captured in closures. Currently `create_master_server` only takes `Arc<Config>`.

#### B. Stream Event Handling Gaps (bridge.rs `stream_response`)

| Feature | Python location | Rust status |
|---------|----------------|-------------|
| Thinking indicator (show/hide temp message) | `_show_thinking` / `_hide_thinking` | Missing |
| Typing indicator management | `channel.typing()` context manager | Missing |
| TodoWrite display during stream | `_handle_stream_event` detects TodoWrite, posts list | Missing |
| Clean tool messages (temp progress) | `_show_tool_progress` / `_delete_tool_progress_messages` | Missing |
| Response timing inline | Appends `-# 3.2s` to last message | Missing |
| Debug mode (thinking as file attachment) | `discord_state.debug` flag → file upload | Missing |
| System messages (compacting, compact_boundary) | `_handle_system_message` | Missing |
| AssistantMessage error handling (rate_limit, billing) | `_handle_assistant_message` | Missing |

#### C. AskUserQuestion Handling

Python: Permission callback intercepts `AskUserQuestion` tool call → posts formatted questions to Discord → adds reaction emojis (1️⃣-4️⃣) → waits for user reaction or text reply → resolves answer.

Rust: **Entirely missing**. No permission callback, no question gate, no reaction answer handling.

#### D. ExitPlanMode Handling

Python: Permission callback intercepts `ExitPlanMode` → posts plan content → adds ✅/❌ reactions → waits for approval.

Rust: **Partially implemented** — reaction handler sends text but no gate mechanism.

#### E. Image Attachment Support

Python: Downloads image attachments, base64-encodes them, creates `ImageBlockParam` content blocks.

Rust: Only adds text description `[Attachment: filename (size, type)]`. No image download or base64 encoding.

#### F. Unwired Slash Commands

| Command | Registered | Wired |
|---------|-----------|-------|
| `/debug` | Yes | No |
| `/plan` | Yes | No |
| `/compact` | Yes | No |
| `/clear` | Yes | No |
| `/claude-usage` | Yes | No |
| `/restart-agent` | Yes | No |

#### G. Text Commands (// prefix)

Python handles `// <command>`: debug, status, todo, clear, compact, flowchart, stop.

Rust: **No text command parsing at all**.

#### H. Channel Lifecycle

| Feature | Python | Rust |
|---------|--------|------|
| Channel recency reordering (debounced) | `mark_channel_active` with rate limiting | Missing |
| Master channel position enforcement | Pinned to top of Axi category | Missing |
| Auto-sleep idle agents (1-min threshold) | Idle check in message handler | Missing |
| Recover stranded messages | On startup, check for unprocessed | Missing |
| Channel creation/deletion listeners | `on_guild_channel_create/delete` | Missing |

#### I. Auto-Compact

Python: After each response, checks `context_tokens / context_window > COMPACT_THRESHOLD` → sends `/compact` → auto-resumes.

Rust: **Missing entirely**. No context token tracking, no compact trigger.

#### J. Graceful Interrupt

Python: `graceful_interrupt` sends `control_request.interrupt` via SDK — CLI aborts API call but stays alive.

Rust: `interrupt_session` does `conn.kill()` which kills the process entirely.

#### K. Agent Config Persistence

Python: `_save_agent_config` / `_load_agent_config` persist MCP server names and packs to `agents/<name>/agent_config.json`.

Rust: Missing.

#### L. Miscellaneous

- Model warning on non-opus model
- System prompt posting to Discord on first wake
- Prompt change detection on resume
- Message queue backpressure (⏳→📨 reaction flow)
- Auto-worktree creation for agents spawned with bot_dir CWD
- Reclaim agent name on respawn
- SHOW_AWAITING_INPUT feature
- Stderr callback/drain (autocompact regex)
- Context tokens tracking from stderr

#### Not Porting

- **DirectProcessConnection / PTY** — Procmux replaces this entirely
- **Per-agent rotating file logs** — Using centralized tracing
- **Crash handler** — Removed per user request

#### Deferred

- **Flowcoder agent type** — Will be ported separately after Rust bot is in production

## Testing Strategy

Each phase produces testable artifacts:
- **Phase 1**: Run procmux-rs binary, connect existing Python bot → verify agents work
- **Phase 2**: Unit tests for protocol parsing + transport
- **Phase 3**: Run supervisor-rs managing Python bot.py → verify restart/crash handling
- **Phase 4-5**: Unit tests for hub logic
- **Phase 6+**: Full test instance with Discord interaction
- **Phase 9**: Production validation

## Decision Log

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Discord library | serenity | Batteries-included, single-guild bot doesn't need twilight's modularity |
| Async runtime | tokio | Industry standard, required by serenity/reqwest |
| Serialization | serde + serde_json | Zero-cost, compile-time validation replaces runtime pydantic |
| Process management | nix + tokio::process | Full Unix API access |
| MCP protocol | Manual JSON-RPC | Custom framework with type-erased async handlers, simpler than rmcp for our use case |
| Error handling | thiserror + anyhow | thiserror for library errors, anyhow for binary error propagation |
| Logging | tracing + tracing-subscriber | Structured logging compatible with OpenTelemetry |
| Cron scheduling | Custom matcher | Simple 5-field cron parser — avoids external dependency for standard cron syntax |

## Build Artifacts

| Binary | Release Size | Description |
|--------|-------------|-------------|
| axi-bot | 17 MB | Discord bot with event handlers, slash commands, scheduler |
| axi-supervisor | 1.8 MB | Process supervisor with crash detection and rollback |
| procmux | 2.9 MB | Process multiplexer (Unix socket server) |
| discordquery | 5.8 MB | Discord message history query CLI |

## Crate Summary

| Crate | Type | Tests | Description |
|-------|------|-------|-------------|
| procmux | lib + bin | 7 | Process multiplexer — wire protocol, server, client |
| claudewire | lib | 21 | Claude CLI stream-json protocol types, parsing, and transport |
| axi-config | lib | 4 | Config loading, Discord REST client, model management |
| axi-hub | lib | 2 | Agent session management, lifecycle, rate limits |
| axi-mcp | lib | 8 | MCP tool servers — protocol, tools, schedules |
| axi-bot | bin | 51 | Discord bot, events, commands, channels, scheduler, streaming, prompts, permissions, todos, frontend, startup, bridge |
| discordquery | bin | 5 | Discord message history query CLI (guilds, channels, history, search, wait) |
| axi-supervisor | bin | 0 | Process supervisor (tested via integration) |
| **Total** | | **98** | |

## Migration Plan

1. **Build**: Run `./build-release.sh` in `axi-rs/` to produce release binaries
2. **Install service**: Copy `axi-bot.service` to `/etc/systemd/system/`
3. **Test**: Deploy to test guild first using existing `axi_test.py` system
4. **Cutover**: Stop Python bot, start Rust bot, verify all commands and agents work
5. **Rollback**: If issues arise, stop Rust bot, restart Python bot (no data migration needed — same JSON files)
