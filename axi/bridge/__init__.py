"""Agent bridge -- compatibility shim.

The core process multiplexer has been extracted to ``procmux``.
Claude-aware transport and CLI arg building have moved to ``claudewire``.

This module re-exports everything under the old names for backward compatibility.
"""

from claudewire import BridgeTransport, build_cli_spawn_args
from procmux import (
    CmdMsg,
    ExitMsg,
    ResultMsg,
    StderrMsg,
    StdinMsg,
    StdoutMsg,
)
from procmux import (
    ManagedProcess as CliProcess,
)
from procmux import (
    ProcmuxConnection as BridgeConnection,
)
from procmux import (
    ProcmuxServer as BridgeServer,
)
from procmux import (
    connect as connect_to_bridge,
)
from procmux import (
    ensure_running as ensure_bridge,
)
from procmux import (
    start as start_bridge,
)

__all__ = [
    "BridgeConnection",
    "BridgeServer",
    "BridgeTransport",
    "CliProcess",
    "CmdMsg",
    "ExitMsg",
    "ResultMsg",
    "StderrMsg",
    "StdinMsg",
    "StdoutMsg",
    "build_cli_spawn_args",
    "connect_to_bridge",
    "ensure_bridge",
    "start_bridge",
]
