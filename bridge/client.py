"""Bridge client — imported by bot.py to communicate with the bridge server."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from .protocol import (
    CmdMsg,
    ExitMsg,
    ResultMsg,
    StderrMsg,
    StdinMsg,
    StdoutMsg,
    parse_server_msg,
)

# Agent message types that flow through queues (None = sentinel for connection loss)
AgentMsg = StdoutMsg | StderrMsg | ExitMsg | None

log = logging.getLogger(__name__)


class BridgeConnection:
    """Manages the Unix socket connection to the bridge from bot.py.

    Runs a demux loop that routes incoming messages to per-agent queues
    and command responses to a dedicated queue.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        self._agent_queues: dict[str, asyncio.Queue[AgentMsg]] = {}
        self._cmd_response: asyncio.Queue[ResultMsg] = asyncio.Queue()
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
                log.debug("[demux] raw line (%d bytes): %.200s", len(line), line.decode(errors="replace").rstrip())
                try:
                    msg: ResultMsg | StdoutMsg | StderrMsg | ExitMsg = parse_server_msg(line)
                except Exception:
                    log.debug("[demux] parse FAILED on: %.200s", line.decode(errors="replace").rstrip())
                    continue

                if isinstance(msg, ResultMsg):
                    log.debug("[demux] routed ResultMsg (ok=%s)", msg.ok)
                    await self._cmd_response.put(msg)
                else:  # StdoutMsg | StderrMsg | ExitMsg
                    if msg.name in self._agent_queues:
                        log.debug("[demux] routed %s -> agent queue '%s'", type(msg).__name__, msg.name)
                        await self._agent_queues[msg.name].put(msg)
                    else:
                        log.debug("[demux] dropped %s for unregistered agent '%s'", type(msg).__name__, msg.name)
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

    async def send_command(self, cmd: str, **kwargs: Any) -> ResultMsg:
        """Send a command to the bridge and wait for the result."""
        async with self._cmd_lock:
            msg = CmdMsg(cmd=cmd, **kwargs)
            self._writer.write(msg.model_dump_json().encode() + b"\n")
            await self._writer.drain()
            return await asyncio.wait_for(self._cmd_response.get(), timeout=30.0)

    async def send_stdin(self, name: str, data: dict[str, Any]):
        """Send data to a CLI's stdin via the bridge."""
        msg = StdinMsg(name=name, data=data)
        self._writer.write(msg.model_dump_json().encode() + b"\n")
        await self._writer.drain()

    def register_agent(self, name: str) -> asyncio.Queue[AgentMsg]:
        """Register an agent and return its message queue."""
        q: asyncio.Queue[AgentMsg] = asyncio.Queue()
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
            await self._demux_task
        except (asyncio.CancelledError, Exception):
            pass
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
        self._queue: asyncio.Queue[AgentMsg] | None = None
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

    async def spawn(self, cli_args: list[str], env: dict[str, str], cwd: str) -> ResultMsg:
        """Tell the bridge to spawn a new CLI process for this agent."""
        result = await self._conn.send_command(
            "spawn",
            name=self._name,
            cli_args=cli_args,
            env=env,
            cwd=cwd,
        )
        if not result.ok:
            raise RuntimeError(f"Bridge spawn failed for '{self._name}': {result}")
        return result

    async def subscribe(self) -> ResultMsg:
        """Subscribe to this agent's output from the bridge (triggers buffer replay)."""
        result = await self._conn.send_command("subscribe", name=self._name)
        if not result.ok:
            raise RuntimeError(f"Bridge subscribe failed for '{self._name}': {result}")
        return result

    async def write(self, data: str) -> None:
        """Write data to the CLI's stdin via the bridge."""
        if not self._conn.is_alive:
            raise ConnectionError("Bridge connection is dead")
        msg = json.loads(data)

        # Intercept initialize for reconnecting agents — fake success
        if (
            self._reconnecting
            and msg.get("type") == "control_request"
            and msg.get("request", {}).get("subtype") == "initialize"
        ):
            request_id = msg.get("request_id")
            fake_response: dict[str, Any] = {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": request_id,
                    "response": {},
                },
            }
            # Inject into our read queue as if it came from the bridge
            if self._queue:
                await self._queue.put(StdoutMsg(name=self._name, data=fake_response))
            self._reconnecting = False
            return

        await self._conn.send_stdin(self._name, msg)

    async def read_messages(self):
        """Async generator yielding parsed JSON dicts from CLI stdout."""
        if not self._queue:
            return
        if not self._conn.is_alive:
            raise ConnectionError("Bridge connection is dead")
        while True:
            msg = await self._queue.get()
            if msg is None:
                raise ConnectionError("Bridge connection lost during read")
            if isinstance(msg, StdoutMsg):
                msg_type = msg.data.get("type", "?")
                log.debug("[read][%s] yielding StdoutMsg type=%s", self._name, msg_type)
                yield msg.data
            elif isinstance(msg, StderrMsg):
                log.debug("[read][%s] stderr: %.200s", self._name, msg.text)
                if self._stderr_callback:
                    self._stderr_callback(msg.text)
            else:  # ExitMsg
                log.debug("[read][%s] ExitMsg code=%s", self._name, msg.code)
                self._cli_exited = True
                self._exit_code = msg.code
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

    @property
    def cli_exited(self) -> bool:
        return self._cli_exited
