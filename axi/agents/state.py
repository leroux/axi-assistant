"""Module-level mutable state and initialization."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from axi import channels as _channels_mod

if TYPE_CHECKING:
    from datetime import datetime

    from discord.ext.commands import Bot

    from axi.axi_types import AgentSession
    from axi.bridge import BridgeConnection
    from axi.shutdown import ShutdownCoordinator


log = logging.getLogger("axi")


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_bot: Bot | None = None

agents: dict[str, AgentSession] = {}
channel_to_agent: dict[int, str] = {}  # channel_id -> agent_name
_wake_lock = asyncio.Lock()

# Bridge connection — initialized in on_ready(), used by wake_agent/sleep_agent
bridge_conn: BridgeConnection | None = None

# Shutdown coordinator — initialized via init_shutdown_coordinator() from on_ready
shutdown_coordinator: ShutdownCoordinator | None = None

# Scheduler state
schedule_last_fired: dict[str, datetime] = {}

# Stream tracing
_stream_counter = 0

# MCP server injection (set by bot.py after tools.py creates them)
_utils_mcp_server: Any = None


# ---------------------------------------------------------------------------
# Initialization — called once from bot.py after Bot creation
# ---------------------------------------------------------------------------


def init(bot_instance: Bot) -> None:
    """Inject the Bot reference. Called once from bot.py."""
    global _bot
    _bot = bot_instance
    from axi.agents.discord_helpers import send_to_exceptions

    _channels_mod.init(bot_instance, agents, channel_to_agent, send_to_exceptions)


def set_utils_mcp_server(server: Any) -> None:
    """Set the utils MCP server reference. Called from bot.py after tools.py init."""
    global _utils_mcp_server
    _utils_mcp_server = server


def _next_stream_id(agent_name: str) -> str:  # pyright: ignore[reportUnusedFunction]  # called from streaming
    """Generate a unique stream ID for tracing."""
    global _stream_counter
    _stream_counter += 1
    return f"{agent_name}:S{_stream_counter}"
