# Axi Rust Rewrite — Smoke Test Report

**Date**: 2026-03-07
**Test instance**: `rust-rewrite` on guild `nova` (1475631458243710977)
**Binary**: `axi-rs/target/release/axi-bot` (18 MB ELF, release build)
**Bot identity**: Axi Nova 1 (id=1475629376430145576)

## Test Environment

- Systemd service: `axi-test@rust-rewrite` via modified `axi-test@.service` template
- Template auto-detects Rust binary (`axi-rs/target/release/axi-bot`) and falls back to Python
- Procmux bridge: Rust `procmux` binary running on `.bridge.sock`
- Model: `claude-haiku-4-5-20251001` (via `AXI_MODEL=haiku`)
- Streaming: Enabled (`STREAMING_DISCORD=true`)
- All other test instances torn down before testing

## Results Summary — Infrastructure

| Area | Status | Notes |
|------|--------|-------|
| Startup | PASS | ~2s cold start, all infrastructure found |
| Discord connection | PASS | Connects, registers 17 slash commands |
| Channel reconstruction | PASS | 77 channel-to-agent mappings restored |
| Guild infrastructure | PASS | Finds Axi, Active, Killed categories |
| Scheduler | PASS | Initializes with 7 slots, 10s tick, "axi-master" protected |
| Pack loading | PASS | Loads 'algorithm' (3537 chars) and 'axi-dev' (6306 chars) |
| Bot message filtering | PASS | Own messages ignored, only processes external messages |
| SIGTERM shutdown | PASS | Clean exit (inactive/dead, not failed) |
| Restart cycle | PASS | `axi_test.py restart` works, full re-init in ~2s |
| Rate limiting | PASS | Detected and handled (logged backoff on PUT reaction) |
| Resource usage | PASS | 3.3 MB RSS, 6 tasks, 26ms CPU total |

## Results Summary — Bridge & Agent Lifecycle

| Area | Status | Notes |
|------|--------|-------|
| Procmux connection | PASS | Bot connects to Rust procmux bridge on startup |
| Claude process spawn | PASS | Spawns via procmux, haiku model used |
| Response streaming | PASS | Live-edit messages update in Discord as tokens arrive |
| Simple Q&A ("What is 2+2?") | PASS | Responds "4" correctly, ~1.8s |
| Longer responses | PASS | 20-element periodic table rendered correctly |
| Multi-turn context | PASS | Remembers "42" across multiple messages |
| Session reconnection | PASS | Resumes with session_id after bot restart |
| Thinking indicator | PASS | "*thinking...*" messages appear during thinking blocks |
| Response timing | PASS | "-# 1.8s" appended to final message |
| Agent sleep/wake cycle | PASS | Properly transitions sleeping -> awake -> sleeping |
| Interrupt recovery | PASS | After `// stop`, next message spawns fresh and works |
| Session ID persistence | PASS | `.master_session_id` saved and used for resume |

## Text Commands (// prefix)

| Command | Status | Response |
|---------|--------|----------|
| `// status` | PASS | Shows agent state (awake/sleeping), session ID |
| `// debug` | PASS | Toggles debug mode on/off |
| `// stop` | PASS | "Agent interrupted." — sends SIGINT to process group |
| `// clear` | PASS | "Sent /clear to agent." |
| `// compact` | PASS | "Sent /compact to agent." |
| `// todo` | PARTIAL | "Todo list display not yet implemented." (stub) |
| `// unknown_cmd` | PASS | Proper error with available commands list |

## Performance Comparison (Rust vs Python)

| Metric | Rust (axi-bot) | Python (axi.main) |
|--------|---------------|-------------------|
| RSS Memory | 15.8 MB | 107.5 MB |
| VSZ Memory | 355 MB | 949 MB |
| %MEM | 0.1% | 1.3% |
| %CPU (idle) | 0.1% | 99.7%* |
| Startup time | ~2s | ~5-10s |
| Binary size | 18 MB | N/A (interpreted) |

*Python CPU at 99.7% was from a runaway process issue, not representative of normal operation.

## Bugs Found & Fixed

### 1. CLAUDECODE environment leak (FIXED)

**Problem**: Procmux spawned Claude CLI processes that inherited the `CLAUDECODE` env var from the
parent session, causing "Claude Code cannot be launched inside another Claude Code session" errors.

**Fix**: Added `cmd.env_clear()` before `cmd.envs(&env)` in `procmux/src/server.rs` so spawned
processes only get explicitly passed environment variables.

### 2. Non-streaming mode doesn't post text (NOT FIXED — workaround applied)

**Problem**: When `STREAMING_DISCORD=false` (default), `StreamContext.live_edit` is None,
so `live_edit_tick()` and `live_edit_finalize()` return immediately. Text accumulates in
`text_buffer` but is never sent to Discord.

**Workaround**: Set `STREAMING_DISCORD=true` in `.env`. Full fix would add a fallback in
non-streaming mode to post the final text buffer as a regular message.

### 3. Query lock held during sleep (FIXED)

**Problem**: In `events.rs`, the spawned message-processing task held the `query_lock` while
calling `sleep_agent`. But `sleep_agent` checks `is_processing()` (which tries to acquire the
same lock), sees it's held, and skips sleeping. This left the agent in an "awake" state with a
dead transport after interrupts, causing subsequent messages to hang.

**Fix**: Restructured the task to drop `query_lock` before calling `process_message_queue`
and `sleep_agent`. The lock is now scoped to just the `process_message` + timeout block.

### 4. Zombie processes after interrupt (cosmetic)

**Problem**: After SIGINT kills a Claude process, procmux doesn't `wait()` on it, leaving a
zombie (`[claude] <defunct>`). This is cosmetic — the zombie is reaped when procmux exits.

**Impact**: Minor. No functional impact.

## Known Limitations

1. **`// todo` not implemented**: Returns stub message. Low priority.

2. **Duplicate "Guild infrastructure ready" log**: The message is logged twice — once
   with details (category IDs) and once without.

3. **`axi_test.py msg` sentinel detection**: The test tool warns "timed out without sentinel"
   even when the bot responds successfully. This is a test harness issue, not a bot issue.

## Not Yet Tested (require specific scenarios)

These features were not tested in this session but the code paths exist:

- MCP tool execution (spawn, kill, send, schedule — require real agent workflows)
- AskUserQuestion permission callback (requires tool call that triggers question)
- ExitPlanMode permission callback (requires plan mode interaction)
- Auto-compact (requires context to exceed threshold)
- TodoWrite in-stream display (requires agent to use TodoWrite tool)
- Image attachment processing (requires sending image in Discord)
- Slash commands (/spawn, /kill, /list-agents — require Discord interaction)
- Inter-agent messaging
- Channel recency reordering

## Recommendations

1. **Fix non-streaming mode**: Add a fallback in `stream_response` to post the final
   text buffer as a regular message when `live_edit` is None.

2. **Fix zombie reaping**: Have procmux call `.wait()` on child processes after exit
   to prevent zombie accumulation.

3. **Test with real agent workflow**: Spawn a coding agent to test MCP tools,
   TodoWrite display, and AskUserQuestion handling end-to-end.

4. **Run supervisor**: For production, update `axi-supervisor` to launch `axi-bot`
   instead of `python -m axi.main`.
