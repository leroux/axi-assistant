# SDK vs Claudewire — Handling Diff

Comparison of the Claude Code Python SDK (`claude_agent_sdk`) against
`claudewire` + `axi` + `flowcoder_engine` to identify missing handling.

---

## Control Protocol Handling

### 1. `can_use_tool` (Tool Permission Callback) — CRITICAL GAP (flowcoder path)

**SDK path (regular agents):** Works correctly. `ClaudeSDKClient.connect()`
sets `--permission-prompt-tool stdio` on the CLI, so the CLI sends
`can_use_tool` control_requests. The `Query` class intercepts them and calls
the callback.

**Axi regular agents:** Also works — they use `SubprocessCLITransport`
directly (not BridgeTransport). SDK handles everything.

**Axi flowcoder agents: BROKEN.** `build_engine_cli_args()` in
`axi/flowcoder.py` does NOT add `--permission-prompt-tool stdio` to the
passthrough args. Inner Claude never sends `can_use_tool` requests. The
`make_cwd_permission_callback()` is registered in the SDK Query but never
invoked.

Missing from `axi/flowcoder.py:build_engine_cli_args()`:

```python
if options.can_use_tool and not options.permission_prompt_tool_name:
    cmd += ["--permission-prompt-tool", "stdio"]
elif options.permission_prompt_tool_name:
    cmd += ["--permission-prompt-tool", options.permission_prompt_tool_name]
```

The `claudewire/cli.py:build_cli_spawn_args()` already has this injection
logic, but `build_engine_cli_args()` was written separately and missed it.

### 2. `hook_callback` — Not used by axi (no gap)

SDK supports hook callbacks via the `initialize` handshake. Axi doesn't use
SDK hooks (`options.hooks` is not set in `_make_agent_options()`). Unused
capability.

### 3. `mcp_message` (SDK MCP servers) — Not used by axi (no gap)

SDK supports in-process MCP servers via `mcp_servers` with `type: "sdk"`.
Axi only uses external MCP servers (passed as config).

### 4. `control_cancel_request` — Neither implements it

Both SDK and claudewire have this as TODO.

---

## CLI Flags: `build_engine_cli_args()` vs SDK `_build_command()`

Flags present in SDK but missing from flowcoder passthrough:

| Flag                        | Impact                                          |
|-----------------------------|--------------------------------------------------|
| `--permission-prompt-tool`  | **CRITICAL** — breaks can_use_tool callback      |
| `--tools`                   | Can't customize base tool set                    |
| `--allowedTools`            | Can't restrict to specific tools                 |
| `--max-turns`               | No turn limit safety                             |
| `--max-budget-usd`          | No cost limit                                    |
| `--betas`                   | Can't enable beta features                       |
| `--continue`                | Can't continue conversations                     |
| `--add-dir`                 | Can't add extra directories                      |
| `--fork-session`            | Can't fork sessions                              |
| `--plugin-dir`              | Can't load plugins                               |
| `--json-schema`             | No structured output                             |

Most are minor since axi doesn't use them. `--permission-prompt-tool` is the
showstopper.

---

## Message Type Handling

| Message Type      | SDK                                     | Axi `_receive_response_safe`                            | Gap? |
|-------------------|-----------------------------------------|---------------------------------------------------------|------|
| `StreamEvent`     | Parsed                                  | Handled                                                 | No   |
| `AssistantMessage`| Parsed (with error field)               | Handles rate_limit, billing_error, other errors         | No   |
| `ResultMessage`   | Parsed (with cost, usage)               | Handled                                                 | No   |
| `SystemMessage`   | Parsed (subtype + data)                 | Handled (flowchart_start/complete + generic)            | No   |
| `UserMessage`     | Parsed (with uuid, tool_use_result)     | Not explicitly handled in stream                        | Minor — not needed unless using file checkpointing |
| `rate_limit_event`| Not a standard SDK type                 | Special-cased in _receive_response_safe                 | No   |
| Unknown types     | Returns None (forward-compatible)       | Logged + reported to exceptions channel                 | No   |

---

## Error Handling

| Error                          | SDK                                        | Axi / Claudewire                                      |
|--------------------------------|--------------------------------------------|--------------------------------------------------------|
| `ProcessError` (non-zero exit) | Raised from SubprocessCLITransport         | BridgeTransport yields ExitEvent (exit code preserved) |
| `SDKJSONDecodeError` (>1MB)    | Raised when buffer overflows               | N/A — procmux handles complete messages                |
| `CLINotFoundError`             | Raised on missing binary                   | Handled separately by procmux spawn                    |
| `CLIConnectionError`           | Various connection failures                | BridgeTransport raises ConnectionError                 |
| `MessageParseError`            | Raised on invalid messages                 | Caught in _receive_response_safe                       |
| Control request timeout        | 60s default                                | Same (SDK Query handles this)                          |
| AssistantMessage errors        | Typed: auth_failed, billing, rate_limit    | Handled (rate_limit/billing → retry, others → transient) |

---

## Stream Lifecycle

| Aspect                   | SDK                                                  | Claudewire / Bridge                                     |
|--------------------------|------------------------------------------------------|---------------------------------------------------------|
| stdin closure timing     | Waits for first result when hooks/MCP present        | `end_input()` is no-op (stays open) — correct for multi-turn |
| Reconnect                | Not supported natively                               | BridgeTransport fakes initialize response               |
| Graceful shutdown        | close() → cancel tasks → close streams → terminate   | close() → kill process → unregister                     |

---

## Architecture: How Permissions Flow

### Regular agents (SDK SubprocessCLITransport)

```
SDK Query (has can_use_tool callback)
  ↕ control protocol
SubprocessCLITransport
  ↕ stdio
Claude CLI (spawned with --permission-prompt-tool stdio)
```

CLI sends `can_use_tool` → Query calls callback → sends allow/deny back. Works.

### Flowcoder agents (BridgeTransport → engine → inner Claude)

```
SDK Query (has can_use_tool callback)
  ↕ control protocol
BridgeTransport
  ↕ procmux
Flowcoder Engine (transparent proxy)
  ↕ stdio
Inner Claude CLI (spawned WITHOUT --permission-prompt-tool stdio)
```

Inner Claude never sends `can_use_tool` requests because it doesn't have the
flag. The callback is never invoked. The `make_cwd_permission_callback()`
that restricts file writes to allowed directories is completely bypassed.

### Flowcoder agents — how it SHOULD work

```
SDK Query (has can_use_tool callback)
  ↕ control protocol
BridgeTransport
  ↕ procmux
Flowcoder Engine
  ↕ relays control_request/response during proxy turns (_drain_control_responses)
  ↕ relays during flowchart takeover (_handle_control_request → protocol.emit)
Inner Claude CLI (spawned WITH --permission-prompt-tool stdio)
```

The relay path already exists in the engine. Both proxy turns and flowchart
takeover correctly forward control_requests from inner Claude to stdout and
control_responses from stdin back to inner Claude. The ONLY missing piece is
the CLI flag.

---

## Fix

In `axi/flowcoder.py:build_engine_cli_args()`, after the `--permission-mode`
block, add:

```python
# Permission prompt tool (enables can_use_tool callback via control protocol)
if options.can_use_tool and not options.permission_prompt_tool_name:
    cmd += ["--permission-prompt-tool", "stdio"]
elif options.permission_prompt_tool_name:
    cmd += ["--permission-prompt-tool", options.permission_prompt_tool_name]
```

This mirrors the injection in `claudewire/cli.py:build_cli_spawn_args()`.
