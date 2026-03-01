"""Procmux client — connects to the procmux server over a Unix socket.

Runs a demux loop that routes incoming messages to per-process queues
and command responses to a dedicated queue.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .protocol import (
    CmdMsg,
    ExitMsg,
    ResultMsg,
    StderrMsg,
    StdinMsg,
    StdoutMsg,
    parse_server_msg,
)

# Message types that flow through per-process queues (None = connection lost sentinel)
ProcessMsg = StdoutMsg | StderrMsg | ExitMsg | None

log = logging.getLogger(__name__)


class ProcmuxConnection:
    """Manages the Unix socket connection to the procmux server.

    Runs a demux loop that routes incoming messages to per-process queues
    and command responses to a dedicated queue.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        self._process_queues: dict[str, asyncio.Queue[ProcessMsg]] = {}
        self._cmd_response: asyncio.Queue[ResultMsg] = asyncio.Queue()
        self._cmd_lock = asyncio.Lock()
        self._demux_task = asyncio.create_task(self._demux_loop())
        self._closed = False

    async def _demux_loop(self):
        """Read from socket, route to per-process queues or command response queue."""
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break
                log.debug("[demux] raw line (%d bytes): %.200s", len(line), line.decode(errors="replace").rstrip())
                try:
                    msg: ResultMsg | StdoutMsg | StderrMsg | ExitMsg = parse_server_msg(line)
                except Exception:
                    log.debug("[demux] parse FAILED on: %.200s", line.decode(errors="replace").rstrip())
                    continue

                if isinstance(msg, ResultMsg):
                    log.debug("[demux] routed ResultMsg (ok=%s)", msg.ok)
                    await self._cmd_response.put(msg)
                else:
                    if msg.name in self._process_queues:
                        log.debug("[demux] routed %s -> queue '%s'", type(msg).__name__, msg.name)
                        await self._process_queues[msg.name].put(msg)
                    else:
                        log.warning("[demux] dropped %s for unregistered process '%s'", type(msg).__name__, msg.name)
        except (ConnectionError, OSError):
            log.info("Procmux connection lost")
        except Exception:
            log.exception("Error in procmux demux loop")
        finally:
            self._closed = True
            for q in self._process_queues.values():
                try:
                    q.put_nowait(None)
                except asyncio.QueueFull:
                    pass
            log.info("Procmux demux loop ended")

    @property
    def is_alive(self) -> bool:
        return not self._closed

    async def send_command(self, cmd: str, **kwargs: Any) -> ResultMsg:
        """Send a command to procmux and wait for the result."""
        async with self._cmd_lock:
            msg = CmdMsg(cmd=cmd, **kwargs)
            self._writer.write(msg.model_dump_json().encode() + b"\n")
            await self._writer.drain()
            return await asyncio.wait_for(self._cmd_response.get(), timeout=30.0)

    async def send_stdin(self, name: str, data: dict[str, Any]):
        """Send data to a process's stdin via procmux."""
        msg = StdinMsg(name=name, data=data)
        self._writer.write(msg.model_dump_json().encode() + b"\n")
        await self._writer.drain()

    def register_process(self, name: str) -> asyncio.Queue[ProcessMsg]:
        """Register a process and return its message queue."""
        q: asyncio.Queue[ProcessMsg] = asyncio.Queue()
        self._process_queues[name] = q
        return q

    def unregister_process(self, name: str):
        """Unregister a process's message queue."""
        self._process_queues.pop(name, None)

    async def close(self):
        """Close the connection to procmux."""
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
