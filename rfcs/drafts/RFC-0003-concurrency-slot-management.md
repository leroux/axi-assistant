# RFC-0003: Concurrency & Slot Management

**Status:** Draft
**Created:** 2026-03-09

## Problem

The slot scheduler is a shared-nothing concurrency primitive: it decides which agents get to run and which must wait or yield. Both implementations converge on the same model (fixed slots, FIFO wait queue, eviction tiers, yield targets), but axi-rs has behaviors not present in axi-py (idempotent request_slot, concurrent-grant-on-timeout handling) and axi-py has behaviors not in axi-rs (config read-modify-write locking, spawn channel guard). Divergence here causes hard-to-diagnose concurrency bugs.

## Behavior

### Slot Pool

The scheduler maintains a fixed pool of `max_slots` awake slots. Each slot is identified by agent name. The pool is the gatekeeper for CLI process creation: an agent MUST hold a slot before creating a CLI subprocess.

### request_slot

Decision table for `request_slot(agent_name)`:

| Condition | Action |
|-----------|--------|
| Agent already holds a slot | Return success immediately (idempotent). |
| Slots available (count < max_slots) | Grant slot, return success. |
| Idle evictable agent exists | Evict it, grant slot, return success. |
| All agents busy, no evictable | Enqueue requester in FIFO, select yield target, block with timeout. |
| Timeout expires | Remove from wait queue, return ConcurrencyLimitError. |
| Timeout expires but slot was concurrently granted | Return success (check slot set before erroring). |

The requesting agent itself is excluded from both eviction and yield target selection.

### Eviction

When all slots are occupied and a new slot is requested:

1. Collect eviction candidates: agents that are awake, not busy, not bridge-busy, and not protected.
2. Partition into tiers: background agents before interactive agents.
3. Within each tier, sort by idle time descending (longest-idle first).
4. Evict the first candidate (sleep it, freeing its slot).

An agent is **protected** if it is configured as such at scheduler construction time (typically the master agent). Protected agents are never evicted and never selected as yield targets.

An agent is **interactive** if it has been recently messaged by a user. Interactive agents have higher eviction resistance than background agents.

### Yield Target Selection

When no idle agents can be evicted (all are busy):

1. Enqueue the requester in a FIFO wait queue.
2. Select a yield target: a busy agent that will sleep after its current turn completes, freeing its slot.
3. Yield target preference: background agents before interactive agents; longest-busy within each tier.
4. Mark the yield target in a `yield_set`.

### Yield Check

After each query completes (before draining the message queue), the agent checks `should_yield`. If the agent is in the yield_set:
- Sleep instead of processing queued messages.
- This frees the slot for the waiting agent.

### release_slot

1. Remove the agent from the slot set.
2. Clean up the agent from `yield_set` and `interactive` tracking sets.
3. If a waiter exists in the FIFO queue, grant the freed slot to the next waiter and notify it.

`release_slot` MUST be synchronous/non-blocking. It relies on single-threaded event loop guarantees (asyncio) or mutex ordering (tokio) to avoid re-entrancy issues when called from the eviction path.

### restore_slot

`restore_slot` unconditionally adds an agent to the slot set without eviction or queueing. This is used for reconnected agents (bridge reconnect) that need to re-enter the pool without displacing currently running agents. It MAY cause the slot count to temporarily exceed `max_slots`.

### Concurrency Guards

- **Spawn channel guard** (`bot_creating_channels`): Prevents `on_guild_channel_create` from auto-registering a plain session over a spawn-in-progress. Must be held from before channel creation through session registration.
- **Config lock** (`_config_lock`): Config read-modify-write operations (e.g., `set_model`) MUST hold a lock across the full load-mutate-save cycle to prevent TOCTOU races.

### Slot Release Obligations

Every code path that disconnects a client (sets it to null/None, sets awake=false) MUST call `release_slot`. Every code path that creates a live client after reconnect MUST call `restore_slot`.

## Invariants

- **I-CS-1**: Every code path that clears the client MUST call `release_slot`. Failure causes phantom slot exhaustion. [axi-py I3.1]
- **I-CS-2**: Every reconnect code path that sets a live client MUST call `restore_slot`. Failure causes slot count drift. [axi-py I3.2]
- **I-CS-3**: `restore_slot` MUST allow exceeding `max_slots` without eviction or blocking. After hot restart, more agents may reconnect than max_slots allows. [axi-py I3.3]
- **I-CS-4**: Spawn signals MUST be processed immediately after the triggering query completes, not deferred to a periodic scheduler tick. A 30s tick interval causes unacceptable delay. [axi-py I3.4]
- **I-CS-5**: The spawn channel-creation guard MUST be held until the session is registered. Gateway events can overwrite sessions if released early. [axi-py I3.5]
- **I-CS-6**: A serial queue processor MUST NOT impose a fixed timeout on waiting callers. Multiple concurrent callers cause false-positive failures. [axi-py I3.6]
- **I-CS-7**: A queue processor MUST set the caller's event on exception, not only on success. Failure leaves callers blocked indefinitely. [axi-py I3.7]
- **I-CS-8**: Config read-modify-write MUST hold a lock across the full cycle (load, mutate, save). Lock-only-on-save allows TOCTOU races. [axi-py I3.8]

## Open Questions

1. **Configurable timeout value.** axi-py specifies a default of 120s for `request_slot` timeout. axi-rs does not specify a default. Should a normative default be set?

2. **Concurrent grant on timeout.** axi-rs B3.14 handles the case where the timeout fires but the slot was concurrently granted (checks slot set before returning error). axi-py does not appear to handle this race. Should this be normative?

3. **Config lock scope.** axi-py has `_config_lock` and I3.8 for config read-modify-write. axi-rs uses a static tokio Mutex for schedule file access (Section 8) but does not explicitly mention config locking. Should config locking be normative?

4. **Slot release synchronicity.** axi-py B3.9 notes that `release_slot` is synchronous, relying on single-threaded asyncio guarantees. axi-rs uses a tokio Mutex which is async. Does this difference matter for correctness in the eviction path?

## Implementation Notes

### axi-py
- Scheduler uses asyncio primitives (`asyncio.Event` for waiters, `asyncio.Lock` for config).
- `release_slot` is synchronous, safe to call inside the scheduler lock.
- `request_slot` has a configurable timeout (120s default), raises `ConcurrencyLimitError`.
- `bot_creating_channels` guard is a set of agent names being created.
- `_config_lock` is an asyncio.Lock used for config read-modify-write.

### axi-rs
- Scheduler uses tokio primitives (`Notify` for waiters, `Mutex` for scheduler state).
- `request_slot` returns immediately if agent already holds a slot (idempotent).
- Concurrent grant on timeout: checks slot set before returning ConcurrencyLimit error.
- Protected agents excluded from eviction and yield via constructor configuration.
- `release_slot` cleans up yield_set and interactive sets in addition to the slot set.
- No explicit config lock mentioned in the spec (schedules use `SCHEDULES_LOCK`).
