# Agent Persistence — Single Bridge (Approach B)

## Overview

A single `agent_bridge.py` process manages ALL Claude CLI subprocesses. It sits between bot.py and the CLI processes, multiplexing messages over one Unix domain socket. The bridge is intentionally dumb — it's just a message router, not a protocol participant.

```
bot.py ←── single Unix socket ──→ agent_bridge.py ──stdio──→ CLI 1
                                                    ──stdio──→ CLI 2
                                                    ──stdio──→ CLI 3
```

The bridge is a **tagged message relay**:
- Bot.py sends `{"type": "stdin", "name": "X", "data": {...}}`
- Bridge strips envelope, writes raw JSON to that agent's CLI stdin
- CLI writes JSON to stdout, bridge tags it as `{"type": "stdout", "name": "X", "data": {...}}`
- When bot.py disconnects: buffer output per-agent
- When bot.py reconnects: replay buffers after subscribe

**The bridge doesn't know anything about Claude's protocol.** ~150-250 lines, almost never changes.

---

## Validated Design Decisions (from prototyping)

### 1. SDK supports custom Transport natively

```python
client = ClaudeSDKClient(options=options, transport=my_bridge_transport)
```

`ClaudeSDKClient.connect()` uses our transport directly — no bypass needed. Creates Query on top of it, calls `initialize()`, everything works.

**Validated**: Prototype confirmed `__aenter__()` calls our `connect()`, initialize control_request flows through our transport, `query()` writes to our transport.

### 2. CLI args building is safe and side-effect-free

```python
temp = SubprocessCLITransport(prompt="", options=options)
cmd = temp._build_command()  # Pure function, zero side effects
```

- No temp files created (MCP config is inline JSON in `--mcp-config`)
- No subprocess spawned
- Sandbox settings go via `--settings` JSON flag
- `_find_cli()` runs in `__init__` but only reads filesystem to locate binary

### 3. Initialize interception for reconnecting agents

BridgeTransport intercepts the `initialize` control_request and fakes a success response locally:

```python
async def write(self, data: str):
    msg = json.loads(data)
    if (self._reconnecting
            and msg.get("type") == "control_request"
            and msg.get("request", {}).get("subtype") == "initialize"):
        fake_response = {
            "type": "control_response",
            "response": {"subtype": "success", "request_id": msg["request_id"], "response": {}}
        }
        await self._queue.put({"type": "stdout", "name": self._name, "data": fake_response})
        self._reconnecting = False
        return
    await self._conn.send_stdin(self._name, msg)
```

ClaudeSDKClient sees no difference — `connect()` works identically for new and reconnecting agents.

### 4. Bridge socket path

```python
BRIDGE_SOCKET_PATH = os.path.join(BOT_DIR, ".bridge.sock")
```

Per-instance (Axi Prime and Nova get separate sockets), deterministic, no conflicts.

### 5. Bridge protocol

Newline-delimited JSON. Every message has a `type` field.

**Bot.py → Bridge:**
```json
{"type": "cmd", "cmd": "spawn", "name": "X", "cli_args": [...], "env": {...}, "cwd": "/path"}
{"type": "cmd", "cmd": "kill", "name": "X"}
{"type": "cmd", "cmd": "interrupt", "name": "X"}
{"type": "cmd", "cmd": "subscribe", "name": "X"}
{"type": "cmd", "cmd": "list"}
{"type": "stdin", "name": "X", "data": {...}}
```

**Bridge → Bot.py:**
```json
{"type": "result", "ok": true, ...}
{"type": "stdout", "name": "X", "data": {...}}
{"type": "stderr", "name": "X", "text": "..."}
{"type": "exit", "name": "X", "code": 0}
```

Commands are serialized (bot.py acquires lock, sends, waits for `result`). Messages (stdin/stdout/stderr/exit) are concurrent.

### 6. Bridge lifecycle

**Start**: Bot.py starts bridge with `subprocess.Popen(["python3", "agent_bridge.py", socket_path], start_new_session=True)`. Bridge creates socket, starts listening.

**Discovery on reconnect**: Bot.py tries to connect to known socket path. If succeeds → bridge is alive. If fails → start new bridge.

**Single client**: Bridge accepts only ONE bot.py connection. New connection replaces old (bot.py crashed and restarted).

**Survive restart**: `start_new_session=True` puts bridge in its own process group. When bot.py exits, bridge stays alive (supervisor keeps the cgroup alive).

### 7. Graceful shutdown — no need to wait for agents!

With the bridge, agents keep running across restarts. Graceful shutdown becomes instant:
1. `_exit_for_restart()` → `os._exit(42)` — no SIGTERM to supervisor
2. Socket closes automatically → bridge detects disconnect → starts buffering
3. Supervisor relaunches bot.py → reconnects to bridge → agents resume

The wait-for-busy-agents logic is unnecessary for graceful restarts. CLIs keep running, tool calls continue (or get buffered `can_use_tool` requests answered on reconnect).

**Force restart** still kills everything: `kill_supervisor()` → SIGTERM supervisor → cgroup dies → bridge dies.

### 8. Reconnect flow on startup

```
on_ready():
  1. Reconstruct sleeping agents from Discord channels (existing)
  2. Connect to bridge (start if needed)
  3. LIST → get running agents + status + buffer sizes
  4. For each running agent matching a reconstructed AgentSession:
     a. Set session._reconnecting = True (blocks on_message from waking)
     b. Schedule _reconnect_and_drain(session, ...) task
  5. _reconnect_and_drain task:
     a. Acquires session.query_lock
     b. Creates BridgeTransport(reconnecting=True) + ClaudeSDKClient
     c. connect() → fakes initialize, subscribes to bridge output
     d. Sets session.client
     e. If has buffered output: send "*(reconnected after restart)*", stream_response_to_channel()
     f. Clears session._reconnecting
     g. Releases query_lock
     h. Processes message queue
```

The `_reconnecting` flag prevents on_message from trying to wake the agent (creating a new CLI) during the tiny window before the drain task acquires the lock.

### 9. Stderr handling

Bridge sends stderr as `{"type": "stderr", "name": "X", "text": "..."}`. BridgeTransport handles it in `_read_impl()` by calling the stderr callback (same mechanism as today):

```python
async def _read_impl(self):
    while True:
        msg = await self._queue.get()
        if msg is None: return  # close sentinel
        if msg["type"] == "stdout": yield msg["data"]
        elif msg["type"] == "stderr":
            if self._stderr_callback: self._stderr_callback(msg["text"])
        elif msg["type"] == "exit": return  # CLI exited
```

### 10. BridgeConnection demux architecture

```python
class BridgeConnection:
    _reader / _writer    → raw socket streams
    _agent_queues        → dict[str, asyncio.Queue]  per-agent routing
    _cmd_response        → asyncio.Queue             for command results
    _cmd_lock            → asyncio.Lock              serialize commands
    _demux_task          → asyncio.Task              background reader

    _demux_loop():
        for each line from socket:
            if type == "result": put in _cmd_response
            if type in (stdout, stderr, exit): put in _agent_queues[name]

    send_command(cmd, **kwargs) → dict:
        async with _cmd_lock:
            write command, await _cmd_response.get()

    send_stdin(name, data):
        write {"type": "stdin", "name": name, "data": data}

    register_agent(name) → Queue:
        create queue, store in _agent_queues

    unregister_agent(name):
        remove from _agent_queues
```

### 11. ClaudeAgentOptions for reconnecting agents

For reconnecting agents, we DON'T need full options (model, thinking, betas are for CLI args which we skip). Minimal options:

```python
reconnect_options = ClaudeAgentOptions(
    can_use_tool=make_cwd_permission_callback(session.cwd),
    mcp_servers=session.mcp_servers or {},
    permission_mode="default",
    cwd=session.cwd,
)
```

### 12. sleep_agent() with bridge

```python
async def sleep_agent(session):
    if session.client is None: return
    await _disconnect_client(session.client, session.name)  # calls transport.close()
    session.client = None
```

BridgeTransport.close() sends KILL to bridge (stops the CLI), unregisters from demux. `_get_subprocess_pid()` returns None (no local process), `_ensure_process_dead()` is a no-op.

### 13. Agent exit detection

When CLI exits, bridge sends `{"type": "exit", "name": "X", "code": N}`. BridgeTransport._read_impl() sees this and returns (generator stops). Query reader sees transport stream end, puts "end" in message stream. stream_response_to_channel() finishes.

BridgeTransport tracks `_cli_exited = True`. After drain, bot.py can check this and sleep the agent (CLI is dead, no point keeping the transport open).

---

## Edge Cases

1. **Bot.py crashes mid-stream**: Bridge detects socket close → starts buffering. Supervisor relaunches bot.py. Bot.py reconnects, replays buffer. Discord gets "*(reconnected)*" then continuation.

2. **CLI finishes while bot.py is down**: Bridge buffers full response. On reconnect, replayed via stream_response_to_channel(). Agent goes idle after drain.

3. **`can_use_tool` buffered during restart gap**: Replayed to bot.py on reconnect. Query handles it normally. If >60s gap, CLI times out tool call — Claude retries. Bash is unaffected (sandbox auto-approves).

4. **SPAWN for agent bridge already has**: Bridge returns existing PID + status. Bot.py treats it as reconnect (skip initialize).

5. **Bridge socket doesn't exist on startup**: Bot.py starts new bridge, waits for socket to appear, connects.

6. **Bridge dies while bot.py is running**: BridgeConnection._demux_loop detects socket close. All agent queues get None sentinel. All transports close. Agents lose clients. On next message, bot.py starts a new bridge and re-wakes the agent (fresh CLI).

7. **New bot.py connection replaces old**: Bridge accepts new connection, drops old. Old buffers are preserved (they're per-agent, not per-connection).

8. **Agent spawned just before crash (no prompt sent yet)**: CLI is running but idle. On reconnect, agent is awake with no buffer. Normal operation.

9. **Idle agent with pending can_use_tool**: Bridge buffered the control_request. On reconnect, Query reader handles it automatically. CLI unblocks, produces output. The _reconnect_and_drain task catches this via stream_response_to_channel().

---

## Implementation Roadmap

### Step 1: agent_bridge.py (~200 lines)
- asyncio-based process: listen on Unix socket, manage CLI subprocesses
- Commands: spawn, kill, interrupt, subscribe, list
- Per-agent stdout/stderr relay with tagging
- Per-agent buffering when no client connected
- Single-client model (new connection replaces old)
- Graceful shutdown on SIGTERM

### Step 2: BridgeConnection + BridgeTransport (~200 lines, new file bridge_transport.py)
- `BridgeConnection`: socket management, demux loop, command request/response
- `BridgeTransport(Transport)`: per-agent transport implementing SDK's 6 abstract methods
- Initialize interception for reconnecting agents
- Stderr callback routing
- CLI exit detection
- Bridge startup/discovery logic

### Step 3: Bot.py integration (~200 lines changed)
- `wake_agent()`: use BridgeTransport + ClaudeSDKClient instead of bare ClaudeSDKClient
- `sleep_agent()`: unchanged (transport.close() sends KILL to bridge)
- `on_ready()`: connect to bridge, LIST, reconnect running agents, drain buffers
- Add `_reconnecting` flag to AgentSession
- Add `_reconnect_and_drain()` task

### Step 4: Shutdown changes (~20 lines)
- New `_exit_for_restart()` in shutdown.py
- ShutdownCoordinator uses it for graceful restarts (skip sleep_all, skip wait-for-busy)
- `kill_supervisor()` unchanged for force restarts

### Step 5: Testing
- Start nova service, verify agents auto-reconnect after restart
- Verify agent_states are preserved through graceful restart
- Verify buffer replay works (start long task, restart mid-stream)
- Verify can_use_tool works after reconnect
- Verify force restart kills everything properly
