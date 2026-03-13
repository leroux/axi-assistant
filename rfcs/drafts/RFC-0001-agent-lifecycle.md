# RFC-0001: Agent Lifecycle

**Status:** Draft
**Created:** 2026-03-09

## Problem

Agent lifecycle (spawn, wake, sleep, kill, reconstruct) is the most regression-prone area of both codebases. The two implementations diverge on key details: axi-py has hot-restart reconnection and bridge transport teardown logic that axi-rs lacks, while axi-rs has an explicit wake_lock and cwd validation that axi-py does not. Without a single normative document, edge cases (stale resume IDs, concurrent wakes, phantom slots) will continue to diverge.

## Behavior

### State Model

An agent session exists in one of four states:

| State | `is_awake` | `is_processing` | Description |
|-------|-----------|-----------------|-------------|
| **Sleeping** | false | false | Session exists, no CLI process. |
| **Awake-Idle** | true | false | CLI process running, no query in flight. |
| **Awake-Processing** | true | true | CLI process running, query_lock held. |
| **Killed** | N/A | N/A | Session removed from registry; channel mapping may persist. |

`is_awake` = the agent has a live CLI process (axi-py: `client is not None`; axi-rs: `session.awake == true`).
`is_processing` = the agent's query_lock is held.

### Spawn

1. Create an AgentSession data object (awake: false, empty message queue, fresh query_lock).
2. Register the session in the sessions registry.
3. Ensure a Discord channel exists for the agent.
4. Set the channel topic with cwd, session_id, prompt_hash, and agent_type. Topic update MUST be fire-and-forget to avoid blocking on Discord's channel-edit rate limit.
5. Optionally launch an initial prompt as a background task.
6. A spawn guard (e.g., `bot_creating_channels`) MUST be held from before channel creation through session registration, preventing gateway events from overwriting the session.

### Wake

1. If the agent is already awake, return success (idempotent).
2. Validate that the agent's cwd directory exists. Fail with an error if it does not.
3. Acquire a wake lock and double-check the awake flag to prevent duplicate concurrent wakes.
4. Request a scheduler slot (with configurable timeout, default 120s). On timeout, raise ConcurrencyLimitError.
5. Build CLI options with optional resume `session_id`.
6. Create the CLI subprocess.
7. If resume fails:
   a. Record the failed session_id in `last_failed_resume_id`.
   b. Clear the session_id.
   c. Retry with a fresh session (no resume_id).
   d. If the fresh attempt also fails, release the scheduler slot and return an error.
8. On first wake (no prior system prompt posted), post the system prompt to the agent's Discord channel.
9. Set `is_awake` to true.
10. On any failure after slot acquisition, release the slot before returning.

### Sleep

1. If the agent `is_processing` and `force` is not set, skip sleep (no-op).
2. Disconnect the CLI client (see Disconnect below).
3. Set `is_awake` to false.
4. Clear `bridge_busy`.
5. Release the scheduler slot.

### Kill (end_session)

1. Disconnect the CLI client.
2. Release the scheduler slot.
3. Close per-agent log handlers.
4. Remove the session from the registry.
5. Messages to killed agents (session removed but channel mapping persists) MUST be explicitly rejected with a user-visible system message.

### Disconnect Client

The disconnect procedure depends on the transport type:
- **Bridge-backed agents**: Call the transport's async `close()` method.
- **Direct subprocess agents**: Call exit with a timeout, then SIGTERM fallback if the process does not terminate.

### Reconstruction (restart recovery)

1. Scan Discord channels in managed categories (Axi, Active).
2. Parse cwd, session_id, prompt_hash, and agent_type from channel topics.
3. Load per-agent config (packs, MCP servers).
4. Create sleeping AgentSession entries.
5. Reconstructed agents MUST have a proper system prompt (not None). If the session already has a system prompt from a prior source (e.g., bridge), preserve it; otherwise generate one from cwd.
6. Verify cwd against expected values and correct stale topic data.

### Hot Restart (bridge reconnection)

1. Create a BridgeTransport.
2. Initialize the SDK client.
3. Subscribe to bridge streams. Subscribe MUST happen after SDK init to avoid replay messages corrupting the handshake.
4. Drain buffered output for mid-task agents.
5. Reconnected agents use `restore_slot` (unconditional, may exceed max_slots) rather than `request_slot`.

### wake_or_queue

1. Attempt to wake the agent.
2. On ConcurrencyLimitError: queue the message, notify the user, return false.
3. On other wake failure: post an error, return false.
4. On success: check `is_processing`.
   - If processing: queue the message (do NOT return true), return false.
   - If not processing: return true (caller sends the query).
5. Messages to killed agents: post an explicit rejection message.
6. Messages to sleeping/queued agents: add an hourglass reaction.

### rebuild_session

1. End the old session (disconnect, release slot, close logs).
2. Create a fresh sleeping AgentSession preserving system prompt, cwd, MCP servers, and frontend state from the old session unless overrides are provided.

### Activity Tracking

- `reset_activity` sets `last_activity` to now, clears idle reminder count, and resets activity phase to Starting.

## Invariants

- **I-LC-1**: Channel topic updates during spawn MUST be fire-and-forget. Synchronous topic edits block indefinitely under Discord's rate limit (2 per 10 min). [axi-py I1.1]
- **I-LC-2**: The spawn guard MUST be held from channel creation through session registration. Gateway events can overwrite the session if the guard is released early. [axi-py I1.2]
- **I-LC-3**: Messages to killed agents MUST be rejected with an explicit message, not silently queued. [axi-py I1.3, axi-rs I1.2]
- **I-LC-4**: `wake_or_queue` MUST check `is_processing` after wake succeeds and queue the message if busy. Returning true while processing causes competing query tasks. [axi-py I1.4, axi-rs I1.1]
- **I-LC-5**: On hot restart, SDK client init MUST precede bridge subscribe. Subscribing first replays buffered messages that corrupt the SDK handshake. [axi-py I1.5]
- **I-LC-6**: Failed wake MUST log stderr and clear the client reference so subsequent messages trigger a fresh wake. [axi-py I1.6]
- **I-LC-7**: System prompt MUST be posted in the actual wake path, not only in dead-code paths. [axi-py I1.7]
- **I-LC-8**: Reconstructed agents MUST have a proper system prompt, not None. [axi-py I1.8]
- **I-LC-9**: Bridge-provided system_prompt and mcp_servers MUST NOT be overwritten during reconstruction. [axi-py I1.9]
- **I-LC-10**: `last_failed_resume_id` MUST prevent re-persisting a session_id that previously failed resume. Without this, implementations can enter an infinite stale-ID resume cycle. [axi-py I1.13]
- **I-LC-11**: `end_session` MUST release the scheduler slot. Failure to do so causes phantom slot exhaustion. [axi-py I1.14]
- **I-LC-12**: `disconnect_client` for non-bridge direct-subprocess transports MUST NOT call SDK `__aexit__()` if it triggers a busy-loop (known SDK bug). Kill the subprocess directly instead. [axi-py I1.11]
- **I-LC-13**: Default `agent_type` MUST be consistent across the codebase. [axi-py I1.12]
- **I-LC-14**: `post_awaiting_input` MUST be called before `sleep_agent` after query completion and queue drain, so external consumers can detect that the bot has finished. [axi-rs I1.3]

## Open Questions

1. **Default agent_type divergence.** axi-py defaults to `claude_code`; axi-rs defaults to `flowcoder`. Which should be normative? The axi-py I1.12 invariant exists because defaulting to flowcoder caused failures when flowcoder was disabled.

2. **Wake lock.** axi-rs uses an explicit wake_lock with double-check on the awake flag. axi-py does not have this guard. Should the wake_lock be required? (Concurrent wakes are possible in both codebases when multiple messages arrive simultaneously.)

3. **cwd validation on wake.** axi-rs validates that the cwd directory exists before waking. axi-py does not. Should this be normative? (A missing cwd will cause the CLI process to fail at startup anyway, but explicit validation gives a clearer error.)

4. **Hot restart / bridge reconnection.** axi-py has full hot-restart logic (reconnect mid-task agents). axi-rs exits with code 42 on bridge death and relies on systemd restart. Should hot restart be normative, or is exit-and-restart acceptable?

5. **Awaiting-input sentinel.** axi-rs posts a "Bot has finished responding and is awaiting input" sentinel message. axi-py does not. Should this be normative? (The sentinel is used by `axi_test msg` for wait-for-response detection.)

## Implementation Notes

### axi-py
- `is_awake` = `session.client is not None`.
- Uses `asyncio.Lock` for query_lock; no explicit wake_lock.
- Bridge disconnect uses BridgeTransport `close()`; direct disconnect uses `__aexit__` with timeout + SIGTERM. I1.11 notes an SDK bug requiring subprocess kill instead of `__aexit__()` for non-bridge transports.
- Hot restart reconnection fully implemented (BridgeTransport, SDK init, bridge subscribe, buffered output drain).
- `rebuild_session` preserves system_prompt, cwd, mcp_servers, and frontend state.
- `fire_and_forget` helper stores asyncio tasks in `_background_tasks` set to prevent GC under Python 3.12+ weak-ref semantics.

### axi-rs
- `is_awake` = `session.awake` boolean flag.
- `is_processing` = `query_lock.try_lock().is_err()`.
- Uses a `wake_lock` (tokio Mutex) with double-check on awake flag to prevent concurrent wakes.
- Validates cwd existence before wake.
- On bridge death, exits with code 42 for systemd restart rather than attempting in-place reconnection.
- `post_awaiting_input` sentinel message posted after query completion, gated by config flag.
- Default agent_type is `"flowcoder"`.
