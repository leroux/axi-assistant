"""Claude Wire -- Claude CLI stream-json protocol wrapper.

Wraps the Claude Code CLI's ``--output-format stream-json`` protocol.
Has NO dependency on procmux or any specific process transport backend.
The wiring layer (e.g. agenthub) provides an adapter from a concrete
transport to the ProcessConnection protocol.

- BridgeTransport: SDK Transport implementation over any ProcessConnection
- ProcessConnection: protocol that any process backend must satisfy
- Event types: StdoutEvent, StderrEvent, ExitEvent
- build_cli_spawn_args: CLI argument construction
- Event parsing and activity tracking
- Session lifecycle helpers (disconnect, subprocess cleanup)
"""

from claudewire.cli import build_cli_spawn_args
from claudewire.events import (
    ActivityState,
    as_stream,
    update_activity,
)
from claudewire.session import disconnect_client, ensure_process_dead, get_subprocess_pid
from claudewire.transport import BridgeTransport
from claudewire.types import (
    CommandResult,
    ExitEvent,
    ProcessConnection,
    ProcessEvent,
    ProcessEventQueue,
    StderrEvent,
    StdoutEvent,
)

__all__ = [
    "ActivityState",
    "BridgeTransport",
    "CommandResult",
    "ExitEvent",
    "ProcessConnection",
    "ProcessEvent",
    "ProcessEventQueue",
    "StderrEvent",
    "StdoutEvent",
    "as_stream",
    "build_cli_spawn_args",
    "disconnect_client",
    "ensure_process_dead",
    "get_subprocess_pid",
    "update_activity",
]
