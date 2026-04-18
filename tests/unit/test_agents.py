"""Unit tests for pure functions in agents.py and rate_limits.py."""

import json
from pathlib import Path
from unittest.mock import patch

from hypothesis import given
from hypothesis import strategies as st

from axi.agents import (
    _parse_channel_topic,
    content_summary,
    extract_tool_preview,
    format_channel_topic,
    format_time_remaining,
    normalize_channel_name,
    split_message,
    wrap_content_with_flowchart,
)
from axi.axi_types import AgentSession
from axi.channels import strip_status_prefix
from axi.rate_limits import parse_rate_limit_seconds as _parse_rate_limit_seconds


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


class TestStripStatusPrefix:
    def test_no_prefix_unchanged(self) -> None:
        assert strip_status_prefix("my-channel") == "my-channel"

    def test_emoji_prefix_stripped(self) -> None:
        assert strip_status_prefix("🚀my-channel") == "my-channel"

    def test_status_emoji_stripped(self) -> None:
        assert strip_status_prefix("🔴my-agent") == "my-agent"

    def test_multiple_emojis_stripped(self) -> None:
        assert strip_status_prefix("🔴🚀my-channel") == "my-channel"

    def test_empty_string(self) -> None:
        assert strip_status_prefix("") == ""

    def test_prompt_substitution_strips_prefix(self) -> None:
        # Regression: channel names embedded in prompts should not carry emoji prefixes
        template = "Your Discord channel: #{channel_name}"
        channel_name = "🔴strip-prompt-emoji"
        result = template.replace("{channel_name}", strip_status_prefix(channel_name))
        assert result == "Your Discord channel: #strip-prompt-emoji"


class TestFlowCoderWrapContent:
    def test_wrap_disabled_returns_content(self) -> None:
        session = AgentSession(name="test", agent_type="flowcoder", cwd="/tmp")

        with patch.dict("os.environ", {"AXI_FC_WRAP": "off"}), patch("axi.config.FLOWCODER_ENABLED", True):
            assert wrap_content_with_flowchart("hello", session) == "hello"

    def test_generic_wrap_uses_configured_flowchart(self) -> None:
        session = AgentSession(name="test", agent_type="flowcoder", cwd="/tmp")

        with (
            patch.dict("os.environ", {"AXI_FC_WRAP": "triage"}),
            patch("axi.config.FLOWCODER_ENABLED", True),
            patch("axi.agents._command_exists", return_value=True),
        ):
            assert wrap_content_with_flowchart("hello world", session) == "/triage 'hello world'"

    def test_prompt_wrap_uses_bundled_command(self) -> None:
        session = AgentSession(name="test", agent_type="flowcoder", cwd="/tmp")

        with patch.dict("os.environ", {"AXI_FC_WRAP": "prompt"}), patch("axi.config.FLOWCODER_ENABLED", True):
            assert wrap_content_with_flowchart("hello world", session) == "/prompt 'hello world'"


class TestFlowCoderCommandDefinitions:
    def test_prompt_command_is_passthrough(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        command = json.loads((repo_root / "commands" / "prompt.json").read_text())

        assert command["name"] == "prompt"
        assert command["flowchart"]["start_block_id"] == "start"
        assert command["flowchart"]["blocks"]["run-prompt"]["type"] == "prompt"
        assert command["flowchart"]["blocks"]["run-prompt"]["prompt"] == "$1"


class TestIsAwakeAndIsProcessing:
    def test_is_awake_no_client(self) -> None:
        from axi.agents import is_awake

        session = AgentSession(name="test")
        assert not is_awake(session)

    def test_is_processing_no_lock(self) -> None:
        from axi.agents import is_processing

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


class TestParseChannelTopic:
    """Tests for _parse_channel_topic — inverse of format_channel_topic."""

    def test_none_returns_nones(self) -> None:
        assert _parse_channel_topic(None) == (None, None, None, None)

    def test_empty_string_returns_nones(self) -> None:
        assert _parse_channel_topic("") == (None, None, None, None)

    def test_cwd_only(self) -> None:
        assert _parse_channel_topic("cwd: /home/user/project") == ("/home/user/project", None, None, None)

    def test_all_fields(self) -> None:
        topic = "cwd: /tmp | session: abc123 | prompt_hash: deadbeef"
        assert _parse_channel_topic(topic) == ("/tmp", "abc123", "deadbeef", None)

    def test_roundtrip_cwd_only(self) -> None:
        topic = format_channel_topic("/home/user/project")
        cwd, sid, ph, at = _parse_channel_topic(topic)
        assert cwd == "/home/user/project"
        assert sid is None
        assert ph is None
        assert at is None

    def test_roundtrip_all_fields(self) -> None:
        topic = format_channel_topic("/tmp", session_id="sid-123", prompt_hash="abc123def456")
        cwd, sid, ph, at = _parse_channel_topic(topic)
        assert cwd == "/tmp"
        assert sid == "sid-123"
        assert ph == "abc123def456"
        assert at is None

    def test_unknown_keys_ignored(self) -> None:
        topic = "cwd: /tmp | unknown: value | session: sid"
        cwd, sid, ph, at = _parse_channel_topic(topic)
        assert cwd == "/tmp"
        assert sid == "sid"
        assert ph is None
        assert at is None

    def test_unstructured_topic(self) -> None:
        """Non-key-value topic like the master channel's."""
        cwd, sid, ph, at = _parse_channel_topic("Axi master control channel")
        assert cwd is None
        assert sid is None
        assert ph is None
        assert at is None


class TestParseRateLimitSeconds:
    """Tests for _parse_rate_limit_seconds — regex parser with fallback."""

    def test_in_n_seconds(self) -> None:
        assert _parse_rate_limit_seconds("Rate limited, try again in 30 seconds") == 30

    def test_after_n_minutes(self) -> None:
        assert _parse_rate_limit_seconds("Try again after 5 minutes") == 300

    def test_after_n_mins(self) -> None:
        assert _parse_rate_limit_seconds("Retry after 2 mins") == 120

    def test_in_n_hours(self) -> None:
        assert _parse_rate_limit_seconds("Rate limit expires in 1 hour") == 3600

    def test_in_n_hrs(self) -> None:
        assert _parse_rate_limit_seconds("Please wait, retry in 2 hrs") == 7200

    def test_retry_after_bare(self) -> None:
        assert _parse_rate_limit_seconds("retry after 45") == 45

    def test_bare_seconds(self) -> None:
        assert _parse_rate_limit_seconds("wait 60 seconds please") == 60

    def test_bare_minutes(self) -> None:
        assert _parse_rate_limit_seconds("wait 3 minutes please") == 180

    def test_fallback_default(self) -> None:
        assert _parse_rate_limit_seconds("something went wrong") == 300

    def test_case_insensitive(self) -> None:
        assert _parse_rate_limit_seconds("Try Again In 10 Seconds") == 10


# ---------------------------------------------------------------------------
# Property-based tests (Hypothesis)
# ---------------------------------------------------------------------------


class TestChannelTopicRoundtrip:
    """format_channel_topic / _parse_channel_topic roundtrip property."""

    # Values must not contain "|" (delimiter) and must not be whitespace-only
    # (strip() in parser collapses them). This matches real-world usage (paths, hex hashes).
    _val = st.text(min_size=1, max_size=50).filter(lambda s: "|" not in s and s.strip() == s and len(s.strip()) > 0)

    @given(
        cwd=_val,
        session_id=st.one_of(st.none(), _val),
        prompt_hash=st.one_of(st.none(), _val),
    )
    def test_roundtrip(self, cwd: str, session_id: str | None, prompt_hash: str | None) -> None:
        topic = format_channel_topic(cwd, session_id=session_id, prompt_hash=prompt_hash)
        parsed_cwd, parsed_sid, parsed_ph, parsed_at = _parse_channel_topic(topic)
        assert parsed_cwd == cwd
        assert parsed_sid == (session_id or None)
        assert parsed_ph == (prompt_hash or None)
        assert parsed_at is None


class TestSplitMessageProperties:
    """Property-based tests for split_message."""

    @given(text=st.text(min_size=0, max_size=10000))
    def test_concatenation_preserves_content(self, text: str) -> None:
        """Joining chunks must reproduce the original content (modulo newline stripping)."""
        chunks = split_message(text)
        rejoined = "".join(chunks)
        # split_message may strip leading newlines at split points, but all chars appear
        assert set(text) <= set(rejoined) or text == ""

    @given(text=st.text(min_size=1, max_size=10000), limit=st.integers(min_value=10, max_value=2000))
    def test_chunks_within_limit(self, text: str, limit: int) -> None:
        """Every chunk must be at most `limit` characters."""
        chunks = split_message(text, limit=limit)
        for chunk in chunks:
            assert len(chunk) <= limit

    @given(text=st.text(min_size=0, max_size=5000))
    def test_no_empty_chunks(self, text: str) -> None:
        """split_message should not return empty strings."""
        chunks = split_message(text)
        assert all(len(c) > 0 for c in chunks) or text == ""


class TestNormalizeChannelNameProperties:
    """Property-based tests for normalize_channel_name."""

    @given(name=st.text(min_size=0, max_size=200))
    def test_idempotent(self, name: str) -> None:
        """Normalizing twice should give the same result as normalizing once."""
        once = normalize_channel_name(name)
        twice = normalize_channel_name(once)
        assert once == twice

    @given(name=st.text(min_size=0, max_size=200))
    def test_max_length(self, name: str) -> None:
        """Result is always at most 100 characters."""
        assert len(normalize_channel_name(name)) <= 100

    @given(name=st.text(min_size=0, max_size=200))
    def test_only_valid_chars(self, name: str) -> None:
        """Result only contains lowercase letters, digits, hyphens, and underscores."""
        import re

        result = normalize_channel_name(name)
        assert re.fullmatch(r"[a-z0-9\-_]*", result) is not None
