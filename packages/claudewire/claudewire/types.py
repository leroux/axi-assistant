"""Process IO protocol for claudewire.

Defines the abstract interface that any process transport must implement.
claudewire works with these types -- it never imports from procmux or any
other specific transport backend. The wiring layer (e.g. agenthub) provides
an adapter from a concrete transport (e.g. procmux) to this protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

# ---------------------------------------------------------------------------
# Process output events
# ---------------------------------------------------------------------------


@dataclass
class StdoutEvent:
    """JSON data from a process's stdout."""

    name: str
    data: dict[str, Any]


@dataclass
class StderrEvent:
    """Text line from a process's stderr."""

    name: str
    text: str


@dataclass
class ExitEvent:
    """Process exited."""

    name: str
    code: int | None


ProcessEvent = StdoutEvent | StderrEvent | ExitEvent


# ---------------------------------------------------------------------------
# Command result
# ---------------------------------------------------------------------------


@dataclass
class CommandResult:
    """Result of a spawn/subscribe/kill command."""

    ok: bool
    error: str | None = None
    already_running: bool = False
    # Fields populated by subscribe
    replayed: int | None = None
    status: str | None = None
    idle: bool | None = None
    # Fields populated by list
    agents: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Process event queue protocol
# ---------------------------------------------------------------------------


class ProcessEventQueue(Protocol):
    """Async queue of process events.

    Both asyncio.Queue[ProcessEvent | None] and custom translating queues
    (like agenthub's _TranslatingQueue) satisfy this protocol.
    """

    async def get(self) -> ProcessEvent | None: ...

    async def put(self, event: ProcessEvent | None) -> None: ...


# ---------------------------------------------------------------------------
# Process connection protocol
# ---------------------------------------------------------------------------


class ProcessConnection(Protocol):
    """Abstract connection to a process manager.

    Any backend (procmux, direct subprocess, SSH, etc.) can implement
    this protocol to be used with claudewire's BridgeTransport.
    """

    @property
    def is_alive(self) -> bool: ...

    def register(self, name: str) -> ProcessEventQueue: ...

    def unregister(self, name: str) -> None: ...

    async def spawn(
        self,
        name: str,
        *,
        cli_args: list[str],
        env: dict[str, str],
        cwd: str,
    ) -> CommandResult: ...

    async def subscribe(self, name: str) -> CommandResult: ...

    async def kill(self, name: str) -> CommandResult: ...

    async def send_stdin(self, name: str, data: dict[str, Any]) -> None: ...
