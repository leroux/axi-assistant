# Agent Bridge Architecture

The agent bridge is a persistent Unix-socket relay process that decouples bot.py's lifecycle from Claude CLI subprocesses. CLI agents keep running inside the bridge while bot.py restarts, and when bot.py reconnects it replays buffered output and resumes where it left off.

## System Overview

```
systemd (axi-bot.service)
  └─ supervisor.py          (restart loop, crash recovery)
       └─ bot.py             (Discord bot, agent orchestration)
            └─ bridge/       (persistent relay, own process group)
                 ├─ claude CLI (agent-1)
                 ├─ claude CLI (agent-2)
                 └─ ...
```

The bridge runs in its own process group (`start_new_session=True`), so it is **not killed** when supervisor.py or bot.py exit. CLI subprocesses are children of the bridge process, not bot.py.

## Files

| File | Role |
|---|---|
| `bridge/protocol.py` | Wire protocol constants (`TYPE_CMD`, `TYPE_STDIN`, etc.) |
| `bridge/server.py` | Server (`BridgeServer`, `CliProcess`, `main()`) |
| `bridge/client.py` | Client (`BridgeConnection`, `BridgeTransport`) |
| `bridge/helpers.py` | Lifecycle helpers (`ensure_bridge`, `start_bridge`, `connect_to_bridge`, `build_cli_spawn_args`) |
| `bridge/__init__.py` | Re-exports for backward-compatible `from bridge import ...` |
| `bridge/__main__.py` | Entry point: `python -m bridge <socket_path>` |
| `bot.py` | Bridge consumer — `_wake_agent_via_bridge`, `_connect_bridge`, `_reconnect_and_drain` |
| `shutdown.py` | Bridge-aware shutdown — skips agent sleep in bridge mode |
| `supervisor.py` | Process supervisor — no bridge awareness, handles exit code 42 as restart |
| `bridge/test_bridge.py` | Tests using real Unix sockets |

## Wire Protocol

All messages are **newline-delimited JSON** over a Unix domain socket.

### Message Types

| Constant | Value | Direction | Purpose |
|---|---|---|---|
| `TYPE_CMD` | `"cmd"` | bot → bridge | Commands: spawn, kill, interrupt, subscribe, list |
| `TYPE_STDIN` | `"stdin"` | bot → bridge | Forward data to a CLI's stdin |
| `TYPE_RESULT` | `"result"` | bridge → bot | Response to any command |
| `TYPE_STDOUT` | `"stdout"` | bridge → bot | Parsed JSON line from CLI stdout |
| `TYPE_STDERR` | `"stderr"` | bridge → bot | Raw text line from CLI stderr |
| `TYPE_EXIT` | `"exit"` | bridge → bot | CLI process exited |

### Command Messages

**spawn** — Start a CLI subprocess:
```json
{"type": "cmd", "cmd": "spawn", "name": "agent-1",
 "cli_args": ["claude", "--model", "opus", ...],
 "env": {"PATH": "...", "CLAUDE_CODE_ENTRYPOINT": "sdk-py", ...},
 "cwd": "/home/ubuntu/axi-assistant"}
```
Response: `{"type": "result", "ok": true, "name": "agent-1", "pid": 12345}`
Or if already running: `{"type": "result", "ok": true, "already_running": true, "pid": 12345}`

**subscribe** — Start receiving live output for an agent:
```json
{"type": "cmd", "cmd": "subscribe", "name": "agent-1"}
```
Response: `{"type": "result", "ok": true, "replayed": 42, "status": "running", "exit_code": null}`
Replays all buffered messages before returning the result.

**kill** — Terminate a CLI subprocess:
```json
{"type": "cmd", "cmd": "kill", "name": "agent-1"}
```

**interrupt** — Send SIGINT to a CLI subprocess:
```json
{"type": "cmd", "cmd": "interrupt", "name": "agent-1"}
```

**list** — List all managed CLI subprocesses:
```json
{"type": "cmd", "cmd": "list"}
```
Response: `{"type": "result", "ok": true, "agents": {"agent-1": {"pid": 12345, "status": "running", "exit_code": null, "buffered_msgs": 0, "subscribed": true}}}`

### Data Messages

**stdin** — Forward input to a CLI:
```json
{"type": "stdin", "name": "agent-1", "data": {"jsonrpc": "2.0", ...}}
```

**stdout** — CLI output (parsed JSON):
```json
{"type": "stdout", "name": "agent-1", "data": {"type": "assistant", ...}}
```

**stderr** — CLI stderr (raw text):
```json
{"type": "stderr", "name": "agent-1", "text": "warning: something"}
```

**exit** — CLI process exited:
```json
{"type": "exit", "name": "agent-1", "code": 0}
```

## Server: `BridgeServer`

### State

| Field | Description |
|---|---|
| `_cli_procs: dict[str, CliProcess]` | Registry of managed CLI subprocesses, keyed by agent name |
| `_client_writer` | The single connected bot.py client (or `None`) |
| `_shutdown_event` | Set by SIGTERM/SIGINT to trigger shutdown |

Each `CliProcess` tracks:

| Field | Description |
|---|---|
| `proc` | `asyncio.subprocess.Process` handle |
| `status` | `"running"` or `"exited"` |
| `exit_code` | Set after exit |
| `buffer` | Accumulated output when no client is subscribed |
| `subscribed` | Whether bot.py is currently receiving live output |
| `stdout_task` / `stderr_task` | Background tasks reading process pipes |

### Single-Client Model

The bridge enforces exactly **one connected bot.py client** at a time. A new connection immediately closes the old one and sets all agents' `subscribed = False`, routing output to buffers until each is re-subscribed.

### Output Routing

Two background tasks per CLI (`_relay_stdout`, `_relay_stderr`) continuously read from the subprocess pipes. For each message:

- If `subscribed == True`: send to client immediately
- If `subscribed == False`: append to `buffer`

The exit message (process terminated) follows the same routing.

### Buffering

When bot.py disconnects:
1. `_client_writer` is set to `None`, all `subscribed` flags cleared
2. All subsequent output goes to per-agent `buffer` lists
3. There is **no size limit** on buffers

On reconnect, `subscribe` replays the entire buffer in order, then clears it.

### Shutdown

`_kill_cli()` sends SIGTERM, waits up to 5 seconds, then escalates to SIGKILL. On bridge shutdown (SIGTERM to bridge process), all CLI processes are killed.

## Client: `BridgeConnection`

The client-side object that bot.py holds. Manages a single Unix socket connection and **demultiplexes** incoming messages to per-agent queues.

### Demultiplexing

A background `_demux_loop` task reads from the socket:
- `TYPE_RESULT` → `_cmd_response` queue (consumed by `send_command`)
- `TYPE_STDOUT` / `TYPE_STDERR` / `TYPE_EXIT` → per-agent queue (looked up by `name`)
- Unregistered agent names → silently dropped (bridge still buffers server-side)

On disconnect, a `None` sentinel is sent to every registered queue to signal consumers.

### Command Serialization

`send_command()` acquires `_cmd_lock` before sending and awaits the response, ensuring only one command is in-flight at a time. Stdin writes (`send_stdin`) bypass this lock.

## Transport: `BridgeTransport`

Implements the claude_agent_sdk `Transport` interface for a single agent. Wraps a `BridgeConnection` and provides per-agent lifecycle management.

### Key Methods

| Method | What it does |
|---|---|
| `connect()` | Registers the agent queue with `BridgeConnection` |
| `spawn(cli_args, env, cwd)` | Sends spawn command to bridge |
| `subscribe()` | Sends subscribe command, triggers buffer replay |
| `write(data)` | Forwards to CLI stdin (with initialize interception) |
| `read_messages()` | Async generator yielding stdout messages from queue |
| `close()` | Sends kill command, unregisters queue |

### Initialize Interception

When reconnecting to an already-running CLI, the SDK always sends an `initialize` control_request during `__aenter__`. But the CLI is already past initialization. `BridgeTransport` handles this:

1. Created with `reconnecting=True`
2. On `write()`: if the message is a `control_request` with `subtype == "initialize"`, intercept it
3. Construct a fake `control_response` with `subtype="success"` and the matching `request_id`
4. Inject it directly into the agent's queue (as if it came from the bridge)
5. Clear `_reconnecting` flag
6. The SDK's state machine advances normally without confusing the running CLI

### Permission Prompt Tool Injection

In direct mode, `ClaudeSDKClient.connect()` sets `permission_prompt_tool_name="stdio"` on the options before creating `SubprocessCLITransport`. This tells the CLI to send permission requests via the control protocol.

In bridge mode, the CLI is spawned **before** `connect()` runs. So `build_cli_spawn_args()` replicates this logic: if `can_use_tool` is set and `permission_prompt_tool_name` is not already set, it injects `"stdio"` before building the CLI command. Without this, MCP tool permission requests bypass the control protocol.

## Lifecycle

### Phase 1: Initial Startup

```
on_ready
  → reconstruct_agents_from_channels()     # rebuild sleeping AgentSession objects
  → _connect_bridge()
      → ensure_bridge(socket_path)
          → connect_to_bridge() → None     # no bridge yet
          → start_bridge()                 # launch python -m bridge with start_new_session=True
          → poll until connected
      → send_command("list") → {}          # no agents yet
  → _init_shutdown_coordinator(bridge_mode=True)
```

### Phase 2: Agent Wake (Bridge Mode)

```
wake_agent(session)
  → _wake_agent_via_bridge(session, options)
      → BridgeTransport(name, bridge_conn)
      → transport.connect()                  # register agent queue
      → build_cli_spawn_args(options)        # inject --permission-prompt-tool stdio
      → transport.spawn(cli_args, env, cwd)  # bridge spawns CLI subprocess
      → transport.subscribe()                # subscribe for live output
      → ClaudeSDKClient(options, transport)
      → client.__aenter__()                  # SDK sends initialize → forwarded to CLI
  → session.client = client
```

### Phase 3: bot.py Restart (Bridge Mode)

```
graceful_shutdown("restart")
  → bridge_mode=True: skip agent wait, skip sleep_all
  → bot.close()
  → exit_for_restart() → os._exit(42)

# Bridge process unaffected (own process group)
# CLI subprocesses keep running, output buffered

supervisor.py sees exit code 42
  → "Restart requested, relaunching..."
  → run_bot()
```

### Phase 4: Reconnection

```
on_ready (new bot.py instance)
  → reconstruct_agents_from_channels()
  → _connect_bridge()
      → ensure_bridge() → connects to existing bridge (no new spawn)
      → send_command("list") → {"agent-1": {status: "running", buffered_msgs: 42}}
      → for each known agent:
          session._reconnecting = True
          asyncio.create_task(_reconnect_and_drain(session))

_reconnect_and_drain(session)
  → acquire query_lock
  → BridgeTransport(name, bridge_conn, reconnecting=True)
  → transport.connect()                     # register queue
  → transport.subscribe()                   # replay buffered messages into queue
  → ClaudeSDKClient(options, transport)
  → client.__aenter__()
      → SDK sends initialize control_request
      → BridgeTransport.write() intercepts it (reconnecting=True)
      → injects fake success response into queue
      → SDK state machine proceeds
  → session.client = client
  → session._reconnecting = False
  → stream_response_to_channel()            # drain buffered output to Discord
  → release query_lock
  → _process_message_queue()                # process messages that arrived during reconnect
```

### Phase 5: Shutdown (Full Stop)

```
systemctl stop axi-bot
  → SIGTERM to supervisor + bot.py (cgroup)
  → supervisor: _signal_handler sets _stopping, forwards SIGTERM to bot.py
  → bot.py: shutdown handler runs
      → sleep_all() → transport.close() → kill each agent in bridge
      → bot.close()
      → kill_supervisor() or os._exit(42)
  → supervisor exits cleanly (exit 0)
  → bridge may still be running (own process group)
  → systemd cleans up remaining processes in cgroup
```

## Shutdown: Bridge vs Direct Mode

| Behavior | Bridge Mode | Direct Mode |
|---|---|---|
| Wait for busy agents | Skipped | Waits with timeout |
| sleep_all() | Skipped (agents survive) | Called (graceful disconnect) |
| kill_fn | `exit_for_restart` → `os._exit(42)` | `kill_supervisor` → SIGTERM to parent |
| Agent CLI processes | Keep running in bridge | Killed with bot.py |

## Reconnect Guard

While `session._reconnecting == True`, incoming Discord messages are queued in `session.message_queue` instead of waking the agent. After reconnect completes, `_process_message_queue()` drains and processes them.

## Error Handling

- **Bridge connection failure**: Falls back to direct subprocess mode (no bridge)
- **Spawn failure**: `RuntimeError` propagated to caller
- **Resume failure**: Retries once without resume ID (loses conversation context but avoids stuck agent)
- **Client disconnect during send**: `_send_to_client` catches `ConnectionError`/`BrokenPipeError`, clears `_client_writer`, sets all `subscribed = False`
- **CLI won't die**: `_kill_cli` sends SIGTERM, waits 5s, escalates to SIGKILL
- **Orphaned bridge agents**: On reconnect, agents in bridge with no matching session are killed

## Key Design Decisions

**Process group isolation**: `start_new_session=True` is the foundation. The bridge survives supervisor and bot.py restarts because it's in a separate process group that doesn't receive their signals.

**Single-client enforcement**: Only one bot.py can be connected at a time. A new connection immediately replaces the old one. This prevents split-brain scenarios.

**Subscription model**: Output doesn't stream to the client until `subscribe` is called. This gives the client a window to register its queue before any output arrives, preventing message loss.

**Initialize interception**: Rather than modifying the SDK, the transport layer fakes the initialize handshake for reconnecting agents. The SDK thinks it's talking to a fresh CLI.

**Unbounded buffers**: The buffer grows without limit. For the expected use case (restarts take seconds), this is fine. A very long bot.py outage with verbose agents could use significant memory in the bridge process.

**Command serialization**: One command in-flight at a time prevents response cross-talk. Stdin writes are fire-and-forget and bypass the lock.
