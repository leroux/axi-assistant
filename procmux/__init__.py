"""Procmux — dumb process multiplexer.

Spawns named OS subprocesses with stdin/stdout/stderr pipes, multiplexes
them over one Unix socket connection (IPC), and buffers output when the
client is disconnected.  Zero intelligence — no knowledge of Claude,
agents, sessions, or any semantic layer.

Server usage (separate process):
    python -m procmux /path/to/socket.sock

Client usage:
    from procmux import ProcmuxConnection, connect, ensure_running

Architecture:
    client <── Unix socket ──> procmux server ──stdio──> process 1
                                               ──stdio──> process 2
                                               ──stdio──> process 3
"""

from procmux.client import ProcmuxConnection
from procmux.helpers import connect, ensure_running, start
from procmux.protocol import (
    CmdMsg,
    ExitMsg,
    ResultMsg,
    StderrMsg,
    StdinMsg,
    StdoutMsg,
)
from procmux.server import ManagedProcess, ProcmuxServer

__all__ = [
    "CmdMsg",
    "ExitMsg",
    "ManagedProcess",
    "ProcmuxConnection",
    "ProcmuxServer",
    "ResultMsg",
    "StderrMsg",
    "StdinMsg",
    "StdoutMsg",
    "connect",
    "ensure_running",
    "start",
]
