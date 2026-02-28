"""Tests for BridgeFlowcoderProcess and flowcoder sleep/wake behavior.

Covers the bridge-backed flowcoder engine wrapper and its interaction with
sleep_agent(). All bridge I/O is mocked — no real sockets or subprocesses.

Run with: pytest test_flowcoder_bridge.py -v
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bridge.protocol import ExitMsg, ResultMsg, StderrMsg, StdoutMsg
from flowcoder import BridgeFlowcoderProcess, FlowcoderProcess

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_result(**kwargs) -> ResultMsg:
    """Build a ResultMsg with defaults."""
    defaults = {"ok": True, "name": ""}
    defaults.update(kwargs)
    return ResultMsg(**defaults)


def make_conn() -> MagicMock:
    """Build a mock BridgeConnection."""
    conn = MagicMock()
    conn.is_alive = True
    conn.send_command = AsyncMock(return_value=make_result())
    conn.send_stdin = AsyncMock()
    conn.register_agent = MagicMock(return_value=asyncio.Queue())
    conn.unregister_agent = MagicMock()
    return conn


def make_proc(conn=None, **kwargs) -> BridgeFlowcoderProcess:
    """Build a BridgeFlowcoderProcess with mock conn."""
    defaults = dict(
        bridge_name="test-agent:flowcoder",
        conn=conn or make_conn(),
        command="test-cmd",
        args="arg1",
        cwd="/tmp/test",
    )
    defaults.update(kwargs)
    return BridgeFlowcoderProcess(**defaults)


# ---------------------------------------------------------------------------
# BridgeFlowcoderProcess: start
# ---------------------------------------------------------------------------


class TestBridgeStart:
    @pytest.mark.asyncio
    async def test_start_spawns_and_subscribes(self):
        """start() sends spawn + subscribe commands with correct args."""
        conn = make_conn()
        proc = make_proc(conn=conn)

        await proc.start()

        assert conn.register_agent.call_count == 1
        conn.register_agent.assert_called_with("test-agent:flowcoder")

        # spawn called with cli_args containing the engine command
        spawn_call = conn.send_command.call_args_list[0]
        assert spawn_call[0][0] == "spawn"
        assert spawn_call[1]["name"] == "test-agent:flowcoder"
        assert "--command" in spawn_call[1]["cli_args"]
        assert "test-cmd" in spawn_call[1]["cli_args"]
        assert "--args" in spawn_call[1]["cli_args"]
        assert "arg1" in spawn_call[1]["cli_args"]
        assert spawn_call[1]["cwd"] == "/tmp/test"

        # subscribe called second
        sub_call = conn.send_command.call_args_list[1]
        assert sub_call[0][0] == "subscribe"
        assert sub_call[1]["name"] == "test-agent:flowcoder"

        assert proc.is_running

    @pytest.mark.asyncio
    async def test_start_already_running(self):
        """Handles already_running=True from bridge."""
        conn = make_conn()
        conn.send_command = AsyncMock(return_value=make_result(already_running=True))
        proc = make_proc(conn=conn)

        await proc.start()
        assert proc.is_running

    @pytest.mark.asyncio
    async def test_start_spawn_failure_raises(self):
        """RuntimeError on ok=False from spawn."""
        conn = make_conn()
        conn.send_command = AsyncMock(return_value=make_result(ok=False, error="spawn failed"))
        proc = make_proc(conn=conn)

        with pytest.raises(RuntimeError, match="spawn failed"):
            await proc.start()

        assert not proc.is_running
        conn.unregister_agent.assert_called_once()


# ---------------------------------------------------------------------------
# BridgeFlowcoderProcess: subscribe (reconnect)
# ---------------------------------------------------------------------------


class TestBridgeSubscribe:
    @pytest.mark.asyncio
    async def test_subscribe_registers_and_subscribes(self):
        """subscribe() registers agent and sends subscribe command."""
        conn = make_conn()
        conn.send_command = AsyncMock(return_value=make_result(replayed=5, status="running"))
        proc = make_proc(conn=conn)

        result = await proc.subscribe()

        conn.register_agent.assert_called_once_with("test-agent:flowcoder")
        conn.send_command.assert_called_once_with("subscribe", name="test-agent:flowcoder")
        assert result.replayed == 5
        assert proc.is_running

    @pytest.mark.asyncio
    async def test_subscribe_failure_raises(self):
        """RuntimeError on subscribe failure."""
        conn = make_conn()
        conn.send_command = AsyncMock(return_value=make_result(ok=False, error="not found"))
        proc = make_proc(conn=conn)

        with pytest.raises(RuntimeError, match="not found"):
            await proc.subscribe()
        assert not proc.is_running


# ---------------------------------------------------------------------------
# BridgeFlowcoderProcess: messages
# ---------------------------------------------------------------------------


class TestBridgeMessages:
    @pytest.mark.asyncio
    async def test_messages_yields_stdout(self):
        """StdoutMsg.data yielded as dicts."""
        conn = make_conn()
        q = asyncio.Queue()
        conn.register_agent = MagicMock(return_value=q)
        proc = make_proc(conn=conn)
        await proc.start()

        # Enqueue some messages
        await q.put(StdoutMsg(name="test-agent:flowcoder", data={"type": "block_start", "id": "1"}))
        await q.put(StdoutMsg(name="test-agent:flowcoder", data={"type": "block_complete", "id": "1"}))
        await q.put(ExitMsg(name="test-agent:flowcoder", code=0))

        collected = []
        async for msg in proc.messages():
            collected.append(msg)

        assert len(collected) == 2
        assert collected[0] == {"type": "block_start", "id": "1"}
        assert collected[1] == {"type": "block_complete", "id": "1"}

    @pytest.mark.asyncio
    async def test_messages_stops_on_exit(self):
        """ExitMsg ends iteration."""
        conn = make_conn()
        q = asyncio.Queue()
        conn.register_agent = MagicMock(return_value=q)
        proc = make_proc(conn=conn)
        await proc.start()

        await q.put(ExitMsg(name="test-agent:flowcoder", code=1))

        collected = []
        async for msg in proc.messages():
            collected.append(msg)

        assert collected == []
        assert not proc.is_running

    @pytest.mark.asyncio
    async def test_messages_stops_on_connection_loss(self):
        """None sentinel ends iteration."""
        conn = make_conn()
        q = asyncio.Queue()
        conn.register_agent = MagicMock(return_value=q)
        proc = make_proc(conn=conn)
        await proc.start()

        await q.put(None)

        collected = []
        async for msg in proc.messages():
            collected.append(msg)

        assert collected == []
        assert not proc.is_running

    @pytest.mark.asyncio
    async def test_messages_logs_stderr(self):
        """StderrMsg is logged, not yielded."""
        conn = make_conn()
        q = asyncio.Queue()
        conn.register_agent = MagicMock(return_value=q)
        proc = make_proc(conn=conn)
        await proc.start()

        await q.put(StderrMsg(name="test-agent:flowcoder", text="warning: something"))
        await q.put(StdoutMsg(name="test-agent:flowcoder", data={"type": "ok"}))
        await q.put(ExitMsg(name="test-agent:flowcoder", code=0))

        collected = []
        async for msg in proc.messages():
            collected.append(msg)

        assert len(collected) == 1
        assert collected[0] == {"type": "ok"}


# ---------------------------------------------------------------------------
# BridgeFlowcoderProcess: send
# ---------------------------------------------------------------------------


class TestBridgeSend:
    @pytest.mark.asyncio
    async def test_send_forwards_to_bridge(self):
        """send_stdin called with correct args."""
        conn = make_conn()
        proc = make_proc(conn=conn)
        await proc.start()

        await proc.send({"type": "user", "message": "hello"})

        conn.send_stdin.assert_called_once_with(
            "test-agent:flowcoder",
            {"type": "user", "message": "hello"},
        )


# ---------------------------------------------------------------------------
# BridgeFlowcoderProcess: stop / detach
# ---------------------------------------------------------------------------


class TestBridgeStopDetach:
    @pytest.mark.asyncio
    async def test_stop_sends_shutdown_then_kill(self):
        """shutdown message + kill command + unregister."""
        conn = make_conn()
        proc = make_proc(conn=conn)
        await proc.start()

        # Reset mocks to track stop calls only
        conn.send_stdin.reset_mock()
        conn.send_command.reset_mock()

        await proc.stop()

        # shutdown sent via send_stdin
        conn.send_stdin.assert_called_once_with(
            "test-agent:flowcoder",
            {"type": "shutdown"},
        )
        # kill command sent
        conn.send_command.assert_called_once_with("kill", name="test-agent:flowcoder")
        # unregistered
        conn.unregister_agent.assert_called_with("test-agent:flowcoder")
        assert not proc.is_running

    @pytest.mark.asyncio
    async def test_detach_unregisters_without_kill(self):
        """unregister_agent called, kill NOT called."""
        conn = make_conn()
        proc = make_proc(conn=conn)
        await proc.start()

        conn.send_command.reset_mock()

        await proc.detach()

        conn.unregister_agent.assert_called_with("test-agent:flowcoder")
        # unsubscribe called, but NOT kill
        conn.send_command.assert_called_once_with("unsubscribe", name="test-agent:flowcoder")

    @pytest.mark.asyncio
    async def test_is_running_tracks_state(self):
        """True after start, False after stop."""
        conn = make_conn()
        proc = make_proc(conn=conn)

        assert not proc.is_running

        await proc.start()
        assert proc.is_running

        await proc.stop()
        assert not proc.is_running


# ---------------------------------------------------------------------------
# is_bridge_backed property
# ---------------------------------------------------------------------------


class TestIsBridgeBacked:
    def test_direct_process_not_bridge_backed(self):
        proc = FlowcoderProcess(command="test")
        assert not proc.is_bridge_backed

    def test_bridge_process_is_bridge_backed(self):
        conn = make_conn()
        proc = make_proc(conn=conn)
        assert proc.is_bridge_backed


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


class TestBuildEngineCmd:
    def test_basic_command(self):
        from flowcoder import build_engine_cmd

        cmd = build_engine_cmd("my-flow")
        assert "--command" in cmd
        assert "my-flow" in cmd

    def test_with_args(self):
        from flowcoder import build_engine_cmd

        cmd = build_engine_cmd("my-flow", args="hello world")
        assert "--args" in cmd
        idx = cmd.index("--args")
        assert cmd[idx + 1] == "hello world"

    def test_with_search_paths(self):
        from flowcoder import build_engine_cmd

        cmd = build_engine_cmd("my-flow", search_paths=["/extra/path"])
        # Should have default search path + the extra one
        sp_indices = [i for i, x in enumerate(cmd) if x == "--search-path"]
        assert len(sp_indices) >= 2  # default + custom

    def test_without_args(self):
        from flowcoder import build_engine_cmd

        cmd = build_engine_cmd("my-flow")
        assert "--args" not in cmd


class TestBuildEngineEnv:
    def test_strips_claude_vars(self):
        from flowcoder import build_engine_env

        with patch.dict("os.environ", {"CLAUDECODE": "1", "PATH": "/usr/bin"}):
            env = build_engine_env()
            assert "CLAUDECODE" not in env
            assert "PATH" in env


# ---------------------------------------------------------------------------
# Sleep/wake behavior
# ---------------------------------------------------------------------------


@dataclass
class FakeAgent:
    """Minimal agent session for sleep tests."""

    name: str
    agent_type: str = "flowcoder"
    flowcoder_process: object = None
    query_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    client: object = None
    _reconnecting: bool = False
    _bridge_busy: bool = False
    _log: object = None


class TestSleepFlowcoder:
    """Tests for sleep_agent() interaction with flowcoder processes."""

    @pytest.mark.asyncio
    async def test_sleep_mid_execution_bridge_detaches(self):
        """Bridge-backed + query_lock locked -> detach() called."""
        conn = make_conn()
        proc = make_proc(conn=conn)
        await proc.start()

        agent = FakeAgent(name="test-agent")
        agent.flowcoder_process = proc

        # Simulate mid-execution: lock is held
        await agent.query_lock.acquire()

        try:
            # Simulate sleep_agent logic for bridge-backed flowcoder
            if agent.flowcoder_process:
                p = agent.flowcoder_process
                if getattr(p, "is_bridge_backed", False) and agent.query_lock.locked():
                    await p.detach()
                else:
                    await p.stop()
                agent.flowcoder_process = None
        finally:
            agent.query_lock.release()

        # detach was called (unsubscribe + unregister, no kill)
        conn.unregister_agent.assert_called_with("test-agent:flowcoder")
        assert agent.flowcoder_process is None

    @pytest.mark.asyncio
    async def test_sleep_idle_bridge_stops(self):
        """Bridge-backed + unlocked -> stop() called."""
        conn = make_conn()
        proc = make_proc(conn=conn)
        await proc.start()

        agent = FakeAgent(name="test-agent")
        agent.flowcoder_process = proc

        # Lock is NOT held (idle)
        assert not agent.query_lock.locked()

        conn.send_command.reset_mock()
        conn.send_stdin.reset_mock()

        # Simulate sleep_agent logic
        if agent.flowcoder_process:
            p = agent.flowcoder_process
            if getattr(p, "is_bridge_backed", False) and agent.query_lock.locked():
                await p.detach()
            else:
                await p.stop()
            agent.flowcoder_process = None

        # stop was called (shutdown + kill)
        conn.send_stdin.assert_called_once()  # shutdown message
        conn.send_command.assert_called_once()  # kill command
        assert agent.flowcoder_process is None

    @pytest.mark.asyncio
    async def test_sleep_direct_always_stops(self):
        """Non-bridge FlowcoderProcess -> stop() called."""
        proc = FlowcoderProcess(command="test")
        # Mock the subprocess
        proc._proc = MagicMock()
        proc._proc.returncode = None
        proc._proc.stdin = MagicMock()
        proc._proc.stdin.is_closing = MagicMock(return_value=True)
        proc._proc.terminate = MagicMock()
        proc._proc.kill = MagicMock()
        proc._proc.wait = AsyncMock(return_value=0)
        proc._proc.returncode = 0  # after wait

        agent = FakeAgent(name="test-agent")
        agent.flowcoder_process = proc

        # Lock held (mid-execution) but direct process → still stops
        await agent.query_lock.acquire()

        try:
            if agent.flowcoder_process:
                p = agent.flowcoder_process
                if getattr(p, "is_bridge_backed", False) and agent.query_lock.locked():
                    await p.detach()
                else:
                    await p.stop()
                agent.flowcoder_process = None
        finally:
            agent.query_lock.release()

        assert not proc.is_bridge_backed
        assert agent.flowcoder_process is None

    @pytest.mark.asyncio
    async def test_sleep_inline_flowcoder_on_claude_agent(self):
        """claude_code agent with active inline flowchart — flowcoder handled before Claude."""
        conn = make_conn()
        proc = make_proc(conn=conn)
        await proc.start()

        agent = FakeAgent(name="test-agent", agent_type="claude_code")
        agent.client = "fake-client"
        agent.flowcoder_process = proc

        # Not locked (inline flowchart finished or idle)
        conn.send_command.reset_mock()

        # Simulate the enhanced sleep_agent logic:
        # Handle flowcoder process first
        if agent.flowcoder_process:
            p = agent.flowcoder_process
            if getattr(p, "is_bridge_backed", False) and agent.query_lock.locked():
                await p.detach()
            else:
                await p.stop()
            agent.flowcoder_process = None

        # Then the Claude part would be handled (not tested here, just verifying
        # flowcoder cleanup happened)
        assert agent.flowcoder_process is None
        assert agent.client is not None  # Claude client untouched


# ---------------------------------------------------------------------------
# Reconnect with status_request
# ---------------------------------------------------------------------------


class TestReconnectStatusRequest:
    """Tests for reconnect using engine status_request instead of bridge idle flag."""

    @pytest.mark.asyncio
    async def test_reconnect_sends_status_request(self):
        """subscribe() + send(status_request) is the reconnect flow."""
        conn = make_conn()
        conn.send_command = AsyncMock(return_value=make_result(replayed=0, status="running"))
        proc = make_proc(conn=conn)

        # Simulate reconnect: subscribe, then send status_request
        await proc.subscribe()
        await proc.send({"type": "status_request"})

        conn.send_stdin.assert_called_once_with(
            "test-agent:flowcoder",
            {"type": "status_request"},
        )

    @pytest.mark.asyncio
    async def test_status_response_busy_false_ends_stream(self):
        """status_response(busy=false) breaks the messages() loop."""
        conn = make_conn()
        q = asyncio.Queue()
        conn.register_agent = MagicMock(return_value=q)
        proc = make_proc(conn=conn)
        await proc.start()

        # Engine responds: not busy (idle after flowchart)
        await q.put(StdoutMsg(name="test-agent:flowcoder", data={"type": "status_response", "busy": False}))

        collected = []
        async for msg in proc.messages():
            if msg.get("type") == "status_response" and not msg.get("busy", False):
                break
            collected.append(msg)

        # Nothing was collected — status_response broke immediately
        assert collected == []

    @pytest.mark.asyncio
    async def test_status_response_busy_true_continues_stream(self):
        """status_response(busy=true) doesn't break — flowchart output follows."""
        conn = make_conn()
        q = asyncio.Queue()
        conn.register_agent = MagicMock(return_value=q)
        proc = make_proc(conn=conn)
        await proc.start()

        # Engine is busy, then sends some output, then result, then idle status
        await q.put(StdoutMsg(name="test-agent:flowcoder", data={"type": "status_response", "busy": True}))
        await q.put(
            StdoutMsg(
                name="test-agent:flowcoder",
                data={"type": "system", "subtype": "block_start", "data": {"block_name": "step1"}},
            )
        )
        await q.put(StdoutMsg(name="test-agent:flowcoder", data={"type": "result", "result": "done"}))
        await q.put(StdoutMsg(name="test-agent:flowcoder", data={"type": "status_response", "busy": False}))

        collected = []
        async for msg in proc.messages():
            if msg.get("type") == "status_response" and not msg.get("busy", False):
                break
            if msg.get("type") != "status_response":
                collected.append(msg)

        assert len(collected) == 2
        assert collected[0]["subtype"] == "block_start"
        assert collected[1]["type"] == "result"

    @pytest.mark.asyncio
    async def test_reconnect_with_replayed_output(self):
        """Replayed messages + status_response(busy=false) = reconnect to finished flowchart."""
        conn = make_conn()
        q = asyncio.Queue()
        conn.register_agent = MagicMock(return_value=q)
        conn.send_command = AsyncMock(return_value=make_result(replayed=2, status="running"))
        proc = make_proc(conn=conn)

        # Simulate reconnect
        sub_result = await proc.subscribe()
        assert sub_result.replayed == 2

        # Replayed messages arrive in queue (bridge replays them on subscribe)
        await q.put(StdoutMsg(name="test-agent:flowcoder", data={"type": "result", "result": "done"}))
        # Then the status_request response arrives
        await q.put(StdoutMsg(name="test-agent:flowcoder", data={"type": "status_response", "busy": False}))

        collected = []
        async for msg in proc.messages():
            if msg.get("type") == "status_response" and not msg.get("busy", False):
                break
            if msg.get("type") != "status_response":
                collected.append(msg)

        assert len(collected) == 1
        assert collected[0]["type"] == "result"
