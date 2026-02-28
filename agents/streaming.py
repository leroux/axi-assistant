# pyright: reportPrivateUsage=false
"""Response streaming: read Claude SDK messages and relay to Discord."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import traceback
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import discord
from claude_agent_sdk._errors import MessageParseError
from claude_agent_sdk._internal.message_parser import parse_message
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
)
from discord import TextChannel

import config
from agents.discord_helpers import send_long, send_system, send_to_exceptions, split_message
from agents.lifecycle import _rebuild_session, _set_session_id
from agents.rate_limiting import _handle_rate_limit
from agents.sdk import as_stream, drain_stderr
from agents.state import _next_stream_id
from rate_limits import record_session_usage as _recordsession_usage
from rate_limits import update_rate_limit_quota as _update_rate_limit_quota

if TYPE_CHECKING:
    from axi_types import AgentSession

log = logging.getLogger("axi")


# ---------------------------------------------------------------------------
# Receive wrapper
# ---------------------------------------------------------------------------


async def _receive_response_safe(session: AgentSession):
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
                await send_to_exceptions(
                    f"⚠️ Unknown SDK message type `{msg_type}` from **{session.name}**:\n```json\n{preview}\n```"
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
    activity = session.activity
    activity.last_event = datetime.now(UTC)
    event_type = event.get("type", "")

    if event_type == "content_block_start":
        block = event.get("content_block", {})
        block_type = block.get("type", "")

        if block_type == "tool_use":
            activity.phase = "tool_use"
            activity.tool_name = block.get("name")
            activity.tool_input_preview = ""
        elif block_type == "thinking":
            activity.phase = "thinking"
            activity.tool_name = None
            activity.tool_input_preview = ""
            activity.thinking_text = ""
        elif block_type == "text":
            activity.phase = "writing"
            activity.tool_name = None
            activity.tool_input_preview = ""
            activity.text_chars = 0

    elif event_type == "content_block_delta":
        delta = event.get("delta", {})
        delta_type = delta.get("type", "")

        if delta_type == "thinking_delta":
            activity.phase = "thinking"
            activity.thinking_text += delta.get("thinking", "")
        elif delta_type == "text_delta":
            activity.phase = "writing"
            activity.text_chars += len(delta.get("text", ""))
        elif delta_type == "input_json_delta":
            if len(activity.tool_input_preview) < 200:
                activity.tool_input_preview += delta.get("partial_json", "")
                activity.tool_input_preview = activity.tool_input_preview[:200]

    elif event_type == "content_block_stop":
        if activity.phase == "tool_use":
            activity.phase = "waiting"

    elif event_type == "message_start":
        activity.turn_count += 1

    elif event_type == "message_delta":
        stop_reason = event.get("delta", {}).get("stop_reason")
        if stop_reason == "end_turn":
            activity.phase = "idle"
            activity.tool_name = None
        elif stop_reason == "tool_use":
            activity.phase = "waiting"


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


class _StreamCtx:
    """Mutable state for a single stream_response_to_channel invocation."""

    __slots__ = ("flush_count", "hit_rate_limit", "hit_transient_error", "msg_total", "text_buffer", "typing_stopped")

    def __init__(self) -> None:
        self.text_buffer: str = ""
        self.hit_rate_limit: bool = False
        self.hit_transient_error: str | None = None
        self.typing_stopped: bool = False
        self.flush_count: int = 0
        self.msg_total: int = 0


async def _flush_text(ctx: _StreamCtx, session: AgentSession, channel: TextChannel, reason: str = "?") -> None:
    """Flush accumulated text buffer to Discord."""
    text = ctx.text_buffer
    if not text.strip():
        return
    ctx.flush_count += 1
    log.info("FLUSH[%s] #%d reason=%s len=%d text=%r", session.name, ctx.flush_count, reason, len(text.strip()), text.strip()[:120])
    await send_long(channel, text.lstrip())


def _stop_typing(ctx: _StreamCtx, typing_ctx: Any) -> None:
    """Cancel the typing indicator."""
    if not ctx.typing_stopped and typing_ctx and typing_ctx.task:
        typing_ctx.task.cancel()
        ctx.typing_stopped = True


async def _drain_stderr_to_channel(session: AgentSession, channel: TextChannel) -> None:
    """Send any accumulated stderr to the channel."""
    for stderr_msg in drain_stderr(session):
        stderr_text = stderr_msg.strip()
        if stderr_text:
            for part in split_message(f"```\n{stderr_text}\n```"):
                await channel.send(part)


# ---------------------------------------------------------------------------
# Stream event handlers
# ---------------------------------------------------------------------------


async def _handle_stream_event(
    ctx: _StreamCtx, session: AgentSession, channel: TextChannel, msg: StreamEvent, typing_ctx: Any
) -> None:
    """Handle a StreamEvent during response streaming."""
    event = msg.event
    event_type = event.get("type", "")

    if msg.session_id and msg.session_id != session.session_id:
        await _set_session_id(session, msg.session_id, channel=channel)

    _update_activity(session, event)

    # Debug output
    if session.debug and event_type == "content_block_stop":
        if session.activity.phase == "thinking" and session.activity.thinking_text:
            thinking = session.activity.thinking_text.strip()
            if thinking:
                file = discord.File(io.BytesIO(thinking.encode("utf-8")), filename="thinking.md")
                await channel.send("💭", file=file)
                session.activity.thinking_text = ""
        elif session.activity.phase == "waiting" and session.activity.tool_name:
            tool = session.activity.tool_name
            preview = extract_tool_preview(tool, session.activity.tool_input_preview)
            if preview:
                await channel.send(f"`🔧 {tool}: {preview[:120]}`")
            else:
                await channel.send(f"`🔧 {tool}`")

    # Log stream events
    if session.agent_log:
        _log_stream_event(session, event_type, event)

    if ctx.hit_rate_limit:
        return

    if event_type == "content_block_delta":
        delta = event.get("delta", {})
        if delta.get("type") == "text_delta":
            ctx.text_buffer += delta.get("text", "")
    elif event_type == "message_delta":
        stop_reason = event.get("delta", {}).get("stop_reason")
        if stop_reason == "end_turn":
            await _flush_text(ctx, session, channel, "end_turn")
            ctx.text_buffer = ""
            _stop_typing(ctx, typing_ctx)


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
        _stop_typing(ctx, typing_ctx)
        await _handle_rate_limit(error_text, session, channel)
        ctx.text_buffer = ""
        ctx.hit_rate_limit = True
    elif msg.error:
        error_text = ctx.text_buffer
        for block in msg.content or []:
            if hasattr(block, "text"):
                error_text += " " + cast("str", getattr(block, "text", ""))
        log.warning("Agent '%s' hit API error (%s): %s", session.name, msg.error, error_text[:200])
        _stop_typing(ctx, typing_ctx)
        await _flush_text(ctx, session, channel, "assistant_error")
        ctx.text_buffer = ""
        ctx.hit_transient_error = msg.error
    else:
        await _flush_text(ctx, session, channel, "assistant_msg")
        ctx.text_buffer = ""
        _stop_typing(ctx, typing_ctx)

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
    _stop_typing(ctx, typing_ctx)
    await _set_session_id(session, msg, channel=channel)
    if not ctx.hit_rate_limit:
        await _flush_text(ctx, session, channel, "result_msg")
    ctx.text_buffer = ""
    if session.agent_log:
        session.agent_log.info(
            "RESULT: cost=$%s turns=%d duration=%dms session=%s",
            msg.total_cost_usd,
            msg.num_turns,
            msg.duration_ms,
            msg.session_id,
        )
    _recordsession_usage(session.name, msg)


async def _handle_system_message(session: AgentSession, channel: TextChannel, msg: SystemMessage) -> None:
    """Handle a SystemMessage during response streaming."""
    if session.agent_log:
        session.agent_log.debug("SYSTEM_MSG: subtype=%s data=%s", msg.subtype, json.dumps(msg.data)[:500])
    if msg.subtype == "compact_boundary":
        metadata = msg.data.get("compact_metadata", {})
        trigger = metadata.get("trigger", "unknown")
        pre_tokens = metadata.get("pre_tokens")
        log.info("Agent '%s' context compacted: trigger=%s pre_tokens=%s", session.name, trigger, pre_tokens)
        token_info = f" ({pre_tokens:,} tokens)" if pre_tokens else ""
        await channel.send(f"🔄 Context compacted{token_info}")


# ---------------------------------------------------------------------------
# Main streaming entrypoint
# ---------------------------------------------------------------------------


async def stream_response_to_channel(session: AgentSession, channel: TextChannel, show_awaiting_input: bool = True) -> str | None:
    """Stream Claude's response from an agent session to a Discord channel.

    Returns None on success, or an error string for transient errors (for retry).
    """
    stream_id = _next_stream_id(session.name)
    log.info(
        "STREAM_START[%s] caller=%s",
        stream_id,
        "".join(f.name or "?" for f in traceback.extract_stack(limit=4)[:-1]),
    )

    ctx = _StreamCtx()

    async with channel.typing() as typing_ctx:
        async for msg in _receive_response_safe(session):
            ctx.msg_total += 1
            if session.agent_log:
                session.agent_log.debug(
                    "MSG_SEQ[%s][%d] type=%s buf_len=%d", stream_id, ctx.msg_total, type(msg).__name__, len(ctx.text_buffer)
                )

            await _drain_stderr_to_channel(session, channel)

            if isinstance(msg, StreamEvent):
                await _handle_stream_event(ctx, session, channel, msg, typing_ctx)
            elif isinstance(msg, AssistantMessage):
                await _handle_assistant_message(ctx, session, channel, msg, typing_ctx)
            elif isinstance(msg, ResultMessage):
                await _handle_result_message(ctx, session, channel, msg, typing_ctx)
            elif isinstance(msg, SystemMessage):
                await _handle_system_message(session, channel, msg)
            elif session.agent_log:
                session.agent_log.debug("OTHER_MSG: %s", type(msg).__name__)

            # Mid-turn flush
            if not ctx.hit_rate_limit and len(ctx.text_buffer) >= 1800:
                split_at = ctx.text_buffer.rfind("\n", 0, 1800)
                if split_at == -1:
                    split_at = 1800
                remainder = ctx.text_buffer[split_at:].lstrip("\n")
                ctx.text_buffer = ctx.text_buffer[:split_at]
                await _flush_text(ctx, session, channel, "mid_turn_split")
                ctx.text_buffer = remainder

    # Flush remaining stderr
    await _drain_stderr_to_channel(session, channel)

    if ctx.hit_rate_limit:
        log.info("STREAM_END[%s] result=rate_limit msgs=%d flushes=%d", stream_id, ctx.msg_total, ctx.flush_count)
        return None

    if ctx.hit_transient_error:
        log.info(
            "STREAM_END[%s] result=transient_error(%s) msgs=%d flushes=%d",
            stream_id,
            ctx.hit_transient_error,
            ctx.msg_total,
            ctx.flush_count,
        )
        return ctx.hit_transient_error

    await _flush_text(ctx, session, channel, "post_loop")
    log.info("STREAM_END[%s] result=ok msgs=%d flushes=%d", stream_id, ctx.msg_total, ctx.flush_count)

    if config.SHOW_AWAITING_INPUT:
        mentions = " ".join(f"<@{uid}>" for uid in config.ALLOWED_USER_IDS)
        await send_system(channel, f"Bot has finished responding and is awaiting input. {mentions}")

    return None


# ---------------------------------------------------------------------------
# Retry / timeout
# ---------------------------------------------------------------------------


async def stream_with_retry(session: AgentSession, channel: TextChannel) -> bool:
    """Stream response with retry on transient API errors. Returns True on success."""
    log.info("RETRY_ENTER[%s] starting initial stream", session.name)
    error = await stream_response_to_channel(session, channel)
    if error is None:
        log.info("RETRY_EXIT[%s] first attempt succeeded", session.name)
        return True

    log.warning("RETRY_TRIGGERED[%s] error=%s — will retry", session.name, error)
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
            f"⚠️ API error, retrying in {delay}s... (attempt {attempt}/{config.API_ERROR_MAX_RETRIES})"
        )
        await asyncio.sleep(delay)

        try:
            assert session.client is not None
            await session.client.query(as_stream("Continue from where you left off."))
        except Exception:
            log.exception("Agent '%s' retry query failed", session.name)
            continue

        error = await stream_response_to_channel(session, channel)
        if error is None:
            return True

    log.error(
        "Agent '%s' transient error persisted after %d retries",
        session.name,
        config.API_ERROR_MAX_RETRIES,
    )
    await channel.send(f"❌ API error persisted after {config.API_ERROR_MAX_RETRIES} retries. Try again later.")
    return False


async def handle_query_timeout(session: AgentSession, channel: TextChannel) -> None:
    """Handle a query timeout. Try interrupt first, then kill and resume."""
    from agents.state import bridge_conn

    log.warning("Query timeout for agent '%s', attempting interrupt", session.name)

    try:
        if bridge_conn and bridge_conn.is_alive:
            result = await bridge_conn.send_command("interrupt", name=session.name)
            if not result.ok:
                log.warning("Bridge SIGINT for '%s' failed: %s", session.name, result.error)
        try:
            if session.client is not None:
                await session.client.interrupt()
        except Exception:
            pass
        async with asyncio.timeout(config.INTERRUPT_TIMEOUT):
            async for msg in _receive_response_safe(session):
                if isinstance(msg, ResultMessage):
                    await _set_session_id(session, msg, channel=channel)
                    break
        session.last_activity = datetime.now(UTC)
        await send_system(
            channel,
            f"Agent **{session.name}** timed out and was interrupted. Context preserved.",
        )
        return
    except Exception:
        log.warning("Interrupt failed for agent '%s', killing and resuming session", session.name)

    old_session_id = session.session_id
    new_session = await _rebuild_session(session.name, session_id=old_session_id)

    if old_session_id:
        await send_system(channel, f"Agent **{new_session.name}** timed out and was recovered (sleeping). Context preserved.")
    else:
        await send_system(channel, f"Agent **{new_session.name}** timed out and was reset (sleeping). Context lost.")
