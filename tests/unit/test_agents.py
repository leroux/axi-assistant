"""Unit tests for pure functions in agents.py."""

from agents import (
    content_summary,
    extract_tool_preview,
    format_channel_topic,
    format_time_remaining,
    normalize_channel_name,
    split_message,
)
from axi_types import AgentSession


class TestSplitMessage:
    def test_short_message(self) -> None:
        assert split_message("hello") == ["hello"]

    def test_exact_limit(self) -> None:
        text = "a" * 2000
        assert split_message(text) == [text]

    def test_over_limit_splits_at_newline(self) -> None:
        text = "a" * 1990 + "\n" + "b" * 20
        chunks = split_message(text)
        assert len(chunks) == 2
        assert all(len(c) <= 2000 for c in chunks)

    def test_no_newline_splits_at_limit(self) -> None:
        text = "a" * 3000
        chunks = split_message(text)
        assert len(chunks) == 2
        assert len(chunks[0]) == 2000
        assert len(chunks[1]) == 1000

    def test_custom_limit(self) -> None:
        text = "a" * 100
        chunks = split_message(text, limit=50)
        assert len(chunks) == 2


class TestFormatTimeRemaining:
    def test_seconds(self) -> None:
        assert format_time_remaining(30) == "30s"

    def test_minutes(self) -> None:
        assert format_time_remaining(120) == "2m"

    def test_minutes_and_seconds(self) -> None:
        assert format_time_remaining(90) == "1m 30s"

    def test_hours(self) -> None:
        assert format_time_remaining(3600) == "1h"

    def test_hours_and_minutes(self) -> None:
        assert format_time_remaining(5400) == "1h 30m"

    def test_zero(self) -> None:
        assert format_time_remaining(0) == "0s"


class TestNormalizeChannelName:
    def test_lowercase(self) -> None:
        assert normalize_channel_name("MyAgent") == "myagent"

    def test_spaces_to_hyphens(self) -> None:
        assert normalize_channel_name("my agent") == "my-agent"

    def test_special_chars_removed(self) -> None:
        assert normalize_channel_name("agent@v2!") == "agentv2"

    def test_truncation(self) -> None:
        long_name = "a" * 150
        assert len(normalize_channel_name(long_name)) == 100


class TestIsAwakeAndIsProcessing:
    def test_is_awake_no_client(self) -> None:
        from agents import is_awake

        session = AgentSession(name="test")
        assert not is_awake(session)

    def test_is_processing_no_lock(self) -> None:
        from agents import is_processing

        session = AgentSession(name="test")
        assert not is_processing(session)


class TestContentSummary:
    def test_string_content(self) -> None:
        assert content_summary("hello world") == "hello world"

    def test_long_string_truncated(self) -> None:
        text = "x" * 300
        result = content_summary(text)
        assert len(result) <= 200

    def test_block_content(self) -> None:
        blocks = [{"type": "text", "text": "hello"}]
        assert "hello" in content_summary(blocks)

    def test_image_block(self) -> None:
        blocks = [{"type": "image", "mimeType": "image/png"}]
        result = content_summary(blocks)
        assert "image" in result


class TestExtractToolPreview:
    def test_bash_command(self) -> None:
        result = extract_tool_preview("Bash", '{"command": "ls -la"}')
        assert result == "ls -la"

    def test_read_file(self) -> None:
        result = extract_tool_preview("Read", '{"file_path": "/tmp/test.py"}')
        assert result == "/tmp/test.py"

    def test_grep_pattern(self) -> None:
        result = extract_tool_preview("Grep", '{"pattern": "TODO", "path": "src"}')
        assert result is not None
        assert "TODO" in result

    def test_invalid_json_bash_fallback(self) -> None:
        result = extract_tool_preview("Bash", '{"command": "echo hello')
        assert result == "echo hello"

    def test_unknown_tool(self) -> None:
        result = extract_tool_preview("UnknownTool", '{"foo": "bar"}')
        assert result is None


class TestFormatChannelTopic:
    def test_cwd_only(self) -> None:
        result = format_channel_topic("/home/user/project")
        assert result == "cwd: /home/user/project"

    def test_with_session_id(self) -> None:
        result = format_channel_topic("/tmp", session_id="abc123")
        assert "session: abc123" in result

    def test_with_prompt_hash(self) -> None:
        result = format_channel_topic("/tmp", prompt_hash="deadbeef")
        assert "prompt_hash: deadbeef" in result

    def test_all_fields(self) -> None:
        result = format_channel_topic("/tmp", session_id="sid", prompt_hash="hash")
        assert "cwd: /tmp" in result
        assert "session: sid" in result
        assert "prompt_hash: hash" in result
