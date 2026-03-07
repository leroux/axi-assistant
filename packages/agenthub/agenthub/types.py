"""Core types for agent orchestration.

AgentSession is a flat data container — lifecycle operations are module-level
functions in lifecycle.py, registry.py, etc. that take (hub, session) args.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import logging

from claudewire.events import ActivityState

# Type alias: message content is either a plain string or a list of
# Anthropic API content blocks (text, image, tool_use, etc.)
MessageContent = str | list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Agent session — hub-facing fields only
# ---------------------------------------------------------------------------


@dataclass
class AgentSession:
    """One agent's state as seen by the orchestration layer.

    Discord-specific state (channel IDs, reactions, UI futures) lives in
    frontend_state, which the hub never touches. The frontend casts it to
    its own type (e.g. DiscordAgentState).
    """

    name: str
    agent_type: str = "claude_code"
    client: Any = None  # ClaudeSDKClient — opaque to the hub
    cwd: str = ""
    query_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Message queue — entries are opaque to the hub. Frontend defines the shape
    # (e.g. Discord uses (content, channel, orig_message) 3-tuples).
    message_queue: deque[Any] = field(default_factory=deque)
    last_activity: datetime = field(default_factory=lambda: datetime.now(UTC))
    idle_reminder_count: int = 0
    session_id: str | None = None
    system_prompt: Any = None  # SystemPromptPreset | str — opaque
    system_prompt_hash: str | None = None  # Hash for prompt change detection across restarts
    mcp_servers: dict[str, Any] | None = None
    mcp_server_names: list[str] | None = None  # Custom MCP server names from mcp_servers.json (for topic persistence)
    reconnecting: bool = False  # True during bridge reconnect
    bridge_busy: bool = False  # True when reconnected to a mid-task CLI
    activity: ActivityState = field(default_factory=ActivityState)
    plan_mode: bool = False
    agent_log: logging.Logger | None = None
    last_failed_resume_id: str | None = None  # Session ID that failed resume (prevents stale-ID cycle)
    transport: Any = None  # BridgeTransport for flowcoder agents (set by create_client)
    frontend_state: Any = None  # Opaque — frontend casts to its own type
    # Custom compact instructions — used for both system prompt injection and manual /compact
    compact_instructions: str | None = None
    # Context window monitoring (updated from stderr autocompact debug lines)
    context_tokens: int = 0
    context_window: int = 0
    # True while context compaction is in progress (prevents interrupts)
    compacting: bool = False


# ---------------------------------------------------------------------------
# Usage tracking
# ---------------------------------------------------------------------------


@dataclass
class SessionUsage:
    """Per-session usage statistics."""

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
    """Rate limit quota state from the Claude API."""

    status: str  # "allowed", "allowed_warning", "rejected"
    resets_at: datetime
    rate_limit_type: str  # "five_hour"
    utilization: float | None = None  # 0.0-1.0
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ConcurrencyLimitError(Exception):
    """Raised when the awake-agent concurrency limit is reached and no slots can be freed."""
