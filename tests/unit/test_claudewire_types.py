"""Tests for claudewire.types — dataclasses, CommandResult, and protocol shapes."""

from __future__ import annotations

import asyncio

import pytest

from claudewire.types import (
    CommandResult,
    ExitEvent,
    ProcessEvent,
    StderrEvent,
    StdoutEvent,
)

# ---------------------------------------------------------------------------
# Event dataclasses
# ---------------------------------------------------------------------------


class TestStdoutEvent:
    def test_construction(self) -> None:
        ev = StdoutEvent(name="a1", data={"type": "msg", "n": 0})
        assert ev.name == "a1"
        assert ev.data == {"type": "msg", "n": 0}

    def test_equality(self) -> None:
        a = StdoutEvent(name="x", data={"k": 1})
        b = StdoutEvent(name="x", data={"k": 1})
        assert a == b

    def test_inequality(self) -> None:
        a = StdoutEvent(name="x", data={"k": 1})
        b = StdoutEvent(name="x", data={"k": 2})
        assert a != b

    def test_empty_data(self) -> None:
        ev = StdoutEvent(name="", data={})
        assert ev.data == {}


class TestStderrEvent:
    def test_construction(self) -> None:
        ev = StderrEvent(name="a1", text="warning: something")
        assert ev.name == "a1"
        assert ev.text == "warning: something"

    def test_equality(self) -> None:
        assert StderrEvent(name="a", text="x") == StderrEvent(name="a", text="x")

    def test_inequality(self) -> None:
        assert StderrEvent(name="a", text="x") != StderrEvent(name="a", text="y")


class TestExitEvent:
    def test_with_code(self) -> None:
        ev = ExitEvent(name="a1", code=0)
        assert ev.name == "a1"
        assert ev.code == 0

    def test_with_none_code(self) -> None:
        ev = ExitEvent(name="a1", code=None)
        assert ev.code is None

    def test_equality(self) -> None:
        assert ExitEvent(name="a", code=1) == ExitEvent(name="a", code=1)


class TestProcessEventUnion:
    def test_stdout_is_process_event(self) -> None:
        ev: ProcessEvent = StdoutEvent(name="x", data={})
        assert isinstance(ev, StdoutEvent)

    def test_stderr_is_process_event(self) -> None:
        ev: ProcessEvent = StderrEvent(name="x", text="err")
        assert isinstance(ev, StderrEvent)

    def test_exit_is_process_event(self) -> None:
        ev: ProcessEvent = ExitEvent(name="x", code=0)
        assert isinstance(ev, ExitEvent)


# ---------------------------------------------------------------------------
# CommandResult
# ---------------------------------------------------------------------------


class TestCommandResult:
    def test_minimal(self) -> None:
        r = CommandResult(ok=True)
        assert r.ok is True
        assert r.error is None
        assert r.already_running is False
        assert r.replayed is None
        assert r.status is None
        assert r.idle is None
        assert r.agents == []

    def test_all_fields(self) -> None:
        r = CommandResult(
            ok=True,
            error=None,
            already_running=True,
            replayed=5,
            status="running",
            idle=False,
            agents=["a1", "a2"],
        )
        assert r.already_running is True
        assert r.replayed == 5
        assert r.status == "running"
        assert r.idle is False
        assert r.agents == ["a1", "a2"]

    def test_error_result(self) -> None:
        r = CommandResult(ok=False, error="not found")
        assert r.ok is False
        assert r.error == "not found"

    def test_agents_default_is_independent(self) -> None:
        """Each CommandResult gets its own agents list (no shared default)."""
        r1 = CommandResult(ok=True)
        r2 = CommandResult(ok=True)
        r1.agents.append("x")
        assert r2.agents == []


# ---------------------------------------------------------------------------
# ProcessEventQueue protocol
# ---------------------------------------------------------------------------


class TestProcessEventQueueProtocol:
    @pytest.mark.asyncio
    async def test_asyncio_queue_satisfies_protocol(self) -> None:
        """asyncio.Queue[ProcessEvent | None] structurally satisfies ProcessEventQueue."""
        q: asyncio.Queue[ProcessEvent | None] = asyncio.Queue()
        await q.put(StdoutEvent(name="a", data={"x": 1}))
        result = await q.get()
        assert isinstance(result, StdoutEvent)
        assert result.data == {"x": 1}

    @pytest.mark.asyncio
    async def test_none_sentinel(self) -> None:
        q: asyncio.Queue[ProcessEvent | None] = asyncio.Queue()
        await q.put(None)
        assert await q.get() is None

    @pytest.mark.asyncio
    async def test_all_event_types_through_queue(self) -> None:
        q: asyncio.Queue[ProcessEvent | None] = asyncio.Queue()
        events: list[ProcessEvent | None] = [
            StdoutEvent(name="a", data={"k": "v"}),
            StderrEvent(name="a", text="warn"),
            ExitEvent(name="a", code=0),
            None,
        ]
        for ev in events:
            await q.put(ev)
        for expected in events:
            assert await q.get() == expected
