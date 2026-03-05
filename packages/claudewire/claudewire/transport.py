"""BridgeTransport -- SDK Transport that routes through a ProcessConnection.

Implements the claude_agent_sdk Transport interface (6 abstract methods).
For reconnecting agents, intercepts the initialize control_request and
fakes a success response -- the CLI is already initialized.

This module has NO dependency on procmux. It works with any backend that
satisfies the ProcessConnection protocol defined in claudewire.types.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from opentelemetry.propagate import inject

from claudewire.schema import (
    SchemaValidationError,
    ValidationError,
    validate_inbound_or_bare,
    validate_outbound,
)
from claudewire.types import CommandResult, ExitEvent, StderrEvent, StdoutEvent

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from claudewire.types import ProcessConnection, ProcessEventQueue

    ValidationCallback = Callable[[dict[str, Any], list[ValidationError]], Awaitable[None] | None]

log = logging.getLogger(__name__)
_tracer = trace.get_tracer(__name__)


class BridgeTransport:
    """SDK Transport that routes through a ProcessConnection for one agent.

    Implements the claude_agent_sdk.Transport interface (6 abstract methods).
    For reconnecting agents, intercepts the initialize control_request and
    fakes a success response -- the CLI is already initialized.
    """

    def __init__(
        self,
        name: str,
        conn: ProcessConnection,
        *,
        reconnecting: bool = False,
        stderr_callback: Callable[[str], None] | None = None,
        stdio_logger: logging.Logger | None = None,
        on_validation_error: ValidationCallback | None = None,
    ):
        self._name = name
        self._conn = conn
        self._reconnecting = reconnecting
        self._stderr_callback = stderr_callback
        self._stdio_logger = stdio_logger
        self._on_validation_error = on_validation_error
        self._queue: ProcessEventQueue | None = None
        self._ready = False
        self._cli_exited = False
        self._exit_code: int | None = None

    async def connect(self) -> None:
        """Register with the process connection for this agent's output.

        Idempotent — calling connect() multiple times (e.g. from both
        _create_transport and ClaudeSDKClient.__aenter__) reuses the
        existing queue instead of creating a duplicate registration.
        """
        if self._ready and self._queue is not None:
            return
        _tracer.start_span("claudewire.connect", attributes={"agent.name": self._name}).end()
        self._queue = self._conn.register(self._name)
        self._ready = True

    async def spawn(self, cli_args: list[str], env: dict[str, str], cwd: str) -> CommandResult:
        """Tell the process connection to spawn a new CLI process."""
        _tracer.start_span("claudewire.spawn", attributes={"agent.name": self._name, "agent.cwd": cwd}).end()
        result = await self._conn.spawn(self._name, cli_args=cli_args, env=env, cwd=cwd)
        if not result.ok:
            raise RuntimeError(f"Spawn failed for '{self._name}': {result}")
        return result

    async def subscribe(self) -> CommandResult:
        """Subscribe to this agent's output (triggers buffer replay)."""
        result = await self._conn.subscribe(self._name)
        if not result.ok:
            raise RuntimeError(f"Subscribe failed for '{self._name}': {result}")
        return result

    async def _handle_validation_errors(
        self, msg: dict[str, Any], errors: list[ValidationError], direction: str
    ) -> None:
        """Log validation errors and invoke the callback if set."""
        summary = "; ".join(str(e) for e in errors[:5])
        log.warning("[%s][%s] schema validation %s: %s", direction, self._name, "errors" if errors else "ok", summary)
        if SchemaValidationError is not None and any(e.level == "error" for e in errors):
            from claudewire.schema import STRICT_MODE

            if STRICT_MODE:
                raise SchemaValidationError(errors, msg)
        if self._on_validation_error:
            result = self._on_validation_error(msg, errors)
            if asyncio.iscoroutine(result):
                await result

    async def write(self, data: str) -> None:
        """Write data to the CLI's stdin."""
        with _tracer.start_as_current_span(
            "claudewire.write",
            attributes={"agent.name": self._name, "data.bytes": len(data)},
        ):
            if not self._conn.is_alive:
                raise ConnectionError("Process connection is dead")
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
                    await self._queue.put(StdoutEvent(name=self._name, data=fake_response))
                self._reconnecting = False
                return

            # Validate outbound message
            outbound_result = validate_outbound(msg)
            if outbound_result.errors:
                await self._handle_validation_errors(msg, outbound_result.errors, "outbound")

            # Inject OTel trace context so downstream processes can link spans
            carrier: dict[str, str] = {}
            inject(carrier)
            if carrier:
                msg["_trace_context"] = carrier

            if self._stdio_logger:
                self._stdio_logger.debug(">>> STDIN  %s", json.dumps(msg))
            await self._conn.send_stdin(self._name, msg)

    async def read_messages(self):
        """Async generator yielding parsed JSON dicts from CLI stdout."""
        if not self._queue:
            return
        if not self._conn.is_alive:
            raise ConnectionError("Process connection is dead")
        while True:
            msg = await self._queue.get()
            # stop() was called — discard any buffered messages and return
            if self._cli_exited:
                return
            if msg is None:
                raise ConnectionError("Process connection lost during read")
            if isinstance(msg, StdoutEvent):
                msg_type = msg.data.get("type", "?")
                log.debug("[read][%s] yielding stdout type=%s", self._name, msg_type)
                if self._stdio_logger:
                    self._stdio_logger.debug("<<< STDOUT %s", json.dumps(msg.data))
                # Validate inbound message
                inbound_result = validate_inbound_or_bare(msg.data)
                if inbound_result.errors:
                    await self._handle_validation_errors(msg.data, inbound_result.errors, "inbound")
                yield msg.data
            elif isinstance(msg, StderrEvent):
                log.debug("[read][%s] stderr: %.200s", self._name, msg.text)
                if self._stdio_logger:
                    self._stdio_logger.debug("<<< STDERR %s", msg.text)
                if self._stderr_callback:
                    self._stderr_callback(msg.text)
            elif isinstance(msg, ExitEvent):
                log.debug("[read][%s] exit code=%s", self._name, msg.code)
                _tracer.start_span(
                    "claudewire.cli_exit",
                    attributes={"agent.name": self._name, "exit.code": msg.code or -1},
                ).end()
                if self._stdio_logger:
                    self._stdio_logger.debug("--- EXIT   code=%s", msg.code)
                self._cli_exited = True
                self._exit_code = msg.code
                return

    async def stop(self) -> None:
        """Immediately terminate the read stream and kill the CLI process.

        Unlike close(), which blocks until the process dies, stop() returns
        instantly.  It injects an ExitEvent into the local queue so that
        read_messages() terminates on the next iteration (discarding any
        buffered messages).  The actual process kill runs in the background.

        Safe to call from any coroutine — including one that is concurrently
        iterating read_messages().
        """
        if self._cli_exited:
            return
        self._cli_exited = True
        log.debug("[stop][%s] injecting ExitEvent and scheduling background kill", self._name)
        _tracer.start_span("claudewire.stop", attributes={"agent.name": self._name}).end()
        # Inject ExitEvent to unblock read_messages() immediately
        if self._queue:
            await self._queue.put(ExitEvent(name=self._name, code=None))
        # Kill the process in the background (don't wait)
        asyncio.create_task(self._background_kill())

    async def _background_kill(self) -> None:
        """Kill the process via the connection. Runs as a fire-and-forget task."""
        try:
            await self._conn.kill(self._name)
        except Exception:
            log.debug("[stop][%s] background kill failed (process may already be dead)", self._name)

    async def close(self) -> None:
        """Kill the CLI process and unregister."""
        _tracer.start_span("claudewire.close", attributes={"agent.name": self._name}).end()
        if self._ready and not self._cli_exited:
            try:
                await self._conn.kill(self._name)
            except Exception:
                pass
        self._conn.unregister(self._name)
        self._ready = False

    def is_ready(self) -> bool:
        return self._ready

    async def end_input(self) -> None:
        """Not needed for bridge mode -- CLI stdin stays open for multi-turn."""

    @property
    def cli_exited(self) -> bool:
        return self._cli_exited
