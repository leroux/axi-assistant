"""Structured logging context using contextvars.

Provides request-scoped context that automatically propagates through async calls.
Every log line can carry: agent_name, channel_id, trigger, trace_id.

Usage::

    from axi.log_context import log_ctx, set_agent_context, set_trigger

    # Set context at the start of a request
    set_agent_context("my-agent", channel_id=123456)
    set_trigger("user_message", message_id=789)

    # All subsequent log calls in this async context automatically include fields
    log.info("Processing request")
    # -> 2026-03-02 12:00:00 INFO [agent=my-agent chan=123456 trigger=user_message:789] Processing request
"""

from __future__ import annotations

import contextvars
import logging
from dataclasses import dataclass
from typing import Any

from opentelemetry import trace


@dataclass
class LogContext:
    """Structured fields attached to the current async context."""

    agent_name: str = ""
    channel_id: int | None = None
    trigger: str = ""  # e.g. "user_message:123", "schedule:daily-check", "reconnect"

    def format_prefix(self) -> str:
        """Build a compact prefix string for log lines."""
        parts: list[str] = []
        if self.agent_name:
            parts.append(f"agent={self.agent_name}")
        if self.channel_id:
            parts.append(f"chan={self.channel_id}")
        if self.trigger:
            parts.append(f"trigger={self.trigger}")

        # Include trace_id if OTel has an active span
        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx and ctx.trace_id:
            tid = format(ctx.trace_id, "032x")
            if tid != "0" * 32:
                parts.append(f"trace={tid[:16]}")

        return " ".join(parts)


# The context variable — one per async task, automatically inherited by child tasks.
_log_ctx: contextvars.ContextVar[LogContext] = contextvars.ContextVar("log_ctx")


def get_log_context() -> LogContext:
    """Get the current log context, or an empty one if none is set."""
    try:
        return _log_ctx.get()
    except LookupError:
        return LogContext()


def set_agent_context(agent_name: str, *, channel_id: int | None = None) -> contextvars.Token[LogContext]:
    """Set the agent context for the current async scope. Returns a token for reset."""
    ctx = LogContext(agent_name=agent_name, channel_id=channel_id)
    return _log_ctx.set(ctx)


def set_trigger(trigger_type: str, **kwargs: Any) -> None:
    """Set the trigger on the current context. Creates context if needed.

    Examples::

        set_trigger("user_message", message_id=123)  # -> "user_message:123"
        set_trigger("schedule", name="daily-check")   # -> "schedule:daily-check"
        set_trigger("reconnect")                       # -> "reconnect"
    """
    try:
        ctx = _log_ctx.get()
    except LookupError:
        ctx = LogContext()
        _log_ctx.set(ctx)

    detail = kwargs.get("message_id") or kwargs.get("name") or kwargs.get("detail")
    ctx.trigger = f"{trigger_type}:{detail}" if detail else trigger_type


def update_channel(channel_id: int) -> None:
    """Update channel_id on the current context."""
    try:
        ctx = _log_ctx.get()
        ctx.channel_id = channel_id
    except LookupError:
        pass


def clear_context() -> None:
    """Clear the log context for the current async scope."""
    try:
        _log_ctx.set(LogContext())
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Logging filter — injects context into every LogRecord
# ---------------------------------------------------------------------------


class StructuredContextFilter(logging.Filter):
    """Logging filter that adds structured fields from the current async context.

    Adds these attributes to every LogRecord:
    - ctx_agent: agent name (str)
    - ctx_channel: channel id (str)
    - ctx_trigger: trigger string (str)
    - ctx_trace: trace id prefix (str)
    - ctx_prefix: formatted prefix string for human-readable logs
    """

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = get_log_context()
        record.ctx_agent = ctx.agent_name  # type: ignore[attr-defined]
        record.ctx_channel = str(ctx.channel_id) if ctx.channel_id else ""  # type: ignore[attr-defined]
        record.ctx_trigger = ctx.trigger  # type: ignore[attr-defined]
        record.ctx_prefix = ctx.format_prefix()  # type: ignore[attr-defined]

        # Trace ID
        span = trace.get_current_span()
        span_ctx = span.get_span_context()
        if span_ctx and span_ctx.trace_id:
            tid = format(span_ctx.trace_id, "032x")
            record.ctx_trace = tid[:16] if tid != "0" * 32 else ""  # type: ignore[attr-defined]
        else:
            record.ctx_trace = ""  # type: ignore[attr-defined]

        return True
