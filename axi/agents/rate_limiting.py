# pyright: reportPrivateUsage=false
"""Rate limit handling — adapts pure rate_limits module for agents context."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from discord import TextChannel

from axi.agents.discord_helpers import send_system
from axi.agents.state import agents
from axi.channels import get_master_channel
from axi.rate_limits import handle_rate_limit as _rl_handle_rate_limit
from axi.rate_limits import notify_rate_limit_expired

if TYPE_CHECKING:
    from axi.axi_types import AgentSession

log = logging.getLogger("axi")


async def _handle_rate_limit(error_text: str, session: AgentSession, channel: TextChannel) -> None:  # pyright: ignore[reportUnusedFunction]  # called from streaming
    """Handle a rate limit error: set global state, notify all agent channels."""
    from axi.agents.state import _bot

    assert _bot is not None
    bot_ref = _bot

    async def _broadcast(msg_text: str) -> None:
        notified_channels: set[int] = set()
        for agent_session in agents.values():
            if not agent_session.discord_channel_id:
                continue
            ch = bot_ref.get_channel(agent_session.discord_channel_id)
            if isinstance(ch, TextChannel) and ch.id not in notified_channels:
                notified_channels.add(ch.id)
                try:
                    await send_system(ch, msg_text)
                except Exception:
                    log.warning("Failed to notify channel %s about rate limit", ch.id)

    def _schedule_expiry(delay: float) -> None:
        asyncio.create_task(
            notify_rate_limit_expired(delay, get_master_channel, send_system)
        )

    await _rl_handle_rate_limit(error_text, _broadcast, _schedule_expiry)
