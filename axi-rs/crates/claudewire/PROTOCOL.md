# Claude CLI Stream-JSON Protocol

Reference for the `--input-format stream-json --output-format stream-json` wire protocol between the Claude CLI process and its host (claudewire/SDK). Documented from real bridge-stdio logs (23,000+ messages analyzed).

## Transport

- Communication is over stdin/stdout of the Claude CLI process
- Each message is a single JSON line (newline-delimited JSON)
- Messages flow in two directions:
  - **Inbound** (CLI → host): CLI sends events, requests, and results
  - **Outbound** (host → CLI): host sends user messages, control responses
- Every message has a `type` field as the top-level discriminator

### Stderr

The CLI also emits unstructured plaintext on stderr (enabled by launching with `--debug-to-stderr`). This is **not** part of the NDJSON protocol — it's a side channel of debug output. Claudewire reads it via `StderrEvent` and forwards it to a callback.

The only actionable stderr output is the **autocompact debug line**, emitted after each query completes:

```
autocompact: tokens=4069 threshold=80 effectiveWindow=200000
```

This provides the current context token count and window size. It is the **only source** of post-compaction token counts (see [Context Compaction](#context-compaction)). The host parses it with:

```
autocompact: tokens=(\d+) threshold=\d+ effectiveWindow=(\d+)
```

All other stderr content is noise and not parsed.

## Session Lifecycle

```
                    ┌─────────────────────────────────┐
                    │         CLI Process Start        │
                    └────────────────┬────────────────┘
                                     │
                    ┌────────────────▼────────────────┐
  OUT  ───────────► │    control_request.initialize    │ (reconnect only)
                    │    control_response.success      │
                    └────────────────┬────────────────┘
                                     │
                    ┌────────────────▼────────────────┐
  IN   ◄─────────── │         system.init              │
                    │  (tools, model, mcp_servers...)  │
                    └────────────────┬────────────────┘
                                     │
                    ┌────────────────▼────────────────┐
  IN   ◄─────────── │     MCP handshake (repeated)     │
                    │  control_request.mcp_message     │
  OUT  ───────────► │  control_response.success        │
                    └────────────────┬────────────────┘
                                     │
  OUT  ───────────► │      user  (initial prompt)      │
                    │                                  │
                    └────────────────┬────────────────┘
                                     │
                    ┌────────────────▼────────────────┐
                    │        Query Loop (turns)        │ ◄──┐
                    │                                  │    │
                    │  1. stream_event.message_start   │    │
                    │  2. stream_event.content_block_* │    │
                    │  3. assistant (complete message)  │    │
                    │  4. control_request.can_use_tool  │    │
                    │  5. control_response.success      │    │
                    │  6. stream_event.message_delta    │    │
                    │  7. stream_event.message_stop     │    │
                    │  8. rate_limit_event (optional)   │    │
                    │  9. user (tool result)            │    │
                    └────────────────┬─────────────────┘    │
                                     │                      │
                                     │  (if tool_use)  ─────┘
                                     │
                    ┌────────────────▼────────────────┐
  IN   ◄─────────── │         result.success           │
                    └─────────────────────────────────┘
```

### New Query (Multi-turn)

After a `result`, the host can send another `user` message to start a new turn. The CLI reuses the same session.

```
  OUT ──► user (new prompt)
  IN  ◄── system.init (re-emitted with current state)
  IN  ◄── [query loop as above]
  IN  ◄── result.success
```

### Reconnection

When reconnecting to an existing CLI process (e.g. after host restart), the host sends `control_request.initialize` first. The CLI responds with `control_response.success`, then the normal flow resumes.

### Context Compaction

When the conversation context gets large, the CLI compacts it:
```
  IN  ◄── system.status  {"status": "compacting"}
  IN  ◄── system.compact_boundary  {compact_metadata: {trigger, pre_tokens}}
  IN  ◄── system.status  {"status": null}   (compaction done)
```

**Post-compaction token count**: The `compact_boundary` message carries `pre_tokens` (context size before compaction) but does **not** include `post_tokens`. The post-compaction token count only becomes available on the **next query**, when the CLI emits its `autocompact:` debug line on stderr:

```
stderr: autocompact: tokens=4069 threshold=80 effectiveWindow=200000
```

This stderr line is parsed by the host (regex: `autocompact: tokens=(\d+) threshold=\d+ effectiveWindow=(\d+)`) to update `context_tokens` and `context_window`. The host defers the compaction summary message until the next query completes, at which point both `pre_tokens` (from `compact_boundary`) and `post_tokens` (from stderr) are available:

```
🔄 Compacted in 596.9s: 150,602 → 4,069 tokens (146,533 freed, 2% used)
```

## Message Types — Inbound (CLI → Host)

### `system`

Session metadata. Dynamic extra fields depending on subtype.

| Subtype | Description | Extra Fields |
|---------|-------------|--------------|
| `init` | Session start/re-init | `cwd`, `session_id`, `tools[]`, `mcp_servers[]`, `model`, `permissionMode`, `slash_commands[]`, `apiKeySource`, `claude_code_version`, `output_style`, `agents[]`, `skills[]`, `plugins[]`, `fast_mode_state`, `uuid`, `betas[]?` |
| `status` | Status change | `status` (`"compacting"` \| null), `permissionMode?`, `session_id`, `uuid` |
| `compact_boundary` | Context compaction marker | `compact_metadata: {trigger: "manual"\|"auto", pre_tokens}`, `session_id`, `uuid` |
| `microcompact_boundary` | Lightweight compaction | _(similar to compact_boundary)_ |
| `local_command_output` | Output from slash command (e.g. /cost) | `content`, `session_id`, `uuid` |
| `task_started` | Subagent task spawned | `task_id`, `tool_use_id?`, `description`, `task_type?`, `session_id`, `uuid` |
| `task_progress` | Subagent progress update | `task_id`, `tool_use_id?`, `description`, `usage: {total_tokens, tool_uses, duration_ms}`, `last_tool_name?`, `session_id`, `uuid` |
| `task_notification` | Subagent completed/failed | `task_id`, `tool_use_id?`, `status: "completed"\|"failed"\|"stopped"`, `output_file`, `summary`, `usage?`, `session_id`, `uuid` |
| `hook_started` | Pre-commit/post-commit hook started | `hook_id`, `hook_name`, `hook_event`, `session_id`, `uuid` |
| `hook_progress` | Hook producing output | `hook_id`, `hook_name`, `hook_event`, `stdout`, `stderr`, `output`, `session_id`, `uuid` |
| `hook_response` | Hook finished | `hook_id`, `hook_name`, `hook_event`, `output`, `stdout`, `stderr`, `exit_code?`, `outcome: "success"\|"error"\|"cancelled"`, `session_id`, `uuid` |
| `files_persisted` | Files uploaded/persisted | `files: [{filename, file_id}]`, `failed: [{filename, error}]`, `processed_at`, `session_id`, `uuid` |
| `elicitation_complete` | MCP elicitation completed | `mcp_server_name`, `elicitation_id`, `session_id`, `uuid` |

### `stream_event`

Wrapper around Anthropic API streaming events. Every stream event is emitted twice: once as `stream_event` (with session metadata) and once as a bare event (for backward compatibility).

```json
{
  "type": "stream_event",
  "uuid": "string",
  "session_id": "string",
  "parent_tool_use_id": "string|null",
  "event": { /* Anthropic stream event */ }
}
```

#### Inner stream events (`event` field)

| Event Type | Fields | Description |
|-----------|--------|-------------|
| `message_start` | `message: {model, id, type, role, content[], usage}` | Start of an API turn |
| `message_delta` | `delta: {stop_reason, stop_sequence}`, `usage`, `context_management` | End of turn metadata |
| `message_stop` | _(none)_ | Turn complete |
| `content_block_start` | `index`, `content_block: ContentBlock` | New content block |
| `content_block_delta` | `index`, `delta: Delta` | Incremental content |
| `content_block_stop` | `index` | Content block complete |

**ContentBlock** (discriminated on `type`):
- `text`: `{type, text}`
- `tool_use`: `{type, id, name, input, caller?}`
- `thinking`: `{type, thinking, signature}`
- `server_tool_use`: `{type, id, name, input}` — server-side tools (web search, etc.)
- `web_search_20250305`: web search tool result block
- `mcp_tools`: MCP tool invocation block
- `citations_delta`: citation references

**Delta** (discriminated on `type`):
- `text_delta`: `{type, text}`
- `input_json_delta`: `{type, partial_json}`
- `thinking_delta`: `{type, thinking}`
- `signature_delta`: `{type, signature}`
- `citations_delta`: `{type, citations}`

### `assistant`

Complete assistant message after all streaming is done. Contains the full assembled content.

```json
{
  "type": "assistant",
  "message": {
    "model": "claude-opus-4-6",
    "id": "msg_...",
    "type": "message",
    "role": "assistant",
    "content": [ContentBlock, ...],
    "stop_reason": null,
    "stop_sequence": null,
    "usage": Usage,
    "context_management": null
  },
  "parent_tool_use_id": "string|null",
  "session_id": "string",
  "uuid": "string"
}
```

### `user`

User message, typically tool results being fed back.

```json
{
  "type": "user",
  "message": {
    "role": "user",
    "content": "string" | [UserContentBlock, ...]
  },
  "session_id": "string",
  "parent_tool_use_id": "string|null",
  "uuid": "string",
  "tool_use_result": any,
  "isSynthetic": bool       // optional
}
```

`tool_use_result` is highly polymorphic — it can be a string, list, or dict with tool-specific fields depending on which tool ran (e.g. `stdout`/`stderr` for Bash, `file` for Read, `filenames` for Glob, `answers` for AskUserQuestion, etc.).

### `result`

Query completion. Two variants based on subtype:

**Success:**
```json
{
  "type": "result",
  "subtype": "success",
  "is_error": false,
  "duration_ms": 57095,
  "duration_api_ms": 31197,
  "num_turns": 2,
  "result": "string",
  "stop_reason": "string|null",
  "total_cost_usd": 0.707,
  "usage": Usage,
  "modelUsage": { "model-name": ModelUsage },
  "permission_denials": [PermissionDenial],
  "structured_output": any,       // optional, only with --json-schema
  "fast_mode_state": "off" | "cooldown" | "on",
  "session_id": "string",
  "uuid": "string"
}
```

**Error:**
```json
{
  "type": "result",
  "subtype": "error_during_execution" | "error_max_turns" | "error_max_budget_usd" | "error_max_structured_output_retries",
  "is_error": true,
  "errors": ["string"],
  "duration_ms": 0,
  "duration_api_ms": 0,
  "num_turns": 0,
  "stop_reason": "string|null",
  "total_cost_usd": 0,
  "usage": Usage,
  "modelUsage": {},
  "permission_denials": [PermissionDenial],
  "fast_mode_state": "off",
  "session_id": "string",
  "uuid": "string"
}
```

**PermissionDenial:** `{tool_name, tool_use_id, tool_input: {}}`

### `control_request`

CLI asks the host to do something. Host must respond with `control_response`.

```json
{
  "type": "control_request",
  "request_id": "string",
  "request": { "subtype": "string", ...extra }
}
```

| Subtype | Purpose | Extra Fields |
|---------|---------|--------------|
| `can_use_tool` | Permission check | `tool_name`, `input`, `permission_suggestions[]`, `tool_use_id`, `decision_reason?` |
| `mcp_message` | MCP tool call relay | `server_name`, `message: {method, jsonrpc, id, params?}` |
| `initialize` | Session init handshake | `sdkMcpServers?` |
| `interrupt` | Interrupt current turn | _(minimal)_ |
| `set_mode` | Change permission mode | `mode` |
| `elicitation` | MCP elicitation request | `mode: "form"\|"url"`, `message`, `requestedSchema?`, `elicitationId?`, `url?` |

**Interactive tools via `can_use_tool`:** Some Claude Code tools require user interaction through the permission system. The CLI sends `can_use_tool` and blocks until the host responds with allow/deny. The SDK provides no built-in support for these — the host must implement the interactive flow in its `can_use_tool` callback:

| Tool Name | `input` Fields | Expected Host Behavior |
|-----------|---------------|----------------------|
| `EnterPlanMode` | _(empty)_ | Auto-allow. Switches the agent to plan mode. |
| `ExitPlanMode` | `allowedPrompts[]?` | Present the agent's plan to the user (read from `~/.claude/plans/*.md` or CWD `PLAN.md`). Wait for user approval. Allow = proceed with implementation, Deny = revise the plan (include feedback in deny message). |
| `AskUserQuestion` | `questions[]: {question, header, options[], multiSelect}` | Display each question with options to the user. Collect answers. Return them in the allow response's `updatedInput.answers` dict, keyed by question text. |

### `control_response`

Response to a `control_request` from the host (inbound only in rare cases like init handshake).

```json
{
  "type": "control_response",
  "response": {
    "subtype": "success" | "error",
    "request_id": "string",
    "response": any,
    "error": "string|null"
  }
}
```

### `rate_limit_event`

Rate limit status update, typically after each API call.

```json
{
  "type": "rate_limit_event",
  "rate_limit_info": {
    "status": "allowed" | "allowed_warning" | "rejected",
    "resetsAt": 1772766000,          // optional, epoch seconds
    "rateLimitType": "five_hour" | "seven_day" | "seven_day_opus" | "seven_day_sonnet" | "overage",
    "utilization": 0.9,              // optional, 0.0-1.0
    "isUsingOverage": false,         // optional
    "surpassedThreshold": 0.9,       // optional
    "overageStatus": "allowed" | "allowed_warning" | "rejected",  // optional
    "overageResetsAt": 1772766000,   // optional
    "overageDisabledReason": "org_level_disabled" | "out_of_credits" | ...  // optional
  },
  "uuid": "string",
  "session_id": "string"
}
```

Full `overageDisabledReason` enum: `"overage_not_provisioned"`, `"org_level_disabled"`, `"org_level_disabled_until"`, `"out_of_credits"`, `"seat_tier_level_disabled"`, `"member_level_disabled"`, `"seat_tier_zero_credit_limit"`, `"group_zero_credit_limit"`, `"member_zero_credit_limit"`, `"org_service_level_disabled"`, `"org_service_zero_credit_limit"`, `"no_limits_configured"`, `"unknown"`.

**Rate limit windows are reported separately.** Each event carries a single `rateLimitType` — either `"five_hour"` or `"seven_day"` (or `null`). They are not combined into one event. A given API call may emit one event for `five_hour`, one for `seven_day`, both, or neither. To track utilization across both windows, keep the most recent event per `rateLimitType`.

### `tool_progress`

Periodic heartbeat during long-running tool execution (e.g. background tasks, slow Bash commands).

```json
{
  "type": "tool_progress",
  "tool_use_id": "string",
  "tool_name": "string",
  "parent_tool_use_id": "string|null",
  "elapsed_time_seconds": 12.5,
  "task_id": "string",         // optional, for Task subagents
  "uuid": "string",
  "session_id": "string"
}
```

### `tool_use_summary`

Aggregated summary of tool calls, emitted in streamlined output mode.

```json
{
  "type": "tool_use_summary",
  "summary": "Read 2 files, wrote 1 file",
  "preceding_tool_use_ids": ["toolu_..."],
  "uuid": "string",
  "session_id": "string"
}
```

### `auth_status`

Authentication state change (e.g. OAuth flow in progress).

```json
{
  "type": "auth_status",
  "isAuthenticating": true,
  "output": ["string"],
  "error": "string",           // optional
  "uuid": "string",
  "session_id": "string"
}
```

### `prompt_suggestion`

Predicted next user prompt, emitted after each turn when prompt suggestions are enabled.

```json
{
  "type": "prompt_suggestion",
  "suggestion": "Run the tests",
  "uuid": "string",
  "session_id": "string"
}
```

### `streamlined_text` / `streamlined_tool_use_summary`

Internal-only. In streamlined output mode, these replace full `assistant` messages:
- `streamlined_text`: Contains `text` (thinking/tool_use blocks stripped)
- `streamlined_tool_use_summary`: Contains `tool_summary` string (e.g. "Read 2 files, wrote 1 file")

### `keep_alive`

Periodic heartbeat to keep the connection alive. No meaningful fields.

### `control_cancel_request`

Cancels a pending `control_request`. Contains the `request_id` of the request to cancel.

## Message Types — Outbound (Host → CLI)

### `user`

Send a prompt or continue conversation.

```json
{
  "type": "user",
  "content": "string",                          // simple form
  "session_id": "string",                       // optional
  "message": {"role": "user", "content": ...},  // alternative form
  "parent_tool_use_id": "string|null"           // optional
}
```

### `control_request`

Host-initiated control (mainly `initialize` on reconnect).

```json
{
  "type": "control_request",
  "request_id": "string",
  "request": { "subtype": "initialize", "hooks": null }
}
```

### `control_response`

Response to an inbound `control_request`.

```json
{
  "type": "control_response",
  "response": {
    "subtype": "success",
    "request_id": "string",
    "response": {
      "behavior": "allow",           // for can_use_tool
      "updatedInput": {},            // optional modified tool input
      "mcp_response": {}             // for mcp_message
    }
  }
}
```

**Error response:**
```json
{
  "type": "control_response",
  "response": {
    "subtype": "error",
    "request_id": "string",
    "error": "string"
  }
}
```

### `update_environment_variables`

Hot-update environment variables on a running CLI process (e.g. refresh OAuth token).

```json
{
  "type": "update_environment_variables",
  "variables": { "KEY": "value" }
}
```

## Shared Types

### Usage

Token usage, present in message_start, message_delta, assistant, and result messages.

```json
{
  "input_tokens": 100,
  "output_tokens": 200,
  "cache_creation_input_tokens": 50,
  "cache_read_input_tokens": 300,
  "cache_creation": {
    "ephemeral_5m_input_tokens": 0,
    "ephemeral_1h_input_tokens": 920
  },
  "service_tier": "standard",
  "inference_geo": "not_available",
  "iterations": [...],              // result only
  "server_tool_use": {              // result only
    "web_search_requests": 1,
    "web_fetch_requests": 0
  },
  "speed": "fast"                   // result only
}
```

### ModelUsage (inside result.modelUsage)

Per-model cost breakdown.

```json
{
  "claude-opus-4-6": {
    "inputTokens": 100,
    "outputTokens": 200,
    "cacheCreationInputTokens": 50,
    "cacheReadInputTokens": 300,
    "contextWindow": 200000,
    "maxOutputTokens": 16384,
    "costUSD": 0.05,
    "webSearchRequests": 0
  }
}
```

## Query Flow Detail

A typical single-tool-use turn:

```
IN  system.init                     # session metadata
IN  stream_event.message_start      # API turn begins
IN  stream_event.content_block_start  # text or thinking or tool_use block
IN  stream_event.content_block_delta  # incremental content (repeated)
IN  assistant                       # complete assembled message
IN  control_request.can_use_tool    # permission check for tool
OUT control_response.success        # allow the tool
IN  stream_event.content_block_stop # block complete
IN  stream_event.message_delta      # stop_reason, usage
IN  stream_event.message_stop       # turn complete
IN  rate_limit_event                # rate limit status (optional)
IN  user                            # tool result fed back
IN  stream_event.message_start      # next turn begins...
...
IN  result.success                  # query complete
```

Key ordering rules:
- `assistant` (complete message) arrives **before** `content_block_stop` / `message_delta` / `message_stop`
- `control_request.can_use_tool` arrives after `assistant` but before the stream events close
- `rate_limit_event` comes after `message_stop`, before the next turn's `user`
- `user` (tool result) arrives between turns
- Multiple content blocks in one turn are sequential: start→deltas→stop, start→deltas→stop

## Dual Emission (stream_event + bare)

The CLI emits every inner stream event twice on stdout:
1. As `stream_event` with `uuid`, `session_id`, `parent_tool_use_id` envelope
2. Immediately followed by the same event bare (e.g. `{"type": "content_block_delta", ...}` with no envelope)

This is **CLI behavior**, not added by procmux or claudewire — the SDK's own `SubprocessCLITransport` sees the same duplication. The bare events are presumably for backward compatibility with consumers that don't understand the `stream_event` wrapper.

The SDK's `parse_message()` only recognizes the wrapped `stream_event` form. Bare events hit the unknown-type path and raise `MessageParseError`.

**claudewire filters bare duplicates.** `CliSession::read_message()` drops any message whose `type` is in the bare stream event set (`message_start`, `message_delta`, `message_stop`, `content_block_start`, `content_block_delta`, `content_block_stop`). This halves the message volume with no data loss — the wrapped `stream_event` form carries the same payload plus session metadata.

## MCP Handshake

On session start, the CLI initializes each MCP server through a series of `control_request.mcp_message` / `control_response.success` exchanges. This happens after `system.init` but before any user query:

```
IN  control_request.mcp_message   {method: "initialize", params: {clientInfo, capabilities, protocolVersion}}
OUT control_response.success      {mcp_response: {result: {serverInfo, capabilities, protocolVersion}}}
IN  control_request.mcp_message   {method: "notifications/initialized"}
OUT control_response.success      {}
IN  control_request.mcp_message   {method: "tools/list"}
OUT control_response.success      {mcp_response: {result: {tools: [...]}}}
```

This repeats for each configured MCP server.

## OTel Trace Context

Outbound messages may include a `_trace_context` field injected by OpenTelemetry for distributed tracing:

```json
{
  "type": "user",
  "content": "hello",
  "_trace_context": {"traceparent": "00-abc123-def456-01"}
}
```

This field is stripped before schema validation and is never part of the protocol schema.

## Provenance — Upstream vs Our Additions

The protocol has three layers: the upstream Claude CLI wire format (Anthropic's code), the
claude-agent-sdk transport contract, and our own claudewire/procmux additions.

### Upstream (Claude CLI / Anthropic)

Everything the CLI emits on stdout and accepts on stdin is upstream. We have no control over
these messages — they can change with any CLI update.

| What | Origin |
|------|--------|
| All inbound message types (`stream_event`, `assistant`, `user`, `system`, `result`, `control_request`, `control_response`, `rate_limit_event`, `tool_progress`, `tool_use_summary`, `auth_status`, `prompt_suggestion`, `keep_alive`, `control_cancel_request`, `streamlined_text`, `streamlined_tool_use_summary`) | Claude CLI stdout |
| Inner stream events (`message_start`, `message_delta`, `message_stop`, `content_block_*`) | Anthropic Messages API streaming, wrapped by CLI |
| Content blocks (`text`, `tool_use`, `thinking`, `server_tool_use`, `web_search_20250305`, `mcp_tools`) and deltas (`text_delta`, `input_json_delta`, `thinking_delta`, `signature_delta`, `citations_delta`) | Anthropic Messages API |
| `system.init` fields (`tools`, `model`, `mcp_servers`, `permissionMode`, `slash_commands`, `agents`, `skills`, `plugins`, `fast_mode_state`, etc.) | Claude CLI session state |
| `system.status` and `system.compact_boundary` | Claude CLI context management |
| `control_request.can_use_tool` (permission check) | Claude CLI permission system |
| `control_request.mcp_message` (MCP relay) | Claude CLI MCP integration |
| `control_request.initialize` / `control_request.interrupt` / `control_request.set_mode` / `control_request.elicitation` | Claude CLI session control |
| `result` message (query completion with costs, usage, modelUsage, permission_denials) | Claude CLI |
| `rate_limit_event` (status, resetsAt, utilization, overage fields) | Claude CLI / Anthropic rate limiting |
| `assistant` message (complete assembled message after streaming) | Claude CLI |
| `user` message echoed back with `tool_use_result` and `isSynthetic` | Claude CLI |
| Dual emission (each stream event emitted twice: wrapped + bare) | Claude CLI behavior |
| MCP handshake sequence (initialize → notifications/initialized → tools/list) | Claude CLI, following MCP spec |
| Task subagent lifecycle (`system.task_started`, `system.task_progress`, `system.task_notification`, `tool_progress`) | Claude CLI task system |
| Teammate mailbox protocol (filesystem IPC via `~/.claude/tasks/`) | Claude CLI teams feature |
| Hooks lifecycle (`system.hook_started`, `system.hook_progress`, `system.hook_response`, `system.stop_hook_summary`) | Claude CLI hooks system |
| Elicitation protocol (`control_request.elicitation`, `system.elicitation_complete`) | Claude CLI MCP elicitation |
| `update_environment_variables` outbound message | claude-agent-sdk contract |
| `updatedInput` in permission responses (tool input modification) | claude-agent-sdk contract |

### Our Additions (claudewire/axi)

These are behaviors we add on top of the upstream protocol. They are not part of the CLI
wire format.

| What | Where | Description |
|------|-------|-------------|
| `_trace_context` field on outbound messages | `session.rs` | OTel distributed tracing injection. Injected by `CliSession::write()` before sending to CLI stdin. Not part of the protocol — just piggybacks on the JSON payload. |
| Reconnect initialize interception | `session.rs` | When `reconnecting=true`, `CliSession::write()` intercepts outbound `control_request.initialize` and synthesizes a fake `control_response.success` locally instead of forwarding to the CLI. The CLI is already initialized — this satisfies the SDK's handshake without confusing the running process. |
| Schema types with `#[serde(flatten)] extra` | `schema.rs` | Serde types use `extra: HashMap` to capture unknown fields, so new upstream fields don't break deserialization. |
| `is_bare_stream_type()` | `schema.rs` | Identifies bare stream events for filtering. Used by `CliSession::read_message()`. |
| Bare event filtering | `session.rs` | `CliSession::read_message()` drops bare stream events (the CLI's duplicate emission), halving message volume. Only wrapped `stream_event` forms are yielded to consumers. |
| procmux buffer replay | `procmux/` | When the bot reconnects after a restart, procmux replays buffered stdout messages. These replayed messages are identical to the originals — procmux adds nothing to the payload. |
| Early message injection (interrupt-and-inject) | `axi` crate | When a user sends a message while an agent is busy, the host sends `control_request.interrupt` to abort the current turn, then processes the queued message as the next query. This is a graceful abort — the CLI keeps its session and conversation context. See "Early Message Injection" section below. |

### Summary

```
┌──────────────────────────────────────────────────────┐
│                  Anthropic API                        │  Anthropic servers
│  (Messages API streaming: message_start, deltas...)  │
└────────────────────────┬─────────────────────────────┘
                         │
┌────────────────────────▼─────────────────────────────┐
│                   Claude CLI                          │  Upstream binary
│  Wraps API events in stream_event envelope            │
│  Adds: system, result, control_request, rate_limit    │
│  Dual-emits stream events (wrapped + bare)            │
│  Manages: MCP relay, permissions, context compaction  │
└────────────────────────┬─────────────────────────────┘
                         │ stdout (NDJSON)
┌────────────────────────▼─────────────────────────────┐
│                    procmux                            │  Ours (transport)
│  Relays stdout/stdin opaquely (zero semantic layer)   │
│  Buffers output during disconnects, replays on sub    │
└────────────────────────┬─────────────────────────────┘
                         │
┌────────────────────────▼─────────────────────────────┐
│                   claudewire                          │  Ours (protocol layer)
│  CliSession: subprocess management + message routing  │
│  Adds: _trace_context injection, reconnect intercept  │
│  Adds: serde schema types, NDJSON parsing             │
│  Filters: bare stream event duplicates (CLI dual emit) │
└──────────────────────────────────────────────────────┘
```

## Task Subagents

When Claude uses the `Task` (or `Agent`) tool, it spawns a subagent — an independent agent loop running in its own process or in-process thread. The parent and subagent communicate through the stream-json protocol and a filesystem mailbox.

### Task Types

| Type | Description |
|------|-------------|
| `local_bash` | Subagent running as a local subprocess |
| `local_agent` | Subagent running as a local subprocess (agent mode) |
| `remote_agent` | Subagent on a remote server |
| `in_process_teammate` | Teammate running in the same process (teams feature) |

### Task Tool Input Schema

```json
{
  "description": "string",      // 3-5 word description
  "prompt": "string",           // the task prompt
  "subagent_type": "string",    // agent type: "general-purpose", "Explore", "Plan", etc.
  "model": "sonnet|opus|haiku", // optional model override
  "resume": "string",           // optional agent ID to resume
  "run_in_background": true,    // optional: run asynchronously
  "max_turns": 50,              // optional: turn limit
  "name": "string",             // optional: for teammates
  "team_name": "string",        // optional: for teams
  "mode": "string",             // optional: permission mode
  "isolation": "string"         // optional: isolation level
}
```

### Task Lifecycle Messages

When a task is spawned, the parent receives these `system` messages:

```
IN  system.task_started       {task_id, description, task_type}
IN  system.task_progress      {task_id, description, usage: {total_tokens, tool_uses, duration_ms}, last_tool_name?}
    ... (repeated periodically)
IN  tool_progress             {tool_use_id, tool_name, elapsed_time_seconds, task_id}
    ... (heartbeats during execution)
IN  system.task_notification  {task_id, status: "completed"|"failed"|"stopped", output_file, summary, usage?}
```

The `output_file` in `task_notification` points to a file containing the subagent's output, which the parent reads via the `TaskOutput` tool.

### `parent_tool_use_id` Field

Every message in the protocol has a `parent_tool_use_id` field (string or null). For top-level messages, this is null. For messages from within a subagent context, it contains the `tool_use_id` of the `Task` tool call that spawned the subagent. This allows the host to route messages to the correct subagent's display context.

### Background vs Foreground Tasks

- **Foreground** (`run_in_background: false`): The parent's agent loop blocks until the task completes. Messages stream as `tool_progress` heartbeats.
- **Background** (`run_in_background: true`): The parent continues working. Task status is delivered via `system.task_started`, `system.task_progress`, and `system.task_notification`.

### Available Subagent Types

Extracted from the CLI binary (these may change with CLI versions):

| Subagent Type | Description |
|---------------|-------------|
| `general-purpose` | Default agent with full tool access |
| `Explore` | Codebase exploration (read-only tools) |
| `Plan` | Planning agent |
| `codebase-locator` | Find files/functions in codebase |
| `thoughts-locator` | Find thinking patterns |
| `thoughts-analyzer` | Analyze thinking patterns |
| `web-search-researcher` | Web search focused |
| `codebase-analyzer` | Analyze codebase patterns |
| `codebase-pattern-finder` | Find patterns across codebase |
| `statusline-setup` | Configure shell statusline |
| `worker` | Generic worker (displayed as "Agent") |

## Teammate Mailbox Protocol (Inter-Agent IPC)

Teams (multiple agents in a tmux session) communicate via a filesystem-based mailbox system. This is **not** part of the stream-json wire protocol — it's a sideband IPC mechanism using JSON files on disk.

### Mailbox Location

```
~/.claude/tasks/<team_name>/inboxes/<agent_name>.json
```

Each agent has an inbox file. Messages are JSON arrays with a lock file for concurrency.

### Mailbox Message Types

These are **not** stream-json messages. They are inter-agent protocol messages stored in the mailbox:

| Type | Description | Key Fields |
|------|-------------|------------|
| `idle_notification` | Agent is idle and waiting | `from`, `timestamp`, `idleReason`, `summary`, `completedTaskId?`, `completedStatus?`, `failureReason?` |
| `task_completed` | Task completion notification | `from`, `taskId`, `status` |
| `permission_request` | Cross-agent permission request | `request_id`, `agent_id`, `tool_name`, `tool_input` |
| `permission_response` | Response to permission request | `request_id`, `behavior: "allow"\|"deny"`, `message?` |
| `sandbox_permission_request` | Sandbox permission delegation | `request_id`, `agent_id`, `tool_name` |
| `sandbox_permission_response` | Sandbox permission response | `request_id`, `behavior` |
| `shutdown_request` | Request agent shutdown | `requestId`, `from`, `reason`, `timestamp` |
| `shutdown_approved` | Approve shutdown | `requestId`, `from`, `timestamp` |
| `shutdown_rejected` | Reject shutdown | `requestId`, `from`, `reason`, `timestamp` |
| `plan_approval_request` | Request plan approval from lead | `from`, `timestamp`, `planFilePath`, `planContent`, `requestId` |
| `plan_approval_response` | Lead approves/rejects plan | `requestId`, `approved: bool`, `feedback?`, `from`, `timestamp` |
| `mode_set_request` | Change agent's permission mode | `from`, `mode`, `timestamp` |
| `team_permission_update` | Update team-wide permissions | _varies_ |

### Teammate Message Rendering

When teammates send messages via the mailbox, they appear in the TUI as:

```xml
<teammate_message teammate_id="agent-name" color="blue" summary="Found the bug">
message content here
</teammate_message>
```

## Hooks Protocol

Hooks are user-defined shell commands that run in response to agent events. They produce three `system` subtypes:

```
IN  system.hook_started   {hook_id, hook_name, hook_event}
IN  system.hook_progress  {hook_id, ..., stdout, stderr, output}  // repeated
IN  system.hook_response  {hook_id, ..., exit_code, outcome: "success"|"error"|"cancelled"}
```

Hook events include: `PreToolUse`, `PostToolUse`, `Notification`, `Stop`, `SubagentStop`, `TeammateIdle`, `TaskCompleted`, `Elicitation`, `Compact`.

The `stop_hook_summary` system subtype may also appear after stop hooks run.

## Elicitation Protocol (MCP)

MCP servers can request user input through the elicitation protocol. Two modes:

### Form Elicitation

```
IN   control_request.elicitation  {mode: "form", message, requestedSchema: {type: "object", properties: {...}}}
OUT  control_response.success     {response: {action: "accept"|"decline"|"cancel", content: {...}}}
IN   system.elicitation_complete  {mcp_server_name, elicitation_id}
```

### URL Elicitation

```
IN   control_request.elicitation  {mode: "url", message, elicitationId, url}
OUT  (user completes OAuth/action at URL)
IN   notifications/elicitation/complete  {elicitationId}
```

## Early Message Injection

When a user sends a message while an agent is actively processing a query, the host
uses the `control_request.interrupt` mechanism to abort the current turn and process
the new message immediately, rather than waiting for the full turn to complete.

### Mechanism

The CLI's stdin processing loop runs concurrently with query execution. It handles
`control_request.interrupt` by aborting the current API call and emitting a `result`
message. The session and conversation context are preserved.

### Flow

```
  User sends message while agent is busy
       │
       ▼
  Host queues message in session.message_queue
       │
       ▼
  Host sends control_request.interrupt via client.interrupt()
       │
       ▼
  CLI aborts current API call
       │
       ▼
  CLI emits result (partial turn preserved in context)
       │
       ▼
  Host streaming loop sees result, exits normally
       │
       ▼
  Host drains message queue (process_message_queue)
       │
       ▼
  Queued message sent as next query via client.query()
```

### Wire-level detail

```
# Agent is mid-turn (streaming response)...

# User sends new message → host interrupts:
OUT  control_request  {"request_id": "...", "request": {"subtype": "interrupt"}}
IN   control_response {"response": {"subtype": "success", "request_id": "..."}}
IN   result           {"subtype": "success", ...}   # current turn ends

# Host sends queued message as new query:
OUT  user             {"message": {"role": "user", "content": "..."}}
IN   system.init      # re-emitted
IN   stream_event.*   # new turn begins
...
IN   result           # new turn completes
```

### Key properties

- **Graceful**: Uses `client.interrupt()` (SDK control protocol), not process kill.
  The CLI stays alive with full conversation context.
- **Context preserved**: The interrupted turn's partial output remains in the
  conversation history. The agent can see what it was doing when interrupted.
- **Degradation**: If the interrupt fails or times out (5s), the message stays
  queued and processes after the current turn completes (original behavior).
- **Multiple messages**: Rapid messages all queue up. The first triggers the
  interrupt. After the turn ends, `process_message_queue` drains them sequentially.

### vs interrupt_session (destructive)

`interrupt_session()` kills the CLI process via `transport.stop()`, which injects
an `ExitEvent` and terminates the process. The session is lost and must be
reconstructed. `graceful_interrupt()` only aborts the current API turn — the CLI
process and session continue normally.
