# Flowcoder Engine Protocol

Flowcoder engine implements a **superset** of the Claude Code stream-json protocol. Any client that speaks the Claude Code protocol (`--input-format stream-json --output-format stream-json`) can use flowcoder-engine as a drop-in replacement. All standard Claude Code messages pass through unchanged.

This document describes the **additional message types** that flowcoder introduces on top of the base protocol.

## Overview

Flowcoder engine operates in two modes:

- **Proxy mode** (default) — all messages pass through transparently to/from the inner Claude process. The client sees exactly the same messages it would from `claude -p`.
- **Takeover mode** — when a user message contains a slash command matching a known flowchart (e.g. `/story "dragons"`), the engine takes over. It executes the flowchart, emitting structured events about execution progress, then returns to proxy mode.

In proxy mode, no new message types are introduced. The protocol extensions only appear during takeover mode.

## Transport

Same as Claude Code:

- **Input** (stdin): JSON lines, one message per line
- **Output** (stdout): JSON lines, one message per line
- **Logs** (stderr): Human-readable log lines prefixed with `[flowcoder]`

## Base Protocol (unchanged)

These Claude Code message types pass through unmodified in both modes:

| Direction | Type | Description |
|-----------|------|-------------|
| stdin | `user` | User message |
| stdin | `control_response` | Response to a permission request |
| stdin | `shutdown` | Graceful shutdown |
| stdout | `system` (subtype `init`) | Session initialization |
| stdout | `assistant` | Claude's response |
| stdout | `stream_event` | Streaming token events |
| stdout | `control_request` | Permission request from Claude |
| stdout | `rate_limit_event` | Rate limit status |
| stdout | `result` | Turn completion |

## Flowcoder Extensions

All flowcoder-specific messages use `type: "system"` with new subtypes. This means clients that ignore unknown system subtypes will work without modification.

### flowchart_start

Emitted when the engine enters takeover mode to execute a flowchart.

```json
{
  "type": "system",
  "subtype": "flowchart_start",
  "data": {
    "command": "story",
    "args": "dragons",
    "block_count": 5
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `command` | string | The slash command name (without `/`) |
| `args` | string | Raw argument string passed to the command |
| `block_count` | integer | Total number of blocks in the flowchart |

### block_start

Emitted when the walker begins executing a block.

```json
{
  "type": "system",
  "subtype": "block_start",
  "data": {
    "block_id": "draft",
    "block_name": "Write Draft",
    "block_type": "prompt"
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `block_id` | string | Unique identifier for the block in the flowchart |
| `block_name` | string | Human-readable block name |
| `block_type` | string | Block type: `start`, `end`, `prompt`, `branch`, `bash`, `variable`, `refresh` |

### block_complete

Emitted when a block finishes executing.

```json
{
  "type": "system",
  "subtype": "block_complete",
  "data": {
    "block_id": "draft",
    "block_name": "Write Draft",
    "success": true
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `block_id` | string | Block identifier (matches the preceding `block_start`) |
| `block_name` | string | Human-readable block name |
| `success` | boolean | Whether the block completed successfully |

### flowchart_complete

Emitted when the engine exits takeover mode, regardless of success or failure.

```json
{
  "type": "system",
  "subtype": "flowchart_complete",
  "data": {
    "status": "completed",
    "duration_ms": 41677,
    "cost_usd": 0.193,
    "blocks_executed": 5
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | `"completed"` or `"error"` |
| `duration_ms` | integer | Total wall-clock time for the flowchart |
| `cost_usd` | float | Estimated API cost for the flowchart run |
| `blocks_executed` | integer | Number of blocks that were executed |

### Inner Claude messages during takeover

During flowchart execution, `assistant` and `stream_event` messages from the inner Claude are forwarded **unwrapped** — they appear on stdout with the exact same format as in proxy mode. The client can distinguish which mode produced them by tracking `flowchart_start`/`flowchart_complete` boundaries.

`rate_limit_event` messages from the inner Claude are suppressed during takeover to reduce noise. The aggregated cost is reported in `flowchart_complete`.

`control_request` messages are forwarded to the client in both modes. The client must respond with `control_response` as usual.

## Message Sequence

### Proxy mode (normal turn)

```
client  →  {"type": "user", "message": {"role": "user", "content": "hello"}}
engine  →  {"type": "system", "subtype": "init", ...}      (first turn only)
engine  →  {"type": "assistant", ...}
engine  →  {"type": "result", ...}
```

### Takeover mode (flowchart turn)

```
client  →  {"type": "user", "message": {"role": "user", "content": "/story dragons"}}
engine  →  {"type": "system", "subtype": "flowchart_start", "data": {...}}
engine  →  {"type": "system", "subtype": "block_start", "data": {"block_id": "start", ...}}
engine  →  {"type": "system", "subtype": "block_complete", "data": {"block_id": "start", ...}}
engine  →  {"type": "system", "subtype": "block_start", "data": {"block_id": "draft", ...}}
engine  →  {"type": "assistant", ...}                       (inner Claude response, unwrapped)
engine  →  {"type": "system", "subtype": "block_complete", "data": {"block_id": "draft", ...}}
  ... (more blocks) ...
engine  →  {"type": "system", "subtype": "flowchart_complete", "data": {...}}
engine  →  {"type": "result", ...}
```

Every flowchart turn ends with a `result` message, just like a proxy turn. The `result` contains the final flowchart variables as a JSON string.

## Backward Compatibility

- Flowcoder's extensions use `type: "system"` with new subtypes. Clients that don't recognize unknown subtypes can safely ignore them.
- The `result` message at the end of a flowchart turn has the same structure as Claude Code's `result`, so existing result-handling logic works unchanged.
- In proxy mode, the protocol is byte-identical to Claude Code. No messages are added, modified, or removed.
