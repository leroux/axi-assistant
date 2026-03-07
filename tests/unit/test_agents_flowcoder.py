"""Unit tests for flowchart system message handling in discord_stream."""

from __future__ import annotations

import pytest
from claude_agent_sdk.types import SystemMessage

from axi.axi_types import AgentSession
from axi.discord_stream import _handle_system_message, _StreamCtx


class FakeChannel:
    """Minimal channel stub that records sent messages."""

    name = "test"

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, msg: str, **kw: object) -> None:
        self.sent.append(msg)


class TestBlockStart:
    @pytest.mark.asyncio
    async def test_sets_activity_phase(self) -> None:
        session = AgentSession(name="test")
        ch = FakeChannel()
        ctx = _StreamCtx()
        msg = SystemMessage(subtype="block_start", data={
            "data": {"block_name": "Step1", "block_type": "llm"},
        })
        await _handle_system_message(session, ch, msg, ctx)  # type: ignore[arg-type]
        assert session.activity.phase == "tool_use"
        assert session.activity.tool_name == "flowcoder:llm"

    @pytest.mark.asyncio
    async def test_sends_block_header(self) -> None:
        session = AgentSession(name="test")
        ch = FakeChannel()
        ctx = _StreamCtx()
        msg = SystemMessage(subtype="block_start", data={
            "data": {"block_name": "Step1", "block_type": "llm"},
        })
        await _handle_system_message(session, ch, msg, ctx)  # type: ignore[arg-type]
        assert any("Step1" in m for m in ch.sent)

    @pytest.mark.asyncio
    async def test_silent_block_type_no_message(self) -> None:
        session = AgentSession(name="test")
        ch = FakeChannel()
        ctx = _StreamCtx()
        msg = SystemMessage(subtype="block_start", data={
            "data": {"block_name": "Init", "block_type": "start"},
        })
        await _handle_system_message(session, ch, msg, ctx)  # type: ignore[arg-type]
        assert len(ch.sent) == 0


class TestBlockComplete:
    @pytest.mark.asyncio
    async def test_failure_sends_message(self) -> None:
        session = AgentSession(name="test")
        ch = FakeChannel()
        ctx = _StreamCtx()
        msg = SystemMessage(subtype="block_complete", data={
            "data": {"block_name": "Step1", "success": False},
        })
        await _handle_system_message(session, ch, msg, ctx)  # type: ignore[arg-type]
        assert any("FAILED" in m for m in ch.sent)

    @pytest.mark.asyncio
    async def test_success_no_message(self) -> None:
        session = AgentSession(name="test")
        ch = FakeChannel()
        ctx = _StreamCtx()
        msg = SystemMessage(subtype="block_complete", data={
            "data": {"block_name": "Step1", "success": True},
        })
        await _handle_system_message(session, ch, msg, ctx)  # type: ignore[arg-type]
        assert len(ch.sent) == 0


class TestFlowchartComplete:
    @pytest.mark.asyncio
    async def test_sends_summary(self) -> None:
        session = AgentSession(name="test")
        ch = FakeChannel()

        # _handle_system_message uses _send_system which must be injected
        import axi.discord_stream as ds
        original = ds._send_system

        async def fake_send_system(channel: object, text: str) -> None:
            ch.sent.append(f"*System:* {text}")

        ds._send_system = fake_send_system  # type: ignore[assignment]
        try:
            msg = SystemMessage(subtype="flowchart_complete", data={
                "data": {
                    "status": "completed",
                    "duration_ms": 30000,
                    "cost_usd": 0.3864,
                    "blocks_executed": 5,
                },
            })
            await _handle_system_message(session, ch, msg)  # type: ignore[arg-type]
            assert any("completed" in m for m in ch.sent)
            assert any("$0.3864" in m for m in ch.sent)
        finally:
            ds._send_system = original
