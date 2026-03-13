# Behavioral Specification — axi-py

Generated: 2026-03-09
Source: `axi-py`
Git range: 17da036..63076db (full repo history)

## How to use this spec

- **Behaviors** (B-codes) are editable — change what the system should do
- **Invariants** (I-codes) are regression guards — delete only if the underlying cause is eliminated
- **Anchors** link spec entries to code — update when code moves

---

## 1. Agent Lifecycle

### Behaviors
- B1.1: Spawning creates an AgentSession data object, registers it in the sessions dict, ensures a Discord channel, sets the channel topic (fire-and-forget), and optionally launches an initial prompt as a background task.
- B1.2: Waking requests a scheduler slot, builds SDK options (with optional resume session_id), creates an SDK client subprocess, and posts the system prompt to the agent's Discord channel on first wake.
- B1.3: If resume fails during wake, the agent retries with a fresh session (no resume_id), clears session_id, and records the failed ID in `last_failed_resume_id` to prevent stale-ID cycling.
- B1.4: Sleeping disconnects the SDK client, releases the scheduler slot, clears `bridge_busy` and `transport`, and is skipped (non-forced) when the agent's `query_lock` is held.
- B1.5: Killing (end_session) disconnects the client, releases the scheduler slot, closes per-agent log handlers, and removes the session from the registry.
- B1.6: Reconstruction on restart scans Discord channels for agent-category channels, parses cwd/session_id/prompt_hash from the channel topic, loads per-agent config (packs, MCP server names), and creates sleeping AgentSession entries.
- B1.7: Hot restart reconnection creates a BridgeTransport, initializes the SDK client, then subscribes to bridge streams (in that order), and drains buffered output for mid-task agents.
- B1.8: `wake_or_queue` attempts to wake the agent; on `ConcurrencyLimitError` it appends the message to the session's deque and notifies the user; on any other failure it posts an error and adds a reaction.
- B1.9: When a woken agent is already processing (query_lock held), incoming messages are queued rather than spawning a competing task.
- B1.10: Messages sent to killed agents (session removed but channel mapping persists) are explicitly rejected with a system message.
- B1.11: `rebuild_session` ends the old session and creates a fresh sleeping AgentSession, preserving system prompt, cwd, MCP servers, and frontend state from the old session unless overrides are provided.
- B1.12: `disconnect_client` uses the transport's async `close()` for bridge-backed agents; for direct subprocess agents, it calls `__aexit__` with a timeout and ensures the process is dead via SIGTERM fallback.
- B1.13: `is_awake` is defined as `session.client is not None`; `is_processing` is defined as `session.query_lock.locked()`.

### Invariants
- I1.1: Channel topic updates must be fire-and-forget to avoid blocking spawn on Discord's channel-edit rate limit (2 per 10 min). [fix: 9c3bed2]
  - Regression: Synchronous `channel.edit(topic=...)` during spawn blocked indefinitely when rate-limited, preventing the initial prompt from launching.
- I1.2: The spawn guard (`bot_creating_channels`) must be held from `axi_spawn_agent` through to `agents` dict population to prevent `on_guild_channel_create` from overwriting the session. [fix: 4d42a17]
  - Regression: Gateway event fired after channel creation but before `agents[name]` was set, causing auto-register to replace the real session with a plain one.
- I1.3: Messages to killed agents (session removed) must be explicitly rejected with a system message, not silently queued. [fix: 855e603]
  - Regression: Killed agents' channel mappings persisted but the session was gone, causing a generic "not awake" path that silently queued messages to nowhere.
- I1.4: `wake_or_queue` must check `is_processing` after wake succeeds and queue the message if the agent is busy, rather than returning true. [fix: 855e603]
  - Regression: A second message arriving while the agent was processing spawned a competing task that raced with the first, corrupting state.
- I1.5: On hot restart, the SDK client must be initialized before subscribing to the bridge agent, because subscribe replays buffered messages that corrupt the SDK's initialize handshake. [fix: 6bba9fe]
  - Regression: Subscribing first pushed stale messages into the transport queue; the SDK's init handshake read those instead of the control_response, leaving the agent stuck.
- I1.6: Failed wake must log stderr and clear `session.client` so subsequent messages trigger a fresh wake instead of querying a dead process. [fix: ae1f328]
  - Regression: `client.__aenter__()` failed but left `session.client` set, so `is_awake` returned true and queries went to a dead subprocess; stderr was never logged so the error was invisible.
- I1.7: System prompt must be posted in the actual wake path (`wake_or_queue` / `wake_agent`), not only in a dead-code path. [fix: 5d15814]
  - Regression: `wake_agent()` was only called from `/telos`; the real message path used `wake_or_queue` which had no system prompt posting logic.
- I1.8: Reconstructed agents must have a proper system prompt generated from `make_spawned_agent_system_prompt(cwd)`, not `None`. [fix: 507b575, 833b0a4]
  - Regression: Reconstructed sessions had `system_prompt=None`, so wake tried to post `None` as the prompt and prompt-dependent behavior was missing.
- I1.9: Reconstructed agent `system_prompt` and `mcp_servers` set from the bridge must not be overwritten during reconstruction; prompt posting for reconnected agents uses fallback to `make_spawned_agent_system_prompt` when `session.system_prompt` is None. [fix: e96a5af]
  - Regression: An earlier fix overwrote bridge-provided prompt/MCP in `reconstruct_agents_from_channels`, breaking agents whose state came from the bridge.
- I1.10: On restart, reconstructed agents' cwd must be verified against expected values (e.g. RECORD_HANDLER_CWD) and corrected if the channel topic contains stale data. [fix: e2e3073]
  - Regression: Stale channel topic preserved an old cwd, causing the record-handler to operate in the wrong directory after restart.
- I1.11: `disconnect_client` must skip SDK `__aexit__()` for non-bridge direct-subprocess transports to avoid an anyio cancel scope busy-loop (SDK bug); instead, kill the subprocess directly. [fix: a9b6ce2]
  - Regression: `client.__aexit__()` triggered an anyio cancel scope hang in cross-task cleanup, causing sleep/disconnect to block indefinitely.
- I1.12: The default `agent_type` must be `claude_code`, not `flowcoder`. [fix: f536c52]
  - Regression: Default was `"flowcoder"` in the spawn tool, causing all agent spawns to fail when `FLOWCODER_ENABLED` was off.
- I1.13: A session_id that previously failed resume must be tracked (`last_failed_resume_id`) and must not be re-persisted when the fresh session returns the same ID, to prevent an infinite stale-ID resume cycle. [fix: aba7f05, c7267cd]
  - Regression: Claude Code reuses session IDs per project; after a failed resume, the fresh session returned the same ID, which was persisted and failed again on next wake, looping forever.
- I1.14: `end_session` must call `scheduler.release_slot` when disconnecting a client to free the concurrency slot. [fix: c7267cd]
  - Regression: The slot was never released, causing the scheduler to think the agent was still awake and eventually exhausting all concurrency slots.

### Anchors
- `axi/agents.py`:spawn_agent @ 9c3bed2 — fire_and_forget for topic update; `bot_creating_channels` guard
- `axi/agents.py`:wake_agent @ aba7f05 — resume retry with fresh session; `last_failed_resume_id` tracking
- `axi/agents.py`:wake_or_queue @ 855e603 — delegates to wake_agent; queues on ConcurrencyLimitError
- `axi/agents.py`:end_session @ c7267cd — disconnect, release_slot, close log, pop from registry
- `axi/agents.py`:reconstruct_agents_from_channels @ 833b0a4 — rebuilds sleeping sessions from channel topics
- `axi/agents.py`:_set_session_id @ aba7f05 — skips persisting session_id matching `last_failed_resume_id`
- `packages/agenthub/agenthub/lifecycle.py`:wake_agent @ c7267cd — scheduler slot request, SDK client creation with resume fallback
- `packages/agenthub/agenthub/lifecycle.py`:sleep_agent @ c7267cd — disconnect client, release slot, skip if query_lock held
- `packages/agenthub/agenthub/registry.py`:spawn_agent @ 63076db — creates AgentSession, registers in sessions dict
- `packages/agenthub/agenthub/registry.py`:end_session @ c7267cd — disconnect, release slot, close log, pop
- `packages/agenthub/agenthub/registry.py`:rebuild_session @ 63076db — end old session, create fresh sleeping session preserving state
- `packages/agenthub/agenthub/reconnect.py`:reconnect_single @ 6bba9fe — SDK init before bridge subscribe
- `packages/agenthub/agenthub/types.py`:AgentSession @ aba7f05 — `last_failed_resume_id` field
- `axi/hub_wiring.py`:_disconnect_client @ a9b6ce2 — delegates to `claudewire.session.disconnect_client`
- `axi/hub_wiring.py`:_create_client @ 6bba9fe — SDK client creation with bridge transport support
- `axi/prompts.py`:make_spawned_agent_system_prompt @ 507b575 — builds system prompt from cwd, packs, and optional CWD SYSTEM_PROMPT.md

---

## 2. Query Processing & Streaming

### Behaviors
- B2.1: `receive_user_message` is the single entry point for all frontends; it rejects during shutdown, queues during reconnect or busy states, acquires the query lock, wakes sleeping agents, and delegates to `process_message` for the actual turn.
- B2.2: `process_message` sends the user content via `client.query()`, then calls `_stream_with_retry` to consume the SDK response stream through a frontend-provided `stream_handler` callback, all under a configurable `query_timeout`.
- B2.3: `_stream_with_retry` invokes the stream handler once; if it returns an error string (transient), it retries with exponential backoff up to `max_retries`, re-querying with "Continue from where you left off."
- B2.4: `stream_response` (agenthub) is the frontend-agnostic stream engine that iterates raw SDK messages via `receive_response_safe`, tracks activity state, buffers text, and yields normalized `StreamOutput` events (TextDelta, TextFlush, ToolUseStart, QueryResult, etc.).
- B2.5: `stream_response_to_channel` (Discord frontend) renders the stream to Discord with live-edit message editing, typing indicators, thinking indicators, mid-turn message splitting at 1800 chars, and inline response timing appended to the final message.
- B2.6: When a new message arrives for a busy agent, `receive_user_message` queues it and sends a `graceful_interrupt` (SDK control protocol) to abort the current turn early so the queued message processes sooner.
- B2.7: `interrupt_session` sends the procmux "interrupt" command (SIGINT) for bridge-managed agents, calls `transport.stop()` for flowcoder agents, or falls back to SDK `client.interrupt()` for direct-subprocess agents.
- B2.8: `handle_query_timeout` interrupts the CLI, then rebuilds the session via `registry.rebuild_session`, preserving the session_id if available so conversation context survives.
- B2.9: `process_message_queue` drains queued messages sequentially after a turn completes, re-acquiring the query lock for each, and respects both shutdown requests and scheduler yield signals.
- B2.10: `receive_response_safe` iterates raw SDK messages, catches `MessageParseError` for unknown types (logging and skipping them), and terminates on `ResultMessage`.
- B2.11: Text is buffered during streaming and flushed at `end_turn` (message_delta stop_reason), at 1800-char mid-turn splits, on block boundaries, and at stream completion.
- B2.12: `session_id` is captured from the first `StreamEvent` (not just `ResultMessage`) and persisted immediately, so it survives bot crashes before the turn completes.
- B2.13: When the stream ends without a `ResultMessage` (CLI killed or crashed), the `StreamKilled` event is yielded and the Discord frontend force-sleeps the agent so the next message triggers a fresh CLI process.
- B2.14: Thinking indicators are shown on `content_block_start` with type "thinking" and hidden when a non-thinking block starts, on `end_turn`, on error, or on `ResultMessage`.
- B2.15: The visibility check is enforced inside `stream_response_to_channel` so it applies uniformly to both user messages and initial prompts from scheduled agents.
- B2.16: `deliver_inter_agent_message` queues the message at the front of the target's queue and interrupts the busy agent; if the target is idle, it fires a background task to wake and process.
- B2.17: SDK commands like `/clear` and `/compact` must call `session.client.query()` before `_stream_with_retry` to actually send the command to the CLI.
- B2.18: Log context (`log_context.py`) uses contextvars to propagate agent_name, channel_id, trigger, and OTel trace_id through async call chains, injected into every LogRecord via `StructuredContextFilter`.

### Invariants
- I2.1: Message queue size must be read with `len(deque)`, never `deque.qsize()`. [fix: 65171f7]
  - Regression: `collections.deque` has no `qsize()` method; the AttributeError was swallowed by discord.py's event handler, silently dropping every incoming message.
- I2.2: Busy-agent detection must check `is_processing` (or `query_lock.locked()`) before spawning a processing task, even if the agent is already awake. [fix: 855e603]
  - Regression: `wake_or_queue` only checked wake state; a newly awakened but busy agent could get a second competing task that raced for the query lock.
- I2.3: Visibility check must be enforced inside `stream_response_to_channel`, not at the caller. [fix: d153a2d]
  - Regression: The check lived only in `_run_initial_prompt`, so scheduled agents never streamed output even when visibility was set to "all".
- I2.4: Thinking indicator must persist across thinking/non-thinking block transitions within the same turn. [fix: 707544f]
  - Regression: The thinking message was never deleted on block transitions or errors, leaving orphaned indicators.
- I2.5: Bridge subscribe replay must write all buffered messages synchronously (no await between writes) before setting `subscribed=True`. [fix: a08f90e]
  - Regression: Setting `subscribed=True` before replay was complete allowed live relay messages to interleave with buffered replay, producing garbled text.
- I2.6: Bridge readline limit must be at least 10MB; transport must fail fast with `ConnectionError` when the bridge is dead. [fix: f15c5f5]
  - Regression: The 64KB default readline limit caused `LimitOverrunError` on large SDK responses, silently killing the bridge connection.
- I2.7: `session_id` must be captured from the first `StreamEvent`, not only from `ResultMessage`. [fix: 8b21e31]
  - Regression: `session_id` was only recorded at end of turn; if the bot crashed mid-turn, spawned agents lost their session_id.
- I2.8: `interrupt_session` must send procmux "interrupt" (SIGINT), not "kill" (SIGTERM/SIGKILL), to preserve conversation context. [fix: 3b091a9]
  - Regression: Both `messaging.py` and `discord_stream.py` sent "kill", which terminated the CLI process and destroyed the conversation.
- I2.9: For bridge agents, `/stop` must use bridge "kill" (not "interrupt"); agent must be force-slept when stream ends without `ResultMessage`. [fix: b4dd2ae]
  - Regression: `/stop` sent SIGINT which the flowcoder-engine caught and survived; the agent appeared stopped but kept running.
- I2.10: Interrupt must send both SIGINT (process group) and SDK interrupt; SIGINT alone only cancels the current step, not the multi-turn query. [fix: 7e63539]
  - Regression: Sending only bridge SIGINT aborted the current tool call but left the CLI ready for another turn.
- I2.11: CLI must be spawned with `start_new_session=True`; interrupt must use `os.killpg` for process-group SIGINT to reach Task subagents. [fix: cfcfe93]
  - Regression: SIGINT was sent only to the CLI process; background Task subagents continued running.
- I2.12: SDK commands (`/clear`, `/compact`) must call `session.client.query()` before streaming the response. [fix: 72e0093]
  - Regression: `_run_agent_sdk_command` called `_stream_with_retry` without first sending the command, so the commands did nothing.

### Anchors
- `packages/agenthub/agenthub/messaging.py`:receive_user_message @ 63076db — entry point: shutdown/reconnect/busy checks, query_lock, wake, process
- `packages/agenthub/agenthub/messaging.py`:process_message @ 63076db — `client.query(as_stream(content))` then `_stream_with_retry`
- `packages/agenthub/agenthub/messaging.py`:_stream_with_retry @ 63076db — retry loop with exponential backoff
- `packages/agenthub/agenthub/messaging.py`:interrupt_session @ 3b091a9 — procmux "interrupt" command
- `packages/agenthub/agenthub/messaging.py`:graceful_interrupt @ 63076db — `session.client.interrupt()` with 5s timeout
- `packages/agenthub/agenthub/messaging.py`:process_message_queue @ 63076db — drain loop with scheduler yield check
- `packages/agenthub/agenthub/streaming.py`:stream_response @ 63076db — async generator yielding StreamOutput events
- `packages/agenthub/agenthub/streaming.py`:receive_response_safe @ 63076db — MessageParseError handling
- `packages/agenthub/agenthub/streaming.py`:_handle_stream_event @ 63076db — session_id capture, activity tracking
- `axi/discord_stream.py`:stream_response_to_channel @ b4dd2ae — StreamKilled force-sleep
- `axi/discord_stream.py`:_live_edit_tick @ 63076db — throttled Discord message editing with cursor
- `axi/discord_stream.py`:interrupt_session @ 3b091a9 — transport.stop() / procmux "interrupt" / SDK interrupt chain
- `axi/log_context.py`:StructuredContextFilter @ 63076db — injects ctx_agent, ctx_channel, ctx_trigger into LogRecord

---

## 3. Concurrency & Slot Management

### Behaviors
- B3.1: The scheduler maintains a fixed pool of awake slots and blocks incoming agents when all slots are occupied, granting access via a FIFO wait queue.
- B3.2: When all slots are full and an idle (non-busy, non-protected) agent exists, the scheduler evicts it immediately, preferring background agents over interactive ones, selecting the longest-idle first.
- B3.3: When all slots are full and all agents are busy, the scheduler queues the requester and marks a yield target — the busy background agent running longest — to sleep after its current turn completes.
- B3.4: Protected agents (master) are never evicted and never selected as yield targets.
- B3.5: Interactive agents (recently messaged by a user) have higher eviction resistance than background agents; eviction and yield target selection try all background candidates before any interactive ones.
- B3.6: When a slot is released, the next waiter in the FIFO queue is granted the slot and its asyncio.Event is set, unblocking the caller.
- B3.7: After each query completes, the agent checks `should_yield` and, if marked, sleeps instead of processing queued messages, freeing its slot for the waiting agent.
- B3.8: `restore_slot` unconditionally adds an agent to the slot set without eviction or queueing, allowing reconnected agents to exceed `max_slots` temporarily.
- B3.9: `release_slot` is synchronous and safe to call inside the scheduler lock (eviction path), relying on single-threaded asyncio event loop guarantees.
- B3.10: The `bot_creating_channels` guard prevents `on_guild_channel_create` from auto-registering a plain session over a spawn-in-progress.
- B3.11: Config read-modify-write operations (e.g., `set_model`) hold `_config_lock` across the full load-mutate-save cycle to prevent TOCTOU races.
- B3.12: The scheduler `request_slot` has a configurable timeout (default 120s) and raises `ConcurrencyLimitError` if no slot is freed in time.

### Invariants
- I3.1: Every code path that sets `session.client = None` must call `scheduler.release_slot(name)`. [fix: 3d52dd4, c7267cd]
  - Regression: `end_session` disconnected the client without releasing the slot, leaving a phantom slot that could never be freed.
- I3.2: Every code path that sets `session.client` to a live client after reconnect must call `scheduler.restore_slot(name)`. [fix: 3d52dd4]
  - Regression: Bridge reconnect created a new SDK client but did not inform the scheduler, so the slot count drifted.
- I3.3: `restore_slot` must allow exceeding `max_slots` without eviction or blocking. [fix: 3d52dd4]
  - Regression: After a hot restart, more agents may reconnect than `max_slots` allows; using `request_slot` would trigger eviction of running agents.
- I3.4: Spawn signals must be processed immediately after the triggering query completes, not deferred to a periodic scheduler tick. [fix: 00eb310]
  - Regression: Spawn signal was only checked in `check_schedules` (30s interval), causing a 30s delay for auto-switch.
- I3.5: The spawn channel-creation guard must be set before the background task starts and held until `agents[name]` is populated. [fix: 4d42a17]
  - Regression: Gateway `on_guild_channel_create` event could overwrite the real session with a plain one.
- I3.6: A serial queue processor must not impose a fixed timeout on waiting callers. [fix: 4e05688]
  - Regression: A 30s timeout caused false-positive failures when multiple agents called concurrently.
- I3.7: A queue processor must set the caller's event on exception, not only on success. [fix: 7e04eb3]
  - Regression: Exception skipped setting the event, leaving callers blocked indefinitely.
- I3.8: Config read-modify-write must hold a lock across the full cycle (load, mutate, save). [fix: 3a303af]
  - Regression: Lock was only held around save, allowing concurrent callers to overwrite each other (TOCTOU race).

### Anchors
- `packages/agenthub/agenthub/scheduler.py`:Scheduler.request_slot @ 3d52dd4 — slot acquisition with eviction cascade and FIFO wait
- `packages/agenthub/agenthub/scheduler.py`:Scheduler.release_slot @ 3d52dd4 — sync slot release that grants to next waiter
- `packages/agenthub/agenthub/scheduler.py`:Scheduler.restore_slot @ 3d52dd4 — unconditional slot registration for reconnect
- `packages/agenthub/agenthub/scheduler.py`:Scheduler._evict_idle @ 3d52dd4 — background-before-interactive, longest-idle-first
- `packages/agenthub/agenthub/scheduler.py`:Scheduler._select_yield_target @ 3d52dd4 — deferred eviction target
- `packages/agenthub/agenthub/messaging.py`:process_message_queue @ 00eb310 — checks `should_yield` between queued messages
- `axi/agents.py`:spawn_agent @ 4d42a17 — holds `bot_creating_channels` guard
- `axi/agents.py`:end_session @ c7267cd — `scheduler.release_slot(name)`
- `axi/config.py`:set_model @ 3a303af — `_config_lock` held across load-mutate-save

---

## 4. Channel & Guild Management

### Behaviors
- B4.1: On startup, `ensure_guild_infrastructure` creates or syncs three Discord categories (Axi, Active, Killed) with permission overwrites for the bot, allowed users, and the default role.
- B4.2: `ensure_agent_channel` determines category placement based on the agent's cwd: agents whose cwd is within BOT_DIR or BOT_WORKTREES_DIR go to the Axi category, all others go to Active.
- B4.3: Channels found in the Killed category are moved back to the appropriate live category (Axi or Active) when the agent is re-ensured.
- B4.4: Channels found in the wrong live category (e.g., Active when they should be in Axi) are moved to the correct one during `ensure_agent_channel`.
- B4.5: `deduplicate_master_channel` runs once at startup before master channel setup, deleting duplicate axi-master channels and preferring the uncategorized (pinned-to-top) or Axi-category survivor.
- B4.6: `ensure_master_channel_position` pins #axi-master to position 0 with no parent category (top of the server) via a direct REST API PATCH call.
- B4.7: Channel status emoji prefixes (working, idle, done, error, plan_review, question, custom) are prepended to channel names via a debounced rename batch that respects a 5-minute per-channel cooldown.
- B4.8: Channel recency reordering sorts channels within Axi and Active categories by last activity (most-recent-first), with axi-master always first, using a 60-second debounced bulk API call.
- B4.9: `move_channel_to_killed` strips any status emoji prefix before moving a channel to the Killed category, and searches both Axi and Active categories.
- B4.10: `reconstruct_agents_from_channels` rebuilds sleeping AgentSession entries from existing channels in Axi and Active categories at startup, parsing cwd and session metadata from channel topics.
- B4.11: Channel topic updates during spawn and session-id changes are fire-and-forget to avoid blocking on Discord's channel-edit rate limit.
- B4.12: `axi_test up` reserves a guild/bot-token slot atomically under an exclusive file lock, writes a non-sensitive .env, and the bot resolves its token at startup from the slots file.
- B4.13: `axi_test down` stops the systemd service (if running) and releases the slot reservation atomically under the same file lock.
- B4.14: `axi_test list` runs a health check that removes orphaned reservations (worktree directory gone) and displays all slots with guild, mode, branch, and status.
- B4.15: Slot reservation uses `_find_free_guild` to select the first guild whose bot token is not claimed by any other slot, preventing double-allocation.

### Invariants
- I4.1: Channel category placement must be based on agent cwd (`_is_axi_cwd` checks BOT_DIR/BOT_WORKTREES_DIR); channels in the wrong category must be moved. [fix: e1b8466]
  - Regression: All agent channels were placed in Active regardless of cwd; `move_channel_to_killed` only searched Active, missing channels in Axi.
- I4.2: Channel topic updates must be fire-and-forget; asyncio tasks must be stored in a `_background_tasks` set to prevent GC under Python 3.12+ weak-ref semantics. [fix: 9c3bed2]
  - Regression: Synchronous `channel.edit(topic=...)` blocked spawn indefinitely when Discord's rate limit was exhausted; bare `asyncio.create_task()` risked silent GC.
- I4.3: Slot allocation conflict detection must check all allocated instances (`.env` or slot reservation exists), not just running ones. [fix: 1fbe3da]
  - Regression: Stopped instances' token reservations were invisible, allowing two instances to get the same bot token.
- I4.4: `cmd_up` must check systemd service status before refusing reuse; stale `.env` and failed-state services must be auto-cleaned. [fix: a109a15]
  - Regression: OOM kills left `.env` files behind; `cmd_up` refused reuse, requiring manual cleanup.
- I4.5: Token resolution for test worktrees must use slot-based lookup first, then DISCORD_TOKEN env var, then sender_token. [fix: c7134a3]
  - Regression: Agents in test worktrees inherited the parent process's DISCORD_TOKEN, targeting the wrong bot.
- I4.6: All Discord REST API calls must go through the httpx `AsyncDiscordClient` wrapper, not raw `discord_request` calls. [fix: 8d036d1]
  - Regression: Direct calls bypassed URL encoding, retry logic, and error handling.

### Anchors
- `axi/channels.py`:_is_axi_cwd @ e1b8466 — `real.startswith((bot_real + os.sep, worktrees_real + os.sep))`
- `axi/channels.py`:ensure_agent_channel @ e1b8466 — cwd-based category selection
- `axi/channels.py`:ensure_guild_infrastructure @ e1b8466 — creates Axi/Active/Killed categories
- `axi/channels.py`:deduplicate_master_channel @ e1b8466 — prefers uncategorized, then Axi category
- `axi/channels.py`:ensure_master_channel_position @ 8d036d1 — REST PATCH to position 0
- `axi/channels.py`:reorder_channels_by_recency @ e1b8466 — sorted by last activity
- `axi/channels.py`:move_channel_to_killed @ e1b8466 — searches both Axi and Active categories
- `axi/agents.py`:fire_and_forget @ 9c3bed2 — `_background_tasks.add(task)` with done callback
- `axi/agents.py`:reconstruct_agents_from_channels @ e1b8466 — parses channel topics for cwd/session_id
- `axi/axi_test.py`:_try_reserve @ a109a15 — auto-clean stale reservation
- `axi/axi_test.py`:_find_free_guild @ 1fbe3da — checks used_tokens set

---

## 5. Rate Limiting & Quota Tracking

### Behaviors
- B5.1: `parse_rate_limit_seconds` extracts a wait duration from API error text, supporting relative formats ("in N seconds/minutes/hours", "retry after N") and falling back to 300s when no pattern matches.
- B5.2: `is_rate_limited` checks `rate_limited_until` against current time and auto-clears expired limits.
- B5.3: `handle_rate_limit` sets the global rate-limit deadline, emits a notification via broadcast callback, and schedules a delayed expiry notification — but only on the first hit (suppresses duplicates while already limited).
- B5.4: `update_rate_limit_quota` ingests `rate_limit_info` events from the stream and upserts per-type quota state (status, resets_at, utilization), appending each event to a persistent JSONL history log.
- B5.5: `record_session_usage` accumulates per-session cost, token, turn, and duration stats in memory and appends each query result to a JSONL usage history file.
- B5.6: `notify_rate_limit_expired` sleeps until the rate limit expires, then sends a recovery message to the master channel only if the limit has actually cleared.
- B5.7: When the stream detects a `rate_limit` or `billing_error` in an `AssistantMessage`, it invokes the rate limit handler, sets `hit_rate_limit=True`, and suppresses all further text flushing for that stream.
- B5.8: `stream_response_to_channel` returns a distinguishable value when a rate limit is hit, so `stream_with_retry` does not retry.
- B5.9: The rate-limit deadline is only extended (never shortened): a new limit replaces the current one only if the new expiry is later.

### Invariants
- I5.1: Rate limit notifications must only go to the triggering channel and master channel, not broadcast to all agent channels. [fix: 1396ad4]
  - Regression: Notifications were broadcast to every agent's channel, spamming unrelated conversations.
- I5.2: `stream_with_retry` must not retry when the error is a rate limit. [fix: 1396ad4]
  - Regression: Rate-limited responses were retried like transient errors, causing repeated failures.
- I5.3: When an "allowed" rate_limit_event arrives without a utilization value for the same reset window, the previous utilization must be preserved; utilization resets only on window rollover. [fix: 60f8713]
  - Regression: Each event without utilization overwrote the stored value with `None`, causing `/claude-usage` to show "?".

### Anchors
- `axi/rate_limits.py`:parse_rate_limit_seconds @ 63076db — regex extraction of wait duration
- `axi/rate_limits.py`:is_rate_limited @ 63076db — auto-clear on expiry
- `axi/rate_limits.py`:handle_rate_limit @ 63076db — first-hit notification with broadcast
- `axi/rate_limits.py`:update_rate_limit_quota @ 63076db — preserves utilization on same window
- `axi/rate_limits.py`:record_session_usage @ 63076db — JSONL usage history append
- `axi/discord_stream.py`:_handle_assistant_message @ 63076db — rate_limit/billing_error detection
- `packages/agenthub/agenthub/rate_limits.py`:update_rate_limit_quota @ 63076db — same utilization preservation logic

---

## 6. Process & Bridge Management

### Behaviors
- B6.1: ProcmuxServer listens on a Unix socket, spawns named subprocesses with piped stdin/stdout/stderr, and multiplexes their I/O over a single client connection.
- B6.2: When the client is disconnected (or not subscribed), the server buffers all stdout/stderr/exit messages per-process and replays them on subscribe.
- B6.3: ProcmuxConnection runs a demux loop that routes incoming server messages to per-process queues (stdout/stderr/exit) or a shared command-response queue.
- B6.4: The `ensure_running` helper attempts to connect to an existing procmux server, and if unavailable, starts one as a detached subprocess and polls until the socket accepts connections.
- B6.5: BridgeTransport implements the SDK Transport interface by delegating spawn/subscribe/kill/stdin to a ProcessConnection, and yields parsed JSON dicts from an async event queue.
- B6.6: For reconnecting agents, BridgeTransport intercepts the `initialize` control_request and injects a fake success response into the local queue, avoiding a redundant CLI initialization.
- B6.7: BridgeTransport.stop() injects an ExitEvent into the local queue to unblock read_messages() immediately and schedules the actual process kill as a background task.
- B6.8: ProcmuxProcessConnection adapts procmux's message types to claudewire's event types via a _TranslatingQueue that converts StdoutMsg/StderrMsg/ExitMsg to StdoutEvent/StderrEvent/ExitEvent.
- B6.9: DirectProcessConnection spawns local PTY subprocesses without procmux, providing a zero-overhead single-process backend that satisfies the same ProcessConnection protocol.
- B6.10: The server tracks per-process idle state by comparing monotonic timestamps of the last stdin write vs. the last stdout message.
- B6.11: Process kill sends SIGTERM to the entire process group (via `os.killpg`), then escalates to SIGKILL after a 5-second timeout.
- B6.12: Only one client connection is allowed at a time; a new connection drops the previous one and unsubscribes all processes.

### Invariants
- I6.1: All socket readline limits (server listener, client connection, subprocess pipes) must be 10 MB, not the asyncio default of 64 KB. [fix: f15c5f5]
  - Regression: Large Claude SDK JSON responses exceeded 64 KB, causing LimitOverrunError that silently killed the demux loop.
- I6.2: BridgeTransport.write() and read_messages() must fail fast with ConnectionError when the process connection is dead. [fix: f15c5f5]
  - Regression: When the bridge died, write/read continued without error, causing queries to hang indefinitely.
- I6.3: Subprocess buffer limit must be 10 MB (matching socket limits) to handle large Claude SDK JSON lines. [fix: 7b724c8]
  - Regression: The subprocess stdout pipe used the default 64 KB buffer; large JSON lines caused LimitOverrunError.
- I6.4: When the stdout relay crashes but the process is still alive, `_relay_stdout` must not block forever on `proc.wait()`. [fix: 7b724c8]
  - Regression: A crashed stdout relay called `await proc.wait()` unconditionally, blocking forever because the subprocess was still running.
- I6.5: In `_cmd_subscribe`, all buffered messages must be written synchronously before setting `subscribed=True`; drain() happens only after all writes and the flag flip. [fix: a08f90e, f0500fc]
  - Regression: Setting `subscribed=True` first allowed live relay messages to interleave with buffered replay, causing garbled output.
- I6.6: During shutdown, the server must force-close the client writer before calling `server.wait_closed()`, and `wait_closed()` must have a timeout. [fix: 6e33e05]
  - Regression: `server.wait_closed()` blocked waiting for the client handler to exit, causing a 15-second restart delay.
- I6.7: The supervisor must escalate SIGTERM to SIGKILL on bridge processes that do not exit within 5 seconds. [fix: 6e33e05]
  - Regression: The supervisor sent SIGTERM but never escalated; a hung bridge survived indefinitely.

### Anchors
- `packages/procmux/procmux/server.py`:ProcmuxServer.start @ 09f09eb — `limit=10 * 1024 * 1024`
- `packages/procmux/procmux/server.py`:ProcmuxServer._cmd_spawn @ 09f09eb — subprocess buffer limit
- `packages/procmux/procmux/server.py`:ProcmuxServer._cmd_subscribe @ 09f09eb — synchronous write loop then `subscribed = True`
- `packages/procmux/procmux/server.py`:ProcmuxServer._shutdown @ 6e33e05 — force-close writer before wait_closed
- `packages/procmux/procmux/server.py`:ProcmuxServer._relay_stdout @ 09f09eb — `wait_for(proc.wait(), timeout=10.0)` only on normal_eof
- `packages/procmux/procmux/server.py`:ProcmuxServer._kill_process @ 09f09eb — SIGTERM then SIGKILL escalation
- `packages/procmux/procmux/helpers.py`:connect @ 09f09eb — `limit=10 * 1024 * 1024`
- `packages/claudewire/claudewire/transport.py`:BridgeTransport.write @ 09f09eb — `raise ConnectionError("Process connection is dead")`
- `packages/claudewire/claudewire/transport.py`:BridgeTransport.read_messages @ 09f09eb — ConnectionError on dead bridge
- `packages/claudewire/claudewire/transport.py`:BridgeTransport.stop @ 09f09eb — ExitEvent injection + background kill
- `packages/agenthub/agenthub/procmux_wire.py`:_TranslatingQueue.get @ 09f09eb — StdoutMsg→StdoutEvent translation

---

## 7. Permissions & Tool Gating

### Behaviors
- B7.1: Policy chains evaluate in order; the first policy to return a non-None result (Allow or Deny) wins, and if all return None the tool call is allowed by default.
- B7.2: `tool_block_policy` denies tools whose name appears in a blocked set (default: Skill, EnterWorktree, Task) with a static message.
- B7.3: `tool_allow_policy` auto-allows tools whose name appears in an allowed set (default: TodoWrite, EnterPlanMode), bypassing downstream policies.
- B7.4: `cwd_policy` restricts file-write tools (Edit, Write, MultiEdit, NotebookEdit) to resolved allowed base paths, denying any write whose path resolves outside them.
- B7.5: `compute_allowed_paths` grants every agent write access to its own cwd and user-data directory, and additionally grants code agents (cwd inside BOT_DIR or worktrees dir) access to the worktrees directory and admin-allowed paths.
- B7.6: `build_permission_callback` composes the full chain: block -> auto-allow -> optional plan-approval hook (ExitPlanMode) -> optional question hook (AskUserQuestion) -> cwd restriction -> default allow.
- B7.7: `discord_send_message` blocks sends to any channel that maps to a registered agent name, returning an error directing the agent to use normal text output.
- B7.8: `discord_send_file` auto-resolves the calling agent's channel by scanning `channel_to_agent` for a session whose `query_lock` is currently held; if no match, returns an error asking for explicit `channel_id`.
- B7.9: The master agent receives the `discord_mcp_server` only when `BOT_WORKTREES_DIR` exists on disk; spawned agents never receive it.
- B7.10: `sdk_mcp_servers_for_cwd` gives admin agents (cwd inside BOT_DIR) the `axi_mcp_server` (spawn/kill/restart); non-admin agents get only utils, schedule, and playwright.
- B7.11: `_build_mcp_servers` assembles the base MCP server set and merges any `extra_mcp_servers` loaded from per-agent config or spawn arguments.
- B7.12: Both `spawn_agent` and `reconstruct_agents_from_channels` call `_build_mcp_servers`, ensuring reconstructed agents get the same tool access as freshly spawned ones.
- B7.13: The master agent gets `axi_master_mcp_server` (including axi_send_message and axi_restart), while spawned admin agents get the narrower `axi_mcp_server`.

### Invariants
- I7.1: Agents must not send Discord messages to their own channel via `discord_send_message`; responses are delivered by the streaming layer and MCP self-sends cause double messages. [fix: 900587f]
  - Regression: The agent used `discord_send_message` to post to its own channel, duplicating every response.
- I7.2: `discord_send_file` must not rely on ContextVar for caller resolution because MCP tools execute in a separate async context where the ContextVar is unset. [fix: 46e867c]
  - Regression: ContextVar set in `stream_response_to_channel` was invisible to the MCP tool's async context.
- I7.3: MCP servers must be wired into both `spawn_agent` and `reconstruct_agents_from_channels`. [fix: c35331c]
  - Regression: `reconstruct_agents_from_channels` hardcoded only utils+schedule, so admin agents lost spawn/kill tools after restart.

### Anchors
- `packages/claudewire/claudewire/permissions.py`:compose @ 09f09eb — first non-None result wins
- `packages/claudewire/claudewire/permissions.py`:cwd_policy @ 09f09eb — restricts write tools to allowed paths
- `packages/claudewire/claudewire/permissions.py`:tool_block_policy @ 09f09eb — static deny for blocked tools
- `packages/agenthub/agenthub/permissions.py`:build_permission_callback @ 09f09eb — full policy chain composition
- `packages/agenthub/agenthub/permissions.py`:compute_allowed_paths @ 09f09eb — cwd-based path grants
- `axi/tools.py`:discord_send_message @ 09f09eb — self-send blocking
- `axi/tools.py`:discord_send_file @ 09f09eb — query_lock-based auto-resolve
- `axi/tools.py`:sdk_mcp_servers_for_cwd @ 09f09eb — admin vs non-admin MCP sets
- `axi/agents.py`:_build_mcp_servers @ 09f09eb — base + extra merge

---

## 8. Scheduling & Cron Jobs

### Behaviors
- B8.1: The `check_schedules` loop runs every 10 seconds, computing both `now_utc` and `now_local` (in `SCHEDULE_TIMEZONE`), then delegates to `_fire_schedules` for cron/one-off evaluation.
- B8.2: Recurring schedules use `croniter(cron_expr, now_local).get_prev(datetime)` to find the most recent cron tick in local time, firing only when that tick exceeds the last recorded fire time.
- B8.3: One-off schedules fire when `datetime.fromisoformat(entry["at"]) <= now_utc`, then are removed from `schedules.json`.
- B8.4: When a schedule fires, routing resolves the target agent via `entry.get("owner") or entry.get("session") or name` — if the agent exists it receives the prompt, otherwise a new agent is spawned.
- B8.5: Each agent gets a per-agent schedule MCP server exposing `schedule_list`, `schedule_create`, and `schedule_delete` tools, all scoped to that agent's `owner` field.
- B8.6: `schedule_create` validates name format (lowercase alphanumeric + hyphens, max 50 chars), prompt non-empty (max 2000 chars), cron validity, and one-off datetimes must be timezone-aware and in the future.
- B8.7: Schedule names must be unique per agent (enforced by `owner` + `name`), with a per-agent limit of 20 schedules.
- B8.8: `schedule_delete` requires both `name` and `owner` to match, preventing agents from deleting each other's schedules.
- B8.9: Recurring schedule fires are recorded in `schedule_history.json` with 5-minute dedup to prevent duplicate history entries.
- B8.10: `prune_history` removes history entries older than 7 days on each scheduler tick.
- B8.11: On first encounter of a schedule key (e.g. after restart), `schedule_last_fired` is initialized to `last_occurrence` (most recent cron tick), suppressing catch-up fires.
- B8.12: All schedule file I/O is serialized through `schedules_lock` to prevent read-modify-write races.
- B8.13: The master agent is registered with schedule and utils MCP servers so it can create and manage its own schedules.

### Invariants
- I8.1: Schedule routing must use the `owner` field to determine the target agent, not the legacy `session` field. [fix: 5a5e0a9]
  - Regression: Schedules fired to orphan agents derived from the `session` field.
- I8.2: `schedule_last_fired` must be initialized to `last_occurrence` (most recent cron tick), not `datetime.min`. [fix: d9eaf35]
  - Regression: After restart, every recurring schedule fired immediately because `datetime.min` was always less than `croniter.get_prev()`.
- I8.3: All datetime comparisons in the scheduler must use timezone-aware datetimes, never naive `datetime.now()`. [fix: d850d70]
  - Regression: Naive `datetime.now()` caused `TypeError` on comparison with timezone-aware stored datetimes.
- I8.4: Cron expressions must be written and evaluated in `SCHEDULE_TIMEZONE` (local time), not UTC. [fix: b545ac4]
  - Regression: Agents wrote cron expressions assuming UTC, so events fired at the wrong local time.
- I8.5: Recurring schedule fires must be recorded in history with dedup. [fix: 5a5e0a9]
  - Regression: Recurring fires were not recorded in history at all.
- I8.6: The master agent must have schedule and utils MCP servers registered. [fix: 5a5e0a9]
  - Regression: Master agent was missing schedule MCP server entirely.

### Anchors
- `axi/main.py`:_fire_schedules @ 63076db — `croniter(cron_expr, now_local).get_prev(datetime)`
- `axi/main.py`:check_schedules @ 63076db — `now_local = datetime.now(config.SCHEDULE_TIMEZONE)`
- `axi/schedule_tools.py`:schedule_key @ 63076db — `"{owner}/{name}"` unique key
- `axi/schedule_tools.py`:append_history @ 63076db — 5-minute dedup
- `axi/schedule_tools.py`:handle_schedule_create @ 63076db — validation and per-agent limit
- `axi/schedule_tools.py`:make_schedule_mcp_server @ 63076db — `"owner": agent_name` in entry
- `axi/main.py`:_register_master_agent @ 63076db — schedule MCP for master

---

## 9. Logging & Observability

### Behaviors
- B9.1: `StructuredContextFilter` injects `ctx_agent`, `ctx_channel`, `ctx_trigger`, `ctx_trace`, and `ctx_prefix` into every `LogRecord` that passes through a handler bearing the filter.
- B9.2: `LogContext` propagates automatically through async child tasks via `contextvars.ContextVar`, so log lines inside spawned coroutines inherit the parent's agent name, channel, and trigger.
- B9.3: `set_trigger` formats the trigger string as `"{type}:{detail}"` when a detail kwarg is provided, or bare `"{type}"` otherwise.
- B9.4: `format_prefix` appends a truncated 16-hex-char trace ID from the active OpenTelemetry span, omitting it when no span is active.
- B9.5: The root `"axi"` logger writes to both a color-formatted console handler (level from `LOG_LEVEL` env, default `INFO`) and a rotating file handler (`DEBUG`, 10 MB x 3 backups).
- B9.6: The `StructuredContextFilter` is installed on handlers (not the logger itself) so child loggers that propagate also get context injection.
- B9.7: Each agent session gets a dedicated per-agent logger writing to `<LOG_DIR>/<name>.log` via a `RotatingFileHandler` (5 MB x 2 backups) with propagation disabled.
- B9.8: `init_tracing` configures an OpenTelemetry `TracerProvider` with OTLP/gRPC export, gracefully degrading when Jaeger is unavailable.
- B9.9: The `@traced` decorator wraps an async function in an OTel span, records exceptions with `ERROR` status, and re-raises.
- B9.10: `AgentLog` is an append-only per-agent event store that notifies subscribers in order and optionally persists each `LogEvent` as a JSONL line.
- B9.11: `AgentLog.replay` returns all events (or those since a given datetime), enabling frontends to catch up after connecting to a running agent.
- B9.12: All log formatters use `time.gmtime` as their converter so timestamps are in UTC regardless of system timezone.

### Invariants
- I9.1: Every module's logger variable must be named consistently and reference the correct logger for that module. [fix: a7f952d]
  - Regression: A module used `logger.debug(...)` but declared `log = logging.getLogger(...)`, causing `NameError` at runtime.
- I9.2: `StructuredContextFilter.filter` must always return `True` and set all `ctx_*` attributes, because formatters unconditionally reference `%(ctx_prefix)s`.
- I9.3: Per-agent loggers must set `propagate = False` to prevent duplicate log lines from reaching root handlers.

### Anchors
- `axi/log_context.py`:StructuredContextFilter.filter @ 63076db — sets ctx_prefix on every record
- `axi/log_context.py`:set_agent_context @ 63076db — creates LogContext with agent_name, channel_id
- `axi/log_context.py`:set_trigger @ 63076db — `"{type}:{detail}"` format
- `axi/config.py`:module-level @ 63076db — console + file handler setup with StructuredContextFilter
- `axi/axi_types.py`:setup_agent_log @ 63076db — `logger.propagate = False`
- `axi/tracing.py`:init_tracing @ 63076db — TracerProvider with OTLP export
- `axi/tracing.py`:traced @ 63076db — span.record_exception on error
- `packages/agenthub/agenthub/agent_log.py`:AgentLog.append @ 63076db — persist + notify subscribers
- `packages/agenthub/agenthub/agent_log.py`:AgentLog.replay @ 63076db — events since datetime

---

## 10. Discord Rendering & UI

### Behaviors
- B10.1: When `STREAMING_DISCORD` is enabled, text deltas are accumulated into a live-edited Discord message with a block cursor (`█`) appended to indicate ongoing output.
- B10.2: Live-edit updates are throttled to one edit per `STREAMING_EDIT_INTERVAL` seconds (default 1.5s) to stay within Discord's per-channel rate limit.
- B10.3: When a live-edit message exceeds 1900 characters, it is finalized at the nearest newline and a new message is started for the remainder.
- B10.4: When `STREAMING_DISCORD` is disabled, text is buffered and sent via `send_long`, which splits at 2000-character boundaries preferring newline split points.
- B10.5: In non-streaming mode, the last message is deferred so that response timing can be appended inline before sending.
- B10.6: A `*thinking...*` indicator message is posted when a `thinking` content block starts and deleted when a non-thinking block begins or the turn ends.
- B10.7: The `channel.typing()` context manager runs for the entire stream; its task is cancelled on result, rate limit, or error.
- B10.8: Response timing (e.g. `-# 4.2s`) is appended as a small-text suffix to the final message.
- B10.9: Plan approval posts the plan as a file attachment, adds approve/reject reaction buttons, and blocks the permission callback until the user reacts or types feedback.
- B10.10: After plan approval or rejection, the unchosen reaction emoji is removed for visual clarity.
- B10.11: `AskUserQuestion` posts questions one at a time, pre-adds numbered keycap reactions for each option, and waits for either a reaction or typed answer.
- B10.12: `TodoWrite` completions are rendered with status icons and persisted to disk.
- B10.13: In debug mode, thinking blocks are uploaded as `thinking.md` file attachments, and tool uses are shown as inline code previews.
- B10.14: Messages that complete processing get a checkmark reaction; queued messages get a mailbox; errors get an X.
- B10.15: Compaction events show a spinner when compaction starts and a completion message with token count when done.
- B10.16: On Discord edit rate limit (HTTP 429), the live-edit backs off by the `retry_after` duration.
- B10.17: On transient API errors, the stream retries with exponential backoff up to `API_ERROR_MAX_RETRIES`.
- B10.18: If the stream ends without a `ResultMessage`, in-flight content is flushed and the agent is force-slept.
- B10.19: `resolve_reaction_answer` maps keycap emoji to option labels; unrecognized emoji are ignored.
- B10.20: `DiscordFrontend` is a thin adapter wrapping module-level functions into the Frontend protocol.

### Invariants
- I10.1: The thinking indicator must be hidden on every exit path — non-thinking block start, end_turn, AssistantMessage, and ResultMessage. [fix: 707544f]
  - Regression: Thinking indicator was only deleted at `end_turn`; a thinking→text transition left the indicator persisted alongside response text.
- I10.2: Rate-limited messages must receive X (not checkmark) as their completion reaction, and `stream_with_retry` must not retry. [fix: 1396ad4]
  - Regression: Rate limits returned the same value as success, causing checkmark reaction and retry attempts.
- I10.3: Multi-question `AskUserQuestion` prompts must collect answers sequentially, not attempt to split a single message across all questions. [fix: 5407b76]
  - Regression: All questions were posted at once and the handler tried to split one reply across all, mismatching answers.

### Anchors
- `axi/discord_stream.py`:_live_edit_tick @ 707544f — throttled message editing with cursor
- `axi/discord_stream.py`:_live_edit_finalize @ 707544f — removes cursor, splits if needed
- `axi/discord_stream.py`:_show_thinking @ 707544f — posts thinking indicator
- `axi/discord_stream.py`:_hide_thinking @ 707544f — deletes thinking indicator
- `axi/discord_stream.py`:_handle_stream_event @ 707544f — dispatches content blocks to UI handlers
- `axi/discord_stream.py`:_flush_text @ 707544f — finalizes live-edit or sends buffered
- `axi/discord_stream.py`:stream_response_to_channel @ 1396ad4 — distinguishes rate-limit from success
- `axi/discord_stream.py`:stream_with_retry @ 1396ad4 — short-circuits on rate limit
- `axi/discord_ui.py`:_handle_ask_user_question @ 5407b76 — sequential question collection
- `axi/discord_ui.py`:_handle_exit_plan_mode @ 5407b76 — plan file attachment + reaction gate
- `axi/discord_ui.py`:resolve_reaction_answer @ 5407b76 — keycap→label mapping
- `axi/discord_frontend.py`:DiscordFrontend @ 5407b76 — thin adapter for Frontend protocol

---

## 11. Shutdown & Restart

### Behaviors
- B11.1: Graceful shutdown polls every 5s for busy agents, waiting up to 5 minutes, then sleeps all agents, closes the Discord bot, and calls the kill function.
- B11.2: Force shutdown skips the busy-agent wait and immediately executes the exit sequence; it can escalate a graceful shutdown in progress.
- B11.3: A daemon "safety deadline" thread calls `os._exit(42)` after 30s if the exit phase hangs.
- B11.4: `kill_supervisor()` sends SIGTERM to the parent supervisor process, pauses 0.5s, then calls `os._exit(42)`.
- B11.5: `exit_for_restart()` calls `os._exit(42)` without killing the supervisor, used in bridge mode so CLI subprocesses survive.
- B11.6: The supervisor interprets exit code 42 as a restart request; exit code 0 or signal-killed causes clean stop.
- B11.7: SIGTERM/SIGINT to the supervisor forwards the signal to bot.py, then after exit kills the bridge and calls `sys.exit(0)`.
- B11.8: SIGHUP triggers hot restart: forwards SIGTERM to bot.py but leaves the bridge alive, then relaunches.
- B11.9: In bridge mode, graceful shutdown skips the busy-agent wait and does not sleep agents; they keep running in the bridge.
- B11.10: Duplicate graceful shutdown requests are ignored (idempotent).
- B11.11: `bot.close()` is wrapped in `wait_for` with a 10s timeout; on timeout, shutdown proceeds to kill.
- B11.12: `sleep_all` swallows per-agent exceptions so one broken agent does not prevent cleanup.
- B11.13: The supervisor distinguishes startup crashes (<60s uptime) from runtime crashes and applies different recovery strategies.
- B11.14: After 3 consecutive crashes, the supervisor stops relaunching and exits.

### Invariants
- I11.1: The supervisor must escalate SIGTERM to SIGKILL on bridge processes that do not exit within 5 seconds. [fix: 6e33e05]
  - Regression: SIGTERM without escalation caused hung bridge to survive indefinitely, delaying restarts.
- I11.2: Bridge shutdown must force-close the client writer before calling `wait_closed()` on the server. [fix: 6e33e05]
  - Regression: `wait_closed()` blocked while the client connection was still open.
- I11.3: Only one bot.py instance may run at a time, enforced by an exclusive `fcntl` file lock on `.bot.lock`. [fix: 7b724c8]
  - Regression: Supervisor could launch a second bot.py before the first fully exited, causing duplicate messages.
- I11.4: `ensure_default_files` must seed `schedules.json` and `schedule_history.json` in `AXI_USER_DATA`, not `BOT_DIR`. [fix: 1c1c04a]
  - Regression: Default files were written to the wrong directory; `config.py` reads from `AXI_USER_DATA`.

### Anchors
- `axi/shutdown.py`:ShutdownCoordinator.graceful_shutdown @ 63076db — idempotent guard, poll loop with 300s timeout
- `axi/shutdown.py`:ShutdownCoordinator._execute_exit @ 63076db — safety deadline thread before sleep/close/kill
- `axi/shutdown.py`:kill_supervisor @ 63076db — SIGTERM to parent then `os._exit(42)`
- `axi/shutdown.py`:exit_for_restart @ 63076db — `os._exit(42)` without supervisor kill
- `axi/supervisor.py`:_stop_handler @ 63076db — forwards signal to bot.py
- `axi/supervisor.py`:_hup_handler @ 63076db — hot restart: SIGTERM to bot.py, bridge stays
- `axi/supervisor.py`:_kill_bridge @ 63076db — SIGTERM→SIGKILL escalation
- `axi/supervisor.py`:main @ 63076db — restart loop with crash counting
- `axi/main.py`:_acquire_lock @ 63076db — `fcntl.flock(lock_fd, LOCK_EX | LOCK_NB)`

---

## 12. Configuration & Model Selection

### Behaviors
- B12.1: `get_model` returns the active model name, checking `AXI_MODEL` env var first and falling back to `config.json` (default "opus").
- B12.2: `set_model` validates model name against `VALID_MODELS` (haiku/sonnet/opus), then persists to `config.json` under `_config_lock`.
- B12.3: Test instances set `AXI_MODEL=haiku` in their `.env`, overriding `config.json`.
- B12.4: `_make_agent_options` calls `get_model()` at wake time so each session picks up the current model.
- B12.5: `_post_model_warning` posts a Discord warning when the active model is not opus.
- B12.6: Packs are loaded once at import time from `packs/<name>/prompt.md` into `_PACKS` dict; unknown names are skipped with a warning.
- B12.7: `make_spawned_agent_system_prompt` builds layered prompts: axi-dev agents get SOUL + dev_context + axi-dev packs; non-admin agents get a mini agent context prompt.
- B12.8: `SYSTEM_PROMPT.md` in an agent's CWD is auto-loaded and either appended or overwrites the base prompt if it contains `<!-- mode: overwrite -->`.
- B12.9: `axi_spawn_agent` accepts optional `packs` and `mcp_servers` parameters; `packs=None` uses defaults, `packs=[]` disables packs entirely.
- B12.10: `load_mcp_servers` reads named server configs from `mcp_servers.json` in `AXI_USER_DATA`; unknown names are logged and skipped.
- B12.11: Per-agent config (packs and MCP server names) is persisted to `AXI_USER_DATA/agents/<name>/agent_config.json` at spawn and reloaded during restart/reconstruction.
- B12.12: `restart_agent` rebuilds the system prompt from saved packs and current CWD, preserving session ID and compact_instructions.
- B12.13: Agent reconstruction at startup loads saved packs and MCP server names from per-agent config.
- B12.14: `SCHEDULES_PATH` and `MCP_SERVERS_PATH` are derived from `AXI_USER_DATA`.
- B12.15: Feature flags (`STREAMING_DISCORD`, `CHANNEL_STATUS_ENABLED`, `CLEAN_TOOL_MESSAGES`, `WEB_ENABLED`) are read from env vars at import time with falsy defaults.
- B12.16: `ALLOWED_CWDS` is assembled from env vars plus `AXI_USER_DATA`, `BOT_DIR`, and `BOT_WORKTREES_DIR`; spawn validates agent CWD against this list.
- B12.17: `compute_prompt_hash` produces a 16-char SHA-256 prefix of the system prompt text, used to detect prompt drift between spawn and resume.

### Invariants
- I12.1: `set_model` must hold `_config_lock` across the entire load-mutate-save cycle. [fix: 3a303af]
  - Regression: Lock was only held around save, leaving a TOCTOU race where concurrent `/model` calls could overwrite each other.
- I12.2: Specialized agents must always rebuild their correct system prompt (including role-specific prompt files and packs) on restart or reconstruction. [fix: e21c15e]
  - Regression: `_ensure_record_handler` only fixed the CWD, leaving the generic prompt which stripped role-specific instructions and packs.
- I12.3: `ensure_default_files` must seed config files in `AXI_USER_DATA`, not `BOT_DIR`. [fix: 1c1c04a]
  - Regression: Schedule files were seeded in the wrong directory.

### Anchors
- `axi/config.py`:get_model @ 09f09eb — env override then config.json fallback
- `axi/config.py`:set_model @ 09f09eb — `_config_lock` across load-mutate-save
- `axi/config.py`:load_mcp_servers @ 09f09eb — reads from MCP_SERVERS_PATH
- `axi/prompts.py`:make_spawned_agent_system_prompt @ 09f09eb — layered prompt assembly
- `axi/prompts.py`:_load_cwd_prompt @ 09f09eb — `<!-- mode: overwrite -->` detection
- `axi/prompts.py`:_load_packs @ 09f09eb — pack loading from packs/ directory
- `axi/tools.py`:axi_spawn_agent @ 09f09eb — packs and mcp_servers parameter handling
- `axi/agents.py`:spawn_agent @ 09f09eb — `_save_agent_config(name, mcp_names, packs=packs)`
- `axi/agents.py`:restart_agent @ 09f09eb — rebuilds prompt from saved packs
- `axi/hub_wiring.py`:_make_agent_options @ 09f09eb — `model=config.get_model()`
- `axi/supervisor.py`:ensure_default_files @ 09f09eb — seeds in AXI_USER_DATA

---

## 13. Hot Restart & Bridge Reconnection

### Behaviors
- B13.1: SIGHUP to the supervisor kills only bot.py (SIGTERM) and relaunches it, leaving the procmux bridge alive so CLI processes survive the restart.
- B13.2: On startup, `connect_procmux` connects to the bridge, lists surviving agents, and fires a concurrent `reconnect_single` task for each one that has a matching session.
- B13.3: Bridge agents with no matching session are killed as orphans via `conn.send_command("kill")`.
- B13.4: `reconnect_single` creates a `BridgeTransport(reconnecting=True)`, initializes the SDK client, and only then subscribes to the bridge (which replays buffered messages).
- B13.5: `BridgeTransport.write` intercepts the `initialize` control_request when `reconnecting=True`, enqueues a fake success `control_response`, and clears the reconnecting flag — no bytes reach the already-initialized CLI.
- B13.6: If the bridge reports `cli_status == "exited"`, the agent is cleaned up and left sleeping for respawn on next message.
- B13.7: If the agent was mid-task (`cli_status == "running"` and `idle=False`), `bridge_busy` is set and the `on_reconnect` callback fires with `was_mid_task=True` for output draining.
- B13.8: Messages arriving during reconnection are queued (`queued_reconnecting` status) and delivered after `session.reconnecting` is cleared.
- B13.9: SIGTERM/SIGINT triggers full stop: supervisor kills both bot.py and the bridge, then cleans up the socket file.
- B13.10: `restore_slot` registers reconnected agents with the scheduler without eviction, allowing slot count to temporarily exceed `max_slots`.

### Invariants
- I13.1: SDK client must be initialized before subscribing to the bridge; subscribe replays buffered messages that corrupt the initialize handshake if they arrive first. [fix: 6bba9fe]
  - Regression: Subscribe was called before SDK client creation, so replayed messages landed in the transport queue and the SDK read stale data.
- I13.2: When resuming a session with an existing `session_id`, the transport must be created with `reconnecting=True`; otherwise the transport sends a real initialize request to an already-initialized CLI. [fix: 754fb40]
  - Regression: `wake()` did not pass `reconnecting=True`, so the transport attempted full CLI initialization instead of faking success.
- I13.3: Bridge reconnect must call `scheduler.restore_slot()` to register the agent's slot; `restore_slot` must allow exceeding `max_slots`. [fix: 3d52dd4]
  - Regression: Reconnected agents were not registered with the scheduler, causing slot count drift.

### Anchors
- `axi/supervisor.py`:_hup_handler @ 63076db — sets `_hot_restart = True`, forwards SIGTERM to bot.py only
- `axi/supervisor.py`:_stop_handler @ 63076db — sets `_stopping = True`, forwards signal
- `axi/supervisor.py`:_kill_bridge @ 63076db — SIGTERM→SIGKILL escalation, unlink socket
- `packages/agenthub/agenthub/reconnect.py`:connect_procmux @ 63076db — connects, lists agents, kills orphans, fires reconnect tasks
- `packages/agenthub/agenthub/reconnect.py`:reconnect_single @ 63076db — init client, subscribe, restore_slot
- `packages/claudewire/claudewire/transport.py`:BridgeTransport.write @ 63076db — intercepts initialize when `_reconnecting`
- `packages/agenthub/agenthub/scheduler.py`:restore_slot @ 63076db — unconditional slot add

---

## 14. Interactive Gates

### Behaviors
- B14.1: When an agent calls ExitPlanMode, the plan content is posted to Discord as a file attachment and the agent blocks until the user reacts with approve/reject or types feedback.
- B14.2: Plan approval pre-adds checkmark and cross-mark reactions so the user can click rather than type.
- B14.3: After plan approval or rejection, the unchosen reaction emoji is removed for visual clarity.
- B14.4: Plan approval sets the agent's permission mode back to "default" and clears plan_mode on the session when approved.
- B14.5: When an agent calls AskUserQuestion with multiple questions, each question is posted and awaited individually in sequence.
- B14.6: Each question message gets pre-added number keycap reactions matching its options plus a custom-text emoji.
- B14.7: Reaction-based answers map keycap emoji to the corresponding option label; typed answers are parsed as option indices or literal text.
- B14.8: AskUserQuestion injects the collected answers dict into the tool_input via `updated_input` on PermissionResultAllow.
- B14.9: TodoWrite posts the formatted todo list to Discord and persists it to disk.
- B14.10: Channel status reflects gate state: "plan_review" while plan approval is pending, "question" while a question future is outstanding.
- B14.11: The permission callback chain intercepts ExitPlanMode and AskUserQuestion via hook policies inserted between block/allow policies and the CWD policy.
- B14.12: If no session or no channel_id exists, all gate handlers return PermissionResultAllow without blocking.
- B14.13: The /stop command resolves pending plan_approval_future with rejection and question_future with empty string, unblocking the agent.

### Invariants
- I14.1: Multi-question AskUserQuestion prompts must collect answers one at a time, each with its own future. [fix: 5407b76]
  - Regression: The handler tried to split a single reply across all questions by line, producing mismatched answers.
- I14.2: Plan file discovery must search the agent's CWD (for PLAN.md/plan.md) in addition to ~/.claude/plans/. [fix: e8dcfb8]
  - Regression: Only ~/.claude/plans/ was searched; SDK agents write PLAN.md to their CWD, so the plan was never found.
- I14.3: Plan posting must fall back to reading the plan file from disk when the tool_input dict does not contain a "plan" key. [fix: 3099d65]
  - Regression: When the LLM omitted the plan from tool_input, the plan content was None and the user saw "plan file not found".
- I14.4: Plan files older than 300 seconds must be ignored to prevent stale plans from a previous session being shown.

### Anchors
- `axi/discord_ui.py`:_handle_exit_plan_mode @ 09f09eb — plan file attachment + reaction gate
- `axi/discord_ui.py`:_read_latest_plan_file @ 09f09eb — searches CWD and ~/.claude/plans/
- `axi/discord_ui.py`:_handle_ask_user_question @ 09f09eb — sequential question collection
- `axi/discord_ui.py`:resolve_reaction_answer @ 09f09eb — keycap→label mapping
- `axi/discord_ui.py`:parse_question_answer @ 09f09eb — typed reply parsing
- `axi/discord_ui.py`:_post_todo_list @ 09f09eb — todo formatting + disk persistence
- `packages/agenthub/agenthub/permissions.py`:build_permission_callback @ 09f09eb — plan_approval_hook and question_hook in chain
- `axi/channels.py`:_detect_agent_status @ 09f09eb — "plan_review" / "question" status

---

## 15. Discord Query Client

### Behaviors
- B15.1: `DiscordClient` provides a synchronous httpx-based REST client for CLI tools, supporting context-manager lifecycle.
- B15.2: `AsyncDiscordClient` provides an asynchronous httpx-based REST client for bots, supporting async context-manager lifecycle.
- B15.3: Both clients retry HTTP 429 rate-limit responses by sleeping for the `retry_after` duration from the response body, looping up to `MAX_RETRIES` (3) attempts.
- B15.4: Both clients retry 5xx server errors with exponential backoff (`2**attempt` seconds), up to `MAX_RETRIES` attempts.
- B15.5: Non-retriable error responses (4xx other than 429) raise `httpx.HTTPStatusError` immediately via `resp.raise_for_status()`.
- B15.6: The sync client accepts 200 and 204 as success; the async client accepts 200, 201, and 204 as success.
- B15.7: `list_channels` resolves category names by building a lookup from type-4 channels, filters to text (type 0) and announcement (type 5) channels, and sorts by position.
- B15.8: `AsyncDiscordClient.find_channel` performs case-insensitive name matching against text and announcement channels in a guild, returning `None` on miss.
- B15.9: `AsyncDiscordClient.send_file` sends a multipart file upload with an optional text `content` field using httpx `data`/`files` kwargs.
- B15.10: `get_messages` clamps the `limit` parameter to 100 (Discord's per-request max) and supports `before`/`after` pagination cursors.
- B15.11: `add_reaction` and `remove_reaction` URL-encode the emoji string via `urllib.parse.quote` before interpolating it into the API path.
- B15.12: `wait_for_messages` polls `get_messages` with an `after` cursor until new non-filtered messages appear or `timeout` expires, returning `(matching_messages, cursor)`.
- B15.13: The wait poller advances its `after_id` baseline when messages exist but all are filtered (by author ID or system-message prefix), preventing infinite re-scanning.
- B15.14: The wait CLI auto-detects the latest message ID as baseline when `--after` is not provided, and emits a `{"cursor": ...}` line for chaining sequential calls.
- B15.15: `is_system_message` identifies bot system messages by the `*System:*` content prefix.
- B15.16: `resolve_snowflake` accepts either a numeric snowflake ID string or an ISO datetime string, converting datetimes to snowflakes using the Discord epoch (1420070400000 ms).
- B15.17: `split_message` splits text at newline boundaries when possible, falling back to hard splits at the 2000-character Discord limit.
- B15.18: `format_message` outputs either JSONL (with id, ts, author, author_id, content, attachment/embed counts) or human-readable text format.
- B15.19: The `query` CLI supports four subcommands (`guilds`, `channels`, `history`, `search`) and validates guild membership before channel/search operations.
- B15.20: `cmd_search` performs client-side case-insensitive substring matching across all text channels in a guild, with per-channel scan limits and optional author filtering.
- B15.21: `resolve_channel` accepts either a raw channel ID or `guild_id:channel_name` syntax, performing case-insensitive name lookup.
- B15.22: The `__main__` entry point dispatches `query` and `wait` subcommands via lazy imports, reading `DISCORD_TOKEN` from the environment.

### Invariants
- I15.1: Emoji strings in reaction API paths must be URL-encoded via `urllib.parse.quote`. [fix: 8d036d1]
  - Regression: Raw emoji unicode was interpolated directly into the URL path, causing malformed API requests.
- I15.2: All Discord REST API calls must go through the `discordquery` httpx client wrapper, not raw `discord_request` calls. [fix: 8d036d1]
  - Regression: Call sites used a lower-level helper directly, bypassing retry/rate-limit handling.

### Anchors
- `packages/discordquery/discordquery/client.py`:DiscordClient.request @ 09f09eb — sync retry loop with 429/5xx handling
- `packages/discordquery/discordquery/client.py`:AsyncDiscordClient.request @ 09f09eb — async success codes (200, 201, 204)
- `packages/discordquery/discordquery/client.py`:AsyncDiscordClient.add_reaction @ 09f09eb — `urllib.parse.quote(emoji)` URL-encoding
- `packages/discordquery/discordquery/client.py`:AsyncDiscordClient.send_file @ 09f09eb — multipart upload
- `packages/discordquery/discordquery/wait.py`:wait_for_messages @ 09f09eb — baseline advance when all messages filtered
- `packages/discordquery/discordquery/wait.py`:is_system_message @ 09f09eb — `*System:*` prefix check
- `packages/discordquery/discordquery/helpers.py`:resolve_snowflake @ 09f09eb — datetime-to-snowflake conversion
- `packages/discordquery/discordquery/helpers.py`:split_message @ 09f09eb — newline-preferring split logic
- `packages/discordquery/discordquery/query.py`:resolve_channel @ 09f09eb — `guild_id:channel_name` syntax
- `packages/discordquery/discordquery/query.py`:cmd_search @ 09f09eb — client-side substring search

---

## Regression Index

| Code | Domain | Invariant | Fix Commit | Status |
|------|--------|-----------|------------|--------|
| I1.1 | Agent Lifecycle | Channel topic updates must be fire-and-forget | 9c3bed2 | Active |
| I1.2 | Agent Lifecycle | Spawn guard must be held through agents dict population | 4d42a17 | Active |
| I1.3 | Agent Lifecycle | Killed agent messages must be explicitly rejected | 855e603 | Active |
| I1.4 | Agent Lifecycle | wake_or_queue must check is_processing | 855e603 | Active |
| I1.5 | Agent Lifecycle | SDK client must init before bridge subscribe | 6bba9fe | Active |
| I1.6 | Agent Lifecycle | Failed wake must log stderr and clear session.client | ae1f328 | Active |
| I1.7 | Agent Lifecycle | System prompt must be in the actual wake path | 5d15814 | Active |
| I1.8 | Agent Lifecycle | Reconstructed agents must have proper system prompt | 507b575, 833b0a4 | Active |
| I1.9 | Agent Lifecycle | Bridge-provided prompt/MCP must not be overwritten | e96a5af | Active |
| I1.10 | Agent Lifecycle | Reconstructed cwd must be verified against expected values | e2e3073 | Active |
| I1.11 | Agent Lifecycle | disconnect_client must skip SDK __aexit__ for direct subprocess | a9b6ce2 | Active |
| I1.12 | Agent Lifecycle | Default agent_type must be claude_code | f536c52 | Active |
| I1.13 | Agent Lifecycle | Stale session_id must be tracked to prevent resume cycle | aba7f05, c7267cd | Active |
| I1.14 | Agent Lifecycle | end_session must call scheduler.release_slot | c7267cd | Active |
| I2.1 | Query & Streaming | Use len(deque) not deque.qsize() | 65171f7 | Active |
| I2.2 | Query & Streaming | Check is_processing before spawning processing task | 855e603 | Active |
| I2.3 | Query & Streaming | Visibility check in stream_response_to_channel | d153a2d | Active |
| I2.4 | Query & Streaming | Thinking indicator must persist across block transitions | 707544f | Active |
| I2.5 | Query & Streaming | Subscribe replay must be synchronous before subscribed=True | a08f90e | Active |
| I2.6 | Query & Streaming | Bridge readline limit must be 10MB; fail fast on dead bridge | f15c5f5 | Active |
| I2.7 | Query & Streaming | session_id from first StreamEvent, not just ResultMessage | 8b21e31 | Active |
| I2.8 | Query & Streaming | interrupt must use SIGINT not kill | 3b091a9 | Active |
| I2.9 | Query & Streaming | /stop must use bridge kill; force-sleep on no ResultMessage | b4dd2ae | Active |
| I2.10 | Query & Streaming | Interrupt must send both SIGINT and SDK interrupt | 7e63539 | Active |
| I2.11 | Query & Streaming | CLI must use start_new_session; killpg for process group | cfcfe93 | Active |
| I2.12 | Query & Streaming | SDK commands must call query() before streaming | 72e0093 | Active |
| I3.1 | Concurrency | release_slot on every client=None path | 3d52dd4, c7267cd | Active |
| I3.2 | Concurrency | restore_slot on every reconnect client assignment | 3d52dd4 | Active |
| I3.3 | Concurrency | restore_slot must allow exceeding max_slots | 3d52dd4 | Active |
| I3.4 | Concurrency | Spawn signals processed immediately, not deferred | 00eb310 | Active |
| I3.5 | Concurrency | Spawn guard held from before task to after dict population | 4d42a17 | Active |
| I3.6 | Concurrency | No fixed timeout on serial queue waiters | 4e05688 | Active |
| I3.7 | Concurrency | Queue processor must set event on exception | 7e04eb3 | Active |
| I3.8 | Concurrency | Config lock across full read-modify-write cycle | 3a303af | Active |
| I4.1 | Channels | Category placement based on agent cwd | e1b8466 | Active |
| I4.2 | Channels | Fire-and-forget topic updates; tasks in _background_tasks set | 9c3bed2 | Active |
| I4.3 | Channels | Slot allocation checks allocated, not just running | 1fbe3da | Active |
| I4.4 | Channels | cmd_up auto-cleans stale .env for non-running instances | a109a15 | Active |
| I4.5 | Channels | Token resolution: slot-based first, then env var | c7134a3 | Active |
| I4.6 | Channels | All Discord REST calls through httpx wrapper | 8d036d1 | Active |
| I5.1 | Rate Limits | Notifications scoped to triggering channel + master | 1396ad4 | Active |
| I5.2 | Rate Limits | No retry on rate limit | 1396ad4 | Active |
| I5.3 | Rate Limits | Preserve utilization on same-window allowed events | 60f8713 | Active |
| I6.1 | Bridge | All readline limits must be 10MB | f15c5f5 | Active |
| I6.2 | Bridge | Transport must fail fast on dead bridge | f15c5f5 | Active |
| I6.3 | Bridge | Subprocess buffer limit 10MB | 7b724c8 | Active |
| I6.4 | Bridge | _relay_stdout must not block on proc.wait() after crash | 7b724c8 | Active |
| I6.5 | Bridge | Subscribe replay synchronous before subscribed=True | a08f90e, f0500fc | Active |
| I6.6 | Bridge | Force-close writer before wait_closed() | 6e33e05 | Active |
| I6.7 | Bridge | Supervisor must escalate SIGTERM→SIGKILL on bridge | 6e33e05 | Active |
| I7.1 | Permissions | No MCP self-sends to own channel | 900587f | Active |
| I7.2 | Permissions | No ContextVar for MCP tool caller resolution | 46e867c | Active |
| I7.3 | Permissions | MCP servers wired in both spawn and reconstruct | c35331c | Active |
| I8.1 | Scheduling | Route by owner field, not session | 5a5e0a9 | Active |
| I8.2 | Scheduling | schedule_last_fired init from last_occurrence | d9eaf35 | Active |
| I8.3 | Scheduling | Timezone-aware datetimes only | d850d70 | Active |
| I8.4 | Scheduling | Cron expressions in SCHEDULE_TIMEZONE | b545ac4 | Active |
| I8.5 | Scheduling | Recurring fires recorded in history with dedup | 5a5e0a9 | Active |
| I8.6 | Scheduling | Master agent must have schedule MCP server | 5a5e0a9 | Active |
| I9.1 | Logging | Logger variable names must match module | a7f952d | Active |
| I9.2 | Logging | StructuredContextFilter must set all ctx_* attributes | — | Active |
| I9.3 | Logging | Per-agent loggers must set propagate=False | — | Active |
| I10.1 | Discord UI | Thinking indicator hidden on every exit path | 707544f | Active |
| I10.2 | Discord UI | Rate-limited messages get X reaction, no retry | 1396ad4 | Active |
| I10.3 | Discord UI | Multi-question collects answers sequentially | 5407b76 | Active |
| I11.1 | Shutdown | Supervisor escalates SIGTERM→SIGKILL on bridge | 6e33e05 | Active |
| I11.2 | Shutdown | Force-close writer before wait_closed() | 6e33e05 | Active |
| I11.3 | Shutdown | Single instance via fcntl file lock | 7b724c8 | Active |
| I11.4 | Shutdown | ensure_default_files seeds in AXI_USER_DATA | 1c1c04a | Active |
| I12.1 | Config | Config lock across full read-modify-write | 3a303af | Active |
| I12.2 | Config | Specialized agents rebuild correct prompt on restart | e21c15e | Active |
| I12.3 | Config | Default files seeded in AXI_USER_DATA | 1c1c04a | Active |
| I13.1 | Reconnect | SDK client init before bridge subscribe | 6bba9fe | Active |
| I13.2 | Reconnect | reconnecting=True for resumed session transport | 754fb40 | Active |
| I13.3 | Reconnect | restore_slot on reconnect, allow exceeding max_slots | 3d52dd4 | Active |
| I14.1 | Gates | Multi-question collects one at a time | 5407b76 | Active |
| I14.2 | Gates | Plan file discovery searches agent CWD | e8dcfb8 | Active |
| I14.3 | Gates | Plan posting falls back to reading from disk | 3099d65 | Active |
| I14.4 | Gates | Ignore plan files older than 300s | — | Active |
| I15.1 | Discord Query | Emoji URL-encoding in reaction paths | 8d036d1 | Active |
| I15.2 | Discord Query | All REST calls through httpx client wrapper | 8d036d1 | Active |
