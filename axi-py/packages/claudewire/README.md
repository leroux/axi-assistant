# claudewire

Claude CLI stream-json protocol wrapper. Backend-agnostic — no dependency on procmux or any specific process transport.

## Why claudewire exists

Claude Pro/Max subscriptions are licensed exclusively for use through Claude Code. You can't use a subscription API key to call the Anthropic API directly for arbitrary applications — but you *can* build on top of Claude Code itself. claudewire makes this viable by turning the Claude Code CLI into a programmable backend: any application (Discord bot, web UI, Slack integration) can drive Claude Code sessions through the stream-json protocol while staying within subscription terms.

Beyond the licensing angle, the `claude-agent-sdk` provides a `Transport` ABC and a built-in `SubprocessCLITransport`, but it has significant gaps for production use:

- **No process reuse.** The SDK always spawns a new CLI process. There's no way to reconnect to an existing process after a host restart — claudewire's `BridgeTransport` handles reconnection by intercepting the initialize handshake and faking a success response.
- **No pluggable process backends.** The SDK's transport is hardcoded to local subprocesses. claudewire defines a `ProcessConnection` protocol that any backend can implement (local PTY, procmux over Unix socket, SSH, etc.).
- **`rate_limit_event` not parsed.** The SDK's message parser throws `MessageParseError` on `rate_limit_event` messages — it only recognizes `user`, `assistant`, `system`, `result`, and `stream_event`. claudewire parses rate limit events and provides typed `RateLimitInfo` objects.
- **No wire-level validation.** The SDK trusts CLI output completely. claudewire validates every message against pydantic models so protocol changes are detected immediately via warnings in logs.
- **No stderr as a transport concept.** The SDK exposes stderr via a callback on `ClaudeAgentOptions`, but the `Transport` ABC has no stderr support. claudewire's `BridgeTransport` reads stderr as `StderrEvent` alongside stdout, making it a first-class part of the transport (important for the `autocompact:` debug line that provides context token counts).
- **Interactive tools require custom handling.** Claude Code tools like `EnterPlanMode`, `ExitPlanMode`, and `AskUserQuestion` arrive as `control_request.can_use_tool` permission checks — the SDK calls your `can_use_tool` callback with the tool name and input, and you must return allow/deny. But the SDK provides no built-in support for the interactive flow these tools require (posting a plan to the user, waiting for approval, collecting multi-choice answers). If you're building a non-terminal UI (Discord bot, web app, Slack), you need to implement this yourself in the permission callback. See `PROTOCOL.md` for the message flow and the `can_use_tool` section below for composable permission policies.

## Purpose

Wraps the Claude Code CLI's `--output-format stream-json` protocol into a clean `ProcessConnection` abstraction. Any process backend (local PTY, procmux, SSH, etc.) can implement `ProcessConnection` and get a working SDK Transport for free.

Also provides stateless permission policies for restricting tool access, rate limit event parsing from the Claude API stream, and schema validation against the full protocol.

## Architecture

```
Claude Agent SDK
      |
BridgeTransport (SDK Transport impl)
      |
ProcessConnection (abstract protocol)
      |
DirectProcessConnection    -- or --    ProcmuxProcessConnection (via agenthub)
(local PTY subprocess)                 (remote via Unix socket)
```

## Usage

### Direct (local subprocess)

```python
from claudewire import BridgeTransport, DirectProcessConnection

conn = DirectProcessConnection()
transport = BridgeTransport("my-agent", conn)

await transport.connect()
await transport.spawn(cli_args=["--model", "sonnet"], env={}, cwd="/tmp")

await transport.write(json.dumps({"type": "user_message", "content": "hello"}))

async for msg in transport.read_messages():
    print(msg)

await transport.close()
```

### Permission policies

Stateless factory functions that return `CanUseTool` callbacks. Compose them with `compose()` to build a policy chain — first non-None result wins.

```python
from claudewire import cwd_policy, tool_block_policy, tool_allow_policy, compose

# Restrict file writes to specific directories
cwd = cwd_policy(["/home/user/project", "/home/user/data"])

# Block specific tools
block = tool_block_policy({"Skill", "Task"}, message="Not available")

# Auto-allow safe tools
allow = tool_allow_policy({"TodoWrite", "EnterPlanMode"})

# Chain them: block -> allow -> cwd -> default allow
permission_cb = compose(block, allow, cwd)

# Use as ClaudeAgentOptions.can_use_tool
options = ClaudeAgentOptions(can_use_tool=permission_cb, ...)
```

Built-in trivial policies: `allow_all`, `deny_all`.

### Rate limit event parsing

```python
from claudewire import parse_rate_limit_event, RateLimitInfo

# Parse rate_limit_event from the raw stream
info = parse_rate_limit_event(event_data)
if info is not None:
    print(info.rate_limit_type)  # "five_hour"
    print(info.status)           # "allowed", "allowed_warning", "rejected"
    print(info.resets_at)        # datetime
    print(info.utilization)      # 0.0-1.0 or None
```

### Activity tracking

```python
from claudewire import ActivityState, update_activity

activity = ActivityState()
# Feed raw stream events to track what the agent is doing
update_activity(activity, event)
print(activity.phase)  # "thinking", "writing", "tool_use", etc.
```

### CLI argument construction (requires claude-agent-sdk)

```python
from claudewire import build_cli_spawn_args

cli_args, env, cwd = build_cli_spawn_args(agent_options)
```

## API

### Transport & Connection

| Export | Description |
|---|---|
| `BridgeTransport` | SDK `Transport` impl over any `ProcessConnection` |
| `ProcessConnection` | Protocol that process backends must satisfy |
| `DirectProcessConnection` | Local PTY subprocess backend |
| `CommandResult` | Result of spawn/subscribe/kill commands |

### Event Types

| Export | Description |
|---|---|
| `StdoutEvent` | JSON data from process stdout |
| `StderrEvent` | Text line from stderr |
| `ExitEvent` | Process exit with code |
| `ProcessEvent` | Union of above |
| `ProcessEventQueue` | Async queue protocol (get/put) |

### Activity Tracking

| Export | Description |
|---|---|
| `ActivityState` | Tracks phase, tool, thinking text, turn count, etc. |
| `update_activity()` | Parse stream events into `ActivityState` |
| `as_stream()` | Wrap a prompt as `AsyncIterable` for SDK streaming |

### Rate Limit Events

| Export | Description |
|---|---|
| `RateLimitInfo` | Parsed rate limit event (type, status, resets_at, utilization) |
| `parse_rate_limit_event()` | Parse a `rate_limit_event` dict into `RateLimitInfo` |

### Permission Policies

| Export | Description |
|---|---|
| `cwd_policy()` | Restrict file writes to allowed base paths |
| `tool_block_policy()` | Block specific tools by name |
| `tool_allow_policy()` | Auto-allow specific tools by name |
| `compose()` | Chain policies — first non-None result wins, all None defaults to allow |
| `allow_all` | Trivial policy: allow everything |
| `deny_all` | Trivial policy: deny everything |

### Session Lifecycle

| Export | Description |
|---|---|
| `disconnect_client()` | Graceful async client teardown |
| `ensure_process_dead()` | SIGTERM cleanup for leaked processes |
| `get_subprocess_pid()` | Extract PID from SDK client |
| `find_claude()` | Locate `claude` binary on PATH |
| `build_cli_spawn_args()` | Build CLI args from `ClaudeAgentOptions` (lazy import) |

### Schema Validation

| Export | Description |
|---|---|
| `validate_inbound()` | Validate an inbound (CLI → host) message against pydantic models |
| `validate_outbound()` | Validate an outbound (host → CLI) message |
| `validate_inbound_or_bare()` | Validate inbound message, handling both `stream_event`-wrapped and bare forms |
| `ValidationResult` | Result with `.ok`, `.errors`, and typed `.model` |

All stream-json messages are validated against strict pydantic models with discriminated unions. Unknown fields produce warnings (not hard errors) so new upstream fields don't break us — but they do get logged, which is how we detect CLI protocol changes.

**When the Claude CLI adds new message types, content block types, or fields**: validation warnings will appear in logs. Update the models in `schema.py` and add real samples to `tests/unit/test_claudewire_schema_real.py`. See `PROTOCOL.md` for the full protocol reference.

## Dependencies

`pydantic>=2.0` for schema validation. `claude-agent-sdk` is optional (only needed for `build_cli_spawn_args`).

Requires Python 3.12+.
