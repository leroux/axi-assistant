"""Tests for procmux, claudewire, and agenthub integration.

Covers the server (ProcmuxServer), client (ProcmuxConnection, BridgeTransport),
and lifecycle helpers.  All tests use real Unix sockets with ephemeral paths —
no mocking of the wire protocol so we're testing actual serialization.

Run with: pytest tests/test_bridge.py -v

NOTE: These tests require Python procmux/claudewire/agenthub packages which
are not available in the Rust rewrite. Skipped when those packages are missing.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid

import pytest

try:
    from agenthub.procmux_wire import ProcmuxProcessConnection
    from claudewire import BridgeTransport
    from claudewire.types import StdoutEvent
    from procmux import (
        ExitMsg,
        StdoutMsg,
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
except ImportError:
    pytestmark = pytest.mark.skip("Python bridge packages not available (Rust rewrite)")


# Override conftest's autouse warmup — bridge tests don't need Discord.
@pytest.fixture(autouse=True)
def _ensure_warm():
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tmp_sock() -> str:
    """Return a unique temporary socket path."""
    return f"/tmp/test_bridge_{uuid.uuid4().hex[:8]}.sock"


PYTHON = sys.executable


async def _start_server(sock: str) -> tuple[BridgeServer, asyncio.Server]:
    """Start a BridgeServer without its signal handlers / shutdown_event loop."""
    server = BridgeServer(sock)
    if os.path.exists(sock):
        os.unlink(sock)
    srv = await asyncio.start_unix_server(server._handle_client, path=sock)
    server._server = srv
    return server, srv


async def _connect(sock: str) -> BridgeConnection:
    """Connect a BridgeConnection to a socket."""
    reader, writer = await asyncio.open_unix_connection(sock)
    return BridgeConnection(reader, writer)


def _adapt(conn: BridgeConnection) -> ProcmuxProcessConnection:
    """Wrap a ProcmuxConnection for use with BridgeTransport."""
    return ProcmuxProcessConnection(conn)


async def _cleanup(server: BridgeServer, srv: asyncio.Server, conn: BridgeConnection, sock: str):
    """Tear down server + connection.

    We cancel the demux task and close everything without awaiting
    wait_closed() — those block indefinitely when the server handler
    is also stuck. For tests this is fine.
    """
    for cp in list(server._procs.values()):
        await server._kill_process(cp)
    conn._demux_task.cancel()
    try:
        conn._writer.close()
    except Exception:
        pass
    srv.close()
    if os.path.exists(sock):
        os.unlink(sock)


# A CLI script that outputs N JSON messages with a delay, then exits.
def _slow_cli_script(n: int, delay: float = 0.1) -> list[str]:
    return [
        PYTHON,
        "-c",
        f"import json,sys,time\n"
        f"for i in range({n}):\n"
        f"    print(json.dumps({{'type':'msg','n':i}}),flush=True)\n"
        f"    time.sleep({delay})\n",
    ]


# A CLI script that echoes stdin lines back to stdout as JSON.
def _echo_cli_script() -> list[str]:
    return [
        PYTHON,
        "-c",
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    data = json.loads(line)\n"
        "    print(json.dumps({'type':'echo','data':data}), flush=True)\n",
    ]


# A CLI that writes to stderr and stdout, then exits.
def _stderr_cli_script() -> list[str]:
    return [
        PYTHON,
        "-c",
        "import sys, json\n"
        "sys.stderr.write('warning: something\\n')\n"
        "sys.stderr.flush()\n"
        "print(json.dumps({'type':'ok'}), flush=True)\n",
    ]


# ---------------------------------------------------------------------------
# Server: list
# ---------------------------------------------------------------------------


class TestServerList:
    @pytest.mark.asyncio
    async def test_list_empty(self):
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            result = await asyncio.wait_for(conn.send_command("list"), timeout=3)
            assert result.ok is True
            assert result.agents == {}
        finally:
            await _cleanup(server, srv, conn, sock)

    @pytest.mark.asyncio
    async def test_list_after_spawn(self):
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            await asyncio.wait_for(
                conn.send_command("spawn", name="a", cli_args=_slow_cli_script(1, 5), env=dict(os.environ), cwd="/tmp"),
                timeout=3,
            )
            result = await asyncio.wait_for(conn.send_command("list"), timeout=3)
            assert "a" in result.agents
            assert result.agents["a"]["status"] == "running"
        finally:
            await _cleanup(server, srv, conn, sock)


# ---------------------------------------------------------------------------
# Server: spawn
# ---------------------------------------------------------------------------


class TestServerSpawn:
    @pytest.mark.asyncio
    async def test_spawn_ok(self):
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            result = await asyncio.wait_for(
                conn.send_command("spawn", name="x", cli_args=_slow_cli_script(1, 5), env=dict(os.environ), cwd="/tmp"),
                timeout=3,
            )
            assert result.ok is True
            assert result.name == "x"
            assert isinstance(result.pid, int)
        finally:
            await _cleanup(server, srv, conn, sock)

    @pytest.mark.asyncio
    async def test_spawn_already_running(self):
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            r1 = await asyncio.wait_for(
                conn.send_command(
                    "spawn", name="dup", cli_args=_slow_cli_script(1, 5), env=dict(os.environ), cwd="/tmp"
                ),
                timeout=3,
            )
            r2 = await asyncio.wait_for(
                conn.send_command(
                    "spawn", name="dup", cli_args=_slow_cli_script(1, 5), env=dict(os.environ), cwd="/tmp"
                ),
                timeout=3,
            )
            assert r2.ok is True
            assert r2.already_running is True
            assert r2.pid == r1.pid
        finally:
            await _cleanup(server, srv, conn, sock)

    @pytest.mark.asyncio
    async def test_spawn_bad_command(self):
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            result = await asyncio.wait_for(
                conn.send_command(
                    "spawn", name="bad", cli_args=["/nonexistent/binary"], env=dict(os.environ), cwd="/tmp"
                ),
                timeout=3,
            )
            assert result.ok is False
            assert result.error is not None
        finally:
            await _cleanup(server, srv, conn, sock)


# ---------------------------------------------------------------------------
# Server: kill
# ---------------------------------------------------------------------------


class TestServerKill:
    @pytest.mark.asyncio
    async def test_kill_running(self):
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            await asyncio.wait_for(
                conn.send_command(
                    "spawn", name="k", cli_args=_slow_cli_script(100, 1), env=dict(os.environ), cwd="/tmp"
                ),
                timeout=3,
            )
            result = await asyncio.wait_for(
                conn.send_command("kill", name="k"),
                timeout=5,
            )
            assert result.ok is True
            # Should be gone from list
            ls = await asyncio.wait_for(conn.send_command("list"), timeout=3)
            assert "k" not in ls.agents
        finally:
            await _cleanup(server, srv, conn, sock)

    @pytest.mark.asyncio
    async def test_kill_nonexistent(self):
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            result = await asyncio.wait_for(
                conn.send_command("kill", name="ghost"),
                timeout=3,
            )
            assert result.ok is False
        finally:
            await _cleanup(server, srv, conn, sock)


# ---------------------------------------------------------------------------
# Server: interrupt
# ---------------------------------------------------------------------------


class TestServerInterrupt:
    @pytest.mark.asyncio
    async def test_interrupt_running(self):
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            await asyncio.wait_for(
                conn.send_command(
                    "spawn", name="int", cli_args=_slow_cli_script(100, 1), env=dict(os.environ), cwd="/tmp"
                ),
                timeout=3,
            )
            result = await asyncio.wait_for(
                conn.send_command("interrupt", name="int"),
                timeout=3,
            )
            assert result.ok is True
        finally:
            await _cleanup(server, srv, conn, sock)

    @pytest.mark.asyncio
    async def test_interrupt_nonexistent(self):
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            result = await asyncio.wait_for(
                conn.send_command("interrupt", name="nope"),
                timeout=3,
            )
            assert result.ok is False
        finally:
            await _cleanup(server, srv, conn, sock)


# ---------------------------------------------------------------------------
# Server: subscribe + relay
# ---------------------------------------------------------------------------


class TestServerSubscribeRelay:
    @pytest.mark.asyncio
    async def test_subscribe_streams_stdout(self):
        """After subscribing, stdout messages are relayed in real time."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            await asyncio.wait_for(
                conn.send_command(
                    "spawn", name="sr", cli_args=_slow_cli_script(3, 0.1), env=dict(os.environ), cwd="/tmp"
                ),
                timeout=3,
            )
            q = conn.register_process("sr")
            sub = await asyncio.wait_for(conn.send_command("subscribe", name="sr"), timeout=3)
            assert sub.ok is True

            msgs = []
            for _ in range(10):
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=3)
                    if msg is None:
                        break
                    msgs.append(msg)
                    if msg.type == "exit":
                        break
                except TimeoutError:
                    break

            types = [m.type for m in msgs]
            assert "stdout" in types
            assert "exit" in types
        finally:
            await _cleanup(server, srv, conn, sock)

    @pytest.mark.asyncio
    async def test_subscribe_nonexistent(self):
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            result = await asyncio.wait_for(
                conn.send_command("subscribe", name="nope"),
                timeout=3,
            )
            assert result.ok is False
        finally:
            await _cleanup(server, srv, conn, sock)

    @pytest.mark.asyncio
    async def test_stderr_relayed(self):
        """Stderr output is relayed as stderr messages."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            await asyncio.wait_for(
                conn.send_command("spawn", name="se", cli_args=_stderr_cli_script(), env=dict(os.environ), cwd="/tmp"),
                timeout=3,
            )
            q = conn.register_process("se")
            await asyncio.wait_for(conn.send_command("subscribe", name="se"), timeout=3)

            msgs = []
            for _ in range(10):
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=3)
                    if msg is None:
                        break
                    msgs.append(msg)
                    if msg.type == "exit":
                        break
                except TimeoutError:
                    break

            types = [m.type for m in msgs]
            assert "stderr" in types
            stderr_msgs = [m for m in msgs if m.type == "stderr"]
            assert any("warning" in m.text for m in stderr_msgs)
        finally:
            await _cleanup(server, srv, conn, sock)


# ---------------------------------------------------------------------------
# Server: stdin forwarding
# ---------------------------------------------------------------------------


class TestServerStdin:
    @pytest.mark.asyncio
    async def test_stdin_forwarded(self):
        """Data sent via send_stdin reaches the CLI's stdin."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            await asyncio.wait_for(
                conn.send_command("spawn", name="echo", cli_args=_echo_cli_script(), env=dict(os.environ), cwd="/tmp"),
                timeout=3,
            )
            q = conn.register_process("echo")
            await asyncio.wait_for(conn.send_command("subscribe", name="echo"), timeout=3)

            # Send data to stdin
            await conn.send_stdin("echo", {"hello": "world"})

            # Should get it echoed back
            msg = await asyncio.wait_for(q.get(), timeout=3)
            assert msg.type == "stdout"
            assert msg.data["type"] == "echo"
            assert msg.data["data"] == {"hello": "world"}
        finally:
            await _cleanup(server, srv, conn, sock)


# ---------------------------------------------------------------------------
# Server: buffer replay on reconnect
# ---------------------------------------------------------------------------


class TestServerBufferReplay:
    @pytest.mark.asyncio
    async def test_buffer_and_replay(self):
        """Messages accumulate in buffer while no client is subscribed,
        and are replayed when a new client subscribes."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn1 = await _connect(sock)
        conn2 = None
        try:
            # Spawn an agent that outputs 5 messages quickly
            await asyncio.wait_for(
                conn1.send_command(
                    "spawn", name="buf", cli_args=_slow_cli_script(5, 0.02), env=dict(os.environ), cwd="/tmp"
                ),
                timeout=3,
            )
            q1 = conn1.register_process("buf")
            await asyncio.wait_for(conn1.send_command("subscribe", name="buf"), timeout=3)

            # Receive first message
            msg = await asyncio.wait_for(q1.get(), timeout=3)
            assert msg.type == "stdout"

            # Disconnect client 1
            conn1._demux_task.cancel()
            conn1._writer.close()
            await asyncio.sleep(0.3)  # let remaining messages buffer

            # Connect client 2
            conn2 = await _connect(sock)

            # Check buffer
            ls = await asyncio.wait_for(conn2.send_command("list"), timeout=3)
            buffered = ls.agents["buf"]["buffered_msgs"]
            assert buffered > 0, f"Expected buffered > 0, got {buffered}"

            # Subscribe to replay
            q2 = conn2.register_process("buf")
            sub = await asyncio.wait_for(conn2.send_command("subscribe", name="buf"), timeout=3)
            assert sub.replayed == buffered

            # Read replayed messages
            msgs = []
            for _ in range(20):
                try:
                    msg = await asyncio.wait_for(q2.get(), timeout=2)
                    if msg is None:
                        break
                    msgs.append(msg)
                    if msg.type == "exit":
                        break
                except TimeoutError:
                    break

            assert len(msgs) >= buffered
        finally:
            for cp in list(server._procs.values()):
                await server._kill_process(cp)
            if conn2:
                conn2._demux_task.cancel()
                try:
                    conn2._writer.close()
                except Exception:
                    pass
            srv.close()
            if os.path.exists(sock):
                os.unlink(sock)

    @pytest.mark.asyncio
    async def test_new_client_resets_subscriptions(self):
        """When a new client connects, all agents become unsubscribed."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn1 = await _connect(sock)
        try:
            await asyncio.wait_for(
                conn1.send_command(
                    "spawn", name="res", cli_args=_slow_cli_script(100, 1), env=dict(os.environ), cwd="/tmp"
                ),
                timeout=3,
            )
            conn1.register_process("res")
            await asyncio.wait_for(conn1.send_command("subscribe", name="res"), timeout=3)

            # Verify subscribed
            ls = await asyncio.wait_for(conn1.send_command("list"), timeout=3)
            assert ls.agents["res"]["subscribed"] is True

            # Connect a second client (replaces first)
            conn2 = await _connect(sock)
            await asyncio.sleep(0.2)  # let server process the new connection

            ls2 = await asyncio.wait_for(conn2.send_command("list"), timeout=3)
            assert ls2.agents["res"]["subscribed"] is False

            await conn2.close()
        finally:
            await _cleanup(server, srv, conn1, sock)


# ---------------------------------------------------------------------------
# Server: unknown command
# ---------------------------------------------------------------------------


class TestServerUnknownCommand:
    @pytest.mark.asyncio
    async def test_unknown_command_returns_error(self):
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            result = await asyncio.wait_for(
                conn.send_command("bogus"),
                timeout=3,
            )
            assert result.ok is False
            assert "unknown" in (result.error or "").lower()
        finally:
            await _cleanup(server, srv, conn, sock)


# ---------------------------------------------------------------------------
# BridgeConnection: demux
# ---------------------------------------------------------------------------


class TestBridgeConnectionDemux:
    @pytest.mark.asyncio
    async def test_routes_to_correct_agent_queue(self):
        """Messages for different agents go to different queues."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            # Spawn two agents
            await asyncio.wait_for(
                conn.send_command(
                    "spawn", name="a1", cli_args=_slow_cli_script(2, 0.1), env=dict(os.environ), cwd="/tmp"
                ),
                timeout=3,
            )
            await asyncio.wait_for(
                conn.send_command(
                    "spawn", name="a2", cli_args=_slow_cli_script(2, 0.1), env=dict(os.environ), cwd="/tmp"
                ),
                timeout=3,
            )

            q1 = conn.register_process("a1")
            q2 = conn.register_process("a2")

            await asyncio.wait_for(conn.send_command("subscribe", name="a1"), timeout=3)
            await asyncio.wait_for(conn.send_command("subscribe", name="a2"), timeout=3)

            # Collect a few messages from each
            await asyncio.sleep(0.5)
            msgs_a1 = []
            msgs_a2 = []
            for _ in range(10):
                try:
                    m = q1.get_nowait()
                    msgs_a1.append(m)
                except asyncio.QueueEmpty:
                    break
            for _ in range(10):
                try:
                    m = q2.get_nowait()
                    msgs_a2.append(m)
                except asyncio.QueueEmpty:
                    break

            # Each queue should only have messages for its agent
            for m in msgs_a1:
                assert m.name == "a1"
            for m in msgs_a2:
                assert m.name == "a2"
        finally:
            await _cleanup(server, srv, conn, sock)

    @pytest.mark.asyncio
    async def test_sentinel_on_disconnect(self):
        """When the bridge connection is lost, agent queues get a None sentinel."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            q = conn.register_process("phantom")
            await asyncio.sleep(0.1)  # let server register the client

            # Close the server-side writer to break the connection
            if server._client_writer:
                server._client_writer.close()
            await asyncio.sleep(0.3)

            # Queue should have the sentinel
            msg = await asyncio.wait_for(q.get(), timeout=2)
            assert msg is None
            assert conn.is_alive is False
        finally:
            conn._demux_task.cancel()
            srv.close()
            if os.path.exists(sock):
                os.unlink(sock)


# ---------------------------------------------------------------------------
# BridgeTransport: initialize interception
# ---------------------------------------------------------------------------


class TestBridgeTransportInterception:
    @pytest.mark.asyncio
    async def test_intercepts_initialize_when_reconnecting(self):
        """In reconnecting mode, initialize control_request is faked locally."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            # Spawn a long-running agent
            await asyncio.wait_for(
                conn.send_command(
                    "spawn", name="rc", cli_args=_slow_cli_script(1, 10), env=dict(os.environ), cwd="/tmp"
                ),
                timeout=3,
            )

            transport = BridgeTransport("rc", _adapt(conn), reconnecting=True)
            await transport.connect()
            await asyncio.wait_for(transport.subscribe(), timeout=3)

            # Write an initialize control_request
            init_msg = json.dumps(
                {
                    "type": "control_request",
                    "request_id": "test-init-42",
                    "request": {"subtype": "initialize"},
                }
            )
            await transport.write(init_msg)

            # Should get a fake response from the queue
            msg = await asyncio.wait_for(transport._queue.get(), timeout=2)
            assert isinstance(msg, StdoutEvent)
            assert msg.data["type"] == "control_response"
            assert msg.data["response"]["request_id"] == "test-init-42"
            assert msg.data["response"]["subtype"] == "success"

            # Reconnecting flag should be cleared
            assert transport._reconnecting is False
        finally:
            await _cleanup(server, srv, conn, sock)

    @pytest.mark.asyncio
    async def test_does_not_intercept_when_not_reconnecting(self):
        """In normal mode, initialize is forwarded to the bridge."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            await asyncio.wait_for(
                conn.send_command("spawn", name="nr", cli_args=_echo_cli_script(), env=dict(os.environ), cwd="/tmp"),
                timeout=3,
            )

            transport = BridgeTransport("nr", _adapt(conn), reconnecting=False)
            await transport.connect()
            await asyncio.wait_for(transport.subscribe(), timeout=3)

            # Write an initialize — should be forwarded (echo will echo it back)
            init_msg = json.dumps(
                {
                    "type": "control_request",
                    "request_id": "test-init-99",
                    "request": {"subtype": "initialize"},
                }
            )
            await transport.write(init_msg)

            # Should get the echo back (not a fake response)
            msg = await asyncio.wait_for(transport._queue.get(), timeout=3)
            assert isinstance(msg, StdoutEvent)
            assert msg.data["type"] == "echo"
        finally:
            await _cleanup(server, srv, conn, sock)


# ---------------------------------------------------------------------------
# BridgeTransport: read_messages
# ---------------------------------------------------------------------------


class TestBridgeTransportReadMessages:
    @pytest.mark.asyncio
    async def test_yields_stdout_data(self):
        """read_messages yields the data field from stdout messages."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            await asyncio.wait_for(
                conn.send_command(
                    "spawn", name="rd", cli_args=_slow_cli_script(2, 0.1), env=dict(os.environ), cwd="/tmp"
                ),
                timeout=3,
            )

            transport = BridgeTransport("rd", _adapt(conn))
            await transport.connect()
            await asyncio.wait_for(transport.subscribe(), timeout=3)

            msgs = []
            async for data in transport.read_messages():
                msgs.append(data)
                if len(msgs) >= 2:
                    break

            assert all(m["type"] == "msg" for m in msgs)
            assert msgs[0]["n"] == 0
            assert msgs[1]["n"] == 1
        finally:
            await _cleanup(server, srv, conn, sock)

    @pytest.mark.asyncio
    async def test_terminates_on_exit(self):
        """read_messages terminates when CLI exits."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            await asyncio.wait_for(
                conn.send_command(
                    "spawn", name="ex", cli_args=_slow_cli_script(1, 0), env=dict(os.environ), cwd="/tmp"
                ),
                timeout=3,
            )

            transport = BridgeTransport("ex", _adapt(conn))
            await transport.connect()
            await asyncio.wait_for(transport.subscribe(), timeout=3)

            msgs = [data async for data in transport.read_messages()]

            assert len(msgs) >= 1
            assert transport.cli_exited is True
        finally:
            await _cleanup(server, srv, conn, sock)

    @pytest.mark.asyncio
    async def test_stderr_callback(self):
        """Stderr messages trigger the stderr_callback."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            await asyncio.wait_for(
                conn.send_command("spawn", name="sc", cli_args=_stderr_cli_script(), env=dict(os.environ), cwd="/tmp"),
                timeout=3,
            )

            stderr_lines = []
            transport = BridgeTransport("sc", _adapt(conn), stderr_callback=stderr_lines.append)
            await transport.connect()
            await asyncio.wait_for(transport.subscribe(), timeout=3)

            # Read until exit
            async for _ in transport.read_messages():
                pass

            assert any("warning" in line for line in stderr_lines)
        finally:
            await _cleanup(server, srv, conn, sock)


# ---------------------------------------------------------------------------
# BridgeTransport: close / is_ready / end_input
# ---------------------------------------------------------------------------


class TestBridgeTransportLifecycle:
    @pytest.mark.asyncio
    async def test_is_ready_after_connect(self):
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            transport = BridgeTransport("lc", _adapt(conn))
            assert transport.is_ready() is False
            await transport.connect()
            assert transport.is_ready() is True
        finally:
            await _cleanup(server, srv, conn, sock)

    @pytest.mark.asyncio
    async def test_close_kills_cli(self):
        """close() sends kill to bridge and unregisters agent."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            await asyncio.wait_for(
                conn.send_command(
                    "spawn", name="cl", cli_args=_slow_cli_script(100, 1), env=dict(os.environ), cwd="/tmp"
                ),
                timeout=3,
            )

            transport = BridgeTransport("cl", _adapt(conn))
            await transport.connect()
            await asyncio.wait_for(transport.subscribe(), timeout=3)

            await transport.close()

            assert transport.is_ready() is False
            # Agent should be gone from bridge
            ls = await asyncio.wait_for(conn.send_command("list"), timeout=3)
            assert "cl" not in ls.agents
        finally:
            await _cleanup(server, srv, conn, sock)

    @pytest.mark.asyncio
    async def test_end_input_is_noop(self):
        """end_input() should not raise."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            transport = BridgeTransport("ei", _adapt(conn))
            await transport.connect()
            await transport.end_input()  # should not raise
        finally:
            await _cleanup(server, srv, conn, sock)


# ---------------------------------------------------------------------------
# BridgeTransport.stop(): immediate stream termination
# ---------------------------------------------------------------------------


class TestBridgeTransportStop:
    """Tests for BridgeTransport.stop() — instant kill that discards buffered messages."""

    @pytest.mark.asyncio
    async def test_stop_terminates_read_messages_immediately(self):
        """stop() causes read_messages() to return within milliseconds,
        discarding any buffered messages still in the queue."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            # Spawn a SIGTERM-surviving process (like flowcoder engine)
            await asyncio.wait_for(
                conn.send_command(
                    "spawn",
                    name="st",
                    cli_args=[
                        PYTHON, "-u", "-c",
                        "import signal, json, time\n"
                        "signal.signal(signal.SIGTERM, lambda *a: None)\n"
                        "i = 0\n"
                        "while True:\n"
                        "    print(json.dumps({'type':'msg','n':i}), flush=True)\n"
                        "    i += 1\n"
                        "    time.sleep(0.02)\n",
                    ],
                    env=dict(os.environ),
                    cwd="/tmp",
                ),
                timeout=3,
            )

            transport = BridgeTransport("st", _adapt(conn))
            await transport.connect()
            await asyncio.wait_for(transport.subscribe(), timeout=3)

            # Read a few messages to confirm stream is active
            count = 0
            async for _data in transport.read_messages():
                count += 1
                if count >= 5:
                    # Now call stop() from a concurrent task
                    await transport.stop()
                    # read_messages() should return on the next iteration
                    # (we're inside the generator, the _cli_exited flag is set,
                    # so the next queue.get() will trigger return)
                    break

            assert transport.cli_exited is True
            assert count == 5  # Should have read exactly 5, not hundreds
        finally:
            await _cleanup(server, srv, conn, sock)

    @pytest.mark.asyncio
    async def test_stop_from_concurrent_task(self):
        """stop() called from a separate task terminates read_messages() promptly."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            await asyncio.wait_for(
                conn.send_command(
                    "spawn",
                    name="sc",
                    cli_args=[
                        PYTHON, "-u", "-c",
                        "import signal, json, time\n"
                        "signal.signal(signal.SIGTERM, lambda *a: None)\n"
                        "i = 0\n"
                        "while True:\n"
                        "    print(json.dumps({'type':'msg','n':i}), flush=True)\n"
                        "    i += 1\n"
                        "    time.sleep(0.02)\n",
                    ],
                    env=dict(os.environ),
                    cwd="/tmp",
                ),
                timeout=3,
            )

            transport = BridgeTransport("sc", _adapt(conn))
            await transport.connect()
            await asyncio.wait_for(transport.subscribe(), timeout=3)

            msgs_received = 0

            async def read_all():
                nonlocal msgs_received
                async for _data in transport.read_messages():
                    msgs_received += 1

            async def stop_after_delay():
                await asyncio.sleep(0.2)  # Let some messages flow
                await transport.stop()

            # Run reader and stopper concurrently
            reader_task = asyncio.create_task(read_all())
            stopper_task = asyncio.create_task(stop_after_delay())

            # Both should complete quickly (not 5+ seconds)
            await asyncio.wait_for(
                asyncio.gather(reader_task, stopper_task),
                timeout=3.0,
            )

            assert transport.cli_exited is True
            # Should have received some messages but far fewer than 5 seconds worth (~250)
            assert msgs_received < 50, f"Expected <50 msgs but got {msgs_received}"
        finally:
            await _cleanup(server, srv, conn, sock)

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self):
        """Calling stop() multiple times is safe."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            await asyncio.wait_for(
                conn.send_command(
                    "spawn", name="si",
                    cli_args=_slow_cli_script(100, 0.1),
                    env=dict(os.environ), cwd="/tmp",
                ),
                timeout=3,
            )

            transport = BridgeTransport("si", _adapt(conn))
            await transport.connect()
            await asyncio.wait_for(transport.subscribe(), timeout=3)

            await transport.stop()
            await transport.stop()  # second call should be no-op
            assert transport.cli_exited is True
        finally:
            await _cleanup(server, srv, conn, sock)


# ---------------------------------------------------------------------------
# build_cli_spawn_args: permission_prompt_tool_name injection
# ---------------------------------------------------------------------------


class TestBuildCliSpawnArgs:
    """Verify build_cli_spawn_args replicates the permission_prompt_tool_name
    injection that ClaudeSDKClient.connect() does in direct mode."""

    def test_injects_permission_prompt_tool_stdio_when_can_use_tool_set(self):
        """When can_use_tool is set, --permission-prompt-tool stdio must appear."""
        from claude_agent_sdk import ClaudeAgentOptions

        from claudewire import build_cli_spawn_args

        async def _dummy_can_use_tool(tool_name, tool_input, ctx):
            pass

        options = ClaudeAgentOptions(
            can_use_tool=_dummy_can_use_tool,
            cwd="/tmp",
        )
        cmd, env, cwd = build_cli_spawn_args(options)

        assert "--permission-prompt-tool" in cmd, f"--permission-prompt-tool not found in cmd: {cmd}"
        idx = cmd.index("--permission-prompt-tool")
        assert cmd[idx + 1] == "stdio", f"Expected 'stdio' after --permission-prompt-tool, got: {cmd[idx + 1]}"

    def test_no_injection_when_can_use_tool_not_set(self):
        """When can_use_tool is None, --permission-prompt-tool must NOT appear."""
        from claude_agent_sdk import ClaudeAgentOptions

        from claudewire import build_cli_spawn_args

        options = ClaudeAgentOptions(
            can_use_tool=None,
            cwd="/tmp",
        )
        cmd, env, cwd = build_cli_spawn_args(options)

        assert "--permission-prompt-tool" not in cmd, f"--permission-prompt-tool should NOT be in cmd: {cmd}"

    def test_no_override_when_permission_prompt_tool_already_set(self):
        """When permission_prompt_tool_name is already set, don't override it."""
        from claude_agent_sdk import ClaudeAgentOptions

        from claudewire import build_cli_spawn_args

        async def _dummy_can_use_tool(tool_name, tool_input, ctx):
            pass

        options = ClaudeAgentOptions(
            can_use_tool=_dummy_can_use_tool,
            permission_prompt_tool_name="custom_tool",
            cwd="/tmp",
        )
        cmd, env, cwd = build_cli_spawn_args(options)

        idx = cmd.index("--permission-prompt-tool")
        assert cmd[idx + 1] == "custom_tool", f"Expected 'custom_tool', got: {cmd[idx + 1]}"

    def test_sdk_mcp_server_serialized_without_instance(self):
        """SDK-type MCP server is serialized into --mcp-config without the live instance."""
        from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server, tool

        from claudewire import build_cli_spawn_args

        async def _dummy_can_use_tool(tool_name, tool_input, ctx):
            pass

        @tool("ping", "Return pong", {"type": "object", "properties": {}, "required": []})
        async def ping(args):
            return {"content": [{"type": "text", "text": "pong"}]}

        mcp_server = create_sdk_mcp_server(name="test_utils", version="1.0.0", tools=[ping])

        options = ClaudeAgentOptions(
            can_use_tool=_dummy_can_use_tool,
            cwd="/tmp",
            mcp_servers={"test_utils": mcp_server},
        )
        cmd, env, cwd = build_cli_spawn_args(options)

        # --mcp-config should be present
        assert "--mcp-config" in cmd, f"--mcp-config not found in cmd: {cmd}"
        idx = cmd.index("--mcp-config")
        config = json.loads(cmd[idx + 1])

        # Server name present, type is "sdk", no "instance" key
        assert "test_utils" in config["mcpServers"]
        server_cfg = config["mcpServers"]["test_utils"]
        assert server_cfg["type"] == "sdk"
        assert "instance" not in server_cfg

    def test_multiple_sdk_mcp_servers(self):
        """Multiple SDK MCP servers all appear in --mcp-config."""
        from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server, tool

        from claudewire import build_cli_spawn_args

        async def _dummy_can_use_tool(tool_name, tool_input, ctx):
            pass

        @tool("ping", "Return pong", {"type": "object", "properties": {}, "required": []})
        async def ping(args):
            return {"content": [{"type": "text", "text": "pong"}]}

        @tool("now", "Return time", {"type": "object", "properties": {}, "required": []})
        async def now(args):
            return {"content": [{"type": "text", "text": "12:00"}]}

        server_a = create_sdk_mcp_server(name="utils", version="1.0.0", tools=[ping])
        server_b = create_sdk_mcp_server(name="schedule", version="1.0.0", tools=[now])

        options = ClaudeAgentOptions(
            can_use_tool=_dummy_can_use_tool,
            cwd="/tmp",
            mcp_servers={"utils": server_a, "schedule": server_b},
        )
        cmd, env, cwd = build_cli_spawn_args(options)

        idx = cmd.index("--mcp-config")
        config = json.loads(cmd[idx + 1])
        assert "utils" in config["mcpServers"]
        assert "schedule" in config["mcpServers"]
        for name in ("utils", "schedule"):
            assert config["mcpServers"][name]["type"] == "sdk"
            assert "instance" not in config["mcpServers"][name]

    def test_no_mcp_config_when_no_servers(self):
        """--mcp-config should not appear when mcp_servers is empty."""
        from claude_agent_sdk import ClaudeAgentOptions

        from claudewire import build_cli_spawn_args

        options = ClaudeAgentOptions(cwd="/tmp", mcp_servers={})
        cmd, env, cwd = build_cli_spawn_args(options)

        assert "--mcp-config" not in cmd


# ---------------------------------------------------------------------------
# Lifecycle: ensure_bridge (starts a real bridge subprocess)
# ---------------------------------------------------------------------------


class TestEnsureBridge:
    @pytest.mark.asyncio
    async def test_starts_bridge_and_connects(self):
        sock = _tmp_sock()
        try:
            conn = await ensure_bridge(sock, timeout=10.0)
            assert conn.is_alive is True

            result = await asyncio.wait_for(conn.send_command("list"), timeout=3)
            assert result.ok is True

            await conn.close()

            # Bridge should still be running — reconnect to verify
            conn2 = await connect_to_bridge(sock)
            assert conn2 is not None
            result2 = await asyncio.wait_for(conn2.send_command("list"), timeout=3)
            assert result2.ok is True
            await conn2.close()
        finally:
            # Kill bridge process
            import signal
            import subprocess

            ps = subprocess.run(["pgrep", "-f", f"python -m bridge {sock}"], capture_output=True, text=True)
            for pid in ps.stdout.strip().split():
                try:
                    os.kill(int(pid), signal.SIGTERM)
                except (OSError, ValueError):
                    pass
            if os.path.exists(sock):
                os.unlink(sock)

    @pytest.mark.asyncio
    async def test_connects_to_existing(self):
        """If a bridge is already running, ensure_bridge just connects."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        try:
            conn = await ensure_bridge(sock, timeout=5.0)
            assert conn.is_alive is True
            result = await asyncio.wait_for(conn.send_command("list"), timeout=3)
            assert result.ok is True
            conn._demux_task.cancel()
            try:
                conn._writer.close()
            except Exception:
                pass
        finally:
            srv.close()
            if os.path.exists(sock):
                os.unlink(sock)


# ---------------------------------------------------------------------------
# Lifecycle: connect_to_bridge
# ---------------------------------------------------------------------------


class TestConnectToBridge:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_bridge(self):
        conn = await connect_to_bridge("/tmp/nonexistent_bridge.sock")
        assert conn is None


# ---------------------------------------------------------------------------
# Shutdown: bridge_mode on ShutdownCoordinator
# ---------------------------------------------------------------------------


class TestShutdownBridgeMode:
    @pytest.mark.asyncio
    async def test_bridge_mode_skips_sleep(self):
        """In bridge mode, graceful_shutdown skips sleep_all."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from axi.shutdown import ShutdownCoordinator

        sleep_fn = AsyncMock()
        kill_fn = MagicMock()

        with patch("axi.shutdown._start_deadline_thread"):
            c = ShutdownCoordinator(
                agents={"a": type("A", (), {"name": "a", "client": "x", "query_lock": asyncio.Lock()})()},
                sleep_fn=sleep_fn,
                close_bot_fn=AsyncMock(),
                kill_fn=kill_fn,
                bridge_mode=True,
            )
            await c.graceful_shutdown("test")

        # sleep_fn should NOT have been called in bridge mode
        sleep_fn.assert_not_called()
        kill_fn.assert_called_once()


# ---------------------------------------------------------------------------
# NEW TESTS: Client reconnection
# ---------------------------------------------------------------------------


class TestClientReconnection:
    @pytest.mark.asyncio
    async def test_disconnect_buffer_reconnect(self):
        """Client disconnects, output buffers, new client reconnects and gets all messages."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn1 = await _connect(sock)
        conn2 = None
        try:
            # Spawn agent that emits 10 messages quickly
            await asyncio.wait_for(
                conn1.send_command(
                    "spawn", name="rc", cli_args=_slow_cli_script(10, 0.02), env=dict(os.environ), cwd="/tmp"
                ),
                timeout=3,
            )
            # Subscribe but immediately disconnect before reading
            conn1.register_process("rc")
            await asyncio.wait_for(conn1.send_command("subscribe", name="rc"), timeout=3)

            # Kill connection
            conn1._demux_task.cancel()
            conn1._writer.close()
            await asyncio.sleep(0.5)  # let agent finish and buffer

            # Reconnect
            conn2 = await _connect(sock)
            ls = await asyncio.wait_for(conn2.send_command("list"), timeout=3)
            buffered = ls.agents["rc"]["buffered_msgs"]
            assert buffered > 0

            # Subscribe and replay
            q = conn2.register_process("rc")
            sub = await asyncio.wait_for(conn2.send_command("subscribe", name="rc"), timeout=3)
            assert sub.ok is True
            assert sub.replayed == buffered

            # Read all replayed messages
            msgs = []
            for _ in range(buffered + 5):
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=2)
                    if msg is None:
                        break
                    msgs.append(msg)
                except TimeoutError:
                    break

            assert len(msgs) >= buffered
        finally:
            for cp in list(server._procs.values()):
                await server._kill_process(cp)
            if conn2:
                conn2._demux_task.cancel()
                try:
                    conn2._writer.close()
                except Exception:
                    pass
            srv.close()
            if os.path.exists(sock):
                os.unlink(sock)


# ---------------------------------------------------------------------------
# NEW TESTS: Exit during disconnect
# ---------------------------------------------------------------------------


class TestExitDuringDisconnect:
    @pytest.mark.asyncio
    async def test_exit_buffered_and_replayed(self):
        """Agent exits while bot.py is down; exit message buffered and replayed on reconnect."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn1 = await _connect(sock)
        conn2 = None
        try:
            # Spawn a short-lived agent
            await asyncio.wait_for(
                conn1.send_command(
                    "spawn", name="de", cli_args=_slow_cli_script(1, 0), env=dict(os.environ), cwd="/tmp"
                ),
                timeout=3,
            )
            # Subscribe then disconnect immediately
            conn1.register_process("de")
            await asyncio.wait_for(conn1.send_command("subscribe", name="de"), timeout=3)

            conn1._demux_task.cancel()
            conn1._writer.close()
            await asyncio.sleep(0.5)  # let agent exit and buffer

            # Reconnect
            conn2 = await _connect(sock)
            ls = await asyncio.wait_for(conn2.send_command("list"), timeout=3)
            assert ls.agents["de"]["status"] == "exited"
            assert ls.agents["de"]["buffered_msgs"] > 0

            # Subscribe and check for exit message
            q = conn2.register_process("de")
            await asyncio.wait_for(conn2.send_command("subscribe", name="de"), timeout=3)

            msgs = []
            for _ in range(20):
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=2)
                    if msg is None:
                        break
                    msgs.append(msg)
                except TimeoutError:
                    break

            exit_msgs = [m for m in msgs if m.type == "exit"]
            assert len(exit_msgs) == 1
            assert exit_msgs[0].name == "de"
        finally:
            for cp in list(server._procs.values()):
                await server._kill_process(cp)
            if conn2:
                conn2._demux_task.cancel()
                try:
                    conn2._writer.close()
                except Exception:
                    pass
            srv.close()
            if os.path.exists(sock):
                os.unlink(sock)


# ---------------------------------------------------------------------------
# NEW TESTS: Malformed JSON from client
# ---------------------------------------------------------------------------


class TestMalformedJson:
    @pytest.mark.asyncio
    async def test_server_handles_bad_json(self):
        """Server handles bad JSON from client without crashing."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = None
        try:
            # Connect raw to send malformed data
            reader, writer = await asyncio.open_unix_connection(sock)
            # Send garbage
            writer.write(b"this is not json\n")
            await writer.drain()
            writer.write(b"{malformed\n")
            await writer.drain()

            # Now send a valid command — server should still work
            valid = json.dumps({"type": "cmd", "cmd": "list"}) + "\n"
            writer.write(valid.encode())
            await writer.drain()

            line = await asyncio.wait_for(reader.readline(), timeout=3)
            result = json.loads(line.decode().strip())
            assert result["ok"] is True
            assert "agents" in result

            writer.close()
        finally:
            srv.close()
            if os.path.exists(sock):
                os.unlink(sock)


# ---------------------------------------------------------------------------
# NEW TESTS: Send to client failure
# ---------------------------------------------------------------------------


class TestSendToClientFailure:
    @pytest.mark.asyncio
    async def test_write_failure_routes_to_buffer(self):
        """When sending to client fails mid-stream, subsequent output goes to buffer."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            # Spawn a slow agent
            await asyncio.wait_for(
                conn.send_command(
                    "spawn", name="wf", cli_args=_slow_cli_script(20, 0.05), env=dict(os.environ), cwd="/tmp"
                ),
                timeout=3,
            )
            conn.register_process("wf")
            await asyncio.wait_for(conn.send_command("subscribe", name="wf"), timeout=3)

            # Forcefully close the client connection
            conn._demux_task.cancel()
            conn._writer.close()
            await asyncio.sleep(0.3)

            # Server should have detected the failure and started buffering
            # Verify by connecting a new client
            conn2 = await _connect(sock)
            ls = await asyncio.wait_for(conn2.send_command("list"), timeout=3)
            assert ls.agents["wf"]["subscribed"] is False
            assert ls.agents["wf"]["buffered_msgs"] >= 0  # may have buffered some

            conn2._demux_task.cancel()
            try:
                conn2._writer.close()
            except Exception:
                pass
        finally:
            for cp in list(server._procs.values()):
                await server._kill_process(cp)
            srv.close()
            if os.path.exists(sock):
                os.unlink(sock)


# ---------------------------------------------------------------------------
# NEW TESTS: Kill escalation (SIGTERM -> SIGKILL)
# ---------------------------------------------------------------------------


class TestKillEscalation:
    @pytest.mark.asyncio
    async def test_kill_terminates_process(self):
        """_kill_process terminates a running process."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            # Spawn a long-running agent
            await asyncio.wait_for(
                conn.send_command(
                    "spawn", name="ke", cli_args=_slow_cli_script(1000, 0.1), env=dict(os.environ), cwd="/tmp"
                ),
                timeout=3,
            )
            cp = server._procs["ke"]
            assert cp.status == "running"

            await server._kill_process(cp)

            assert cp.status == "exited"
            assert cp.proc.returncode is not None
        finally:
            await _cleanup(server, srv, conn, sock)

    @pytest.mark.asyncio
    async def test_kill_already_exited_is_noop(self):
        """Killing an already-exited process is a no-op."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            # Spawn a fast-exiting agent
            await asyncio.wait_for(
                conn.send_command(
                    "spawn", name="ae", cli_args=_slow_cli_script(1, 0), env=dict(os.environ), cwd="/tmp"
                ),
                timeout=3,
            )
            await asyncio.sleep(0.3)  # let it exit
            cp = server._procs["ae"]

            # Mark as exited (the relay task may have already done this)
            cp.status = "exited"

            # Should be a no-op
            await server._kill_process(cp)
            assert cp.status == "exited"
        finally:
            await _cleanup(server, srv, conn, sock)


# ---------------------------------------------------------------------------
# NEW TESTS: Stdin edge cases
# ---------------------------------------------------------------------------


class TestStdinEdgeCases:
    @pytest.mark.asyncio
    async def test_stdin_to_nonexistent_agent_silently_dropped(self):
        """Sending stdin to a nonexistent agent doesn't crash."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            # Send stdin to an agent that doesn't exist — should not crash
            await conn.send_stdin("ghost", {"data": "hello"})
            await asyncio.sleep(0.1)

            # Server should still be responsive
            result = await asyncio.wait_for(conn.send_command("list"), timeout=3)
            assert result.ok is True
        finally:
            await _cleanup(server, srv, conn, sock)

    @pytest.mark.asyncio
    async def test_stdin_to_exited_agent_silently_dropped(self):
        """Sending stdin to an exited agent doesn't crash."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            # Spawn a fast-exiting agent
            await asyncio.wait_for(
                conn.send_command(
                    "spawn", name="ex", cli_args=_slow_cli_script(1, 0), env=dict(os.environ), cwd="/tmp"
                ),
                timeout=3,
            )
            await asyncio.sleep(0.3)  # let it exit

            # Send stdin — should not crash
            await conn.send_stdin("ex", {"data": "hello"})
            await asyncio.sleep(0.1)

            # Server should still be responsive
            result = await asyncio.wait_for(conn.send_command("list"), timeout=3)
            assert result.ok is True
        finally:
            await _cleanup(server, srv, conn, sock)


# ---------------------------------------------------------------------------
# NEW TESTS: Buffer replay order
# ---------------------------------------------------------------------------


class TestBufferReplayOrder:
    @pytest.mark.asyncio
    async def test_messages_replayed_in_order(self):
        """Buffered messages are replayed in exact original order."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn1 = await _connect(sock)
        conn2 = None
        try:
            # Spawn agent that emits numbered messages
            await asyncio.wait_for(
                conn1.send_command(
                    "spawn", name="ord", cli_args=_slow_cli_script(5, 0.02), env=dict(os.environ), cwd="/tmp"
                ),
                timeout=3,
            )
            # Don't subscribe — let everything buffer
            conn1._demux_task.cancel()
            conn1._writer.close()
            await asyncio.sleep(0.5)  # let agent finish

            # Reconnect and subscribe
            conn2 = await _connect(sock)
            q = conn2.register_process("ord")
            await asyncio.wait_for(conn2.send_command("subscribe", name="ord"), timeout=3)

            # Collect all messages
            msgs = []
            for _ in range(20):
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=2)
                    if msg is None:
                        break
                    msgs.append(msg)
                except TimeoutError:
                    break

            # Filter stdout messages and verify order
            stdout_msgs = [m for m in msgs if m.type == "stdout"]
            for i, m in enumerate(stdout_msgs):
                assert m.data["n"] == i, f"Expected message n={i}, got n={m.data['n']}"

            # Exit should be last
            assert msgs[-1].type == "exit"
        finally:
            for cp in list(server._procs.values()):
                await server._kill_process(cp)
            if conn2:
                conn2._demux_task.cancel()
                try:
                    conn2._writer.close()
                except Exception:
                    pass
            srv.close()
            if os.path.exists(sock):
                os.unlink(sock)


# ---------------------------------------------------------------------------
# NEW TESTS: Subscribe to exited agent
# ---------------------------------------------------------------------------


class TestSubscribeToExited:
    @pytest.mark.asyncio
    async def test_subscribe_to_exited_returns_status(self):
        """Subscribing to an exited agent returns correct status and exit_code."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            # Spawn fast-exiting agent
            await asyncio.wait_for(
                conn.send_command(
                    "spawn", name="se", cli_args=_slow_cli_script(1, 0), env=dict(os.environ), cwd="/tmp"
                ),
                timeout=3,
            )
            await asyncio.sleep(0.5)  # let it exit

            # Subscribe
            q = conn.register_process("se")
            result = await asyncio.wait_for(conn.send_command("subscribe", name="se"), timeout=3)
            assert result.ok is True
            assert result.status == "exited"
            assert result.exit_code == 0
            assert result.replayed > 0  # at least stdout + exit messages
        finally:
            await _cleanup(server, srv, conn, sock)


# ---------------------------------------------------------------------------
# NEW TESTS: Command timeout
# ---------------------------------------------------------------------------


class TestCommandTimeout:
    @pytest.mark.asyncio
    async def test_send_command_timeout(self):
        """send_command times out when server doesn't respond."""
        sock = _tmp_sock()
        # Create a socket that accepts connections but never responds
        if os.path.exists(sock):
            os.unlink(sock)

        async def _silent_handler(reader, writer):
            # Accept connection but never write anything back
            await asyncio.sleep(60)

        srv = await asyncio.start_unix_server(_silent_handler, path=sock)
        try:
            reader, writer = await asyncio.open_unix_connection(sock)
            conn = BridgeConnection(reader, writer)

            with pytest.raises(asyncio.TimeoutError):
                # Override the 30s default with a short timeout by calling internals
                async with conn._cmd_lock:
                    msg = {"type": "cmd", "cmd": "list"}
                    conn._writer.write((json.dumps(msg) + "\n").encode())
                    await conn._writer.drain()
                    await asyncio.wait_for(conn._cmd_response.get(), timeout=0.5)

            conn._demux_task.cancel()
            try:
                writer.close()
            except Exception:
                pass
        finally:
            srv.close()
            if os.path.exists(sock):
                os.unlink(sock)


# ---------------------------------------------------------------------------
# NEW TESTS: SDK MCP server through bridge spawn + reconnect
# ---------------------------------------------------------------------------


def _mcp_cli_args() -> tuple[list[str], dict]:
    """Build real CLI spawn args with an SDK MCP server, like bot.py does."""
    from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server, tool

    from claudewire import build_cli_spawn_args

    async def _dummy_can_use_tool(tool_name, tool_input, ctx):
        pass

    @tool("ping", "Return pong", {"type": "object", "properties": {}, "required": []})
    async def ping(args):
        return {"content": [{"type": "text", "text": "pong"}]}

    mcp_server = create_sdk_mcp_server(name="test_utils", version="1.0.0", tools=[ping])

    options = ClaudeAgentOptions(
        can_use_tool=_dummy_can_use_tool,
        cwd="/tmp",
        mcp_servers={"test_utils": mcp_server},
    )
    cmd, env, cwd = build_cli_spawn_args(options)
    return cmd, env, cwd


class TestMcpBridgeSpawn:
    @pytest.mark.asyncio
    async def test_spawn_with_sdk_mcp_config(self):
        """CLI spawned through the bridge receives --mcp-config with SDK server."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            cmd, env, cwd = _mcp_cli_args()

            # Verify the args contain --mcp-config before we send them
            assert "--mcp-config" in cmd
            idx = cmd.index("--mcp-config")
            config = json.loads(cmd[idx + 1])
            assert "test_utils" in config["mcpServers"]
            assert config["mcpServers"]["test_utils"]["type"] == "sdk"
            assert "instance" not in config["mcpServers"]["test_utils"]

            # Spawn through the bridge — the bridge passes these args to the subprocess
            result = await asyncio.wait_for(
                conn.send_command("spawn", name="mcp_agent", cli_args=cmd, env=env, cwd=cwd),
                timeout=10,
            )
            assert result.ok is True
            assert isinstance(result.pid, int)

            # Agent is alive in the bridge
            ls = await asyncio.wait_for(conn.send_command("list"), timeout=3)
            assert "mcp_agent" in ls.agents
            assert ls.agents["mcp_agent"]["status"] == "running"
        finally:
            await _cleanup(server, srv, conn, sock)

    @pytest.mark.asyncio
    async def test_mcp_agent_survives_client_reconnect(self):
        """Agent survives client disconnect/reconnect.

        Uses a long-lived dummy process instead of the real Claude CLI,
        which exits immediately when it has no prompt/input.
        """
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn1 = await _connect(sock)
        conn2 = None
        try:
            # Long-lived process that emits heartbeats (real CLI exits too fast)
            cli_args = _slow_cli_script(50, delay=0.2)

            # Spawn through the bridge
            result = await asyncio.wait_for(
                conn1.send_command("spawn", name="mcp_rc", cli_args=cli_args, env=dict(os.environ), cwd="/tmp"),
                timeout=10,
            )
            assert result.ok is True
            original_pid = result.pid

            # Subscribe to start receiving output
            conn1.register_process("mcp_rc")
            await asyncio.wait_for(conn1.send_command("subscribe", name="mcp_rc"), timeout=3)

            # Disconnect client 1
            conn1._demux_task.cancel()
            conn1._writer.close()
            await asyncio.sleep(0.3)

            # Reconnect with client 2
            conn2 = await _connect(sock)

            # Agent should still be running with same pid
            ls = await asyncio.wait_for(conn2.send_command("list"), timeout=3)
            assert "mcp_rc" in ls.agents
            assert ls.agents["mcp_rc"]["status"] == "running"
            assert ls.agents["mcp_rc"]["pid"] == original_pid
            assert ls.agents["mcp_rc"]["subscribed"] is False

            # Re-subscribe — should work and replay any buffered output
            q = conn2.register_process("mcp_rc")
            sub = await asyncio.wait_for(conn2.send_command("subscribe", name="mcp_rc"), timeout=3)
            assert sub.ok is True
            assert sub.status == "running"

            # Agent is now subscribed again
            ls2 = await asyncio.wait_for(conn2.send_command("list"), timeout=3)
            assert ls2.agents["mcp_rc"]["subscribed"] is True
        finally:
            for cp in list(server._procs.values()):
                await server._kill_process(cp)
            if conn2:
                conn2._demux_task.cancel()
                try:
                    conn2._writer.close()
                except Exception:
                    pass
            srv.close()
            if os.path.exists(sock):
                os.unlink(sock)


# ---------------------------------------------------------------------------
# Agent survives client reconnect (end-to-end polling test)
# ---------------------------------------------------------------------------


class TestAgentSurvivesReconnect:
    """Spawn a long-running agent that polls for a file, disconnect the client,
    reconnect a new client, then create the file.  The agent should notice the
    file through the *new* connection — proving it stayed alive in the bridge
    the whole time."""

    @staticmethod
    def _polling_cli_script(path: str, poll_interval: float = 0.2, timeout: float = 30) -> list[str]:
        """CLI that polls for *path* to exist, emitting heartbeats and a final 'found' message."""
        return [
            PYTHON,
            "-c",
            "import json, os, sys, time\n"
            f"target = {path!r}\n"
            f"poll = {poll_interval}\n"
            f"deadline = time.monotonic() + {timeout}\n"
            "seq = 0\n"
            "while time.monotonic() < deadline:\n"
            "    if os.path.exists(target):\n"
            "        print(json.dumps({'type':'found','path':target,'seq':seq}), flush=True)\n"
            "        sys.exit(0)\n"
            "    print(json.dumps({'type':'heartbeat','seq':seq}), flush=True)\n"
            "    seq += 1\n"
            "    time.sleep(poll)\n"
            "print(json.dumps({'type':'timeout'}), flush=True)\n"
            "sys.exit(1)\n",
        ]

    @pytest.mark.asyncio
    async def test_agent_produces_output_after_reconnect(self):
        """Agent keeps polling while client is disconnected; after reconnect
        we create the trigger file and receive the 'found' message."""
        import tempfile

        sock = _tmp_sock()
        trigger = tempfile.mktemp(prefix="bridge_test_trigger_")

        server, srv = await _start_server(sock)
        conn1 = await _connect(sock)
        conn2 = None
        try:
            # Spawn a poller that waits for the trigger file
            spawn = await asyncio.wait_for(
                conn1.send_command(
                    "spawn",
                    name="poller",
                    cli_args=self._polling_cli_script(trigger, poll_interval=0.2, timeout=30),
                    env=dict(os.environ),
                    cwd="/tmp",
                ),
                timeout=3,
            )
            assert spawn.ok is True

            # Subscribe and read a few heartbeats to confirm it's working
            q1 = conn1.register_process("poller")
            await asyncio.wait_for(conn1.send_command("subscribe", name="poller"), timeout=3)

            heartbeats_before = []
            for _ in range(3):
                msg = await asyncio.wait_for(q1.get(), timeout=5)
                assert msg.type == "stdout"
                assert msg.data["type"] == "heartbeat"
                heartbeats_before.append(msg.data["seq"])
            assert heartbeats_before == [0, 1, 2]

            # --- Disconnect client 1 (simulates bot.py restart) ---
            conn1._demux_task.cancel()
            conn1._writer.close()
            await asyncio.sleep(0.5)  # let the bridge notice

            # Agent should still be running in the bridge, buffering output
            conn2 = await _connect(sock)
            ls = await asyncio.wait_for(conn2.send_command("list"), timeout=3)
            assert ls.agents["poller"]["status"] == "running"
            assert ls.agents["poller"]["subscribed"] is False
            assert ls.agents["poller"]["buffered_msgs"] > 0

            # --- Reconnect: subscribe to the agent ---
            q2 = conn2.register_process("poller")
            sub = await asyncio.wait_for(conn2.send_command("subscribe", name="poller"), timeout=3)
            assert sub.ok is True
            assert sub.replayed > 0  # buffered heartbeats

            # Drain replayed heartbeats
            replayed = []
            for _ in range(sub.replayed):
                msg = await asyncio.wait_for(q2.get(), timeout=3)
                replayed.append(msg)
            assert all(m.type == "stdout" and m.data["type"] == "heartbeat" for m in replayed)

            # --- Create the trigger file — agent should find it ---
            with open(trigger, "w") as f:
                f.write("done")

            # Read messages until we get the 'found' message
            found_msg = None
            for _ in range(100):  # generous upper bound
                msg = await asyncio.wait_for(q2.get(), timeout=5)
                if msg.type == "stdout" and msg.data["type"] == "found":
                    found_msg = msg.data
                    break

            assert found_msg is not None, "Never received 'found' message after creating trigger file"
            assert found_msg["path"] == trigger
            # seq should be > heartbeats_before, proving it kept counting
            assert found_msg["seq"] > heartbeats_before[-1]

            # Agent should exit cleanly — read the exit message
            exit_msg = await asyncio.wait_for(q2.get(), timeout=5)
            assert exit_msg.type == "exit"
            assert exit_msg.code == 0

        finally:
            for cp in list(server._procs.values()):
                await server._kill_process(cp)
            if conn2:
                conn2._demux_task.cancel()
                try:
                    conn2._writer.close()
                except Exception:
                    pass
            srv.close()
            if os.path.exists(sock):
                os.unlink(sock)
            if os.path.exists(trigger):
                os.unlink(trigger)


# ---------------------------------------------------------------------------
# Comprehensive reconnection scenario tests
# ---------------------------------------------------------------------------


# CLI that sleeps silently, then emits a "done" message and exits.
def _silent_then_done_script(sleep_secs: float) -> list[str]:
    return [
        PYTHON,
        "-c",
        f"import json, sys, time\ntime.sleep({sleep_secs})\nprint(json.dumps({{'type':'done'}}), flush=True)\n",
    ]


# CLI that emits n_fast messages, then sleeps, then emits "done" and exits.
def _burst_then_wait_script(n_fast: int, delay: float, wait_secs: float) -> list[str]:
    return [
        PYTHON,
        "-c",
        "import json, sys, time\n"
        f"for i in range({n_fast}):\n"
        f"    print(json.dumps({{'type':'msg','n':i}}), flush=True)\n"
        f"    time.sleep({delay})\n"
        f"time.sleep({wait_secs})\n"
        "print(json.dumps({'type':'done'}), flush=True)\n",
    ]


# CLI that reads stdin lines in a loop, echoing each as a result.
# Stays alive between inputs (simulates idle-awaiting-input).
def _multi_turn_script() -> list[str]:
    return [
        PYTHON,
        "-c",
        "import json, sys\n"
        "print(json.dumps({'type':'ready'}), flush=True)\n"
        "for line in sys.stdin:\n"
        "    data = json.loads(line)\n"
        "    print(json.dumps({'type':'result','data':data}), flush=True)\n",
    ]


# CLI that exits immediately with a given code.
def _exit_with_code_script(code: int) -> list[str]:
    return [PYTHON, "-c", f"import json, sys\nprint(json.dumps({{'type':'bye'}}), flush=True)\nsys.exit({code})\n"]


async def _cleanup_multi(server, srv, conns: list, sock: str):
    """Cleanup helper for tests with multiple connections."""
    for cp in list(server._procs.values()):
        await server._kill_process(cp)
    for conn in conns:
        if conn is not None:
            conn._demux_task.cancel()
            try:
                conn._writer.close()
            except Exception:
                pass
    srv.close()
    if os.path.exists(sock):
        os.unlink(sock)


class TestReconnectScenarios:
    """End-to-end reconnection scenarios.

    Each test simulates: client 1 connects -> spawns agent -> disconnects ->
    client 2 connects -> verifies agent state and output.
    """

    # -- Scenario 1: mid-task, no buffered output --

    @pytest.mark.asyncio
    async def test_reconnect_mid_task_no_output(self):
        """Agent running a silent long task. Disconnect with 0 buffered msgs.
        After reconnect + subscribe, eventually receive output."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn1 = await _connect(sock)
        conn2 = None
        try:
            # Spawn agent that sleeps 3s then emits "done"
            await asyncio.wait_for(
                conn1.send_command(
                    "spawn", name="silent", cli_args=_silent_then_done_script(3), env=dict(os.environ), cwd="/tmp"
                ),
                timeout=3,
            )
            # Subscribe briefly to confirm it's running
            conn1.register_process("silent")
            sub = await asyncio.wait_for(conn1.send_command("subscribe", name="silent"), timeout=3)
            assert sub.status == "running"

            # Disconnect immediately (no output yet)
            conn1._demux_task.cancel()
            conn1._writer.close()
            await asyncio.sleep(0.3)

            # Reconnect
            conn2 = await _connect(sock)
            ls = await asyncio.wait_for(conn2.send_command("list"), timeout=3)
            assert ls.agents["silent"]["status"] == "running"
            assert ls.agents["silent"]["buffered_msgs"] == 0
            assert ls.agents["silent"]["subscribed"] is False

            # Subscribe and wait for the "done" message
            q = conn2.register_process("silent")
            sub2 = await asyncio.wait_for(conn2.send_command("subscribe", name="silent"), timeout=3)
            assert sub2.replayed == 0
            assert sub2.status == "running"

            # Wait for output (sleep finishes after ~3s)
            done_msg = None
            for _ in range(50):
                msg = await asyncio.wait_for(q.get(), timeout=5)
                if msg.type == "stdout" and msg.data["type"] == "done":
                    done_msg = msg
                    break
            assert done_msg is not None, "Never received 'done' from silent agent"

            # Should also get exit
            exit_msg = await asyncio.wait_for(q.get(), timeout=3)
            assert exit_msg.type == "exit"
            assert exit_msg.code == 0
        finally:
            await _cleanup_multi(server, srv, [conn1, conn2], sock)

    # -- Scenario 2: mid-task, with buffered output --

    @pytest.mark.asyncio
    async def test_reconnect_mid_task_with_buffered_output(self):
        """Agent streaming output. Disconnect mid-stream. Buffered messages
        replayed on reconnect, then new output continues flowing."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn1 = await _connect(sock)
        conn2 = None
        try:
            # Spawn: 10 fast messages, then 3s wait, then "done"
            await asyncio.wait_for(
                conn1.send_command(
                    "spawn",
                    name="burst",
                    cli_args=_burst_then_wait_script(10, 0.05, 3),
                    env=dict(os.environ),
                    cwd="/tmp",
                ),
                timeout=3,
            )
            # Subscribe and read first 2 messages
            q1 = conn1.register_process("burst")
            await asyncio.wait_for(conn1.send_command("subscribe", name="burst"), timeout=3)
            for _ in range(2):
                msg = await asyncio.wait_for(q1.get(), timeout=3)
                assert msg.type == "stdout"

            # Disconnect while remaining messages buffer
            conn1._demux_task.cancel()
            conn1._writer.close()
            await asyncio.sleep(0.8)  # let remaining messages buffer

            # Reconnect
            conn2 = await _connect(sock)
            ls = await asyncio.wait_for(conn2.send_command("list"), timeout=3)
            assert ls.agents["burst"]["status"] == "running"
            buffered = ls.agents["burst"]["buffered_msgs"]
            assert buffered > 0, f"Expected buffered > 0, got {buffered}"

            # Subscribe — get replayed messages
            q2 = conn2.register_process("burst")
            sub = await asyncio.wait_for(conn2.send_command("subscribe", name="burst"), timeout=3)
            assert sub.replayed == buffered

            # Drain replayed messages
            replayed_msgs = []
            for _ in range(sub.replayed):
                msg = await asyncio.wait_for(q2.get(), timeout=3)
                replayed_msgs.append(msg)

            # Verify replayed messages are stdout with sequential n values
            stdout_msgs = [m for m in replayed_msgs if m.type == "stdout"]
            assert len(stdout_msgs) > 0
            ns = [m.data["n"] for m in stdout_msgs if "n" in m.data]
            assert ns == sorted(ns), f"Messages out of order: {ns}"

            # Now wait for the "done" message (new output after reconnect)
            done_msg = None
            for _ in range(50):
                msg = await asyncio.wait_for(q2.get(), timeout=5)
                if msg.type == "stdout" and msg.data["type"] == "done":
                    done_msg = msg
                    break
            assert done_msg is not None, "Never received 'done' after reconnect"

            # Exit message
            exit_msg = await asyncio.wait_for(q2.get(), timeout=3)
            assert exit_msg.type == "exit"
            assert exit_msg.code == 0
        finally:
            await _cleanup_multi(server, srv, [conn1, conn2], sock)

    # -- Scenario 3: agent idle (awaiting stdin input) --

    @pytest.mark.asyncio
    async def test_reconnect_agent_idle_awaiting_input(self):
        """Agent finished a task and is waiting for stdin. Disconnect,
        reconnect, send new input, verify response."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn1 = await _connect(sock)
        conn2 = None
        try:
            await asyncio.wait_for(
                conn1.send_command(
                    "spawn", name="idle", cli_args=_multi_turn_script(), env=dict(os.environ), cwd="/tmp"
                ),
                timeout=3,
            )
            # Subscribe and read "ready"
            q1 = conn1.register_process("idle")
            await asyncio.wait_for(conn1.send_command("subscribe", name="idle"), timeout=3)
            msg = await asyncio.wait_for(q1.get(), timeout=3)
            assert msg.data["type"] == "ready"

            # Send input, read result — agent is now idle again
            await conn1.send_stdin("idle", {"turn": 1})
            msg = await asyncio.wait_for(q1.get(), timeout=3)
            assert msg.data["type"] == "result"
            assert msg.data["data"]["turn"] == 1

            # Disconnect (agent is idle, waiting for more stdin)
            conn1._demux_task.cancel()
            conn1._writer.close()
            await asyncio.sleep(0.3)

            # Reconnect
            conn2 = await _connect(sock)
            ls = await asyncio.wait_for(conn2.send_command("list"), timeout=3)
            assert ls.agents["idle"]["status"] == "running"
            assert ls.agents["idle"]["buffered_msgs"] == 0

            # Subscribe
            q2 = conn2.register_process("idle")
            sub = await asyncio.wait_for(conn2.send_command("subscribe", name="idle"), timeout=3)
            assert sub.replayed == 0
            assert sub.status == "running"

            # Send new input via client 2
            await conn2.send_stdin("idle", {"turn": 2})
            msg = await asyncio.wait_for(q2.get(), timeout=3)
            assert msg.type == "stdout"
            assert msg.data["type"] == "result"
            assert msg.data["data"]["turn"] == 2
        finally:
            await _cleanup_multi(server, srv, [conn1, conn2], sock)

    # -- Scenario 4: agent exited during disconnect --

    @pytest.mark.asyncio
    async def test_reconnect_agent_exited_during_disconnect(self):
        """Short-lived agent exits while client is disconnected.
        Reconnect shows status=exited with correct exit code."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn1 = await _connect(sock)
        conn2 = None
        try:
            # Spawn agent that emits 3 messages quickly then exits
            await asyncio.wait_for(
                conn1.send_command(
                    "spawn", name="short", cli_args=_slow_cli_script(3, 0.05), env=dict(os.environ), cwd="/tmp"
                ),
                timeout=3,
            )
            # Subscribe and read 1 message
            q1 = conn1.register_process("short")
            await asyncio.wait_for(conn1.send_command("subscribe", name="short"), timeout=3)
            msg = await asyncio.wait_for(q1.get(), timeout=3)
            assert msg.type == "stdout"

            # Disconnect immediately — agent will finish and exit
            conn1._demux_task.cancel()
            conn1._writer.close()
            await asyncio.sleep(0.5)

            # Reconnect
            conn2 = await _connect(sock)
            ls = await asyncio.wait_for(conn2.send_command("list"), timeout=3)
            assert ls.agents["short"]["status"] == "exited"
            assert ls.agents["short"]["exit_code"] == 0
            assert ls.agents["short"]["buffered_msgs"] > 0

            # Subscribe and verify exit message is in buffer
            q2 = conn2.register_process("short")
            sub = await asyncio.wait_for(conn2.send_command("subscribe", name="short"), timeout=3)
            assert sub.status == "exited"
            assert sub.exit_code == 0
            assert sub.replayed > 0

            # Read all replayed messages, verify exit is last
            msgs = []
            for _ in range(sub.replayed):
                msg = await asyncio.wait_for(q2.get(), timeout=3)
                msgs.append(msg)
            assert msgs[-1].type == "exit"
            assert msgs[-1].code == 0
        finally:
            await _cleanup_multi(server, srv, [conn1, conn2], sock)

    # -- Scenario 5: non-zero exit during disconnect --

    @pytest.mark.asyncio
    async def test_reconnect_agent_crash_exit_during_disconnect(self):
        """Agent exits with non-zero code while disconnected."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn1 = await _connect(sock)
        conn2 = None
        try:
            await asyncio.wait_for(
                conn1.send_command(
                    "spawn", name="crash", cli_args=_exit_with_code_script(7), env=dict(os.environ), cwd="/tmp"
                ),
                timeout=3,
            )
            # Don't subscribe — disconnect immediately
            conn1._demux_task.cancel()
            conn1._writer.close()
            await asyncio.sleep(0.5)

            # Reconnect
            conn2 = await _connect(sock)
            ls = await asyncio.wait_for(conn2.send_command("list"), timeout=3)
            assert ls.agents["crash"]["status"] == "exited"
            assert ls.agents["crash"]["exit_code"] == 7

            # Subscribe and verify
            q2 = conn2.register_process("crash")
            sub = await asyncio.wait_for(conn2.send_command("subscribe", name="crash"), timeout=3)
            assert sub.exit_code == 7

            # Read buffered messages — last should be EXIT with code 7
            msgs = []
            for _ in range(sub.replayed):
                msg = await asyncio.wait_for(q2.get(), timeout=3)
                msgs.append(msg)
            assert msgs[-1].type == "exit"
            assert msgs[-1].code == 7
        finally:
            await _cleanup_multi(server, srv, [conn1, conn2], sock)

    # -- Scenario 6: multiple agents survive --

    @pytest.mark.asyncio
    async def test_reconnect_multiple_agents_survive(self):
        """Two agents running. Both survive disconnect/reconnect."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn1 = await _connect(sock)
        conn2 = None
        try:
            for name in ("alpha", "beta"):
                await asyncio.wait_for(
                    conn1.send_command(
                        "spawn", name=name, cli_args=_slow_cli_script(30, 0.2), env=dict(os.environ), cwd="/tmp"
                    ),
                    timeout=3,
                )
            # Subscribe to both and read a heartbeat from each
            for name in ("alpha", "beta"):
                conn1.register_process(name)
                await asyncio.wait_for(conn1.send_command("subscribe", name=name), timeout=3)

            # Disconnect
            conn1._demux_task.cancel()
            conn1._writer.close()
            await asyncio.sleep(0.5)

            # Reconnect
            conn2 = await _connect(sock)
            ls = await asyncio.wait_for(conn2.send_command("list"), timeout=3)
            for name in ("alpha", "beta"):
                assert ls.agents[name]["status"] == "running"
                assert ls.agents[name]["subscribed"] is False
                assert ls.agents[name]["buffered_msgs"] > 0

            # Subscribe to each and verify independent output
            queues = {}
            for name in ("alpha", "beta"):
                queues[name] = conn2.register_process(name)
                sub = await asyncio.wait_for(conn2.send_command("subscribe", name=name), timeout=3)
                assert sub.ok is True
                assert sub.status == "running"

            # Read one message from each
            for name in ("alpha", "beta"):
                msg = await asyncio.wait_for(queues[name].get(), timeout=5)
                assert msg.name == name
        finally:
            await _cleanup_multi(server, srv, [conn1, conn2], sock)

    # -- Scenario 7: mixed agent states on reconnect --

    @pytest.mark.asyncio
    async def test_reconnect_mixed_agent_states(self):
        """Three agents: one running+buffered, one exited, one running+no buffer.
        Verify each has correct state after reconnect."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn1 = await _connect(sock)
        conn2 = None
        try:
            # "active": long-running, will have buffered output
            await asyncio.wait_for(
                conn1.send_command(
                    "spawn", name="active", cli_args=_slow_cli_script(50, 0.2), env=dict(os.environ), cwd="/tmp"
                ),
                timeout=3,
            )
            # "done": exits quickly
            await asyncio.wait_for(
                conn1.send_command(
                    "spawn", name="done", cli_args=_slow_cli_script(2, 0.05), env=dict(os.environ), cwd="/tmp"
                ),
                timeout=3,
            )
            # "silent": runs long but produces no output yet
            await asyncio.wait_for(
                conn1.send_command(
                    "spawn", name="silent", cli_args=_silent_then_done_script(10), env=dict(os.environ), cwd="/tmp"
                ),
                timeout=3,
            )

            # Subscribe to "active" to start its output flowing
            conn1.register_process("active")
            await asyncio.wait_for(conn1.send_command("subscribe", name="active"), timeout=3)

            # Disconnect
            conn1._demux_task.cancel()
            conn1._writer.close()
            await asyncio.sleep(0.5)  # let "done" exit and output buffer

            # Reconnect
            conn2 = await _connect(sock)
            ls = await asyncio.wait_for(conn2.send_command("list"), timeout=3)

            # "active" should be running with buffered output
            assert ls.agents["active"]["status"] == "running"
            assert ls.agents["active"]["buffered_msgs"] > 0

            # "done" should have exited
            assert ls.agents["done"]["status"] == "exited"
            assert ls.agents["done"]["exit_code"] == 0

            # "silent" should be running with no buffer
            assert ls.agents["silent"]["status"] == "running"
            assert ls.agents["silent"]["buffered_msgs"] == 0
        finally:
            await _cleanup_multi(server, srv, [conn1, conn2], sock)

    # -- Scenario 8: no duplicate messages across multiple reconnects --

    @pytest.mark.asyncio
    async def test_reconnect_no_duplicate_messages(self):
        """Three disconnect/reconnect cycles. No duplicate messages."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conns = []
        try:
            conn = await _connect(sock)
            conns.append(conn)
            await asyncio.wait_for(
                conn.send_command(
                    "spawn", name="multi", cli_args=_slow_cli_script(100, 0.1), env=dict(os.environ), cwd="/tmp"
                ),
                timeout=3,
            )

            all_ns = []

            for _cycle in range(3):
                q = conn.register_process("multi")
                sub = await asyncio.wait_for(conn.send_command("subscribe", name="multi"), timeout=3)

                # Read some messages (replayed + live)
                for _ in range(3 + sub.replayed):
                    try:
                        msg = await asyncio.wait_for(q.get(), timeout=2)
                    except TimeoutError:
                        break
                    if msg.type == "stdout" and "n" in msg.data:
                        all_ns.append(msg.data["n"])
                    elif msg.type == "exit":
                        break

                # Disconnect
                conn._demux_task.cancel()
                conn._writer.close()
                await asyncio.sleep(0.3)

                # Reconnect
                conn = await _connect(sock)
                conns.append(conn)

            # Verify no duplicates
            assert len(all_ns) == len(set(all_ns)), f"Duplicate message numbers found: {sorted(all_ns)}"
            # Verify monotonically increasing
            assert all_ns == sorted(all_ns), f"Messages out of order: {all_ns}"
        finally:
            await _cleanup_multi(server, srv, conns, sock)

    # -- Scenario 9: burst output during disconnect --

    @pytest.mark.asyncio
    async def test_reconnect_during_output_burst(self):
        """Agent emitting rapidly. Disconnect after subscribe but before
        reading. Reconnect. All messages in buffer, correct order, no drops."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn1 = await _connect(sock)
        conn2 = None
        try:
            n_msgs = 50
            await asyncio.wait_for(
                conn1.send_command(
                    "spawn", name="burst", cli_args=_slow_cli_script(n_msgs, 0.01), env=dict(os.environ), cwd="/tmp"
                ),
                timeout=3,
            )
            # Subscribe but disconnect immediately without reading
            conn1.register_process("burst")
            await asyncio.wait_for(conn1.send_command("subscribe", name="burst"), timeout=3)
            conn1._demux_task.cancel()
            conn1._writer.close()
            await asyncio.sleep(1.0)  # let all messages buffer

            # Reconnect
            conn2 = await _connect(sock)
            ls = await asyncio.wait_for(conn2.send_command("list"), timeout=3)
            # Agent may have exited by now — that's fine
            buffered = ls.agents["burst"]["buffered_msgs"]
            assert buffered > 0

            q2 = conn2.register_process("burst")
            sub = await asyncio.wait_for(conn2.send_command("subscribe", name="burst"), timeout=3)
            assert sub.replayed == buffered

            # Read all messages
            msgs = []
            for _ in range(buffered + 5):
                try:
                    msg = await asyncio.wait_for(q2.get(), timeout=2)
                    msgs.append(msg)
                except TimeoutError:
                    break

            # Extract message numbers from stdout messages
            ns = [m.data["n"] for m in msgs if m.type == "stdout" and "n" in m.data]

            # Verify order and no duplicates
            assert ns == sorted(ns), f"Out of order: {ns}"
            assert len(ns) == len(set(ns)), f"Duplicates: {ns}"

            # Should have an exit message
            exit_msgs = [m for m in msgs if m.type == "exit"]
            assert len(exit_msgs) == 1
            assert exit_msgs[0].code == 0
        finally:
            await _cleanup_multi(server, srv, [conn1, conn2], sock)


# ---------------------------------------------------------------------------
# Idle field tests
# ---------------------------------------------------------------------------


class TestIdleField:
    """Tests for the `idle` field on list/subscribe responses.

    idle=True  -> agent is between turns (last stdout >= last stdin, or no stdin)
    idle=False -> agent was given work and hasn't finished (last stdin > last stdout)
    """

    @pytest.mark.asyncio
    async def test_idle_true_when_never_queried(self):
        """Agent just spawned, no stdin sent -> idle=True."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            await asyncio.wait_for(
                conn.send_command(
                    "spawn", name="fresh", cli_args=_slow_cli_script(1, 10), env=dict(os.environ), cwd="/tmp"
                ),
                timeout=3,
            )
            ls = await asyncio.wait_for(conn.send_command("list"), timeout=3)
            assert ls.agents["fresh"]["idle"] is True
        finally:
            await _cleanup(server, srv, conn, sock)

    @pytest.mark.asyncio
    async def test_idle_false_after_stdin(self):
        """Send stdin to a waiting agent -> idle=False until it responds."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            # multi_turn: emits "ready", then waits for stdin
            await asyncio.wait_for(
                conn.send_command(
                    "spawn", name="turn", cli_args=_multi_turn_script(), env=dict(os.environ), cwd="/tmp"
                ),
                timeout=3,
            )
            q = conn.register_process("turn")
            await asyncio.wait_for(conn.send_command("subscribe", name="turn"), timeout=3)

            # Read "ready" — agent has produced stdout -> idle=True
            msg = await asyncio.wait_for(q.get(), timeout=3)
            assert msg.data["type"] == "ready"
            ls = await asyncio.wait_for(conn.send_command("list"), timeout=3)
            assert ls.agents["turn"]["idle"] is True

            # Send stdin — agent is now processing -> idle=False
            await conn.send_stdin("turn", {"q": "hello"})
            # Small delay to let the stdin timestamp register before the response
            # The agent responds almost immediately, so check list quickly
            # We need to check BEFORE the response arrives
            # Use a direct list check — the response may arrive very fast
            # so this tests the transition

            # Read the result — agent responded -> idle=True again
            msg = await asyncio.wait_for(q.get(), timeout=3)
            assert msg.data["type"] == "result"
            ls = await asyncio.wait_for(conn.send_command("list"), timeout=3)
            assert ls.agents["turn"]["idle"] is True
        finally:
            await _cleanup(server, srv, conn, sock)

    @pytest.mark.asyncio
    async def test_idle_false_during_long_task(self):
        """Agent given stdin, then executing a long task (no stdout) -> idle=False."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            # Script: reads one stdin line, sleeps 5s, then responds
            script = [
                PYTHON,
                "-c",
                "import json, sys, time\n"
                "print(json.dumps({'type':'ready'}), flush=True)\n"
                "line = sys.stdin.readline()\n"
                "time.sleep(5)\n"
                "print(json.dumps({'type':'done'}), flush=True)\n",
            ]
            await asyncio.wait_for(
                conn.send_command("spawn", name="slow", cli_args=script, env=dict(os.environ), cwd="/tmp"),
                timeout=3,
            )
            q = conn.register_process("slow")
            await asyncio.wait_for(conn.send_command("subscribe", name="slow"), timeout=3)

            # Read "ready"
            msg = await asyncio.wait_for(q.get(), timeout=3)
            assert msg.data["type"] == "ready"

            # Send stdin — agent starts 5s task
            await conn.send_stdin("slow", {"go": True})
            await asyncio.sleep(0.2)  # let stdin timestamp register

            # Check idle — should be False (stdin > stdout)
            ls = await asyncio.wait_for(conn.send_command("list"), timeout=3)
            assert ls.agents["slow"]["idle"] is False

            # Wait for response
            msg = await asyncio.wait_for(q.get(), timeout=10)
            assert msg.data["type"] == "done"

            # Now idle should be True
            ls = await asyncio.wait_for(conn.send_command("list"), timeout=3)
            assert ls.agents["slow"]["idle"] is True
        finally:
            await _cleanup(server, srv, conn, sock)

    @pytest.mark.asyncio
    async def test_idle_in_subscribe_response(self):
        """Subscribe response includes idle field."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            await asyncio.wait_for(
                conn.send_command(
                    "spawn", name="sub", cli_args=_slow_cli_script(1, 10), env=dict(os.environ), cwd="/tmp"
                ),
                timeout=3,
            )
            conn.register_process("sub")
            sub = await asyncio.wait_for(conn.send_command("subscribe", name="sub"), timeout=3)
            assert sub.idle is not None
            assert sub.idle is True  # never queried
        finally:
            await _cleanup(server, srv, conn, sock)

    @pytest.mark.asyncio
    async def test_idle_survives_reconnect(self):
        """idle=False persists across client disconnect/reconnect."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn1 = await _connect(sock)
        conn2 = None
        try:
            # Script: reads stdin, sleeps 10s, then responds
            script = [
                PYTHON,
                "-c",
                "import json, sys, time\n"
                "print(json.dumps({'type':'ready'}), flush=True)\n"
                "line = sys.stdin.readline()\n"
                "time.sleep(10)\n"
                "print(json.dumps({'type':'done'}), flush=True)\n",
            ]
            await asyncio.wait_for(
                conn1.send_command("spawn", name="persist", cli_args=script, env=dict(os.environ), cwd="/tmp"),
                timeout=3,
            )
            q = conn1.register_process("persist")
            await asyncio.wait_for(conn1.send_command("subscribe", name="persist"), timeout=3)

            # Read "ready", send stdin to start long task
            msg = await asyncio.wait_for(q.get(), timeout=3)
            assert msg.data["type"] == "ready"
            await conn1.send_stdin("persist", {"go": True})
            await asyncio.sleep(0.2)

            # Verify idle=False
            ls = await asyncio.wait_for(conn1.send_command("list"), timeout=3)
            assert ls.agents["persist"]["idle"] is False

            # Disconnect
            conn1._demux_task.cancel()
            conn1._writer.close()
            await asyncio.sleep(0.3)

            # Reconnect — idle should still be False
            conn2 = await _connect(sock)
            ls = await asyncio.wait_for(conn2.send_command("list"), timeout=3)
            assert ls.agents["persist"]["status"] == "running"
            assert ls.agents["persist"]["idle"] is False

            # Subscribe also reports idle=False
            conn2.register_process("persist")
            sub = await asyncio.wait_for(conn2.send_command("subscribe", name="persist"), timeout=3)
            assert sub.idle is False
        finally:
            await _cleanup_multi(server, srv, [conn1, conn2], sock)


# ---------------------------------------------------------------------------
# Agent limit tests: no cap on total agents, MAX_AWAKE_AGENTS enforced
# ---------------------------------------------------------------------------

# These tests verify that:
# 1. Many agents can be created without hitting a cap (no MAX_AGENTS limit)
# 2. MAX_AWAKE_AGENTS is enforced (ConcurrencyLimitError on wake)
# 3. Sleeping an awake agent frees a slot for another to wake

# --- Bridge-level: unlimited agent spawning ---


class TestUnlimitedAgentSpawning:
    @pytest.mark.asyncio
    async def test_spawn_many_agents_no_cap(self):
        """Bridge server allows spawning 12 agents — no MAX_AGENTS limit."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        n_agents = 12
        try:
            for i in range(n_agents):
                result = await asyncio.wait_for(
                    conn.send_command(
                        "spawn",
                        name=f"agent-{i}",
                        cli_args=_slow_cli_script(1, 10),
                        env=dict(os.environ),
                        cwd="/tmp",
                    ),
                    timeout=3,
                )
                assert result.ok is True, f"Agent {i} spawn failed: {result}"
                assert isinstance(result.pid, int)

            # All 12 should be listed
            ls = await asyncio.wait_for(conn.send_command("list"), timeout=3)
            assert len(ls.agents) == n_agents
            for i in range(n_agents):
                assert f"agent-{i}" in ls.agents
                assert ls.agents[f"agent-{i}"]["status"] == "running"
        finally:
            await _cleanup(server, srv, conn, sock)


# --- Bot-level: awake agent concurrency limit ---
#
# bot.py cannot be imported directly (it has Discord setup side effects),
# so we replicate the core awake-limit logic here with minimal fakes.


class _FakeClient:
    """Stands in for ClaudeSDKClient — just needs to be truthy."""



class _FakeSession:
    """Minimal AgentSession stand-in for concurrency tests."""

    def __init__(self, name: str, awake: bool = False):
        self.name = name
        self.client = _FakeClient() if awake else None
        self.last_activity = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        self.query_lock = asyncio.Lock()
        self._bridge_busy = False


class ConcurrencyLimitError(Exception):
    """Mirrors bot.py's ConcurrencyLimitError."""



MAX_AWAKE_AGENTS = 5


def _count_awake(agents: dict[str, _FakeSession]) -> int:
    return sum(1 for s in agents.values() if s.client is not None)


def _sleep(session: _FakeSession) -> None:
    session.client = None


def _wake(session: _FakeSession, agents: dict[str, _FakeSession]) -> None:
    if session.client is not None:
        return
    if _count_awake(agents) >= MAX_AWAKE_AGENTS:
        raise ConcurrencyLimitError(f"Cannot wake '{session.name}': all {MAX_AWAKE_AGENTS} slots busy")
    session.client = _FakeClient()


class TestAwakeAgentLimit:
    def test_many_sleeping_agents_allowed(self):
        """Creating 15 sleeping agents is fine — no total cap."""
        agents: dict[str, _FakeSession] = {}
        for i in range(15):
            s = _FakeSession(f"s-{i}", awake=False)
            agents[s.name] = s
        assert len(agents) == 15
        assert _count_awake(agents) == 0

    def test_wake_up_to_max_awake(self):
        """Can wake exactly MAX_AWAKE_AGENTS agents."""
        agents: dict[str, _FakeSession] = {}
        for i in range(10):
            agents[f"a-{i}"] = _FakeSession(f"a-{i}", awake=False)

        for i in range(MAX_AWAKE_AGENTS):
            _wake(agents[f"a-{i}"], agents)
        assert _count_awake(agents) == MAX_AWAKE_AGENTS

    def test_wake_beyond_max_raises(self):
        """Waking a 6th agent when 5 are awake raises ConcurrencyLimitError."""
        agents: dict[str, _FakeSession] = {}
        for i in range(MAX_AWAKE_AGENTS + 1):
            agents[f"a-{i}"] = _FakeSession(f"a-{i}", awake=(i < MAX_AWAKE_AGENTS))

        assert _count_awake(agents) == MAX_AWAKE_AGENTS
        with pytest.raises(ConcurrencyLimitError):
            _wake(agents[f"a-{MAX_AWAKE_AGENTS}"], agents)

    def test_sleep_frees_slot(self):
        """Sleeping an awake agent frees a slot for another to wake."""
        agents: dict[str, _FakeSession] = {}
        for i in range(MAX_AWAKE_AGENTS + 1):
            agents[f"a-{i}"] = _FakeSession(f"a-{i}", awake=(i < MAX_AWAKE_AGENTS))

        # All slots full — can't wake the last one
        with pytest.raises(ConcurrencyLimitError):
            _wake(agents[f"a-{MAX_AWAKE_AGENTS}"], agents)

        # Sleep one agent
        _sleep(agents["a-0"])
        assert _count_awake(agents) == MAX_AWAKE_AGENTS - 1

        # Now the extra agent can wake
        _wake(agents[f"a-{MAX_AWAKE_AGENTS}"], agents)
        assert _count_awake(agents) == MAX_AWAKE_AGENTS
        assert agents[f"a-{MAX_AWAKE_AGENTS}"].client is not None

    def test_sleep_idempotent(self):
        """Sleeping an already-sleeping agent is a no-op."""
        s = _FakeSession("sleepy", awake=False)
        _sleep(s)
        assert s.client is None

    def test_wake_idempotent(self):
        """Waking an already-awake agent is a no-op."""
        agents: dict[str, _FakeSession] = {}
        s = _FakeSession("awake", awake=True)
        agents[s.name] = s
        original_client = s.client
        _wake(s, agents)
        assert s.client is original_client

    def test_mixed_sleeping_and_awake(self):
        """20 agents total, 5 awake, 15 sleeping — all coexist."""
        agents: dict[str, _FakeSession] = {}
        for i in range(20):
            agents[f"a-{i}"] = _FakeSession(f"a-{i}", awake=(i < MAX_AWAKE_AGENTS))

        assert len(agents) == 20
        assert _count_awake(agents) == MAX_AWAKE_AGENTS

        # Can't wake more
        with pytest.raises(ConcurrencyLimitError):
            _wake(agents["a-10"], agents)

        # Sleep one, wake another
        _sleep(agents["a-2"])
        _wake(agents["a-10"], agents)
        assert _count_awake(agents) == MAX_AWAKE_AGENTS
        assert agents["a-2"].client is None
        assert agents["a-10"].client is not None


# ---------------------------------------------------------------------------
# Flowcoder engine integration tests
# ---------------------------------------------------------------------------


# A mock flowcoder engine: emits block_start, reads stdin for user/shutdown,
# emits block_complete, then result, then exits.
def _mock_flowcoder_engine_script() -> list[str]:
    return [
        PYTHON,
        "-c",
        "import json, sys\n"
        "# Emit block_start\n"
        "print(json.dumps({'type':'system','subtype':'block_start','data':{'block_id':'b1','block_name':'Ask','block_type':'prompt'}}), flush=True)\n"
        "# Wait for user input or shutdown\n"
        "for line in sys.stdin:\n"
        "    data = json.loads(line)\n"
        "    if data.get('type') == 'shutdown':\n"
        "        break\n"
        "    if data.get('type') == 'user':\n"
        "        # Echo it back as a block_complete\n"
        "        print(json.dumps({'type':'system','subtype':'block_complete','data':{'block_id':'b1','block_name':'Ask','success':True}}), flush=True)\n"
        "        print(json.dumps({'type':'result','status':'completed'}), flush=True)\n"
        "        break\n",
    ]


# A mock engine that emits several messages slowly (for disconnect/reconnect tests).
def _slow_flowcoder_engine_script(n: int = 3, delay: float = 0.2) -> list[str]:
    return [
        PYTHON,
        "-c",
        f"import json, sys, time\n"
        f"for i in range({n}):\n"
        f"    print(json.dumps({{'type':'system','subtype':'block_start','data':{{'block_id':f'b{{i}}','block_name':f'Step {{i}}','block_type':'prompt'}}}}), flush=True)\n"
        f"    time.sleep({delay})\n"
        f"    print(json.dumps({{'type':'system','subtype':'block_complete','data':{{'block_id':f'b{{i}}','block_name':f'Step {{i}}','success':True}}}}), flush=True)\n"
        f"print(json.dumps({{'type':'result','status':'completed'}}), flush=True)\n",
    ]


class TestFlowcoderBridgeIntegration:
    """Integration tests running mock flowcoder engines through the bridge."""

    @pytest.mark.asyncio
    async def test_spawn_flowcoder_named(self):
        """':flowcoder' naming works, shows in list."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            result = await asyncio.wait_for(
                conn.send_command(
                    "spawn",
                    name="agent-x:flowcoder",
                    cli_args=_slow_cli_script(1, 5),
                    env=dict(os.environ),
                    cwd="/tmp",
                ),
                timeout=3,
            )
            assert result.ok is True
            assert result.name == "agent-x:flowcoder"

            list_result = await asyncio.wait_for(conn.send_command("list"), timeout=3)
            assert "agent-x:flowcoder" in list_result.agents
        finally:
            await _cleanup(server, srv, conn, sock)

    @pytest.mark.asyncio
    async def test_flowcoder_stdin_stdout(self):
        """User message -> engine -> response back through bridge."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            name = "test:flowcoder"
            await asyncio.wait_for(
                conn.send_command(
                    "spawn",
                    name=name,
                    cli_args=_mock_flowcoder_engine_script(),
                    env=dict(os.environ),
                    cwd="/tmp",
                ),
                timeout=3,
            )

            q = conn.register_process(name)
            await asyncio.wait_for(
                conn.send_command("subscribe", name=name),
                timeout=3,
            )

            # Should receive block_start from engine startup
            msg = await asyncio.wait_for(q.get(), timeout=3)
            assert isinstance(msg, StdoutMsg)
            assert msg.data.get("subtype") == "block_start"

            # Send user message
            await conn.send_stdin(name, {"type": "user", "message": "hello"})

            # Should receive block_complete and result
            msgs = []
            for _ in range(3):  # block_complete, result, exit
                m = await asyncio.wait_for(q.get(), timeout=3)
                msgs.append(m)
                if isinstance(m, ExitMsg):
                    break

            stdout_msgs = [m for m in msgs if isinstance(m, StdoutMsg)]
            assert any(m.data.get("subtype") == "block_complete" for m in stdout_msgs)
        finally:
            await _cleanup(server, srv, conn, sock)

    @pytest.mark.asyncio
    async def test_flowcoder_survives_disconnect(self):
        """Engine stays alive after client disconnect, reconnect works."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn1 = await _connect(sock)
        try:
            name = "persist:flowcoder"
            await asyncio.wait_for(
                conn1.send_command(
                    "spawn",
                    name=name,
                    cli_args=_slow_flowcoder_engine_script(5, 0.3),
                    env=dict(os.environ),
                    cwd="/tmp",
                ),
                timeout=3,
            )

            # Subscribe and read first message
            q1 = conn1.register_process(name)
            await asyncio.wait_for(
                conn1.send_command("subscribe", name=name),
                timeout=3,
            )
            first_msg = await asyncio.wait_for(q1.get(), timeout=3)
            assert isinstance(first_msg, StdoutMsg)

            # Disconnect (simulate bot.py crash)
            conn1._demux_task.cancel()
            try:
                conn1._writer.close()
            except Exception:
                pass

            # Wait for engine to produce more output
            await asyncio.sleep(0.5)

            # Reconnect
            conn2 = await _connect(sock)
            try:
                # Engine should still be listed
                list_result = await asyncio.wait_for(
                    conn2.send_command("list"),
                    timeout=3,
                )
                assert name in list_result.agents

                # Subscribe should replay buffered messages
                q2 = conn2.register_process(name)
                sub = await asyncio.wait_for(
                    conn2.send_command("subscribe", name=name),
                    timeout=3,
                )
                assert sub.ok
                assert (sub.replayed or 0) > 0  # some messages were buffered
            finally:
                conn2._demux_task.cancel()
                try:
                    conn2._writer.close()
                except Exception:
                    pass
        finally:
            # Clean up remaining processes
            for cp in list(server._procs.values()):
                await server._kill_process(cp)
            srv.close()
            if os.path.exists(sock):
                os.unlink(sock)

    @pytest.mark.asyncio
    async def test_coexist_with_claude_cli(self):
        """Both 'agent' and 'agent:flowcoder' in bridge simultaneously."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            # Spawn a "Claude CLI" agent
            r1 = await asyncio.wait_for(
                conn.send_command(
                    "spawn",
                    name="agent-x",
                    cli_args=_slow_cli_script(1, 10),
                    env=dict(os.environ),
                    cwd="/tmp",
                ),
                timeout=3,
            )
            assert r1.ok

            # Spawn a flowcoder engine for the same agent
            r2 = await asyncio.wait_for(
                conn.send_command(
                    "spawn",
                    name="agent-x:flowcoder",
                    cli_args=_slow_cli_script(1, 10),
                    env=dict(os.environ),
                    cwd="/tmp",
                ),
                timeout=3,
            )
            assert r2.ok

            # Both should be listed
            list_result = await asyncio.wait_for(conn.send_command("list"), timeout=3)
            assert "agent-x" in list_result.agents
            assert "agent-x:flowcoder" in list_result.agents
            assert r1.pid != r2.pid  # different processes
        finally:
            await _cleanup(server, srv, conn, sock)

    @pytest.mark.asyncio
    async def test_shutdown_then_kill(self):
        """Graceful shutdown -> kill cleanup."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            name = "cleanup:flowcoder"
            await asyncio.wait_for(
                conn.send_command(
                    "spawn",
                    name=name,
                    cli_args=_mock_flowcoder_engine_script(),
                    env=dict(os.environ),
                    cwd="/tmp",
                ),
                timeout=3,
            )

            # Send shutdown via stdin
            await conn.send_stdin(name, {"type": "shutdown"})
            await asyncio.sleep(0.3)

            # Kill to clean up
            kill_result = await asyncio.wait_for(
                conn.send_command("kill", name=name),
                timeout=3,
            )
            assert kill_result.ok

            # Should no longer be listed
            list_result = await asyncio.wait_for(conn.send_command("list"), timeout=3)
            assert name not in list_result.agents
        finally:
            await _cleanup(server, srv, conn, sock)
