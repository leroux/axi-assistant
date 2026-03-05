"""Unit tests for ProcmuxServer shutdown behavior.

Tests that the server shuts down promptly in all scenarios,
especially when a client is connected (the main bug that caused
15-second restart delays via systemd TimeoutStopSec).

Uses real Unix sockets with ephemeral paths — no mocking of the wire.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid

import pytest

from procmux import ProcmuxServer


def _tmp_sock() -> str:
    return f"/tmp/test_procmux_shutdown_{uuid.uuid4().hex[:8]}.sock"


PYTHON = sys.executable


async def _start_server(sock: str) -> tuple[ProcmuxServer, asyncio.Server]:
    """Start a ProcmuxServer without its signal-handler / shutdown_event loop."""
    server = ProcmuxServer(sock)
    if os.path.exists(sock):
        os.unlink(sock)
    srv = await asyncio.start_unix_server(server._handle_client, path=sock)
    server._server = srv
    return server, srv


@pytest.fixture
def sock_path(tmp_path):
    """Return a unique socket path, cleaned up after test."""
    path = str(tmp_path / "test.sock")
    yield path
    if os.path.exists(path):
        os.unlink(path)


class TestShutdownNoClient:
    async def test_shutdown_completes_promptly(self, sock_path: str) -> None:
        """Server with no connected client shuts down fast."""
        server, srv = await _start_server(sock_path)
        t0 = time.monotonic()
        await server._shutdown()
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0, f"Shutdown took {elapsed:.1f}s — expected < 2s"
        assert not os.path.exists(sock_path), "Socket file should be cleaned up"


class TestShutdownWithClient:
    async def test_shutdown_with_connected_client(self, sock_path: str) -> None:
        """Server shuts down promptly even when a client is connected.

        This is the main regression test: previously, _shutdown() hung
        indefinitely on server.wait_closed() because _handle_client was
        blocked on readline() with an open writer.
        """
        server, srv = await _start_server(sock_path)

        # Connect a client
        reader, writer = await asyncio.open_unix_connection(sock_path)
        await asyncio.sleep(0.1)  # let _handle_client start

        t0 = time.monotonic()
        await server._shutdown()
        elapsed = time.monotonic() - t0

        assert elapsed < 5.0, f"Shutdown took {elapsed:.1f}s — expected < 5s"
        assert not os.path.exists(sock_path), "Socket file should be cleaned up"

        # Client should see the connection closed
        writer.close()

    async def test_shutdown_with_client_and_managed_process(self, sock_path: str) -> None:
        """Server kills managed processes and closes client during shutdown."""
        server, srv = await _start_server(sock_path)

        # Connect a client
        reader, writer = await asyncio.open_unix_connection(sock_path)
        await asyncio.sleep(0.1)

        # Spawn a long-running process via the server
        from procmux.protocol import CmdMsg
        cmd = CmdMsg(
            cmd="spawn",
            name="test-proc",
            cli_args=[PYTHON, "-c", "import time; time.sleep(3600)"],
        )
        writer.write(cmd.model_dump_json().encode() + b"\n")
        await writer.drain()
        await asyncio.sleep(0.5)  # let spawn complete

        assert "test-proc" in server._procs
        proc_pid = server._procs["test-proc"].proc.pid

        t0 = time.monotonic()
        await server._shutdown()
        elapsed = time.monotonic() - t0

        assert elapsed < 10.0, f"Shutdown took {elapsed:.1f}s — expected < 10s"

        # Managed process should be dead
        try:
            os.kill(proc_pid, 0)
            pytest.fail(f"Process {proc_pid} should be dead after shutdown")
        except ProcessLookupError:
            pass  # expected

        writer.close()


class TestShutdownTiming:
    async def test_total_shutdown_under_5_seconds(self, sock_path: str) -> None:
        """Regression gate: total shutdown must complete in < 5 seconds
        when there are no managed processes (the common restart case).
        """
        server, srv = await _start_server(sock_path)

        # Connect a client (simulates bot.py connection)
        reader, writer = await asyncio.open_unix_connection(sock_path)
        await asyncio.sleep(0.1)

        t0 = time.monotonic()
        await server._shutdown()
        elapsed = time.monotonic() - t0

        assert elapsed < 5.0, (
            f"Shutdown took {elapsed:.1f}s — must be < 5s to avoid systemd timeout"
        )
        writer.close()
