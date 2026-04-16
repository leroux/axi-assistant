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


_AGENT_NAMES = ["a", "b", "c", "d"]


@st.composite
def _idle_agent_specs(draw: st.DrawFn) -> tuple[list[dict[str, object]], set[str], str]:
    candidate_names = draw(st.lists(st.sampled_from(_AGENT_NAMES), unique=True, min_size=2, max_size=4))
    exclude = draw(st.sampled_from(candidate_names))
    remaining = [name for name in candidate_names if name != exclude]

    specs: list[dict[str, object]] = [
        {
            "name": name,
            "idle_secs": draw(st.integers(min_value=0, max_value=300)),
            "awake": True,
            "busy": False,
            "bridge_busy": False,
            "interactive": draw(st.booleans()),
        }
        for name in candidate_names
    ]

    protected_names = set(draw(st.lists(st.sampled_from(remaining), unique=True, max_size=max(0, len(remaining) - 1))))

    extras = [name for name in _AGENT_NAMES if name not in candidate_names]
    extra_names = draw(st.lists(st.sampled_from(extras), unique=True, max_size=len(extras))) if extras else []
    extra_specs = [
        {
            "name": name,
            "idle_secs": draw(st.integers(min_value=0, max_value=300)),
            "awake": draw(st.booleans()),
            "busy": draw(st.booleans()),
            "bridge_busy": draw(st.booleans()),
            "interactive": draw(st.booleans()),
        }
        for name in extra_names
    ]

    return specs + extra_specs, protected_names, exclude


@st.composite
def _busy_agent_specs(draw: st.DrawFn) -> tuple[list[dict[str, object]], set[str], str]:
    candidate_names = draw(st.lists(st.sampled_from(_AGENT_NAMES), unique=True, min_size=2, max_size=4))
    exclude = draw(st.sampled_from(candidate_names))
    remaining = [name for name in candidate_names if name != exclude]

    specs: list[dict[str, object]] = [
        {
            "name": name,
            "query_started_secs_ago": draw(st.integers(min_value=0, max_value=300)),
            "awake": True,
            "busy": True,
            "interactive": draw(st.booleans()),
        }
        for name in candidate_names
    ]

    protected_names = set(draw(st.lists(st.sampled_from(remaining), unique=True, max_size=max(0, len(remaining) - 1))))

    extras = [name for name in _AGENT_NAMES if name not in candidate_names]
    extra_specs: list[dict[str, object]] = []
    extra_names = draw(st.lists(st.sampled_from(extras), unique=True, max_size=len(extras))) if extras else []
    for name in extra_names:
        extra_specs.append(
            {
                "name": name,
                "query_started_secs_ago": draw(st.integers(min_value=0, max_value=300)),
                "awake": False,
                "busy": draw(st.booleans()),
                "interactive": draw(st.booleans()),
            }
        )

    return specs + extra_specs, protected_names, exclude


@given(data=_idle_agent_specs())
@pytest.mark.asyncio
async def test_evict_idle_property_prefers_background_then_longest_idle(
    data: tuple[list[dict[str, object]], set[str], str],
) -> None:
    specs, protected, exclude = data
    sessions: dict[str, AgentSession] = {}
    evicted: list[str] = []

    async def sleep(session: AgentSession) -> None:
        evicted.append(session.name)
        session.client = None

    scheduler = Scheduler(
        max_slots=max(len(specs), 1),
        protected=protected,
        get_sessions=lambda: sessions,
        sleep_fn=sleep,
    )

    for spec in specs:
        name = str(spec["name"])
        session = await make_agent(
            name,
            awake=bool(spec["awake"]),
            busy=bool(spec["busy"]),
            idle_secs=float(spec["idle_secs"]),
            bridge_busy=bool(spec.get("bridge_busy", False)),
        )
        sessions[name] = session
        if session.client is not None:
            scheduler.restore_slot(name)
        if bool(spec["interactive"]):
            scheduler.mark_interactive(name)

    result = await scheduler._evict_idle(exclude=exclude)

    candidates = [
        spec
        for spec in specs
        if str(spec["name"]) != exclude
        and str(spec["name"]) not in protected
        and bool(spec["awake"])
        and not bool(spec["busy"])
        and not bool(spec.get("bridge_busy", False))
    ]
    background = [spec for spec in candidates if not bool(spec["interactive"])]
    pool = background or candidates
    expected = max(pool, key=lambda spec: int(spec["idle_secs"]))

    assert result is True
    assert evicted == [str(expected["name"])]
    assert sessions[str(expected["name"])].client is None


@given(data=_busy_agent_specs())
def test_select_yield_target_property_prefers_background_then_longest_busy(
    data: tuple[list[dict[str, object]], set[str], str],
) -> None:
    specs, protected, exclude = data
    sessions: dict[str, AgentSession] = {}
    scheduler = make_scheduler(sessions, max_slots=max(len(specs), 1), protected=protected)

    for spec in specs:
        name = str(spec["name"])
        session = AgentSession(name=name)
        if bool(spec["awake"]):
            session.client = _SENTINEL
            scheduler.restore_slot(name)
        if bool(spec["busy"]):
            session.activity = ActivityState(
                phase="tool_use",
                query_started=datetime.now(UTC) - timedelta(seconds=float(spec["query_started_secs_ago"])),
            )
        if bool(spec["interactive"]):
            scheduler.mark_interactive(name)
        sessions[name] = session

    scheduler._select_yield_target(exclude=exclude)

    candidates = [
        spec
        for spec in specs
        if str(spec["name"]) != exclude and str(spec["name"]) not in protected and bool(spec["awake"])
    ]
    background = [spec for spec in candidates if not bool(spec["interactive"])]
    pool = background or candidates
    expected = max(pool, key=lambda spec: int(spec["query_started_secs_ago"]))

    assert scheduler.status()["yield_targets"] == [str(expected["name"])]
