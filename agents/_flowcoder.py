# pyright: reportPrivateUsage=false
"""Flowcoder management: start, stream, and dispatch flowcoder events."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import config
from agents._discord import send_long, send_system
from axi_types import ActivityState, AgentSession, ContentBlock

if TYPE_CHECKING:
    from discord import TextChannel

    from flowcoder import BridgeFlowcoderProcess, FlowcoderProcess

if config.FLOWCODER_ENABLED:
    from flowcoder import BridgeFlowcoderProcess, FlowcoderProcess

log = logging.getLogger("axi")


# ---------------------------------------------------------------------------
# Flowcoder context (replaces scattered locals)
# ---------------------------------------------------------------------------


@dataclass
class _FlowcoderCtx:
    """Mutable state for a single flowcoder streaming session."""

    text_buffer: list[str] = field(default_factory=lambda: list[str]())
    current_block: str = ""
    block_count: int = 0
    start_time: float = field(default_factory=time.monotonic)

    async def flush_buffer(self, channel: TextChannel) -> None:
        if self.text_buffer:
            text = "".join(self.text_buffer)
            self.text_buffer.clear()
            if text.strip():
                await send_long(channel, text)


_SILENT_BLOCK_TYPES = {"start", "end", "variable"}


# ---------------------------------------------------------------------------
# Dispatch table handlers
# ---------------------------------------------------------------------------


async def _handle_block_start(
    ctx: _FlowcoderCtx, session: AgentSession, channel: TextChannel, msg: dict[str, Any]
) -> None:
    await ctx.flush_buffer(channel)
    data = msg.get("data", {})
    ctx.block_count += 1
    block_name = data.get("block_name", "?")
    block_type = data.get("block_type", "?")
    ctx.current_block = block_name
    session.activity = ActivityState(
        phase="tool_use",
        tool_name=f"flowcoder:{block_type}",
        query_started=session.activity.query_started,
    )
    if block_type not in _SILENT_BLOCK_TYPES:
        await channel.send(f"▶ **{block_name}** (`{block_type}`)")


async def _handle_block_complete(
    ctx: _FlowcoderCtx, session: AgentSession, channel: TextChannel, msg: dict[str, Any]
) -> None:
    await ctx.flush_buffer(channel)
    data = msg.get("data", {})
    success = data.get("success", True)
    block_name = data.get("block_name", ctx.current_block)
    if not success:
        await channel.send(f"> {block_name} **FAILED**")


async def _handle_session_message(
    ctx: _FlowcoderCtx, session: AgentSession, channel: TextChannel, msg: dict[str, Any]
) -> None:
    data = msg.get("data", {})
    inner = data.get("message", {})
    inner_type = inner.get("type", "")

    if inner_type == "assistant":
        if ctx.text_buffer:
            await ctx.flush_buffer(channel)
        else:
            message: dict[str, Any] = inner.get("message", {})
            raw_content: Any = message.get("content", [])
            parts: list[str] = []
            if isinstance(raw_content, str):
                parts.append(raw_content)
            elif isinstance(raw_content, list):
                content_blocks = cast("list[ContentBlock]", raw_content)
                parts.extend(
                    str(blk.get("text", ""))
                    for blk in content_blocks
                    if blk.get("type") == "text"
                )
            fallback_text = "\n".join(parts)
            if fallback_text.strip():
                await send_long(channel, fallback_text)

    elif inner_type == "stream_event":
        event = inner.get("event", {})
        event_type = event.get("type", "")
        if event_type == "content_block_delta":
            delta = event.get("delta", {})
            if delta.get("type") == "text_delta":
                ctx.text_buffer.append(delta.get("text", ""))
                buf_len = sum(len(t) for t in ctx.text_buffer)
                if buf_len >= 1800:
                    await ctx.flush_buffer(channel)


async def _handle_fc_result(
    ctx: _FlowcoderCtx, session: AgentSession, channel: TextChannel, msg: dict[str, Any]
) -> bool:
    """Returns True to signal the stream loop should break."""
    await ctx.flush_buffer(channel)
    elapsed = time.monotonic() - ctx.start_time
    is_error = msg.get("is_error", False)
    status = "**failed**" if is_error else "**completed**"
    cost = msg.get("total_cost_usd", 0)
    summary = f"Flowchart {status} in {elapsed:.0f}s | Cost: ${cost:.4f} | Blocks: {ctx.block_count}"
    await send_system(channel, summary)
    return True


async def _handle_fc_status(
    ctx: _FlowcoderCtx, session: AgentSession, channel: TextChannel, msg: dict[str, Any]
) -> bool:
    """Returns True to signal the stream loop should break."""
    return bool(not msg.get("busy", False))


async def _handle_fc_control(
    ctx: _FlowcoderCtx, session: AgentSession, channel: TextChannel, msg: dict[str, Any]
) -> None:
    assert session.flowcoder_process is not None
    request = msg.get("request", msg)
    request_id = request.get("request_id", "")
    response = {
        "type": "control_response",
        "response": {
            "request_id": request_id,
            "allowed": True,
        },
    }
    await session.flowcoder_process.send(response)


# ---------------------------------------------------------------------------
# Stream flowcoder to channel (dispatch-table driven)
# ---------------------------------------------------------------------------


async def _stream_flowcoder_to_channel(session: AgentSession, channel: TextChannel) -> None:
    """Read flowcoder messages and translate to Discord output."""
    proc = session.flowcoder_process
    assert proc is not None

    ctx = _FlowcoderCtx()

    async for msg in proc.messages():
        session.last_activity = datetime.now(UTC)
        msg_type = msg.get("type", "")
        msg_subtype = msg.get("subtype", "")

        if msg_type == "system" and msg_subtype == "block_start":
            await _handle_block_start(ctx, session, channel, msg)
        elif msg_type == "system" and msg_subtype == "block_complete":
            await _handle_block_complete(ctx, session, channel, msg)
        elif msg_type == "system" and msg_subtype == "session_message":
            await _handle_session_message(ctx, session, channel, msg)
        elif msg_type == "result":
            if await _handle_fc_result(ctx, session, channel, msg):
                break
        elif msg_type == "status_response":
            if await _handle_fc_status(ctx, session, channel, msg):
                break
        elif msg_type == "control_request":
            await _handle_fc_control(ctx, session, channel, msg)

    await ctx.flush_buffer(channel)


# ---------------------------------------------------------------------------
# Run helpers
# ---------------------------------------------------------------------------


async def _run_and_stream_flowcoder(
    session: AgentSession, channel: TextChannel, command: str, args: str, label: str
) -> None:
    """Shared: create flowcoder process, stream output, handle cancellation/cleanup."""
    from agents._state import bridge_conn

    if bridge_conn and bridge_conn.is_alive:
        proc = BridgeFlowcoderProcess(
            bridge_name=f"{session.name}:flowcoder",
            conn=bridge_conn,
            command=command,
            args=args,
            cwd=session.cwd,
        )
    else:
        proc = FlowcoderProcess(command=command, args=args, cwd=session.cwd)
    await proc.start()
    session.flowcoder_process = proc

    try:
        await _stream_flowcoder_to_channel(session, channel)
    except asyncio.CancelledError:
        if isinstance(proc, BridgeFlowcoderProcess):
            await proc.detach()
            session.flowcoder_process = None
            session.activity = ActivityState(phase="idle")
            log.info("%s '%s' detached on cancel (bridge will buffer)", label, session.name)
            raise
        raise
    except Exception:
        log.exception("Error streaming %s for '%s'", label, session.name)
        await send_system(channel, f"{label} **{session.name}** encountered a streaming error.")
    finally:
        if session.flowcoder_process is not None:
            await proc.stop()
            session.flowcoder_process = None
            session.activity = ActivityState(phase="idle")


async def _run_flowcoder(session: AgentSession, channel: TextChannel) -> None:  # pyright: ignore[reportUnusedFunction]  # called from _messaging
    """Run a flowcoder agent to completion, streaming output to Discord."""
    log.info("Starting flowcoder agent '%s' (channel=%s)", session.name, channel.id)
    try:
        async with session.query_lock:
            session.last_activity = datetime.now(UTC)
            session.activity = ActivityState(phase="starting", query_started=datetime.now(UTC))

            cmd_display = session.flowcoder_command
            if session.flowcoder_args:
                cmd_display += f" {session.flowcoder_args}"
            await channel.send(f"*System:* Running flowcoder command: `{cmd_display}`")
            await _run_and_stream_flowcoder(
                session, channel, session.flowcoder_command, session.flowcoder_args, "Flowcoder"
            )

        log.info("Flowcoder agent '%s' finished", session.name)

    except asyncio.CancelledError:
        log.debug("Flowcoder agent '%s' task cancelled", session.name)
    except Exception:
        log.exception("Error running flowcoder agent '%s'", session.name)
        await send_system(channel, f"Flowcoder agent **{session.name}** encountered an error.")


async def run_inline_flowchart(session: AgentSession, channel: TextChannel, command: str, args: str) -> None:
    """Run a flowchart command inline in an agent's channel."""
    async with session.query_lock:
        session.last_activity = datetime.now(UTC)
        session.activity = ActivityState(phase="starting", query_started=datetime.now(UTC))

        cmd_display = command + (f" {args}" if args else "")
        await send_system(channel, f"Running flowchart: `{cmd_display}`")
        await _run_and_stream_flowcoder(session, channel, command, args, f"Flowchart `{command}`")

    from agents._messaging import process_message_queue

    await process_message_queue(session)
