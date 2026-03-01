"""Tests for agenthub.callbacks.FrontendCallbacks."""

from __future__ import annotations

import pytest

from agenthub.callbacks import FrontendCallbacks


class TestFrontendCallbacks:
    def test_construction_with_async_callables(self) -> None:
        calls: list[tuple[str, ...]] = []

        async def post_message(agent_name: str, text: str) -> None:
            calls.append(("post_message", agent_name, text))

        async def post_system(agent_name: str, text: str) -> None:
            calls.append(("post_system", agent_name, text))

        async def on_wake(agent_name: str) -> None:
            calls.append(("on_wake", agent_name))

        async def on_sleep(agent_name: str) -> None:
            calls.append(("on_sleep", agent_name))

        async def on_session_id(agent_name: str, session_id: str) -> None:
            calls.append(("on_session_id", agent_name, session_id))

        async def get_channel(agent_name: str) -> object:
            return f"channel-{agent_name}"

        cb = FrontendCallbacks(
            post_message=post_message,
            post_system=post_system,
            on_wake=on_wake,
            on_sleep=on_sleep,
            on_session_id=on_session_id,
            get_channel=get_channel,
        )

        assert cb.post_message is post_message
        assert cb.post_system is post_system
        assert cb.on_wake is on_wake
        assert cb.on_sleep is on_sleep
        assert cb.on_session_id is on_session_id
        assert cb.get_channel is get_channel

    @pytest.mark.asyncio
    async def test_callbacks_are_callable(self) -> None:
        calls: list[str] = []

        async def post_message(agent_name: str, text: str) -> None:
            calls.append(f"msg:{agent_name}:{text}")

        async def post_system(agent_name: str, text: str) -> None:
            calls.append(f"sys:{agent_name}:{text}")

        async def on_wake(agent_name: str) -> None:
            calls.append(f"wake:{agent_name}")

        async def on_sleep(agent_name: str) -> None:
            calls.append(f"sleep:{agent_name}")

        async def on_session_id(agent_name: str, session_id: str) -> None:
            calls.append(f"sid:{agent_name}:{session_id}")

        async def get_channel(agent_name: str) -> object:
            return None

        cb = FrontendCallbacks(
            post_message=post_message,
            post_system=post_system,
            on_wake=on_wake,
            on_sleep=on_sleep,
            on_session_id=on_session_id,
            get_channel=get_channel,
        )

        await cb.post_message("a1", "hello")
        await cb.post_system("a1", "started")
        await cb.on_wake("a1")
        await cb.on_sleep("a1")
        await cb.on_session_id("a1", "sess-123")

        assert calls == [
            "msg:a1:hello",
            "sys:a1:started",
            "wake:a1",
            "sleep:a1",
            "sid:a1:sess-123",
        ]
