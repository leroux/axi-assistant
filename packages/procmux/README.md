# procmux

Dumb process multiplexer. Manages named subprocesses over a Unix socket. Buffers output when the client is disconnected. Zero semantic awareness — no knowledge of Claude, agents, or any higher-level abstraction.

## Architecture

```
Client (asyncio)
    |
Unix socket (JSON lines)
    |
procmux server
    |--- subprocess 1 (stdin/stdout/stderr pipes)
    |--- subprocess 2
    |--- subprocess N
```

Single client at a time. When the client disconnects, output is buffered. On reconnect and subscribe, buffered messages replay synchronously.

## Usage

### Server

```bash
python -m procmux /path/to/socket.sock
```

### Client

```python
from procmux import ProcmuxConnection, ensure_running

# Connect (starts server if needed)
conn = await ensure_running("/path/to/socket.sock")

# Spawn a process
conn.register_process("worker-1")
result = await conn.send_command("spawn", name="worker-1", cli_args=["python", "run.py"])

# Subscribe to output
await conn.send_command("subscribe", name="worker-1")

# Send stdin
await conn.send_stdin("worker-1", {"type": "message", "text": "hello"})

# Read output from the per-process queue
queue = conn._process_queues["worker-1"]
msg = await queue.get()  # StdoutMsg | StderrMsg | ExitMsg | None

# Kill
await conn.send_command("kill", name="worker-1")
conn.unregister_process("worker-1")
```

## Wire Protocol

JSON lines over Unix socket. Client sends `CmdMsg` or `StdinMsg`, server replies with `ResultMsg`, `StdoutMsg`, `StderrMsg`, or `ExitMsg`.

### Client → Server

| Message | Fields | Description |
|---|---|---|
| `CmdMsg` | `cmd`, `name`, `cli_args`, `env`, `cwd` | spawn, kill, interrupt, subscribe, unsubscribe, list, status |
| `StdinMsg` | `name`, `data` (dict) | Forward JSON to process stdin |

### Server → Client

| Message | Fields | Description |
|---|---|---|
| `ResultMsg` | `ok`, `error`, `pid`, `already_running`, `replayed`, `status`, `exit_code`, `idle`, `agents`, `uptime_seconds` | Command response |
| `StdoutMsg` | `name`, `data` (dict) | JSON from process stdout |
| `StderrMsg` | `name`, `text` | Raw stderr line |
| `ExitMsg` | `name`, `code` | Process exited |

## API

### Client

| Export | Description |
|---|---|
| `ProcmuxConnection` | Async Unix socket client with message demux |
| `connect()` | Connect to existing server (returns `None` if unavailable) |
| `ensure_running()` | Connect or start server, with retry and timeout |
| `start()` | Start server as subprocess |

### Server

| Export | Description |
|---|---|
| `ProcmuxServer` | Server that manages subprocesses |
| `ManagedProcess` | Dataclass tracking a subprocess (status, buffer, idle state) |

### Protocol

`CmdMsg`, `StdinMsg`, `ResultMsg`, `StdoutMsg`, `StderrMsg`, `ExitMsg`

## Features

- Per-process output buffering during client disconnection
- Per-process stdio logging (rotating, 5 MB, 2 backups)
- Idle detection via stdin/stdout timestamp comparison
- Graceful shutdown on SIGTERM/SIGINT (terminates all subprocesses)
- Process group isolation (new sessions for signal handling)
- 10 MB socket buffer limit

## Dependencies

- `pydantic>=2.0`

Requires Python 3.12+.
