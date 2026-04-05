"""Discord streaming engine.

Renders Claude agent responses in Discord: live-editing, thinking indicators,
typing indicators, message splitting, and inline duration timing.

Extracted from agents.py (Phase 0). All behavior is identical.

Dependencies on agents.py are injected via init() to avoid circular imports,
following the same pattern as channels.py.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import time
import traceback
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import discord
import httpx
from claude_agent_sdk._errors import MessageParseError
from claude_agent_sdk._internal.message_parser import parse_message
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
)
from discord import TextChannel
from opentelemetry import context as otel_context
from opentelemetry import trace

from axi import config
from axi.axi_types import ActivityState, AgentSession, discord_state
from axi.rate_limits import (
    record_session_usage as _record_session_usage,
)
from axi.rate_limits import (
    update_rate_limit_quota as _update_rate_limit_quota,
)
from claudewire.events import as_stream, update_activity
from claudewire.session import get_stdio_logger
from discordquery import split_message

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from procmux import ProcmuxConnection

log = logging.getLogger("axi")
_tracer = trace.get_tracer(__name__)

# Explicit exports for re-export from agents.py (suppresses pyright reportPrivateUsage)
__all__ = [
    "_LiveEditState",
    "_StreamCtx",
    "_cancel_typing",
    "_compact_start_times",
    "_handle_system_message",
    "_live_edit_finalize",
    "_live_edit_tick",
    "_pending_compact",
    "_self_compacting",
    "_update_activity",
    "extract_tool_preview",
    "interrupt_session",
    "stream_response_to_channel",
    "stream_with_retry",
]


# ---------------------------------------------------------------------------
# Injected references — set via init(), same pattern as channels.py
# ---------------------------------------------------------------------------

_send_long: Callable[..., Awaitable[discord.Message | None]] | None = None
_send_system: Callable[..., Awaitable[None]] | None = None
_send_to_exceptions: Callable[..., Awaitable[bool]] | None = None
_post_todo_list_fn: Callable[..., Awaitable[None]] | None = None
_set_session_id_fn: Callable[..., Awaitable[None]] | None = None
_handle_rate_limit_fn: Callable[..., Awaitable[None]] | None = None
_sleep_agent_fn: Callable[..., Awaitable[None]] | None = None
_get_procmux_conn: Callable[[], ProcmuxConnection | None] | None = None


def init(
    *,
    send_long_fn: Callable[..., Any],
    send_system_fn: Callable[..., Any],
    send_to_exceptions_fn: Callable[..., Any],
    post_todo_list_fn: Callable[..., Any],
    set_session_id_fn: Callable[..., Any],
    handle_rate_limit_fn: Callable[..., Any],
    sleep_agent_fn: Callable[..., Any],
    get_procmux_conn_fn: Callable[..., Any],
) -> None:
    """Inject function references to avoid circular imports with agents.py."""
    global _send_long, _send_system, _send_to_exceptions, _post_todo_list_fn
    global _set_session_id_fn, _handle_rate_limit_fn, _sleep_agent_fn, _get_procmux_conn
    _send_long = send_long_fn
    _send_system = send_system_fn
    _send_to_exceptions = send_to_exceptions_fn
    _post_todo_list_fn = post_todo_list_fn
    _set_session_id_fn = set_session_id_fn
    _handle_rate_limit_fn = handle_rate_limit_fn
    _sleep_agent_fn = sleep_agent_fn
    _get_procmux_conn = get_procmux_conn_fn


# ---------------------------------------------------------------------------
# Stream ID counter
# ---------------------------------------------------------------------------

_stream_counter = 0


def _next_stream_id(agent_name: str) -> str:
    """Generate a unique stream ID for tracing."""
    global _stream_counter
    _stream_counter += 1
    return f"{agent_name}:S{_stream_counter}"


# ---------------------------------------------------------------------------
# Compaction state (shared with agents.py via import)
# ---------------------------------------------------------------------------

_self_compacting: set[str] = set()  # Agent names currently in Axi-triggered compaction
_compact_start_times: dict[str, float] = {}  # Agent name -> monotonic start time
# Pending compact result — shown at the start of the next query when post_tokens is available
_pending_compact: dict[str, dict[str, int | float]] = {}  # agent name -> {"pre_tokens": int, "start_time": float}


# ---------------------------------------------------------------------------
# Receive response from SDK
# ---------------------------------------------------------------------------


async def _receive_response_safe(session: AgentSession) -> AsyncIterator[Any]:
    """Wrapper around receive_messages() that handles unknown message types."""
    assert session.client is not None
    assert session.client._query is not None  # pyright: ignore[reportPrivateUsage]
    async for data in session.client._query.receive_messages():  # pyright: ignore[reportPrivateUsage]
        try:
            parsed = parse_message(data)
        except MessageParseError:
            msg_type = data.get("type", "?")
            if msg_type == "rate_limit_event":
                log.info("Rate limit event for '%s': %s", session.name, data)
                if session.agent_log:
                    session.agent_log.info("RATE_LIMIT_EVENT: %s", json.dumps(data)[:500])
                _update_rate_limit_quota(data)
            else:
                log.warning(
                    "Unknown SDK message type from '%s': type=%s data=%s",
                    session.name,
                    msg_type,
                    json.dumps(data)[:500],
                )
                if session.agent_log:
                    session.agent_log.warning("UNKNOWN_MSG: type=%s data=%s", msg_type, json.dumps(data)[:500])
                preview = json.dumps(data)[:400]
                assert _send_to_exceptions is not None
                await _send_to_exceptions(
                    f"\u26a0\ufe0f Unknown SDK message type `{msg_type}` from **{session.name}**:\n```json\n{preview}\n```"
                )
            continue
        yield parsed
        if isinstance(parsed, ResultMessage):
            return


# ---------------------------------------------------------------------------
# Activity tracking
# ---------------------------------------------------------------------------


def _update_activity(session: AgentSession, event: dict[str, Any]) -> None:
    """Update the agent's activity state from a raw Anthropic stream event."""
    update_activity(session.activity, event)


def extract_tool_preview(tool_name: str, raw_json: str) -> str | None:
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


# ---------------------------------------------------------------------------
# Stream context + helpers
# ---------------------------------------------------------------------------


class _LiveEditState:
    """Tracks the Discord message currently being live-edited during streaming."""

    __slots__ = (
        "channel_id",
        "content",
        "edit_pending",
        "finalized",
        "last_edit_time",
        "message_id",
    )

    def __init__(self, channel_id: int) -> None:
        self.channel_id: int = channel_id
        self.message_id: str | None = None  # Set after first message is posted
        self.content: str = ""  # Full content of the current message
        self.last_edit_time: float = 0.0  # monotonic timestamp of last edit
        self.finalized: bool = False  # True once this message is complete
        self.edit_pending: bool = False  # True if content changed since last edit


class _StreamCtx:
    """Mutable state for a single stream_response_to_channel invocation."""

    __slots__ = (
        "deferred_msg",
        "flush_count",
        "got_result",
        "hit_rate_limit",
        "hit_transient_error",
        "in_flowchart",
        "last_flushed_channel_id",
        "last_flushed_content",
        "last_flushed_msg_id",
        "live_edit",
        "msg_total",
        "text_buffer",
        "thinking_message",
        "tool_input_json",
        "typing_stopped",
        "suppress_stream",
    )

    def __init__(self, live_edit: _LiveEditState | None = None) -> None:
        self.text_buffer: str = ""
        self.got_result: bool = False  # True once a ResultMessage is received
        self.hit_rate_limit: bool = False
        self.hit_transient_error: str | None = None
        self.typing_stopped: bool = False
        self.flush_count: int = 0
        self.msg_total: int = 0
        self.tool_input_json: str = ""  # Accumulates full tool input JSON for current tool_use block
        self.in_flowchart: bool = False  # True during flowchart execution (protects session_id)
        self.suppress_stream: bool = False  # True when current block has output_schema (JSON is internal)
        self.live_edit: _LiveEditState | None = live_edit  # Set when STREAMING_DISCORD is enabled
        self.thinking_message: discord.Message | None = None  # Temporary "thinking..." indicator
        self.last_flushed_msg_id: str | None = None  # ID of the last Discord message sent/edited
        self.last_flushed_channel_id: int | None = None
        self.last_flushed_content: str = ""  # Content of the last flushed message
        self.deferred_msg: str = ""  # Non-streaming: holds back the last message for timing append


async def _flush_text(ctx: _StreamCtx, session: AgentSession, channel: TextChannel, reason: str = "?") -> None:
    """Flush accumulated text buffer to Discord.

    When live-edit streaming is active, this finalizes the current streaming
    message (ensuring content is up to date) and resets the live-edit state
    so the next text block starts a new message.
    """
    text = ctx.text_buffer
    if not text.strip():
        return
    ctx.flush_count += 1
    log.info(
        "FLUSH[%s] #%d reason=%s len=%d text=%r",
        session.name,
        ctx.flush_count,
        reason,
        len(text.strip()),
        text.strip()[:120],
    )

    le = ctx.live_edit
    if le is not None:
        # Streaming mode: finalize the current live-edit message(s)
        await _live_edit_finalize(ctx, session)
    else:
        # Non-streaming: send any previously deferred message, then defer the current one.
        # This holds back the last message so timing can be appended before sending.
        assert _send_long is not None
        if ctx.deferred_msg:
            last_msg = await _send_long(channel, ctx.deferred_msg)
            if last_msg is not None:
                ctx.last_flushed_msg_id = str(last_msg.id)
                ctx.last_flushed_channel_id = channel.id
                ctx.last_flushed_content = last_msg.content
        ctx.deferred_msg = text.lstrip()


# ---------------------------------------------------------------------------
# Live-edit streaming helpers (STREAMING_DISCORD)
# ---------------------------------------------------------------------------

_STREAMING_CURSOR = "\u2588"  # Block cursor to indicate "still typing"
_STREAMING_MSG_LIMIT = 1900  # Leave room for cursor and splitting overhead


async def _live_edit_post(le: _LiveEditState, content: str, session: AgentSession) -> None:
    """Post a new message via REST and record its ID in the live-edit state."""
    try:
        resp = await config.discord_client.send_message(le.channel_id, content)
        le.message_id = resp["id"]
        le.content = content
        le.last_edit_time = time.monotonic()
        le.edit_pending = False
        log.debug("LIVE_EDIT_POST[%s] msg_id=%s len=%d", session.name, le.message_id, len(content))
    except Exception:
        log.exception("LIVE_EDIT_POST[%s] failed to post initial message", session.name)
        raise


async def _live_edit_update(le: _LiveEditState, content: str, session: AgentSession) -> None:
    """Edit the current live-edit message with new content."""
    if le.message_id is None:
        return
    try:
        await config.discord_client.edit_message(le.channel_id, le.message_id, content)
        le.content = content
        le.last_edit_time = time.monotonic()
        le.edit_pending = False
        log.debug("LIVE_EDIT_UPDATE[%s] msg_id=%s len=%d", session.name, le.message_id, len(content))
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            retry_after = exc.response.json().get("retry_after", 2.0)
            log.warning("LIVE_EDIT_UPDATE[%s] rate limited, backing off %.1fs", session.name, retry_after)
            le.last_edit_time = time.monotonic() + retry_after
            le.edit_pending = True
        else:
            log.warning("LIVE_EDIT_UPDATE[%s] edit failed: %s", session.name, exc)
    except Exception:
        log.warning("LIVE_EDIT_UPDATE[%s] edit failed", session.name, exc_info=True)


async def _live_edit_tick(ctx: _StreamCtx, session: AgentSession) -> None:
    """Called on each text_delta. Posts or edits the message if enough time has passed.

    Flow:
    1. If no message exists yet, post the first chunk immediately.
    2. If content exceeds the limit, finalize current message and start a new one.
    3. If enough time has passed since last edit, edit with current content + cursor.
    """
    le = ctx.live_edit
    if le is None or le.finalized:
        return

    text = ctx.text_buffer.lstrip()
    if not text:
        return

    now = time.monotonic()

    # First message: post immediately
    if le.message_id is None:
        await _live_edit_post(le, text + _STREAMING_CURSOR, session)
        return

    # Content exceeds limit: finalize current message at a good split point, start new
    if len(text) > _STREAMING_MSG_LIMIT:
        split_at = text.rfind("\n", 0, _STREAMING_MSG_LIMIT)
        if split_at == -1:
            split_at = _STREAMING_MSG_LIMIT
        # Finalize the current message with content up to split point (no cursor)
        final_content = text[:split_at]
        await _live_edit_update(le, final_content, session)
        # Reset buffer to remainder and start a new message
        ctx.text_buffer = text[split_at:].lstrip("\n")
        le.message_id = None
        le.content = ""
        le.edit_pending = False
        # Post the remainder as a new message if there's content
        remainder = ctx.text_buffer.lstrip()
        if remainder:
            await _live_edit_post(le, remainder + _STREAMING_CURSOR, session)
        return

    # Throttled edit: only update if enough time has passed
    if now - le.last_edit_time >= config.STREAMING_EDIT_INTERVAL:
        await _live_edit_update(le, text + _STREAMING_CURSOR, session)


async def _live_edit_finalize(ctx: _StreamCtx, session: AgentSession) -> None:
    """Finalize the current live-edit message: remove cursor, post any remaining content."""
    le = ctx.live_edit
    if le is None:
        return

    text = ctx.text_buffer.lstrip()

    if le.message_id is not None and text:
        # Final edit: remove the cursor character
        # If text is too long, we need to split
        chunks = split_message(text)
        if len(chunks) == 1:
            await _live_edit_update(le, chunks[0], session)
            ctx.last_flushed_msg_id = le.message_id
            ctx.last_flushed_channel_id = le.channel_id
            ctx.last_flushed_content = chunks[0]
        else:
            # First chunk goes into the existing message
            await _live_edit_update(le, chunks[0], session)
            # Remaining chunks are new messages
            for chunk in chunks[1:]:
                resp = await config.discord_client.send_message(le.channel_id, chunk)
                ctx.last_flushed_msg_id = resp["id"]
                ctx.last_flushed_channel_id = le.channel_id
                ctx.last_flushed_content = chunk
    elif le.message_id is None and text:
        # Never posted — just send normally
        for chunk in split_message(text):
            resp = await config.discord_client.send_message(le.channel_id, chunk)
            ctx.last_flushed_msg_id = resp["id"]
            ctx.last_flushed_channel_id = le.channel_id
            ctx.last_flushed_content = chunk

    # Reset for next text block
    le.message_id = None
    le.content = ""
    le.edit_pending = False
    le.finalized = False


async def _show_thinking(ctx: _StreamCtx, channel: TextChannel) -> None:
    """Send a temporary 'thinking...' indicator message."""
    if ctx.thinking_message is None:
        try:
            ctx.thinking_message = await channel.send("*thinking...*")
        except Exception:
            log.debug("Failed to send thinking indicator", exc_info=True)


async def _hide_thinking(ctx: _StreamCtx) -> None:
    """Delete the temporary thinking indicator message."""
    msg = ctx.thinking_message
    if msg is not None:
        ctx.thinking_message = None
        try:
            await msg.delete()
        except Exception:
            log.debug("Failed to delete thinking indicator", exc_info=True)


def _stop_typing(ctx: _StreamCtx, typing_ctx: Any) -> None:
    """Cancel the typing indicator."""
    if not ctx.typing_stopped and typing_ctx and typing_ctx.task:
        typing_ctx.task.cancel()
        ctx.typing_stopped = True


def _cancel_typing(ds: Any) -> None:
    """Cancel the typing indicator stored on DiscordAgentState.

    Used by permission callbacks (plan approval, questions) to stop
    the typing indicator while waiting for user input.
    """
    typing_obj = getattr(ds, 'typing_obj', None)
    if typing_obj and hasattr(typing_obj, 'task'):
        typing_obj.task.cancel()


# ---------------------------------------------------------------------------
# Stream event handlers
# ---------------------------------------------------------------------------


async def _handle_stream_event(
    ctx: _StreamCtx, session: AgentSession, channel: TextChannel, msg: StreamEvent, typing_ctx: Any
) -> None:
    """Handle a StreamEvent during response streaming."""
    event = msg.event
    event_type = event.get("type", "")

    if not ctx.in_flowchart and msg.session_id and msg.session_id != session.session_id:
        assert _set_session_id_fn is not None
        await _set_session_id_fn(session, msg.session_id, channel=channel)

    _update_activity(session, event)

    # Thinking indicator — show/hide based on phase transitions
    if event_type == "content_block_start":
        block = event.get("content_block", {})
        block_type = block.get("type", "")
        if block_type == "thinking":
            await _show_thinking(ctx, channel)
        elif ctx.thinking_message is not None:
            # New non-thinking block started — hide the indicator
            await _hide_thinking(ctx)

    # Track tool input JSON for TodoWrite display
    if event_type == "content_block_start":
        block = event.get("content_block", {})
        if block.get("type") == "tool_use":
            ctx.tool_input_json = ""
    elif event_type == "content_block_delta":
        delta = event.get("delta", {})
        if delta.get("type") == "input_json_delta":
            ctx.tool_input_json += delta.get("partial_json", "")

    # TodoWrite display — post/update todo list in Discord
    if event_type == "content_block_stop" and session.activity.phase == "waiting":
        if session.activity.tool_name == "TodoWrite":
            try:
                tool_input: dict[str, Any] = json.loads(ctx.tool_input_json) if ctx.tool_input_json else {}
                assert _post_todo_list_fn is not None
                await _post_todo_list_fn(session, tool_input)
            except Exception:
                log.exception("Failed to parse/post TodoWrite for '%s'", session.name)
        ctx.tool_input_json = ""

    # Debug output
    if discord_state(session).debug and event_type == "content_block_stop":
        if session.activity.phase == "thinking" and session.activity.thinking_text:
            thinking = session.activity.thinking_text.strip()
            if thinking:
                file = discord.File(io.BytesIO(thinking.encode("utf-8")), filename="thinking.md")
                await channel.send("\U0001f4ad", file=file)
                session.activity.thinking_text = ""
        elif session.activity.phase == "waiting" and session.activity.tool_name:
            tool = session.activity.tool_name
            preview = extract_tool_preview(tool, session.activity.tool_input_preview)
            if preview:
                await channel.send(f"`\U0001f527 {tool}: {preview[:120]}`")
            else:
                await channel.send(f"`\U0001f527 {tool}`")

    # Log stream events
    if session.agent_log:
        _log_stream_event(session, event_type, event)

    # Raw stdio log
    get_stdio_logger(session.name, config.LOG_DIR).debug("<<< STDOUT %s", json.dumps(event))

    if ctx.hit_rate_limit:
        return

    if event_type == "content_block_delta":
        delta = event.get("delta", {})
        if delta.get("type") == "text_delta":
            ctx.text_buffer += delta.get("text", "")
            if ctx.live_edit is not None:
                await _live_edit_tick(ctx, session)
    elif event_type == "message_delta":
        stop_reason = event.get("delta", {}).get("stop_reason")
        if stop_reason == "end_turn":
            await _flush_text(ctx, session, channel, "end_turn")
            ctx.text_buffer = ""
            await _hide_thinking(ctx)


def _log_stream_event(session: AgentSession, event_type: str, event: dict[str, Any]) -> None:
    """Log a stream event to the agent's log."""
    assert session.agent_log is not None
    if event_type == "content_block_delta":
        delta = event.get("delta", {})
        delta_type = delta.get("type", "")
        if delta_type not in ("text_delta", "thinking_delta", "signature_delta"):
            session.agent_log.debug("STREAM: %s delta=%s", event_type, delta_type)
    elif event_type in ("content_block_start", "content_block_stop"):
        block = event.get("content_block", {})
        session.agent_log.debug("STREAM: %s type=%s index=%s", event_type, block.get("type", "?"), event.get("index"))
    elif event_type == "message_start":
        msg_data = event.get("message", {})
        session.agent_log.debug("STREAM: message_start model=%s", msg_data.get("model", "?"))
    elif event_type == "message_delta":
        delta = event.get("delta", {})
        session.agent_log.debug("STREAM: message_delta stop_reason=%s", delta.get("stop_reason"))
    elif event_type == "message_stop":
        session.agent_log.debug("STREAM: message_stop")
    else:
        session.agent_log.debug("STREAM: %s %s", event_type, json.dumps(event)[:300])


async def _handle_assistant_message(
    ctx: _StreamCtx, session: AgentSession, channel: TextChannel, msg: AssistantMessage, typing_ctx: Any
) -> None:
    """Handle an AssistantMessage during response streaming."""
    if msg.error in ("rate_limit", "billing_error"):
        error_text = ctx.text_buffer
        for block in msg.content or []:
            if hasattr(block, "text"):
                error_text += " " + cast("str", getattr(block, "text", ""))
        log.warning("Agent '%s' hit %s error: %s", session.name, msg.error, error_text[:200])
        await _hide_thinking(ctx)
        _stop_typing(ctx, typing_ctx)
        assert _handle_rate_limit_fn is not None
        await _handle_rate_limit_fn(error_text, session, channel)
        ctx.text_buffer = ""
        ctx.hit_rate_limit = True
    elif msg.error:
        error_text = ctx.text_buffer
        for block in msg.content or []:
            if hasattr(block, "text"):
                error_text += " " + cast("str", getattr(block, "text", ""))
        log.warning("Agent '%s' hit API error (%s): %s", session.name, msg.error, error_text[:200])
        await _hide_thinking(ctx)
        _stop_typing(ctx, typing_ctx)
        await _flush_text(ctx, session, channel, "assistant_error")
        ctx.text_buffer = ""
        ctx.hit_transient_error = msg.error
    else:
        # When text arrives in full AssistantMessages (flowcoder engine path)
        # rather than via StreamEvent deltas (Claude Code path), the buffer
        # will be empty.  Extract text from content blocks in that case.
        if not ctx.text_buffer.strip():
            for block in msg.content or []:
                if hasattr(block, "text"):
                    ctx.text_buffer += cast("str", getattr(block, "text", ""))
        await _flush_text(ctx, session, channel, "assistant_msg")
        ctx.text_buffer = ""
        await _hide_thinking(ctx)

    if session.agent_log:
        for block in msg.content or []:
            block_any: Any = block
            if hasattr(block, "text"):
                session.agent_log.info("ASSISTANT: %s", block_any.text[:2000])
            elif hasattr(block, "type") and block_any.type == "tool_use":
                session.agent_log.info(
                    "TOOL_USE: %s(%s)",
                    block_any.name,
                    json.dumps(block_any.input)[:500] if hasattr(block, "input") else "",
                )


async def _handle_result_message(
    ctx: _StreamCtx, session: AgentSession, channel: TextChannel, msg: ResultMessage, typing_ctx: Any
) -> None:
    """Handle a ResultMessage during response streaming."""
    ctx.got_result = True
    await _hide_thinking(ctx)
    _stop_typing(ctx, typing_ctx)
    if not ctx.hit_rate_limit:
        await _flush_text(ctx, session, channel, "result_msg")
    ctx.text_buffer = ""

    # Flowchart results use session_id="flowchart" — don't update agent session or record usage
    if msg.session_id == "flowchart":
        if session.agent_log:
            session.agent_log.info(
                "FLOWCHART_RESULT: cost=$%s turns=%d duration=%dms error=%s",
                msg.total_cost_usd,
                msg.num_turns,
                msg.duration_ms,
                msg.is_error,
            )
        return

    assert _set_session_id_fn is not None
    await _set_session_id_fn(session, msg, channel=channel)
    if session.agent_log:
        session.agent_log.info(
            "RESULT: cost=$%s turns=%d duration=%dms session=%s",
            msg.total_cost_usd,
            msg.num_turns,
            msg.duration_ms,
            msg.session_id,
        )
    _record_session_usage(session.name, msg)


_SILENT_BLOCK_TYPES = {"start", "end", "variable"}

# Flowchart commands that suppress block-entry/completion messages in Discord.
# Prevents soul/soul-flow block transitions from spamming the channel.
_fc_quiet_str = os.environ.get("FC_QUIET_COMMANDS", "soul,soul-flow")
_FC_QUIET_COMMANDS: set[str] = {c.strip() for c in _fc_quiet_str.split(",") if c.strip()}


async def _handle_system_message(
    session: AgentSession, channel: TextChannel, msg: SystemMessage, ctx: _StreamCtx | None = None
) -> None:
    """Handle a SystemMessage during response streaming."""
    if session.agent_log:
        session.agent_log.debug("SYSTEM_MSG: subtype=%s data=%s", msg.subtype, json.dumps(msg.data)[:500])
    if msg.subtype == "status" and msg.data.get("status") == "compacting":
        # Set compacting flag — prevents interrupts during compaction
        session.compacting = True
        # CLI signals compaction is starting — only notify if we didn't trigger it ourselves
        if session.name not in _self_compacting:
            token_info = f" ({session.context_tokens:,} tokens)" if session.context_tokens else ""
            log.info("Agent '%s' compaction started (CLI-triggered)%s", session.name, token_info)
            await channel.send(f"\U0001f504 Compacting{token_info}...")

    elif msg.subtype == "compact_boundary":
        # Clear compacting flag — compaction is done
        session.compacting = False
        metadata = msg.data.get("compact_metadata", {})
        trigger = metadata.get("trigger", "unknown")
        pre_tokens = metadata.get("pre_tokens")
        start_time = _compact_start_times.pop(session.name, None)
        log.info(
            "Agent '%s' context compacted: trigger=%s pre_tokens=%s",
            session.name, trigger, pre_tokens,
        )
        # Defer the completion message to the next query, when post_tokens
        # will be available from the autocompact stderr line.
        if pre_tokens:
            _pending_compact[session.name] = {
                "pre_tokens": pre_tokens,
                "start_time": start_time or time.monotonic(),
            }
        else:
            await channel.send("\U0001f504 Context compacted")

    # Flowchart events (emitted by flowcoder-engine during takeover mode)
    elif msg.subtype == "block_start":
        if ctx:
            await _flush_text(ctx, session, channel, "block_start")
            ctx.text_buffer = ""
            ctx.suppress_stream = (
                bool(msg.data.get("data", {}).get("has_output_schema"))
                and not os.environ.get("FC_SHOW_OUTPUT_SCHEMA", "").lower() in ("1", "true", "yes")
            )
        data = msg.data.get("data", {})
        block_name = data.get("block_name", "?")
        block_type = data.get("block_type", "?")
        session.activity = ActivityState(
            phase="tool_use",
            tool_name=f"flowcoder:{block_type}",
            query_started=session.activity.query_started,
        )
        ds = discord_state(session)
        if block_type not in _SILENT_BLOCK_TYPES and (ds.debug or ds.fc_current_command not in _FC_QUIET_COMMANDS):
            await channel.send(f"\u25b6 **{block_name}** (`{block_type}`)")

    elif msg.subtype == "block_complete":
        if ctx:
            # Only flush text if block doesn't have output_schema.
            # Blocks with output_schema produce JSON for internal branching, not for users.
            if not ctx.suppress_stream:
                await _flush_text(ctx, session, channel, "block_complete")
            ctx.text_buffer = ""
            ctx.suppress_stream = False
        data = msg.data.get("data", {})
        ds = discord_state(session)
        if not data.get("success", True) and (ds.debug or ds.fc_current_command not in _FC_QUIET_COMMANDS):
            block_name = data.get("block_name", "?")
            await channel.send(f"> {block_name} **FAILED**")

    elif msg.subtype == "flowchart_start":
        data = msg.data.get("data", {})
        ds = discord_state(session)
        ds.fc_current_command = data.get("command")
        log.info(
            "Flowchart started for '%s': command=%s blocks=%s",
            session.name,
            data.get("command"),
            data.get("block_count"),
        )

    elif msg.subtype == "flowchart_complete":
        data = msg.data.get("data", {})
        ds = discord_state(session)
        ds.fc_current_command = None
        duration_s = data.get("duration_ms", 0) / 1000
        cost = data.get("cost_usd", 0)
        blocks = data.get("blocks_executed", 0)
        status = "**completed**" if data.get("status") == "completed" else "**failed**"
        assert _send_system is not None
        await _send_system(channel, f"Flowchart {status} in {duration_s:.0f}s | Cost: ${cost:.4f} | Blocks: {blocks}")


# ---------------------------------------------------------------------------
# Main streaming entrypoint
# ---------------------------------------------------------------------------


async def stream_response_to_channel(session: AgentSession, channel: TextChannel) -> str | None:
    """Stream Claude's response from an agent session to a Discord channel.

    Returns None on success, or an error string for transient errors (for retry).
    """
    stream_id = _next_stream_id(session.name)
    if log.isEnabledFor(logging.INFO):
        caller = "".join(f.name or "?" for f in traceback.extract_stack(limit=4)[:-1])
        log.info("STREAM_START[%s] caller=%s", stream_id, caller)

    span = _tracer.start_span("stream_to_discord", attributes={"agent.name": session.name, "discord.channel": str(channel.id)})
    ctx_token = otel_context.attach(trace.set_span_in_context(span))
    t0 = time.monotonic()
    t_first_event: float | None = None

    live_edit = _LiveEditState(channel.id) if config.STREAMING_DISCORD else None
    ctx = _StreamCtx(live_edit=live_edit)

    async with channel.typing() as typing_ctx:
        async for msg in _receive_response_safe(session):
            if t_first_event is None:
                t_first_event = time.monotonic()
            ctx.msg_total += 1
            if session.agent_log:
                session.agent_log.debug(
                    "MSG_SEQ[%s][%d] type=%s buf_len=%d",
                    stream_id,
                    ctx.msg_total,
                    type(msg).__name__,
                    len(ctx.text_buffer),
                )

            if isinstance(msg, StreamEvent):
                await _handle_stream_event(ctx, session, channel, msg, typing_ctx)
            elif isinstance(msg, AssistantMessage):
                await _handle_assistant_message(ctx, session, channel, msg, typing_ctx)
            elif isinstance(msg, ResultMessage):
                await _handle_result_message(ctx, session, channel, msg, typing_ctx)
            elif isinstance(msg, SystemMessage):
                if msg.subtype == "flowchart_start":
                    ctx.in_flowchart = True
                elif msg.subtype == "flowchart_complete":
                    ctx.in_flowchart = False
                await _handle_system_message(session, channel, msg, ctx)
            elif session.agent_log:
                session.agent_log.debug("OTHER_MSG: %s", type(msg).__name__)

            # Mid-turn flush (skipped in streaming mode — live-edit handles splitting)
            if not ctx.hit_rate_limit and ctx.live_edit is None and not ctx.suppress_stream and len(ctx.text_buffer) >= 1800:
                split_at = ctx.text_buffer.rfind("\n", 0, 1800)
                if split_at == -1:
                    split_at = 1800
                remainder = ctx.text_buffer[split_at:].lstrip("\n")
                ctx.text_buffer = ctx.text_buffer[:split_at]
                await _flush_text(ctx, session, channel, "mid_turn_split")
                ctx.text_buffer = remainder

    if ctx.hit_rate_limit:
        # Finalize any in-flight streaming message (remove cursor)
        if ctx.live_edit is not None and ctx.live_edit.message_id is not None:
            await _live_edit_finalize(ctx, session)
        log.info("STREAM_END[%s] result=rate_limit msgs=%d flushes=%d", stream_id, ctx.msg_total, ctx.flush_count)
        span.set_attributes({"stream.msg_total": ctx.msg_total, "stream.flush_count": ctx.flush_count})
        otel_context.detach(ctx_token)
        span.end()
        return None

    if ctx.hit_transient_error:
        # Send any deferred message before exiting
        if ctx.deferred_msg:
            assert _send_long is not None
            await _send_long(channel, ctx.deferred_msg)
            ctx.deferred_msg = ""
        log.info(
            "STREAM_END[%s] result=transient_error(%s) msgs=%d flushes=%d",
            stream_id,
            ctx.hit_transient_error,
            ctx.msg_total,
            ctx.flush_count,
        )
        span.set_attributes({"stream.msg_total": ctx.msg_total, "stream.flush_count": ctx.flush_count})
        otel_context.detach(ctx_token)
        span.end()
        return ctx.hit_transient_error

    if not ctx.got_result:
        # Stream ended without a ResultMessage — the CLI process was killed
        # (e.g. by /stop) or crashed.  Sleep the agent so the next message
        # triggers a fresh wake with a new CLI process.
        if ctx.deferred_msg:
            assert _send_long is not None
            await _send_long(channel, ctx.deferred_msg)
            ctx.deferred_msg = ""
        if ctx.live_edit is not None and ctx.live_edit.message_id is not None:
            await _live_edit_finalize(ctx, session)
        await _flush_text(ctx, session, channel, "post_kill")
        log.info("STREAM_END[%s] result=killed msgs=%d flushes=%d", stream_id, ctx.msg_total, ctx.flush_count)
        span.set_attributes({"stream.msg_total": ctx.msg_total, "stream.flush_count": ctx.flush_count})
        span.end()
        assert _sleep_agent_fn is not None
        await _sleep_agent_fn(session, force=True)
        return None

    # Append response timing inline to the last message
    assert _send_long is not None
    if session.activity.query_started and (ctx.flush_count > 0 or ctx.text_buffer.strip()):
        elapsed = (datetime.now(UTC) - session.activity.query_started).total_seconds()
        from axi.agents import get_active_trace_tag
        _trace_tag = get_active_trace_tag(session.name)
        timing_suffix = f"\n-# {elapsed:.1f}s{_trace_tag}"

        if ctx.deferred_msg:
            # Non-streaming: deferred message waiting — append timing and send (no edit needed)
            await _send_long(channel, ctx.deferred_msg + timing_suffix)
            ctx.deferred_msg = ""
        elif ctx.text_buffer.strip():
            # Buffer has content — append inline and flush normally
            ctx.text_buffer += timing_suffix
            await _flush_text(ctx, session, channel, "post_loop")
        elif ctx.last_flushed_msg_id is not None and ctx.last_flushed_channel_id is not None:
            # Streaming: buffer empty, message already sent — edit to append timing
            new_content = ctx.last_flushed_content + timing_suffix
            try:
                await config.discord_client.edit_message(
                    ctx.last_flushed_channel_id, ctx.last_flushed_msg_id, new_content
                )
            except Exception:
                log.warning("Failed to edit last message to append timing", exc_info=True)
                await channel.send(f"-# {elapsed:.1f}s{_trace_tag}")
    else:
        # No timing — send any deferred message as-is
        if ctx.deferred_msg:
            await _send_long(channel, ctx.deferred_msg)
            ctx.deferred_msg = ""
        await _flush_text(ctx, session, channel, "post_loop")
    log.info("STREAM_END[%s] result=ok msgs=%d flushes=%d", stream_id, ctx.msg_total, ctx.flush_count)

    mentions = " ".join(f"<@{uid}>" for uid in config.ALLOWED_USER_IDS)
    await channel.send(mentions)

    ttfe_ms = (t_first_event - t0) * 1000 if t_first_event is not None else -1
    span.set_attributes({
        "stream.msg_total": ctx.msg_total,
        "stream.flush_count": ctx.flush_count,
        "stream.time_to_first_event_ms": ttfe_ms,
    })
    otel_context.detach(ctx_token)
    span.end()
    return None


# ---------------------------------------------------------------------------
# Interrupt
# ---------------------------------------------------------------------------


async def interrupt_session(session: AgentSession) -> None:
    """Interrupt the current turn for an agent session.

    For bridge-managed agents (flowcoder): calls transport.stop() which
    immediately terminates the streaming loop by injecting an ExitEvent,
    then kills the process in the background.  Returns instantly.

    For procmux-managed agents: sends "interrupt" (SIGINT) so the CLI
    stays alive with conversation context preserved.

    For direct-subprocess agents (claude_code): uses the SDK interrupt with
    a short timeout.
    """
    # Flowcoder agents: use transport.stop() for instant termination
    if session.transport is not None:
        await session.transport.stop()
        return

    # Fallback: try procmux interrupt directly (e.g. transport lost but procmux alive)
    assert _get_procmux_conn is not None
    procmux_conn = _get_procmux_conn()
    if procmux_conn and procmux_conn.is_alive:
        result = await procmux_conn.send_command("interrupt", name=session.name)
        if not result.ok:
            log.warning("Bridge interrupt for '%s' failed: %s", session.name, result.error)
        else:
            return

    # Non-bridge agents (claude_code): SDK interrupt
    if session.client is not None:
        try:
            async with asyncio.timeout(5):
                await session.client.interrupt()
        except (TimeoutError, Exception):
            pass


# ---------------------------------------------------------------------------
# Retry / timeout
# ---------------------------------------------------------------------------


async def stream_with_retry(session: AgentSession, channel: TextChannel) -> bool:
    """Stream response with retry on transient API errors. Returns True on success."""
    with _tracer.start_as_current_span("stream_with_retry", attributes={"agent.name": session.name}) as span:
        log.info("RETRY_ENTER[%s] starting initial stream", session.name)
        error = await stream_response_to_channel(session, channel)
        if error is None:
            log.info("RETRY_EXIT[%s] first attempt succeeded", session.name)
            span.set_attribute("retry.attempts", 1)
            return True

        log.warning("RETRY_TRIGGERED[%s] error=%s \u2014 will retry", session.name, error)
        for attempt in range(2, config.API_ERROR_MAX_RETRIES + 1):
            delay = config.API_ERROR_BASE_DELAY * (2 ** (attempt - 2))
            log.warning(
                "Agent '%s' transient error '%s', retrying in %ds (attempt %d/%d)",
                session.name,
                error,
                delay,
                attempt,
                config.API_ERROR_MAX_RETRIES,
            )
            await channel.send(
                f"\u26a0\ufe0f API error, retrying in {delay}s... (attempt {attempt}/{config.API_ERROR_MAX_RETRIES})"
            )
            await asyncio.sleep(delay)

            try:
                assert session.client is not None
                get_stdio_logger(session.name, config.LOG_DIR).debug(
                    ">>> STDIN  %s", json.dumps({"type": "retry", "content": "Continue from where you left off."})
                )
                await session.client.query(as_stream("Continue from where you left off."))
            except Exception:
                log.exception("Agent '%s' retry query failed", session.name)
                continue

            error = await stream_response_to_channel(session, channel)
            if error is None:
                span.set_attribute("retry.attempts", attempt)
                return True

        log.error(
            "Agent '%s' transient error persisted after %d retries",
            session.name,
            config.API_ERROR_MAX_RETRIES,
        )
        await channel.send(f"\u274c API error persisted after {config.API_ERROR_MAX_RETRIES} retries. Try again later.")
        span.set_attribute("retry.exhausted", True)
        span.set_status(trace.StatusCode.ERROR, "retries exhausted")
        return False
