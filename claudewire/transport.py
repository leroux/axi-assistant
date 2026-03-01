"""BridgeTransport -- SDK Transport that routes through procmux.

Implements the claude_agent_sdk Transport interface (6 abstract methods).
For reconnecting agents, intercepts the initialize control_request and
fakes a success response -- the CLI is already initialized.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from procmux import ProcmuxConnection, ResultMsg, StderrMsg, StdoutMsg

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable

    from procmux.client import ProcessMsg

log = logging.getLogger(__name__)


class BridgeTransport:
    """SDK Transport that routes through procmux for one agent.

    Implements the claude_agent_sdk.Transport interface (6 abstract methods).
    For reconnecting agents, intercepts the initialize control_request and
    fakes a success response -- the CLI is already initialized.
    """

    def __init__(
        self,
        name: str,
        conn: ProcmuxConnection,
        *,
        reconnecting: bool = False,
        stderr_callback: Callable[[str], None] | None = None,
        stdio_logger: logging.Logger | None = None,
    ):
        self._name = name
        self._conn = conn
        self._reconnecting = reconnecting
        self._stderr_callback = stderr_callback
        self._stdio_logger = stdio_logger
        self._queue: asyncio.Queue[ProcessMsg] | None = None
        self._ready = False
        self._cli_exited = False
        self._exit_code: int | None = None

    async def connect(self) -> None:
        """Register with procmux for this agent's output.

        SPAWN is handled separately via spawn() before connect().
        SUBSCRIBE is handled separately via subscribe() after connect().
        """
        self._queue = self._conn.register_process(self._name)
        self._ready = True

    async def spawn(self, cli_args: list[str], env: dict[str, str], cwd: str) -> ResultMsg:
        """Tell procmux to spawn a new CLI process for this agent."""
        result = await self._conn.send_command(
            "spawn",
            name=self._name,
            cli_args=cli_args,
            env=env,
            cwd=cwd,
        )
        if not result.ok:
            raise RuntimeError(f"Procmux spawn failed for '{self._name}': {result}")
        return result

    async def subscribe(self) -> ResultMsg:
        """Subscribe to this agent's output from procmux (triggers buffer replay)."""
        result = await self._conn.send_command("subscribe", name=self._name)
        if not result.ok:
            raise RuntimeError(f"Procmux subscribe failed for '{self._name}': {result}")
        return result

    async def write(self, data: str) -> None:
        """Write data to the CLI's stdin via procmux."""
        if not self._conn.is_alive:
            raise ConnectionError("Procmux connection is dead")
        msg = json.loads(data)

        # Intercept initialize for reconnecting agents -- fake success
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
            if self._queue:
                await self._queue.put(StdoutMsg(name=self._name, data=fake_response))
            self._reconnecting = False
            return

        if self._stdio_logger:
            self._stdio_logger.debug(">>> STDIN  %s", json.dumps(msg))
        await self._conn.send_stdin(self._name, msg)

    async def read_messages(self):
        """Async generator yielding parsed JSON dicts from CLI stdout."""
        if not self._queue:
            return
        if not self._conn.is_alive:
            raise ConnectionError("Procmux connection is dead")
        while True:
            msg = await self._queue.get()
            if msg is None:
                raise ConnectionError("Procmux connection lost during read")
            if isinstance(msg, StdoutMsg):
                msg_type = msg.data.get("type", "?")
                log.debug("[read][%s] yielding StdoutMsg type=%s", self._name, msg_type)
                if self._stdio_logger:
                    self._stdio_logger.debug("<<< STDOUT %s", json.dumps(msg.data))
                yield msg.data
            elif isinstance(msg, StderrMsg):
                log.debug("[read][%s] stderr: %.200s", self._name, msg.text)
                if self._stdio_logger:
                    self._stdio_logger.debug("<<< STDERR %s", msg.text)
                if self._stderr_callback:
                    self._stderr_callback(msg.text)
            else:  # ExitMsg
                log.debug("[read][%s] ExitMsg code=%s", self._name, msg.code)
                if self._stdio_logger:
                    self._stdio_logger.debug("--- EXIT   code=%s", msg.code)
                self._cli_exited = True
                self._exit_code = msg.code
                return

    async def close(self) -> None:
        """Kill the CLI process and unregister from procmux."""
        if self._ready and not self._cli_exited:
            try:
                await self._conn.send_command("kill", name=self._name)
            except Exception:
                pass
        self._conn.unregister_process(self._name)
        self._ready = False

    def is_ready(self) -> bool:
        return self._ready

    async def end_input(self) -> None:
        """Not needed for bridge mode -- CLI stdin stays open for multi-turn."""

    @property
    def cli_exited(self) -> bool:
        return self._cli_exited
