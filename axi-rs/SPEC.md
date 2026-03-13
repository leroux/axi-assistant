# Behavioral Specification — axi-rs

Generated: 2026-03-09
Source: `axi-rs`
Git range: 17da036..8b244e3 (full repo history)

## How to use this spec

- **Behaviors** (B-codes) are editable — change what the system should do
- **Invariants** (I-codes) are regression guards — delete only if the underlying cause is eliminated
- **Anchors** link spec entries to code — update when code moves

---

## 1. Agent Lifecycle

### Behaviors
- B1.1: `is_awake` checks the boolean `session.awake` flag to determine if an agent has an active CLI process.
- B1.2: `is_processing` attempts `query_lock.try_lock()` and returns `true` when the mutex is held, indicating a query is in flight.
- B1.3: `count_awake` iterates all sessions and returns the number with `awake == true`.
- B1.4: `reset_activity` sets `last_activity` to now, clears `idle_reminder_count`, and resets the activity state to `Phase::Starting`.
- B1.5: `sleep_agent` skips sleeping when `is_processing` returns true (unless `force` is set), preventing premature teardown of a busy agent.
- B1.6: `sleep_agent` disconnects the CLI client, sets `awake = false` and `bridge_busy = false`, then releases the scheduler slot.
- B1.7: `wake_agent` returns `Ok(())` immediately if the agent is already awake (idempotent wake).
- B1.8: `wake_agent` validates that the agent's `cwd` directory exists before proceeding, returning `HubError::Other` if it does not.
- B1.9: `wake_agent` acquires `state.wake_lock` and double-checks the awake flag, preventing duplicate concurrent wakes.
- B1.10: `wake_agent` requests a scheduler slot (with timeout) before creating the CLI process, and releases the slot on failure.
- B1.11: `wake_agent` attempts to resume with an existing `session_id`; if resume fails, it retries with a fresh session (no session_id) and records `last_failed_resume_id`.
- B1.12: `wake_agent` releases the scheduler slot and returns an error if both resume and fresh creation fail.
- B1.13: `wake_or_queue` wakes the agent, then checks `is_processing` to decide whether to return `true` (caller should send query) or queue the message for the running task to pick up.
- B1.14: `wake_or_queue` queues the message and returns `false` when `wake_agent` returns `ConcurrencyLimit`.
- B1.15: `wake_or_queue` returns `false` and logs a warning when `wake_agent` returns any other error (message is dropped).
- B1.16: `post_awaiting_input` posts a sentinel message ("Bot has finished responding and is awaiting input") after query completion, gated by the `show_awaiting_input` config flag.
- B1.17: `post_awaiting_input` includes @-mentions of all `allowed_user_ids` in the sentinel message.
- B1.18: The query task in `handle_message` acquires `query_lock`, processes the message, then drops the lock before draining the message queue, posting the awaiting-input sentinel, and sleeping the agent — in that order.
- B1.19: `AgentSession::new` initializes with `awake: false`, an empty `message_queue`, a fresh `query_lock`, and `agent_type` defaulting to `"flowcoder"`.
- B1.20: When `wake_or_queue` returns `false` and no session exists for the agent name (killed agent), `handle_message` posts an explicit rejection message ("has been killed. Messages are no longer accepted").
- B1.21: When `wake_or_queue` returns `false` and a session exists but the agent is sleeping/queued, `handle_message` adds an hourglass reaction to the user's message.

### Invariants
- I1.1: `wake_or_queue` must not return `true` when `is_processing` is true — the caller must not spawn a competing query task while the lock is held. [fix: 855e603]
  - Regression: `wake_or_queue` previously returned `true` unconditionally after a successful wake, causing the caller to spawn a second query task that raced with the first; the second task could acquire `query_lock` after the agent had already slept, leading to queries sent to a dead process.
- I1.2: Messages sent to a killed agent (session removed but channel mapping still exists) must be explicitly rejected with a user-visible message, not silently queued. [fix: 855e603]
  - Regression: After an agent was killed, its channel mapping persisted but its session was removed; incoming messages fell through to the "not awake" branch and were silently queued (into a nonexistent session), with no feedback to the user.
- I1.3: After query completion and message queue drain, `post_awaiting_input` must be called before `sleep_agent` so external consumers (e.g., `axi_test msg`) can detect that the bot has finished responding. [fix: 64831ba]
  - Regression: Without the awaiting-input sentinel, the test harness (`axi_test msg`) had no reliable signal that the bot had finished processing, leading to flaky wait-for-response detection.

### Anchors
- `lifecycle.rs`::`is_awake` @ 64831ba — `session.awake`
- `lifecycle.rs`::`is_processing` @ 855e603 — `query_lock.try_lock().is_err()`
- `lifecycle.rs`::`sleep_agent` @ 64831ba — `!force && is_processing(session)`
- `lifecycle.rs`::`wake_agent` @ 64831ba — `let _wake_lock = state.wake_lock.lock().await`
- `lifecycle.rs`::`wake_agent` @ 64831ba — `last_failed_resume_id = resume_id.clone()`
- `lifecycle.rs`::`wake_or_queue` @ 855e603 — `sessions.get(name).is_some_and(is_processing)`
- `lifecycle.rs`::`post_awaiting_input` @ 64831ba — `"Bot has finished responding and is awaiting input."`
- `events.rs`::`handle_message` @ 855e603 — `"has been killed. Messages are no longer accepted."`
- `events.rs`::`handle_message` @ 64831ba — `post_awaiting_input(&state_ref, &name).await`
- `types.rs`::`AgentSession::new` @ 64831ba — `awake: false`

---

## 2. Query Processing & Streaming

### Behaviors
- B2.1: `process_message` validates the agent is awake, resets activity state, sends the query to the Claude process, and dispatches streaming with automatic retry on transient errors.
- B2.2: `stream_with_retry` retries failed streams with exponential backoff (delay = `retry_base_delay * 2^(attempt-2)`), posting system messages to Discord on each retry, up to `max_retries` attempts.
- B2.3: `handle_query_timeout` interrupts the timed-out session (graceful interrupt with kill fallback), rebuilds it preserving the session ID if possible, and posts a recovery/reset status message.
- B2.4: `interrupt_session` attempts a graceful interrupt via the process connection, falling back to a hard kill if the graceful path fails or errors.
- B2.5: `run_initial_prompt` acquires `query_lock`, wakes the agent if sleeping, runs `process_message` under a `query_timeout` deadline, drains the message queue, then sleeps the agent.
- B2.6: `process_message_queue` pops messages from the front of the queue in a loop, waking the agent if needed, and breaks on shutdown or scheduler yield.
- B2.7: `deliver_inter_agent_message` queues-and-interrupts if the target agent is busy (but not compacting), or spawns a `run_initial_prompt` task if idle.
- B2.8: `inject_pending_flowchart` takes a pending `(command, args)` tuple from the session and pushes a synthetic `/<command> <args>` message to the front of the queue.
- B2.9: `live_edit_tick` accumulates text deltas and either posts a new Discord message (first chunk), splits at `MSG_LIMIT` (1900 chars, preferring newline boundaries), or throttle-edits the existing message at `EDIT_INTERVAL` (0.8s) intervals.
- B2.10: `live_edit_finalize` removes the streaming cursor from the current message, posts any remaining text (splitting if needed), and records the last flushed message ID for timing annotation.
- B2.11: `append_timing` edits the last flushed message to append a `-# {elapsed}{trace_id}` suffix, skipping responses under 0.5 seconds.
- B2.12: `show_thinking` / `hide_thinking` post and delete a temporary `*thinking...*` indicator message, operating only when live-edit streaming is enabled.
- B2.13: `show_tool_progress` / `delete_tool_progress` post and bulk-delete ephemeral tool progress messages (e.g., `*Running command...*`), gated by the `clean_tool_messages` flag.
- B2.14: `update_activity` transitions the agent's phase (`Starting` / `Thinking` / `ToolUse` / `Working` / `Idle`) based on `content_block_start` block type or `result` event type, unwrapping `stream_event` wrappers as needed.
- B2.15: `parse_rate_limit_event` extracts `status`, `resets_at`, `rate_limit_type`, and optional `utilization` from a rate limit JSON event.
- B2.16: `split_message` breaks text exceeding 1900 chars into chunks, preferring splits at the last newline before the limit.
- B2.17: `queue_and_wake` unconditionally pushes a message to the queue first, then wakes and spawns a drain task only if the agent was sleeping, avoiding double-lock of `query_lock` by delegating locking to `process_message_queue`.

### Invariants
- I2.1: Stream events wrapped in `stream_event` envelopes must be unwrapped before extracting `content_block`, `delta`, and `name` fields. [fix: f35e47d]
  - Regression: `content_block_start` and `content_block_delta` field lookups (block type, tool name, text delta) silently returned `None` when the Claude CLI wrapped events in `{"type": "stream_event", "event": {...}}` envelopes, causing activity tracking to stay in `Starting` phase and tool names to be lost.
- I2.2: Agents undergoing context compaction (`compacting == true`) must never be interrupted by inter-agent messages; the message is queued without calling `interrupt_session`. [fix: 628262e]
  - Regression: Inter-agent messages to a busy agent always triggered `interrupt_session`, which killed the compaction mid-flight, corrupting the agent's context window and requiring a full session rebuild.
- I2.3: Auto-compact threshold logic must be a pure function (`should_auto_compact`) that returns `None` when `context_window == 0` or `context_tokens == 0`, preventing division-by-zero and spurious compaction triggers. [fix: 82503f7]
  - Regression: Threshold check was inline in an async function, untestable in isolation; zero-value guards and the utilization comparison were coupled to session-lock and Discord I/O, making edge cases (e.g., freshly-created sessions with zero context window) hard to verify.
- I2.4: `queue_and_wake` must not acquire `query_lock` before calling `process_message_queue`, because `process_message_queue` acquires its own `query_lock` internally; double-acquisition deadlocks the agent. [fix: 339015c]
  - Regression: Voice transcript messages routed through `wake_or_queue` were silently dropped for sleeping agents (it only queues when the agent is already busy). The replacement `queue_and_wake` initially double-locked `query_lock`, deadlocking the agent on the first voice message.
- I2.5: The `compacting` flag must be set both on CLI-reported `system.status "compacting"` events and on self-triggered auto-compact (`check_auto_compact`), and cleared only on `compact_boundary`. [fix: 628262e]
  - Regression: Only CLI-reported compaction set the flag; auto-compact (triggered by `check_auto_compact` when utilization exceeded threshold) did not, leaving a window where inter-agent interrupts could kill self-initiated compaction.

### Anchors
- `messaging.rs`::`process_message` @ 82503f7 — `crate::claude_process::send_query(state, name, content).await`
- `messaging.rs`::`stream_with_retry` @ 82503f7 — `let delay = state.retry_base_delay * 2f64.powi((attempt - 2).cast_signed())`
- `messaging.rs`::`interrupt_session` @ 82503f7 — `conn.interrupt(name).await` / `conn.kill(name).await`
- `messaging.rs`::`deliver_inter_agent_message` @ 628262e — `if is_compacting { ... format!("queued, will process after compaction") }`
- `messaging.rs`::`process_message_queue` @ 82503f7 — `state.scheduler().await.should_yield(name).await`
- `streaming.rs`::`live_edit_tick` @ 82503f7 — `if le.last_edit_time.elapsed().as_secs_f64() >= EDIT_INTERVAL`
- `streaming.rs`::`live_edit_finalize` @ 82503f7 — `le.message_id = None; le.content.clear(); le.edit_pending = false`
- `streaming.rs`::`split_message` @ 82503f7 — `remaining[..MAX_LEN].rfind('\n').unwrap_or(MAX_LEN)`
- `activity.rs`::`update_activity` @ f35e47d — `let inner_type = if event_type == "stream_event" { event.get("event")...`
- `activity.rs`::`update_activity` @ f35e47d — `event.get("content_block").or_else(|| event.get("event").and_then(|e| e.get("content_block")))`
- `lifecycle.rs`::`queue_and_wake` @ 339015c — `crate::messaging::process_message_queue(&state, &name, &stream_handler).await`

---

## 3. Concurrency & Slot Management

### Behaviors
- B3.1: When an agent already holds a slot, `request_slot` returns immediately without consuming an additional slot.
- B3.2: When available slots exist below `max_slots`, the requesting agent is granted a slot immediately on the fast path.
- B3.3: When all slots are occupied, the scheduler attempts to evict idle agents by calling `evict_idle`, preferring background agents over interactive agents.
- B3.4: Eviction candidates are sorted by idle time descending, so the longest-idle non-busy agent is evicted first.
- B3.5: Agents that are busy (`is_busy`), bridge-busy (`is_bridge_busy`), not awake, or protected are never considered for eviction.
- B3.6: When no idle agents can be evicted (all are busy), the requesting agent is enqueued in a FIFO wait queue and a yield target is selected via `select_yield_target`.
- B3.7: Yield target selection marks a busy agent to sleep after its current turn completes, preferring background agents over interactive agents, and selecting the longest-busy agent within each tier.
- B3.8: A queued waiter blocks on a `Notify` with a configurable timeout; on timeout, it is removed from the wait queue and a `ConcurrencyLimit` error is returned.
- B3.9: When a slot is released, the next waiter in the FIFO queue is granted the freed slot and notified to unblock.
- B3.10: `release_slot` also cleans up the agent from `yield_set` and `interactive` tracking sets.
- B3.11: `restore_slot` unconditionally inserts an agent into the slot set, used for agents that reconnect from the bridge without going through the normal request flow.
- B3.12: Protected agents (configured at construction, typically the master agent) are never evicted or marked as yield targets.
- B3.13: The requesting agent itself is excluded from both eviction and yield target selection.
- B3.14: If the timeout fires but the slot was concurrently granted (agent found in `slots`), `request_slot` returns success instead of an error.

### Invariants
No invariants mined from fix history.

### Anchors
- `slots.rs`::`Scheduler::request_slot` @ 1e4f8ce — `if slots.contains(agent_name) { return Ok(()); }`
- `slots.rs`::`Scheduler::evict_idle` @ 1e4f8ce — `candidates.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap())`
- `slots.rs`::`Scheduler::select_yield_target` @ 1e4f8ce — `yield_set.insert(target.clone())`
- `slots.rs`::`Scheduler::release_slot` @ 1e4f8ce — `if let Some((waiter_name, notify)) = waiters.pop_front()`

---

## 4. Channel & Guild Management

### Behaviors
- B4.1: `normalize_channel_name` lowercases, replaces spaces with hyphens, strips all non-alphanumeric/non-hyphen/non-underscore characters, and truncates to 100 characters.
- B4.2: Seven status prefixes are defined (working, plan_review, question, done, idle, error, custom), each mapped to a Unicode emoji.
- B4.3: `strip_status_prefix` removes a leading `{emoji}-` prefix from a channel name, returning the bare agent name for matching purposes.
- B4.4: `match_channel_name` compares a channel name to a normalized agent name, optionally stripping status prefixes when `status_enabled` is true.
- B4.5: `format_channel_topic` encodes cwd, session ID, prompt hash, and agent type into a pipe-delimited string; `agent_type` is omitted when its value is `"flowcoder"` (the default type).
- B4.6: `parse_channel_topic` is the inverse of `format_channel_topic`, extracting the four fields from a pipe-delimited topic string; returns all `None` for empty or absent topics.
- B4.7: `ensure_guild_infrastructure` discovers existing "Axi", "Active", and "Killed" categories by name, creating any that are missing.
- B4.8: `ensure_agent_channel` finds an existing text channel matching the normalized agent name (with optional status prefix stripping), or creates a new one under the specified category.
- B4.9: `move_channel_to_killed` re-parents a channel into the Killed category via a Discord edit call.
- B4.10: `set_channel_status` renames a channel to `{emoji}-{normalized_name}` for a known status, or to the bare normalized name for an unknown status.
- B4.11: `reconstruct_channel_map` iterates all text channels in the guild, filtering to only those parented under the Axi, Active, or Killed categories, and builds a `ChannelId -> agent_name` mapping.
- B4.12: `mark_channel_active` moves a channel to position 0 within its category so the most recently active agent appears at the top.
- B4.13: On startup, the initialization sequence calls `ensure_guild_infrastructure`, then `reconstruct_channel_map`, then registers all discovered channel-to-agent mappings into bot state.
- B4.14: The master agent's channel is always ensured to exist during startup and is placed under the Axi category.
- B4.15: A startup notification message is sent to the master channel after the master agent is registered.
- B4.16: The `startup_complete` flag is set atomically with `SeqCst` ordering only after all infrastructure, channel maps, scheduler, bridge, and master agent are initialized.

### Invariants
No invariants mined from fix history.

### Anchors
- `channels.rs`::`normalize_channel_name` @ 1e4f8ce — `name.chars().take(100).collect()`
- `channels.rs`::`format_channel_topic` @ 1e4f8ce — `if atype != "flowcoder"`
- `channels.rs`::`reconstruct_channel_map` @ 1e4f8ce — `let in_managed_category = channel.parent_id.is_some_and(|parent| ...)`
- `startup.rs`::`initialize` @ de3e8ed — `state.startup_complete.store(true, Ordering::SeqCst)`
- `startup.rs`::`connect_bridge` @ de3e8ed — `backoff_ms = (backoff_ms * 2).min(16_000)`

---

## 5. Rate Limiting & Quota Tracking

### Behaviors
- B5.1: `parse_rate_limit_seconds` extracts a wait duration from free-text error messages using a cascading chain of four regex patterns, supporting "in/after N units", "retry after N", "N seconds", and "N minutes" formats.
- B5.2: When no regex matches the input text, `parse_rate_limit_seconds` returns a default of 300 seconds.
- B5.3: When a numeric capture is present but fails to parse as `u64`, each regex branch also falls back to a hardcoded default (300 for seconds-based, 5 for minutes-based).
- B5.4: `is_rate_limited` checks if the current time is before `rate_limited_until`; if the deadline has passed, it auto-clears `rate_limited_until` to `None` and returns false.
- B5.5: `rate_limit_remaining_seconds` returns the non-negative seconds remaining until the rate limit expires, or 0 if not rate-limited.
- B5.6: `format_time_remaining` formats seconds into human-readable strings, using "s" for sub-minute, "Nm Ns" for sub-hour, and "Nh Nm" for hour-scale durations, omitting zero-valued sub-components.
- B5.7: `record_session_usage` creates a new `SessionUsage` entry keyed by `session_id` on first use, setting `first_query` to the current timestamp.
- B5.8: On each call, `record_session_usage` increments the query count and accumulates cost, turns, duration, and token counts into the existing entry, updating `last_query` to the current timestamp.
- B5.9: When `usage_history_path` is configured, `record_session_usage` appends a JSON-lines record to the file for each usage event, using append mode with `create(true)`.
- B5.10: `RateLimitTracker` also stores `rate_limit_quotas` keyed by string, tracking per-quota status, reset time, type, and utilization, though quota update logic is managed externally.
- B5.11: File I/O errors during usage history append are silently ignored (`.ok()`), ensuring recording failures do not disrupt bot operation.

### Invariants
No invariants mined from fix history.

### Anchors
- `rate_limits.rs`::`parse_rate_limit_seconds` @ 1e4f8ce — `300` (default fallback at end of function)
- `rate_limits.rs`::`is_rate_limited` @ 1e4f8ce — `tracker.rate_limited_until = None`
- `rate_limits.rs`::`record_session_usage` @ 1e4f8ce — `entry.queries += 1`
- `rate_limits.rs`::`format_time_remaining` @ 1e4f8ce — `format!("{hours}h {minutes}m")`

---

## 6. Process & Bridge Management

### Behaviors
- B6.1: The procmux server (`ProcmuxServer`) listens on a Unix socket, accepts one client at a time, and manages named subprocesses with multiplexed stdin/stdout/stderr over the single connection.
- B6.2: When a new client connects, the server drops the previous connection, resets all processes to unsubscribed, and begins buffering output until the new client subscribes to each process.
- B6.3: The `spawn` command starts an OS subprocess in a new session (via `setsid` in `pre_exec`), pipes stdin/stdout/stderr, and spawns relay tasks that parse stdout as NDJSON and forward stderr as text lines.
- B6.4: Non-JSON stdout lines from a subprocess are re-routed as stderr messages rather than discarded, so non-protocol output is still visible.
- B6.5: The `subscribe` command replays all buffered messages for a process, reports idle/status/exit_code, and marks the process as subscribed for live forwarding.
- B6.6: The `kill` command sends SIGTERM to the process group, waits up to 5 seconds, then escalates to SIGKILL, and aborts the relay tasks.
- B6.7: The `interrupt` command sends SIGINT to the process group (not just the process), enabling child processes spawned by the CLI to also receive the signal.
- B6.8: The procmux client (`ProcmuxConnection`) runs a demux loop that routes `ResultMsg` to a command-response channel and stdout/stderr/exit messages to per-process registered queues.
- B6.9: When the demux loop detects EOF (server died or socket closed), it sets a `closed` atomic flag and sends `ProcessMsg::ConnectionLost` to all registered process queues.
- B6.10: Command responses use a single `cmd_lock` mutex to serialize concurrent command sends, with a 30-second timeout on each response.
- B6.11: `ProcmuxProcessConnection` is the adapter layer that wraps `ProcmuxConnection` with the `CommandResult` type, and `translate_process_msg` converts procmux `ProcessMsg` variants into claudewire `ProcessEvent` variants, mapping `ConnectionLost` to `None` (silently dropped).
- B6.12: `CliSession` has two construction paths: `spawn(config)` for direct subprocess mode, and `new(channels)` for procmux-backed mode with externally-provided event channels and stdin/kill closures.
- B6.13: For reconnecting agents (resume session), `CliSession::write` intercepts the first `control_request` with subtype `initialize` and injects a fake `control_response` success without forwarding to the subprocess, then clears the `reconnecting` flag.
- B6.14: `CliSession::read_message` filters bare duplicate stream events (`message_start`, `content_block_delta`, etc.) by checking against `BARE_STREAM_TYPES`, yielding only the `stream_event`-wrapped versions.
- B6.15: `CliSession::stop` injects a synthetic `ExitEvent` into the event channel to unblock a waiting `read_message`, then calls the kill closure asynchronously.
- B6.16: `Config::to_cli_args` is the single source of truth for Claude CLI flag names, always emitting `--output-format stream-json`, `--input-format stream-json`, and `--permission-prompt-tool stdio`.
- B6.17: `Config::to_env` sets `CLAUDE_CODE_ENTRYPOINT=sdk-py` and `CLAUDE_AGENT_SDK_VERSION` for SDK protocol compatibility, removes `CLAUDECODE` to prevent nested-session detection, and disables internal compaction via `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=100`.
- B6.18: MCP server configs are merged with SDK servers inserted first and external servers second, so external entries override SDK entries with the same name.
- B6.19: `create_client` spawns a process via procmux, subscribes to its event stream, creates a translator task (procmux `ProcessMsg` to claudewire `ProcessEvent`), and wraps the result in a `CliSession` stored in the shared `TransportMap`.
- B6.20: The bridge monitor loop (`bridge_monitor_loop`) checks `is_alive()` every 2 seconds; on connection loss it notifies the master channel, waits a 3-second grace period for procmux to restart, then exits with code 42.
- B6.21: Initial bridge connection uses exponential backoff (500ms doubling up to 16s) for 6 attempts (~30s total); if all fail, the bridge monitor loop handles subsequent reconnection.
- B6.22: The `InboundMsg` enum covers all Claude CLI stream-json message types including `stream_event`, `assistant`, `user`, `system`, `result`, `control_request`, `control_response`, `rate_limit_event`, `tool_progress`, `tool_use_summary`, `keep_alive`, `auth_status`, `prompt_suggestion`, and `control_cancel_request`.
- B6.23: Stdio log files rotate at 10 MB with up to 3 rotations, and each line is prefixed with a UTC timestamp.
- B6.24: The procmux wire protocol uses `#[serde(tag = "type")]` for both `ClientMsg` (cmd, stdin) and `ServerMsg` (result, stdout, stderr, exit), with newline-delimited JSON framing.
- B6.25: `disconnect_client` removes the transport from the map, calls `close()` on the `CliSession`, and unregisters the process queue from the procmux client.

### Invariants
- I6.1: Procmux service must derive its binary path from `AXI_RS_BINARY` environment variable, not hardcode a release path. [fix: c16f599]
  - Regression: The systemd unit hardcoded `/home/ubuntu/axi-tests/%i/axi-rs/target/release/procmux`, which broke debug builds that place binaries under `target/debug/`.
- I6.2: Systemd `ExecStart` must use a `/bin/bash -c 'exec "$VAR"'` shell wrapper when the binary path comes from an environment variable, because systemd does not expand `${}` syntax in `ExecStart`. [fix: 6fad00c]
  - Regression: `ExecStart=${AXI_RS_BINARY}` was passed literally to exec, failing to find a binary named `${AXI_RS_BINARY}`.
- I6.3: The bot must exit with code 42 on procmux bridge death to trigger a clean systemd restart, rather than attempting in-place reconnection with stale state. [fix: 9a0dc4e]
  - Regression: The original supervisor process managed restart internally with crash counting and signal forwarding; replacing it with native systemd required explicit exit-code signaling since all agent sessions are lost when procmux dies.
- I6.4: Unknown protocol message types in `ContentBlock` and `Delta` enums must use `#[serde(other)]` fallbacks to prevent deserialization failures when the upstream CLI adds new types. [fix: 221849d]
  - Regression: A `web_search_20250305` content block type caused a serde deserialization error that crashed the stream handler, because the enum had no catch-all variant.
- I6.5: Debug mode wrench emoji tool messages must fall back to `channel_id` when `live_edit` is unavailable, because not all code paths initialize a live-edit context. [fix: 855e603]
  - Regression: Debug mode tool messages were only sent to `live_edit.channel_id`, which was `None` in contexts where live-edit hadn't been initialized, silently swallowing the debug output.
- I6.6: SDK MCP servers must not be stripped from the engine config when using flowcoder, because the engine relays `control_request` messages (including the SDK MCP handshake) to the outer client. [fix: 855e603]
  - Regression: The flowcoder path set `fc_config.mcp_servers.sdk = None`, assuming the engine couldn't do the SDK handshake, but the engine's proxy relays those messages, so stripping them broke MCP tool availability.
- I6.7: `Config::to_cli_args` must always emit `--permission-prompt-tool stdio` so permission prompts are routed through the control protocol instead of being auto-denied in pipe mode. [fix: 855e603]
  - Regression: Without this flag, the CLI auto-denied all tool permissions in pipe mode, causing every tool use to fail silently.

### Anchors
- `procmux/src/server.rs`::cmd_spawn @ 9a0dc4e — `cmd.pre_exec(|| { nix::unistd::setsid() })`
- `procmux/src/server.rs`::relay_or_buffer @ 9a0dc4e — `if mp.subscribed { send_to_client_tx } else { mp.buffer.push(msg) }`
- `procmux/src/server.rs`::kill_process @ 9a0dc4e — `signal::killpg(pgid, Signal::SIGTERM)` then SIGKILL escalation
- `procmux/src/server.rs`::relay_stdout @ 9a0dc4e — `serde_json::from_str::<Value>(&line)` with stderr fallback for non-JSON
- `procmux/src/client.rs`::ProcmuxConnection::connect @ 9a0dc4e — demux loop with `demux_closed.store(true)` on EOF
- `procmux/src/client.rs`::send_command @ 9a0dc4e — `let _lock = self.cmd_lock.lock().await` serialization
- `procmux/src/protocol.rs`::ClientMsg @ 9a0dc4e — `#[serde(tag = "type")]` with cmd/stdin variants
- `claudewire/src/session.rs`::CliSession::write @ 855e603 — `if self.reconnecting && subtype == "initialize"` interception
- `claudewire/src/session.rs`::read_message @ 221849d — `if is_bare_stream_type(msg_type) { continue }`
- `claudewire/src/session.rs`::stop @ 9a0dc4e — `tx.send(ProcessEvent::Exit(...))` synthetic injection
- `claudewire/src/config.rs`::to_cli_args @ 855e603 — `args.extend(["--permission-prompt-tool".into(), "stdio".into()])`
- `claudewire/src/config.rs`::to_env @ 855e603 — `CLAUDE_CODE_ENTRYPOINT=sdk-py`, `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=100`
- `claudewire/src/schema.rs`::ContentBlock @ 221849d — `#[serde(other)] Unknown`
- `claudewire/src/schema.rs`::Delta @ 221849d — `#[serde(other)] Unknown`
- `claude_process.rs`::create_client @ 855e603 — `crate::flowcoder::build_engine_cli_args(&engine, &search_paths, &cli_config.to_cli_args())`
- `procmux_wire.rs`::translate_process_msg @ 9a0dc4e — `ProcessMsg::ConnectionLost => None`
- `startup.rs`::bridge_monitor_loop @ 9a0dc4e — `std::process::exit(42)` on bridge loss
- `startup.rs`::connect_bridge @ 9a0dc4e — `backoff_ms = (backoff_ms * 2).min(16_000)` retry loop

---

## 7. Permissions & Tool Gating

### Behaviors
- B7.1: File-writing tools (Edit, Write, MultiEdit, NotebookEdit) are path-validated against the agent's CWD, user data directory, and optionally worktrees and admin-extra directories.
- B7.2: Forbidden tools (Skill, EnterWorktree, Task) are unconditionally denied with a message explaining Discord-agent mode incompatibility.
- B7.3: Always-allowed tools (TodoWrite, EnterPlanMode) bypass all permission checks.
- B7.4: Tools not in the forbidden, always-allowed, or write-tool lists default to allowed.
- B7.5: An agent is classified as a "code agent" when its CWD is within bot_dir or worktrees_dir, granting write access to the entire worktrees directory and admin-extra directories.
- B7.6: Path resolution for write tools attempts `canonicalize(path)`, falls back to `canonicalize(parent).join(filename)`, and finally falls back to lexical `normalize_path` to catch `..` escapes even when paths do not exist on disk.
- B7.7: Agent names are validated to be 1-50 characters of lowercase alphanumeric plus hyphens, with no leading or trailing hyphen.
- B7.8: The CWD-based permission check is invoked from the bridge's `handle_permission_request` for any tool not already handled by plan-approval or ask-user interactive gates.
- B7.9: The Config struct uses a custom Debug impl that replaces the discord_token field with `[REDACTED]`.

### Invariants
- I7.1: Permission timeouts must deny, not allow. [fix: 4b06483]
  - Regression: The old bridge code had `// Timeout or channel closed -- auto-allow` for both plan approval timeouts and generic permission timeouts. This meant an unresponsive frontend would silently grant any tool permission. Changed to auto-deny in the consolidated permission handler.
- I7.2: Path traversal via `..` components must be caught even when intermediate directories do not exist. [fix: 4b06483]
  - Regression: Without the `normalize_path` lexical fallback, a path like `/allowed/work/../../etc/shadow` could bypass canonicalization if the intermediate path did not exist on disk, falling through to an un-normalized `PathBuf::from()` which preserved the `..` components.
- I7.3: Agent names must be restricted to `[a-z0-9-]` with no leading/trailing hyphen. [fix: 4b06483]
  - Regression: Unrestricted agent names could contain path separators, spaces, or special characters that could be used for directory traversal or Discord channel-name injection when the agent name is used to construct filesystem paths or channel names.
- I7.4: Discord token must not appear in Debug output. [fix: 4b06483]
  - Regression: The derived Debug impl on Config would print the raw discord_token in log output, risking credential exposure in tracing/error logs.
- I7.5: CWD-based write restrictions must be enforced in the permission handler, not just auto-allowed. [fix: 29af831]
  - Regression: Before this fix, the bridge's permission handler auto-allowed all non-interactive tool requests. The fix wires `PermissionConfig::new` and `check_permission` into `handle_permission_request` so that Edit/Write/MultiEdit/NotebookEdit calls outside the agent's CWD are denied.
- I7.6: SDK MCP servers must survive session rebuilds (restart/reconnect). [fix: 29af831]
  - Regression: `rebuild_session` created a fresh `AgentSession` and did not carry over `sdk_mcp_servers`, so after a restart the agent's in-process MCP tools (utils, schedule, discord, axi) disappeared. Fixed by copying `old.sdk_mcp_servers` into the new session.

### Anchors
- `permissions.rs`::normalize_path @ 4b06483 — `fn normalize_path(path: &Path) -> PathBuf`
- `permissions.rs`::check_permission @ 29af831 — `if WRITE_TOOLS.contains(&tool_name)`
- `permissions.rs`::starts_with_or_eq @ 4b06483 — `child == parent || child.starts_with(parent)`
- `mcp_tools.rs`::validate_agent_name @ 4b06483 — `fn validate_agent_name(name: &str) -> Result<(), String>`
- `axi-config/src/config.rs`::Debug for Config @ 4b06483 — `.field("discord_token", &"[REDACTED]")`
- `registry.rs`::rebuild_session @ 29af831 — `new_session.sdk_mcp_servers = old_sdk_mcp`

---

## 8. Scheduling & Cron Jobs

### Behaviors
- B8.1: The scheduler runs as a background tokio task, ticking every 10 seconds to check for due schedules.
- B8.2: Cron matching supports 5-field expressions (minute, hour, day-of-month, month, day-of-week) with wildcards, ranges, steps, comma-separated lists, and day-of-week 7 as alias for Sunday (0).
- B8.3: Recurring schedules use a `last_fired` HashMap keyed by `"owner/name"` to prevent duplicate firings within the same clock minute.
- B8.4: One-off schedules fire when `at <= now_utc`, are immediately removed from the schedules file, and recorded in history.
- B8.5: Schedule history records are appended with dedup: recurring schedules use a 5-minute dedup window; one-off schedules use no dedup (window=0).
- B8.6: History is pruned every tick, removing entries older than 7 days.
- B8.7: The MCP schedule tools (schedule_list, schedule_create, schedule_delete) are scoped per-agent via the `owner` field, with each agent limited to 20 schedules.
- B8.8: Schedule names must be 1-50 lowercase alphanumeric characters and hyphens, starting with an alphanumeric character (same validation as agent names).
- B8.9: All schedule file read-modify-write cycles are protected by a static tokio Mutex (`SCHEDULES_LOCK`) shared between the scheduler and MCP schedule tools.
- B8.10: Fired schedules are routed to their owner agent via `wake_or_queue`, spawning the agent session if it does not exist yet.
- B8.11: Schedules support a `reset_context` boolean that resets conversation context when fired.
- B8.12: The `schedule_key` function falls back from `owner` to `session` field for backward compatibility with older schedule entries.

### Invariants
- I8.1: Recurring schedules must skip their first firing after startup to avoid immediate spurious triggers. [fix: 6de3211]
  - Regression: Without the first-seen skip, every recurring schedule whose cron expression matched the current time would fire immediately at startup, even if it had already fired moments before the restart. The fix inserts the current time into `last_fired` on first encounter without actually firing, so the schedule only triggers on the next cron match.
- I8.2: Schedule history must deduplicate within a time window to prevent double-fires. [fix: 6de3211]
  - Regression: If the scheduler ticked twice during the same cron-matching minute (possible with the 10-second tick interval), a recurring schedule could fire multiple times. The `append_history` function now checks the most recent history record for the same schedule name/owner and skips if within the dedup window (5 minutes for recurring).
- I8.3: Crash markers must be routed to analysis agents, not silently dropped. [fix: 6de3211]
  - Regression: The crash handler reads supervisor marker files on startup but previously had no routing mechanism. The fix generates Discord notification messages and analysis prompts for crash-handler agents.

### Anchors
- `scheduler.rs`::check_and_fire @ 6de3211 — `if !last_fired.contains_key(&skey) { last_fired.insert(skey.clone(), now_local); continue; }`
- `scheduler.rs`::cron_matches @ 6de3211 — `pub fn cron_matches(expr: &str, dt: &DateTime<Local>) -> bool`
- `scheduler.rs`::append_history @ 6de3211 — `if *fired_at - last_utc < Duration::minutes(dedup_minutes) { return; }`
- `scheduler.rs`::schedule_key @ 6de3211 — `fn schedule_key(entry: &Value) -> String`
- `mcp_schedule.rs`::create_schedule_server @ 6de3211 — `pub fn create_schedule_server(agent_name: String, schedules_path: PathBuf`
- `mcp_schedule.rs`::is_valid_name @ 6de3211 — `name.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-')`

---

## 10. Discord Rendering & UI

### Behaviors
- B10.1: The Frontend trait defines an async interface with methods for message posting, lifecycle events (wake/sleep/spawn/kill), stream events, interactive gates (plan approval, questions), and shutdown.
- B10.2: `DiscordFrontend` implements Frontend by mapping agent names to Discord channels, editing channel names with status emoji prefixes on wake/sleep, and rendering session metadata in channel topics.
- B10.3: `WebFrontend` implements Frontend by broadcasting JSON events to subscribed WebSocket clients, with agent-level subscription filtering (empty subscription = all agents).
- B10.4: `FrontendRouter` broadcasts all non-interactive events (messages, lifecycle, stream) sequentially to every registered frontend.
- B10.5: For interactive gates (plan approval, questions), `FrontendRouter` spawns all frontends concurrently and races them via `futures_unordered_first`, aborting remaining frontends once the first responds.
- B10.6: Channel status emoji is set to "working" on wake and "idle" on sleep, gated by `channel_status_enabled` config.
- B10.7: On agent kill, the channel is moved to a "Killed" category if one exists in the guild infrastructure.
- B10.8: The `close_app` function exits with code 42 (triggering systemd restart), while `kill_process` exits with code 0.
- B10.9: Discord plan approval uses reaction-based interaction (checkmark/cross emoji) with a 10-minute timeout; web plan approval uses oneshot channels with JSON gate messages.
- B10.10: `BotState` holds a generic `FrontendRouter` plus a direct `Arc<DiscordFrontend>` reference for Discord-specific operations (channel lookups, reactions) that don't go through the trait.

### Invariants
- I10.1: Interactive gates must race across all frontends, returning the first response and aborting the rest. [fix: e6f8f75]
  - Regression: Before the multi-frontend architecture, interactive gates (plan approval, questions) were hardcoded to Discord-only. With multiple frontends, a single-frontend blocking wait would prevent other frontends from ever responding. The `FrontendRouter` now spawns concurrent tasks for each frontend and uses `JoinSet` to return the first result, aborting remaining tasks.
- I10.2: Discord-specific state must be extracted from BotState into DiscordFrontend to support multiple frontends. [fix: e6f8f75]
  - Regression: `BotState` previously held `channel_map`, `agent_channels`, `infra`, `pending_questions`, and `discord_client` directly. This made it impossible to add a second frontend without the core state depending on Discord types. The fix moves all Discord state into `DiscordFrontend` and routes through the `Frontend` trait.

### Anchors
- `frontend.rs`::FrontendRouter::request_plan_approval @ e6f8f75 — `let (result, _, remaining) = futures_unordered_first(handles).await;`
- `frontend.rs`::futures_unordered_first @ e6f8f75 — `async fn futures_unordered_first<T: Send + 'static>`
- `frontend.rs`::Frontend @ e6f8f75 — `pub trait Frontend: Send + Sync`
- `frontend.rs`::DiscordFrontend @ e6f8f75 — `impl Frontend for DiscordFrontend`
- `frontend.rs`::close_app @ e6f8f75 — `std::process::exit(42)`

---

## 11. Shutdown & Restart

### Behaviors
- B11.1: `ShutdownCoordinator` tracks whether a shutdown is already in progress via an `AtomicBool` and ignores duplicate requests.
- B11.2: Graceful shutdown in non-bridge mode polls every 5 seconds for up to 300 seconds waiting for busy agents to finish, posting status updates to Discord every 30 seconds.
- B11.3: In bridge mode, graceful shutdown skips the agent wait entirely since agents survive independently in procmux.
- B11.4: A 30-second safety deadline thread calls `std::process::exit(42)` to guarantee the process terminates even if the graceful path hangs.
- B11.5: `execute_exit` sends a goodbye message to the master agent channel, sleeps all agents (in non-bridge mode), then calls `close_app()` which exits with code 42.
- B11.6: Force shutdown sets the requested flag (escalating if already in progress) and immediately calls `execute_exit` without waiting for agents.
- B11.7: Exit code 42 is the universal restart signal; systemd's `SuccessExitStatus=42` treats it as a clean restart trigger.
- B11.8: The bridge monitor loop checks `is_alive()` every 2 seconds, and on connection loss notifies the master channel, waits a 3-second grace period (for procmux's own restart), then exits with code 42.

### Invariants
- I11.1: Bridge connection loss triggers exit code 42, not an in-place reconnect attempt. [fix: 9a0dc4e]
  - Regression: Without the bridge monitor loop, procmux death left the bot running with stale state and no way to send messages to agents.
- I11.2: Shutdown deduplication uses `AtomicBool::swap` with `SeqCst` ordering so only the first caller proceeds. [fix: 4b06483]
  - Regression: Concurrent shutdown triggers (e.g., SIGTERM during a `/restart` command) could race and execute the exit path multiple times.

### Anchors
- `shutdown.rs`::ShutdownCoordinator::graceful_shutdown @ 4b06483 — `if self.requested.swap(true, Ordering::SeqCst)`
- `shutdown.rs`::ShutdownCoordinator::execute_exit @ 4b06483 — `std::process::exit(RESTART_EXIT_CODE)`
- `startup.rs`::bridge_monitor_loop @ 9a0dc4e — `error!("Exiting with code 42 to trigger restart after bridge loss")`
- `frontend.rs`::close_app @ 4b06483 — `std::process::exit(42)`

---

## 12. Configuration & Model Selection

### Behaviors
- B12.1: `Config::from_env()` loads all configuration from environment variables and `.env` files at startup, with typed parsing and sensible defaults.
- B12.2: Discord token resolution follows a two-step fallback: first checks `DISCORD_TOKEN` env var, then looks up the instance name in `~/.config/axi/.test-slots.json` to resolve a token from `test-config.json`.
- B12.3: `ALLOWED_USER_IDS` is required and must parse to at least one valid u64; empty sets are rejected with `ConfigError::InvalidValue`.
- B12.4: Model selection uses a two-tier precedence: `AXI_MODEL` env var overrides the `config.json` file, which defaults to `"opus"` if absent or unparseable.
- B12.5: `set_model` validates against `VALID_MODELS` (haiku, sonnet, opus) and rejects invalid names before writing to disk.
- B12.6: Model config access is serialized through a `Mutex<()>` (`CONFIG_LOCK`) to prevent concurrent read-write races on the JSON file.
- B12.7: MCP servers are loaded from `mcp_servers.json` by name; unknown names are logged as warnings and skipped rather than causing failures.
- B12.8: `DiscordClient` wraps `reqwest` with built-in rate-limit handling (429 retry with `retry_after` delay) and exponential backoff on 5xx errors (up to 3 retries).
- B12.9: Allowed CWDs are built by merging `ALLOWED_CWDS` env var paths with the bot directory, user data directory, and worktrees directory, all canonicalized via `real_path`.
- B12.10: The `Config::for_test` constructor provides a minimal config rooted in a single `base_dir` with placeholder credentials for integration tests.
- B12.11: The `--permission-prompt-tool stdio` flag is always emitted in CLI args, routing permission prompts through the control protocol so the bot can handle them programmatically.

### Invariants
- I12.1: `discord_token` is replaced with `"[REDACTED]"` in Config's Debug output. [fix: 4b06483]
  - Regression: Deriving `Debug` on Config would print the raw Discord token in log messages, leaking secrets to log files and tracing output.
- I12.2: `--permission-prompt-tool stdio` is unconditionally added to all CLI invocations. [fix: 855e603]
  - Regression: Without this flag, permission prompts would go to Claude CLI's default handler which auto-denies in pipe mode, causing agents to fail on any tool that requires permission.

### Anchors
- `axi-config/src/config.rs`::Config::fmt @ 4b06483 — `.field("discord_token", &"[REDACTED]")`
- `axi-config/src/config.rs`::resolve_discord_token @ 4b06483 — `slots.get(&instance_name)`
- `axi-config/src/model.rs`::get_model @ 4b06483 — `std::env::var("AXI_MODEL")`
- `axi-config/src/model.rs`::set_model @ 4b06483 — `if !VALID_MODELS.contains(&lower.as_str())`
- `claudewire/src/config.rs`::Config::to_cli_args @ 855e603 — `args.extend(["--permission-prompt-tool".into(), "stdio".into()])`
- `axi-config/src/discord.rs`::DiscordClient::request @ 4b06483 — `if status == 429`

---

## 13. Hot Restart & Bridge Reconnection

### Behaviors
- B13.1: `connect_procmux` establishes a bridge connection and discovers surviving agents by calling `list_agents`, then spawns a reconnection task for each.
- B13.2: Agents found on the bridge but not in the local session map are killed immediately to clean up orphans.
- B13.3: Each agent's session is marked `reconnecting = true` before its reconnection task starts, and cleared to `false` on success, CLI-exited status, or any failure.
- B13.4: Reconnection subscribes to the agent's procmux stream, replaying buffered output, and creates a new client via `create_client` with the existing `session_id` for resume.
- B13.5: If the agent's CLI exited while the bot was down (`status == "exited"`), the reconnect cleans up without attempting a client creation.
- B13.6: Mid-task agents (CLI `status == "running"` and `idle == false`) have their `bridge_busy` flag set and are notified with a distinct "reconnected (was mid-task, resuming)" message.
- B13.7: `connect_bridge` retries startup connection with exponential backoff: 6 attempts at 500ms, 1s, 2s, 4s, 8s, 16s (approximately 30 seconds total).
- B13.8: On successful reconnect, the scheduler slot is restored and the frontend is notified via `on_reconnect`.

### Invariants
- I13.1: Startup connection uses exponential backoff (6 attempts, ~30s total) to tolerate procmux starting after the bot. [fix: 9a0dc4e]
  - Regression: A single connection attempt at startup would fail if procmux (started by a separate systemd unit) hadn't created its socket yet, leaving agents disconnected.

### Anchors
- `reconnect.rs`::connect_procmux @ 4b06483 — `conn.list_agents().await`
- `reconnect.rs`::reconnect_single @ 4b06483 — `session.bridge_busy = true`
- `startup.rs`::connect_bridge @ 9a0dc4e — `let mut backoff_ms = 500_u64`

---

## 14. Interactive Gates

### Behaviors
- B14.1: Pending questions are stored in `BotState.pending_questions` keyed by Discord message ID string, allowing reactions on the question message to resolve them.
- B14.2: Plan approval recognizes checkmark emoji (U+2705, U+2714+FE0F) as approved and X emoji (U+274C, U+274E) as denied.
- B14.3: `AskUser` questions present numbered emoji reactions (1-4) and map the reaction index to the corresponding option.
- B14.4: Unrecognized emoji reactions on pending questions re-insert the question back into the pending map rather than consuming it.
- B14.5: Fallback legacy plan approval handles reactions on non-pending messages by sending text messages ("Plan approved. Proceed with implementation.") to the agent.
- B14.6: Message dedup uses a bounded `VecDeque` (FIFO eviction) to prevent duplicate processing from Discord gateway reconnects.
- B14.7: The `FrontendRouter` races all registered frontends on interactive gates (plan approval, questions); the first response wins and remaining tasks are aborted.
- B14.8: If no frontends are registered, plan approval auto-approves and questions auto-timeout.
- B14.9: Reactions are filtered to only process from allowed users in the target guild, ignoring the bot's own reactions.
- B14.10: The `Frontend` trait defines `request_plan_approval` and `ask_question` as async methods that each frontend implements independently, enabling Discord reactions and web UI buttons to compete.
- B14.11: The goodbye message goes to the master agent's channel only (not broadcast) during shutdown.

### Invariants
- I14.1: Interactive gates race across all active frontends; first response wins, remaining are aborted. [fix: e6f8f75]
  - Regression: Before multi-frontend support, plan approval and question gates were hardcoded to Discord-only. Adding the web frontend required a racing mechanism so either frontend could answer, preventing questions from blocking indefinitely when the responding user is on a different frontend.

### Anchors
- `events.rs`::handle_reaction_add @ 4b06483 — `pending.remove(&message_id)`
- `events.rs`::EMOJI_NUMBERS @ 4b06483 — `const EMOJI_NUMBERS: &[&str]`
- `frontend.rs`::FrontendRouter::request_plan_approval @ e6f8f75 — `let (result, _, remaining) = futures_unordered_first(handles).await`
- `frontend.rs`::futures_unordered_first @ e6f8f75 — `set.abort_all()`
- `frontend.rs`::send_goodbye @ 4b06483 — `state.channel_for_agent(master_name).await`

---

## 16. MCP Tools & Protocol

### Behaviors
- B16.1: The MCP protocol module defines JSON-RPC 2.0 wire types (`JsonRpcRequest`, `JsonRpcResponse`, `JsonRpcError`) for tool registration and invocation between Claude Agent SDK and in-process MCP servers.
- B16.2: `McpServer` is a named collection of tools with async handlers; `add_tool` registers a `ToolDefinition` + handler closure, and `call_tool` dispatches by name or returns an error result for unknown tools.
- B16.3: `ToolResult` has two constructors: `text()` for success (is_error = None) and `error()` for failure (is_error = Some(true)), both wrapping a single text `ContentBlock`.
- B16.4: `build_sdk_mcp_config` determines which MCP servers each agent receives: all agents get utils, schedule, discord; master gets `create_master_server` (with restart/send_message); spawned agents get `create_agent_server` (without restart/send_message); all get playwright as an external stdio server.
- B16.5: The `handle_mcp_message` function routes JSON-RPC methods (`initialize`, `tools/list`, `tools/call`, `notifications/initialized`) to the appropriate `McpServer` instance stored on the agent's session via `sdk_mcp_servers`.
- B16.6: MCP messages arrive as `control_request` events with subtype `mcp_message`; the handler extracts `server_name` and `message`, dispatches to the matching `McpServer`, and wraps the result in a `control_response`.
- B16.7: The utils server provides `get_date_and_time` (with logical day boundary), `discord_send_file`, and `set_agent_status`/`clear_agent_status` tools.
- B16.8: The Discord MCP server provides `send_message`, `read_messages`, `list_channels`, `add_reaction`, `edit_message`, and `delete_message` tools, all operating via `httpx::AsyncClient` (DiscordClient).
- B16.9: The master server provides `axi_spawn_agent`, `axi_kill_agent`, `axi_list_agents`, `axi_restart`, and `axi_send_message`; the agent server provides `axi_spawn_agent`, `axi_kill_agent`, and `axi_list_agents` but omits restart and send_message.
- B16.10: Permission handling runs CWD-based checks before auto-allowing tool calls, denying writes outside the agent's CWD and blocking forbidden tools (like `Task`).
- B16.11: Discord snowflake IDs are parsed flexibly from both string and number JSON values via `parse_id`.
- B16.12: Agent names are validated to 1-50 chars of `[a-z0-9-]` with no leading/trailing hyphens.

### Invariants
- I16.1: SDK MCP servers must survive session rebuilds. [fix: 29af831]
  - Regression: `rebuild_session` created a fresh `AgentSession` without copying `sdk_mcp_servers` from the old session, so agents lost access to in-process MCP tools (utils, schedule, discord, axi) after any restart that called rebuild_session.
- I16.2: SDK MCP servers must not be stripped from flowcoder engine config. [fix: 855e603]
  - Regression: `create_client` in claude_process.rs stripped `sdk` MCP servers from the CLI config before passing it to flowcoder, under the assumption that the engine could not handle SDK handshakes. After the engine gained `control_request` relay support (proxy.rs), this stripping caused agents running through flowcoder to lose SDK MCP tools entirely.

### Anchors
- `mcp_protocol.rs`::McpServer::call_tool @ 1e4f8ce — `match self.handlers.get(name)`
- `mcp_protocol.rs`::McpServer::add_tool @ 1e4f8ce — `self.handlers.insert(name, Arc::new(move |args| Box::pin(handler(args))))`
- `mcp_tools.rs`::build_sdk_mcp_config @ 64831ba — `pub fn build_sdk_mcp_config(`
- `mcp_tools.rs`::create_utils_server @ 64831ba — `pub fn create_utils_server(state: Arc<BotState>) -> McpServer`
- `mcp_tools.rs`::create_master_server @ 64831ba — `pub fn create_master_server(state: Arc<BotState>) -> McpServer`
- `mcp_tools.rs`::create_agent_server @ 64831ba — `pub fn create_agent_server(state: Arc<BotState>) -> McpServer`
- `mcp_tools.rs`::create_discord_server @ 64831ba — `pub fn create_discord_server(dc: Arc<DiscordClient>) -> McpServer`
- `registry.rs`::rebuild_session @ de3e8ed — `new_session.sdk_mcp_servers = old_sdk_mcp`

---

## 17. Flowchart Execution

### Behaviors
- B17.1: Flowchart JSON is deserialized into a typed model via serde, with `BlockData` discriminated by a `type` tag (`start`, `end`, `prompt`, `branch`, `variable`, `bash`, `command`, `refresh`, `exit`, `spawn`, `wait`).
- B17.2: Connection fields accept both `source_id`/`target_id` and `source_block_id`/`target_block_id` aliases, and `VariableType` accepts `int`/`float` as aliases for `Number`.
- B17.3: Validation enforces exactly one start block, at least one end/exit block, all connection references resolve to existing blocks, no orphaned blocks (BFS reachability), and branch blocks have exactly two outgoing connections with `is_true_path` true and false.
- B17.4: `GraphWalker` is a pure state machine that advances through the flowchart graph, yielding `Action` variants whenever external IO is needed (Query, Bash, SubCommand, Clear, Spawn, Wait) and processing Variable/Branch blocks internally without yielding.
- B17.5: Template interpolation replaces `{{variable_name}}` and `$N` positional references against the variable map, rendering whole-number floats (e.g. `"3.0"`) as integers (`"3"`).
- B17.6: Branch condition evaluation supports simple truthiness (missing/empty/`"false"`/`"0"`/`"no"` are falsy), negation (`!var`), and comparison operators (`==`, `!=`, `>`, `<`, `>=`, `<=`) with numeric-first then string fallback.
- B17.7: Variable blocks coerce values by declared type: `Number` validates and normalizes (whole floats to ints), `Boolean` maps `"true"`/`"1"`/`"yes"` to `"true"`, `Json` validates parse-ability, and `String` passes through.
- B17.8: `feed_bash` stores exit code in the pending variable, halts with `Action::Error` on non-zero exit unless `continue_on_error` is set, and trims stdout before storing in the output variable.
- B17.9: `feed_subcommand` merges child variables into the parent, skipping positional (`$`-prefixed) keys to prevent argument leakage.
- B17.10: A safety limit (default 1000 blocks, configurable via `with_max_blocks`/`ExecutorConfig::max_blocks`) halts execution with `Action::Error` when exceeded, protecting against infinite loops.
- B17.11: Command resolution searches `cwd/commands/<name>.json`, `cwd/<name>.json`, each search path (flat and `/commands/` subdir), then `~/.flowchart/commands/<name>.json`; `list_commands` scans the same directories deduplicating by name.
- B17.12: `build_variables` performs shell-like splitting (single/double quotes, backslash escaping), maps positional args to both `$N` and named keys from argument definitions, applies defaults for missing optional args, and errors on missing required args.
- B17.13: The `run_flowchart` executor drives the walker in an async loop, dispatching each action through a `Session` trait (query, bash, clear, sub-command) and reporting progress via a synchronous `Protocol` trait (block start/complete, stream text, flowchart start/complete).
- B17.14: Sub-command execution recurses via `Box::pin(run_walker)` with a call stack depth limit (default 10), detects direct recursion (same command calling itself), inherits parent variables when `inherit_variables` is set, and merges child output variables when `merge_output` is set.
- B17.15: When `output_schema` is set on a prompt block, the executor appends JSON format instructions to the prompt and extracts JSON fields from the response into individual walker variables.
- B17.16: Cancellation is checked between every action via `CancellationToken`, returning `ExecutionStatus::Interrupted` and calling `session.interrupt()`; pause/resume is driven by an `AtomicBool` flag and `Notify`.
- B17.17: The `flowcoder-engine` binary is a transparent NDJSON proxy between an outer client and an inner Claude CLI subprocess, intercepting `/command`-prefixed user messages to run flowcharts while forwarding all other messages unchanged.
- B17.18: `build_claude_args` ensures `--print`, `--output-format stream-json`, `--input-format stream-json`, and `--replay-user-messages` are present on the inner CLI, deduplicating any caller-provided equivalents.
- B17.19: `extract_command_name` parses user message content starting with `/` to extract a flowchart command name and arguments, returning `None` for plain text, bare `/`, or non-string content.
- B17.20: `EngineSession` implements the `Session` trait by writing user messages as NDJSON to the inner CLI's stdin, reading stream events, relaying `control_request` messages to the outer client via stdout, and waiting for `control_response` messages from the router.
- B17.21: On `clear()`, `EngineSession` kills the inner CLI subprocess and respawns it from the original CLI args, preserving cost accumulation across clears.
- B17.22: The background `control::spawn_control_reader` task owns the message channel during flowchart execution, processing `engine_control` commands (pause/resume/cancel/status) and buffering non-control messages for replay after the flowchart completes.
- B17.23: The `flowcoder` TUI binary supports single-command mode (resolve, validate, run, exit with flowchart exit code) and REPL mode, with SIGINT mapped to cancellation and `--skip-permissions` auto-approving all tool permissions.
- B17.24: The `axi` crate's `flowcoder` module resolves the `flowcoder-engine` binary from `$PATH`, builds CLI args with `--search-path` flags and `--` separator for Claude passthrough args, and discovers commands by scanning `FLOWCODER_SEARCH_PATH` directories for YAML command definitions.

### Invariants
- I17.1: Engine must drain startup `control_request` messages before accepting user input (60s first-message timeout, 2s inter-message timeout). [fix: 855e603]
  - Regression: Without the startup drain, the inner Claude CLI blocks waiting for `control_response`s during SDK MCP server initialization while the engine's main loop blocks waiting for user messages — a mutual deadlock that prevents the engine from ever becoming responsive.
- I17.2: Pre-query drain must flush pending `control_request`s with 100ms timeout before writing user messages to the inner CLI. [fix: 855e603]
  - Regression: `control_request` messages arriving between the startup drain and the first user message caused the inner CLI to read a user message when it was still expecting a `control_response`, corrupting the message protocol and causing hangs.
- I17.3: SDK MCP servers must not be stripped from the CLI config when spawning the flowcoder engine. [fix: 855e603]
  - Regression: The engine was stripping `mcp_servers.sdk = None` under the assumption it could not handle SDK handshakes, but the engine actually relays `control_request`/`control_response` messages. Stripping SDK servers meant MCP tools were unavailable to the inner Claude session, breaking flowcharts that depend on them.

### Anchors
- `flowchart/src/model.rs`::BlockData @ 855e603 — `#[serde(tag = "type", rename_all = "snake_case")]`
- `flowchart/src/parse.rs`::parse_command @ 855e603 — `serde_json::from_str(json)?`
- `flowchart/src/validate.rs`::validate @ 855e603 — `let start_ids: Vec<&str> = flowchart.blocks.iter().filter`
- `flowchart/src/walker.rs`::GraphWalker::advance @ 855e603 — `self.blocks_executed += 1; if self.blocks_executed > self.max_blocks`
- `flowchart/src/walker.rs`::GraphWalker::feed_bash @ 855e603 — `if exit_code != 0 && !continue_on_error`
- `flowchart/src/condition.rs`::evaluate @ 855e603 — `!is_truthy(variables.get(inner).map(String::as_str))`
- `flowchart/src/interpolate.rs`::interpolate @ 855e603 — `bytes[i] == b'{' && bytes[i + 1] == b'{'`
- `flowchart/src/resolve.rs`::resolve_command @ 855e603 — `candidates.push(cwd.join("commands").join(format!("{name}.json")))`
- `flowchart-runner/src/executor.rs`::run_walker @ 855e603 — `if call_stack.len() > config.max_depth`
- `flowchart-runner/src/executor.rs`::run_flowchart @ 855e603 — `let variables = build_variables(args, &command.arguments)?`
- `flowchart-runner/src/variables.rs`::build_variables @ 855e603 — `let parts = shell_split(args_str)`
- `flowchart-runner/src/session.rs`::Session @ 855e603 — `pub trait Session: Send`
- `flowchart-runner/src/protocol.rs`::Protocol @ 855e603 — `pub trait Protocol: Send`
- `flowcoder-engine/src/main.rs`::drain_startup_control_requests @ 855e603 — `Duration::from_secs(60)`
- `flowcoder-engine/src/proxy.rs`::drain_pending_messages @ 855e603 — `Duration::from_millis(100)`
- `flowcoder-engine/src/proxy.rs`::extract_command_name @ 855e603 — `trimmed[1..].splitn(2, ' ')`
- `flowcoder-engine/src/control.rs`::spawn_control_reader @ 855e603 — `if msg_type == "engine_control"`
- `flowcoder-engine/src/engine_session.rs`::EngineSession::query @ 855e603 — `events::emit_raw(&msg)` (control_request relay)
- `flowcoder/src/main.rs`::run_single_command @ 855e603 — `if let Err(errors) = validate(&command.flowchart)`
- `flowcoder.rs`::build_engine_cli_args @ 855e603 — `args.push("--".to_string())`

---

## 18. Voice I/O

### Behaviors
- B18.1: `VoiceSession::join` connects to a Discord voice channel via Songbird, wires up STT (Deepgram) and TTS providers, and returns `(Arc<VoiceSession>, mpsc::Receiver<String>)` — the host owns transcript routing, not the voice library.
- B18.2: Event handlers are registered BEFORE joining the voice channel (via `get_or_insert`) because some events fire during the join handshake.
- B18.3: `VoiceReceiveHandler` handles both `SpeakingStateUpdate` (maps authorized user's Discord UserId to SSRC) and `VoiceTick` (extracts decoded audio for that SSRC) using shared state via `VoiceReceiveShared`.
- B18.4: Audio from the authorized user is downsampled from 48 kHz stereo i16 to 16 kHz mono i16 (picking the middle stereo pair of each 3-pair chunk) and forwarded to the STT provider via `mpsc::Sender<Bytes>`.
- B18.5: The `DeepgramStt` provider opens a WebSocket to Deepgram Nova-3, sends 16 kHz mono s16le PCM as binary frames, and parses streaming JSON results into `Transcript` structs with `is_final` and `speech_final` flags.
- B18.6: The `transcript_filter` background task only passes through transcripts where `speech_final == true` and the text is non-empty after trimming, discarding interim results.
- B18.7: Three TTS providers are supported: OpenAI TTS (24 kHz mono s16le streamed, upsampled 2x to 48 kHz stereo f32), Piper (22050 Hz mono s16le via subprocess, linearly interpolated to 48 kHz stereo f32), and espeak-ng (22050 Hz mono with 44-byte WAV header stripped, same resampling as Piper).
- B18.8: `play_tts` collects all TTS chunks into a contiguous PCM buffer, wraps it in a `RawAdapter` (48 kHz stereo f32), plays via Songbird, and waits for the `TrackEnd` event via a oneshot channel before returning.
- B18.9: TTS requests are serialized through a `tts_consumer` background task that reads from an mpsc channel, ensuring sequential playback with a 250ms gap between utterances.
- B18.10: The `VoiceSession` holds the STT shutdown oneshot sender (`_stt_shutdown`) to keep the Deepgram session alive; dropping it triggers a `CloseStream` message.
- B18.11: The voice command router (`router.rs`) parses transcripts into `VoiceCommand` variants (SwitchAgent, ListAgents, Briefing, Stop, Leave, SetMode, AgentMessage) via lowercased prefix/keyword matching, preserving original casing for AgentMessage fallthrough.
- B18.12: A `CancellationToken` coordinates shutdown across transcript_filter and tts_consumer tasks, triggered by `VoiceSession::leave`.
- B18.13: The `is_listening` atomic bool gates audio forwarding in VoiceTick — when false, decoded audio is not sent to STT.
- B18.14: Diagnostic logging fires on the first tick and every 500 ticks (~10 seconds) to avoid spamming logs.

### Invariants
- I18.1: Voice library must be decoupled from host — channel-based API, no callback or host-specific logic. [fix: c2475a9]
  - Regression: The original `VoiceSession::join` took a `ChatSendFn` callback and owned `active_agent` state, embedding host-specific logic (voice prefix formatting, agent tracking, greeting text) inside the voice library. This prevented reuse and tangled concerns — the library was formatting voice messages and choosing which agent to route to.
- I18.2: Voice transcripts must not be silently dropped when agent is sleeping. [fix: 339015c]
  - Regression: Voice transcripts were routed through `wake_or_queue` which silently dropped messages when the agent was not awake, causing spoken input to vanish without any response.
- I18.3: TTS playback must not overlap — each utterance must complete before the next starts. [fix: 339015c]
  - Regression: Multiple TTS responses played simultaneously because `play_tts` returned immediately without waiting for the track to finish. The fix added a `TrackEndNotifier` with a oneshot channel to block until playback completes, plus a 250ms inter-utterance gap.
- I18.4: Deepgram WebSocket must send KeepAlive messages to prevent idle timeout. [fix: 339015c]
  - Regression: When the bot auto-joined a voice channel before the user arrived, no audio flowed to Deepgram, causing the WebSocket to time out and disconnect. A 5-second keepalive timer in the send task now sends `{"type":"KeepAlive"}` when no audio is flowing.
- I18.5: Voice receive handler must not double-lock shared state (deadlock prevention). [fix: 339015c]
  - Regression: `queue_and_wake` for voice messages double-locked `query_lock` because `process_message_queue` also acquired the same lock internally, causing a deadlock that froze the voice pipeline.

### Anchors
- `axi-voice/src/gateway.rs`::VoiceSession::join @ c2475a9 — `pub async fn join(ctx: &Context, config: VoiceConfig) -> anyhow::Result<(Arc<Self>, mpsc::Receiver<String>)>`
- `axi-voice/src/gateway.rs`::transcript_filter @ c2475a9 — `async fn transcript_filter(`
- `axi-voice/src/gateway.rs`::tts_consumer @ c2475a9 — `async fn tts_consumer(`
- `axi-voice/src/receive.rs`::VoiceReceiveHandler::act @ 339015c — `EventContext::VoiceTick(tick) =>`
- `axi-voice/src/receive.rs`::handle_voice_tick @ 339015c — `let resampled = resample::downsample_48k_stereo_to_16k_mono(decoded)`
- `axi-voice/src/stt.rs`::DeepgramStt::connect @ 339015c — `let keepalive_msg = r#"{"type":"KeepAlive"}"#`
- `axi-voice/src/stt.rs`::parse_deepgram_result @ 339015c — `if v.get("type").and_then(|t| t.as_str()) != Some("Results")`
- `axi-voice/src/playback.rs`::play_tts @ 339015c — `let _ = end_rx.await`
- `axi-voice/src/tts.rs`::OpenAiTts::synthesize @ 339015c — `resample::upsample_24k_mono_to_48k_stereo_f32(&pcm_s16)`
- `axi-voice/src/resample.rs`::downsample_48k_stereo_to_16k_mono @ 339015c — `let l = i32::from(chunk[2])`
- `axi-voice/src/router.rs`::parse_command @ 339015c — `VoiceCommand::AgentMessage(transcript.trim().to_string())`

---

## Regression Index

| Code | Domain | Invariant | Fix Commit | Status |
|------|--------|-----------|------------|--------|
| I1.1 | Agent Lifecycle | wake_or_queue must not return true when is_processing is true | 855e603 | Active |
| I1.2 | Agent Lifecycle | Killed agent messages must be explicitly rejected | 855e603 | Active |
| I1.3 | Agent Lifecycle | post_awaiting_input must precede sleep_agent | 64831ba | Active |
| I2.1 | Query Processing | stream_event envelopes must be unwrapped | f35e47d | Active |
| I2.2 | Query Processing | Compacting agents must not be interrupted | 628262e | Active |
| I2.3 | Query Processing | should_auto_compact must guard zero values | 82503f7 | Active |
| I2.4 | Query Processing | queue_and_wake must not double-lock query_lock | 339015c | Active |
| I2.5 | Query Processing | Compacting flag must be set on both CLI and self-triggered compaction | 628262e | Active |
| I6.1 | Process Management | Procmux path from AXI_RS_BINARY, not hardcoded | c16f599 | Active |
| I6.2 | Process Management | Systemd ExecStart needs shell wrapper for env vars | 6fad00c | Active |
| I6.3 | Process Management | Exit code 42 on bridge death for clean restart | 9a0dc4e | Active |
| I6.4 | Process Management | serde(other) fallbacks for unknown protocol types | 221849d | Active |
| I6.5 | Process Management | Debug wrench emoji fallback to channel_id | 855e603 | Active |
| I6.6 | Process Management | SDK MCP servers not stripped from flowcoder config | 855e603 | Active |
| I6.7 | Process Management | --permission-prompt-tool stdio always emitted | 855e603 | Active |
| I7.1 | Permissions | Permission timeouts must deny, not allow | 4b06483 | Active |
| I7.2 | Permissions | Path traversal caught via normalize_path fallback | 4b06483 | Active |
| I7.3 | Permissions | Agent names restricted to [a-z0-9-] | 4b06483 | Active |
| I7.4 | Permissions | Discord token redacted in Debug output | 4b06483 | Active |
| I7.5 | Permissions | CWD-based write restrictions enforced in handler | 29af831 | Active |
| I7.6 | Permissions | SDK MCP servers survive session rebuilds | 29af831 | Active |
| I8.1 | Scheduling | Recurring schedules skip first firing after startup | 6de3211 | Active |
| I8.2 | Scheduling | Schedule history dedup prevents double-fires | 6de3211 | Active |
| I8.3 | Scheduling | Crash markers routed to analysis agents | 6de3211 | Active |
| I10.1 | Discord Rendering | Interactive gates race across all frontends | e6f8f75 | Active |
| I10.2 | Discord Rendering | Discord state extracted into DiscordFrontend | e6f8f75 | Active |
| I11.1 | Shutdown | Bridge loss triggers exit code 42 | 9a0dc4e | Active |
| I11.2 | Shutdown | Shutdown dedup via AtomicBool::swap SeqCst | 4b06483 | Active |
| I12.1 | Configuration | discord_token redacted in Debug output | 4b06483 | Active |
| I12.2 | Configuration | --permission-prompt-tool stdio unconditionally added | 855e603 | Active |
| I13.1 | Hot Restart | Startup connection uses exponential backoff | 9a0dc4e | Active |
| I14.1 | Interactive Gates | Gates race across frontends, first wins | e6f8f75 | Active |
| I16.1 | MCP Protocol | SDK MCP servers survive session rebuilds | 29af831 | Active |
| I16.2 | MCP Protocol | SDK MCP servers not stripped from flowcoder config | 855e603 | Active |
| I17.1 | Flowchart | Engine drains startup control_requests (60s timeout) | 855e603 | Active |
| I17.2 | Flowchart | Pre-query drain flushes pending control_requests | 855e603 | Active |
| I17.3 | Flowchart | SDK MCP servers not stripped from engine config | 855e603 | Active |
| I18.1 | Voice | Voice library decoupled from host | c2475a9 | Active |
| I18.2 | Voice | Voice transcripts not silently dropped | 339015c | Active |
| I18.3 | Voice | TTS playback must not overlap | 339015c | Active |
| I18.4 | Voice | Deepgram WebSocket keepalive required | 339015c | Active |
| I18.5 | Voice | No double-lock in voice receive handler | 339015c | Active |
