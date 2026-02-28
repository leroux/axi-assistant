"""Agent bridge — persistent relay between bot.py and Claude CLI subprocesses.

This package contains both the bridge server (runs as a separate process) and the
client-side transport (imported by bot.py). The bridge is intentionally dumb — it
routes JSON lines between a single bot.py connection and N CLI subprocesses, buffering
output when bot.py is disconnected.

Server usage (separate process):
    python -m axi.bridge /path/to/.bridge.sock

Client usage (from bot.py):
    from axi.bridge import BridgeConnection, BridgeTransport, ensure_bridge

Architecture:
    bot.py <── Unix socket ──> bridge server ──stdio──> CLI 1
                                              ──stdio──> CLI 2
                                              ──stdio──> CLI 3
"""

from axi.bridge.client import BridgeConnection, BridgeTransport
from axi.bridge.helpers import build_cli_spawn_args, connect_to_bridge, ensure_bridge, start_bridge
from axi.bridge.protocol import (
    CmdMsg,
    ExitMsg,
    ResultMsg,
    StderrMsg,
    StdinMsg,
    StdoutMsg,
)
from axi.bridge.server import BridgeServer, CliProcess

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
