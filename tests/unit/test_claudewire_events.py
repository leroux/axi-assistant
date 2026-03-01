"""Tests for claudewire.events — ActivityState, update_activity, tool_display, as_stream."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from claudewire.events import ActivityState, as_stream, tool_display, update_activity

# ---------------------------------------------------------------------------
# ActivityState defaults
# ---------------------------------------------------------------------------


class TestActivityState:
    def test_defaults(self) -> None:
        s = ActivityState()
        assert s.phase == "idle"
        assert s.tool_name is None
        assert s.tool_input_preview == ""
        assert s.thinking_text == ""
        assert s.turn_count == 0
        assert s.query_started is None
        assert s.last_event is None
        assert s.text_chars == 0

    def test_custom_values(self) -> None:
        s = ActivityState(phase="thinking", turn_count=3, text_chars=42)
        assert s.phase == "thinking"
        assert s.turn_count == 3
        assert s.text_chars == 42


# ---------------------------------------------------------------------------
# tool_display
# ---------------------------------------------------------------------------


class TestToolDisplay:
    def test_known_tools(self) -> None:
        assert tool_display("Bash") == "running bash command"
        assert tool_display("Read") == "reading file"
        assert tool_display("Write") == "writing file"
        assert tool_display("Edit") == "editing file"
        assert tool_display("MultiEdit") == "editing file"
        assert tool_display("Glob") == "searching for files"
        assert tool_display("Grep") == "searching code"
        assert tool_display("WebSearch") == "searching the web"
        assert tool_display("WebFetch") == "fetching web page"
        assert tool_display("Task") == "running subagent"
        assert tool_display("NotebookEdit") == "editing notebook"
        assert tool_display("TodoWrite") == "updating tasks"

    def test_mcp_three_parts(self) -> None:
        assert tool_display("mcp__server__action") == "server: action"

    def test_mcp_two_parts(self) -> None:
        assert tool_display("mcp__only") == "using mcp__only"

    def test_unknown(self) -> None:
        assert tool_display("CustomTool") == "using CustomTool"

    def test_empty(self) -> None:
        assert tool_display("") == "using "


# ---------------------------------------------------------------------------
# update_activity
# ---------------------------------------------------------------------------


class TestUpdateActivity:
    def test_content_block_start_tool_use(self) -> None:
        a = ActivityState()
        update_activity(a, {
            "type": "content_block_start",
            "content_block": {"type": "tool_use", "name": "Bash"},
        })
        assert a.phase == "tool_use"
        assert a.tool_name == "Bash"
        assert a.tool_input_preview == ""
        assert a.last_event is not None

    def test_content_block_start_thinking(self) -> None:
        a = ActivityState(thinking_text="old")
        update_activity(a, {
            "type": "content_block_start",
            "content_block": {"type": "thinking"},
        })
        assert a.phase == "thinking"
        assert a.tool_name is None
        assert a.thinking_text == ""

    def test_content_block_start_text(self) -> None:
        a = ActivityState(text_chars=99)
        update_activity(a, {
            "type": "content_block_start",
            "content_block": {"type": "text"},
        })
        assert a.phase == "writing"
        assert a.tool_name is None
        assert a.text_chars == 0

    def test_content_block_delta_thinking(self) -> None:
        a = ActivityState(phase="thinking")
        update_activity(a, {
            "type": "content_block_delta",
            "delta": {"type": "thinking_delta", "thinking": "hello "},
        })
        update_activity(a, {
            "type": "content_block_delta",
            "delta": {"type": "thinking_delta", "thinking": "world"},
        })
        assert a.thinking_text == "hello world"
        assert a.phase == "thinking"

    def test_content_block_delta_text(self) -> None:
        a = ActivityState(phase="writing")
        update_activity(a, {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "hello"},
        })
        assert a.text_chars == 5
        assert a.phase == "writing"

    def test_content_block_delta_input_json(self) -> None:
        a = ActivityState(phase="tool_use", tool_name="Bash")
        update_activity(a, {
            "type": "content_block_delta",
            "delta": {"type": "input_json_delta", "partial_json": '{"cmd":'},
        })
        assert a.tool_input_preview == '{"cmd":'

    def test_content_block_delta_input_json_truncates_at_200(self) -> None:
        a = ActivityState(phase="tool_use")
        update_activity(a, {
            "type": "content_block_delta",
            "delta": {"type": "input_json_delta", "partial_json": "x" * 300},
        })
        assert len(a.tool_input_preview) == 200

    def test_content_block_delta_input_json_skips_when_full(self) -> None:
        a = ActivityState(phase="tool_use", tool_input_preview="y" * 200)
        update_activity(a, {
            "type": "content_block_delta",
            "delta": {"type": "input_json_delta", "partial_json": "more"},
        })
        assert len(a.tool_input_preview) == 200
        assert a.tool_input_preview == "y" * 200

    def test_content_block_stop_from_tool_use(self) -> None:
        a = ActivityState(phase="tool_use")
        update_activity(a, {"type": "content_block_stop"})
        assert a.phase == "waiting"

    def test_content_block_stop_from_other_phase(self) -> None:
        a = ActivityState(phase="writing")
        update_activity(a, {"type": "content_block_stop"})
        assert a.phase == "writing"

    def test_message_start_increments_turn(self) -> None:
        a = ActivityState(turn_count=2)
        update_activity(a, {"type": "message_start"})
        assert a.turn_count == 3

    def test_message_delta_end_turn(self) -> None:
        a = ActivityState(phase="writing", tool_name="Bash")
        update_activity(a, {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
        })
        assert a.phase == "idle"
        assert a.tool_name is None

    def test_message_delta_tool_use(self) -> None:
        a = ActivityState(phase="writing")
        update_activity(a, {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
        })
        assert a.phase == "waiting"

    def test_message_delta_no_stop_reason(self) -> None:
        a = ActivityState(phase="writing")
        update_activity(a, {"type": "message_delta", "delta": {}})
        assert a.phase == "writing"

    def test_unknown_event_type_no_error(self) -> None:
        a = ActivityState()
        update_activity(a, {"type": "unknown_event"})
        assert a.last_event is not None

    def test_last_event_updated(self) -> None:
        a = ActivityState()
        before = datetime.now(UTC)
        update_activity(a, {"type": "message_start"})
        assert a.last_event is not None
        assert a.last_event >= before

    def test_missing_fields_in_event(self) -> None:
        """Events with missing fields shouldn't crash."""
        a = ActivityState()
        update_activity(a, {"type": "content_block_start", "content_block": {}})
        update_activity(a, {"type": "content_block_delta", "delta": {}})
        update_activity(a, {"type": "message_delta"})


# ---------------------------------------------------------------------------
# as_stream
# ---------------------------------------------------------------------------


class TestAsStream:
    @pytest.mark.asyncio
    async def test_yields_user_message(self) -> None:
        results = [msg async for msg in as_stream("hello")]
        assert len(results) == 1
        msg = results[0]
        assert msg["type"] == "user"
        assert msg["message"]["role"] == "user"
        assert msg["message"]["content"] == "hello"
        assert msg["parent_tool_use_id"] is None
        assert msg["session_id"] == ""

    @pytest.mark.asyncio
    async def test_with_list_content(self) -> None:
        content = [{"type": "text", "text": "hi"}]
        results = [msg async for msg in as_stream(content)]
        assert results[0]["message"]["content"] == content
