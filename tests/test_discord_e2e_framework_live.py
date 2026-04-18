"""Live Discord E2E tests for the reusable framework-backed Axi adapter."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

from .axi_e2e import AxiDiscordEntrypoints
from .conftest import agent_cwd

if TYPE_CHECKING:
    from .helpers import Discord


@pytest.mark.slow
def test_flowcoder_spawn_with_startup_command(discord: Discord, master_channel: str) -> None:
    name = "smoke-fc-seed"
    master = discord.channel(master_channel, name="axi-master")
    axi = AxiDiscordEntrypoints(master)
    try:
        axi.spawn_agent(
            name=name,
            cwd=agent_cwd(name),
            prompt="Say exactly: STARTUP_FLOW_OK",
            command="prompt",
            timeout=180.0,
        )
        time.sleep(3)
        agent = discord.require_channel(name)
        text = agent.wait_for_bot_response(
            after="0",
            timeout=120.0,
            poll_interval=4.0,
            sentinel=None,
            check="STARTUP_FLOW_OK",
        ).text
        assert "STARTUP_FLOW_OK" in text
    finally:
        axi.kill_agent(name, timeout=60.0)
