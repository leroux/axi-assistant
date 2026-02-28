"""Unit tests for agents.streaming — activity tracking and tool preview."""

from __future__ import annotations

from axi.agents import _StreamCtx, _update_activity, extract_tool_preview
from axi.axi_types import AgentSession


class TestUpdateActivity:
    """Tests for _update_activity — pure function updating agent activity state."""

    def _make_session(self) -> AgentSession:
        return AgentSession(name="test")

    def test_content_block_start_tool_use(self) -> None:
        session = self._make_session()
        _update_activity(session, {
            "type": "content_block_start",
            "content_block": {"type": "tool_use", "name": "Bash"},
        })
        assert session.activity.phase == "tool_use"
        assert session.activity.tool_name == "Bash"

    def test_content_block_start_thinking(self) -> None:
        session = self._make_session()
        _update_activity(session, {
            "type": "content_block_start",
            "content_block": {"type": "thinking"},
        })
        assert session.activity.phase == "thinking"
        assert session.activity.tool_name is None

    def test_content_block_start_text(self) -> None:
        session = self._make_session()
        _update_activity(session, {
            "type": "content_block_start",
            "content_block": {"type": "text"},
        })
        assert session.activity.phase == "writing"
        assert session.activity.text_chars == 0

    def test_content_block_delta_text(self) -> None:
        session = self._make_session()
        session.activity.phase = "writing"
        _update_activity(session, {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "hello"},
        })
        assert session.activity.text_chars == 5

    def test_content_block_delta_thinking(self) -> None:
        session = self._make_session()
        session.activity.phase = "thinking"
        _update_activity(session, {
            "type": "content_block_delta",
            "delta": {"type": "thinking_delta", "thinking": "hmm"},
        })
        assert session.activity.thinking_text == "hmm"

    def test_content_block_delta_input_json(self) -> None:
        session = self._make_session()
        session.activity.phase = "tool_use"
        _update_activity(session, {
            "type": "content_block_delta",
            "delta": {"type": "input_json_delta", "partial_json": '{"cmd":'},
        })
        assert session.activity.tool_input_preview == '{"cmd":'

    def test_content_block_stop_tool_use(self) -> None:
        session = self._make_session()
        session.activity.phase = "tool_use"
        _update_activity(session, {"type": "content_block_stop"})
        assert session.activity.phase == "waiting"

    def test_message_start_increments_turn(self) -> None:
        session = self._make_session()
        assert session.activity.turn_count == 0
        _update_activity(session, {"type": "message_start"})
        assert session.activity.turn_count == 1

    def test_message_delta_end_turn(self) -> None:
        session = self._make_session()
        session.activity.phase = "writing"
        _update_activity(session, {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
        })
        assert session.activity.phase == "idle"
        assert session.activity.tool_name is None

    def test_message_delta_tool_use(self) -> None:
        session = self._make_session()
        session.activity.phase = "writing"
        _update_activity(session, {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
        })
        assert session.activity.phase == "waiting"

    def test_last_event_updated(self) -> None:
        session = self._make_session()
        assert session.activity.last_event is None
        _update_activity(session, {"type": "message_start"})
        assert session.activity.last_event is not None


class TestStreamCtx:
    """Tests for _StreamCtx state transitions."""

    def test_initial_state(self) -> None:
        ctx = _StreamCtx()
        assert ctx.text_buffer == ""
        assert ctx.hit_rate_limit is False
        assert ctx.hit_transient_error is None
        assert ctx.typing_stopped is False
        assert ctx.flush_count == 0
        assert ctx.msg_total == 0

    def test_text_accumulation(self) -> None:
        ctx = _StreamCtx()
        ctx.text_buffer += "hello "
        ctx.text_buffer += "world"
        assert ctx.text_buffer == "hello world"

    def test_rate_limit_flag(self) -> None:
        ctx = _StreamCtx()
        ctx.hit_rate_limit = True
        assert ctx.hit_rate_limit

    def test_transient_error(self) -> None:
        ctx = _StreamCtx()
        ctx.hit_transient_error = "overloaded"
        assert ctx.hit_transient_error == "overloaded"


class TestExtractToolPreviewExtended:
    """Additional tests for extract_tool_preview."""

    def test_glob_pattern(self) -> None:
        result = extract_tool_preview("Glob", '{"pattern": "**/*.py"}')
        assert result == "**/*.py"

    def test_write_file(self) -> None:
        result = extract_tool_preview("Write", '{"file_path": "/tmp/out.txt", "content": "..."}')
        assert result == "/tmp/out.txt"

    def test_edit_file(self) -> None:
        result = extract_tool_preview("Edit", '{"file_path": "/src/main.py"}')
        assert result == "/src/main.py"

    def test_invalid_json_read_fallback(self) -> None:
        result = extract_tool_preview("Read", '{"file_path": "/tmp/test.py')
        assert result == "/tmp/test.py"

    def test_completely_broken_json(self) -> None:
        result = extract_tool_preview("Bash", "not json at all")
        assert result is None
