# Preemptive Agent Scheduling — Design

## Executive Summary

**Verdict: Feasible.** The infrastructure for preemption already exists — inter-agent messaging already interrupts busy agents mid-turn. Preemptive scheduling is the same mechanism triggered by a timer instead of an external message.

The design adds a **watchdog task** to the scheduler that time-slices busy agents when slots are contended. It's a ~200-line change across 3 files with no architectural upheaval.

---

## Problem

The current scheduler is **cooperative**: agents yield slots voluntarily between turns. But `should_yield()` is only checked between complete query cycles (between `process_message` calls). A single turn — one `client.query()` + `stream_response_to_channel()` — can run for minutes to hours:

| Percentile | Duration |
|-----------|----------|
| Median | 28s |
| P75 | 2.4m |
| P90 | 8.9m |
| P95 | 16.3m |
| P99 | 47.1m |
| Max | 4.4h |

**16% of turns exceed 5 minutes.** When all slots are full and a waiting agent needs one, it can be blocked for the entire duration of a long turn — even though the scheduler has already marked a yield target.

## How Interruption Already Works

Inter-agent messaging (`deliver_inter_agent_message` in agents.py:2501) already preempts busy agents:

1. **Interrupt**: `interrupt_session()` sends bridge "kill" (SIGTERM → SIGKILL after 5s), terminating the CLI process
2. **Stream terminates**: `stream_response_to_channel()` receives no `ResultMessage`, detects the kill (line 2033), force-sleeps the agent
3. **Queue**: The inter-agent message is prepended to `message_queue`
4. **Resume**: On next wake, `--resume <session_id>` reloads conversation history. The queued message is processed as a new turn.

**Session context is preserved** — `session_id` survives process termination. Only in-flight tool calls are lost (the agent retries them naturally on the next turn).

## Design

### Core Concept: Watchdog Timer

A background `asyncio.Task` that runs every N seconds and checks:
- Are there agents waiting for slots (`has_waiters`)?
- Is any non-protected busy agent over its time quantum?
- If yes: preempt the longest-running agent to free a slot.

### Preemption Flow

```
Watchdog tick
  ├─ No waiters? → skip (no contention, let agents run freely)
  ├─ Find busy agents exceeding quantum (exclude protected)
  ├─ Pick the one running longest (background before interactive)
  └─ Preempt:
       1. Add to scheduler._preempt_set (analogous to _yield_set)
       2. Call interrupt_session() → kills CLI process
       3. Queue a "continue" message so the agent resumes its work
       4. Stream loop terminates → agent sleeps → slot freed → waiter gets it
       5. When preempted agent gets a slot again, it processes the
          "continue" message and picks up where it left off
```

### The "Continue" Message

When an agent is preempted, we inject a system-tagged message into its queue:

```python
continue_msg = (
    "[System: Your previous turn was preempted to free a slot for another agent. "
    "Continue from where you left off.]",
    channel,  # or metadata in agenthub version
    None,     # no orig_message to react on
)
session.message_queue.appendleft(continue_msg)
```

This ensures the agent doesn't just sit sleeping forever — when it wakes, it has work to resume.

### Why Not SIGINT (Graceful Interrupt)?

Procmux has `_cmd_interrupt` (sends SIGINT to process group). However:
- The flowcoder engine catches SIGINT and survives it (agents.py:2085-2086 documents this)
- Claude Code CLI behavior on SIGINT is not well-defined for SDK-managed sessions
- Kill (SIGTERM) is the proven path — inter-agent messaging already uses it successfully
- Session resume (`--resume`) handles the state recovery

### Configuration

```python
# In config.py or scheduler init
PREEMPT_QUANTUM_SECS = int(os.environ.get("PREEMPT_QUANTUM_SECS", "300"))  # 5 minutes default
PREEMPT_CHECK_INTERVAL_SECS = 10  # watchdog tick rate
PREEMPT_MASTER_IMMUNE = True  # master agent never preempted (already protected)
```

**Why 5 minutes?** 66% of turns complete within 60s, 83% within 5 minutes. A 5-minute quantum means only truly long-running turns get preempted, and only when there's actual contention.

The quantum should be configurable per-agent via a "nice" value in the future, but start with a global default.

### Scheduler Changes (agenthub/scheduler.py)

Add to `Scheduler`:

```python
class Scheduler:
    def __init__(self, ..., preempt_quantum: float = 300.0):
        ...
        self._preempt_quantum = preempt_quantum
        self._preempt_set: set[str] = set()  # agents marked for preemption
        self._watchdog_task: asyncio.Task | None = None

    def start_watchdog(self, preempt_fn: Callable[[AgentSession], Awaitable[None]]) -> None:
        """Start the preemption watchdog. Called after hub init."""
        self._preempt_fn = preempt_fn
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    async def stop_watchdog(self) -> None:
        """Stop the watchdog (shutdown)."""
        if self._watchdog_task:
            self._watchdog_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._watchdog_task

    async def _watchdog_loop(self) -> None:
        """Periodic check for agents exceeding their time quantum."""
        while True:
            await asyncio.sleep(10)  # check interval
            if not self._waiters:
                continue  # no contention, skip
            await self._check_preemption()

    async def _check_preemption(self) -> None:
        """Find and preempt the longest-running non-protected busy agent."""
        sessions = self._get_sessions()
        candidates: list[tuple[float, str, AgentSession]] = []

        for name, session in sessions.items():
            if name in self._protected:
                continue
            if session.client is None:
                continue
            if not session.query_lock.locked():
                continue  # not busy
            if name in self._preempt_set:
                continue  # already being preempted
            started = session.activity.query_started
            if started is None:
                continue
            busy_secs = (datetime.now(UTC) - started).total_seconds()
            if busy_secs < self._preempt_quantum:
                continue
            candidates.append((busy_secs, name, session))

        if not candidates:
            return

        # Preempt the longest-running agent (background first, then interactive)
        # Sort by: (not interactive, busy_time) — background agents preempted first
        candidates.sort(key=lambda x: (x[1] not in self._interactive, x[0]), reverse=True)
        busy_secs, name, session = candidates[0]
        self._preempt_set.add(name)
        log.info(
            "Preempting agent '%s' (busy %.0fs, quantum=%.0fs, %d waiters)",
            name, busy_secs, self._preempt_quantum, len(self._waiters),
        )
        try:
            await self._preempt_fn(session)
        except Exception:
            log.exception("Failed to preempt agent '%s'", name)
        finally:
            self._preempt_set.discard(name)

    def release_slot(self, agent_name: str) -> None:
        """Release a slot. (Extended to clear preempt_set.)"""
        # ... existing code ...
        self._preempt_set.discard(agent_name)
        # ... rest of existing logic ...

    def status(self) -> dict[str, Any]:
        """Return scheduler state for diagnostics. (Extended.)"""
        return {
            # ... existing fields ...
            "preempt_targets": sorted(self._preempt_set),
            "preempt_quantum": self._preempt_quantum,
        }
```

### Preemption Callback (agenthub/messaging.py or lifecycle)

```python
async def preempt_agent(hub: AgentHub, session: AgentSession) -> None:
    """Preempt a busy agent: interrupt it and queue a continue message."""
    log.info("Preempting agent '%s'", session.name)

    # Queue a continue message so the agent resumes work on next wake
    continue_msg = (
        "[System: Your previous turn was preempted by the scheduler to free a slot "
        "for a waiting agent. Continue from where you left off.]",
        None,  # metadata — frontend will fill in channel on processing
    )
    session.message_queue.appendleft(continue_msg)

    # Interrupt the CLI process — this causes stream_response_to_channel
    # to exit (no ResultMessage → got_result=False → force sleep).
    await interrupt_session(hub, session)
```

### Integration Points

1. **Hub init** (`hub.py` or `hub_wiring.py`):
   ```python
   hub.scheduler.start_watchdog(
       preempt_fn=lambda session: preempt_agent(hub, session)
   )
   ```

2. **Shutdown** — stop the watchdog:
   ```python
   await hub.scheduler.stop_watchdog()
   ```

3. **Status command** — already handled by extending `scheduler.status()`

4. **Discord notification** — the frontend callback `post_system` can be called to notify the user:
   ```
   ⏰ Agent **foo** preempted after 5m12s (slot needed by waiting agent).
   Will resume when a slot opens.
   ```

### Where the "Continue" Message Gets Processed

After preemption:
1. `interrupt_session()` kills CLI → ExitEvent → `stream_response_to_channel` line 2033-2044 → `sleep_agent(force=True)`
2. Slot freed → waiting agent gets it via `release_slot` → waiter event set
3. The preempted agent now has a "continue" message in its queue
4. Eventually a slot opens (or is freed by eviction), and `process_message_queue` processes the continue message
5. The agent wakes with `--resume`, gets the continue prompt, and picks up its work

### Edge Cases

**1. Agent preempted while writing to Discord (partial message)**
- `stream_response_to_channel` already handles this: the live-edit state is finalized (cursor removed), any buffered text is flushed. See lines 2037-2039 (`_live_edit_finalize` + `_flush_text` in "post_kill" path).

**2. Agent preempted during a multi-step tool sequence**
- The CLI process dies, so any in-flight tool call is lost. When the agent resumes, it has conversation history up to the last committed point. Claude naturally retries the tool call on the next turn. This is the same behavior as query timeout and inter-agent interrupt.

**3. Agent preempted mid-stream**
- Same as #1. The streaming live-edit finalization path handles this.

**4. Interaction with message queue**
- The "continue" message is prepended (`appendleft`) so it's processed first. Any user messages already in the queue come after — the agent finishes its resumed work before processing new messages.

**5. What if the agent is preempted repeatedly?**
- Each preemption adds a "continue" message. The agent might get preempted multiple times on a long task. This is fine — each resume continues from the last state. But we should add a guard: if the queue already has a "continue" message, don't add another.

**6. What if preemption races with natural completion?**
- The interrupt is sent, but the agent finishes its turn in the ~5s SIGTERM grace period. The stream gets a ResultMessage (natural completion) — `got_result=True` — so the force-sleep path is NOT taken. The agent processes its queue normally, finds the "continue" message, and just gets a redundant prompt. Harmless.

**7. Protected agents (master)**
- Protected agents are never preempted. The master agent always keeps its slot.

**8. No waiters by the time preemption completes**
- The watchdog only triggers when `has_waiters()` is true. If the waiter gets a slot from another source (idle eviction, natural completion) before the preemption completes, the slot freed by preemption just sits available for the next requester. No harm done.

## What Changes

| File | Change |
|------|--------|
| `packages/agenthub/agenthub/scheduler.py` | Add `_preempt_set`, `_preempt_quantum`, `_watchdog_task`, `start_watchdog()`, `stop_watchdog()`, `_watchdog_loop()`, `_check_preemption()`. Extend `release_slot()` and `status()`. |
| `packages/agenthub/agenthub/messaging.py` | Add `preempt_agent()` function. |
| `axi/hub_wiring.py` | Call `hub.scheduler.start_watchdog(...)` after hub creation. |
| `axi/config.py` | Add `PREEMPT_QUANTUM_SECS` config var. |
| `axi/agents.py` | Add preemption notification to Discord (post_system callback). Handle the case where `metadata` (channel) is None in `process_message_queue` for preemption-queued messages. |

Estimated ~200 lines of new code.

## What Doesn't Change

- The cooperative `should_yield` mechanism stays as-is (for between-turn yielding)
- The existing eviction logic (idle agents) is untouched
- The interrupt/kill mechanism is reused as-is
- The session resume mechanism is reused as-is
- The streaming/rendering pipeline is untouched

## Alternatives Considered

### 1. Just increase MAX_AWAKE_AGENTS
- Doesn't solve the problem — just delays it. Each awake agent consumes a Claude Code CLI process (~200MB RSS). At 10+ agents, memory pressure becomes real.

### 2. Check should_yield mid-turn (inside the stream loop)
- Would require injecting yield checks inside `stream_response_to_channel`, breaking the clean stream-to-completion model.
- The agent can't gracefully "pause" mid-stream — there's no save/restore for partial turns.
- Much more invasive than the watchdog approach.

### 3. Use SIGINT instead of SIGKILL
- Flowcoder engine catches SIGINT and survives it. Not reliable.
- Claude Code CLI SIGINT behavior is undefined for SDK sessions.
- Kill is the proven path.

### 4. CFS-style fair scheduling (vruntime tracking)
- Over-engineering for the current scale. With 4-10 agents and simple priority classes (protected/interactive/background), round-robin with a fixed quantum is sufficient. CFS adds complexity with no clear benefit at this scale.

## Testing Plan

1. **Unit test**: Mock scheduler with 2 slots, 3 agents. Verify agent 3 gets a slot after agent 1 exceeds quantum.
2. **Integration test**: Spawn a test bot instance, create 2 agents with MAX_AWAKE=1. First agent runs a long prompt. Second agent's message should trigger preemption. Verify second agent gets a response within quantum + grace period.
3. **Edge case test**: Preempt an agent, verify it resumes correctly with the "continue" message.
4. **Race test**: Agent finishes naturally during SIGTERM grace period. Verify no corruption.

## Open Questions

1. **Quantum value**: 5 minutes is a reasonable default based on duration stats. Should it be shorter? 2-3 minutes would preempt ~25% of turns. Probably start at 5m and tune based on experience.
2. **Per-agent quantum**: Should agents be able to set their own quantum (nice value)? Not for v1 — global default is sufficient.
3. **Preemption notification**: Should the preempted agent's Discord channel show a message? Yes — the user should know why the agent paused.
4. **Max preemptions per agent per session**: Should we limit how many times an agent can be preempted? Probably not for v1 — let the scheduler handle it naturally.
