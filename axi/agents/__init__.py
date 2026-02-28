# pyright: reportUnusedImport=false
"""Agent lifecycle, streaming, rate limits, bridge, and channel management.

This package decomposes the former agents.py monolith into domain-focused submodules.
All public names are re-exported here so that ``import agents; agents.xxx`` works identically.
"""

from __future__ import annotations

# bridge_conn
from axi.agents.bridge_conn import connect_bridge

# discord_helpers — Discord helpers
from axi.agents.discord_helpers import (
    add_reaction,
    content_summary,
    extract_message_content,
    remove_reaction,
    send_long,
    send_system,
    send_to_exceptions,
    split_message,
)

# flowcoder
from axi.agents.flowcoder import run_inline_flowchart

# lifecycle — session lifecycle
from axi.agents.lifecycle import (
    count_awake_agents,
    end_session,
    get_master_session,
    is_awake,
    is_processing,
    reconstruct_agents_from_channels,
    reset_session,
    sleep_agent,
    wake_agent,
    wake_or_queue,
)

# messaging — message processing + spawning
from axi.agents.messaging import (
    deliver_inter_agent_message,
    process_message,
    process_message_queue,
    reclaim_agent_name,
    run_initial_prompt,
    send_prompt_to_agent,
    spawn_agent,
)

# permissions
from axi.agents.permissions import make_cwd_permission_callback

# sdk — SDK/stderr utilities
from axi.agents.sdk import (
    as_stream,
    drain_sdk_buffer,
    drain_stderr,
    make_stderr_callback,
)

# shutdown_mgr
from axi.agents.shutdown_mgr import (
    init_shutdown_coordinator,
    make_shutdown_coordinator,
)

# ---------------------------------------------------------------------------
# Re-exports from submodules
# ---------------------------------------------------------------------------
# state — module-level state + init
from axi.agents.state import (
    agents,
    bridge_conn,
    channel_to_agent,
    init,
    schedule_last_fired,
    set_utils_mcp_server,
    shutdown_coordinator,
)

# streaming — response streaming
from axi.agents.streaming import (
    extract_tool_preview,
    handle_query_timeout,
    stream_response_to_channel,
    stream_with_retry,
)

# ---------------------------------------------------------------------------
# Re-exports from channels module (agents.py used to re-export these)
# ---------------------------------------------------------------------------
from axi.channels import (
    ensure_agent_channel,
    ensure_guild_infrastructure,
    format_channel_topic,
    get_agent_channel,
    get_master_channel,
    move_channel_to_killed,
    normalize_channel_name,
)
from axi.channels import (
    parse_channel_topic as _parse_channel_topic,
)

# ---------------------------------------------------------------------------
# Re-exports from rate_limits module
# ---------------------------------------------------------------------------
from axi.rate_limits import (
    format_time_remaining,
    is_rate_limited,
    rate_limit_quotas,
    rate_limit_remaining_seconds,
    rate_limited_until,
    session_usage,
)

__all__ = [
    "_parse_channel_topic",
    # Discord helpers
    "add_reaction",
    # Module-level state
    "agents",
    # Streaming
    "as_stream",
    "bridge_conn",
    "channel_to_agent",
    # Bridge
    "connect_bridge",
    "content_summary",
    "count_awake_agents",
    "deliver_inter_agent_message",
    # SDK helpers
    "drain_sdk_buffer",
    "drain_stderr",
    "end_session",
    # Channel/guild management (re-exported from channels module)
    "ensure_agent_channel",
    "ensure_guild_infrastructure",
    # Message handling
    "extract_message_content",
    "extract_tool_preview",
    "format_channel_topic",
    "format_time_remaining",
    "get_agent_channel",
    "get_master_channel",
    "get_master_session",
    "handle_query_timeout",
    # Initialization
    "init",
    "init_shutdown_coordinator",
    # Session lifecycle
    "is_awake",
    "is_processing",
    # Rate limiting
    "is_rate_limited",
    "make_cwd_permission_callback",
    "make_shutdown_coordinator",
    "make_stderr_callback",
    "move_channel_to_killed",
    "normalize_channel_name",
    "process_message",
    "process_message_queue",
    "rate_limit_quotas",
    "rate_limit_remaining_seconds",
    "rate_limited_until",
    "reclaim_agent_name",
    "reconstruct_agents_from_channels",
    "remove_reaction",
    "reset_session",
    "run_initial_prompt",
    # Flowcoder
    "run_inline_flowchart",
    "schedule_last_fired",
    "send_long",
    "send_prompt_to_agent",
    "send_system",
    "send_to_exceptions",
    "session_usage",
    "set_utils_mcp_server",
    "shutdown_coordinator",
    "sleep_agent",
    "spawn_agent",
    "split_message",
    "stream_response_to_channel",
    "stream_with_retry",
    "wake_agent",
    "wake_or_queue",
]
