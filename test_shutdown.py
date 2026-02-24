"""Pure unit tests for the ShutdownCoordinator.

All side effects are mocked — no Discord, no subprocess, no os._exit.
Run with: pytest test_shutdown.py -v
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shutdown import ShutdownCoordinator


# ---------------------------------------------------------------------------
# Fake agent session — satisfies the Sleepable protocol
# ---------------------------------------------------------------------------

@dataclass
class FakeAgent:
    name: str
    client: object | None = "fake-client"
    query_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_coordinator(
    agents: dict[str, FakeAgent] | None = None,
    sleep_fn: AsyncMock | None = None,
    close_bot_fn: AsyncMock | None = None,
    kill_fn: MagicMock | None = None,
    notify_fn: AsyncMock | None = None,
    deadline_timeout: float = 999,    # high default so deadline thread doesn't fire
    bot_close_timeout: float = 5,
) -> ShutdownCoordinator:
    return ShutdownCoordinator(
        agents=agents or {},
        sleep_fn=sleep_fn or AsyncMock(),
        close_bot_fn=close_bot_fn or AsyncMock(),
        kill_fn=kill_fn or MagicMock(),
        notify_fn=notify_fn or AsyncMock(),
        deadline_timeout=deadline_timeout,
        bot_close_timeout=bot_close_timeout,
    )


# ---------------------------------------------------------------------------
# Tests: get_busy_agents
# ---------------------------------------------------------------------------

class TestGetBusyAgents:
    def test_empty(self):
        c = make_coordinator(agents={})
        assert c.get_busy_agents() == {}

    def test_no_busy(self):
        agents = {"a": FakeAgent("a"), "b": FakeAgent("b")}
        c = make_coordinator(agents=agents)
        assert c.get_busy_agents() == {}

    @pytest.mark.asyncio
    async def test_detects_busy(self):
        agent = FakeAgent("a")
        await agent.query_lock.acquire()
        try:
            c = make_coordinator(agents={"a": agent})
            busy = c.get_busy_agents()
            assert "a" in busy
        finally:
            agent.query_lock.release()

    @pytest.mark.asyncio
    async def test_skip_agent(self):
        a = FakeAgent("master")
        b = FakeAgent("worker")
        await a.query_lock.acquire()
        await b.query_lock.acquire()
        try:
            c = make_coordinator(agents={"master": a, "worker": b})
            busy = c.get_busy_agents(skip="master")
            assert "master" not in busy
            assert "worker" in busy
        finally:
            a.query_lock.release()
            b.query_lock.release()


# ---------------------------------------------------------------------------
# Tests: sleep_all
# ---------------------------------------------------------------------------

class TestSleepAll:
    @pytest.mark.asyncio
    async def test_sleeps_all_awake(self):
        a = FakeAgent("a", client="alive")
        b = FakeAgent("b", client="alive")
        sleep_fn = AsyncMock()
        c = make_coordinator(agents={"a": a, "b": b}, sleep_fn=sleep_fn)

        await c.sleep_all()

        assert sleep_fn.call_count == 2
        slept_names = {call.args[0].name for call in sleep_fn.call_args_list}
        assert slept_names == {"a", "b"}

    @pytest.mark.asyncio
    async def test_skips_sleeping_agents(self):
        a = FakeAgent("a", client="alive")
        b = FakeAgent("b", client=None)  # already sleeping
        sleep_fn = AsyncMock()
        c = make_coordinator(agents={"a": a, "b": b}, sleep_fn=sleep_fn)

        await c.sleep_all()

        assert sleep_fn.call_count == 1
        assert sleep_fn.call_args_list[0].args[0].name == "a"

    @pytest.mark.asyncio
    async def test_skip_agent(self):
        a = FakeAgent("master", client="alive")
        b = FakeAgent("worker", client="alive")
        sleep_fn = AsyncMock()
        c = make_coordinator(agents={"master": a, "worker": b}, sleep_fn=sleep_fn)

        await c.sleep_all(skip="master")

        assert sleep_fn.call_count == 1
        assert sleep_fn.call_args_list[0].args[0].name == "worker"

    @pytest.mark.asyncio
    async def test_exception_in_sleep_continues(self):
        """One agent failing to sleep should not prevent others from being slept."""
        a = FakeAgent("a", client="alive")
        b = FakeAgent("b", client="alive")
        sleep_fn = AsyncMock(side_effect=[RuntimeError("boom"), None])
        c = make_coordinator(agents={"a": a, "b": b}, sleep_fn=sleep_fn)

        await c.sleep_all()  # should not raise

        assert sleep_fn.call_count == 2


# ---------------------------------------------------------------------------
# Tests: graceful_shutdown — no busy agents
# ---------------------------------------------------------------------------

class TestGracefulShutdownNoBusy:
    @pytest.mark.asyncio
    @patch("shutdown._start_deadline_thread")
    async def test_exits_immediately_when_no_agents_busy(self, mock_deadline):
        kill_fn = MagicMock()
        sleep_fn = AsyncMock()
        close_fn = AsyncMock()
        agents = {"a": FakeAgent("a"), "b": FakeAgent("b")}
        c = make_coordinator(
            agents=agents, sleep_fn=sleep_fn, close_bot_fn=close_fn, kill_fn=kill_fn
        )

        await c.graceful_shutdown("test")

        assert c.requested is True
        # sleep_all should have been called (both agents have client != None)
        assert sleep_fn.call_count == 2
        close_fn.assert_awaited_once()
        kill_fn.assert_called_once()
        mock_deadline.assert_called_once()

    @pytest.mark.asyncio
    @patch("shutdown._start_deadline_thread")
    async def test_skips_master_in_sleep(self, mock_deadline):
        """When triggered by master, sleep_all should skip master."""
        kill_fn = MagicMock()
        sleep_fn = AsyncMock()
        close_fn = AsyncMock()
        agents = {"master": FakeAgent("master"), "worker": FakeAgent("worker")}
        c = make_coordinator(
            agents=agents, sleep_fn=sleep_fn, close_bot_fn=close_fn, kill_fn=kill_fn
        )

        await c.graceful_shutdown("test", skip_agent="master")

        # Only worker should be slept
        assert sleep_fn.call_count == 1
        assert sleep_fn.call_args_list[0].args[0].name == "worker"

    @pytest.mark.asyncio
    @patch("shutdown._start_deadline_thread")
    async def test_idempotent(self, mock_deadline):
        """Second call should be a no-op."""
        kill_fn = MagicMock()
        c = make_coordinator(kill_fn=kill_fn)

        await c.graceful_shutdown("first")
        await c.graceful_shutdown("second")

        kill_fn.assert_called_once()

    @pytest.mark.asyncio
    @patch("shutdown._start_deadline_thread")
    async def test_empty_agents_dict(self, mock_deadline):
        kill_fn = MagicMock()
        close_fn = AsyncMock()
        c = make_coordinator(agents={}, close_bot_fn=close_fn, kill_fn=kill_fn)

        await c.graceful_shutdown("test")

        close_fn.assert_awaited_once()
        kill_fn.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: graceful_shutdown — busy agents
# ---------------------------------------------------------------------------

class TestGracefulShutdownBusy:
    @pytest.mark.asyncio
    @patch("shutdown._start_deadline_thread")
    async def test_waits_for_busy_agent_then_exits(self, mock_deadline):
        """Shutdown waits for a busy agent, then exits when it finishes."""
        worker = FakeAgent("worker")
        await worker.query_lock.acquire()
        kill_fn = MagicMock()
        sleep_fn = AsyncMock()
        close_fn = AsyncMock()
        notify_fn = AsyncMock()
        agents = {"worker": worker}
        c = make_coordinator(
            agents=agents, sleep_fn=sleep_fn, close_bot_fn=close_fn,
            kill_fn=kill_fn, notify_fn=notify_fn,
        )

        # Release the lock after a short delay (simulates agent finishing)
        async def release_later():
            await asyncio.sleep(0.05)
            worker.query_lock.release()

        asyncio.create_task(release_later())

        await c.graceful_shutdown("test")

        kill_fn.assert_called_once()
        # Notification should have been sent
        notify_fn.assert_any_call("worker", "Restart pending — waiting for **worker** to finish current task...")

    @pytest.mark.asyncio
    @patch("shutdown.POLL_INTERVAL", 0.01)
    @patch("shutdown._start_deadline_thread")
    async def test_skip_agent_not_waited_on(self, mock_deadline):
        """The skip_agent should not be waited on even if its lock is held."""
        master = FakeAgent("master")
        await master.query_lock.acquire()
        kill_fn = MagicMock()
        agents = {"master": master}
        c = make_coordinator(agents=agents, kill_fn=kill_fn)

        # If skip_agent is working correctly, this should exit immediately
        # (no busy agents after excluding master)
        await c.graceful_shutdown("test", skip_agent="master")

        kill_fn.assert_called_once()
        master.query_lock.release()

    @pytest.mark.asyncio
    @patch("shutdown.POLL_INTERVAL", 0.01)
    @patch("shutdown.STATUS_INTERVAL", 0.02)
    @patch("shutdown._start_deadline_thread")
    async def test_sends_periodic_status(self, mock_deadline):
        """Status updates should be sent to busy agents periodically."""
        worker = FakeAgent("worker")
        await worker.query_lock.acquire()
        notify_fn = AsyncMock()
        kill_fn = MagicMock()
        agents = {"worker": worker}
        c = make_coordinator(
            agents=agents, kill_fn=kill_fn, notify_fn=notify_fn,
        )

        async def release_later():
            await asyncio.sleep(0.1)
            worker.query_lock.release()

        asyncio.create_task(release_later())

        await c.graceful_shutdown("test")

        # Should have at least the initial notification + some status updates
        assert notify_fn.call_count >= 1
        kill_fn.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: force_shutdown
# ---------------------------------------------------------------------------

class TestForceShutdown:
    @pytest.mark.asyncio
    @patch("shutdown._start_deadline_thread")
    async def test_does_not_wait_for_busy(self, mock_deadline):
        """Force shutdown should exit immediately even with busy agents."""
        worker = FakeAgent("worker")
        await worker.query_lock.acquire()
        kill_fn = MagicMock()
        sleep_fn = AsyncMock()
        close_fn = AsyncMock()
        agents = {"worker": worker}
        c = make_coordinator(
            agents=agents, sleep_fn=sleep_fn, close_bot_fn=close_fn, kill_fn=kill_fn
        )

        await c.force_shutdown("test")

        assert c.requested is True
        kill_fn.assert_called_once()
        close_fn.assert_awaited_once()
        worker.query_lock.release()

    @pytest.mark.asyncio
    @patch("shutdown._start_deadline_thread")
    async def test_does_not_skip_any_agent(self, mock_deadline):
        """Force shutdown should sleep ALL agents (no skip)."""
        a = FakeAgent("a", client="alive")
        b = FakeAgent("b", client="alive")
        sleep_fn = AsyncMock()
        c = make_coordinator(
            agents={"a": a, "b": b}, sleep_fn=sleep_fn, kill_fn=MagicMock()
        )

        await c.force_shutdown("test")

        assert sleep_fn.call_count == 2

    @pytest.mark.asyncio
    @patch("shutdown._start_deadline_thread")
    async def test_escalate_from_graceful(self, mock_deadline):
        """force_shutdown should work even if graceful was already requested."""
        kill_fn = MagicMock()
        c = make_coordinator(kill_fn=kill_fn)
        c._requested = True  # simulate graceful already in progress

        await c.force_shutdown("user")

        kill_fn.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: bot.close() timeout
# ---------------------------------------------------------------------------

class TestBotCloseTimeout:
    @pytest.mark.asyncio
    @patch("shutdown._start_deadline_thread")
    async def test_proceeds_if_close_times_out(self, mock_deadline):
        """If bot.close() hangs, we should still proceed to kill."""
        async def slow_close():
            await asyncio.sleep(999)

        kill_fn = MagicMock()
        c = make_coordinator(
            close_bot_fn=slow_close,
            kill_fn=kill_fn,
            bot_close_timeout=0.01,
        )

        await c.graceful_shutdown("test")

        kill_fn.assert_called_once()

    @pytest.mark.asyncio
    @patch("shutdown._start_deadline_thread")
    async def test_proceeds_if_close_raises(self, mock_deadline):
        """If bot.close() raises, we should still proceed to kill."""
        async def bad_close():
            raise RuntimeError("discord broke")

        kill_fn = MagicMock()
        c = make_coordinator(close_bot_fn=bad_close, kill_fn=kill_fn)

        await c.graceful_shutdown("test")

        kill_fn.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: safety deadline thread
# ---------------------------------------------------------------------------

class TestDeadlineThread:
    @patch("shutdown.os._exit")
    def test_deadline_fires(self, mock_exit):
        """The deadline thread should call os._exit after the timeout."""
        from shutdown import _start_deadline_thread

        t = _start_deadline_thread(0.05)
        t.join(timeout=1.0)

        mock_exit.assert_called_once_with(42)


# ---------------------------------------------------------------------------
# Tests: requested flag blocks new work
# ---------------------------------------------------------------------------

class TestRequestedFlag:
    def test_initially_false(self):
        c = make_coordinator()
        assert c.requested is False

    @pytest.mark.asyncio
    @patch("shutdown._start_deadline_thread")
    async def test_set_after_graceful(self, mock_deadline):
        c = make_coordinator(kill_fn=MagicMock())
        await c.graceful_shutdown("test")
        assert c.requested is True

    @pytest.mark.asyncio
    @patch("shutdown._start_deadline_thread")
    async def test_set_after_force(self, mock_deadline):
        c = make_coordinator(kill_fn=MagicMock())
        await c.force_shutdown("test")
        assert c.requested is True
