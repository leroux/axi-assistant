"""Pure data types for the Axi bot.

Dataclasses, type aliases, and exceptions. No methods that delegate to other modules.
AgentSession is a pure data container — lifecycle operations use module-level
functions in agents.py (e.g. agents.wake_agent(session)).
"""

from __future__ import annotations

__all__ = [
    "TOOL_DISPLAY_NAMES",
    "ActivityState",
    "AgentSession",
    "ConcurrencyLimitError",
    "ContentBlock",
    "McpArgs",
    "McpResult",
    "MessageContent",
    "PlanApprovalResult",
    "RateLimitQuota",
    "SessionUsage",
    "tool_display",
]

import asyncio
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from typing import TYPE_CHECKING, Any, TypedDict

from axi import config

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeSDKClient
    from claude_agent_sdk.types import SystemPromptPreset


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

# Anthropic API content block (text, image, tool_use, etc.)
ContentBlock = dict[str, Any]

# Message content: plain string or list of content blocks
MessageContent = str | list[ContentBlock]

# MCP tool handler: receives JSON args, returns MCP response
McpArgs = dict[str, Any]
McpResult = dict[str, Any]


class PlanApprovalResult(TypedDict):
    """Result from the plan approval gate in on_message."""

    approved: bool
    message: str  # empty string when no feedback



# ---------------------------------------------------------------------------
# Activity tracking (canonical definitions live in claudewire.events)
# ---------------------------------------------------------------------------

from claudewire.events import TOOL_DISPLAY_NAMES, ActivityState, tool_display

# ---------------------------------------------------------------------------
# Agent session
# ---------------------------------------------------------------------------


@dataclass
class AgentSession:
    name: str
    agent_type: str = "flowcoder"  # "flowcoder" (default) or "claude_code"
    client: ClaudeSDKClient | None = None
    cwd: str = ""
    query_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    stderr_buffer: list[str] = field(default_factory=lambda: list[str]())
    stderr_lock: threading.Lock = field(default_factory=threading.Lock)
    last_activity: datetime = field(default_factory=lambda: datetime.now(UTC))
    system_prompt: SystemPromptPreset | str | None = None
    system_prompt_hash: str | None = None  # Hash of system prompt text for change detection across restarts
    system_prompt_posted: bool = False  # Set True after posting system prompt to Discord
    last_idle_notified: datetime | None = None
    idle_reminder_count: int = 0
    session_id: str | None = None
    discord_channel_id: int | None = None
    message_queue: deque[tuple[MessageContent, Any, Any]] = field(default_factory=lambda: deque[tuple[MessageContent, Any, Any]]())
    mcp_servers: dict[str, Any] | None = None
    reconnecting: bool = False  # True during bridge reconnect (blocks on_message from waking)
    bridge_busy: bool = False  # True when reconnected to a mid-task CLI (bridge idle=False)
    activity: ActivityState = field(default_factory=ActivityState)
    debug: bool = field(
        default_factory=lambda: os.environ.get("DISCORD_DEBUG", "").strip().lower() in ("1", "true", "on")
    )  # Post tool calls and thinking phases to Discord
    plan_approval_future: asyncio.Future[PlanApprovalResult] | None = None  # Set when waiting for user to approve/reject a plan
    plan_approval_message_id: int | None = None  # Discord message ID of the approval prompt (for reaction-based approval)
    plan_mode: bool = False  # When True, agent is in plan mode (read-only, plan before implement)
    question_future: asyncio.Future[str] | None = None  # Set when waiting for user to answer a single question
    question_data: dict[str, Any] | None = None  # Current question being asked (options, multiSelect, etc.)
    question_message_id: int | None = None  # Discord message ID of the current question (for reaction matching)
    todo_message_id: int | None = None  # Discord message ID for the todo list display (edited in-place on updates)
    todo_items: list[dict[str, Any]] = field(default_factory=lambda: list[dict[str, Any]]())  # Last known todo list from TodoWrite
    agent_log: logging.Logger | None = None

    def __post_init__(self) -> None:
        """Set up per-agent logger writing to <assistant_dir>/logs/<name>.log."""
        from axi.log_context import StructuredContextFilter

        os.makedirs(config.LOG_DIR, exist_ok=True)
        logger = logging.getLogger(f"agent.{self.name}")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        if not logger.handlers:  # Avoid duplicate handlers on re-creation
            fh = RotatingFileHandler(
                os.path.join(config.LOG_DIR, f"{self.name}.log"),
                maxBytes=5 * 1024 * 1024,
                backupCount=2,
            )
            fh.setLevel(logging.DEBUG)
            fh.addFilter(StructuredContextFilter())
            _agent_fmt = logging.Formatter("%(asctime)s %(levelname)-8s [%(ctx_prefix)s] %(message)s")
            _agent_fmt.converter = time.gmtime
            fh.setFormatter(_agent_fmt)
            logger.addHandler(fh)
        self.agent_log = logger

    def close_log(self) -> None:
        """Remove all handlers from the per-agent logger."""
        if self.agent_log:
            for handler in self.agent_log.handlers[:]:
                handler.close()
                self.agent_log.removeHandler(handler)


# ---------------------------------------------------------------------------
# Usage tracking
# ---------------------------------------------------------------------------


@dataclass
class SessionUsage:
    agent_name: str
    queries: int = 0
    total_cost_usd: float = 0.0
    total_turns: int = 0
    total_duration_ms: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    first_query: datetime | None = None
    last_query: datetime | None = None


@dataclass
class RateLimitQuota:
    status: str  # "allowed", "allowed_warning", "rejected"
    resets_at: datetime  # from resetsAt unix timestamp
    rate_limit_type: str  # "five_hour"
    utilization: float | None = None  # 0.0-1.0, only present on warnings
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Exceptions (canonical definition in agenthub.types)
# ---------------------------------------------------------------------------

from agenthub.types import ConcurrencyLimitError
