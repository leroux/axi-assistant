"""Tests for claudewire.transport.BridgeTransport.

Uses a FakeProcessConnection — a real in-memory implementation of the
ProcessConnection protocol with async queues. No mocks.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from claudewire.transport import BridgeTransport
from claudewire.types import CommandResult, ExitEvent, StderrEvent, StdoutEvent

# ---------------------------------------------------------------------------
# Fake ProcessConnection — real async queues, no mock
# ---------------------------------------------------------------------------


class FakeProcessConnection:
    """In-memory ProcessConnection for testing BridgeTransport in isolation."""

    def __init__(self, *, alive: bool = True) -> None:
        self._alive = alive
        self._queues: dict[str, asyncio.Queue[StdoutEvent | StderrEvent | ExitEvent | None]] = {}
        self._spawned: list[dict] = []
        self._subscribed: list[str] = []
        self._killed: list[str] = []
        self._unregistered: list[str] = []
        self._stdin: list[tuple[str, dict]] = []
        # Configurable results
        self.spawn_result = CommandResult(ok=True)
        self.subscribe_result = CommandResult(ok=True, replayed=0, status="running")
        self.kill_result = CommandResult(ok=True)

    @property
    def is_alive(self) -> bool:
        return self._alive

    def register(self, name: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._queues[name] = q
        return q

    def unregister(self, name: str) -> None:
        self._unregistered.append(name)
        self._queues.pop(name, None)

    async def spawn(self, name: str, *, cli_args: list[str], env: dict[str, str], cwd: str) -> CommandResult:
        self._spawned.append({"name": name, "cli_args": cli_args, "env": env, "cwd": cwd})
        return self.spawn_result

    async def subscribe(self, name: str) -> CommandResult:
        self._subscribed.append(name)
        return self.subscribe_result

    async def kill(self, name: str) -> CommandResult:
        self._killed.append(name)
        return self.kill_result

    async def send_stdin(self, name: str, data: dict) -> None:
        self._stdin.append((name, data))

    def inject(self, name: str, event: StdoutEvent | StderrEvent | ExitEvent | None) -> None:
        """Push an event into an agent's queue (simulates process output)."""
        self._queues[name].put_nowait(event)


# ---------------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------------


class TestConnect:
    @pytest.mark.asyncio
    async def test_registers_queue_and_sets_ready(self) -> None:
        conn = FakeProcessConnection()
        t = BridgeTransport("a1", conn)
        assert t.is_ready() is False
        await t.connect()
        assert t.is_ready() is True
        assert "a1" in conn._queues


# ---------------------------------------------------------------------------
# spawn
# ---------------------------------------------------------------------------


class TestSpawn:
    @pytest.mark.asyncio
    async def test_spawn_success(self) -> None:
        conn = FakeProcessConnection()
        t = BridgeTransport("a1", conn)
        result = await t.spawn(cli_args=["echo"], env={"A": "1"}, cwd="/tmp")
        assert result.ok is True
        assert conn._spawned[0]["name"] == "a1"
        assert conn._spawned[0]["cli_args"] == ["echo"]

    @pytest.mark.asyncio
    async def test_spawn_failure_raises(self) -> None:
        conn = FakeProcessConnection()
        conn.spawn_result = CommandResult(ok=False, error="bad command")
        t = BridgeTransport("a1", conn)
        with pytest.raises(RuntimeError, match="Spawn failed"):
            await t.spawn(cli_args=["bad"], env={}, cwd="/tmp")


# ---------------------------------------------------------------------------
# subscribe
# ---------------------------------------------------------------------------


class TestSubscribe:
    @pytest.mark.asyncio
    async def test_subscribe_success(self) -> None:
        conn = FakeProcessConnection()
        t = BridgeTransport("a1", conn)
        result = await t.subscribe()
        assert result.ok is True
        assert "a1" in conn._subscribed

    @pytest.mark.asyncio
    async def test_subscribe_failure_raises(self) -> None:
        conn = FakeProcessConnection()
        conn.subscribe_result = CommandResult(ok=False, error="not found")
        t = BridgeTransport("a1", conn)
        with pytest.raises(RuntimeError, match="Subscribe failed"):
            await t.subscribe()


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


class TestWrite:
    @pytest.mark.asyncio
    async def test_forwards_to_stdin(self) -> None:
        conn = FakeProcessConnection()
        t = BridgeTransport("a1", conn)
        await t.connect()
        msg = {"type": "user_message", "text": "hello"}
        await t.write(json.dumps(msg))
        assert conn._stdin == [("a1", msg)]

    @pytest.mark.asyncio
    async def test_raises_when_dead(self) -> None:
        conn = FakeProcessConnection(alive=False)
        t = BridgeTransport("a1", conn)
        with pytest.raises(ConnectionError, match="dead"):
            await t.write('{"type": "test"}')

    @pytest.mark.asyncio
    async def test_intercepts_initialize_when_reconnecting(self) -> None:
        conn = FakeProcessConnection()
        t = BridgeTransport("a1", conn, reconnecting=True)
        await t.connect()

        init_msg = json.dumps({
            "type": "control_request",
            "request_id": "init-42",
            "request": {"subtype": "initialize"},
        })
        await t.write(init_msg)

        # Nothing sent to stdin
        assert conn._stdin == []
        # Fake response injected into queue
        result = await asyncio.wait_for(t._queue.get(), timeout=1)
        assert isinstance(result, StdoutEvent)
        assert result.data["type"] == "control_response"
        assert result.data["response"]["request_id"] == "init-42"
        assert result.data["response"]["subtype"] == "success"
        # Reconnecting flag cleared
        assert t._reconnecting is False

    @pytest.mark.asyncio
    async def test_does_not_intercept_when_not_reconnecting(self) -> None:
        conn = FakeProcessConnection()
        t = BridgeTransport("a1", conn, reconnecting=False)
        await t.connect()

        init_msg = json.dumps({
            "type": "control_request",
            "request_id": "init-99",
            "request": {"subtype": "initialize"},
        })
        await t.write(init_msg)
        # Should forward to stdin
        assert len(conn._stdin) == 1
        assert conn._stdin[0][1]["request_id"] == "init-99"

    @pytest.mark.asyncio
    async def test_non_initialize_forwarded_even_when_reconnecting(self) -> None:
        conn = FakeProcessConnection()
        t = BridgeTransport("a1", conn, reconnecting=True)
        await t.connect()

        msg = json.dumps({"type": "user_message", "text": "hi"})
        await t.write(msg)
        assert len(conn._stdin) == 1

    @pytest.mark.asyncio
    async def test_stdio_logger_called(self) -> None:
        conn = FakeProcessConnection()
        logger = logging.getLogger("test.stdio.write")
        logger.setLevel(logging.DEBUG)
        logged = []
        logger.addHandler(type("H", (logging.Handler,), {"emit": lambda self, r: logged.append(r.getMessage())})())

        t = BridgeTransport("a1", conn, stdio_logger=logger)
        await t.connect()
        await t.write(json.dumps({"type": "test"}))
        assert any("STDIN" in m for m in logged)


# ---------------------------------------------------------------------------
# read_messages
# ---------------------------------------------------------------------------


class TestReadMessages:
    @pytest.mark.asyncio
    async def test_yields_stdout_data(self) -> None:
        conn = FakeProcessConnection()
        t = BridgeTransport("a1", conn)
        await t.connect()

        conn.inject("a1", StdoutEvent(name="a1", data={"type": "msg", "n": 0}))
        conn.inject("a1", StdoutEvent(name="a1", data={"type": "msg", "n": 1}))
        conn.inject("a1", ExitEvent(name="a1", code=0))

        msgs = [data async for data in t.read_messages()]
        assert len(msgs) == 2
        assert msgs[0] == {"type": "msg", "n": 0}
        assert msgs[1] == {"type": "msg", "n": 1}
        assert t.cli_exited is True

    @pytest.mark.asyncio
    async def test_stderr_callback(self) -> None:
        lines: list[str] = []
        conn = FakeProcessConnection()
        t = BridgeTransport("a1", conn, stderr_callback=lines.append)
        await t.connect()

        conn.inject("a1", StderrEvent(name="a1", text="warning: stuff"))
        conn.inject("a1", ExitEvent(name="a1", code=0))

        async for _ in t.read_messages():
            pass
        assert lines == ["warning: stuff"]

    @pytest.mark.asyncio
    async def test_exit_sets_code(self) -> None:
        conn = FakeProcessConnection()
        t = BridgeTransport("a1", conn)
        await t.connect()

        conn.inject("a1", ExitEvent(name="a1", code=42))
        async for _ in t.read_messages():
            pass
        assert t._exit_code == 42

    @pytest.mark.asyncio
    async def test_none_sentinel_raises_connection_error(self) -> None:
        conn = FakeProcessConnection()
        t = BridgeTransport("a1", conn)
        await t.connect()

        conn.inject("a1", None)
        with pytest.raises(ConnectionError, match="connection lost"):
            async for _ in t.read_messages():
                pass

    @pytest.mark.asyncio
    async def test_returns_immediately_if_no_queue(self) -> None:
        conn = FakeProcessConnection()
        t = BridgeTransport("a1", conn)
        # Don't call connect() — no queue
        msgs = [data async for data in t.read_messages()]
        assert msgs == []

    @pytest.mark.asyncio
    async def test_raises_when_dead(self) -> None:
        conn = FakeProcessConnection(alive=False)
        t = BridgeTransport("a1", conn)
        t._queue = asyncio.Queue()  # manually set queue without connect
        t._ready = True
        with pytest.raises(ConnectionError, match="dead"):
            async for _ in t.read_messages():
                pass

    @pytest.mark.asyncio
    async def test_stdio_logger_logs_stdout_and_stderr(self) -> None:
        logged: list[str] = []
        logger = logging.getLogger("test.stdio.read")
        logger.setLevel(logging.DEBUG)
        logger.handlers.clear()
        logger.addHandler(type("H", (logging.Handler,), {"emit": lambda self, r: logged.append(r.getMessage())})())

        conn = FakeProcessConnection()
        t = BridgeTransport("a1", conn, stdio_logger=logger)
        await t.connect()

        conn.inject("a1", StdoutEvent(name="a1", data={"type": "test"}))
        conn.inject("a1", StderrEvent(name="a1", text="err line"))
        conn.inject("a1", ExitEvent(name="a1", code=0))

        async for _ in t.read_messages():
            pass

        assert any("STDOUT" in m for m in logged)
        assert any("STDERR" in m for m in logged)
        assert any("EXIT" in m for m in logged)


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


class TestClose:
    @pytest.mark.asyncio
    async def test_kills_and_unregisters(self) -> None:
        conn = FakeProcessConnection()
        t = BridgeTransport("a1", conn)
        await t.connect()
        await t.close()
        assert "a1" in conn._killed
        assert "a1" in conn._unregistered
        assert t.is_ready() is False

    @pytest.mark.asyncio
    async def test_skips_kill_when_already_exited(self) -> None:
        conn = FakeProcessConnection()
        t = BridgeTransport("a1", conn)
        await t.connect()
        t._cli_exited = True
        await t.close()
        assert conn._killed == []
        assert "a1" in conn._unregistered

    @pytest.mark.asyncio
    async def test_close_before_connect(self) -> None:
        conn = FakeProcessConnection()
        t = BridgeTransport("a1", conn)
        await t.close()
        assert conn._killed == []
        assert "a1" in conn._unregistered

    @pytest.mark.asyncio
    async def test_close_survives_kill_exception(self) -> None:
        """If kill() raises, close() still unregisters without propagating."""

        class FailKillConn(FakeProcessConnection):
            async def kill(self, name: str) -> CommandResult:
                raise RuntimeError("kill failed")

        conn = FailKillConn()
        t = BridgeTransport("a1", conn)
        await t.connect()
        await t.close()  # should not raise
        assert "a1" in conn._unregistered
        assert t.is_ready() is False


# ---------------------------------------------------------------------------
# end_input / cli_exited
# ---------------------------------------------------------------------------


class TestMisc:
    @pytest.mark.asyncio
    async def test_end_input_is_noop(self) -> None:
        conn = FakeProcessConnection()
        t = BridgeTransport("a1", conn)
        await t.end_input()  # should not raise

    def test_cli_exited_default(self) -> None:
        conn = FakeProcessConnection()
        t = BridgeTransport("a1", conn)
        assert t.cli_exited is False
