"""Unit tests for the agent scheduler — no mocks.

Tests use real AgentSession objects and real asyncio primitives.
The scheduler's sleep_fn callback sets session.client = None and calls release_slot.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from axi import scheduler
from axi.axi_types import AgentSession
from claudewire.events import ActivityState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SENTINEL = object()  # truthy non-None stand-in for a real SDK client


async def make_agent(
    name: str,
    *,
    awake: bool = False,
    busy: bool = False,
    idle_secs: float = 0,
    queued: int = 0,
    query_started_secs_ago: float | None = None,
) -> AgentSession:
    """Create a real AgentSession with controlled state."""
    s = AgentSession(name=name)
    if awake:
        s.client = _SENTINEL  # type: ignore[assignment]
    if busy:
        # Actually acquire the lock so .locked() returns True.
        # We hold it via a background task that never releases it during the test.
        await s.query_lock.acquire()
    s.last_activity = datetime.now(UTC) - timedelta(seconds=idle_secs)
    for _ in range(queued):
        s.message_queue.append(("msg", None, None))
    if query_started_secs_ago is not None:
        s.activity = ActivityState(
            phase="tool_use",
            query_started=datetime.now(UTC) - timedelta(seconds=query_started_secs_ago),
        )
    return s


async def real_sleep(session: AgentSession) -> None:
    """Sleep callback — mirrors what agents.sleep_agent does for scheduling."""
    session.client = None
    scheduler.release_slot(session.name)


@pytest.fixture(autouse=True)
def _reset_scheduler():
    """Reset scheduler state before each test."""
    scheduler._slots.clear()
    scheduler._waiters.clear()
    scheduler._yield_set.clear()
    scheduler._interactive.clear()
    scheduler._max_slots = 10
    scheduler._protected = set()
    scheduler._get_agents = None
    scheduler._sleep_fn = None
    yield
    # Clean up any remaining locks that tests may have left
    scheduler._slots.clear()
    scheduler._waiters.clear()
    scheduler._yield_set.clear()
    scheduler._interactive.clear()


def init_scheduler(
    agents_dict: dict[str, AgentSession],
    *,
    max_slots: int = 2,
    protected: set[str] | None = None,
) -> None:
    """Helper to init scheduler with a test agents dict."""
    scheduler.init(
        max_slots=max_slots,
        protected=protected or set(),
        get_agents=lambda: agents_dict,
        sleep_fn=real_sleep,
    )


# ---------------------------------------------------------------------------
# Basic slot management
# ---------------------------------------------------------------------------


class TestBasicSlots:
    async def test_request_slot_when_available(self) -> None:
        init_scheduler({})
        await scheduler.request_slot("agent-a")
        assert "agent-a" in scheduler._slots
        assert scheduler.slot_count() == 1

    async def test_request_slot_idempotent(self) -> None:
        init_scheduler({})
        await scheduler.request_slot("agent-a")
        await scheduler.request_slot("agent-a")
        assert scheduler.slot_count() == 1

    async def test_release_slot_frees_capacity(self) -> None:
        init_scheduler({})
        await scheduler.request_slot("agent-a")
        assert scheduler.slot_count() == 1
        scheduler.release_slot("agent-a")
        assert scheduler.slot_count() == 0

    async def test_release_unknown_agent(self) -> None:
        init_scheduler({})
        # Should not raise
        scheduler.release_slot("nonexistent")
        assert scheduler.slot_count() == 0

    async def test_slot_count_and_status(self) -> None:
        init_scheduler({}, max_slots=5)
        await scheduler.request_slot("a")
        await scheduler.request_slot("b")
        assert scheduler.slot_count() == 2
        st = scheduler.status()
        assert st["max_slots"] == 5
        assert st["slot_count"] == 2
        assert sorted(st["slots"]) == ["a", "b"]

    async def test_multiple_slots_up_to_max(self) -> None:
        init_scheduler({}, max_slots=3)
        await scheduler.request_slot("a")
        await scheduler.request_slot("b")
        await scheduler.request_slot("c")
        assert scheduler.slot_count() == 3


# ---------------------------------------------------------------------------
# Idle eviction
# ---------------------------------------------------------------------------


class TestIdleEviction:
    async def test_evict_idle_background_first(self) -> None:
        """With one idle background and one idle interactive, evicts background."""
        bg = await make_agent("bg", awake=True, idle_secs=10)
        ia = await make_agent("ia", awake=True, idle_secs=20)
        agents_dict = {"bg": bg, "ia": ia}
        init_scheduler(agents_dict, max_slots=2)
        scheduler._slots.update(["bg", "ia"])
        scheduler.mark_interactive("ia")

        # Request a third slot — should evict bg (background), not ia (interactive)
        await scheduler.request_slot("new-agent")
        assert "new-agent" in scheduler._slots
        assert bg.client is None  # evicted
        assert ia.client is not None  # kept

    async def test_evict_longest_idle(self) -> None:
        """Among two idle background agents, evicts the one idle longer."""
        a = await make_agent("a", awake=True, idle_secs=100)
        b = await make_agent("b", awake=True, idle_secs=10)
        agents_dict = {"a": a, "b": b}
        init_scheduler(agents_dict, max_slots=2)
        scheduler._slots.update(["a", "b"])

        await scheduler.request_slot("new")
        assert a.client is None  # idle longer, evicted
        assert b.client is not None  # kept

    async def test_no_evict_protected(self) -> None:
        """Master agent is never evicted even if idle longest."""
        master = await make_agent("master", awake=True, idle_secs=1000)
        other = await make_agent("other", awake=True, idle_secs=5)
        agents_dict = {"master": master, "other": other}
        init_scheduler(agents_dict, max_slots=2, protected={"master"})
        scheduler._slots.update(["master", "other"])

        await scheduler.request_slot("new")
        assert master.client is not None  # protected
        assert other.client is None  # evicted instead

    async def test_no_evict_busy_agent(self) -> None:
        """Agent with query_lock held is not evicted as idle."""
        busy = await make_agent("busy", awake=True, busy=True, idle_secs=100)
        idle = await make_agent("idle", awake=True, idle_secs=50)
        agents_dict = {"busy": busy, "idle": idle}
        init_scheduler(agents_dict, max_slots=2)
        scheduler._slots.update(["busy", "idle"])

        await scheduler.request_slot("new")
        assert busy.client is not None  # busy, not evicted
        assert idle.client is None  # evicted


# ---------------------------------------------------------------------------
# Deferred eviction (yield)
# ---------------------------------------------------------------------------


class TestDeferredEviction:
    async def test_should_yield_when_marked(self) -> None:
        """When all agents are busy, request_slot marks a yield target."""
        a = await make_agent("a", awake=True, busy=True, query_started_secs_ago=30)
        agents_dict = {"a": a}
        init_scheduler(agents_dict, max_slots=1)
        scheduler._slots.add("a")

        # Request slot in background — will block
        event_set = asyncio.Event()

        async def request_and_signal():
            await scheduler.request_slot("b", timeout=5.0)
            event_set.set()

        task = asyncio.create_task(request_and_signal())
        await asyncio.sleep(0.05)  # let request_slot run and queue

        assert scheduler.should_yield("a")
        assert not scheduler.should_yield("b")

        # Simulate agent a finishing its turn
        a.client = None
        scheduler.release_slot("a")

        await asyncio.wait_for(event_set.wait(), timeout=2.0)
        assert "b" in scheduler._slots
        task.cancel()

    async def test_should_yield_false_when_not_marked(self) -> None:
        init_scheduler({})
        assert not scheduler.should_yield("any-agent")

    async def test_yield_prefers_background(self) -> None:
        """When all agents are busy, marks background over interactive for yield."""
        bg = await make_agent("bg", awake=True, busy=True, query_started_secs_ago=10)
        ia = await make_agent("ia", awake=True, busy=True, query_started_secs_ago=20)
        agents_dict = {"bg": bg, "ia": ia}
        init_scheduler(agents_dict, max_slots=2)
        scheduler._slots.update(["bg", "ia"])
        scheduler.mark_interactive("ia")

        # Request a third slot — should mark bg (background) for yield, not ia
        task = asyncio.create_task(scheduler.request_slot("new", timeout=1.0))
        await asyncio.sleep(0.05)

        assert scheduler.should_yield("bg")
        assert not scheduler.should_yield("ia")

        # Clean up: release bg's slot so the waiter unblocks
        scheduler.release_slot("bg")
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except Exception:
            pass

    async def test_yield_never_marks_protected(self) -> None:
        """Master is never in yield_set even if it's the only busy agent."""
        master = await make_agent("master", awake=True, busy=True, query_started_secs_ago=100)
        agents_dict = {"master": master}
        init_scheduler(agents_dict, max_slots=1, protected={"master"})
        scheduler._slots.add("master")

        task = asyncio.create_task(scheduler.request_slot("new", timeout=0.5))
        await asyncio.sleep(0.05)

        assert not scheduler.should_yield("master")

        # Clean up — let the timeout fire
        from axi.axi_types import ConcurrencyLimitError

        with pytest.raises(ConcurrencyLimitError):
            await task


# ---------------------------------------------------------------------------
# Wait queue
# ---------------------------------------------------------------------------


class TestWaitQueue:
    async def test_waiter_unblocked_by_release(self) -> None:
        """request_slot blocks when full, release_slot unblocks it."""
        a = await make_agent("a", awake=True, busy=True)
        agents_dict = {"a": a}
        init_scheduler(agents_dict, max_slots=1)
        scheduler._slots.add("a")

        unblocked = asyncio.Event()

        async def wait_for_slot():
            await scheduler.request_slot("b", timeout=5.0)
            unblocked.set()

        task = asyncio.create_task(wait_for_slot())
        await asyncio.sleep(0.05)
        assert not unblocked.is_set()

        # Release the slot
        scheduler.release_slot("a")
        await asyncio.wait_for(unblocked.wait(), timeout=2.0)
        assert "b" in scheduler._slots
        task.cancel()

    async def test_fifo_ordering(self) -> None:
        """First waiter gets the slot first."""
        a = await make_agent("a", awake=True, busy=True)
        agents_dict = {"a": a}
        init_scheduler(agents_dict, max_slots=1)
        scheduler._slots.add("a")

        order: list[str] = []

        async def wait_and_record(name: str):
            await scheduler.request_slot(name, timeout=5.0)
            order.append(name)

        t1 = asyncio.create_task(wait_and_record("first"))
        await asyncio.sleep(0.02)
        t2 = asyncio.create_task(wait_and_record("second"))
        await asyncio.sleep(0.02)

        # Release slot — "first" should get it
        scheduler.release_slot("a")
        await asyncio.sleep(0.05)
        assert order == ["first"]

        # Release again — "second" should get it
        scheduler.release_slot("first")
        await asyncio.sleep(0.05)
        assert order == ["first", "second"]

        t1.cancel()
        t2.cancel()

    async def test_timeout_raises_concurrency_error(self) -> None:
        """Waiter times out with ConcurrencyLimitError."""
        from axi.axi_types import ConcurrencyLimitError

        a = await make_agent("a", awake=True, busy=True)
        agents_dict = {"a": a}
        init_scheduler(agents_dict, max_slots=1)
        scheduler._slots.add("a")

        with pytest.raises(ConcurrencyLimitError):
            await scheduler.request_slot("b", timeout=0.1)

    async def test_timeout_race_slot_granted_last_moment(self) -> None:
        """If release grants a slot just before timeout check, waiter succeeds."""
        a = await make_agent("a", awake=True, busy=True)
        agents_dict = {"a": a}
        init_scheduler(agents_dict, max_slots=1)
        scheduler._slots.add("a")

        async def release_soon():
            await asyncio.sleep(0.05)
            scheduler.release_slot("a")

        asyncio.create_task(release_soon())
        # Generous timeout so it doesn't actually expire
        await scheduler.request_slot("b", timeout=2.0)
        assert "b" in scheduler._slots


# ---------------------------------------------------------------------------
# Priority classes
# ---------------------------------------------------------------------------


class TestPriorityClasses:
    async def test_mark_interactive_affects_eviction_order(self) -> None:
        """Interactive agents are evicted after background agents."""
        bg = await make_agent("bg", awake=True, idle_secs=5)
        ia = await make_agent("ia", awake=True, idle_secs=100)  # idle much longer
        agents_dict = {"bg": bg, "ia": ia}
        init_scheduler(agents_dict, max_slots=2)
        scheduler._slots.update(["bg", "ia"])
        scheduler.mark_interactive("ia")

        # Despite ia being idle longer, bg (background) should be evicted first
        await scheduler.request_slot("new")
        assert bg.client is None
        assert ia.client is not None

    async def test_mark_background_demotes_agent(self) -> None:
        """After mark_background, agent is evictable as background."""
        a = await make_agent("a", awake=True, idle_secs=10)
        b = await make_agent("b", awake=True, idle_secs=100)
        agents_dict = {"a": a, "b": b}
        init_scheduler(agents_dict, max_slots=2)
        scheduler._slots.update(["a", "b"])

        # Mark both interactive, then demote b
        scheduler.mark_interactive("a")
        scheduler.mark_interactive("b")
        scheduler.mark_background("b")

        # b is now background, should be evicted first
        await scheduler.request_slot("new")
        assert b.client is None
        assert a.client is not None

    async def test_has_waiters(self) -> None:
        a = await make_agent("a", awake=True, busy=True)
        agents_dict = {"a": a}
        init_scheduler(agents_dict, max_slots=1)
        scheduler._slots.add("a")

        assert not scheduler.has_waiters()

        task = asyncio.create_task(scheduler.request_slot("b", timeout=1.0))
        await asyncio.sleep(0.05)
        assert scheduler.has_waiters()

        scheduler.release_slot("a")
        await asyncio.sleep(0.05)
        assert not scheduler.has_waiters()
        task.cancel()


# ---------------------------------------------------------------------------
# Concurrent scenarios
# ---------------------------------------------------------------------------


class TestConcurrentScenarios:
    async def test_multiple_waiters_multiple_releases(self) -> None:
        """3 waiters, 3 sequential releases, all unblocked in FIFO order."""
        a = await make_agent("a", awake=True, busy=True)
        agents_dict = {"a": a}
        init_scheduler(agents_dict, max_slots=1)
        scheduler._slots.add("a")

        order: list[str] = []

        async def wait_and_record(name: str):
            await scheduler.request_slot(name, timeout=5.0)
            order.append(name)

        tasks = [
            asyncio.create_task(wait_and_record("w1")),
            asyncio.create_task(wait_and_record("w2")),
            asyncio.create_task(wait_and_record("w3")),
        ]
        # Stagger creation slightly so FIFO order is deterministic
        await asyncio.sleep(0.02)
        # But they were all created in order, so w1 should be first

        # Release slots one by one
        scheduler.release_slot("a")
        await asyncio.sleep(0.05)
        assert order == ["w1"]

        scheduler.release_slot("w1")
        await asyncio.sleep(0.05)
        assert order == ["w1", "w2"]

        scheduler.release_slot("w2")
        await asyncio.sleep(0.05)
        assert order == ["w1", "w2", "w3"]

        for t in tasks:
            t.cancel()

    async def test_evict_then_wait(self) -> None:
        """2 slots: 1 idle + 1 busy. Evicts idle, then next request queues for busy."""
        from axi.axi_types import ConcurrencyLimitError

        idle = await make_agent("idle", awake=True, idle_secs=60)
        busy = await make_agent("busy", awake=True, busy=True, query_started_secs_ago=10)
        agents_dict = {"idle": idle, "busy": busy}
        init_scheduler(agents_dict, max_slots=2)
        scheduler._slots.update(["idle", "busy"])

        # First request: evicts idle, gets slot
        await scheduler.request_slot("new1")
        assert idle.client is None
        assert "new1" in scheduler._slots

        # Second request: busy is the only other occupant, can't evict — queues
        task = asyncio.create_task(scheduler.request_slot("new2", timeout=0.2))
        await asyncio.sleep(0.05)
        assert scheduler.has_waiters()
        assert scheduler.should_yield("busy")

        # Let it timeout
        with pytest.raises(ConcurrencyLimitError):
            await task

    async def test_release_clears_interactive_state(self) -> None:
        """release_slot cleans up interactive marking."""
        init_scheduler({})
        await scheduler.request_slot("a")
        scheduler.mark_interactive("a")
        assert "a" in scheduler._interactive
        scheduler.release_slot("a")
        assert "a" not in scheduler._interactive


# ---------------------------------------------------------------------------
# restore_slot
# ---------------------------------------------------------------------------


class TestRestoreSlot:
    async def test_restore_slot_basic(self) -> None:
        """restore_slot adds to _slots and increases slot_count."""
        init_scheduler({})
        assert scheduler.slot_count() == 0
        scheduler.restore_slot("agent-a")
        assert "agent-a" in scheduler._slots
        assert scheduler.slot_count() == 1

    async def test_restore_slot_over_max(self) -> None:
        """restore_slot works even if it exceeds max_slots.

        After a hot restart the bridge may reconnect more agents than the
        current config allows — we must not drop them.
        """
        init_scheduler({}, max_slots=1)
        scheduler.restore_slot("a")
        scheduler.restore_slot("b")
        assert scheduler.slot_count() == 2

    async def test_restore_slot_idempotent(self) -> None:
        """Restoring the same name twice doesn't double-count."""
        init_scheduler({})
        scheduler.restore_slot("a")
        scheduler.restore_slot("a")
        assert scheduler.slot_count() == 1

    async def test_restore_then_normal_eviction(self) -> None:
        """After restoring N slots, requesting N+1 triggers eviction."""
        a = await make_agent("a", awake=True, idle_secs=60)
        agents_dict = {"a": a}
        init_scheduler(agents_dict, max_slots=1)
        scheduler.restore_slot("a")

        b = await make_agent("b", awake=False)
        agents_dict["b"] = b

        await scheduler.request_slot("b")
        assert a.client is None  # evicted
        assert "b" in scheduler._slots

    async def test_restore_then_release(self) -> None:
        """Restored slot can be released normally, freeing capacity."""
        init_scheduler({}, max_slots=1)
        scheduler.restore_slot("a")
        assert scheduler.slot_count() == 1
        scheduler.release_slot("a")
        assert scheduler.slot_count() == 0

    async def test_restore_slot_appears_in_status(self) -> None:
        """Restored slots show up in status() output."""
        init_scheduler({})
        scheduler.restore_slot("reconnected")
        st = scheduler.status()
        assert "reconnected" in st["slots"]
        assert st["slot_count"] == 1
