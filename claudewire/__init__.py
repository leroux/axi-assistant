"""Claude Wire -- Claude CLI stream-json protocol wrapper.

Wraps the Claude Code CLI's ``--output-format stream-json`` protocol:
- BridgeTransport: SDK Transport implementation over procmux
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

__all__ = [
    "ActivityState",
    "BridgeTransport",
    "as_stream",
    "build_cli_spawn_args",
    "disconnect_client",
    "ensure_process_dead",
    "get_subprocess_pid",
    "update_activity",
]
