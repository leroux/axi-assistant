"""Unit tests for the AgentHub scheduler."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta

import pytest
from claudewire.events import ActivityState
from hypothesis import given
from hypothesis import strategies as st

from agenthub.scheduler import Scheduler
from agenthub.types import AgentSession, ConcurrencyLimitError

_SENTINEL = object()


async def make_agent(
    name: str,
    *,
    awake: bool = False,
    busy: bool = False,
    idle_secs: float = 0,
    query_started_secs_ago: float | None = None,
    bridge_busy: bool = False,
) -> AgentSession:
    session = AgentSession(name=name)
    if awake:
        session.client = _SENTINEL
    if busy:
        await session.query_lock.acquire()
    session.last_activity = datetime.now(UTC) - timedelta(seconds=idle_secs)
    session.bridge_busy = bridge_busy
    if query_started_secs_ago is not None:
        session.activity = ActivityState(
            phase="tool_use",
            query_started=datetime.now(UTC) - timedelta(seconds=query_started_secs_ago),
        )
    return session


@pytest.fixture
def sessions() -> dict[str, AgentSession]:
    return {}


def make_scheduler(
    sessions: dict[str, AgentSession],
    *,
    max_slots: int = 2,
    protected: set[str] | None = None,
) -> Scheduler:
    scheduler: Scheduler | None = None

    async def sleep(session: AgentSession) -> None:
        session.client = None
        assert scheduler is not None
        scheduler.release_slot(session.name)

    scheduler = Scheduler(
        max_slots=max_slots,
        protected=protected or set(),
        get_sessions=lambda: sessions,
        sleep_fn=sleep,
    )
    return scheduler


@pytest.fixture
def scheduler(sessions: dict[str, AgentSession]) -> Scheduler:
    return make_scheduler(sessions)


@pytest.mark.asyncio
async def test_request_slot_when_available(scheduler: Scheduler) -> None:
    await scheduler.request_slot("a")
    assert scheduler.slot_count() == 1
    assert "a" in scheduler.status()["slots"]


@pytest.mark.asyncio
async def test_request_slot_idempotent(scheduler: Scheduler) -> None:
    await scheduler.request_slot("a")
    await scheduler.request_slot("a")
    assert scheduler.slot_count() == 1


@pytest.mark.asyncio
async def test_release_slot_grants_next_waiter(scheduler: Scheduler, sessions: dict[str, AgentSession]) -> None:
    sessions["a"] = await make_agent("a", awake=True, busy=True, query_started_secs_ago=10)
    sessions["b"] = await make_agent("b", awake=True, busy=True, query_started_secs_ago=20)
    scheduler.restore_slot("a")
    scheduler.restore_slot("b")

    task = asyncio.create_task(scheduler.request_slot("c", timeout=1.0))
    await asyncio.sleep(0.05)
    assert scheduler.has_waiters() is True

    scheduler.release_slot("a")
    await asyncio.wait_for(task, timeout=1.0)
    assert "c" in scheduler.status()["slots"]


@pytest.mark.asyncio
async def test_evicts_background_before_interactive(scheduler: Scheduler, sessions: dict[str, AgentSession]) -> None:
    sessions["bg"] = await make_agent("bg", awake=True, idle_secs=50)
    sessions["ia"] = await make_agent("ia", awake=True, idle_secs=100)
    scheduler.restore_slot("bg")
    scheduler.restore_slot("ia")
    scheduler.mark_interactive("ia")

    await scheduler.request_slot("new")

    assert sessions["bg"].client is None
    assert sessions["ia"].client is _SENTINEL
    assert "new" in scheduler.status()["slots"]


@pytest.mark.asyncio
async def test_does_not_evict_protected_busy_or_bridge_busy() -> None:
    sessions: dict[str, AgentSession] = {}
    scheduler = make_scheduler(
        sessions,
        max_slots=3,
        protected={"protected"},
    )
    sessions["protected"] = await make_agent("protected", awake=True, idle_secs=100)
    sessions["busy"] = await make_agent("busy", awake=True, busy=True, idle_secs=100, query_started_secs_ago=30)
    sessions["bridge"] = await make_agent("bridge", awake=True, idle_secs=100, bridge_busy=True)
    scheduler.restore_slot("protected")
    scheduler.restore_slot("busy")
    scheduler.restore_slot("bridge")

    task = asyncio.create_task(scheduler.request_slot("new", timeout=0.1))
    await asyncio.sleep(0.05)

    assert sessions["protected"].client is _SENTINEL
    assert sessions["busy"].client is _SENTINEL
    assert sessions["bridge"].client is _SENTINEL
    assert scheduler.slot_count() == 3
    assert scheduler.has_waiters() is True

    with pytest.raises(ConcurrencyLimitError):
        await task


@pytest.mark.asyncio
async def test_select_yield_target_prefers_background(scheduler: Scheduler, sessions: dict[str, AgentSession]) -> None:
    sessions["bg"] = await make_agent("bg", awake=True, busy=True, query_started_secs_ago=10)
    sessions["ia"] = await make_agent("ia", awake=True, busy=True, query_started_secs_ago=20)
    scheduler.restore_slot("bg")
    scheduler.restore_slot("ia")
    scheduler.mark_interactive("ia")

    task = asyncio.create_task(scheduler.request_slot("new", timeout=0.2))
    await asyncio.sleep(0.05)

    assert scheduler.should_yield("bg") is True
    assert scheduler.should_yield("ia") is False

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_status_reports_restore_and_priority_marks(scheduler: Scheduler) -> None:
    scheduler.restore_slot("a")
    scheduler.mark_interactive("a")
    scheduler.restore_slot("b")
    scheduler.mark_background("b")

    status = scheduler.status()
    assert status["slot_count"] == 2
    assert status["slots"] == ["a", "b"]
    assert status["interactive"] == ["a"]


@given(
    max_slots=st.integers(min_value=1, max_value=4),
    slot_names=st.lists(st.sampled_from(["a", "b", "c", "d"]), unique=True, min_size=0, max_size=4),
)
def test_restore_slot_never_exceeds_unique_slot_count(max_slots: int, slot_names: list[str]) -> None:
    scheduler = Scheduler(
        max_slots=max_slots,
        protected=set(),
        get_sessions=dict,
        sleep_fn=lambda session: asyncio.sleep(0),
    )
    for name in slot_names:
        scheduler.restore_slot(name)
        scheduler.restore_slot(name)
    assert scheduler.slot_count() == len(set(slot_names))
    assert scheduler.slot_count() >= 0
