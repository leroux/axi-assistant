# pyright: reportPrivateUsage=false
"""Shutdown coordinator initialization."""

from __future__ import annotations

import logging
from typing import Any

from agents.discord_helpers import send_system
from agents.lifecycle import sleep_agent
from agents.state import agents
from channels import get_agent_channel, get_master_channel
from shutdown import ShutdownCoordinator, exit_for_restart, kill_supervisor

log = logging.getLogger("axi")


async def _notify_agent_channel(agent_name: str, message: str) -> None:
    """Notify an agent's Discord channel with a system message."""
    channel = await get_agent_channel(agent_name)
    if channel:
        await send_system(channel, message)


def make_shutdown_coordinator(
    *,
    close_bot_fn: Any,
    kill_fn: Any,
    goodbye_fn: Any,
    bridge_mode: bool,
) -> ShutdownCoordinator:
    """Create a ShutdownCoordinator with standard agents/sleep/notify wiring."""
    return ShutdownCoordinator(
        agents=agents,
        sleep_fn=lambda s: sleep_agent(s, force=True),
        close_bot_fn=close_bot_fn,
        kill_fn=kill_fn,
        notify_fn=_notify_agent_channel,
        goodbye_fn=goodbye_fn,
        bridge_mode=bridge_mode,
    )


def init_shutdown_coordinator() -> None:
    """Wire up the ShutdownCoordinator with real bot callbacks.

    Called once from on_ready after all helpers are defined.
    """
    import agents.state as _state
    from agents.state import _bot, bridge_conn

    assert _bot is not None

    async def _send_goodbye() -> None:
        from flowcoder import BridgeFlowcoderProcess

        for s in agents.values():
            if s.flowcoder_process and isinstance(s.flowcoder_process, BridgeFlowcoderProcess):
                await s.flowcoder_process.detach()
                log.info("Detached bridge flowcoder for '%s' before shutdown", s.name)
                s.flowcoder_process = None

        master_ch = await get_master_channel()
        if master_ch:
            await master_ch.send("*System:* Shutting down — see you soon!")

    use_bridge = bridge_conn is not None and bridge_conn.is_alive
    _state.shutdown_coordinator = make_shutdown_coordinator(
        close_bot_fn=_bot.close,
        kill_fn=exit_for_restart if use_bridge else kill_supervisor,
        goodbye_fn=_send_goodbye,
        bridge_mode=use_bridge,
    )
