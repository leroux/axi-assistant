"""Rate limit tracking, parsing, usage recording, and quota management.

Extracted from agents.py. All rate-limit state lives here; notification
side effects are injected as callbacks (matching the shutdown.py DI pattern).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from opentelemetry import trace

from axi import config
from axi.axi_types import RateLimitQuota, SessionUsage

if TYPE_CHECKING:
    from claude_agent_sdk import ResultMessage

log = logging.getLogger(__name__)
_tracer = trace.get_tracer(__name__)

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

rate_limited_until: datetime | None = None
session_usage: dict[str, SessionUsage] = {}
rate_limit_quotas: dict[str, RateLimitQuota] = {}

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def parse_rate_limit_seconds(text: str) -> int:
    """Parse wait duration from rate limit error text. Returns seconds."""
    text_lower = text.lower()

    match = re.search(r"(?:in|after)\s+(\d+)\s*(seconds?|minutes?|mins?|hours?|hrs?)", text_lower)
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


def is_rate_limited() -> bool:
    """Check if we're currently rate limited."""
    global rate_limited_until
    if rate_limited_until is None:
        return False
    if datetime.now(UTC) >= rate_limited_until:
        rate_limited_until = None
        return False
    return True


def rate_limit_remaining_seconds() -> int:
    """Get remaining rate limit time in seconds."""
    if rate_limited_until is None:
        return 0
    remaining = (rate_limited_until - datetime.now(UTC)).total_seconds()
    return max(0, int(remaining))


# ---------------------------------------------------------------------------
# Usage recording
# ---------------------------------------------------------------------------


def record_session_usage(agent_name: str, msg: ResultMessage) -> None:
    """Update in-memory session usage stats and append to JSONL history."""
    sid = msg.session_id
    if not sid:
        return
    now = datetime.now(UTC)
    usage: dict[str, Any] = getattr(msg, "usage", None) or {}
    input_tokens: int = usage.get("input_tokens", 0)
    output_tokens: int = usage.get("output_tokens", 0)

    if sid not in session_usage:
        session_usage[sid] = SessionUsage(agent_name=agent_name, first_query=now)
    entry = session_usage[sid]
    entry.queries += 1
    entry.total_cost_usd += msg.total_cost_usd or 0.0
    entry.total_turns += msg.num_turns or 0
    entry.total_duration_ms += msg.duration_ms or 0
    entry.total_input_tokens += input_tokens
    entry.total_output_tokens += output_tokens
    entry.last_query = now

    try:
        record: dict[str, Any] = {
            "ts": now.isoformat(),
            "agent": agent_name,
            "session_id": sid,
            "cost_usd": msg.total_cost_usd,
            "turns": msg.num_turns,
            "duration_ms": msg.duration_ms,
            "duration_api_ms": msg.duration_api_ms,
            "is_error": msg.is_error,
            "usage": usage or None,
        }
        with open(config.USAGE_HISTORY_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        log.warning("Failed to write usage history", exc_info=True)


def update_rate_limit_quota(data: dict[str, Any]) -> None:
    """Parse rate_limit_info from a stream event and update quota tracking."""
    info = data.get("rate_limit_info", {})
    resets_at_unix = info.get("resetsAt")
    if resets_at_unix is None:
        return
    rl_type = info.get("rateLimitType", "unknown")
    new_status = info.get("status", "unknown")
    new_resets_at = datetime.fromtimestamp(resets_at_unix, tz=UTC)
    new_utilization = info.get("utilization")

    existing = rate_limit_quotas.get(rl_type)
    if existing is not None and new_utilization is None and existing.resets_at == new_resets_at:
        new_utilization = existing.utilization

    rate_limit_quotas[rl_type] = RateLimitQuota(
        status=new_status,
        resets_at=new_resets_at,
        rate_limit_type=rl_type,
        utilization=new_utilization,
    )

    try:
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "type": rl_type,
            "status": new_status,
            "utilization": new_utilization,
        }
        with open(config.RATE_LIMIT_HISTORY_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        log.warning("Failed to write rate limit history", exc_info=True)


# ---------------------------------------------------------------------------
# Rate limit event handling (with DI for notifications)
# ---------------------------------------------------------------------------

#: Callback type: broadcast a message to all known agent channels
BroadcastFn = Callable[[str], Awaitable[None]]

#: Callback type: schedule a delayed notification when rate limit expires
ScheduleExpiryFn = Callable[[float], None]


async def handle_rate_limit(
    error_text: str,
    broadcast_fn: BroadcastFn,
    schedule_expiry_fn: ScheduleExpiryFn,
) -> None:
    """Handle a rate limit error: update global state and notify channels.

    Args:
        error_text: The error message from the API.
        broadcast_fn: Async callable to send a message to all agent channels.
        schedule_expiry_fn: Callable to schedule an expiry notification after N seconds.
    """
    global rate_limited_until

    wait_seconds = parse_rate_limit_seconds(error_text)
    _tracer.start_span("rate_limit.hit", attributes={"rate_limit.wait_seconds": wait_seconds}).end()
    new_limit = datetime.now(UTC) + timedelta(seconds=wait_seconds)
    already_limited = is_rate_limited()

    if rate_limited_until is None or new_limit > rate_limited_until:
        rate_limited_until = new_limit

    log.warning("Rate limited — waiting %ds (until %s)", wait_seconds, rate_limited_until.isoformat())

    if not already_limited:
        remaining = format_time_remaining(wait_seconds)
        reset_time = rate_limited_until.strftime("%H:%M:%S UTC")

        quota_lines = ""
        if rate_limit_quotas:
            rl_parts: list[str] = []
            for rl_type, quota in rate_limit_quotas.items():
                pct = f"{quota.utilization:.0%}" if quota.utilization is not None else "?"
                rl_parts.append(f"{rl_type}: {pct}")
            quota_lines = "\nUtilization: " + " · ".join(rl_parts)

        msg_text = f"⚠️ **Rate limited by Claude API.** Resets in ~**{remaining}** (at {reset_time}).{quota_lines}"

        await broadcast_fn(msg_text)
        schedule_expiry_fn(wait_seconds)


async def notify_rate_limit_expired(
    delay: float,
    get_master_channel_fn: Callable[[], Awaitable[Any]],
    send_system_fn: Callable[[Any, str], Awaitable[None]],
) -> None:
    """Sleep until rate limit expires, then notify master channel."""
    try:
        await asyncio.sleep(delay)
        if not is_rate_limited():
            ch = await get_master_channel_fn()
            if ch:
                await send_system_fn(ch, "✅ Rate limit expired — usage available again.")
    except asyncio.CancelledError:
        return
    except Exception:
        log.warning("Failed to send rate limit expiry notification", exc_info=True)
