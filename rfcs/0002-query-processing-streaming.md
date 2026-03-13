# RFC-0002: Query Processing & Streaming

**Status:** Draft
**Created:** 2026-03-09

## Context

A **query** is one message-in, response-out cycle with a Claude CLI process. The user
(or another agent) sends a message; the CLI runs autonomously — thinking, calling tools,
writing code — producing a stream of events until it emits a final result or something
goes wrong. This RFC defines the protocol for sending queries, consuming streams,
handling errors, and routing messages between agents and frontends.

This RFC builds on [RFC-0001 (Agent State Machine)](0001-agent-state-machine.md), which
defines agent states and transitions. This RFC specifies what happens *inside* the
PROCESSING state and during the IDLE → PROCESSING → IDLE cycle.

## Problem

Both axi-py and axi-rs implement query processing, but the designs diverge in important
ways:

- **Stream normalization**: axi-py has a frontend-agnostic `stream_response` generator
  yielding typed `StreamOutput` events. axi-rs passes raw SDK JSON to frontends, which
  each parse it independently. This duplicates parsing logic and caused the
  `stream_event` envelope unwrapping bug (axi-rs I2.1).
- **Interrupt model**: Both have soft and hard interrupts but the choice of which to use
  is ad-hoc — scattered across `interrupt_session`, `handle_query_timeout`, and
  `deliver_inter_agent_message` with different logic in each.
- **Retry**: Both retry at the query layer with "Continue from where you left off", but
  error classification (transient vs fatal) is implicit.
- **Interactive gates**: axi-py handles plan approval/questions in the Discord frontend
  only. axi-rs races multiple frontends. The multi-frontend race pattern should be
  canonical.

This RFC defines a single query processing protocol that both implementations (and any
future rewrite) must follow.

## Message Routing

When a message arrives for an agent, routing depends on state (per RFC-0001):

| State | Action |
|-------|--------|
| SLEEPING | Queue message, trigger wake |
| IDLE | Acquire query lock, enter PROCESSING |
| PROCESSING | Queue message, soft-interrupt current turn |
| PROCESSING (compacting) | Queue message, do NOT interrupt |
| RECONNECTING | Queue message (delivered after reconnection) |
| KILLED | Reject with error |

### Entry point

`receive_message` is the single entry point for all message sources (user, inter-agent,
scheduled, SDK commands). It:

1. Rejects if agent is KILLED or system is shutting down
2. If SLEEPING: queues message, triggers wake (RFC-0001 SLEEPING → IDLE)
3. If RECONNECTING: queues message
4. If PROCESSING: queues message, soft-interrupts if not compacting
5. If IDLE: acquires query lock, enters PROCESSING

This function does not distinguish between frontends. Frontend-specific behavior
(reactions, typing indicators) is handled by the frontend's stream handler, not here.

### Message queue

Two-level priority queue per RFC-0001:

```
struct MessageQueue {
    high: VecDeque<Message>,   // inter-agent coordination
    normal: VecDeque<Message>, // user messages, scheduled, SDK commands
}
```

`push`: inter-agent → `high.push_back`, all others → `normal.push_back`.
`pop`: drain `high` first (FIFO), then `normal` (FIFO).

Inter-agent messages maintain arrival order among themselves while always being processed
before user messages. This prevents the LIFO bug both implementations had with
`push_front`/`appendleft`.

### Queue drain

After each completed turn:

1. Check scheduler yield flag — if set, sleep (RFC-0001 yield transition) and fire
   background wake if queue is non-empty
2. If queue is non-empty and not yielding: pop next message, stay in PROCESSING
3. If queue is empty: release query lock, enter IDLE, start idle timer

The drain loop must check `shutdown_requested` on each iteration.

## Query Lifecycle

A single turn follows this sequence:

```
acquire query_lock
  ├── send message to CLI via client.query()
  ├── consume stream (see Stream Events below)
  │     ├── capture session_id from first event
  │     ├── yield normalized events to frontends
  │     ├── track activity phase
  │     └── handle errors / interrupts
  ├── on success: ResultMessage received
  ├── on transient error: retry (see Retry below)
  ├── on fatal error: report, sleep agent
  └── on stream killed: yield StreamKilled, sleep agent
release query_lock
drain message queue
```

### session_id capture

`session_id` is captured from the **first** stream event (not just `ResultMessage`) and
persisted to durable storage immediately. This ensures the ID survives bot crashes
mid-turn. If a fresh session returns the same ID as a previously failed resume attempt
(`last_failed_resume_id`), it must NOT be re-persisted (prevents infinite retry loops).

See RFC-0001 "Session Identity" for full lifecycle.

### Activity tracking

The stream normalizer tracks the agent's current activity phase:

```
Starting → Thinking → Working → ToolUse → Idle
```

Transitions are driven by stream events:
- `content_block_start` with type `thinking` → Thinking
- `content_block_start` with type `text` → Working
- `content_block_start` with type `tool_use` → ToolUse
- `result` → Idle

Activity phase is observable metadata for frontends (status display, channel emoji) but
does not affect query processing logic.

## Stream Events

The stream normalizer sits between the raw SDK/CLI output and frontends. It consumes
raw JSON events from the CLI process and emits typed `StreamEvent` values.

```
enum StreamEvent {
    // Text output
    TextDelta { text: String }
    TextFlush                          // end of text block or mid-turn split point

    // Thinking
    ThinkingStart
    ThinkingDelta { text: String }
    ThinkingEnd

    // Tool use
    ToolUseStart { id: String, name: String }
    ToolResult { id: String }

    // Context management
    Compacting
    CompactDone { context_window: u64, context_tokens: u64 }

    // Rate limiting
    RateLimit { status: String, resets_at: DateTime, utilization: Option<f64> }

    // Session lifecycle
    Result { session_id: String, duration_ms: u64 }
    StreamKilled                       // stream ended without ResultMessage

    // Retry
    RetryAttempt { attempt: u32, max: u32, delay_ms: u64 }

    // Escape hatch
    Raw { event_type: String, data: JsonValue }
}
```

### Normalization rules

1. **Envelope unwrapping**: If the CLI wraps events in `{"type": "stream_event",
   "event": {...}}`, unwrap before processing. This is transparent to frontends.

2. **Known events** are parsed into their typed variant. Unknown event types become
   `Raw`.

3. **`session_id` extraction** happens in the normalizer, not in frontends. The
   normalizer updates the session's `session_id` on the first event that contains one.

4. **Compaction flag**: The normalizer sets `session.compacting = true` on `Compacting`
   and clears it on `CompactDone`. This flag gates interrupt behavior (see below).

5. **`TextFlush`** is emitted at: `end_turn` (message_delta stop_reason), block
   boundaries, and stream completion. Frontends use this as a signal to finalize
   buffered text.

6. **`StreamKilled`** is emitted when the stream ends without a `ResultMessage` — the
   CLI crashed, was killed, or the connection dropped. Frontends should flush partial
   content and expect the agent to sleep.

### Frontend handler

Frontends implement a handler that receives `StreamEvent` values:

```
trait StreamHandler {
    async fn on_event(&mut self, event: StreamEvent)
    async fn on_error(&mut self, error: QueryError)
}
```

The stream normalizer calls `on_event` for each normalized event. Frontends render
as appropriate (Discord: live-edit messages; Web: broadcast JSON over WebSocket; CLI:
print to stdout).

**Interactive gates** (plan approval, AskUserQuestion) are special — they block the
stream until the user responds. For multi-frontend setups:

```
async fn request_gate(frontends: &[Frontend], gate: Gate) -> GateResponse {
    // Spawn all frontends concurrently
    // First response wins, cancel the rest
    select_first(frontends.map(|f| f.handle_gate(gate))).await
}
```

This ensures any frontend can respond to interactive prompts, regardless of which
frontend originated the conversation.

## Interrupt Model

Interrupts use layered escalation. There is one `interrupt` function with a reason
that determines escalation behavior:

```
enum InterruptReason {
    NewMessage,      // soft only — new message arrived for busy agent
    Stop,            // user /stop — soft then escalate
    Timeout,         // query timeout exceeded — soft then escalate
    ForceKill,       // eviction, shutdown — hard immediately
}
```

### Escalation sequence

```
NewMessage:
  1. Send SDK soft interrupt (client.interrupt())
  2. No escalation — partial result preserved in conversation

Stop / Timeout:
  1. Send SDK soft interrupt
  2. If not complete within 5s: SIGINT to process group
  3. On Timeout: rebuild session (preserving session_id)

ForceKill:
  1. SIGINT to process group
  2. If not exited within 5s: SIGKILL to process group
```

### Compaction protection

When `session.compacting == true`, `NewMessage` interrupts are suppressed — the message
is queued but no interrupt is sent. Interrupting compaction corrupts the agent's context
window and requires a full session rebuild.

`Stop`, `Timeout`, and `ForceKill` ignore the compaction flag — they always interrupt.

### Bridge-managed agents

For agents running behind a process bridge (procmux), the interrupt path is:

1. **Soft**: Send bridge "interrupt" command (delivers SIGINT to CLI process group)
2. **Hard**: Send bridge "kill" command (delivers SIGTERM, then SIGKILL)

The SDK `client.interrupt()` is not sufficient alone for bridge agents — it only
cancels the current API call, not the multi-turn CLI loop. Both SIGINT (process group)
AND SDK interrupt must be sent for a complete soft interrupt.

### Invariants (from both specs)

- SIGINT must target the process group (`os.killpg` / `kill(-pid)`), not just the CLI
  PID, so Task subagents are also interrupted (axi-py I2.11)
- CLI must be spawned with `start_new_session=True` / `setsid` for process group
  signaling to work (axi-py I2.11)
- `/stop` for bridge agents must use bridge "kill", not "interrupt" — the CLI catches
  SIGINT and survives (axi-py I2.9)
- Interrupt must send both SIGINT and SDK interrupt; SIGINT alone only cancels the
  current step (axi-py I2.10)

## Retry

Retry operates at the query layer, wrapping the entire query+stream cycle.

```
async fn query_with_retry(client, content, handler, config) -> QueryResult {
    let mut message = content;
    for attempt in 1..=config.max_retries {
        match send_and_stream(client, message, handler).await {
            Ok(result) => return Ok(result),
            Err(e) if e.is_transient() && attempt < config.max_retries => {
                let delay = config.base_delay * 2^(attempt - 1);
                handler.on_event(RetryAttempt { attempt, max, delay_ms });
                sleep(delay).await;
                message = "Continue from where you left off.";
            }
            Err(e) => return Err(e),
        }
    }
}
```

### Error classification

| Error | Transient? | Action |
|-------|:---:|--------|
| API overloaded (529) | yes | retry with backoff |
| Rate limited (429) | **no** | report to frontend, do not retry |
| Network error | yes | retry with backoff |
| Invalid request (400) | no | report, fail |
| Auth error (401/403) | no | report, fail |
| CLI crash / stream killed | no | emit StreamKilled, sleep agent |
| `MessageParseError` (unknown event type) | skip | log, skip event, continue stream |

Rate limits are explicitly NOT retried — they indicate quota exhaustion, not a transient
failure. The frontend should display the rate limit information (resets_at, type) so the
user knows when to retry.

`MessageParseError` for unknown event types is not an error — the normalizer logs and
skips, continuing to process the stream. This forward-compatibility allows the CLI to
add new event types without breaking the bot.

## SDK Commands

SDK commands (`/clear`, `/compact`, custom slash commands) follow the same query
lifecycle as user messages with one constraint:

The command must be sent via `client.query()` **before** streaming the response. Both
axi-py (I2.12) and axi-rs had bugs where the stream was consumed without first sending
the command.

```
// Correct:
client.query(command_text);       // sends the command
stream_response(client, handler); // consumes the response

// Wrong:
stream_response(client, handler); // command never sent
```

## Auto-Compact

When a `RateLimit` event includes `context_window` and `context_tokens`, the normalizer
evaluates whether auto-compaction should trigger:

```
fn should_auto_compact(context_window: u64, context_tokens: u64, threshold: f64) -> bool {
    if context_window == 0 || context_tokens == 0 {
        return false;  // guard against division by zero
    }
    (context_tokens as f64 / context_window as f64) >= threshold
}
```

This must be a pure function (axi-rs I2.3). When it returns true:

1. Set `session.compacting = true`
2. Inject a `/compact` command at the front of the message queue
3. The compact command follows the normal query lifecycle

The `compacting` flag must be set both on CLI-reported compaction events AND on
self-triggered auto-compact (axi-rs I2.5).

## Invariants

These must hold in any correct implementation:

1. **Single entry point:** All message sources (user, inter-agent, scheduled, SDK
   commands) flow through `receive_message`. No alternate paths that bypass queue
   management or state checks.

2. **Query lock exclusivity:** At most one turn executes per agent at any time. The
   query lock is acquired before sending and released after stream completion (or error).
   Queue drain re-acquires for each message.

3. **session_id early capture:** `session_id` must be captured from the first stream
   event, not deferred to `ResultMessage`. Persisted immediately.

4. **Compaction protection:** A compacting agent must never be soft-interrupted. The
   `compacting` flag is set on both CLI-reported and self-triggered compaction.

5. **Interrupt completeness:** For bridge agents, both SIGINT (process group) and SDK
   interrupt must be sent. Process group signaling requires `setsid` at spawn time.

6. **Retry non-interference with rate limits:** Rate limit errors (429) must not
   trigger retry. They are reported to the frontend as-is.

7. **SDK command ordering:** SDK commands must be sent via `client.query()` before
   the response stream is consumed.

8. **Stream killed handling:** When the stream ends without `ResultMessage`, the agent
   must be slept. Partial content must be flushed to frontends before sleeping.

9. **Envelope transparency:** SDK event envelope wrapping (`stream_event`) must be
   handled by the normalizer, never leaked to frontends.

10. **Queue drain liveness:** Queue drain must not deadlock. The drain function acquires
    its own query lock — callers must not hold it when invoking drain (axi-rs I2.4).

## Open Questions

None — all design decisions resolved.

## Implementation Notes

**Stream event envelope:** The Claude CLI currently wraps some events in
`{"type": "stream_event", "event": {...}}`. The normalizer must check for this and
unwrap. This is a CLI implementation detail that may change — the normalizer absorbs
the complexity so frontends don't need to care.

**Bridge readline limit:** The bridge transport must support messages up to at least
10MB. The default 64KB readline limit caused `LimitOverrunError` on large SDK responses
(axi-py I2.6).

**Bridge subscribe ordering:** When reconnecting, SDK client initialization must
complete before subscribing to the bridge. Subscribe replays buffered messages that
corrupt the initialization handshake if they arrive first (axi-py I2.5, RFC-0001
invariant 8).
