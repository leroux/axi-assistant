"""Live AgentHub contract tests using the existing smoke-test Discord harness."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from .conftest import agent_cwd

if TYPE_CHECKING:
    from .helpers import Discord

AXI_PY_DIR = Path(__file__).parent.parent


@pytest.mark.slow
def test_agenthub_live_queue_contract(discord: Discord, master_channel: str) -> None:
    name = "smoke-ah-queue"
    try:
        discord.send_and_wait(
            master_channel,
            f'Spawn an agent named "{name}" with cwd "{agent_cwd(name)}" and prompt "Say exactly: READY_QUEUE"',
            timeout=180.0,
        )
        time.sleep(3)
        agent_ch = discord.find_channel(name)
        assert agent_ch is not None
        discord.wait_for_bot(agent_ch, after="0", timeout=120.0)

        first_id = discord.send(agent_ch, "Count from 1 to 20, one per line")
        time.sleep(0.5)
        second_id = discord.send(agent_ch, "Say exactly: QUEUE_FOLLOWUP_OK")

        text = discord.poll_history(agent_ch, after=first_id, check="QUEUE_FOLLOWUP_OK", timeout=180.0, poll_interval=4.0)
        assert "QUEUE_FOLLOWUP_OK" in text
        discord.wait_for_bot(agent_ch, after=second_id, timeout=30.0, sentinel=False)
    finally:
        discord.send_and_wait(master_channel, f'Kill the agent named "{name}"', timeout=60.0)


@pytest.mark.slow
def test_agenthub_live_reuse_channel_after_kill(discord: Discord, master_channel: str) -> None:
    name = "smoke-ah-reuse"
    first_channel: str | None = None
    try:
        discord.send_and_wait(
            master_channel,
            f'Spawn an agent named "{name}" with cwd "{agent_cwd(name)}" and prompt "Say exactly: FIRST_OK"',
            timeout=180.0,
        )
        time.sleep(3)
        first_channel = discord.find_channel(name)
        assert first_channel is not None
        discord.wait_for_bot(first_channel, after="0", timeout=120.0)

        discord.send_and_wait(master_channel, f'Kill the agent named "{name}"', timeout=60.0)
        time.sleep(5)

        discord.send_and_wait(
            master_channel,
            f'Spawn an agent named "{name}" with cwd "{agent_cwd(name)}" and prompt "Say exactly: SECOND_OK"',
            timeout=180.0,
        )
        time.sleep(3)
        second_channel = discord.find_channel(name)
        assert second_channel == first_channel

        msgs = discord.send_and_wait(second_channel, "Say exactly: SECOND_OK", timeout=120.0)
        text = discord.bot_response_text(msgs)
        assert "SECOND_OK" in text
    finally:
        discord.send_and_wait(master_channel, f'Kill the agent named "{name}"', timeout=60.0)


@pytest.mark.slow
def test_agenthub_live_restart_then_wake_existing_agent(discord: Discord, master_channel: str) -> None:
    name = "smoke-ah-restart"
    try:
        discord.send_and_wait(
            master_channel,
            f'Spawn an agent named "{name}" with cwd "{agent_cwd(name)}" and prompt "Say exactly: BEFORE_RESTART"',
            timeout=180.0,
        )
        time.sleep(3)
        agent_ch = discord.find_channel(name)
        assert agent_ch is not None
        discord.wait_for_bot(agent_ch, after="0", timeout=120.0)

        latest = discord.latest_message_id(master_channel)
        env = os.environ.copy()
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        subprocess.run(
            ["uv", "run", "python", "../axi_test.py", "restart", "smoke-test"],
            cwd=str(AXI_PY_DIR),
            env=env,
            capture_output=True,
            timeout=30,
        )
        discord.poll_history(master_channel, after=latest, check="ready", timeout=60.0, poll_interval=4.0)
        time.sleep(5)

        msgs = discord.send_and_wait(agent_ch, "Say exactly: AFTER_RESTART_OK", timeout=120.0)
        text = discord.bot_response_text(msgs)
        assert "AFTER_RESTART_OK" in text
    finally:
        discord.send_and_wait(master_channel, f'Kill the agent named "{name}"', timeout=60.0)
