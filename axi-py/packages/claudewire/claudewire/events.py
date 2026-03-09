"""Event parsing and activity tracking for the Claude CLI stream-json protocol."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

# ---------------------------------------------------------------------------
# Activity state
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
# Stream event helpers
# ---------------------------------------------------------------------------


def update_activity(activity: ActivityState, event: dict[str, Any]) -> None:
    """Update activity state from a raw Claude stream event.

    Parses content_block_start/delta/stop, message_start, and message_delta
    events to track what the agent is currently doing.
    """
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


# ---------------------------------------------------------------------------
# Rate limit event parsing
# ---------------------------------------------------------------------------


@dataclass
class RateLimitInfo:
    """Parsed rate_limit_event from the Claude CLI stream."""

    rate_limit_type: str  # "five_hour", etc.
    status: str  # "allowed", "allowed_warning", "rejected"
    resets_at: datetime
    utilization: float | None = None


def parse_rate_limit_event(data: dict[str, Any]) -> RateLimitInfo | None:
    """Parse a rate_limit_event stream message.

    Returns None if the message isn't a rate_limit_event or lacks required data.
    """
    if data.get("type") != "rate_limit_event":
        return None
    info = data.get("rate_limit_info", {})
    resets_at_unix = info.get("resetsAt")
    if resets_at_unix is None:
        return None
    return RateLimitInfo(
        rate_limit_type=info.get("rateLimitType", "unknown"),
        status=info.get("status", "unknown"),
        resets_at=datetime.fromtimestamp(resets_at_unix, tz=UTC),
        utilization=info.get("utilization"),
    )


# ---------------------------------------------------------------------------
# Stream helpers
# ---------------------------------------------------------------------------


async def as_stream(content: str | list[dict[str, Any]]):
    """Wrap a prompt as an AsyncIterable for the SDK streaming interface."""
    yield {
        "type": "user",
        "session_id": "",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
    }
