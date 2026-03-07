"""Unit tests for AgentLog — per-agent event log with subscribers."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

import pytest

from agenthub.agent_log import AgentLog, LogEvent, make_agent_log, make_event


class TestLogEvent:
    def test_make_event_auto_timestamps(self) -> None:
        e = make_event("user", "master", text="hello")
        assert e.kind == "user"
        assert e.agent == "master"
        assert e.text == "hello"
        assert e.ts.tzinfo is not None  # UTC-aware

    def test_make_event_with_data(self) -> None:
        e = make_event("tool_use", "agent-1", tool_name="Bash", command="ls")
        assert e.data == {"tool_name": "Bash", "command": "ls"}

    def test_make_event_defaults(self) -> None:
        e = make_event("system", "master")
        assert e.text == ""
        assert e.source == ""
        assert e.data == {}


class TestAgentLog:
    @pytest.mark.asyncio
    async def test_append_records_event(self) -> None:
        log = AgentLog("test-agent")
        e = make_event("user", "test-agent", text="hi")
        await log.append(e)
        assert len(log.events) == 1
        assert log.events[0] is e

    @pytest.mark.asyncio
    async def test_subscriber_called_on_append(self) -> None:
        log = AgentLog("test-agent")
        received: list[LogEvent] = []

        async def on_event(event: LogEvent) -> None:
            received.append(event)

        log.subscribe(on_event)
        e = make_event("user", "test-agent", text="hello")
        await log.append(e)
        assert len(received) == 1
        assert received[0] is e

    @pytest.mark.asyncio
    async def test_multiple_subscribers(self) -> None:
        log = AgentLog("test-agent")
        calls_a: list[str] = []
        calls_b: list[str] = []

        async def sub_a(event: LogEvent) -> None:
            calls_a.append(event.kind)

        async def sub_b(event: LogEvent) -> None:
            calls_b.append(event.kind)

        log.subscribe(sub_a)
        log.subscribe(sub_b)
        await log.append(make_event("user", "test-agent"))
        await log.append(make_event("assistant", "test-agent"))
        assert calls_a == ["user", "assistant"]
        assert calls_b == ["user", "assistant"]

    @pytest.mark.asyncio
    async def test_unsubscribe(self) -> None:
        log = AgentLog("test-agent")
        received: list[LogEvent] = []

        async def on_event(event: LogEvent) -> None:
            received.append(event)

        log.subscribe(on_event)
        await log.append(make_event("user", "test-agent"))
        log.unsubscribe(on_event)
        await log.append(make_event("assistant", "test-agent"))
        assert len(received) == 1  # only first event

    @pytest.mark.asyncio
    async def test_unsubscribe_nonexistent_is_noop(self) -> None:
        log = AgentLog("test-agent")

        async def dummy(event: LogEvent) -> None:
            pass

        log.unsubscribe(dummy)  # should not raise

    @pytest.mark.asyncio
    async def test_subscriber_error_doesnt_break_others(self) -> None:
        log = AgentLog("test-agent")
        received: list[str] = []

        async def bad_sub(event: LogEvent) -> None:
            raise RuntimeError("boom")

        async def good_sub(event: LogEvent) -> None:
            received.append(event.kind)

        log.subscribe(bad_sub)
        log.subscribe(good_sub)
        await log.append(make_event("user", "test-agent"))
        assert received == ["user"]  # good_sub still called

    @pytest.mark.asyncio
    async def test_replay_all(self) -> None:
        log = AgentLog("test-agent")
        await log.append(make_event("user", "test-agent", text="a"))
        await log.append(make_event("assistant", "test-agent", text="b"))
        events = log.replay()
        assert len(events) == 2
        assert events[0].text == "a"
        assert events[1].text == "b"

    @pytest.mark.asyncio
    async def test_replay_since(self) -> None:
        log = AgentLog("test-agent")
        t0 = datetime.now(UTC)
        await log.append(LogEvent(ts=t0, kind="user", agent="test-agent", text="old"))
        t1 = datetime(2099, 1, 1, tzinfo=UTC)  # far future
        await log.append(LogEvent(ts=t1, kind="assistant", agent="test-agent", text="new"))
        events = log.replay(since=t1)
        assert len(events) == 1
        assert events[0].text == "new"

    @pytest.mark.asyncio
    async def test_clear(self) -> None:
        log = AgentLog("test-agent")
        await log.append(make_event("user", "test-agent"))
        log.clear()
        assert len(log.events) == 0
        assert len(log.replay()) == 0

    @pytest.mark.asyncio
    async def test_persistence(self, tmp_path: pytest.TempPathFactory) -> None:
        persist_dir = str(tmp_path)
        log = AgentLog("test-agent", persist_dir=persist_dir)
        await log.append(make_event("user", "test-agent", text="hello"))
        await log.append(make_event("assistant", "test-agent", text="hi back"))

        # Check JSONL file
        path = os.path.join(persist_dir, "test-agent.events.jsonl")
        assert os.path.exists(path)
        with open(path) as f:
            lines = f.readlines()
        assert len(lines) == 2
        first = json.loads(lines[0])
        assert first["kind"] == "user"
        assert first["text"] == "hello"

    def test_make_agent_log_factory(self, tmp_path: pytest.TempPathFactory) -> None:
        log = make_agent_log("factory-test", persist_dir=str(tmp_path))
        assert log.agent_name == "factory-test"

    @pytest.mark.asyncio
    async def test_no_duplicate_subscriber(self) -> None:
        log = AgentLog("test-agent")
        calls = 0

        async def sub(event: LogEvent) -> None:
            nonlocal calls
            calls += 1

        log.subscribe(sub)
        log.subscribe(sub)  # duplicate
        await log.append(make_event("user", "test-agent"))
        assert calls == 1  # only called once
