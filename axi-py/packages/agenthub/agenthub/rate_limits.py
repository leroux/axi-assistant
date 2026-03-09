"""Rate limit tracking, parsing, usage recording, and quota management.

RateLimitTracker holds the state. Functions operate on tracker instances.
Notification side effects are injected as callbacks (DI pattern).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from opentelemetry import trace

from agenthub.types import RateLimitQuota, SessionUsage

if TYPE_CHECKING:
    from claudewire.events import RateLimitInfo

log = logging.getLogger(__name__)
_tracer = trace.get_tracer(__name__)


# ---------------------------------------------------------------------------
# State holder
# ---------------------------------------------------------------------------


class RateLimitTracker:
    """Holds rate limit and usage state.

    Class exists because AgentHub needs to own this state (no module-level
    globals). Logic is in the module-level functions below.
    """

    def __init__(
        self,
        *,
        usage_history_path: str | None = None,
        rate_limit_history_path: str | None = None,
    ) -> None:
        self.rate_limited_until: datetime | None = None
        self.session_usage: dict[str, SessionUsage] = {}
        self.rate_limit_quotas: dict[str, RateLimitQuota] = {}
        self.usage_history_path = usage_history_path
        self.rate_limit_history_path = rate_limit_history_path


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def parse_rate_limit_seconds(text: str) -> int:
    """Parse wait duration from rate limit error text. Returns seconds."""
    text_lower = text.lower()

    match = re.search(
        r"(?:in|after)\s+(\d+)\s*(seconds?|minutes?|mins?|hours?|hrs?)", text_lower
    )
    if match:
        value = int(match.group(1))
        unit = match.group(2)
        if unit.startswith("min"):
            return value * 60
        elif unit.startswith(("hour", "hr")):
            return value * 3600
        return value

    match = re.search(r"retry\s+after\s+(\d+)", text_lower)
    if match:
        return int(match.group(1))

    match = re.search(r"(\d+)\s*(?:seconds?|secs?)", text_lower)
    if match:
        return int(match.group(1))

    match = re.search(r"(\d+)\s*(?:minutes?|mins?)", text_lower)
    if match:
        return int(match.group(1)) * 60

    return 300


def format_time_remaining(seconds: int) -> str:
    """Format seconds into a human-readable duration string."""
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        minutes = seconds // 60
        secs = seconds % 60
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    else:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"


# ---------------------------------------------------------------------------
# State accessors
# ---------------------------------------------------------------------------


def is_rate_limited(tracker: RateLimitTracker) -> bool:
    """Check if we're currently rate limited."""
    if tracker.rate_limited_until is None:
        return False
    if datetime.now(UTC) >= tracker.rate_limited_until:
        tracker.rate_limited_until = None
        return False
    return True


def rate_limit_remaining_seconds(tracker: RateLimitTracker) -> int:
    """Get remaining rate limit time in seconds."""
    if tracker.rate_limited_until is None:
        return 0
    remaining = (tracker.rate_limited_until - datetime.now(UTC)).total_seconds()
    return max(0, int(remaining))


# ---------------------------------------------------------------------------
# Usage recording
# ---------------------------------------------------------------------------


def record_session_usage(tracker: RateLimitTracker, agent_name: str, msg: Any) -> None:
    """Update in-memory session usage stats and append to JSONL history.

    msg is expected to have: session_id, total_cost_usd, num_turns,
    duration_ms, duration_api_ms, is_error, usage (dict with input/output tokens).
    """
    sid = getattr(msg, "session_id", None) or (msg.get("session_id") if isinstance(msg, dict) else None)
    if not sid:
        return

    now = datetime.now(UTC)
    usage: dict[str, Any] = getattr(msg, "usage", None) or {}
    if isinstance(msg, dict):
        usage = msg.get("usage") or {}
    input_tokens: int = usage.get("input_tokens", 0)
    output_tokens: int = usage.get("output_tokens", 0)

    if sid not in tracker.session_usage:
        tracker.session_usage[sid] = SessionUsage(agent_name=agent_name, first_query=now)
    entry = tracker.session_usage[sid]
    entry.queries += 1
    entry.total_cost_usd += getattr(msg, "total_cost_usd", 0) or 0.0
    entry.total_turns += getattr(msg, "num_turns", 0) or 0
    entry.total_duration_ms += getattr(msg, "duration_ms", 0) or 0
    entry.total_input_tokens += input_tokens
    entry.total_output_tokens += output_tokens
    entry.last_query = now

    if tracker.usage_history_path:
        try:
            record: dict[str, Any] = {
                "ts": now.isoformat(),
                "agent": agent_name,
                "session_id": sid,
                "cost_usd": getattr(msg, "total_cost_usd", None),
                "turns": getattr(msg, "num_turns", None),
                "duration_ms": getattr(msg, "duration_ms", None),
                "duration_api_ms": getattr(msg, "duration_api_ms", None),
                "is_error": getattr(msg, "is_error", None),
                "usage": usage or None,
            }
            with open(tracker.usage_history_path, "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception:
            log.warning("Failed to write usage history", exc_info=True)


def update_rate_limit_quota(tracker: RateLimitTracker, info: RateLimitInfo) -> None:
    """Update quota tracking from a parsed RateLimitInfo event."""
    existing = tracker.rate_limit_quotas.get(info.rate_limit_type)
    utilization = info.utilization
    if existing is not None and utilization is None and existing.resets_at == info.resets_at:
        utilization = existing.utilization

    tracker.rate_limit_quotas[info.rate_limit_type] = RateLimitQuota(
        status=info.status,
        resets_at=info.resets_at,
        rate_limit_type=info.rate_limit_type,
        utilization=utilization,
    )

    if tracker.rate_limit_history_path:
        try:
            record = {
                "ts": datetime.now(UTC).isoformat(),
                "type": info.rate_limit_type,
                "status": info.status,
                "utilization": utilization,
            }
            with open(tracker.rate_limit_history_path, "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception:
            log.warning("Failed to write rate limit history", exc_info=True)


# ---------------------------------------------------------------------------
# Rate limit event handling (with DI for notifications)
# ---------------------------------------------------------------------------

BroadcastFn = Callable[[str], Awaitable[None]]
ScheduleExpiryFn = Callable[[float], None]


async def handle_rate_limit(
    tracker: RateLimitTracker,
    error_text: str,
    broadcast_fn: BroadcastFn,
    schedule_expiry_fn: ScheduleExpiryFn,
) -> None:
    """Handle a rate limit error: update state and notify via callbacks."""
    wait_seconds = parse_rate_limit_seconds(error_text)
    _tracer.start_span(
        "rate_limit.hit", attributes={"rate_limit.wait_seconds": wait_seconds}
    ).end()
    new_limit = datetime.now(UTC) + timedelta(seconds=wait_seconds)
    already_limited = is_rate_limited(tracker)

    if tracker.rate_limited_until is None or new_limit > tracker.rate_limited_until:
        tracker.rate_limited_until = new_limit

    log.warning(
        "Rate limited — waiting %ds (until %s)",
        wait_seconds,
        tracker.rate_limited_until.isoformat(),
    )

    if not already_limited:
        remaining = format_time_remaining(wait_seconds)
        reset_time = tracker.rate_limited_until.strftime("%H:%M:%S UTC")

        quota_lines = ""
        if tracker.rate_limit_quotas:
            rl_parts: list[str] = []
            for rl_type, quota in tracker.rate_limit_quotas.items():
                pct = f"{quota.utilization:.0%}" if quota.utilization is not None else "?"
                rl_parts.append(f"{rl_type}: {pct}")
            quota_lines = "\nUtilization: " + " · ".join(rl_parts)

        msg_text = (
            f"\u26a0\ufe0f **Rate limited by Claude API.** "
            f"Resets in ~**{remaining}** (at {reset_time}).{quota_lines}"
        )

        await broadcast_fn(msg_text)
        schedule_expiry_fn(wait_seconds)
