"""Tests for the agent bridge (bridge.py).

Covers the server (BridgeServer), client (BridgeConnection, BridgeTransport),
and lifecycle helpers.  All tests use real Unix sockets with ephemeral paths —
no mocking of the wire protocol so we're testing actual serialization.

Run with: pytest test_bridge.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid

import pytest

from bridge import (
    TYPE_CMD,
    TYPE_EXIT,
    TYPE_RESULT,
    TYPE_STDERR,
    TYPE_STDIN,
    TYPE_STDOUT,
    BridgeConnection,
    BridgeServer,
    BridgeTransport,
    connect_to_bridge,
    ensure_bridge,
)

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


async def _cleanup(server: BridgeServer, srv: asyncio.Server, conn: BridgeConnection, sock: str):
    """Tear down server + connection.

    We cancel the demux task and close everything without awaiting
    wait_closed() — those block indefinitely when the server handler
    is also stuck. For tests this is fine.
    """
    for cp in list(server._cli_procs.values()):
        await server._kill_cli(cp)
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
        PYTHON, "-c",
        f"import json,sys,time\n"
        f"for i in range({n}):\n"
        f"    print(json.dumps({{'type':'msg','n':i}}),flush=True)\n"
        f"    time.sleep({delay})\n"
    ]


# A CLI script that echoes stdin lines back to stdout as JSON.
def _echo_cli_script() -> list[str]:
    return [
        PYTHON, "-c",
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    data = json.loads(line)\n"
        "    print(json.dumps({'type':'echo','data':data}), flush=True)\n"
    ]


# A CLI that writes to stderr and stdout, then exits.
def _stderr_cli_script() -> list[str]:
    return [
        PYTHON, "-c",
        "import sys, json\n"
        "sys.stderr.write('warning: something\\n')\n"
        "sys.stderr.flush()\n"
        "print(json.dumps({'type':'ok'}), flush=True)\n"
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
            assert result["ok"] is True
            assert result["agents"] == {}
        finally:
            await _cleanup(server, srv, conn, sock)

    @pytest.mark.asyncio
    async def test_list_after_spawn(self):
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            await asyncio.wait_for(
                conn.send_command("spawn", name="a", cli_args=_slow_cli_script(1, 5),
                                  env=dict(os.environ), cwd="/tmp"),
                timeout=3,
            )
            result = await asyncio.wait_for(conn.send_command("list"), timeout=3)
            assert "a" in result["agents"]
            assert result["agents"]["a"]["status"] == "running"
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
                conn.send_command("spawn", name="x", cli_args=_slow_cli_script(1, 5),
                                  env=dict(os.environ), cwd="/tmp"),
                timeout=3,
            )
            assert result["ok"] is True
            assert result["name"] == "x"
            assert isinstance(result["pid"], int)
        finally:
            await _cleanup(server, srv, conn, sock)

    @pytest.mark.asyncio
    async def test_spawn_already_running(self):
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            r1 = await asyncio.wait_for(
                conn.send_command("spawn", name="dup", cli_args=_slow_cli_script(1, 5),
                                  env=dict(os.environ), cwd="/tmp"),
                timeout=3,
            )
            r2 = await asyncio.wait_for(
                conn.send_command("spawn", name="dup", cli_args=_slow_cli_script(1, 5),
                                  env=dict(os.environ), cwd="/tmp"),
                timeout=3,
            )
            assert r2["ok"] is True
            assert r2["already_running"] is True
            assert r2["pid"] == r1["pid"]
        finally:
            await _cleanup(server, srv, conn, sock)

    @pytest.mark.asyncio
    async def test_spawn_bad_command(self):
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            result = await asyncio.wait_for(
                conn.send_command("spawn", name="bad",
                                  cli_args=["/nonexistent/binary"],
                                  env=dict(os.environ), cwd="/tmp"),
                timeout=3,
            )
            assert result["ok"] is False
            assert "error" in result
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
                conn.send_command("spawn", name="k", cli_args=_slow_cli_script(100, 1),
                                  env=dict(os.environ), cwd="/tmp"),
                timeout=3,
            )
            result = await asyncio.wait_for(
                conn.send_command("kill", name="k"), timeout=5,
            )
            assert result["ok"] is True
            # Should be gone from list
            ls = await asyncio.wait_for(conn.send_command("list"), timeout=3)
            assert "k" not in ls["agents"]
        finally:
            await _cleanup(server, srv, conn, sock)

    @pytest.mark.asyncio
    async def test_kill_nonexistent(self):
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            result = await asyncio.wait_for(
                conn.send_command("kill", name="ghost"), timeout=3,
            )
            assert result["ok"] is False
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
                conn.send_command("spawn", name="int", cli_args=_slow_cli_script(100, 1),
                                  env=dict(os.environ), cwd="/tmp"),
                timeout=3,
            )
            result = await asyncio.wait_for(
                conn.send_command("interrupt", name="int"), timeout=3,
            )
            assert result["ok"] is True
        finally:
            await _cleanup(server, srv, conn, sock)

    @pytest.mark.asyncio
    async def test_interrupt_nonexistent(self):
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            result = await asyncio.wait_for(
                conn.send_command("interrupt", name="nope"), timeout=3,
            )
            assert result["ok"] is False
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
                conn.send_command("spawn", name="sr", cli_args=_slow_cli_script(3, 0.1),
                                  env=dict(os.environ), cwd="/tmp"),
                timeout=3,
            )
            q = conn.register_agent("sr")
            sub = await asyncio.wait_for(conn.send_command("subscribe", name="sr"), timeout=3)
            assert sub["ok"] is True

            msgs = []
            for _ in range(10):
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=3)
                    if msg is None:
                        break
                    msgs.append(msg)
                    if msg["type"] == TYPE_EXIT:
                        break
                except asyncio.TimeoutError:
                    break

            types = [m["type"] for m in msgs]
            assert TYPE_STDOUT in types
            assert TYPE_EXIT in types
        finally:
            await _cleanup(server, srv, conn, sock)

    @pytest.mark.asyncio
    async def test_subscribe_nonexistent(self):
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            result = await asyncio.wait_for(
                conn.send_command("subscribe", name="nope"), timeout=3,
            )
            assert result["ok"] is False
        finally:
            await _cleanup(server, srv, conn, sock)

    @pytest.mark.asyncio
    async def test_stderr_relayed(self):
        """Stderr output is relayed as TYPE_STDERR messages."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            await asyncio.wait_for(
                conn.send_command("spawn", name="se", cli_args=_stderr_cli_script(),
                                  env=dict(os.environ), cwd="/tmp"),
                timeout=3,
            )
            q = conn.register_agent("se")
            await asyncio.wait_for(conn.send_command("subscribe", name="se"), timeout=3)

            msgs = []
            for _ in range(10):
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=3)
                    if msg is None:
                        break
                    msgs.append(msg)
                    if msg["type"] == TYPE_EXIT:
                        break
                except asyncio.TimeoutError:
                    break

            types = [m["type"] for m in msgs]
            assert TYPE_STDERR in types
            stderr_msgs = [m for m in msgs if m["type"] == TYPE_STDERR]
            assert any("warning" in m.get("text", "") for m in stderr_msgs)
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
                conn.send_command("spawn", name="echo", cli_args=_echo_cli_script(),
                                  env=dict(os.environ), cwd="/tmp"),
                timeout=3,
            )
            q = conn.register_agent("echo")
            await asyncio.wait_for(conn.send_command("subscribe", name="echo"), timeout=3)

            # Send data to stdin
            await conn.send_stdin("echo", {"hello": "world"})

            # Should get it echoed back
            msg = await asyncio.wait_for(q.get(), timeout=3)
            assert msg["type"] == TYPE_STDOUT
            assert msg["data"]["type"] == "echo"
            assert msg["data"]["data"] == {"hello": "world"}
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
                conn1.send_command("spawn", name="buf", cli_args=_slow_cli_script(5, 0.02),
                                   env=dict(os.environ), cwd="/tmp"),
                timeout=3,
            )
            q1 = conn1.register_agent("buf")
            await asyncio.wait_for(conn1.send_command("subscribe", name="buf"), timeout=3)

            # Receive first message
            msg = await asyncio.wait_for(q1.get(), timeout=3)
            assert msg["type"] == TYPE_STDOUT

            # Disconnect client 1
            conn1._demux_task.cancel()
            conn1._writer.close()
            await asyncio.sleep(0.3)  # let remaining messages buffer

            # Connect client 2
            conn2 = await _connect(sock)

            # Check buffer
            ls = await asyncio.wait_for(conn2.send_command("list"), timeout=3)
            buffered = ls["agents"]["buf"]["buffered_msgs"]
            assert buffered > 0, f"Expected buffered > 0, got {buffered}"

            # Subscribe to replay
            q2 = conn2.register_agent("buf")
            sub = await asyncio.wait_for(conn2.send_command("subscribe", name="buf"), timeout=3)
            assert sub["replayed"] == buffered

            # Read replayed messages
            msgs = []
            for _ in range(20):
                try:
                    msg = await asyncio.wait_for(q2.get(), timeout=2)
                    if msg is None:
                        break
                    msgs.append(msg)
                    if msg["type"] == TYPE_EXIT:
                        break
                except asyncio.TimeoutError:
                    break

            assert len(msgs) >= buffered
        finally:
            for cp in list(server._cli_procs.values()):
                await server._kill_cli(cp)
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
                conn1.send_command("spawn", name="res", cli_args=_slow_cli_script(100, 1),
                                   env=dict(os.environ), cwd="/tmp"),
                timeout=3,
            )
            conn1.register_agent("res")
            await asyncio.wait_for(conn1.send_command("subscribe", name="res"), timeout=3)

            # Verify subscribed
            ls = await asyncio.wait_for(conn1.send_command("list"), timeout=3)
            assert ls["agents"]["res"]["subscribed"] is True

            # Connect a second client (replaces first)
            conn2 = await _connect(sock)
            await asyncio.sleep(0.2)  # let server process the new connection

            ls2 = await asyncio.wait_for(conn2.send_command("list"), timeout=3)
            assert ls2["agents"]["res"]["subscribed"] is False

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
                conn.send_command("bogus"), timeout=3,
            )
            assert result["ok"] is False
            assert "unknown" in result.get("error", "").lower()
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
                conn.send_command("spawn", name="a1", cli_args=_slow_cli_script(2, 0.1),
                                  env=dict(os.environ), cwd="/tmp"),
                timeout=3,
            )
            await asyncio.wait_for(
                conn.send_command("spawn", name="a2", cli_args=_slow_cli_script(2, 0.1),
                                  env=dict(os.environ), cwd="/tmp"),
                timeout=3,
            )

            q1 = conn.register_agent("a1")
            q2 = conn.register_agent("a2")

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
                assert m["name"] == "a1"
            for m in msgs_a2:
                assert m["name"] == "a2"
        finally:
            await _cleanup(server, srv, conn, sock)

    @pytest.mark.asyncio
    async def test_sentinel_on_disconnect(self):
        """When the bridge connection is lost, agent queues get a None sentinel."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            q = conn.register_agent("phantom")
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
                conn.send_command("spawn", name="rc", cli_args=_slow_cli_script(1, 10),
                                  env=dict(os.environ), cwd="/tmp"),
                timeout=3,
            )

            transport = BridgeTransport("rc", conn, reconnecting=True)
            await transport.connect()
            await asyncio.wait_for(transport.subscribe(), timeout=3)

            # Write an initialize control_request
            init_msg = json.dumps({
                "type": "control_request",
                "request_id": "test-init-42",
                "request": {"subtype": "initialize"},
            })
            await transport.write(init_msg)

            # Should get a fake response from the queue
            msg = await asyncio.wait_for(transport._queue.get(), timeout=2)
            assert msg["type"] == TYPE_STDOUT
            assert msg["data"]["type"] == "control_response"
            assert msg["data"]["response"]["request_id"] == "test-init-42"
            assert msg["data"]["response"]["subtype"] == "success"

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
                conn.send_command("spawn", name="nr", cli_args=_echo_cli_script(),
                                  env=dict(os.environ), cwd="/tmp"),
                timeout=3,
            )

            transport = BridgeTransport("nr", conn, reconnecting=False)
            await transport.connect()
            await asyncio.wait_for(transport.subscribe(), timeout=3)

            # Write an initialize — should be forwarded (echo will echo it back)
            init_msg = json.dumps({
                "type": "control_request",
                "request_id": "test-init-99",
                "request": {"subtype": "initialize"},
            })
            await transport.write(init_msg)

            # Should get the echo back (not a fake response)
            msg = await asyncio.wait_for(transport._queue.get(), timeout=3)
            assert msg["type"] == TYPE_STDOUT
            assert msg["data"]["type"] == "echo"
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
                conn.send_command("spawn", name="rd", cli_args=_slow_cli_script(2, 0.1),
                                  env=dict(os.environ), cwd="/tmp"),
                timeout=3,
            )

            transport = BridgeTransport("rd", conn)
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
                conn.send_command("spawn", name="ex", cli_args=_slow_cli_script(1, 0),
                                  env=dict(os.environ), cwd="/tmp"),
                timeout=3,
            )

            transport = BridgeTransport("ex", conn)
            await transport.connect()
            await asyncio.wait_for(transport.subscribe(), timeout=3)

            msgs = []
            async for data in transport.read_messages():
                msgs.append(data)

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
                conn.send_command("spawn", name="sc", cli_args=_stderr_cli_script(),
                                  env=dict(os.environ), cwd="/tmp"),
                timeout=3,
            )

            stderr_lines = []
            transport = BridgeTransport("sc", conn, stderr_callback=stderr_lines.append)
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
            transport = BridgeTransport("lc", conn)
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
                conn.send_command("spawn", name="cl", cli_args=_slow_cli_script(100, 1),
                                  env=dict(os.environ), cwd="/tmp"),
                timeout=3,
            )

            transport = BridgeTransport("cl", conn)
            await transport.connect()
            await asyncio.wait_for(transport.subscribe(), timeout=3)

            await transport.close()

            assert transport.is_ready() is False
            # Agent should be gone from bridge
            ls = await asyncio.wait_for(conn.send_command("list"), timeout=3)
            assert "cl" not in ls["agents"]
        finally:
            await _cleanup(server, srv, conn, sock)

    @pytest.mark.asyncio
    async def test_end_input_is_noop(self):
        """end_input() should not raise."""
        sock = _tmp_sock()
        server, srv = await _start_server(sock)
        conn = await _connect(sock)
        try:
            transport = BridgeTransport("ei", conn)
            await transport.connect()
            await transport.end_input()  # should not raise
        finally:
            await _cleanup(server, srv, conn, sock)


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
            assert result["ok"] is True

            await conn.close()

            # Bridge should still be running — reconnect to verify
            conn2 = await connect_to_bridge(sock)
            assert conn2 is not None
            result2 = await asyncio.wait_for(conn2.send_command("list"), timeout=3)
            assert result2["ok"] is True
            await conn2.close()
        finally:
            # Kill bridge process
            import subprocess, signal
            ps = subprocess.run(["pgrep", "-f", f"bridge.py {sock}"],
                                capture_output=True, text=True)
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
            assert result["ok"] is True
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
        from shutdown import ShutdownCoordinator

        sleep_fn = AsyncMock()
        kill_fn = MagicMock()

        with patch("shutdown._start_deadline_thread"):
            c = ShutdownCoordinator(
                agents={"a": type("A", (), {"name": "a", "client": "x",
                                             "query_lock": asyncio.Lock()})()},
                sleep_fn=sleep_fn,
                close_bot_fn=AsyncMock(),
                kill_fn=kill_fn,
                bridge_mode=True,
            )
            await c.graceful_shutdown("test")

        # sleep_fn should NOT have been called in bridge mode
        sleep_fn.assert_not_called()
        kill_fn.assert_called_once()
