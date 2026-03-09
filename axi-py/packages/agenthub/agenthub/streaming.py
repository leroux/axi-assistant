"""Frontend-agnostic stream engine — transforms raw SDK messages into StreamOutput events.

Iterates the SDK response stream, tracks activity state, and yields
normalized StreamOutput events that any frontend can consume.
No Discord/Slack/web code here.

Usage:
    async for event in stream_response(session):
        match event:
            case TextDelta(text=t): ...
            case QueryResult(cost_usd=c): ...
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from claude_agent_sdk._errors import MessageParseError
from claude_agent_sdk._internal.message_parser import parse_message
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
)

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
from claudewire.events import update_activity

if TYPE_CHECKING:
    from agenthub.types import AgentSession

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Safe message receiver (shared between this module and discord_stream.py)
# ---------------------------------------------------------------------------


async def receive_response_safe(session: AgentSession) -> AsyncIterator[Any]:
    """Iterate SDK messages, handling parse errors gracefully.

    Yields parsed SDK message objects (StreamEvent, AssistantMessage,
    ResultMessage, SystemMessage). Unknown/unparseable messages are logged
    and skipped.
    """
    assert session.client is not None
    assert session.client._query is not None  # pyright: ignore[reportPrivateUsage]
    async for data in session.client._query.receive_messages():  # pyright: ignore[reportPrivateUsage]
        try:
            parsed = parse_message(data)
        except MessageParseError:
            msg_type = data.get("type", "?")
            if msg_type == "rate_limit_event":
                log.info("Rate limit event for '%s': %s", session.name, data)
            else:
                log.warning(
                    "Unknown SDK message type from '%s': type=%s data=%s",
                    session.name,
                    msg_type,
                    json.dumps(data)[:500],
                )
            continue
        yield parsed
        if isinstance(parsed, ResultMessage):
            return


# ---------------------------------------------------------------------------
# Stream context (internal tracking state)
# ---------------------------------------------------------------------------


class _Ctx:
    """Internal state for one stream_response() invocation."""

    __slots__ = (
        "current_tool_name",
        "flush_count",
        "got_result",
        "hit_rate_limit",
        "hit_transient_error",
        "in_flowchart",
        "in_thinking",
        "msg_total",
        "text_buffer",
        "tool_input_json",
    )

    def __init__(self) -> None:
        self.got_result = False
        self.hit_rate_limit = False
        self.hit_transient_error: str | None = None
        self.in_flowchart = False
        self.msg_total = 0
        self.flush_count = 0
        self.text_buffer = ""
        self.tool_input_json = ""
        self.current_tool_name = ""
        self.in_thinking = False


# ---------------------------------------------------------------------------
# Main stream engine
# ---------------------------------------------------------------------------


async def stream_response(
    session: AgentSession,
    *,
    self_compacting_names: set[str] | None = None,
    compact_start_times: dict[str, float] | None = None,
    pending_compact: dict[str, dict[str, int | float]] | None = None,
    set_session_id_fn: Any = None,
    record_usage_fn: Any = None,
    report_unknown_fn: Any = None,
) -> AsyncIterator[StreamOutput]:
    """Yield normalized StreamOutput events from a Claude SDK response stream.

    This is the frontend-agnostic core. It:
    1. Iterates raw SDK messages
    2. Updates session.activity
    3. Yields StreamOutput events for the frontend to render
    4. Handles text buffering and mid-turn splitting
    5. Tracks tool input JSON for TodoWrite extraction
    6. Records session usage on QueryResult

    The caller (frontend stream renderer) consumes these events and
    renders them appropriately for its platform.
    """
    if self_compacting_names is None:
        self_compacting_names = set()
    if compact_start_times is None:
        compact_start_times = {}
    if pending_compact is None:
        pending_compact = {}

    ctx = _Ctx()
    t0 = time.monotonic()

    yield StreamStart()

    async for msg in receive_response_safe(session):
        ctx.msg_total += 1

        if isinstance(msg, StreamEvent):
            async for out in _handle_stream_event(ctx, session, msg, set_session_id_fn):
                yield out

        elif isinstance(msg, AssistantMessage):
            async for out in _handle_assistant_message(ctx, session, msg):
                yield out

        elif isinstance(msg, ResultMessage):
            async for out in _handle_result_message(
                ctx, session, msg, set_session_id_fn, record_usage_fn
            ):
                yield out

        elif isinstance(msg, SystemMessage):
            if msg.subtype == "flowchart_start":
                ctx.in_flowchart = True
            elif msg.subtype == "flowchart_complete":
                ctx.in_flowchart = False
            async for out in _handle_system_message(
                ctx, session, msg, self_compacting_names, compact_start_times, pending_compact
            ):
                yield out

        # Mid-turn text splitting (when buffer gets large)
        if not ctx.hit_rate_limit and len(ctx.text_buffer) >= 1800:
            split_at = ctx.text_buffer.rfind("\n", 0, 1800)
            if split_at == -1:
                split_at = 1800
            flush_text = ctx.text_buffer[:split_at]
            ctx.text_buffer = ctx.text_buffer[split_at:].lstrip("\n")
            ctx.flush_count += 1
            yield TextFlush(text=flush_text, reason="mid_turn_split")

    # Post-loop: determine terminal state
    elapsed = time.monotonic() - t0

    if ctx.hit_rate_limit:
        pass  # Already yielded RateLimitHit

    elif ctx.hit_transient_error:
        if ctx.text_buffer.strip():
            ctx.flush_count += 1
            yield TextFlush(text=ctx.text_buffer, reason="transient_error")
            ctx.text_buffer = ""
        yield TransientError(error_type=ctx.hit_transient_error)

    elif not ctx.got_result:
        if ctx.text_buffer.strip():
            ctx.flush_count += 1
            yield TextFlush(text=ctx.text_buffer, reason="post_kill")
            ctx.text_buffer = ""
        yield StreamKilled()

    else:
        # Normal completion — flush remaining text
        if ctx.text_buffer.strip():
            ctx.flush_count += 1
            yield TextFlush(text=ctx.text_buffer, reason="post_loop")
            ctx.text_buffer = ""

    yield StreamEnd(
        elapsed_s=elapsed,
        msg_count=ctx.msg_total,
        flush_count=ctx.flush_count,
    )


# ---------------------------------------------------------------------------
# SDK message handlers -> StreamOutput generators
# ---------------------------------------------------------------------------


async def _handle_stream_event(
    ctx: _Ctx,
    session: AgentSession,
    msg: StreamEvent,
    set_session_id_fn: Any,
) -> AsyncIterator[StreamOutput]:
    """Transform a StreamEvent into StreamOutput events."""
    event = msg.event
    event_type = event.get("type", "")

    # Session ID tracking
    if not ctx.in_flowchart and msg.session_id and msg.session_id != session.session_id:
        if set_session_id_fn:
            await set_session_id_fn(session, msg.session_id)
        yield SessionId(session_id=msg.session_id)

    # Activity tracking (updates session.activity in-place)
    update_activity(session.activity, event)

    # Thinking indicators
    if event_type == "content_block_start":
        block = event.get("content_block", {})
        block_type = block.get("type", "")
        if block_type == "thinking":
            ctx.in_thinking = True
            yield ThinkingStart()
        elif ctx.in_thinking:
            ctx.in_thinking = False
            yield ThinkingEnd(thinking_text=session.activity.thinking_text or "")

    # Tool use tracking
    if event_type == "content_block_start":
        block = event.get("content_block", {})
        if block.get("type") == "tool_use":
            ctx.tool_input_json = ""
            ctx.current_tool_name = block.get("name", "")
            yield ToolUseStart(
                tool_name=ctx.current_tool_name,
                index=event.get("index", 0),
            )
    elif event_type == "content_block_delta":
        delta = event.get("delta", {})
        if delta.get("type") == "input_json_delta":
            partial = delta.get("partial_json", "")
            ctx.tool_input_json += partial
            yield ToolInputDelta(partial_json=partial)
    elif event_type == "content_block_stop" and session.activity.phase == "waiting":
        tool_name = session.activity.tool_name or ctx.current_tool_name
        if tool_name:
            tool_input = {}
            if ctx.tool_input_json:
                try:
                    tool_input = json.loads(ctx.tool_input_json)
                except (json.JSONDecodeError, TypeError):
                    pass

            # Extract human-readable preview
            preview = _extract_tool_preview(tool_name, ctx.tool_input_json)

            yield ToolUseEnd(
                tool_name=tool_name,
                tool_input=tool_input,
                preview=preview,
            )

            # Special case: TodoWrite
            if tool_name == "TodoWrite" and tool_input:
                todos = tool_input.get("todos", [])
                if todos:
                    yield TodoUpdate(todos=todos)

            ctx.tool_input_json = ""
            ctx.current_tool_name = ""

    # Text deltas
    if ctx.hit_rate_limit:
        return

    if event_type == "content_block_delta":
        delta = event.get("delta", {})
        if delta.get("type") == "text_delta":
            text = delta.get("text", "")
            ctx.text_buffer += text
            yield TextDelta(text=text)
    elif event_type == "message_delta":
        stop_reason = event.get("delta", {}).get("stop_reason")
        if stop_reason == "end_turn" and ctx.text_buffer.strip():
            ctx.flush_count += 1
            yield TextFlush(text=ctx.text_buffer, reason="end_turn")
            ctx.text_buffer = ""
            if ctx.in_thinking:
                ctx.in_thinking = False
                yield ThinkingEnd()


async def _handle_assistant_message(
    ctx: _Ctx,
    session: AgentSession,
    msg: AssistantMessage,
) -> AsyncIterator[StreamOutput]:
    """Transform an AssistantMessage into StreamOutput events."""
    if msg.error in ("rate_limit", "billing_error"):
        error_text = ctx.text_buffer
        for block in msg.content or []:
            if hasattr(block, "text"):
                error_text += " " + cast("str", getattr(block, "text", ""))
        log.warning("Agent '%s' hit %s error: %s", session.name, msg.error, error_text[:200])

        if ctx.in_thinking:
            ctx.in_thinking = False
            yield ThinkingEnd()
        if ctx.text_buffer.strip():
            ctx.flush_count += 1
            yield TextFlush(text=ctx.text_buffer, reason="rate_limit")
        ctx.text_buffer = ""
        ctx.hit_rate_limit = True
        yield RateLimitHit(error_type=msg.error, error_text=error_text.strip())

    elif msg.error:
        error_text = ctx.text_buffer
        for block in msg.content or []:
            if hasattr(block, "text"):
                error_text += " " + cast("str", getattr(block, "text", ""))
        log.warning("Agent '%s' hit API error (%s): %s", session.name, msg.error, error_text[:200])

        if ctx.in_thinking:
            ctx.in_thinking = False
            yield ThinkingEnd()
        if ctx.text_buffer.strip():
            ctx.flush_count += 1
            yield TextFlush(text=ctx.text_buffer, reason="assistant_error")
        ctx.text_buffer = ""
        ctx.hit_transient_error = msg.error

    else:
        # Normal assistant message — extract text from content blocks if buffer empty
        if not ctx.text_buffer.strip():
            for block in msg.content or []:
                if hasattr(block, "text"):
                    ctx.text_buffer += cast("str", getattr(block, "text", ""))
        if ctx.text_buffer.strip():
            ctx.flush_count += 1
            yield TextFlush(text=ctx.text_buffer, reason="assistant_msg")
        ctx.text_buffer = ""
        if ctx.in_thinking:
            ctx.in_thinking = False
            yield ThinkingEnd()


async def _handle_result_message(
    ctx: _Ctx,
    session: AgentSession,
    msg: ResultMessage,
    set_session_id_fn: Any,
    record_usage_fn: Any,
) -> AsyncIterator[StreamOutput]:
    """Transform a ResultMessage into StreamOutput events."""
    ctx.got_result = True

    if ctx.in_thinking:
        ctx.in_thinking = False
        yield ThinkingEnd()

    if not ctx.hit_rate_limit and ctx.text_buffer.strip():
        ctx.flush_count += 1
        yield TextFlush(text=ctx.text_buffer, reason="result_msg")
    ctx.text_buffer = ""

    is_flowchart = msg.session_id == "flowchart"

    if not is_flowchart and set_session_id_fn:
        await set_session_id_fn(session, msg)

    if not is_flowchart and record_usage_fn:
        record_usage_fn(session.name, msg)

    yield QueryResult(
        session_id=msg.session_id,
        cost_usd=msg.total_cost_usd or 0.0,
        num_turns=msg.num_turns or 0,
        duration_ms=msg.duration_ms or 0,
        is_error=bool(msg.is_error),
        is_flowchart=is_flowchart,
    )


_SILENT_BLOCK_TYPES = {"start", "end", "variable"}


async def _handle_system_message(
    ctx: _Ctx,
    session: AgentSession,
    msg: SystemMessage,
    self_compacting_names: set[str],
    compact_start_times: dict[str, float],
    pending_compact: dict[str, dict[str, int | float]],
) -> AsyncIterator[StreamOutput]:
    """Transform a SystemMessage into StreamOutput events."""
    if msg.subtype == "status" and msg.data.get("status") == "compacting":
        # Set compacting flag — prevents interrupts during compaction
        session.compacting = True
        self_triggered = session.name in self_compacting_names
        yield CompactStart(
            token_count=session.context_tokens,
            self_triggered=self_triggered,
        )

    elif msg.subtype == "compact_boundary":
        # Clear compacting flag — compaction is done
        session.compacting = False
        metadata = msg.data.get("compact_metadata", {})
        trigger = metadata.get("trigger", "unknown")
        pre_tokens = metadata.get("pre_tokens", 0)
        start_time = compact_start_times.pop(session.name, None)

        if pre_tokens:
            pending_compact[session.name] = {
                "pre_tokens": pre_tokens,
                "start_time": start_time or time.monotonic(),
            }
        yield CompactComplete(pre_tokens=pre_tokens, trigger=trigger)

    elif msg.subtype == "flowchart_start":
        data = msg.data.get("data", {})
        yield FlowchartStart(
            command=data.get("command", ""),
            block_count=data.get("block_count", 0),
        )

    elif msg.subtype == "flowchart_complete":
        data = msg.data.get("data", {})
        yield FlowchartEnd(
            status=data.get("status", ""),
            duration_ms=data.get("duration_ms", 0),
            cost_usd=data.get("cost_usd", 0.0),
            blocks_executed=data.get("blocks_executed", 0),
        )

    elif msg.subtype == "block_start":
        # Flush any pending text before the block
        if ctx.text_buffer.strip():
            ctx.flush_count += 1
            yield TextFlush(text=ctx.text_buffer, reason="block_start")
            ctx.text_buffer = ""
        data = msg.data.get("data", {})
        block_name = data.get("block_name", "?")
        block_type = data.get("block_type", "?")
        if block_type not in _SILENT_BLOCK_TYPES:
            yield BlockStart(block_name=block_name, block_type=block_type)

    elif msg.subtype == "block_complete":
        if ctx.text_buffer.strip():
            ctx.flush_count += 1
            yield TextFlush(text=ctx.text_buffer, reason="block_complete")
            ctx.text_buffer = ""
        data = msg.data.get("data", {})
        if not data.get("success", True):
            yield BlockComplete(
                block_name=data.get("block_name", "?"),
                success=False,
            )

    else:
        yield SystemNotification(subtype=msg.subtype, data=msg.data)


# ---------------------------------------------------------------------------
# Tool preview extraction (shared utility)
# ---------------------------------------------------------------------------

import re


def _extract_tool_preview(tool_name: str, raw_json: str) -> str | None:
    """Try to extract a useful preview from partial tool input JSON."""
    try:
        data = json.loads(raw_json)
        if tool_name == "Bash":
            return data.get("command", "")[:100]
        elif tool_name in ("Read", "Write", "Edit"):
            return data.get("file_path", "")[:100]
        elif tool_name == "Grep":
            return f'grep "{data.get("pattern", "")}" {data.get("path", ".")}'[:100]
        elif tool_name == "Glob":
            return f"{data.get('pattern', '')}"[:100]
    except (json.JSONDecodeError, TypeError):
        if tool_name == "Bash":
            match = re.search(r'"command"\s*:\s*"([^"]*)', raw_json)
            if match:
                return match.group(1)[:100]
        elif tool_name in ("Read", "Write", "Edit"):
            match = re.search(r'"file_path"\s*:\s*"([^"]*)', raw_json)
            if match:
                return match.group(1)[:100]
    return None
