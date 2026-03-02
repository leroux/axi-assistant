# agenthub

Agent lifecycle management — wires procmux to claudewire. Provides multi-agent orchestration primitives.

## Purpose

Glue layer between `procmux` (process multiplexing) and `claudewire` (Claude protocol). The main adapter is `ProcmuxProcessConnection`, which lets claudewire's `BridgeTransport` route through a procmux server without either package knowing about the other.

Also provides shared primitives for multi-agent systems: background task management, frontend callback protocols, and concurrency limits.

## Architecture

```
Claude Agent SDK
      |
BridgeTransport (claudewire)
      |
ProcmuxProcessConnection (agenthub) ← adapter
      |
ProcmuxConnection (procmux)
      |
Unix socket → procmux server → subprocesses
```

## Usage

```python
from procmux import ensure_running
from claudewire import BridgeTransport
from agenthub import ProcmuxProcessConnection

# Connect to procmux
conn = await ensure_running("/path/to/socket.sock")

# Wrap as claudewire ProcessConnection
proc_conn = ProcmuxProcessConnection(conn)

# Use with BridgeTransport
transport = BridgeTransport("agent-1", proc_conn)
await transport.connect()
await transport.spawn(cli_args=["--model", "sonnet"], env={}, cwd="/tmp")
```

## API

| Export | Description |
|---|---|
| `ProcmuxProcessConnection` | Adapter: wraps `ProcmuxConnection` to satisfy claudewire's `ProcessConnection` protocol |
| `BackgroundTaskSet` | GC-safe fire-and-forget async task set (prevents Python 3.12+ weak-ref collection) |
| `FrontendCallbacks` | Callback protocol for frontends (post_message, post_system, on_wake, on_sleep, on_session_id, get_channel) |
| `ConcurrencyLimitError` | Raised when awake-agent slots are exhausted |

### BackgroundTaskSet

```python
from agenthub import BackgroundTaskSet

tasks = BackgroundTaskSet()
tasks.fire_and_forget(some_coroutine())
print(len(tasks))  # number of active tasks
```

### FrontendCallbacks

```python
from agenthub import FrontendCallbacks

callbacks = FrontendCallbacks(
    post_message=my_post_fn,     # async (agent_name, text) -> None
    post_system=my_system_fn,    # async (agent_name, text) -> None
    on_wake=my_wake_fn,          # async (agent_name) -> None
    on_sleep=my_sleep_fn,        # async (agent_name) -> None
    on_session_id=my_sid_fn,     # async (agent_name, session_id) -> None
    get_channel=my_channel_fn,   # async (agent_name) -> channel
)
```

## Dependencies

- `claudewire`
- `procmux`

Requires Python 3.12+.
