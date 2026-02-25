"""Agent Bridge — persistent relay between bot.py and Claude CLI subprocesses.

This module contains both the bridge server (runs as a separate process) and the
client-side transport (imported by bot.py). The bridge is intentionally dumb — it
routes JSON lines between a single bot.py connection and N CLI subprocesses, buffering
output when bot.py is disconnected.

Server usage (separate process):
    python bridge.py /path/to/.bridge.sock

Client usage (from bot.py):
    from bridge import BridgeConnection, BridgeTransport, ensure_bridge

Architecture:
    bot.py ←── Unix socket ──→ bridge.py ──stdio──→ CLI 1
                                           ──stdio──→ CLI 2
                                           ──stdio──→ CLI 3
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------

# Bot.py → Bridge message types
TYPE_CMD = "cmd"       # command: spawn, kill, interrupt, subscribe, list
TYPE_STDIN = "stdin"   # forward data to CLI stdin

# Bridge → Bot.py message types
TYPE_RESULT = "result"   # response to a command
TYPE_STDOUT = "stdout"   # CLI stdout (parsed JSON)
TYPE_STDERR = "stderr"   # CLI stderr (raw text)
TYPE_EXIT = "exit"       # CLI process exited


# ---------------------------------------------------------------------------
# Server side — runs as a separate process
# ---------------------------------------------------------------------------

@dataclass
class CliProcess:
    """A CLI subprocess managed by the bridge."""
    name: str
    proc: asyncio.subprocess.Process
    status: str = "running"        # "running" | "exited"
    exit_code: int | None = None
    buffer: list[dict] = field(default_factory=list)
    subscribed: bool = False       # whether bot.py is receiving output
    stdout_task: asyncio.Task | None = None
    stderr_task: asyncio.Task | None = None


class BridgeServer:
    """The bridge relay process. Manages CLI subprocesses and relays to bot.py."""

    def __init__(self, socket_path: str):
        self._socket_path = socket_path
        self._cli_procs: dict[str, CliProcess] = {}
        self._client_writer: asyncio.StreamWriter | None = None
        self._client_lock = asyncio.Lock()   # protects _client_writer
        self._server: asyncio.Server | None = None
        self._shutdown_event = asyncio.Event()

    async def start(self):
        """Start listening on the Unix socket."""
        # Clean up stale socket
        if os.path.exists(self._socket_path):
            os.unlink(self._socket_path)

        self._server = await asyncio.start_unix_server(
            self._handle_client, path=self._socket_path,
        )
        log.info("Bridge listening on %s", self._socket_path)

        # Install signal handlers
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: self._shutdown_event.set())

        await self._shutdown_event.wait()
        await self._shutdown()

    async def _shutdown(self):
        """Gracefully shut down: kill all CLIs, close socket."""
        log.info("Bridge shutting down, killing %d CLI(s)", len(self._cli_procs))
        for cp in list(self._cli_procs.values()):
            await self._kill_cli(cp)
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if os.path.exists(self._socket_path):
            os.unlink(self._socket_path)
        log.info("Bridge shutdown complete")

    # -- Client connection handling --

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle a bot.py connection. Only one client at a time."""
        async with self._client_lock:
            old_writer = self._client_writer
            self._client_writer = writer
            # Unsubscribe all agents (new client must re-subscribe)
            for cp in self._cli_procs.values():
                cp.subscribed = False

        if old_writer is not None:
            log.warning("New client connected — dropping previous connection")
            try:
                old_writer.close()
                await old_writer.wait_closed()
            except Exception:
                pass

        log.info("Client connected")

        try:
            while True:
                line = await reader.readline()
                if not line:
                    break  # Client disconnected
                try:
                    msg = json.loads(line.decode().strip())
                except json.JSONDecodeError:
                    log.warning("Invalid JSON from client: %r", line[:200])
                    continue

                msg_type = msg.get("type")
                if msg_type == TYPE_CMD:
                    await self._handle_command(msg)
                elif msg_type == TYPE_STDIN:
                    await self._handle_stdin(msg)
                else:
                    log.warning("Unknown message type from client: %s", msg_type)
        except (ConnectionError, OSError) as e:
            log.info("Client disconnected: %s", e)
        except Exception:
            log.exception("Error handling client")
        finally:
            async with self._client_lock:
                if self._client_writer is writer:
                    self._client_writer = None
                    for cp in self._cli_procs.values():
                        cp.subscribed = False
            log.info("Client disconnected — buffering all output")

    # -- Command handling --

    async def _handle_command(self, msg: dict):
        cmd = msg.get("cmd", "")
        name = msg.get("name", "")

        if cmd == "spawn":
            await self._cmd_spawn(msg)
        elif cmd == "kill":
            await self._cmd_kill(name)
        elif cmd == "interrupt":
            await self._cmd_interrupt(name)
        elif cmd == "subscribe":
            await self._cmd_subscribe(name)
        elif cmd == "list":
            await self._cmd_list()
        else:
            await self._send_result({"ok": False, "error": f"unknown command: {cmd}"})

    async def _cmd_spawn(self, msg: dict):
        name = msg.get("name", "")
        cli_args = msg.get("cli_args", [])
        env = msg.get("env", {})
        cwd = msg.get("cwd")

        if name in self._cli_procs and self._cli_procs[name].status == "running":
            cp = self._cli_procs[name]
            await self._send_result({
                "ok": True, "name": name, "pid": cp.proc.pid,
                "already_running": True,
            })
            return

        # Clean up dead entry if exists
        self._cli_procs.pop(name, None)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cli_args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
        except Exception as e:
            await self._send_result({"ok": False, "name": name, "error": str(e)})
            return

        cp = CliProcess(name=name, proc=proc)
        cp.stdout_task = asyncio.create_task(self._relay_stdout(cp))
        cp.stderr_task = asyncio.create_task(self._relay_stderr(cp))
        self._cli_procs[name] = cp

        log.info("Spawned CLI '%s' (pid=%d)", name, proc.pid)
        await self._send_result({"ok": True, "name": name, "pid": proc.pid})

    async def _cmd_kill(self, name: str):
        cp = self._cli_procs.get(name)
        if cp is None:
            await self._send_result({"ok": False, "name": name, "error": "not found"})
            return
        await self._kill_cli(cp)
        self._cli_procs.pop(name, None)
        await self._send_result({"ok": True, "name": name})

    async def _cmd_interrupt(self, name: str):
        cp = self._cli_procs.get(name)
        if cp is None or cp.status != "running":
            await self._send_result({"ok": False, "name": name, "error": "not running"})
            return
        try:
            cp.proc.send_signal(signal.SIGINT)
            await self._send_result({"ok": True, "name": name})
        except Exception as e:
            await self._send_result({"ok": False, "name": name, "error": str(e)})

    async def _cmd_subscribe(self, name: str):
        cp = self._cli_procs.get(name)
        if cp is None:
            await self._send_result({"ok": False, "name": name, "error": "not found"})
            return

        cp.subscribed = True
        buffered_count = len(cp.buffer)

        # Replay buffer
        for msg in cp.buffer:
            await self._send_to_client(msg)
        cp.buffer.clear()

        await self._send_result({
            "ok": True, "name": name, "replayed": buffered_count,
            "status": cp.status, "exit_code": cp.exit_code,
        })

    async def _cmd_list(self):
        agents = {}
        for name, cp in self._cli_procs.items():
            agents[name] = {
                "pid": cp.proc.pid,
                "status": cp.status,
                "exit_code": cp.exit_code,
                "buffered_msgs": len(cp.buffer),
                "subscribed": cp.subscribed,
            }
        await self._send_result({"ok": True, "agents": agents})

    # -- stdin forwarding --

    async def _handle_stdin(self, msg: dict):
        name = msg.get("name", "")
        data = msg.get("data")
        cp = self._cli_procs.get(name)
        if cp is None or cp.status != "running" or cp.proc.stdin is None:
            return
        try:
            line = json.dumps(data) + "\n"
            cp.proc.stdin.write(line.encode())
            await cp.proc.stdin.drain()
        except (ConnectionError, OSError):
            log.warning("Failed to write to CLI '%s' stdin", name)

    # -- stdout/stderr relay --

    async def _relay_stdout(self, cp: CliProcess):
        """Read JSON lines from CLI stdout, relay or buffer."""
        try:
            while True:
                line = await cp.proc.stdout.readline()
                if not line:
                    break  # EOF
                try:
                    data = json.loads(line.decode().strip())
                except json.JSONDecodeError:
                    # Non-JSON output — treat as stderr
                    text = line.decode().strip()
                    if text:
                        msg = {"type": TYPE_STDERR, "name": cp.name, "text": text}
                        if cp.subscribed:
                            await self._send_to_client(msg)
                        else:
                            cp.buffer.append(msg)
                    continue

                msg = {"type": TYPE_STDOUT, "name": cp.name, "data": data}
                if cp.subscribed:
                    await self._send_to_client(msg)
                else:
                    cp.buffer.append(msg)
        except Exception:
            log.exception("Error relaying stdout for '%s'", cp.name)
        finally:
            # Wait for process to exit
            try:
                await cp.proc.wait()
            except Exception:
                pass
            cp.status = "exited"
            cp.exit_code = cp.proc.returncode
            exit_msg = {"type": TYPE_EXIT, "name": cp.name, "code": cp.exit_code}
            if cp.subscribed:
                await self._send_to_client(exit_msg)
            else:
                cp.buffer.append(exit_msg)
            log.info("CLI '%s' exited (code=%s)", cp.name, cp.exit_code)

    async def _relay_stderr(self, cp: CliProcess):
        """Read lines from CLI stderr, relay or buffer."""
        try:
            while True:
                line = await cp.proc.stderr.readline()
                if not line:
                    break  # EOF
                text = line.decode().strip()
                if not text:
                    continue
                msg = {"type": TYPE_STDERR, "name": cp.name, "text": text}
                if cp.subscribed:
                    await self._send_to_client(msg)
                else:
                    cp.buffer.append(msg)
        except Exception:
            log.exception("Error relaying stderr for '%s'", cp.name)

    # -- Helpers --

    async def _kill_cli(self, cp: CliProcess):
        """Terminate a CLI process."""
        if cp.status != "running":
            return
        try:
            cp.proc.terminate()
            try:
                await asyncio.wait_for(cp.proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                cp.proc.kill()
                await cp.proc.wait()
        except ProcessLookupError:
            pass  # Already dead
        except Exception:
            log.exception("Error killing CLI '%s'", cp.name)
        cp.status = "exited"
        cp.exit_code = cp.proc.returncode
        if cp.stdout_task:
            cp.stdout_task.cancel()
        if cp.stderr_task:
            cp.stderr_task.cancel()

    async def _send_to_client(self, msg: dict) -> bool:
        """Send a message to the connected bot.py client. Returns False if no client."""
        writer = self._client_writer
        if writer is None:
            return False
        try:
            writer.write((json.dumps(msg) + "\n").encode())
            await writer.drain()
            return True
        except (ConnectionError, BrokenPipeError, OSError):
            # Client disconnected mid-write — mark as gone
            async with self._client_lock:
                if self._client_writer is writer:
                    self._client_writer = None
                    for cp in self._cli_procs.values():
                        cp.subscribed = False
            return False

    async def _send_result(self, result: dict):
        """Send a command result to the client."""
        result["type"] = TYPE_RESULT
        await self._send_to_client(result)


# ---------------------------------------------------------------------------
# Client side — imported by bot.py
# ---------------------------------------------------------------------------

class BridgeConnection:
    """Manages the Unix socket connection to the bridge from bot.py.

    Runs a demux loop that routes incoming messages to per-agent queues
    and command responses to a dedicated queue.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        self._agent_queues: dict[str, asyncio.Queue] = {}
        self._cmd_response: asyncio.Queue = asyncio.Queue()
        self._cmd_lock = asyncio.Lock()
        self._demux_task = asyncio.create_task(self._demux_loop())
        self._closed = False

    async def _demux_loop(self):
        """Read from socket, route to per-agent queues or command response queue."""
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break  # Socket closed
                try:
                    msg = json.loads(line.decode().strip())
                except json.JSONDecodeError:
                    continue

                msg_type = msg.get("type")
                if msg_type == TYPE_RESULT:
                    await self._cmd_response.put(msg)
                elif msg_type in (TYPE_STDOUT, TYPE_STDERR, TYPE_EXIT):
                    name = msg.get("name")
                    if name and name in self._agent_queues:
                        await self._agent_queues[name].put(msg)
                    # Messages for unregistered agents are dropped (bridge still buffers them)
        except (ConnectionError, OSError):
            log.info("Bridge connection lost")
        except Exception:
            log.exception("Error in bridge demux loop")
        finally:
            self._closed = True
            # Signal all agent queues that connection is dead
            for q in self._agent_queues.values():
                try:
                    q.put_nowait(None)  # Sentinel
                except asyncio.QueueFull:
                    pass
            log.info("Bridge demux loop ended")

    @property
    def is_alive(self) -> bool:
        return not self._closed

    async def send_command(self, cmd: str, **kwargs) -> dict:
        """Send a command to the bridge and wait for the result."""
        async with self._cmd_lock:
            msg = {"type": TYPE_CMD, "cmd": cmd, **kwargs}
            self._writer.write((json.dumps(msg) + "\n").encode())
            await self._writer.drain()
            result = await asyncio.wait_for(self._cmd_response.get(), timeout=30.0)
            return result

    async def send_stdin(self, name: str, data: dict):
        """Send data to a CLI's stdin via the bridge."""
        msg = {"type": TYPE_STDIN, "name": name, "data": data}
        self._writer.write((json.dumps(msg) + "\n").encode())
        await self._writer.drain()

    def register_agent(self, name: str) -> asyncio.Queue:
        """Register an agent and return its message queue."""
        q = asyncio.Queue()
        self._agent_queues[name] = q
        return q

    def unregister_agent(self, name: str):
        """Unregister an agent's message queue."""
        self._agent_queues.pop(name, None)

    async def close(self):
        """Close the connection to the bridge."""
        self._closed = True
        self._demux_task.cancel()
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except Exception:
            pass


class BridgeTransport:
    """SDK Transport that routes through the bridge for one agent.

    Implements the claude_agent_sdk.Transport interface (6 abstract methods).
    For reconnecting agents, intercepts the initialize control_request and
    fakes a success response — the CLI is already initialized.
    """

    def __init__(
        self,
        name: str,
        conn: BridgeConnection,
        *,
        reconnecting: bool = False,
        stderr_callback: Callable[[str], None] | None = None,
    ):
        self._name = name
        self._conn = conn
        self._reconnecting = reconnecting
        self._stderr_callback = stderr_callback
        self._queue: asyncio.Queue | None = None
        self._ready = False
        self._cli_exited = False
        self._exit_code: int | None = None

    async def connect(self) -> None:
        """Connect to the bridge for this agent.

        For new agents: sends SPAWN command (caller must provide cli_args via spawn()).
        For reconnecting agents: sends SUBSCRIBE to start receiving output.
        """
        self._queue = self._conn.register_agent(self._name)
        self._ready = True
        # SPAWN is handled separately via spawn() before connect()
        # SUBSCRIBE is handled separately via subscribe() after connect()

    async def spawn(self, cli_args: list[str], env: dict[str, str], cwd: str) -> dict:
        """Tell the bridge to spawn a new CLI process for this agent."""
        result = await self._conn.send_command(
            "spawn", name=self._name, cli_args=cli_args, env=env, cwd=cwd,
        )
        if not result.get("ok"):
            raise RuntimeError(f"Bridge spawn failed for '{self._name}': {result}")
        return result

    async def subscribe(self) -> dict:
        """Subscribe to this agent's output from the bridge (triggers buffer replay)."""
        result = await self._conn.send_command("subscribe", name=self._name)
        if not result.get("ok"):
            raise RuntimeError(f"Bridge subscribe failed for '{self._name}': {result}")
        return result

    async def write(self, data: str) -> None:
        """Write data to the CLI's stdin via the bridge."""
        msg = json.loads(data)

        # Intercept initialize for reconnecting agents — fake success
        if (self._reconnecting
                and msg.get("type") == "control_request"
                and msg.get("request", {}).get("subtype") == "initialize"):
            request_id = msg.get("request_id")
            fake_response = {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": request_id,
                    "response": {},
                },
            }
            # Inject into our read queue as if it came from the bridge
            if self._queue:
                await self._queue.put({
                    "type": TYPE_STDOUT,
                    "name": self._name,
                    "data": fake_response,
                })
            self._reconnecting = False
            return

        await self._conn.send_stdin(self._name, msg)

    def read_messages(self):
        """Async generator yielding parsed JSON dicts from CLI stdout."""
        return self._read_impl()

    async def _read_impl(self):
        if not self._queue:
            return
        while True:
            msg = await self._queue.get()
            if msg is None:
                # Sentinel — connection lost
                return
            msg_type = msg.get("type")
            if msg_type == TYPE_STDOUT:
                yield msg["data"]
            elif msg_type == TYPE_STDERR:
                if self._stderr_callback:
                    self._stderr_callback(msg.get("text", ""))
            elif msg_type == TYPE_EXIT:
                self._cli_exited = True
                self._exit_code = msg.get("code")
                return

    async def close(self) -> None:
        """Kill the CLI process and unregister from the bridge."""
        if self._ready and not self._cli_exited:
            try:
                await self._conn.send_command("kill", name=self._name)
            except Exception:
                pass
        self._conn.unregister_agent(self._name)
        self._ready = False

    def is_ready(self) -> bool:
        return self._ready

    async def end_input(self) -> None:
        """Not needed for bridge mode — CLI stdin stays open for multi-turn."""
        pass

    @property
    def cli_exited(self) -> bool:
        return self._cli_exited


# ---------------------------------------------------------------------------
# Bridge lifecycle helpers (used by bot.py)
# ---------------------------------------------------------------------------

async def connect_to_bridge(socket_path: str) -> BridgeConnection | None:
    """Try to connect to an existing bridge. Returns None if bridge isn't running."""
    try:
        reader, writer = await asyncio.open_unix_connection(socket_path)
        conn = BridgeConnection(reader, writer)
        log.info("Connected to bridge at %s", socket_path)
        return conn
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        return None


async def start_bridge(socket_path: str) -> asyncio.subprocess.Process:
    """Start the bridge as a subprocess in its own process group."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, __file__, socket_path,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    log.info("Started bridge process (pid=%d)", proc.pid)
    return proc


async def ensure_bridge(socket_path: str, timeout: float = 10.0) -> BridgeConnection:
    """Ensure the bridge is running and return a connection to it.

    Tries to connect first. If that fails, starts the bridge and waits for it.
    """
    # Try existing bridge
    conn = await connect_to_bridge(socket_path)
    if conn is not None:
        return conn

    # Start new bridge
    bridge_proc = await start_bridge(socket_path)

    # Wait for socket to appear
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        conn = await connect_to_bridge(socket_path)
        if conn is not None:
            return conn
        # Check if bridge died
        if bridge_proc.returncode is not None:
            stderr = ""
            if bridge_proc.stderr:
                stderr = (await bridge_proc.stderr.read()).decode()[:500]
            raise RuntimeError(
                f"Bridge process died (exit code {bridge_proc.returncode}): {stderr}"
            )

    raise RuntimeError(f"Timed out waiting for bridge at {socket_path}")


def build_cli_spawn_args(options) -> tuple[list[str], dict[str, str], str]:
    """Build CLI command, env, and cwd from ClaudeAgentOptions.

    Uses SubprocessCLITransport._build_command() to stay in sync with
    the SDK's command building logic.
    """
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
    from claude_agent_sdk._version import __version__ as sdk_version

    # Create temp transport just for _build_command() — no side effects
    temp = SubprocessCLITransport(prompt="", options=options)
    cmd = temp._build_command()

    # Replicate env construction from SubprocessCLITransport.connect()
    env = {
        **os.environ,
        **(options.env or {}),
        "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
        "CLAUDE_AGENT_SDK_VERSION": sdk_version,
    }
    if getattr(options, "enable_file_checkpointing", False):
        env["CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING"] = "true"
    cwd_path = str(options.cwd or os.getcwd())
    if cwd_path:
        env["PWD"] = cwd_path

    return cmd, env, cwd_path


# ---------------------------------------------------------------------------
# Entry point — run as bridge server process
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <socket_path>", file=sys.stderr)
        sys.exit(1)

    socket_path = sys.argv[1]

    # Set up logging for the bridge process
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s [bridge] %(message)s",
        stream=sys.stderr,
    )

    server = BridgeServer(socket_path)
    asyncio.run(server.start())


if __name__ == "__main__":
    main()
