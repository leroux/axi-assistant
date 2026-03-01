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

    from axi.flowcoder import BridgeFlowcoderProcess, FlowcoderProcess

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
# Activity tracking
# ---------------------------------------------------------------------------


@dataclass
class ActivityState:
    """Real-time activity tracking for an agent during a query."""

    phase: str = "idle"  # "thinking", "writing", "tool_use", "waiting", "starting", "idle"
    tool_name: str | None = None  # Current tool being called (e.g. "Bash", "Read")
    tool_input_preview: str = ""  # First ~200 chars of tool input JSON
    thinking_text: str = ""  # Accumulated thinking content for debug display
    turn_count: int = 0  # Number of API turns in current query
    query_started: datetime | None = None  # When the current query began
    last_event: datetime | None = None  # When the last stream event arrived
    text_chars: int = 0  # Characters of text generated in current turn


TOOL_DISPLAY_NAMES = {
    "Bash": "running bash command",
    "Read": "reading file",
    "Write": "writing file",
    "Edit": "editing file",
    "MultiEdit": "editing file",
    "Glob": "searching for files",
    "Grep": "searching code",
    "WebSearch": "searching the web",
    "WebFetch": "fetching web page",
    "Task": "running subagent",
    "NotebookEdit": "editing notebook",
    "TodoWrite": "updating tasks",
}


def tool_display(name: str) -> str:
    """Human-readable description of a tool call."""
    if name in TOOL_DISPLAY_NAMES:
        return TOOL_DISPLAY_NAMES[name]
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            return f"{parts[1]}: {parts[2]}"
    return f"using {name}"


# ---------------------------------------------------------------------------
# Agent session
# ---------------------------------------------------------------------------


@dataclass
class AgentSession:
    name: str
    agent_type: str = "claude_code"  # "claude_code" or "flowcoder"
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
    flowcoder_process: FlowcoderProcess | BridgeFlowcoderProcess | None = None
    flowcoder_command: str = ""
    flowcoder_args: str = ""
    debug: bool = field(
        default_factory=lambda: os.environ.get("DISCORD_DEBUG", "").strip().lower() in ("1", "true", "on")
    )  # Post tool calls and thinking phases to Discord
    plan_approval_future: asyncio.Future[PlanApprovalResult] | None = None  # Set when waiting for user to approve/reject a plan
    plan_mode: bool = False  # When True, agent is in plan mode (read-only, plan before implement)
    todo_message_id: int | None = None  # Discord message ID for the todo list display (edited in-place on updates)
    todo_items: list[dict[str, Any]] = field(default_factory=lambda: list[dict[str, Any]]())  # Last known todo list from TodoWrite
    agent_log: logging.Logger | None = None

    def __post_init__(self) -> None:
        """Set up per-agent logger writing to <assistant_dir>/logs/<name>.log."""
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
            _agent_fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s")
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
# Exceptions
# ---------------------------------------------------------------------------


class ConcurrencyLimitError(Exception):
    """Raised when the awake-agent concurrency limit is reached and no slots can be freed."""
