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
- [ ] Per-process stdio logging (rotating files)
- [x] Signal handling (SIGTERM/SIGINT → graceful shutdown)
- [x] ProcmuxClient: connect, demux loop, command/stdin sending
- [ ] Test: run procmux-rs, connect Python bot to it via Unix socket

### Phase 2: Claudewire [ ]
**Goal**: Rust library implementing the Claude CLI stream-json protocol.

Tasks:
- [ ] Protocol types: all inbound/outbound message types (serde structs)
- [ ] Stream event types: MessageStart, ContentBlockDelta, etc.
- [ ] Content block types: TextBlock, ToolUseBlock, ThinkingBlock
- [ ] Delta types: TextDelta, InputJsonDelta, ThinkingDelta, SignatureDelta
- [ ] Control protocol: ControlRequest, ControlResponse
- [ ] Rate limit event parsing
- [ ] Schema validation (serde does this at deserialize time)
- [ ] ProcessConnection trait (replaces Python Protocol)
- [ ] ProcessEvent types: StdoutEvent, StderrEvent, ExitEvent, CommandResult
- [ ] BridgeTransport: connect, spawn, subscribe, write, read_messages, stop, close
- [ ] Initialize interception for reconnecting agents
- [ ] OTel trace context injection
- [ ] DirectProcessConnection: PTY subprocess management
- [ ] ActivityState tracking from stream events
- [ ] Bare stream event deduplication
- [ ] Unit tests for serde round-trips and event parsing

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
- [x] Crash analysis marker file writing

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
- [ ] Response streaming to Discord (live-edit messages)
- [x] Reaction-based interactions (plan approval, user questions)
- [ ] Todo list rendering and tracking
- [x] Channel topic updates with session state
- [ ] Idle agent reminders
- [ ] System prompt generation (SOUL.md, dev_context.md, packs)
- [x] Channel status prefixes

### Phase 7: MCP Tools [x]
**Goal**: MCP server implementations for agent tooling.

Tasks:
- [x] MCP server framework integration (rmcp or manual JSON-RPC)
- [x] Master tools: axi_spawn_agent, axi_kill_agent, axi_send_message, axi_restart
- [x] Discord tools: discord_send_message, discord_read_messages, discord_list_channels, discord_send_file
- [x] Schedule tools: schedule_create, schedule_list, schedule_delete
- [x] Utility tools: get_date_and_time, set_agent_status, clear_agent_status, discord_send_file
- [ ] Permission callback for cwd-based tool access control

### Phase 8: Scheduler & Advanced Features [ ]
**Goal**: Cron scheduler, flowcoder integration, remaining features.

Tasks:
- [ ] Cron-based event scheduler (recurring + one-off)
- [ ] Schedule persistence (JSON file)
- [ ] Flowcoder agent type support (optional — may keep in Python)
- [ ] Test instance system (axi_test.py equivalent or keep Python version)
- [ ] Crash handler and analysis
- [ ] Hot restart end-to-end validation

### Phase 9: Integration Testing & Deployment [ ]
**Goal**: Full system test, service files, deployment.

Tasks:
- [ ] Build release binary
- [ ] Update systemd service files for Rust binary
- [ ] End-to-end test with test instance (full bot lifecycle)
- [ ] Hot restart test (SIGHUP → reconnect → verify agents survive)
- [ ] Multi-agent stress test
- [ ] Performance comparison (CPU, memory, startup time)
- [ ] Migration plan for production cutover

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
| MCP protocol | rmcp or manual | Evaluate when we reach Phase 7 |
| Error handling | thiserror + anyhow | thiserror for library errors, anyhow for binary error propagation |
| Logging | tracing + tracing-subscriber | Structured logging compatible with OpenTelemetry |
