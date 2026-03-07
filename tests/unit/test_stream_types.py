"""Unit tests for StreamOutput types — validates the normalized event types."""

from __future__ import annotations

from agenthub.stream_types import (
    BlockComplete,
    BlockStart,
    CompactComplete,
    CompactStart,
    FlowchartEnd,
    FlowchartStart,
    QueryResult,
    RateLimitHit,
    SessionId,
    StreamEnd,
    StreamKilled,
    StreamOutput,
    StreamStart,
    SystemNotification,
    TextDelta,
    TextFlush,
    ThinkingEnd,
    ThinkingStart,
    TodoUpdate,
    ToolInputDelta,
    ToolUseEnd,
    ToolUseStart,
    TransientError,
)


class TestStreamOutputTypes:
    """Verify that all StreamOutput types can be instantiated and pattern-matched."""

    def test_text_delta(self) -> None:
        d = TextDelta(text="hello")
        assert d.text == "hello"

    def test_text_flush(self) -> None:
        f = TextFlush(text="content", reason="end_turn")
        assert f.text == "content"
        assert f.reason == "end_turn"

    def test_thinking_start_end(self) -> None:
        s = ThinkingStart()
        e = ThinkingEnd(thinking_text="I think...")
        assert e.thinking_text == "I think..."

    def test_tool_use_lifecycle(self) -> None:
        start = ToolUseStart(tool_name="Bash", index=0)
        delta = ToolInputDelta(partial_json='{"command":')
        end = ToolUseEnd(tool_name="Bash", tool_input={"command": "ls"}, preview="ls")
        assert start.tool_name == "Bash"
        assert delta.partial_json == '{"command":'
        assert end.preview == "ls"

    def test_todo_update(self) -> None:
        t = TodoUpdate(todos=[{"content": "fix bug", "status": "pending"}])
        assert len(t.todos) == 1

    def test_session_id(self) -> None:
        s = SessionId(session_id="abc-123")
        assert s.session_id == "abc-123"

    def test_stream_start_end(self) -> None:
        s = StreamStart()
        e = StreamEnd(elapsed_s=5.2, msg_count=100, flush_count=3)
        assert e.elapsed_s == 5.2

    def test_query_result(self) -> None:
        r = QueryResult(session_id="s1", cost_usd=0.05, num_turns=3, duration_ms=5000)
        assert r.cost_usd == 0.05
        assert not r.is_flowchart

    def test_query_result_flowchart(self) -> None:
        r = QueryResult(is_flowchart=True)
        assert r.is_flowchart

    def test_rate_limit_hit(self) -> None:
        r = RateLimitHit(error_type="rate_limit", error_text="too many requests")
        assert r.error_type == "rate_limit"

    def test_transient_error(self) -> None:
        e = TransientError(error_type="overloaded", error_text="server busy")
        assert e.error_type == "overloaded"

    def test_stream_killed(self) -> None:
        k = StreamKilled()
        assert isinstance(k, StreamKilled)

    def test_compact_events(self) -> None:
        s = CompactStart(token_count=50000, self_triggered=True)
        c = CompactComplete(pre_tokens=50000, trigger="axi")
        assert s.self_triggered
        assert c.pre_tokens == 50000

    def test_flowchart_events(self) -> None:
        s = FlowchartStart(command="deploy", block_count=5)
        e = FlowchartEnd(status="completed", duration_ms=3000, cost_usd=0.1, blocks_executed=5)
        assert s.command == "deploy"
        assert e.status == "completed"

    def test_block_events(self) -> None:
        s = BlockStart(block_name="build", block_type="action")
        c = BlockComplete(block_name="build", success=True)
        assert s.block_name == "build"
        assert c.success

    def test_system_notification(self) -> None:
        n = SystemNotification(subtype="custom", data={"key": "value"})
        assert n.subtype == "custom"

    def test_pattern_matching(self) -> None:
        """Verify Python match/case works with StreamOutput types."""
        events: list[StreamOutput] = [
            TextDelta(text="hi"),
            ThinkingStart(),
            QueryResult(cost_usd=0.01),
        ]
        kinds: list[str] = []
        for e in events:
            match e:
                case TextDelta(text=t):
                    kinds.append(f"text:{t}")
                case ThinkingStart():
                    kinds.append("thinking")
                case QueryResult(cost_usd=c):
                    kinds.append(f"result:{c}")
        assert kinds == ["text:hi", "thinking", "result:0.01"]

    def test_default_values(self) -> None:
        """All types should have sensible defaults for optional fields."""
        assert TextFlush(text="x").reason == ""
        assert ThinkingEnd().thinking_text == ""
        assert ToolUseEnd(tool_name="X").preview is None
        assert ToolUseEnd(tool_name="X").tool_input == {}
        assert TodoUpdate().todos == []
        assert StreamEnd().elapsed_s == 0.0
        assert QueryResult().session_id is None
        assert QueryResult().is_error is False
