# Behavioral Specification Diff — axi-py vs axi-rs

Generated: 2026-03-09

## Summary Table

| # | Domain | Status | Py Behaviors | Rs Behaviors | Py Invariants | Rs Invariants |
|---|--------|--------|-------------|-------------|---------------|---------------|
| 1 | Agent Lifecycle | Diverged | 13 | 21 | 14 | 3 |
| 2 | Query Processing & Streaming | Diverged | 18 | 17 | 12 | 5 |
| 3 | Concurrency & Slot Management | Diverged | 12 | 14 | 8 | 0 |
| 4 | Channel & Guild Management | Diverged | 15 | 16 | 6 | 0 |
| 5 | Rate Limiting & Quota Tracking | Diverged | 9 | 11 | 3 | 0 |
| 6 | Process & Bridge Management | Diverged | 12 | 25 | 7 | 7 |
| 7 | Permissions & Tool Gating | Diverged | 13 | 9 | 3 | 6 |
| 8 | Scheduling & Cron Jobs | Matched | 13 | 12 | 6 | 3 |
| 9 | Logging & Observability | Python-only | 12 | — | 3 | — |
| 10 | Discord Rendering & UI | Diverged | 20 | 10 | 3 | 2 |
| 11 | Shutdown & Restart | Diverged | 14 | 8 | 4 | 2 |
| 12 | Configuration & Model Selection | Diverged | 17 | 11 | 3 | 2 |
| 13 | Hot Restart & Bridge Reconnection | Matched | 10 | 8 | 3 | 1 |
| 14 | Interactive Gates | Diverged | 13 | 11 | 4 | 1 |
| 15 | Discord Query Client | Python-only | 22 | — | 2 | — |
| 16 | MCP Tools & Protocol | Rust-only | — | 12 | — | 2 |
| 17 | Flowchart Execution | Rust-only | — | 24 | — | 3 |
| 18 | Voice I/O | Rust-only | — | 14 | — | 5 |

---

## 1. Agent Lifecycle

### Status: Diverged

### Behaviors
- B1.13 (py) `is_awake`/`is_processing` definitions ↔ B1.1-B1.3 (rs): Matched semantically. Rust decomposes into separate behaviors (is_awake, is_processing, count_awake).
- B1.1 (py) spawn_agent ↔ (rs): No direct spawn behavior in Rust; Rust AgentSession::new (B1.19) covers construction only.
- B1.2 (py) wake_agent ↔ B1.7-B1.12 (rs): Matched. Rust decomposes into finer granularity (idempotent wake, cwd validation, wake_lock, slot request, resume retry, failure cleanup).
- B1.3 (py) resume retry ↔ B1.11 (rs): Matched — both retry with fresh session on failed resume, tracking `last_failed_resume_id`.
- B1.4 (py) sleep_agent ↔ B1.5-B1.6 (rs): Matched — both skip when processing unless forced, disconnect client, release slot.
- B1.5 (py) end_session ↔ (rs): No explicit kill/end_session behavior in Rust spec.
- B1.6 (py) reconstruction on restart ↔ (rs): No reconstruction behavior in Rust spec (channel map reconstruction exists in domain 4, but not full session reconstruction).
- B1.7 (py) hot restart reconnection ↔ (rs): Covered in domain 13 for Rust.
- B1.8 (py) wake_or_queue ↔ B1.13-B1.15 (rs): Matched. Rust adds explicit handling for ConcurrencyLimit return (B1.14) and error-with-warning (B1.15).
- B1.9 (py) busy-agent queueing ↔ B1.13 (rs): Matched — both queue when agent is processing.
- B1.10 (py) killed agent rejection ↔ B1.20 (rs): Matched.
- B1.11 (py) rebuild_session ↔ (rs): No explicit rebuild_session behavior in Rust domain 1.
- B1.12 (py) disconnect_client transport variations ↔ (rs): Covered in Rust domain 6 (B6.25).
- B1.4 (rs) reset_activity ↔ (py): No explicit activity reset behavior in Python spec.
- B1.8 (rs) cwd validation before wake ↔ (py): Python-implicit, not spec'd.
- B1.9 (rs) wake_lock double-check ↔ (py): Python-implicit, not spec'd.
- B1.16-B1.17 (rs) post_awaiting_input ↔ (py): Rust-only. Sentinel message for test harness detection.
- B1.18 (rs) query task ordering ↔ (py): Rust-only. Explicit ordering of lock drop → queue drain → awaiting input → sleep.
- B1.19 (rs) AgentSession::new defaults ↔ (py): Rust-only. Explicit default agent_type "flowcoder".
- B1.21 (rs) hourglass reaction ↔ (py): Rust-only. Queued messages get reaction feedback.

### Invariants
- I1.1 (py) fire-and-forget topic updates: Python-only
- I1.2 (py) spawn guard: Python-only
- I1.3 (py) killed agent rejection ↔ I1.2 (rs): Matched
- I1.4 (py) wake_or_queue is_processing check ↔ I1.1 (rs): Matched
- I1.5 (py) SDK init before subscribe: Python-only (covered in py domain 13)
- I1.6 (py) failed wake stderr/client clear: Python-only
- I1.7 (py) system prompt in wake path: Python-only
- I1.8 (py) reconstructed agents proper prompt: Python-only
- I1.9 (py) bridge prompt/MCP not overwritten: Python-only
- I1.10 (py) reconstructed cwd verification: Python-only
- I1.11 (py) skip SDK __aexit__ for direct subprocess: Python-only (Python-specific SDK bug)
- I1.12 (py) default agent_type claude_code: Python-only
- I1.13 (py) stale session_id tracking: Python-only (but behavior is matched in B1.11 rs)
- I1.14 (py) end_session release_slot: Python-only (but covered semantically in rs scheduler)
- I1.3 (rs) post_awaiting_input before sleep_agent: Rust-only

### Key Differences
- Rust has significantly more fine-grained behaviors (21 vs 13), decomposing wake/sleep into atomic steps.
- Python has 14 invariants from its longer bug history; Rust has only 3.
- Python tracks reconstruction, rebuild_session, and disconnect_client transport variations explicitly; Rust distributes these across other domains.
- Rust adds post_awaiting_input sentinel (B1.16-B1.17) and hourglass reaction (B1.21), both absent in Python.

---

## 2. Query Processing & Streaming

### Status: Diverged

### Behaviors
- B2.1 (py) receive_user_message ↔ B2.1 (rs) process_message: Diverged — Python has a unified entry point handling shutdown/reconnect/busy checks; Rust's process_message validates awake state and dispatches streaming.
- B2.2 (py) process_message query+stream ↔ B2.1 (rs): Matched at high level.
- B2.3 (py) _stream_with_retry ↔ B2.2 (rs) stream_with_retry: Matched — both retry with exponential backoff.
- B2.4 (py) stream_response normalized events ↔ (rs): Python-only as a distinct behavior. Rust handles streaming in domain 10 (live_edit_tick etc.).
- B2.5 (py) stream_response_to_channel ↔ B2.9-B2.10 (rs): Matched — live-edit, typing, splitting. Rust has finer decomposition.
- B2.6 (py) graceful_interrupt on busy ↔ (rs): Not explicit in Rust spec (Rust does queue-and-interrupt in B2.7).
- B2.7 (py) interrupt_session transport dispatch ↔ B2.4 (rs): Matched — graceful interrupt with kill fallback.
- B2.8 (py) handle_query_timeout ↔ B2.3 (rs): Matched — interrupt then rebuild.
- B2.9 (py) process_message_queue ↔ B2.6 (rs): Matched — sequential drain with yield/shutdown checks.
- B2.10 (py) receive_response_safe ↔ (rs): Python-only. Rust handles unknown types via serde(other) (I6.4).
- B2.11 (py) text flush points ↔ B2.9-B2.10 (rs): Matched.
- B2.12 (py) session_id from first StreamEvent ↔ (rs): Not explicitly spec'd in Rust.
- B2.13 (py) StreamKilled force-sleep ↔ (rs): Not explicitly spec'd in Rust.
- B2.14 (py) thinking indicators ↔ B2.12 (rs): Matched.
- B2.15 (py) visibility check ↔ (rs): Python-only.
- B2.16 (py) deliver_inter_agent_message ↔ B2.7 (rs): Matched — queue-and-interrupt or background wake.
- B2.17 (py) SDK commands /clear /compact ↔ (rs): Python-only explicit behavior.
- B2.18 (py) log context via contextvars ↔ (rs): Python-only (no domain 9 in Rust).
- B2.5 (rs) run_initial_prompt ↔ (py): Rust-only explicit behavior.
- B2.8 (rs) inject_pending_flowchart ↔ (py): Rust-only (flowchart integration).
- B2.11 (rs) append_timing ↔ (py): Matched with B10.8 (py).
- B2.13 (rs) show_tool_progress ↔ (py): Rust-only explicit behavior.
- B2.14 (rs) update_activity phase tracking ↔ (py): Rust-only. Python tracks activity but doesn't spec phases.
- B2.15 (rs) parse_rate_limit_event ↔ (py): Rust-only explicit behavior.
- B2.16 (rs) split_message ↔ (py): Matched with B10.3 (py).
- B2.17 (rs) queue_and_wake ↔ (py): Rust-only. Distinct from wake_or_queue.

### Invariants
- I2.1 (py) len(deque) not qsize(): Python-only (Python-specific API issue)
- I2.2 (py) is_processing before processing task ↔ I1.1 (rs): Matched
- I2.3 (py) visibility check in stream handler: Python-only
- I2.4 (py) thinking indicator persistence: Python-only
- I2.5 (py) subscribe replay synchronous ↔ (rs): Python-only (covered in domain 6 for both)
- I2.6 (py) 10MB readline limit: Python-only (covered in domain 6 for both)
- I2.7 (py) session_id from first StreamEvent: Python-only
- I2.8 (py) SIGINT not kill for interrupt: Python-only
- I2.9 (py) /stop uses bridge kill: Python-only
- I2.10 (py) both SIGINT and SDK interrupt: Python-only
- I2.11 (py) start_new_session + killpg: Python-only
- I2.12 (py) SDK commands call query() first: Python-only
- I2.1 (rs) stream_event unwrapping ↔ (py): Rust-only
- I2.2 (rs) compacting agents not interrupted ↔ (py): Rust-only
- I2.3 (rs) should_auto_compact zero guards ↔ (py): Rust-only
- I2.4 (rs) queue_and_wake no double-lock ↔ (py): Rust-only
- I2.5 (rs) compacting flag on both CLI and self-triggered ↔ (py): Rust-only

### Key Differences
- Python has more invariants (12 vs 5), reflecting its longer bug history.
- Rust adds auto-compaction behavior (I2.2, I2.3, I2.5), flowchart injection (B2.8), tool progress messages (B2.13), and activity phase tracking (B2.14) — none present in Python.
- Python specs graceful_interrupt and SDK command handling explicitly; Rust omits or distributes these.
- Rust adds queue_and_wake (B2.17) as a separate path from wake_or_queue.

---

## 3. Concurrency & Slot Management

### Status: Diverged

### Behaviors
- B3.1 (py) fixed pool + FIFO wait ↔ B3.2, B3.8 (rs): Matched.
- B3.2 (py) evict idle, prefer background ↔ B3.3-B3.5 (rs): Matched.
- B3.3 (py) yield target for busy background ↔ B3.6-B3.7 (rs): Matched.
- B3.4 (py) protected agents ↔ B3.12 (rs): Matched.
- B3.5 (py) interactive eviction resistance ↔ B3.3, B3.5 (rs): Matched.
- B3.6 (py) FIFO slot grant ↔ B3.9 (rs): Matched.
- B3.7 (py) should_yield ↔ (rs): Matched (covered in B3.7 rs yield target).
- B3.8 (py) restore_slot ↔ B3.11 (rs): Matched.
- B3.9 (py) release_slot synchronous ↔ B3.10 (rs): Matched.
- B3.10 (py) bot_creating_channels guard ↔ (rs): Python-only.
- B3.11 (py) config read-modify-write lock ↔ (rs): Python-only (Rust covers this in domain 12).
- B3.12 (py) request_slot timeout ↔ B3.8 (rs): Matched.
- B3.1 (rs) idempotent slot check ↔ (py): Rust-only explicit behavior — request_slot returns immediately if agent already holds slot.
- B3.13 (rs) requesting agent excluded from eviction/yield ↔ (py): Rust-only explicit behavior.
- B3.14 (rs) timeout-vs-concurrent-grant ↔ (py): Rust-only — handles edge case where slot granted concurrently with timeout.

### Invariants
- I3.1-I3.8 (py): All Python-only. Rust has no mined invariants for this domain.

### Key Differences
- Largely matched in core semantics.
- Rust adds edge-case behaviors (B3.1 idempotent check, B3.13 self-exclusion, B3.14 timeout race).
- Python has 8 invariants from bug history; Rust has none, suggesting the Rust implementation hasn't yet encountered (or hasn't yet documented) the same regression patterns.

---

## 4. Channel & Guild Management

### Status: Diverged

### Behaviors
- B4.1 (py) ensure_guild_infrastructure ↔ B4.7 (rs): Matched — both create Axi/Active/Killed categories.
- B4.2 (py) cwd-based category placement ↔ (rs): Python-only. Rust uses B4.8 (ensure_agent_channel) but doesn't spec cwd-based category selection.
- B4.3 (py) killed→live category move ↔ (rs): Python-only.
- B4.4 (py) wrong-category correction ↔ (rs): Python-only.
- B4.5 (py) deduplicate_master_channel ↔ (rs): Python-only.
- B4.6 (py) master channel position 0 ↔ (rs): Python-only.
- B4.7 (py) status emoji + debounced rename ↔ B4.2-B4.3, B4.10 (rs): Matched — both define status prefixes and set_channel_status.
- B4.8 (py) channel recency reordering ↔ B4.12 (rs): Diverged — Python sorts all channels with debounce; Rust moves individual channels to position 0.
- B4.9 (py) move_channel_to_killed strip prefix ↔ B4.9 (rs): Matched.
- B4.10 (py) reconstruct_agents_from_channels ↔ B4.11-B4.13 (rs): Matched — both rebuild from existing channels.
- B4.11 (py) fire-and-forget topic updates ↔ (rs): Python-only.
- B4.12-B4.15 (py) axi_test slot management ↔ (rs): Python-only (test infrastructure).
- B4.1 (rs) normalize_channel_name ↔ (py): Rust-only explicit behavior.
- B4.4 (rs) match_channel_name ↔ (py): Rust-only explicit behavior.
- B4.5-B4.6 (rs) format/parse_channel_topic ↔ (py): Rust-only explicit behavior (Python does this but doesn't spec it separately).
- B4.14-B4.16 (rs) master channel and startup_complete ↔ (py): Rust-only explicit behaviors.

### Invariants
- I4.1-I4.6 (py): All Python-only.
- Rust has no invariants for this domain.

### Key Differences
- Python specs extensive category management (cwd-based placement, dedup, reordering) and test infrastructure (axi_test).
- Rust specs lower-level utilities (normalize, match, format/parse topic) and startup sequencing (startup_complete flag).
- Both implement the same channel infrastructure but spec different aspects.

---

## 5. Rate Limiting & Quota Tracking

### Status: Diverged

### Behaviors
- B5.1 (py) parse_rate_limit_seconds ↔ B5.1-B5.3 (rs): Matched — Rust decomposes into finer detail (cascading regex, default, per-pattern fallback).
- B5.2 (py) is_rate_limited ↔ B5.4 (rs): Matched.
- B5.3 (py) handle_rate_limit ↔ (rs): Python-only — broadcast notification, duplicate suppression, delayed expiry notification.
- B5.4 (py) update_rate_limit_quota ↔ B5.10 (rs): Diverged — Python specs per-type upsert with JSONL history; Rust stores quotas but notes "managed externally".
- B5.5 (py) record_session_usage ↔ B5.7-B5.9 (rs): Matched.
- B5.6 (py) notify_rate_limit_expired ↔ (rs): Python-only.
- B5.7 (py) rate_limit in AssistantMessage ↔ (rs): Python-only.
- B5.8 (py) distinguishable rate limit return ↔ (rs): Python-only.
- B5.9 (py) deadline only extended ↔ (rs): Python-only.
- B5.5 (rs) rate_limit_remaining_seconds ↔ (py): Rust-only.
- B5.6 (rs) format_time_remaining ↔ (py): Rust-only.
- B5.11 (rs) file I/O errors silently ignored ↔ (py): Rust-only explicit behavior.

### Invariants
- I5.1-I5.3 (py): All Python-only.
- Rust has no invariants for this domain.

### Key Differences
- Python specs the full rate-limit handling pipeline (notification, suppression, expiry, retry avoidance).
- Rust specs lower-level utility functions (remaining seconds, time formatting) but lacks the higher-level handling behaviors.
- Potential gap: Rust may not have rate-limit notification broadcasting or retry suppression logic.

---

## 6. Process & Bridge Management

### Status: Diverged

### Behaviors
- B6.1 (py) ProcmuxServer Unix socket ↔ B6.1 (rs): Matched.
- B6.2 (py) buffer when unsubscribed ↔ B6.2, B6.5 (rs): Matched.
- B6.3 (py) ProcmuxConnection demux ↔ B6.8 (rs): Matched.
- B6.4 (py) ensure_running ↔ (rs): Python-only (procmux lifecycle managed differently in Rust).
- B6.5 (py) BridgeTransport SDK interface ↔ B6.12 (rs) CliSession: Matched conceptually (different abstraction names).
- B6.6 (py) BridgeTransport intercept initialize ↔ B6.13 (rs): Matched.
- B6.7 (py) BridgeTransport.stop ExitEvent injection ↔ B6.15 (rs): Matched.
- B6.8 (py) _TranslatingQueue ↔ B6.11 (rs) translate_process_msg: Matched.
- B6.9 (py) DirectProcessConnection ↔ (rs): Python-only (direct subprocess mode without procmux).
- B6.10 (py) per-process idle tracking ↔ (rs): Python-only.
- B6.11 (py) process kill SIGTERM→SIGKILL ↔ B6.6 (rs): Matched.
- B6.12 (py) single client connection ↔ B6.2 (rs): Matched.
- B6.3 (rs) spawn with setsid ↔ (py): Rust-only explicit. Python uses start_new_session on CLI spawn.
- B6.4 (rs) non-JSON stdout as stderr ↔ (py): Rust-only explicit.
- B6.7 (rs) SIGINT to process group ↔ (py): Rust-only explicit.
- B6.9 (rs) ConnectionLost on EOF ↔ (py): Rust-only explicit.
- B6.10 (rs) cmd_lock serialization ↔ (py): Rust-only explicit.
- B6.14 (rs) bare stream event filtering ↔ (py): Rust-only.
- B6.16 (rs) Config::to_cli_args ↔ (py): Rust-only explicit.
- B6.17 (rs) Config::to_env ↔ (py): Rust-only explicit.
- B6.18 (rs) MCP server merge order ↔ (py): Rust-only explicit.
- B6.19 (rs) create_client full pipeline ↔ (py): Rust-only explicit.
- B6.20 (rs) bridge_monitor_loop ↔ (py): Rust-only (Python supervisor handles this).
- B6.21 (rs) exponential backoff ↔ (py): Rust-only explicit (Python covers in domain 13).
- B6.22 (rs) InboundMsg enum ↔ (py): Rust-only (typed message enum).
- B6.23 (rs) stdio log rotation ↔ (py): Rust-only.
- B6.24 (rs) wire protocol format ↔ (py): Rust-only explicit.
- B6.25 (rs) disconnect_client ↔ B1.12 (py): Matched.

### Invariants
- I6.1-I6.7 (py): Python-only bridge/procmux invariants.
- I6.1-I6.7 (rs): Rust-only invariants (systemd, serde, debug fallbacks, flowcoder MCP, permission-prompt-tool).
- No overlap — different bug histories produce entirely different invariant sets.

### Key Differences
- Rust has nearly double the behaviors (25 vs 12), reflecting the Rust implementation encoding more detail into the spec (CLI args, env vars, wire protocol, monitor loop).
- Python has DirectProcessConnection (no-bridge mode) which Rust lacks.
- Invariants are completely disjoint — Python's are about buffer limits and relay crashes; Rust's are about systemd integration, serde resilience, and config correctness.

---

## 7. Permissions & Tool Gating

### Status: Diverged

### Behaviors
- B7.1 (py) policy chain first-non-None ↔ (rs): Python-only. Rust doesn't spec a chain model; it checks categories directly.
- B7.2 (py) tool_block_policy ↔ B7.2 (rs): Matched.
- B7.3 (py) tool_allow_policy ↔ B7.3 (rs): Matched.
- B7.4 (py) cwd_policy ↔ B7.1 (rs): Matched — both restrict write tools to allowed paths.
- B7.5 (py) compute_allowed_paths ↔ B7.5 (rs): Matched — code agents get worktrees access.
- B7.6 (py) build_permission_callback ↔ B7.8 (rs): Matched — full chain composition.
- B7.7 (py) discord_send_message self-send block ↔ (rs): Python-only.
- B7.8 (py) discord_send_file auto-resolve ↔ (rs): Python-only.
- B7.9 (py) discord_mcp_server gating ↔ (rs): Python-only.
- B7.10 (py) sdk_mcp_servers_for_cwd ↔ (rs): Python-only (Rust covers in domain 16).
- B7.11-B7.13 (py) MCP server assembly ↔ (rs): Python-only (Rust covers in domain 16).
- B7.6 (rs) path resolution with normalize_path fallback ↔ (py): Rust-only explicit behavior.
- B7.7 (rs) agent name validation ↔ (py): Rust-only explicit behavior.
- B7.9 (rs) Config Debug token redaction ↔ (py): Rust-only explicit behavior.

### Invariants
- I7.1 (py) no MCP self-sends ↔ (rs): Python-only.
- I7.2 (py) no ContextVar for MCP caller ↔ (rs): Python-only.
- I7.3 (py) MCP servers in spawn+reconstruct ↔ I7.6 (rs): Matched — SDK MCP servers survive rebuilds.
- I7.1 (rs) permission timeouts deny ↔ (py): Rust-only.
- I7.2 (rs) path traversal normalize_path ↔ (py): Rust-only.
- I7.3 (rs) agent name validation ↔ (py): Rust-only.
- I7.4 (rs) token redaction ↔ (py): Rust-only.
- I7.5 (rs) CWD write restrictions enforced ↔ (py): Rust-only.

### Key Differences
- Python spec includes MCP server assembly and Discord tool gating (B7.7-B7.13) that Rust moves to domain 16.
- Rust has more security-focused invariants (permission timeout deny, path traversal, name validation, token redaction).
- Python has more application-level invariants (self-send blocking, ContextVar isolation).

---

## 8. Scheduling & Cron Jobs

### Status: Matched

### Behaviors
- B8.1 (py) 10s tick loop ↔ B8.1 (rs): Matched.
- B8.2 (py) croniter cron matching ↔ B8.2 (rs) custom cron_matches: Matched semantically. Python uses croniter library; Rust has custom parser.
- B8.3 (py) one-off fire+remove ↔ B8.4 (rs): Matched.
- B8.4 (py) routing via owner/session/name ↔ B8.10, B8.12 (rs): Matched — both route to owner agent.
- B8.5 (py) per-agent MCP schedule server ↔ B8.7 (rs): Matched.
- B8.6 (py) schedule_create validation ↔ B8.8 (rs): Matched.
- B8.7 (py) per-agent uniqueness + limit ↔ B8.7 (rs): Matched — 20-schedule limit.
- B8.8 (py) schedule_delete scoped by owner ↔ (rs): Python-only explicit. Rust MCP tools are scoped (B8.7) but delete is not separately spec'd.
- B8.9 (py) history dedup 5min ↔ B8.5 (rs): Matched.
- B8.10 (py) prune_history 7 days ↔ B8.6 (rs): Matched.
- B8.11 (py) last_fired init from last_occurrence ↔ (rs): Python-only. Rust uses first-seen skip (B8.3 tracking via last_fired HashMap).
- B8.12 (py) schedules_lock ↔ B8.9 (rs): Matched — both serialize file I/O.
- B8.13 (py) master agent schedule MCP ↔ (rs): Matched implicitly.
- B8.3 (rs) last_fired HashMap dedup ↔ (py): Rust-only explicit. Same effect as Python's last_occurrence init.
- B8.11 (rs) reset_context ↔ (py): Rust-only.
- B8.12 (rs) schedule_key backward compat ↔ (py): Rust-only explicit.

### Invariants
- I8.1 (py) route by owner ↔ (rs): Both. Rust has I8.3 (crash markers routed) which is different.
- I8.2 (py) init from last_occurrence ↔ I8.1 (rs) skip first firing: Matched — different mechanisms, same goal (no spurious catch-up fires).
- I8.3 (py) timezone-aware datetimes: Python-only.
- I8.4 (py) cron in SCHEDULE_TIMEZONE: Python-only.
- I8.5 (py) recurring fires in history ↔ I8.2 (rs) dedup: Matched.
- I8.6 (py) master agent schedule MCP: Python-only.
- I8.3 (rs) crash markers routed: Rust-only.

### Key Differences
- Largely matched. Differences are mostly in implementation detail (croniter vs custom parser, last_occurrence init vs first-seen skip).
- Rust adds reset_context capability (B8.11) not present in Python.
- Rust adds crash marker routing (I8.3) not present in Python.

---

## 9. Logging & Observability

### Status: Python-only

Python defines 12 behaviors covering structured log context propagation via contextvars, per-agent rotating log files, OpenTelemetry tracing integration, and an append-only agent event store with subscriber notification. 3 invariants guard logger naming consistency, context filter attribute completeness, and per-agent propagation settings.

Rust has no equivalent domain. Logging is handled via the tracing crate but is not spec'd.

---

## 10. Discord Rendering & UI

### Status: Diverged

### Behaviors
- B10.1 (py) live-edit with cursor ↔ B2.9 (rs): Matched.
- B10.2 (py) edit throttle 1.5s ↔ B2.9 (rs) 0.8s: Diverged — Python throttles at 1.5s, Rust at 0.8s.
- B10.3 (py) split at 1900 chars ↔ B2.9, B2.16 (rs): Matched — both use 1900-char limit with newline preference.
- B10.4 (py) non-streaming send_long ↔ (rs): Python-only.
- B10.5 (py) deferred last message for timing ↔ (rs): Python-only.
- B10.6 (py) thinking indicator ↔ B2.12 (rs): Matched.
- B10.7 (py) typing context manager ↔ (rs): Python-only.
- B10.8 (py) response timing suffix ↔ B2.11 (rs): Matched.
- B10.9 (py) plan approval file+reactions ↔ B10.9, B14.2 (rs): Matched.
- B10.10 (py) remove unchosen reaction ↔ (rs): Python-only.
- B10.11 (py) AskUserQuestion sequential ↔ B14.3-B14.4 (rs): Matched.
- B10.12 (py) TodoWrite rendering ↔ (rs): Python-only.
- B10.13 (py) debug mode thinking/tool preview ↔ (rs): Python-only.
- B10.14 (py) checkmark/mailbox/X reactions ↔ (rs): Python-only explicit.
- B10.15 (py) compaction spinner ↔ (rs): Python-only explicit.
- B10.16 (py) 429 rate limit backoff ↔ (rs): Python-only explicit.
- B10.17 (py) transient error retry ↔ (rs): Python-only explicit (covered in domain 2 for Rust).
- B10.18 (py) no ResultMessage force-sleep ↔ (rs): Python-only explicit.
- B10.19 (py) resolve_reaction_answer ↔ (rs): Python-only explicit.
- B10.20 (py) DiscordFrontend adapter ↔ B10.2 (rs): Matched.
- B10.1 (rs) Frontend trait ↔ (py): Rust-only. Python uses DiscordFrontend class but doesn't spec a generic trait.
- B10.3 (rs) WebFrontend ↔ (py): Rust-only. Python has no web frontend.
- B10.4-B10.5 (rs) FrontendRouter broadcast + racing ↔ (py): Rust-only. Multi-frontend architecture.
- B10.8 (rs) close_app exit(42) ↔ (py): Rust-only explicit (Python covers in domain 11).
- B10.10 (rs) BotState holds both Router and DiscordFrontend ↔ (py): Rust-only explicit.

### Invariants
- I10.1 (py) thinking indicator hidden on every exit path: Python-only.
- I10.2 (py) rate-limited messages get X: Python-only.
- I10.3 (py) multi-question sequential: Python-only (matched in domain 14 for Rust).
- I10.1 (rs) interactive gates race frontends ↔ (py): Rust-only.
- I10.2 (rs) Discord state extracted into DiscordFrontend ↔ (py): Rust-only.

### Key Differences
- Python has 20 behaviors focusing on Discord-specific rendering details (debug mode, reactions, typing, non-streaming mode).
- Rust has 10 behaviors but introduces multi-frontend architecture (Frontend trait, WebFrontend, FrontendRouter) absent from Python.
- Python has no web frontend at all.
- Edit throttle differs: 1.5s (Python) vs 0.8s (Rust).

---

## 11. Shutdown & Restart

### Status: Diverged

### Behaviors
- B11.1 (py) graceful shutdown poll ↔ B11.2 (rs): Matched.
- B11.2 (py) force shutdown ↔ B11.6 (rs): Matched.
- B11.3 (py) 30s safety deadline ↔ B11.4 (rs): Matched.
- B11.4 (py) kill_supervisor SIGTERM ↔ (rs): Python-only. Rust uses exit code 42 directly.
- B11.5 (py) exit_for_restart exit(42) ↔ B11.5, B11.7 (rs): Matched.
- B11.6 (py) supervisor exit code 42 ↔ B11.7 (rs): Matched.
- B11.7 (py) SIGTERM/SIGINT forwarding ↔ (rs): Python-only. Rust has no supervisor process.
- B11.8 (py) SIGHUP hot restart ↔ (rs): Python-only. Rust has no supervisor process; hot restart handled differently.
- B11.9 (py) bridge mode skip wait ↔ B11.3 (rs): Matched.
- B11.10 (py) idempotent graceful ↔ B11.1 (rs): Matched.
- B11.11 (py) bot.close 10s timeout ↔ (rs): Python-only.
- B11.12 (py) sleep_all swallows exceptions ↔ (rs): Python-only.
- B11.13 (py) startup vs runtime crash distinction ↔ (rs): Python-only (supervisor feature).
- B11.14 (py) 3 consecutive crashes → stop ↔ (rs): Python-only (supervisor feature).
- B11.8 (rs) bridge monitor loop ↔ (py): Rust-only (also spec'd in domain 6).

### Invariants
- I11.1-I11.2 (py) bridge SIGTERM escalation, force-close writer: Python-only (supervisor).
- I11.3 (py) single instance fcntl lock: Python-only.
- I11.4 (py) ensure_default_files in AXI_USER_DATA: Python-only.
- I11.1 (rs) bridge loss triggers exit 42: Rust-only.
- I11.2 (rs) shutdown dedup AtomicBool: Rust-only.

### Key Differences
- Python has a supervisor process (supervisor.py) with signal forwarding, crash counting, and hot restart via SIGHUP. Rust has no supervisor — it relies on systemd directly.
- Rust is simpler (8 behaviors vs 14) because it eliminates the supervisor layer.
- Invariants are entirely disjoint due to architectural difference.

---

## 12. Configuration & Model Selection

### Status: Diverged

### Behaviors
- B12.1 (py) get_model env→config ↔ B12.4 (rs): Matched.
- B12.2 (py) set_model validation ↔ B12.5 (rs): Matched.
- B12.3 (py) test instances AXI_MODEL=haiku ↔ (rs): Python-only.
- B12.4 (py) model at wake time ↔ (rs): Python-only explicit.
- B12.5 (py) post_model_warning ↔ (rs): Python-only.
- B12.6 (py) packs loaded from prompt.md ↔ (rs): Python-only. Rust has no pack system.
- B12.7 (py) make_spawned_agent_system_prompt ↔ (rs): Python-only. Rust has no layered prompt assembly.
- B12.8 (py) SYSTEM_PROMPT.md auto-load ↔ (rs): Python-only.
- B12.9 (py) axi_spawn_agent packs parameter ↔ (rs): Python-only.
- B12.10 (py) load_mcp_servers ↔ B12.7 (rs): Matched.
- B12.11 (py) per-agent config persistence ↔ (rs): Python-only.
- B12.12 (py) restart_agent prompt rebuild ↔ (rs): Python-only.
- B12.13 (py) reconstruction loads agent config ↔ (rs): Python-only.
- B12.14 (py) paths from AXI_USER_DATA ↔ (rs): Python-only.
- B12.15 (py) feature flags from env ↔ (rs): Python-only explicit (Rust has Config::from_env B12.1).
- B12.16 (py) ALLOWED_CWDS assembly ↔ B12.9 (rs): Matched.
- B12.17 (py) prompt hash ↔ (rs): Python-only.
- B12.1 (rs) Config::from_env ↔ (py): Rust-only explicit.
- B12.2 (rs) token resolution slot-based fallback ↔ (py): Rust-only explicit. Python has this but in domain 4 (axi_test).
- B12.3 (rs) ALLOWED_USER_IDS required ↔ (py): Rust-only explicit.
- B12.6 (rs) config lock via Mutex ↔ (py): Matched with B3.11 (py).
- B12.8 (rs) DiscordClient retry/backoff ↔ (py): Rust-only explicit (Python covers in domain 15).
- B12.10 (rs) Config::for_test ↔ (py): Rust-only.
- B12.11 (rs) --permission-prompt-tool stdio ↔ (py): Rust-only explicit (also in domain 6).

### Invariants
- I12.1 (py) config lock across full cycle ↔ (rs): Matched (both have this invariant).
- I12.2 (py) specialized agent prompt rebuild: Python-only.
- I12.3 (py) default files in AXI_USER_DATA: Python-only.
- I12.1 (rs) token redaction: Rust-only.
- I12.2 (rs) --permission-prompt-tool: Rust-only.

### Key Differences
- Python has extensive prompt/pack system (B12.6-B12.9, B12.11-B12.13, B12.17) with no Rust equivalent. This is a significant feature gap in Rust.
- Rust has no packs, no SYSTEM_PROMPT.md auto-loading, no per-agent config persistence.
- Python has 17 behaviors vs Rust's 11.

---

## 13. Hot Restart & Bridge Reconnection

### Status: Matched

### Behaviors
- B13.1 (py) SIGHUP kills only bot.py ↔ (rs): Python-only (supervisor-based).
- B13.2 (py) connect_procmux list+reconnect ↔ B13.1 (rs): Matched.
- B13.3 (py) kill orphan agents ↔ B13.2 (rs): Matched.
- B13.4 (py) reconnect_single init→subscribe ↔ B13.4 (rs): Matched.
- B13.5 (py) intercept initialize on reconnect ↔ (rs): Covered in domain 6 (B6.13 rs).
- B13.6 (py) cli_status exited cleanup ↔ B13.5 (rs): Matched.
- B13.7 (py) bridge_busy for mid-task ↔ B13.6 (rs): Matched.
- B13.8 (py) queued_reconnecting status ↔ B13.3 (rs): Matched — session marked reconnecting.
- B13.9 (py) SIGTERM full stop ↔ (rs): Python-only (supervisor).
- B13.10 (py) restore_slot ↔ B13.8 (rs): Matched.
- B13.7 (rs) connect_bridge backoff ↔ (py): Rust-only explicit.

### Invariants
- I13.1 (py) SDK init before subscribe ↔ (rs): Matched semantically (Rust has no explicit invariant but behavior covers it).
- I13.2 (py) reconnecting=True for resumed transport ↔ (rs): Python-only.
- I13.3 (py) restore_slot on reconnect ↔ (rs): Python-only.
- I13.1 (rs) startup backoff for procmux: Rust-only.

### Key Differences
- Functionally equivalent despite supervisor vs systemd architectural difference.
- Python has more invariants (3) from bug history; Rust has 1.

---

## 14. Interactive Gates

### Status: Diverged

### Behaviors
- B14.1 (py) plan file attachment ↔ B10.9 (rs): Matched.
- B14.2 (py) pre-add reactions ↔ B14.2 (rs): Matched.
- B14.3 (py) remove unchosen reaction ↔ (rs): Python-only.
- B14.4 (py) plan approval clears plan_mode ↔ (rs): Python-only.
- B14.5 (py) sequential question collection ↔ B14.3 (rs): Matched.
- B14.6 (py) number keycap reactions ↔ B14.3 (rs): Matched.
- B14.7 (py) reaction→option mapping ↔ B14.4 (rs): Matched.
- B14.8 (py) AskUserQuestion updated_input ↔ (rs): Python-only.
- B14.9 (py) TodoWrite post+persist ↔ (rs): Python-only.
- B14.10 (py) channel status plan_review/question ↔ (rs): Python-only.
- B14.11 (py) hook policies in chain ↔ (rs): Python-only.
- B14.12 (py) no session = allow ↔ (rs): Python-only.
- B14.13 (py) /stop resolves futures ↔ (rs): Python-only.
- B14.1 (rs) pending_questions by message ID ↔ (py): Rust-only explicit.
- B14.5 (rs) fallback legacy plan approval ↔ (py): Rust-only.
- B14.6 (rs) message dedup VecDeque ↔ (py): Rust-only.
- B14.7 (rs) FrontendRouter races gates ↔ (py): Rust-only (multi-frontend).
- B14.8 (rs) no frontends = auto-approve ↔ (py): Rust-only.
- B14.9 (rs) reaction filter by allowed users ↔ (py): Rust-only explicit.
- B14.10 (rs) Frontend trait gates ↔ (py): Rust-only (multi-frontend).
- B14.11 (rs) goodbye to master channel only ↔ (py): Rust-only explicit.

### Invariants
- I14.1 (py) multi-question sequential ↔ (rs): Python-only.
- I14.2 (py) plan file search in CWD ↔ (rs): Python-only.
- I14.3 (py) plan fallback to disk ↔ (rs): Python-only.
- I14.4 (py) ignore stale plan files ↔ (rs): Python-only.
- I14.1 (rs) gates race across frontends ↔ (py): Rust-only.

### Key Differences
- Python specs more Discord-specific gate behaviors (channel status, /stop resolution, TodoWrite).
- Rust specs multi-frontend gate racing, message dedup, and legacy fallback behaviors.
- Invariants are entirely disjoint.

---

## 15. Discord Query Client

### Status: Python-only

Python defines 22 behaviors covering sync and async httpx-based Discord REST clients, rate-limit retry, 5xx backoff, channel listing, message pagination, file uploads, reaction URL-encoding, wait-for-messages polling, snowflake resolution, message formatting, and a CLI with query/wait subcommands. 2 invariants guard emoji URL-encoding and use of the httpx wrapper.

Rust has no equivalent domain. Discord REST operations are handled by DiscordClient in the config crate (B12.8 in Rust), but the full query/wait CLI tooling is not implemented.

---

## 16. MCP Tools & Protocol

### Status: Rust-only

Rust defines 12 behaviors covering JSON-RPC 2.0 MCP protocol implementation, McpServer tool registration and dispatch, per-agent MCP server configuration (utils, schedule, discord, master/agent variants), permission handling integration, and agent name validation. 2 invariants guard SDK MCP server survival across session rebuilds and flowcoder config.

Python distributes equivalent functionality across domains 7 (permissions/tool gating) and 12 (MCP server assembly). The Rust spec consolidates this into a dedicated domain.

---

## 17. Flowchart Execution

### Status: Rust-only

Rust defines 24 behaviors covering a flowchart execution engine: typed block model (start/end/prompt/branch/variable/bash/command/refresh/exit/spawn/wait), validation, graph walker state machine, template interpolation, branch condition evaluation, variable coercion, command resolution across search paths, sub-command recursion with depth limits, output schema extraction, cancellation/pause/resume, an NDJSON proxy engine, and a TUI binary. 3 invariants guard startup control_request draining, pre-query flush, and SDK MCP server preservation.

Python has no flowchart execution system.

---

## 18. Voice I/O

### Status: Rust-only

Rust defines 14 behaviors covering Discord voice channel connectivity via Songbird, STT via Deepgram Nova-3 WebSocket streaming, TTS via OpenAI/Piper/espeak-ng with resampling, audio downsampling (48kHz stereo to 16kHz mono), playback serialization, transcript filtering, voice command routing, and cancellation coordination. 5 invariants guard library decoupling, transcript delivery, TTS non-overlap, WebSocket keepalive, and deadlock prevention.

Python has no voice I/O system.
