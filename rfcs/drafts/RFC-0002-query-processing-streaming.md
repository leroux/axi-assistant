# RFC-0002: Query Processing & Streaming

**Status:** Draft
**Created:** 2026-03-09

## Problem

Query processing is the hot path: every user message and scheduled prompt flows through it. The two implementations agree on the broad shape (query lock, streaming with retry, message queue drain) but diverge on interrupt semantics, message splitting thresholds, live-edit timing, and how rate-limit events terminate a stream. These differences create observable behavioral inconsistencies between implementations.

## Behavior

### Message Entry Point

1. A single entry point receives all incoming messages (user messages, inter-agent messages, scheduled prompts).
2. Reject during shutdown.
3. Queue during reconnect or busy states.
4. Wake the agent if sleeping (via `wake_or_queue`).
5. Acquire the query_lock.
6. Delegate to process_message for the actual turn.

### process_message

1. Validate the agent is awake.
2. Reset activity state (last_activity, idle_reminder_count, phase).
3. Send the user content to the CLI process via `query()`.
4. Consume the response stream via `stream_with_retry`.
5. The entire process_message call SHOULD be bounded by a configurable `query_timeout`.

### stream_with_retry

1. Invoke the stream handler once.
2. If it returns a transient error (NOT a rate limit), retry with exponential backoff:
   - Delay = `base_delay * 2^(attempt - 2)` (first retry has no delay, subsequent retries double).
   - Post a system message to Discord on each retry.
   - Re-query with "Continue from where you left off."
3. Maximum `max_retries` attempts.
4. If the error is a rate limit, do NOT retry. Return a distinguishable rate-limit result.

### Streaming Output

The stream engine iterates raw CLI messages and produces normalized events:

| Event | Trigger |
|-------|---------|
| TextDelta | `content_block_delta` with text delta |
| TextFlush | End of turn, mid-turn split, block boundary, stream completion |
| ToolUseStart | `content_block_start` with tool_use type |
| ThinkingStart | `content_block_start` with thinking type |
| ThinkingEnd | Next non-thinking block, end_turn, error, or result |
| QueryResult | `ResultMessage` received |
| StreamKilled | Stream ends without `ResultMessage` |
| RateLimitHit | Rate limit or billing error detected in assistant message |

**session_id capture**: The session_id MUST be captured from the first StreamEvent (not just ResultMessage) and persisted immediately, so it survives bot crashes before the turn completes.

**StreamKilled handling**: When the stream ends without a ResultMessage (CLI killed or crashed), the agent MUST be force-slept so the next message triggers a fresh CLI process.

### Discord Rendering (live-edit)

1. Text is buffered and flushed to Discord via live-edit (in-place message editing).
2. Message split threshold: messages MUST be split before reaching Discord's 2000-char limit.
3. Split point: prefer the last newline before the limit; fall back to the limit itself.
4. Edit throttle interval: edits SHOULD be throttled to avoid Discord rate limits.
5. A streaming cursor (e.g., a block character) is appended during streaming and removed on finalize.
6. Response timing is appended to the final message as a suffix. Responses under 0.5 seconds skip the timing annotation.
7. Thinking indicators: a temporary message (e.g., `*thinking...*`) is posted on thinking block start and deleted when a non-thinking block starts, on end_turn, on error, or on result.
8. Tool progress messages (e.g., `*Running command...*`) are posted and bulk-deleted, gated by a config flag.

### Interrupt

When a new message arrives for a busy agent:

1. Queue the message.
2. Send a graceful interrupt to abort the current turn early.
3. Interrupt semantics by transport type:
   - **Bridge agents**: Send SIGINT to the process group (not just the process), so child subagents also receive it. Then send SDK-level interrupt. Both are needed: SIGINT cancels the current step; SDK interrupt cancels the multi-turn query.
   - **Direct subprocess agents**: SDK `client.interrupt()`.
4. Graceful interrupt with kill fallback: attempt graceful first, escalate to hard kill if it fails.

**Compaction guard**: Agents undergoing context compaction MUST NOT be interrupted. The message is queued without calling interrupt.

### Query Timeout

1. When a query times out, interrupt the CLI (graceful + kill fallback).
2. Rebuild the session, preserving the session_id if available so conversation context survives.
3. Post a recovery/reset status message.

### Message Queue Drain

After a turn completes:

1. Drop the query_lock.
2. Check `should_yield` (scheduler). If marked for yield, sleep instead of draining.
3. Otherwise, drain queued messages sequentially:
   - Pop from the front of the queue.
   - Wake the agent if needed.
   - Re-acquire query_lock for each message.
   - Break on shutdown or scheduler yield.
4. Post the awaiting-input sentinel (if configured).
5. Sleep the agent.

### Inter-Agent Messages

1. If the target agent is busy (but not compacting): queue the message at the front and interrupt.
2. If the target is idle: spawn a background task to wake and process (via `run_initial_prompt`).

### SDK Commands

Commands like `/clear` and `/compact` MUST call `client.query()` before streaming the response. Without the query call, the command does nothing.

## Invariants

- **I-QP-1**: Message queue size MUST be read with `len(deque)`, never `.qsize()`. The latter does not exist on all deque types. [axi-py I2.1]
- **I-QP-2**: Busy-agent detection MUST check `is_processing` before spawning a processing task, even if the agent is already awake. [axi-py I2.2]
- **I-QP-3**: Visibility check MUST be enforced inside the stream-to-channel renderer, not at the caller. Otherwise scheduled agents bypass it. [axi-py I2.3]
- **I-QP-4**: Thinking indicator MUST be cleaned up on block transitions, errors, and end-of-turn. Orphaned indicators are a regression. [axi-py I2.4]
- **I-QP-5**: Bridge subscribe replay MUST write all buffered messages synchronously before setting `subscribed=True`. Interleaving live and buffered messages produces garbled output. [axi-py I2.5]
- **I-QP-6**: Bridge readline limit MUST be at least 10MB. The default 64KB causes LimitOverrunError on large SDK responses. [axi-py I2.6]
- **I-QP-7**: session_id MUST be captured from the first StreamEvent, not only from ResultMessage. Mid-turn crashes lose the session_id otherwise. [axi-py I2.7]
- **I-QP-8**: Interrupt MUST send SIGINT (not SIGTERM/SIGKILL) to preserve conversation context. [axi-py I2.8]
- **I-QP-9**: `/stop` MUST use bridge kill (not interrupt) and force-sleep the agent when the stream ends without ResultMessage. [axi-py I2.9]
- **I-QP-10**: Interrupt MUST send both process-group SIGINT and SDK interrupt. SIGINT alone only cancels the current step. [axi-py I2.10]
- **I-QP-11**: CLI MUST be spawned with a new session; interrupt MUST use process-group SIGINT to reach subagents. [axi-py I2.11]
- **I-QP-12**: SDK commands MUST call `client.query()` before streaming. [axi-py I2.12]
- **I-QP-13**: Stream events wrapped in `stream_event` envelopes MUST be unwrapped before extracting content_block, delta, and name fields. [axi-rs I2.1]
- **I-QP-14**: Agents undergoing compaction MUST NOT be interrupted by inter-agent messages. Interrupting compaction corrupts the context window. [axi-rs I2.2]
- **I-QP-15**: Auto-compact threshold logic MUST be a pure function that handles zero-value context_window and context_tokens without division-by-zero. [axi-rs I2.3]
- **I-QP-16**: `queue_and_wake` MUST NOT double-acquire query_lock. The drain function acquires its own lock internally. [axi-rs I2.4]
- **I-QP-17**: The `compacting` flag MUST be set on both CLI-reported and self-triggered compaction, cleared only on compact_boundary. [axi-rs I2.5]

## Open Questions

1. **Message split threshold.** axi-py uses 1800 chars; axi-rs uses 1900 chars (MSG_LIMIT). Should this be standardized? Both are below Discord's 2000-char limit, but the difference affects where messages break.

2. **Edit throttle interval.** axi-rs uses 0.8s. axi-py's interval is not explicitly specified in the spec. Should a normative interval be set?

3. **Timing annotation format.** axi-rs uses `-# {elapsed}{trace_id}`. axi-py appends inline timing. Should the format be standardized?

4. **Compaction guard.** axi-rs has explicit compaction protection (I2.2, I2.5). axi-py does not mention compaction guards. Should compaction protection be normative?

5. **`queue_and_wake` vs `wake_or_queue`.** axi-rs added `queue_and_wake` (queue first, then wake) for voice transcripts alongside the existing `wake_or_queue` (wake first, then check). Should both entry points be normative, or should they be unified?

## Implementation Notes

### axi-py
- `receive_user_message` is the single entry point for all frontends.
- `_stream_with_retry` uses a stream_handler callback pattern.
- `stream_response` (agenthub) yields `StreamOutput` events; `stream_response_to_channel` (Discord) renders them.
- Text buffered and flushed at end_turn, 1800-char splits, block boundaries, stream completion.
- `interrupt_session` dispatches by transport: procmux "interrupt" for bridge, `transport.stop()` for flowcoder, SDK `client.interrupt()` for direct.
- `graceful_interrupt` uses `session.client.interrupt()` with 5s timeout.
- Log context via contextvars (`log_context.py`) propagates agent_name, channel_id, trigger through async chains.

### axi-rs
- `process_message` validates awake, resets activity, sends query, dispatches streaming.
- `stream_with_retry` delay formula: `retry_base_delay * 2^(attempt-2)`.
- `live_edit_tick` with MSG_LIMIT=1900 chars, EDIT_INTERVAL=0.8s.
- `append_timing` with `-# {elapsed}` suffix, skips <0.5s responses.
- `update_activity` transitions phase (Starting/Thinking/ToolUse/Working/Idle) based on content_block_start type.
- `split_message` prefers newline-boundary splits.
- `deliver_inter_agent_message` has explicit compaction guard: queues without interrupt when `compacting == true`.
- `queue_and_wake` is a separate entry point that queues first, then wakes (for voice transcripts).
- `inject_pending_flowchart` pushes synthetic /command messages to front of queue.
