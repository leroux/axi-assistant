"""Bridge lifecycle helpers — used by bot.py to start/connect to the bridge."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

from .client import BridgeConnection

log = logging.getLogger(__name__)


async def connect_to_bridge(socket_path: str) -> BridgeConnection | None:
    """Try to connect to an existing bridge. Returns None if bridge isn't running."""
    try:
        reader, writer = await asyncio.open_unix_connection(socket_path, limit=10 * 1024 * 1024)
        conn = BridgeConnection(reader, writer)
        log.info("Connected to bridge at %s", socket_path)
        return conn
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        return None


def _bridge_log_path(socket_path: str) -> str:
    """Derive the bridge log file path from the socket path."""
    # .bridge.sock -> .bridge.log
    base = socket_path.rsplit(".", 1)[0] if "." in socket_path else socket_path
    return base + ".log"


async def start_bridge(socket_path: str) -> asyncio.subprocess.Process:
    """Start the bridge as a subprocess in its own process group."""
    log_path = _bridge_log_path(socket_path)
    log_file = open(log_path, "a")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "bridge",
        socket_path,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=log_file,
        start_new_session=True,
    )
    log_file.close()  # child inherited the fd, we can close ours
    log.info("Started bridge process (pid=%d), logging to %s", proc.pid, log_path)
    return proc


async def ensure_bridge(socket_path: str, timeout: float = 10.0) -> BridgeConnection:
    """Ensure the bridge is running and return a connection to it.

    Tries to connect first. If that fails, starts the bridge and waits for it.
    """
    # Try existing bridge
    conn = await connect_to_bridge(socket_path)
    if conn is not None:
        return conn

    # Start new bridge
    bridge_proc = await start_bridge(socket_path)

    # Wait for socket to appear
    log_path = _bridge_log_path(socket_path)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        conn = await connect_to_bridge(socket_path)
        if conn is not None:
            return conn
        # Check if bridge died
        if bridge_proc.returncode is not None:
            stderr = ""
            try:
                stderr = open(log_path).read()[-500:]
            except OSError:
                pass
            raise RuntimeError(f"Bridge process died (exit code {bridge_proc.returncode}): {stderr}")

    raise RuntimeError(f"Timed out waiting for bridge at {socket_path}")


def build_cli_spawn_args(options) -> tuple[list[str], dict[str, str], str]:
    """Build CLI command, env, and cwd from ClaudeAgentOptions.

    Uses SubprocessCLITransport._build_command() to stay in sync with
    the SDK's command building logic.
    """
    from dataclasses import replace

    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
    from claude_agent_sdk._version import __version__ as sdk_version

    # Replicate the permission_prompt_tool_name injection from
    # ClaudeSDKClient.connect() (client.py ~line 122).  In direct mode,
    # connect() sets this before creating SubprocessCLITransport; in bridge
    # mode the CLI is already spawned by the time connect() runs, so we must
    # apply the same logic here.
    if getattr(options, "can_use_tool", None) and not options.permission_prompt_tool_name:
        options = replace(options, permission_prompt_tool_name="stdio")

    # Create temp transport just for _build_command() — no side effects
    temp = SubprocessCLITransport(prompt="", options=options)
    cmd = temp._build_command()

    # Replicate env construction from SubprocessCLITransport.connect()
    env = {
        **os.environ,
        **(options.env or {}),
        "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
        "CLAUDE_AGENT_SDK_VERSION": sdk_version,
    }
    if getattr(options, "enable_file_checkpointing", False):
        env["CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING"] = "true"
    cwd_path = str(options.cwd or os.getcwd())
    if cwd_path:
        env["PWD"] = cwd_path

    return cmd, env, cwd_path
