"""Unit tests for the frontend-agnostic streaming engine (agenthub/streaming.py).

Tests the transformation from raw SDK messages to StreamOutput events
without any Discord/frontend dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
)

from agenthub.stream_types import (
    BlockStart,
    CompactStart,
    FlowchartEnd,
    QueryResult,
    RateLimitHit,
    StreamEnd,
    StreamKilled,
    StreamOutput,
    StreamStart,
    TextDelta,
    TextFlush,
    ThinkingEnd,
    ThinkingStart,
    TodoUpdate,
    ToolUseEnd,
    ToolUseStart,
    TransientError,
)
from agenthub.streaming import _extract_tool_preview, stream_response
from claudewire.events import ActivityState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeSession:
    """Minimal AgentSession stand-in for testing."""

    name: str = "test-agent"
    client: Any = None
    session_id: str | None = None
    activity: ActivityState = field(default_factory=ActivityState)
    agent_log: Any = None
    transport: Any = None
    context_tokens: int = 0
    context_window: int = 0
    compact_instructions: str | None = None
    cwd: str = ""


def _se(event: dict[str, Any], sid: str | None = None) -> StreamEvent:
    """Make a StreamEvent."""
    return StreamEvent(uuid="u", session_id=sid or "", event=event)


def _result(sid: str = "s1", cost: float = 0.01) -> ResultMessage:
    """Make a ResultMessage."""
    return ResultMessage(
        subtype="result", duration_ms=500, duration_api_ms=500,
        is_error=False, num_turns=1, session_id=sid, total_cost_usd=cost,
    )


def _assistant(error: str | None = None, text: str = "") -> AssistantMessage:
    """Make an AssistantMessage."""
    content = [TextBlock(text=text)] if text else []
    return AssistantMessage(content=content, model="test", error=error)  # type: ignore[arg-type]


def _system(subtype: str, data: dict[str, Any] | None = None) -> SystemMessage:
    """Make a SystemMessage."""
    return SystemMessage(subtype=subtype, data=data or {})


async def _collect(session: Any, messages: list[Any], **kwargs: Any) -> list[StreamOutput]:
    """Run stream_response with mocked receive and collect all outputs."""
    with patch("agenthub.streaming.receive_response_safe") as mock_recv:
        async def _gen(s: Any):  # type: ignore[no-untyped-def]
            for m in messages:
                yield m
        mock_recv.return_value = _gen(session)
        return [event async for event in stream_response(session, **kwargs)]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestStreamResponseBasic:
    @pytest.mark.asyncio
    async def test_empty_stream(self) -> None:
        events = await _collect(FakeSession(), [])
        assert isinstance(events[0], StreamStart)
        assert isinstance(events[-1], StreamEnd)

    @pytest.mark.asyncio
    async def test_text_delta(self) -> None:
        messages = [
            _se({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hello"}}),
            _result(),
        ]
        events = await _collect(FakeSession(), messages)
        deltas = [e for e in events if isinstance(e, TextDelta)]
        assert len(deltas) == 1
        assert deltas[0].text == "Hello"

    @pytest.mark.asyncio
    async def test_end_turn_flush(self) -> None:
        messages = [
            _se({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hello world"}}),
            _se({"type": "message_delta", "delta": {"stop_reason": "end_turn"}}),
            _result(),
        ]
        events = await _collect(FakeSession(), messages)
        flushes = [e for e in events if isinstance(e, TextFlush)]
        assert len(flushes) >= 1
        assert "Hello world" in flushes[0].text
        assert flushes[0].reason == "end_turn"

    @pytest.mark.asyncio
    async def test_query_result(self) -> None:
        messages = [_result("s1", 0.05)]
        events = await _collect(FakeSession(), messages)
        results = [e for e in events if isinstance(e, QueryResult)]
        assert len(results) == 1
        assert results[0].session_id == "s1"
        assert results[0].cost_usd == 0.05


class TestStreamResponseThinking:
    @pytest.mark.asyncio
    async def test_thinking_lifecycle(self) -> None:
        messages = [
            _se({"type": "content_block_start", "content_block": {"type": "thinking"}}),
            _se({"type": "content_block_start", "content_block": {"type": "text"}}),
            _result(),
        ]
        events = await _collect(FakeSession(), messages)
        assert any(isinstance(e, ThinkingStart) for e in events)
        assert any(isinstance(e, ThinkingEnd) for e in events)


class TestStreamResponseToolUse:
    @pytest.mark.asyncio
    async def test_tool_use_lifecycle(self) -> None:
        session = FakeSession()
        session.activity = ActivityState(phase="waiting", tool_name="Bash")
        messages = [
            _se({"type": "content_block_start", "content_block": {"type": "tool_use", "name": "Bash"}, "index": 0}),
            _se({"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": '{"command":"ls"}'}}),
            _se({"type": "content_block_stop"}),
            _result(),
        ]
        events = await _collect(session, messages)
        starts = [e for e in events if isinstance(e, ToolUseStart)]
        ends = [e for e in events if isinstance(e, ToolUseEnd)]
        assert len(starts) == 1
        assert starts[0].tool_name == "Bash"
        assert len(ends) == 1
        assert ends[0].preview == "ls"

    @pytest.mark.asyncio
    async def test_todo_write_extraction(self) -> None:
        session = FakeSession()
        session.activity = ActivityState(phase="waiting", tool_name="TodoWrite")
        todo_json = '{"todos":[{"content":"fix bug","status":"pending"}]}'
        messages = [
            _se({"type": "content_block_start", "content_block": {"type": "tool_use", "name": "TodoWrite"}, "index": 0}),
            _se({"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": todo_json}}),
            _se({"type": "content_block_stop"}),
            _result(),
        ]
        events = await _collect(session, messages)
        todos = [e for e in events if isinstance(e, TodoUpdate)]
        assert len(todos) == 1
        assert todos[0].todos[0]["content"] == "fix bug"


class TestStreamResponseErrors:
    @pytest.mark.asyncio
    async def test_rate_limit(self) -> None:
        messages = [_assistant(error="rate_limit", text="Rate limited")]
        events = await _collect(FakeSession(), messages)
        hits = [e for e in events if isinstance(e, RateLimitHit)]
        assert len(hits) == 1
        assert hits[0].error_type == "rate_limit"

    @pytest.mark.asyncio
    async def test_transient_error(self) -> None:
        messages = [_assistant(error="overloaded")]
        events = await _collect(FakeSession(), messages)
        errors = [e for e in events if isinstance(e, TransientError)]
        assert len(errors) == 1
        assert errors[0].error_type == "overloaded"

    @pytest.mark.asyncio
    async def test_stream_killed(self) -> None:
        messages = [
            _se({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "partial"}}),
        ]
        events = await _collect(FakeSession(), messages)
        assert any(isinstance(e, StreamKilled) for e in events)
        # Partial text should be flushed
        flushes = [e for e in events if isinstance(e, TextFlush)]
        assert any("partial" in f.text for f in flushes)


class TestStreamResponseFlowchart:
    @pytest.mark.asyncio
    async def test_flowchart_block(self) -> None:
        messages = [
            _system("block_start", {"data": {"block_name": "build", "block_type": "action"}}),
            _result(),
        ]
        events = await _collect(FakeSession(), messages)
        blocks = [e for e in events if isinstance(e, BlockStart)]
        assert len(blocks) == 1
        assert blocks[0].block_name == "build"

    @pytest.mark.asyncio
    async def test_flowchart_complete(self) -> None:
        messages = [
            _system("flowchart_complete", {"data": {"status": "completed", "duration_ms": 3000, "cost_usd": 0.1, "blocks_executed": 5}}),
            _result(),
        ]
        events = await _collect(FakeSession(), messages)
        ends = [e for e in events if isinstance(e, FlowchartEnd)]
        assert len(ends) == 1
        assert ends[0].status == "completed"


class TestStreamResponseCompaction:
    @pytest.mark.asyncio
    async def test_compact_start(self) -> None:
        messages = [_system("status", {"status": "compacting"}), _result()]
        events = await _collect(FakeSession(), messages)
        assert any(isinstance(e, CompactStart) for e in events)


class TestExtractToolPreview:
    def test_bash_preview(self) -> None:
        assert _extract_tool_preview("Bash", '{"command": "ls -la"}') == "ls -la"

    def test_read_preview(self) -> None:
        assert _extract_tool_preview("Read", '{"file_path": "/foo/bar.py"}') == "/foo/bar.py"

    def test_grep_preview(self) -> None:
        result = _extract_tool_preview("Grep", '{"pattern": "foo", "path": "/src"}')
        assert result is not None
        assert "foo" in result

    def test_glob_preview(self) -> None:
        assert _extract_tool_preview("Glob", '{"pattern": "**/*.py"}') == "**/*.py"

    def test_partial_json_bash(self) -> None:
        assert _extract_tool_preview("Bash", '{"command": "git status') == "git status"

    def test_unknown_tool(self) -> None:
        assert _extract_tool_preview("Unknown", '{"foo": "bar"}') is None


class TestMidTurnSplit:
    @pytest.mark.asyncio
    async def test_large_text_gets_split(self) -> None:
        big_text = "x" * 2000
        messages = [
            _se({"type": "content_block_delta", "delta": {"type": "text_delta", "text": big_text}}),
            _result(),
        ]
        events = await _collect(FakeSession(), messages)
        flushes = [e for e in events if isinstance(e, TextFlush)]
        assert len(flushes) >= 1
        total_text = "".join(f.text for f in flushes)
        assert len(total_text) == 2000
