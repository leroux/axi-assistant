"""Unit tests for agents.flowcoder — dispatch table routing."""

from __future__ import annotations

from typing import ClassVar

import pytest

from agents.flowcoder import (
    _FlowcoderCtx,
    _handle_block_complete,
    _handle_block_start,
    _handle_fc_result,
    _handle_fc_status,
)
from axi_types import AgentSession


class TestFlowcoderCtx:
    def test_initial_state(self) -> None:
        ctx = _FlowcoderCtx()
        assert ctx.text_buffer == []
        assert ctx.current_block == ""
        assert ctx.block_count == 0


class TestHandleBlockStart:
    @pytest.mark.asyncio
    async def test_increments_block_count(self) -> None:
        ctx = _FlowcoderCtx()
        session = AgentSession(name="test")

        class FakeChannel:
            name = "test"
            sent: ClassVar[list[str]] = []
            async def send(self, msg: str, **kw: object) -> None:
                self.sent.append(msg)

        ch = FakeChannel()
        await _handle_block_start(ctx, session, ch, {  # type: ignore[arg-type]
            "data": {"block_name": "Step1", "block_type": "llm"},
        })
        assert ctx.block_count == 1
        assert ctx.current_block == "Step1"
        assert session.activity.phase == "tool_use"


class TestHandleBlockComplete:
    @pytest.mark.asyncio
    async def test_failure_sends_message(self) -> None:
        ctx = _FlowcoderCtx()
        ctx.current_block = "Step1"
        session = AgentSession(name="test")

        class FakeChannel:
            name = "test"
            sent: ClassVar[list[str]] = []
            async def send(self, msg: str, **kw: object) -> None:
                self.sent.append(msg)

        ch = FakeChannel()
        await _handle_block_complete(ctx, session, ch, {  # type: ignore[arg-type]
            "data": {"block_name": "Step1", "success": False},
        })
        assert any("FAILED" in m for m in ch.sent)


class TestHandleFcResult:
    @pytest.mark.asyncio
    async def test_returns_true(self) -> None:
        ctx = _FlowcoderCtx()
        session = AgentSession(name="test")

        class FakeChannel:
            name = "test"
            sent: ClassVar[list[str]] = []
            async def send(self, msg: str, **kw: object) -> None:
                self.sent.append(msg)

        ch = FakeChannel()
        result = await _handle_fc_result(ctx, session, ch, {  # type: ignore[arg-type]
            "is_error": False,
            "total_cost_usd": 0.01,
        })
        assert result is True


class TestHandleFcStatus:
    @pytest.mark.asyncio
    async def test_busy_returns_false(self) -> None:
        ctx = _FlowcoderCtx()
        session = AgentSession(name="test")

        class FakeChannel:
            name = "test"

        ch = FakeChannel()
        result = await _handle_fc_status(ctx, session, ch, {"busy": True})  # type: ignore[arg-type]
        assert result is False

    @pytest.mark.asyncio
    async def test_not_busy_returns_true(self) -> None:
        ctx = _FlowcoderCtx()
        session = AgentSession(name="test")

        class FakeChannel:
            name = "test"

        ch = FakeChannel()
        result = await _handle_fc_status(ctx, session, ch, {"busy": False})  # type: ignore[arg-type]
        assert result is True
