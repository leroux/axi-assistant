"""Tests for agenthub.callbacks.FrontendCallbacks."""

from __future__ import annotations

from typing import Any

import pytest

from agenthub.callbacks import FrontendCallbacks


def _make_callbacks(**overrides: Any) -> FrontendCallbacks:
    """Build a FrontendCallbacks with no-op defaults, applying overrides."""

    async def _noop(*args: object, **kwargs: object) -> None:
        pass

    async def _noop_channel(agent_name: str) -> object:
        return None

    async def _noop_spawn(session: object) -> None:
        pass

    defaults: dict[str, Any] = {
        "post_message": _noop,
        "post_system": _noop,
        "on_wake": _noop,
        "on_sleep": _noop,
        "on_session_id": _noop,
        "get_channel": _noop_channel,
        "on_spawn": _noop_spawn,
        "on_kill": _noop,
        "broadcast": _noop,
        "schedule_rate_limit_expiry": _noop,
        "on_idle_reminder": _noop,
        "on_reconnect": _noop,
        "close_app": _noop,
        "kill_process": _noop,
    }
    defaults.update(overrides)
    return FrontendCallbacks(**defaults)


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

        cb = _make_callbacks(
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

        cb = _make_callbacks(
            post_message=post_message,
            post_system=post_system,
            on_wake=on_wake,
            on_sleep=on_sleep,
            on_session_id=on_session_id,
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
