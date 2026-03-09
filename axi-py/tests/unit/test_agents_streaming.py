"""Unit tests for agents.streaming — activity tracking, tool preview, and live-edit streaming."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from axi.agents import (
    _live_edit_finalize,
    _live_edit_tick,
    _LiveEditState,
    _StreamCtx,
    _update_activity,
    extract_tool_preview,
)
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


# ---------------------------------------------------------------------------
# Live-edit streaming tests
# ---------------------------------------------------------------------------


class TestLiveEditState:
    """Tests for _LiveEditState initialization and state management."""

    def test_initial_state(self) -> None:
        le = _LiveEditState(channel_id=12345)
        assert le.channel_id == 12345
        assert le.message_id is None
        assert le.content == ""
        assert le.last_edit_time == 0.0
        assert le.finalized is False
        assert le.edit_pending is False

    def test_stream_ctx_with_live_edit(self) -> None:
        le = _LiveEditState(channel_id=12345)
        ctx = _StreamCtx(live_edit=le)
        assert ctx.live_edit is le

    def test_stream_ctx_without_live_edit(self) -> None:
        ctx = _StreamCtx()
        assert ctx.live_edit is None


class TestLiveEditTick:
    """Tests for _live_edit_tick — the per-token streaming logic."""

    def _make_ctx(self, channel_id: int = 12345) -> _StreamCtx:
        le = _LiveEditState(channel_id=channel_id)
        return _StreamCtx(live_edit=le)

    def _make_session(self) -> AgentSession:
        return AgentSession(name="test")

    @pytest.mark.asyncio
    async def test_first_chunk_posts_message(self) -> None:
        ctx = self._make_ctx()
        session = self._make_session()
        ctx.text_buffer = "Hello"

        mock_resp = {"id": "msg_001"}
        with patch("axi.config.discord_client") as mock_client:
            mock_client.send_message = AsyncMock(return_value=mock_resp)
            await _live_edit_tick(ctx, session)

        assert ctx.live_edit is not None
        assert ctx.live_edit.message_id == "msg_001"
        mock_client.send_message.assert_called_once_with(12345, "Hello\u2588")

    @pytest.mark.asyncio
    async def test_throttled_edit_respects_interval(self) -> None:
        ctx = self._make_ctx()
        session = self._make_session()
        assert ctx.live_edit is not None
        ctx.live_edit.message_id = "msg_001"
        ctx.live_edit.last_edit_time = time.monotonic()  # Just edited
        ctx.text_buffer = "Hello world"

        with patch("axi.config.discord_client") as mock_client:
            mock_client.edit_message = AsyncMock(return_value={})
            await _live_edit_tick(ctx, session)

        # Should NOT have edited — not enough time has passed
        mock_client.edit_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_edit_after_interval(self) -> None:
        ctx = self._make_ctx()
        session = self._make_session()
        assert ctx.live_edit is not None
        ctx.live_edit.message_id = "msg_001"
        ctx.live_edit.last_edit_time = time.monotonic() - 10.0  # 10s ago (well past any interval)
        ctx.text_buffer = "Hello world updated"

        with patch("axi.config.discord_client") as mock_client:
            mock_client.edit_message = AsyncMock(return_value={})
            await _live_edit_tick(ctx, session)

        mock_client.edit_message.assert_called_once_with(12345, "msg_001", "Hello world updated\u2588")

    @pytest.mark.asyncio
    async def test_split_on_long_content(self) -> None:
        ctx = self._make_ctx()
        session = self._make_session()
        assert ctx.live_edit is not None
        ctx.live_edit.message_id = "msg_001"
        ctx.live_edit.last_edit_time = 0.0
        # Build content that exceeds 1900 chars
        ctx.text_buffer = "A" * 1950

        with patch("axi.config.discord_client") as mock_client:
            mock_client.edit_message = AsyncMock(return_value={})
            mock_client.send_message = AsyncMock(return_value={"id": "msg_002"})
            await _live_edit_tick(ctx, session)

        # Should have edited first message (finalized, no cursor)
        mock_client.edit_message.assert_called_once()
        first_edit_content = mock_client.edit_message.call_args[0][2]
        assert len(first_edit_content) <= 1900
        assert "\u2588" not in first_edit_content

        # Should have posted a new message with remainder + cursor
        mock_client.send_message.assert_called_once()
        new_msg_content = mock_client.send_message.call_args[0][1]
        assert new_msg_content.endswith("\u2588")
        assert ctx.live_edit.message_id == "msg_002"

    @pytest.mark.asyncio
    async def test_empty_buffer_noop(self) -> None:
        ctx = self._make_ctx()
        session = self._make_session()
        ctx.text_buffer = ""

        with patch("axi.config.discord_client") as mock_client:
            await _live_edit_tick(ctx, session)

        mock_client.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_whitespace_only_buffer_noop(self) -> None:
        ctx = self._make_ctx()
        session = self._make_session()
        ctx.text_buffer = "   \n  "

        with patch("axi.config.discord_client") as mock_client:
            await _live_edit_tick(ctx, session)

        mock_client.send_message.assert_not_called()


class TestLiveEditFinalize:
    """Tests for _live_edit_finalize — final edit to remove cursor."""

    def _make_ctx(self, channel_id: int = 12345) -> _StreamCtx:
        le = _LiveEditState(channel_id=channel_id)
        return _StreamCtx(live_edit=le)

    def _make_session(self) -> AgentSession:
        return AgentSession(name="test")

    @pytest.mark.asyncio
    async def test_finalize_removes_cursor(self) -> None:
        ctx = self._make_ctx()
        session = self._make_session()
        assert ctx.live_edit is not None
        ctx.live_edit.message_id = "msg_001"
        ctx.text_buffer = "Final content here"

        with patch("axi.config.discord_client") as mock_client:
            mock_client.edit_message = AsyncMock(return_value={})
            await _live_edit_finalize(ctx, session)

        mock_client.edit_message.assert_called_once_with(12345, "msg_001", "Final content here")

    @pytest.mark.asyncio
    async def test_finalize_resets_state(self) -> None:
        ctx = self._make_ctx()
        session = self._make_session()
        assert ctx.live_edit is not None
        ctx.live_edit.message_id = "msg_001"
        ctx.text_buffer = "Done"

        with patch("axi.config.discord_client") as mock_client:
            mock_client.edit_message = AsyncMock(return_value={})
            await _live_edit_finalize(ctx, session)

        assert ctx.live_edit.message_id is None
        assert ctx.live_edit.content == ""

    @pytest.mark.asyncio
    async def test_finalize_no_message_sends_normally(self) -> None:
        ctx = self._make_ctx()
        session = self._make_session()
        ctx.text_buffer = "Never posted content"

        with patch("axi.config.discord_client") as mock_client:
            mock_client.send_message = AsyncMock(return_value={"id": "msg_001"})
            await _live_edit_finalize(ctx, session)

        mock_client.send_message.assert_called_once_with(12345, "Never posted content")

    @pytest.mark.asyncio
    async def test_finalize_empty_buffer_noop(self) -> None:
        ctx = self._make_ctx()
        session = self._make_session()
        assert ctx.live_edit is not None
        ctx.live_edit.message_id = "msg_001"
        ctx.text_buffer = ""

        with patch("axi.config.discord_client") as mock_client:
            await _live_edit_finalize(ctx, session)

        mock_client.edit_message.assert_not_called()
        mock_client.send_message.assert_not_called()
