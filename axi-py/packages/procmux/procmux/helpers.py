"""Procmux lifecycle helpers — start, connect, and ensure the server is running."""

from __future__ import annotations

import asyncio
import logging
import sys
import time

from .client import ProcmuxConnection

log = logging.getLogger(__name__)


async def connect(socket_path: str) -> ProcmuxConnection | None:
    """Try to connect to an existing procmux server. Returns None if not running."""
    try:
        reader, writer = await asyncio.open_unix_connection(socket_path, limit=10 * 1024 * 1024)
        conn = ProcmuxConnection(reader, writer)
        log.info("Connected to procmux at %s", socket_path)
        return conn
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        return None


def _log_path(socket_path: str) -> str:
    """Derive the log file path from the socket path."""
    base = socket_path.rsplit(".", 1)[0] if "." in socket_path else socket_path
    return base + ".log"


async def start(socket_path: str) -> asyncio.subprocess.Process:
    """Start the procmux server as a subprocess in its own process group."""
    log_file_path = _log_path(socket_path)
    with open(log_file_path, "a") as log_file:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "procmux",
            socket_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=log_file,
            start_new_session=True,
        )
    log.info("Started procmux server (pid=%d), logging to %s", proc.pid, log_file_path)
    return proc


async def ensure_running(socket_path: str, timeout: float = 10.0) -> ProcmuxConnection:
    """Ensure the procmux server is running and return a connection.

    Tries to connect first. If that fails, starts the server and waits for it.
    """
    conn = await connect(socket_path)
    if conn is not None:
        return conn

    proc = await start(socket_path)
    log_file_path = _log_path(socket_path)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        conn = await connect(socket_path)
        if conn is not None:
            return conn
        if proc.returncode is not None:
            stderr = ""
            try:
                with open(log_file_path) as f:
                    stderr = f.read()[-500:]
            except OSError:
                pass
            raise RuntimeError(f"Procmux server died (exit code {proc.returncode}): {stderr}")

    raise RuntimeError(f"Timed out waiting for procmux at {socket_path}")
