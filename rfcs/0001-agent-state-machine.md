# RFC-0001: Agent State Machine

**Status:** Draft
**Created:** 2026-03-09

## Context

An **agent** is a long-lived coding assistant backed by a Claude CLI process. Unlike a
single API call, the CLI runs autonomously — reading files, writing code, calling tools
— until it decides it's done or is interrupted. A single message to an agent may
trigger dozens of internal steps before a response comes back.

The system manages multiple agents concurrently. Each agent is an OS process (the
Claude CLI) with its own working directory, conversation history, and tool access. A
**bot** orchestrates these agents: spawning them, routing messages to them, and
rendering their output to a frontend (Discord, CLI, web — whatever).

Agents are expensive. Each awake agent holds an OS process, an API session, and memory.
The system can't keep them all awake, so it uses **slots** — a fixed pool that limits
how many agents run simultaneously. Agents that aren't actively needed are put to sleep
(process killed, slot freed) and woken on demand.

A **turn** is one message-in, response-out cycle. The agent receives a user message,
then runs autonomously — thinking, calling tools, writing code — producing a stream of
events until it emits a final result. One turn may involve many internal API calls and
tool executions. A turn ends when the CLI emits a `ResultMessage` or when something
goes wrong (timeout, crash, interrupt).

The **bridge** (procmux) is a process multiplexer that keeps agent CLI processes alive
across bot restarts. When the bot restarts, it reconnects to surviving processes
instead of starting fresh. Not all deployments use a bridge, but the state machine
must account for it.

## Problem

The agent lifecycle is the foundation everything else builds on — scheduling, message
routing, reconnection, frontends. Both axi-py and axi-rs implement it, but the states
and transitions are implicit (derived from field combinations like `client != null &&
query_lock.locked()`). This makes it hard to verify both implementations agree, and
easy for new code to violate invariants it doesn't know about.

This RFC defines the canonical agent state machine for a process-based agent system.

## States

An agent is always in exactly one of these states:

```
                ┌────────────┐
 spawn ────────▶│  SLEEPING  │◀─────────────────────────┐
                └──────┬─────┘                          │
                       │ wake                           │
                       ▼                                │
                ┌────────────┐  idle timeout /          │
            ┌──▶│    IDLE    │──sleep / eviction───────▶│
            │   └──────┬─────┘                          │
            │          │ msg                            │
            │          ▼                                │
            │   ┌────────────┐  yield / timeout /       │
            └───│ PROCESSING │──crash / eviction───────▶│
         turn   └────────────┘
        done

 SLEEPING ──▶ RECONNECTING ──▶ IDLE or SLEEPING
 (restart)    (reattach)

 any state ──▶ KILLED
```

### SLEEPING

No client process. Agent is registered and can be woken. Message queue may be
non-empty (messages arrived while sleeping, or left over from yield).

Observable: `client == null`, not in scheduler slot set.

### IDLE

Client process is live. Query lock is free. Ready to process the next message.
An **idle timer** starts when the agent enters this state. If no message arrives
within `idle_timeout` (default 60s), the agent transitions to SLEEPING. This
prevents agents from holding slots indefinitely when no one is talking to them.

Observable: `client != null`, in scheduler slot set, query lock free.

### PROCESSING

Client process is live. Query lock is held. A message is being sent to the client,
streamed back, and rendered to a frontend.

Behavioral variants (flags, not substates):
- `compacting`: Context compaction in progress. Incoming messages are queued without
  sending a graceful interrupt, since interrupting compaction corrupts context.

Observable: `client != null`, in scheduler slot set, query lock held.

### RECONNECTING

Bot process has restarted. A live client process exists in the bridge but this bot
instance does not yet control it. The agent is being re-attached to its process.

Messages are queued during this state and delivered after reconnection completes.

This state exists because the agent's situation is fundamentally different from
SLEEPING — it has a live process, but cannot accept messages or be woken normally.

Observable: `reconnecting == true`.

### KILLED (terminal)

Session removed from registry. Client disconnected, scheduler slot released, logs
closed. The agent name may be reused by spawning a new agent.

Observable: session absent from registry.

## Transitions

### spawn → SLEEPING

**Trigger:** External request (user command, schedule, inter-agent spawn tool).

**Effects:**
1. Create session object with: `name`, `cwd`, `system_prompt`, `mcp_servers`
2. Register in session registry
3. Client is null, no scheduler slot held

**Identity established:** `name` is the primary key. `cwd` and `system_prompt` define
the agent's role. `session_id` may be provided for resuming a prior conversation.

**Initial prompt:** The caller may send an initial message immediately after spawn.
This is just a normal message — it triggers the standard SLEEPING → IDLE → PROCESSING
flow. When the turn completes and the queue is empty, the idle timer starts, and the
agent sleeps after `idle_timeout` like any other agent.

### SLEEPING → IDLE (wake)

**Trigger:** Message arrives for a sleeping agent, explicit wake request, or auto-wake
after yield with non-empty queue.

**`wake` is idempotent.** If the agent is already IDLE or PROCESSING, wake is a no-op.
If multiple callers try to wake the same agent concurrently, only one spawns a process;
the others observe the agent is awake and proceed. Callers never need to check state
before calling wake.

**Effects:**
1. Request scheduler slot (may block, may trigger eviction of another agent)
2. Build client options (model, system prompt, resume session_id)
3. Start client process
4. Set client reference on session

**Resume handling:**
- If `session_id` is set, attempt to resume the prior conversation
- If resume fails, retry once with `session_id = null` (fresh session)
- Record the failed ID in `last_failed_resume_id` to prevent stale-ID cycling
- A fresh session that returns the same ID as the failed one must NOT re-persist it

**Failure:**
- Slot request timeout → remain SLEEPING, return error to caller
- Client process fails to start → release slot, remain SLEEPING, return error
- Resume fails → retry fresh (one attempt), then fail as above

### IDLE → PROCESSING

**Trigger:** Message received or dequeued while agent is idle.

**Effects:**
1. Acquire query lock
2. Send message content to client
3. Stream response events through frontend handler
4. Track activity phase (starting → thinking → writing → tool_use → idle)

### PROCESSING → IDLE (turn complete)

**Trigger:** Client stream completes normally (ResultMessage received).

**Effects:**
1. Release query lock
2. Check scheduler yield flag — if marked for yield, transition to SLEEPING instead
   (see yield below)
3. Check message queue — if non-empty, immediately transition back to PROCESSING
4. If queue is empty, start the idle timer. If no message arrives within
   `idle_timeout` (default 60s), transition to SLEEPING

### PROCESSING → SLEEPING (yield)

**Trigger:** Scheduler marked this agent for yield (another agent needs its slot)
and the current turn has completed.

**Preconditions:**
- `should_yield(name)` returns true
- Current query has completed (checked between turns, not mid-turn)

**Effects:**
1. Disconnect client, release slot
2. If message queue is non-empty: the yielding task fires a background wake request
   before exiting. This re-enters the slot request queue (behind whoever triggered
   the yield), preserving fairness. When a slot becomes available, the agent wakes
   and drains its queue.

### PROCESSING → SLEEPING (timeout)

**Trigger:** Query exceeds `query_timeout`.

**Effects:**
1. Hard-interrupt the client process (SIGINT to process group)
2. Rebuild session atomically (see rebuild below), preserving `session_id`
3. Agent is now SLEEPING with a fresh session object

### PROCESSING → SLEEPING (stream killed)

**Trigger:** Client stream ends without a ResultMessage (CLI crashed or was killed).

**Effects:**
1. Disconnect client, release slot
2. Agent is SLEEPING — next message triggers a fresh wake

This is distinct from normal completion. The frontend should indicate the abnormal
termination. The conversation may have partial/corrupt state; `session_id` is preserved
so resume can attempt recovery, but implementations should expect resume to fail.

### IDLE → SLEEPING (idle timeout)

**Trigger:** Agent has been IDLE for `idle_timeout` with no incoming messages.

**Effects:**
1. Disconnect client, release slot
2. Agent sleeps until the next message arrives

This is the normal end of an interaction. All agents — including the master — sleep
when idle. Slots are a limited resource; no agent holds one indefinitely.

### IDLE → SLEEPING (explicit)

**Trigger:** Explicit sleep request (user command, shutdown, admin tool).

**Effects:**
1. Disconnect client
2. Release scheduler slot

### IDLE/PROCESSING → SLEEPING (eviction)

**Trigger:** Scheduler needs a slot for another agent and selected this one.

**Preconditions for IDLE eviction:** Agent is not protected.

**Preconditions for PROCESSING eviction:** `force = true`. The in-flight query will
fail — the frontend must handle this gracefully (flush partial output, show error).

**Effects:**
1. Disconnect client (with timeout — must succeed even if process is hung)
2. Release scheduler slot

### any → KILLED

**Trigger:** Explicit kill request (user command, cleanup).

**Effects:**
1. If client is live: disconnect with timeout, escalate to SIGKILL if needed
2. Release scheduler slot (if held)
3. Close per-agent log handlers
4. Remove session from registry

### SLEEPING → SLEEPING (rebuild)

**Trigger:** Session needs fresh state (prompt change, cwd change, error recovery).

**Effects (must be atomic from the registry's perspective):**
1. Create new session object with same `name`
2. Swap new session into registry (single operation — the agent is never absent)
3. Clean up old session (disconnect client if live, release slot, close logs)
4. Preserve: `cwd`, `system_prompt`, `mcp_servers`, `frontend_state` (unless overridden)
5. Clear: `client`, `query_lock`, `message_queue`, `activity`

Atomicity requirement: the registry must never contain a gap where the agent name
resolves to nothing. The old session is replaced, not removed-then-added.

### SLEEPING → RECONNECTING → IDLE or SLEEPING

**Trigger:** Bot process restarts, discovers live client processes in bridge.

**Effects:**
1. Mark session `reconnecting = true`
2. Create transport to existing process (no new spawn)
3. Initialize SDK client handshake
4. Subscribe to bridge — replays buffered messages (must happen AFTER SDK init)
5. Restore scheduler slot (unconditional, may temporarily exceed `max_slots`)
6. Clear `reconnecting`
7. Detect client state:
   - Client was idle → transition to IDLE
   - Client was mid-task → transition to PROCESSING, drain buffered output
   - Client had exited → clean up, remain SLEEPING

Messages arriving during RECONNECTING are queued. After reconnection completes, the
queue is drained normally.

Orphan processes (running in bridge but no matching session) are killed.

## Message Routing

When a message arrives for an agent, the routing depends on state:

| State | Action |
|-------|--------|
| SLEEPING | Queue message, attempt wake |
| IDLE | Process immediately (acquire query lock, enter PROCESSING) |
| PROCESSING | Queue message, send soft interrupt to current turn |
| PROCESSING (compacting) | Queue message, do NOT interrupt |
| RECONNECTING | Queue message (delivered after reconnection completes) |
| KILLED | Reject with error message |

### Queue ordering

Messages are processed in FIFO order with one exception: **inter-agent messages are
pushed to the front of the queue.** This is intentional — inter-agent messages are
coordination signals (task results, status updates). An agent waiting for a sub-agent's
results should not be blocked behind queued user messages.

When multiple inter-agent messages arrive while the agent is busy, they accumulate at
the front in arrival order (each new one is inserted behind the previous inter-agent
messages, not ahead of them). This preserves ordering among inter-agent messages while
keeping them ahead of user messages.

### Queue drain

After each completed turn, the agent checks its message queue. If non-empty and not
yielding, it processes the next message immediately, staying in PROCESSING.

If the agent is yielding, it sleeps and auto-wakes (re-entering the slot request queue)
so queued messages are eventually processed.

### Interrupts

Two interrupt mechanisms exist with different severity:

**Soft interrupt** (graceful): Sends an SDK control protocol message. The client
cleanly aborts the current API call and emits a partial result. Used when a new message
arrives for a busy agent — the goal is to finish quickly, not to kill the process.

The partial result becomes part of the conversation history. The next turn sees a
truncated response. In-flight tool calls may be lost. This is a trade-off: the user
gets faster response to their new message at the cost of a ragged previous turn.

**Hard interrupt** (kill): Sends SIGINT to the process group. The client process may
terminate. Used for timeouts, `/stop` commands, and forced eviction. The conversation
may have corrupt state; session rebuild is typically needed after a hard interrupt.

## Session Identity

A session's identity consists of:

| Field | Survives rebuild? | Survives restart? | Description |
|-------|:-:|:-:|---|
| `name` | always | persisted | Primary key, immutable |
| `cwd` | unless overridden | persisted | Working directory |
| `system_prompt` | unless overridden | reconstructed | Agent role and personality |
| `mcp_servers` | unless overridden | reconstructed | Available tools |
| `session_id` | only if passed | persisted | Conversation resume handle |
| `frontend_state` | yes | reconstructed | Frontend-specific state |

**Persisted** fields are written to durable storage (how is frontend-specific — e.g.
Discord channel topics, a database, a file) and survive a full process restart.

**Reconstructed** fields are derived from current config at startup. If the config
changes between crash and restart, the agent gets new values. This is by design —
the agent should reflect current config, not stale state.

### `session_id` lifecycle

1. Null at spawn (unless resuming a prior conversation)
2. Captured from the first stream event during PROCESSING (not just ResultMessage)
3. Persisted to durable storage immediately (survives bot crash mid-turn)
4. Used on next wake to resume conversation context
5. Cleared on resume failure; failed ID tracked in `last_failed_resume_id`
6. A fresh session returning the same ID as a failed one must NOT re-persist it

## Concurrency

### Scheduler slots

A fixed pool of `max_slots` controls how many agents can be IDLE or PROCESSING
simultaneously. SLEEPING and KILLED agents do not consume slots. RECONNECTING agents
use `restore_slot` which may temporarily exceed `max_slots`.

- **request_slot(name, timeout):** Acquires a slot. If all slots are occupied, attempts
  eviction. If eviction fails (all agents busy or protected), enqueues a waiter and
  marks a yield target. Raises `ConcurrencyLimitError` on timeout.
- **release_slot(name):** Frees a slot and grants it to the next waiter (FIFO).
- **restore_slot(name):** Registers an already-awake agent without eviction or blocking.
  Used during reconnection. May cause slot count to temporarily exceed `max_slots`.

### Eviction

When a slot is needed and all are occupied:

1. Find the longest-idle non-protected agent. Evict it (sleep, release slot).
2. If all agents are busy (no idle candidates): mark the longest-running non-protected
   agent for yield. Enqueue the waiter. The yield target will sleep after its current
   turn completes, freeing the slot.

**Protected agents** are never evicted or yield-targeted. An agent is protected if it
is marked as such at spawn time (e.g. the master/primary agent). Protection is a
session field, not a name check — any agent can be protected if the spawner decides.

### Locks

| Lock | Protects | Scope |
|------|----------|-------|
| query_lock | Concurrent message processing | Per-agent |
| scheduler_lock | Slot set and waiter queue | Global |

Wake idempotency (invariant 3) requires some form of internal serialization, but the
mechanism is an implementation choice — a lock, compare-and-swap, or actor model all
work. See Implementation Notes for current approaches.

## Invariants

These must hold in any correct implementation:

1. **Slot accounting:** The set of agents holding slots must equal the set of agents
   with a live client, plus any RECONNECTING agents that called `restore_slot`. Every
   path that sets `client = null` must call `release_slot`. Every path that sets
   `client` to a live value must have previously called `request_slot` or `restore_slot`.

2. **Single processor:** At most one task holds `query_lock` for a given agent at any
   time. Messages arriving during PROCESSING are queued, never processed concurrently.

3. **Wake idempotency:** `wake` must be safe to call from any number of callers
   concurrently. At most one process is spawned; redundant calls are no-ops.

4. **Resume safety:** A `session_id` that failed resume must be tracked. If a fresh
   session returns the same ID, it must not be re-persisted (prevents infinite retry).

5. **Graceful degradation:** Force-sleep and kill must succeed even if the client
   process is hung. Implementations must use timeouts and escalate to SIGKILL.

6. **Queue liveness:** Messages in the queue must eventually be processed. Specifically:
   yielding with a non-empty queue must trigger an auto-wake (slot re-request), not
   leave messages stranded. Messages queued during RECONNECTING must be delivered after
   reconnection completes.

7. **Rebuild atomicity:** The session registry must never contain a gap for an agent
   name during rebuild. The old session is replaced by the new one in a single
   operation; cleanup of the old session happens after the swap.

8. **Reconnect ordering:** SDK client initialization must complete before subscribing
   to the bridge. Subscribe replays buffered messages that corrupt the initialization
   handshake if they arrive first.

## Implementation Notes

**axi-py:** States are implicit. `is_awake()` = `client is not None`.
`is_processing()` = `query_lock.locked()`. No state enum. Wake idempotency via a
global `hub.wake_lock` — this serializes ALL agent wakes, not just per-agent. Waking
agent A blocks waking agent B. Acceptable for small agent counts (< 20) but a
per-agent lock or CAS would remove unnecessary serialization.

**axi-rs:** Uses `awake: bool` field plus `query_lock` for processing. No state enum.
Same global `wake_lock` approach as axi-py, same limitation.

Both implementations should consider adding an explicit state enum for assertions and
logging. The enum would be derived from the underlying fields, not authoritative — but
it makes invariant violations visible immediately.

### Current divergences from this RFC

Both implementations need updates to match this RFC:

- **Yield + auto-wake:** Neither implementation re-requests a slot after yield-sleep
  when the queue is non-empty. Messages are stranded until a new message arrives.
- **Rebuild atomicity:** Both implementations have a registry gap during rebuild
  (remove, then insert with an `await` in between).
- **Eviction simplification:** Both implementations have the 3-tier eviction model
  (background/interactive). This RFC specifies longest-idle-first with protected list.
- **Idle timeout:** Neither implementation has an idle timer. The master agent stays
  awake forever; spawned agents auto-sleep immediately after their initial prompt.
  This RFC unifies both: all agents sleep after `idle_timeout` of inactivity.
- **Inter-agent queue ordering:** Both implementations use `push_front`/`appendleft`,
  which is LIFO among inter-agent messages (second arrival processed before first).
  This RFC specifies inter-agent messages should maintain arrival order among
  themselves while staying ahead of user messages.
