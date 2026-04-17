from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from axi.axi_types import AgentSession
from axi.discord_stream import stream_response_to_channel
from claude_agent_sdk.types import StreamEvent


class FakeTypingTask:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class FakeTypingCtx:
    def __init__(self) -> None:
        self.task = FakeTypingTask()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeChannel:
    def __init__(self) -> None:
        self.id = 12345
        self.send = AsyncMock()

    def typing(self):
        return FakeTypingCtx()


@pytest.mark.asyncio
async def test_stream_killed_clears_thinking_and_typing_before_sleep() -> None:
    session = AgentSession(name="test")
    channel = FakeChannel()
    hide_calls: list[str] = []
    stop_calls: list[bool] = []
    flush_calls: list[str] = []
    sleep_calls: list[bool] = []

    async def fake_receive_response_safe(_session):
        yield StreamEvent(uuid="u", session_id="", event={"type": "content_block_start", "content_block": {"type": "thinking"}})

    async def fake_hide(ctx):
        hide_calls.append("hide")
        ctx.thinking_message = None

    def fake_stop(ctx, typing_ctx):
        stop_calls.append(True)
        typing_ctx.task.cancel()
        ctx.typing_stopped = True

    async def fake_flush(ctx, _session, _channel, reason="?"):
        flush_calls.append(reason)

    async def fake_sleep(_session, force=False):
        sleep_calls.append(force)

    with patch("axi.discord_stream._receive_response_safe", fake_receive_response_safe), \
         patch("axi.discord_stream._drain_and_send_stderr", AsyncMock()), \
         patch("axi.discord_stream._stall_watchdog", AsyncMock()), \
         patch("axi.discord_stream._hide_thinking", fake_hide), \
         patch("axi.discord_stream._stop_typing", fake_stop), \
         patch("axi.discord_stream._flush_text", fake_flush), \
         patch("axi.discord_stream._retry_discord_503", AsyncMock()), \
         patch("axi.discord_stream._sleep_agent_fn", fake_sleep), \
         patch("axi.discord_stream._send_long", AsyncMock()), \
         patch("axi.discord_stream._next_stream_id", lambda _name: "stream-1"), \
         patch("axi.discord_stream._tracer") as tracer:
        tracer.start_span.return_value = SimpleNamespace(set_attributes=lambda *_a, **_k: None, end=lambda: None)
        result = await stream_response_to_channel(session, channel)

    assert result is None
    assert hide_calls == ["hide"]
    assert stop_calls == [True]
    assert flush_calls == ["post_kill"]
    assert sleep_calls == [True]
