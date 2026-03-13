# RFC-0013: Hot Restart & Bridge Reconnection

**Status:** Draft
**Created:** 2026-03-09

## Problem

Hot restart allows the bot process to upgrade its own code without killing running agent CLI processes. The procmux bridge holds agent subprocesses across restarts, but reconnecting to mid-task agents requires careful ordering of SDK initialization, bridge subscription, and message replay. axi-py implements full in-place reconnection with fake-initialize interception and message queueing. axi-rs implements bridge startup with exponential backoff and basic reconnection but lacks the initialize-interception mechanism. Divergence here means one implementation can hot-restart cleanly while the other may corrupt agent sessions.

## Behavior

### Architecture

The system has three process layers:
1. **Supervisor** (optional; axi-py only) — manages bot.py and bridge lifecycle.
2. **Bot process** — the main application (bot.py / axi-rs binary).
3. **Bridge (procmux)** — holds agent CLI subprocesses, communicates via Unix socket.

Hot restart kills only the bot process and relaunches it. The bridge and its agent subprocesses survive.

### Signal Handling (axi-py supervisor)

| Signal | Behavior |
|--------|----------|
| SIGHUP | Forward SIGTERM to bot.py only. Leave bridge alive. Relaunch bot.py. |
| SIGTERM / SIGINT | Forward signal to bot.py. After exit, kill bridge (SIGTERM then SIGKILL after 5s). Exit 0. |

### Bridge Startup Connection

On startup, the bot process MUST connect to the bridge with retry:
- Use exponential backoff: 6 attempts at intervals starting from 500ms, doubling each time (500ms, 1s, 2s, 4s, 8s, 16s; approximately 30 seconds total).
- This tolerates procmux starting after the bot (separate systemd unit or supervisor child).
- After all retries exhausted, fail startup.

### Reconnection Sequence

After establishing a bridge connection, the bot discovers surviving agents:

1. Call `list_agents` on the bridge to enumerate surviving agent sessions.
2. For each bridge agent:
   a. If the agent has a matching session in the local session map, spawn a reconnection task.
   b. If the agent has **no** matching session, kill it as an orphan via `send_command("kill")`.
3. Fire all reconnection tasks concurrently.

### Per-Agent Reconnection (reconnect_single)

For each surviving agent with a matching session:

1. Mark the session as `reconnecting = true`.
2. Create a bridge transport with `reconnecting = true`.
3. Initialize the SDK client with the existing `session_id` for resume. The SDK client MUST be created **before** subscribing to the bridge stream.
4. Subscribe to the bridge stream. Subscribe replays buffered messages.
5. Check the agent's bridge-reported status:

| CLI Status | Idle | Action |
|------------|------|--------|
| `"exited"` | — | Clean up session. Leave agent sleeping for respawn on next message. Do not create a client. |
| `"running"` | true | Agent is idle. Clear `reconnecting`. Restore scheduler slot. |
| `"running"` | false | Agent was mid-task. Set `bridge_busy = true`. Fire `on_reconnect(was_mid_task=true)`. Restore scheduler slot. |

6. On success or CLI-exited, clear `reconnecting = false`.
7. On any failure, clear `reconnecting = false` and log the error.

### Initialize Interception (axi-py)

When `reconnecting = true`, the transport MUST intercept the SDK's `initialize` control_request:
- Do NOT forward the initialize bytes to the already-initialized CLI process.
- Enqueue a fake success `control_response` so the SDK believes initialization succeeded.
- Clear the `reconnecting` flag after interception.

This avoids corrupting an already-initialized CLI with a duplicate initialize handshake.

### Message Queueing During Reconnection

Messages arriving for an agent while `session.reconnecting = true` MUST be queued (not dropped, not delivered to the transport). Queued messages are delivered after `reconnecting` is cleared.

### Scheduler Slot Restoration

Reconnected agents MUST be registered with the scheduler via `restore_slot`, which adds the slot unconditionally (may temporarily exceed `max_slots`). This avoids evicting other agents and reflects reality — these agents are already running.

## Invariants

- **I-HR-1**: SDK client MUST be initialized before subscribing to the bridge. Subscribe replays buffered messages that corrupt the initialize handshake if they arrive first. [axi-py I13.1]
- **I-HR-2**: When resuming a session with an existing session_id, the transport MUST be created with `reconnecting = true`. Otherwise, the transport sends a real initialize request to an already-initialized CLI, corrupting its state. [axi-py I13.2]
- **I-HR-3**: Bridge reconnect MUST call `restore_slot` to register the agent's scheduler slot. `restore_slot` MUST allow exceeding `max_slots`. Without this, reconnected agents are invisible to the scheduler, causing slot count drift. [axi-py I13.3]
- **I-HR-4**: Startup bridge connection MUST use exponential backoff to tolerate procmux starting after the bot. A single attempt fails if the socket does not exist yet. [axi-rs I13.1]

## Open Questions

1. **Initialize interception.** axi-py intercepts the SDK's initialize request during reconnection and fakes a success response. axi-rs uses `create_client` with `session_id` for resume but does not describe an initialize-interception mechanism. Does the Rust SDK handle resume differently (no initialize sent on resume), or is this a gap?

2. **Message queueing during reconnection.** axi-py explicitly queues messages while `reconnecting = true` and delivers them after. axi-rs marks sessions as `reconnecting` but does not specify message queueing behavior. Should message queueing be normative?

3. **Hot restart trigger.** axi-py uses SIGHUP to the supervisor for hot restart. axi-rs has no supervisor and no hot-restart signal path — on bridge death it exits and relies on systemd. Should SIGHUP-triggered hot restart be normative, or is exit-and-restart sufficient?

4. **Orphan detection scope.** Both implementations kill bridge agents with no matching local session. But after a crash (vs. a clean SIGHUP restart), the local session map may be empty (no reconstruction yet). Should orphan killing be deferred until after reconstruction?

5. **Reconnection timeout.** Neither implementation specifies a per-agent reconnection timeout. Should there be one? A hung reconnection task could block the agent slot indefinitely.

## Implementation Notes

### axi-py
- Supervisor `_hup_handler` sets `_hot_restart = True`, sends SIGTERM to bot.py only.
- `connect_procmux` in `packages/agenthub/agenthub/reconnect.py` connects, lists agents, kills orphans, fires concurrent `reconnect_single` tasks.
- `reconnect_single` creates `BridgeTransport(reconnecting=True)`, inits SDK client, subscribes.
- `BridgeTransport.write` in `packages/claudewire/claudewire/transport.py` intercepts `initialize` when `_reconnecting`, enqueues fake success `control_response`, clears flag.
- `restore_slot` in `packages/agenthub/agenthub/scheduler.py` does unconditional slot add.
- Messages queued as `queued_reconnecting` status, delivered after `session.reconnecting` cleared.
- `_stop_handler` kills both bot.py and bridge; `_kill_bridge` escalates SIGTERM to SIGKILL.

### axi-rs
- `connect_bridge` in `startup.rs` retries with exponential backoff: 6 attempts at 500ms..16s.
- `connect_procmux` in `reconnect.rs` lists agents, kills orphans, spawns reconnection tasks.
- `reconnect_single` marks `reconnecting = true`, subscribes, creates client with `session_id`.
- No initialize interception — relies on SDK resume behavior.
- No explicit message queueing during reconnection.
- No supervisor process; hot restart not supported. Bridge death triggers exit(42) for systemd restart.
- Bridge monitor in `startup.rs` polls `is_alive()` every 2s with 3s grace period.
