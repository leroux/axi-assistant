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
- [ ] Initialize interception for reconnecting agents
- [ ] OTel trace context injection
- [ ] DirectProcessConnection: PTY subprocess management

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
- [x] Response streaming to Discord (live-edit messages)
- [x] Reaction-based interactions (plan approval, user questions)
- [x] Todo list rendering and tracking
- [x] Channel topic updates with session state
- [ ] Idle agent reminders
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
- [ ] Flowcoder agent type support (optional — may keep in Python)
- [ ] Test instance system (axi_test.py equivalent or keep Python version)
- [x] Crash handler and analysis
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
- [ ] Stream handler (bridge → live-edit rendering) — placeholder, needs claudewire bridge integration
- [ ] Client factories (create/disconnect/send) — placeholder, needs procmux bridge integration

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
| axi-bot | 16 MB | Discord bot with event handlers, slash commands, scheduler |
| axi-supervisor | 1.8 MB | Process supervisor with crash detection and rollback |
| procmux | 2.8 MB | Process multiplexer (Unix socket server) |

## Crate Summary

| Crate | Type | Tests | Description |
|-------|------|-------|-------------|
| procmux | lib + bin | 7 | Process multiplexer — wire protocol, server, client |
| claudewire | lib | 16 | Claude CLI stream-json protocol types and parsing |
| axi-config | lib | 4 | Config loading, Discord REST client, model management |
| axi-hub | lib | 2 | Agent session management, lifecycle, rate limits |
| axi-mcp | lib | 8 | MCP tool servers — protocol, tools, schedules |
| axi-bot | bin | 53 | Discord bot, events, commands, channels, scheduler, crash handler, streaming, prompts, permissions, todos, frontend, startup |
| axi-supervisor | bin | 0 | Process supervisor (tested via integration) |
| **Total** | | **90** | |

## Migration Plan

1. **Build**: Run `./build-release.sh` in `axi-rs/` to produce release binaries
2. **Install service**: Copy `axi-bot.service` to `/etc/systemd/system/`
3. **Test**: Deploy to test guild first using existing `axi_test.py` system
4. **Cutover**: Stop Python bot, start Rust bot, verify all commands and agents work
5. **Rollback**: If issues arise, stop Rust bot, restart Python bot (no data migration needed — same JSON files)
