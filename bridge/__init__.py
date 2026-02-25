"""Agent bridge — persistent relay between bot.py and Claude CLI subprocesses.

This package contains both the bridge server (runs as a separate process) and the
client-side transport (imported by bot.py). The bridge is intentionally dumb — it
routes JSON lines between a single bot.py connection and N CLI subprocesses, buffering
output when bot.py is disconnected.

Server usage (separate process):
    python -m bridge /path/to/.bridge.sock

Client usage (from bot.py):
    from bridge import BridgeConnection, BridgeTransport, ensure_bridge

Architecture:
    bot.py <── Unix socket ──> bridge server ──stdio──> CLI 1
                                              ──stdio──> CLI 2
                                              ──stdio──> CLI 3
"""

from bridge.protocol import (
    CmdMsg, StdinMsg, ResultMsg, StdoutMsg, StderrMsg, ExitMsg,
)

# String constants for test compatibility
TYPE_CMD = "cmd"
TYPE_STDIN = "stdin"
TYPE_RESULT = "result"
TYPE_STDOUT = "stdout"
TYPE_STDERR = "stderr"
TYPE_EXIT = "exit"
from bridge.server import BridgeServer, CliProcess
from bridge.client import BridgeConnection, BridgeTransport
from bridge.helpers import connect_to_bridge, ensure_bridge, start_bridge, build_cli_spawn_args
