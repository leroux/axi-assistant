# Claude CLI Stream-JSON Protocol

Reference for the `--output-format stream-json` wire protocol between the Claude CLI process and its host (claudewire/SDK). Documented from real bridge-stdio logs (23,000+ messages analyzed).

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

```python
re.compile(r"autocompact: tokens=(\d+) threshold=\d+ effectiveWindow=(\d+)")
```

All other stderr content (Python warnings, wrapper process lifecycle messages) is noise and not parsed.

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
| `init` | Session start/re-init | `cwd`, `session_id`, `tools[]`, `mcp_servers[]`, `model`, `permissionMode`, `slash_commands[]`, `apiKeySource`, `claude_code_version`, `output_style`, `agents[]`, `skills[]`, `plugins[]`, `fast_mode_state`, `uuid` |
| `status` | Status change | `status` (string\|null), `session_id`, `uuid` |
| `compact_boundary` | Context compaction marker | `session_id`, `uuid`, `compact_metadata: {trigger, pre_tokens}` |

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

**Delta** (discriminated on `type`):
- `text_delta`: `{type, text}`
- `input_json_delta`: `{type, partial_json}`
- `thinking_delta`: `{type, thinking}`
- `signature_delta`: `{type, signature}`

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

Query completion.

```json
{
  "type": "result",
  "subtype": "success" | "error",
  "is_error": false,
  "duration_ms": 57095,
  "duration_api_ms": 31197,
  "num_turns": 2,
  "session_id": "string",
  "uuid": "string",
  "result": "string|null",
  "stop_reason": any,
  "total_cost_usd": 0.707,
  "usage": Usage,
  "modelUsage": { "model-name": ModelUsage },
  "permission_denials": [],
  "fast_mode_state": "off"
}
```

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
| `initialize` | Session init handshake | _(minimal)_ |
| `interrupt` | Interrupt signal | _(minimal)_ |

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
    "resetsAt": 1772766000,          // optional
    "rateLimitType": "five_hour",    // optional
    "utilization": 0.9,              // optional
    "isUsingOverage": false,         // optional
    "surpassedThreshold": 0.9,       // optional
    "overageStatus": "string",       // optional
    "overageDisabledReason": "string" // optional
  },
  "uuid": "string",
  "session_id": "string"
}
```

**Rate limit windows are reported separately.** Each event carries a single `rateLimitType` — either `"five_hour"` or `"seven_day"` (or `null`). They are not combined into one event. A given API call may emit one event for `five_hour`, one for `seven_day`, both, or neither. To track utilization across both windows, keep the most recent event per `rateLimitType`.
```

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

The SDK's `parse_message()` only recognizes the wrapped `stream_event` form. Bare events hit the unknown-type path and raise `MessageParseError`. In claudewire, `validate_inbound_or_bare()` handles both forms, but the bare duplicates are redundant in normal operation — they carry the same data as the wrapped version.

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
| All inbound message types (`stream_event`, `assistant`, `user`, `system`, `result`, `control_request`, `control_response`, `rate_limit_event`) | Claude CLI stdout |
| Inner stream events (`message_start`, `message_delta`, `message_stop`, `content_block_*`) | Anthropic Messages API streaming, wrapped by CLI |
| Content blocks (`text`, `tool_use`, `thinking`) and deltas | Anthropic Messages API |
| `system.init` fields (`tools`, `model`, `mcp_servers`, `permissionMode`, `slash_commands`, `agents`, `skills`, `plugins`, `fast_mode_state`, etc.) | Claude CLI session state |
| `system.status` and `system.compact_boundary` | Claude CLI context management |
| `control_request.can_use_tool` (permission check) | Claude CLI permission system |
| `control_request.mcp_message` (MCP relay) | Claude CLI MCP integration |
| `control_request.initialize` / `control_request.interrupt` | Claude CLI session control |
| `result` message (query completion with costs, usage, modelUsage) | Claude CLI |
| `rate_limit_event` (status, resetsAt, utilization, overage fields) | Claude CLI / Anthropic rate limiting |
| `assistant` message (complete assembled message after streaming) | Claude CLI |
| `user` message echoed back with `tool_use_result` and `isSynthetic` | Claude CLI |
| Dual emission (each stream event emitted twice: wrapped + bare) | Claude CLI behavior |
| MCP handshake sequence (initialize → notifications/initialized → tools/list) | Claude CLI, following MCP spec |
| `updatedInput` in permission responses (tool input modification) | claude-agent-sdk contract |

### Our Additions (claudewire/axi)

These are behaviors we add on top of the upstream protocol. They are not part of the CLI
wire format.

| What | Where | Description |
|------|-------|-------------|
| `_trace_context` field on outbound messages | `transport.py` | OTel distributed tracing injection. Injected by `BridgeTransport.write()` before sending to CLI stdin. Stripped by `schema.py` before validation. Not part of the protocol — just piggybacks on the JSON payload. |
| Reconnect initialize interception | `transport.py` | When `reconnecting=True`, `BridgeTransport.write()` intercepts outbound `control_request.initialize` and synthesizes a fake `control_response.success` locally instead of forwarding to the CLI. The CLI is already initialized — this satisfies the SDK's handshake without confusing the running process. |
| Schema validation (warnings for unknown keys) | `schema.py` | Pydantic validation runs on every inbound and outbound message. Unknown keys produce warnings (not errors) so new upstream fields don't break us. This is purely our layer — the CLI has no awareness of it. |
| `validate_inbound_or_bare()` | `schema.py` | Handles the CLI's dual emission by accepting both `stream_event`-wrapped and bare stream events. The CLI emits both forms on stdout — the SDK's `parse_message()` only handles wrapped, so bare events need separate handling. |
| Bridge-stdio logging | `transport.py` | Optional `stdio_logger` logs all stdin/stdout traffic for debugging. The `bridge-stdio-*.log` files that informed this document are produced by this logger. |
| procmux buffer replay | `procmux/` | When the bot reconnects after a restart, procmux replays buffered stdout messages. These replayed messages are identical to the originals — procmux adds nothing to the payload. |

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
│  BridgeTransport: SDK transport interface             │
│  Adds: _trace_context injection, reconnect intercept  │
│  Adds: schema validation, bridge-stdio logging        │
│  Handles: bare event validation (CLI dual emission)    │
└──────────────────────────────────────────────────────┘
```
