"""Unit tests for ShutdownCoordinator using mock callbacks."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from axi.shutdown import ShutdownCoordinator


def _make_session(name: str, *, busy: bool = False, has_client: bool = True) -> MagicMock:
    """Create a mock session with configurable state."""
    session = MagicMock()
    session.name = name
    session.client = object() if has_client else None
    session.flowcoder_process = None
    lock = asyncio.Lock()
    if busy:
        # Simulate a locked query_lock without actually holding it in a coroutine
        lock._locked = True
    session.query_lock = lock
    return session


class TestGetBusyAgents:
    def test_no_agents(self) -> None:
        coord = ShutdownCoordinator(
            agents={}, sleep_fn=AsyncMock(), close_bot_fn=AsyncMock()
        )
        assert coord.get_busy_agents() == {}

    def test_idle_agents_not_busy(self) -> None:
        agents = {"a": _make_session("a"), "b": _make_session("b")}
        coord = ShutdownCoordinator(
            agents=agents, sleep_fn=AsyncMock(), close_bot_fn=AsyncMock()
        )
        assert coord.get_busy_agents() == {}

    def test_busy_agent_detected(self) -> None:
        agents = {"a": _make_session("a"), "b": _make_session("b", busy=True)}
        coord = ShutdownCoordinator(
            agents=agents, sleep_fn=AsyncMock(), close_bot_fn=AsyncMock()
        )
        busy = coord.get_busy_agents()
        assert "b" in busy
        assert "a" not in busy

    def test_skip_excludes_agent(self) -> None:
        agents = {"a": _make_session("a", busy=True), "b": _make_session("b", busy=True)}
        coord = ShutdownCoordinator(
            agents=agents, sleep_fn=AsyncMock(), close_bot_fn=AsyncMock()
        )
        busy = coord.get_busy_agents(skip="a")
        assert "a" not in busy
        assert "b" in busy


class TestSleepAll:
    @pytest.mark.asyncio
    async def test_sleeps_awake_agents(self) -> None:
        sleep_fn = AsyncMock()
        agents = {"a": _make_session("a"), "b": _make_session("b")}
        coord = ShutdownCoordinator(
            agents=agents, sleep_fn=sleep_fn, close_bot_fn=AsyncMock()
        )
        await coord.sleep_all()
        assert sleep_fn.call_count == 2

    @pytest.mark.asyncio
    async def test_skips_named_agent(self) -> None:
        sleep_fn = AsyncMock()
        agents = {"a": _make_session("a"), "b": _make_session("b")}
        coord = ShutdownCoordinator(
            agents=agents, sleep_fn=sleep_fn, close_bot_fn=AsyncMock()
        )
        await coord.sleep_all(skip="a")
        assert sleep_fn.call_count == 1
        sleep_fn.assert_called_once_with(agents["b"])

    @pytest.mark.asyncio
    async def test_skips_sleeping_agents(self) -> None:
        sleep_fn = AsyncMock()
        agents = {"a": _make_session("a", has_client=False), "b": _make_session("b")}
        coord = ShutdownCoordinator(
            agents=agents, sleep_fn=sleep_fn, close_bot_fn=AsyncMock()
        )
        await coord.sleep_all()
        assert sleep_fn.call_count == 1
        sleep_fn.assert_called_once_with(agents["b"])

    @pytest.mark.asyncio
    async def test_error_in_one_agent_doesnt_block_others(self) -> None:
        sleep_fn = AsyncMock(side_effect=[Exception("boom"), None])
        agents = {"a": _make_session("a"), "b": _make_session("b")}
        coord = ShutdownCoordinator(
            agents=agents, sleep_fn=sleep_fn, close_bot_fn=AsyncMock()
        )
        await coord.sleep_all()
        assert sleep_fn.call_count == 2


class TestRequestedState:
    def test_initially_false(self) -> None:
        coord = ShutdownCoordinator(
            agents={}, sleep_fn=AsyncMock(), close_bot_fn=AsyncMock()
        )
        assert coord.requested is False

    @pytest.mark.asyncio
    async def test_graceful_sets_requested(self) -> None:
        kill_fn = MagicMock()
        coord = ShutdownCoordinator(
            agents={},
            sleep_fn=AsyncMock(),
            close_bot_fn=AsyncMock(),
            kill_fn=kill_fn,
        )
        await coord.graceful_shutdown("test")
        assert coord.requested is True

    @pytest.mark.asyncio
    async def test_force_sets_requested(self) -> None:
        kill_fn = MagicMock()
        coord = ShutdownCoordinator(
            agents={},
            sleep_fn=AsyncMock(),
            close_bot_fn=AsyncMock(),
            kill_fn=kill_fn,
        )
        await coord.force_shutdown("test")
        assert coord.requested is True

    @pytest.mark.asyncio
    async def test_duplicate_graceful_is_noop(self) -> None:
        kill_fn = MagicMock()
        coord = ShutdownCoordinator(
            agents={},
            sleep_fn=AsyncMock(),
            close_bot_fn=AsyncMock(),
            kill_fn=kill_fn,
        )
        await coord.graceful_shutdown("first")
        kill_fn.reset_mock()
        await coord.graceful_shutdown("second")
        # Second graceful call should be a no-op
        kill_fn.assert_not_called()


class TestGracefulShutdown:
    @pytest.mark.asyncio
    async def test_no_busy_agents_exits_immediately(self) -> None:
        kill_fn = MagicMock()
        close_fn = AsyncMock()
        coord = ShutdownCoordinator(
            agents={"a": _make_session("a")},
            sleep_fn=AsyncMock(),
            close_bot_fn=close_fn,
            kill_fn=kill_fn,
        )
        await coord.graceful_shutdown("test")
        close_fn.assert_awaited_once()
        kill_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_goodbye_fn_called(self) -> None:
        goodbye_fn = AsyncMock()
        coord = ShutdownCoordinator(
            agents={},
            sleep_fn=AsyncMock(),
            close_bot_fn=AsyncMock(),
            kill_fn=MagicMock(),
            goodbye_fn=goodbye_fn,
        )
        await coord.graceful_shutdown("test")
        goodbye_fn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_bridge_mode_skips_sleep(self) -> None:
        sleep_fn = AsyncMock()
        coord = ShutdownCoordinator(
            agents={"a": _make_session("a")},
            sleep_fn=sleep_fn,
            close_bot_fn=AsyncMock(),
            kill_fn=MagicMock(),
            bridge_mode=True,
        )
        await coord.graceful_shutdown("test")
        sleep_fn.assert_not_called()


class TestForceShutdown:
    @pytest.mark.asyncio
    async def test_exits_immediately(self) -> None:
        kill_fn = MagicMock()
        close_fn = AsyncMock()
        coord = ShutdownCoordinator(
            agents={"a": _make_session("a", busy=True)},
            sleep_fn=AsyncMock(),
            close_bot_fn=close_fn,
            kill_fn=kill_fn,
        )
        await coord.force_shutdown("test")
        close_fn.assert_awaited_once()
        kill_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_sleeps_agents_even_if_busy(self) -> None:
        sleep_fn = AsyncMock()
        coord = ShutdownCoordinator(
            agents={"a": _make_session("a", busy=True)},
            sleep_fn=sleep_fn,
            close_bot_fn=AsyncMock(),
            kill_fn=MagicMock(),
        )
        await coord.force_shutdown("test")
        sleep_fn.assert_awaited_once()
