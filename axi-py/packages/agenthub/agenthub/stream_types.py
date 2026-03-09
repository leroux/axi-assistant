"""Normalized stream output types — frontend-agnostic events yielded by the streaming engine.

Each type represents one semantic event from an SDK conversation turn.
Frontends consume these to render their UI (Discord edits messages,
web pushes WebSocket events, etc.). No frontend-specific code here.

The streaming engine (streaming.py) yields these from raw SDK messages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Text content
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TextDelta:
    """Incremental text from the model's response."""

    text: str


@dataclass(slots=True)
class TextFlush:
    """Accumulated text ready to be rendered (mid-turn split or end-of-turn).

    reason: why the flush happened ("end_turn", "mid_turn_split",
    "assistant_msg", "block_start", "block_complete", "post_loop", "post_kill")
    """

    text: str
    reason: str = ""


# ---------------------------------------------------------------------------
# Thinking
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ThinkingStart:
    """Model started extended thinking."""



@dataclass(slots=True)
class ThinkingEnd:
    """Model finished extended thinking."""

    thinking_text: str = ""  # full thinking text (available if debug mode)


# ---------------------------------------------------------------------------
# Tool use
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ToolUseStart:
    """A tool_use content block started."""

    tool_name: str
    index: int = 0


@dataclass(slots=True)
class ToolInputDelta:
    """Partial JSON for the current tool invocation."""

    partial_json: str


@dataclass(slots=True)
class ToolUseEnd:
    """A tool_use content block completed."""

    tool_name: str
    tool_input: dict[str, Any] = field(default_factory=dict)
    preview: str | None = None  # short human-readable preview


@dataclass(slots=True)
class TodoUpdate:
    """Agent updated its todo list (extracted from TodoWrite tool use)."""

    todos: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Session / lifecycle
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SessionId:
    """Session ID update (from StreamEvent or ResultMessage)."""

    session_id: str


@dataclass(slots=True)
class StreamStart:
    """Response stream opened."""



@dataclass(slots=True)
class StreamEnd:
    """Response stream closed."""

    elapsed_s: float = 0.0
    msg_count: int = 0
    flush_count: int = 0


@dataclass(slots=True)
class QueryResult:
    """Final result from a conversation turn."""

    session_id: str | None = None
    cost_usd: float = 0.0
    num_turns: int = 0
    duration_ms: int = 0
    is_error: bool = False
    is_flowchart: bool = False  # True for session_id="flowchart"


# ---------------------------------------------------------------------------
# Errors / rate limits
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RateLimitHit:
    """Agent hit a rate limit or billing error."""

    error_type: str  # "rate_limit", "billing_error"
    error_text: str = ""


@dataclass(slots=True)
class TransientError:
    """Transient API error (triggers retry at the caller level)."""

    error_type: str
    error_text: str = ""


@dataclass(slots=True)
class StreamKilled:
    """Stream ended without a ResultMessage — CLI killed or crashed."""



# ---------------------------------------------------------------------------
# System messages
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CompactStart:
    """CLI or Axi-triggered compaction started."""

    token_count: int = 0
    self_triggered: bool = False  # True if Axi triggered it


@dataclass(slots=True)
class CompactComplete:
    """Compaction finished."""

    pre_tokens: int = 0
    trigger: str = ""  # "cli" or "axi"


@dataclass(slots=True)
class FlowchartStart:
    """Flowchart execution started."""

    command: str = ""
    block_count: int = 0


@dataclass(slots=True)
class FlowchartEnd:
    """Flowchart execution completed."""

    status: str = ""  # "completed" or "failed"
    duration_ms: int = 0
    cost_usd: float = 0.0
    blocks_executed: int = 0


@dataclass(slots=True)
class BlockStart:
    """Flowchart block started executing."""

    block_name: str = ""
    block_type: str = ""


@dataclass(slots=True)
class BlockComplete:
    """Flowchart block finished."""

    block_name: str = ""
    success: bool = True


@dataclass(slots=True)
class SystemNotification:
    """Generic system message not covered by specific types."""

    subtype: str
    data: dict[str, Any] = field(default_factory=dict)


# Union of all stream output types for type checking
StreamOutput = (
    TextDelta
    | TextFlush
    | ThinkingStart
    | ThinkingEnd
    | ToolUseStart
    | ToolInputDelta
    | ToolUseEnd
    | TodoUpdate
    | SessionId
    | StreamStart
    | StreamEnd
    | QueryResult
    | RateLimitHit
    | TransientError
    | StreamKilled
    | CompactStart
    | CompactComplete
    | FlowchartStart
    | FlowchartEnd
    | BlockStart
    | BlockComplete
    | SystemNotification
)
