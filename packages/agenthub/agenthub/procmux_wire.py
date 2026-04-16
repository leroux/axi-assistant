"""Adapter wiring procmux to claudewire's ProcessConnection protocol.

This is the glue between the two standalone packages: it wraps a
ProcmuxConnection so that claudewire's BridgeTransport can use it
without importing procmux directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from claudewire.types import CommandResult, ExitEvent, ProcessEvent, StderrEvent, StdoutEvent

from procmux.protocol import ExitMsg, ResultMsg, StderrMsg, StdoutMsg

if TYPE_CHECKING:
    import asyncio

    from procmux import ProcmuxConnection


def _result_processes(result: ResultMsg) -> dict[str, Any]:
    processes = getattr(result, "processes", None)
    if processes is not None:
        return processes
    agents = getattr(result, "agents", None)
    return agents or {}


class _TranslatingQueue:
    """Wraps a procmux message queue, translating to claudewire event types on the fly."""

    def __init__(self, raw: asyncio.Queue[StdoutMsg | StderrMsg | ExitMsg | None]) -> None:
        self._raw = raw

    async def get(self) -> ProcessEvent | None:
        msg = await self._raw.get()
        if msg is None:
            return None
        if isinstance(msg, StdoutMsg):
            return StdoutEvent(name=msg.name, data=msg.data)
        if isinstance(msg, StderrMsg):
            return StderrEvent(name=msg.name, text=msg.text)
        # ExitMsg
        return ExitEvent(name=msg.name, code=msg.code)

    async def put(self, event: ProcessEvent | None) -> None:
        """Reverse-translate claudewire events back to procmux types.

        Only used by BridgeTransport's reconnect interception (fake initialize response).
        """
        if event is None:
            await self._raw.put(None)
        elif isinstance(event, StdoutEvent):
            await self._raw.put(StdoutMsg(name=event.name, data=event.data))
        elif isinstance(event, StderrEvent):
            await self._raw.put(StderrMsg(name=event.name, text=event.text))
        elif isinstance(event, ExitEvent):
            await self._raw.put(ExitMsg(name=event.name, code=event.code))

    def get_nowait(self) -> ProcessEvent | None:
        msg = self._raw.get_nowait()
        if msg is None:
            return None
        if isinstance(msg, StdoutMsg):
            return StdoutEvent(name=msg.name, data=msg.data)
        if isinstance(msg, StderrMsg):
            return StderrEvent(name=msg.name, text=msg.text)
        return ExitEvent(name=msg.name, code=msg.code)


class ProcmuxProcessConnection:
    """Adapts a ProcmuxConnection to claudewire's ProcessConnection protocol.

    Usage::

        from procmux import connect
        from agenthub.procmux_wire import ProcmuxProcessConnection
        from claudewire import BridgeTransport

        raw_conn = await connect(socket_path)
        conn = ProcmuxProcessConnection(raw_conn)
        transport = BridgeTransport("agent-1", conn)
    """

    def __init__(self, conn: ProcmuxConnection) -> None:
        self._conn = conn

    @property
    def is_alive(self) -> bool:
        return self._conn.is_alive

    def register(self, name: str) -> _TranslatingQueue:  # type: ignore[override]
        raw_queue = self._conn.register_process(name)
        return _TranslatingQueue(raw_queue)

    def unregister(self, name: str) -> None:
        self._conn.unregister_process(name)

    async def spawn(
        self,
        name: str,
        *,
        cli_args: list[str],
        env: dict[str, str],
        cwd: str,
    ) -> CommandResult:
        result = await self._conn.send_command(
            "spawn",
            name=name,
            cli_args=cli_args,
            env=env,
            cwd=cwd,
            env_inherit=False,
        )
        return CommandResult(
            ok=result.ok,
            error=result.error,
            already_running=result.already_running,
        )

    async def subscribe(self, name: str) -> CommandResult:
        result = await self._conn.send_command("subscribe", name=name)
        return CommandResult(
            ok=result.ok,
            error=result.error,
            replayed=result.replayed,
            status=result.status,
            idle=result.idle,
        )

    async def kill(self, name: str) -> CommandResult:
        result = await self._conn.send_command("kill", name=name)
        return CommandResult(ok=result.ok, error=result.error)

    async def send_stdin(self, name: str, data: dict[str, Any]) -> None:
        await self._conn.send_stdin(name, data)

    async def close(self) -> None:
        """Close the underlying procmux connection."""
        await self._conn.close()

    async def list_agents(self) -> CommandResult:
        """List all processes managed by procmux."""
        result = await self._conn.send_command("list")
        return CommandResult(ok=result.ok, agents=list(_result_processes(result)))

    async def send_raw_command(self, cmd: str, **kwargs: Any) -> CommandResult:
        """Send an arbitrary command to procmux (for commands not in the protocol)."""
        result = await self._conn.send_command(cmd, **kwargs)
        return CommandResult(
            ok=result.ok,
            error=result.error,
            already_running=result.already_running,
            replayed=result.replayed,
            status=result.status,
            idle=result.idle,
            agents=list(_result_processes(result)),
        )
