"""Pure data types for the Axi bot.

AgentSession comes from agenthub (hub-facing fields only). Discord-specific
fields live in DiscordAgentState, stored as session.frontend_state.
"""

from __future__ import annotations

__all__ = [
    "TOOL_DISPLAY_NAMES",
    "ActivityState",
    "AgentSession",
    "ConcurrencyLimitError",
    "ContentBlock",
    "DiscordAgentState",
    "McpArgs",
    "McpResult",
    "MessageContent",
    "PlanApprovalResult",
    "RateLimitQuota",
    "SessionUsage",
    "discord_state",
    "setup_agent_log",
    "tool_display",
]

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from typing import TYPE_CHECKING, Any, TypedDict

from agenthub.types import (
    AgentSession,
    ConcurrencyLimitError,
    MessageContent,
    RateLimitQuota,
    SessionUsage,
)
from axi import config
from claudewire.events import TOOL_DISPLAY_NAMES, ActivityState, tool_display

if TYPE_CHECKING:
    import asyncio
    from datetime import datetime

# Anthropic API content block (text, image, tool_use, etc.)
ContentBlock = dict[str, Any]

# MCP tool handler: receives JSON args, returns MCP response
McpArgs = dict[str, Any]
McpResult = dict[str, Any]


class PlanApprovalResult(TypedDict):
    """Result from the plan approval gate in on_message."""

    approved: bool
    message: str  # empty string when no feedback


# ---------------------------------------------------------------------------
# Discord-specific session state
# ---------------------------------------------------------------------------


@dataclass
class DiscordAgentState:
    """Fields specific to the Discord frontend.

    Stored as session.frontend_state on each AgentSession. The hub never
    touches these — only Discord-side code reads/writes them.
    """

    channel_id: int | None = None
    # Stderr buffering (thread-safe — SDK stderr callback runs in a thread)
    stderr_buffer: list[str] = field(default_factory=lambda: list[str]())
    stderr_lock: threading.Lock = field(default_factory=threading.Lock)
    # Prompt change tracking
    system_prompt_posted: bool = False
    # Idle notification state
    last_idle_notified: datetime | None = None
    # Debug mode (post tool calls and thinking to Discord)
    debug: bool = field(
        default_factory=lambda: os.environ.get("DISCORD_DEBUG", "").strip().lower()
        in ("1", "true", "on")
    )
    # Plan approval gate
    plan_approval_future: asyncio.Future[PlanApprovalResult] | None = None
    plan_approval_message_id: int | None = None
    plan_mode: bool = False
    # Question gate
    question_future: asyncio.Future[str] | None = None
    question_data: dict[str, Any] | None = None
    question_message_id: int | None = None
    # Todo display
    todo_items: list[dict[str, Any]] = field(default_factory=lambda: list[dict[str, Any]]())
    agent_log: logging.Logger | None = None
    last_failed_resume_id: str | None = None


def discord_state(session: AgentSession) -> DiscordAgentState:
    """Get the DiscordAgentState from a session, creating one if absent."""
    if session.frontend_state is None:
        session.frontend_state = DiscordAgentState()
    return session.frontend_state  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Agent logger setup
# ---------------------------------------------------------------------------


def setup_agent_log(session: AgentSession) -> None:
    """Set up per-agent logger writing to <LOG_DIR>/<name>.log."""
    from axi.log_context import StructuredContextFilter

    os.makedirs(config.LOG_DIR, exist_ok=True)
    logger = logging.getLogger(f"agent.{session.name}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    if not logger.handlers:
        fh = RotatingFileHandler(
            os.path.join(config.LOG_DIR, f"{session.name}.log"),
            maxBytes=5 * 1024 * 1024,
            backupCount=2,
        )
        fh.setLevel(logging.DEBUG)
        fh.addFilter(StructuredContextFilter())
        fmt = logging.Formatter("%(asctime)s %(levelname)-8s [%(ctx_prefix)s] %(message)s")
        fmt.converter = time.gmtime
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    session.agent_log = logger
