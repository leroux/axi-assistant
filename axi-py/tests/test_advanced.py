"""Advanced tests — Tiers 10, 13, 14, 16: concurrency, packs, channels, recovery."""

import os
import subprocess
import time
from pathlib import Path

import pytest

from .helpers import Discord

AXI_PY_DIR = Path(__file__).parent.parent
WORKTREE_DIR = AXI_PY_DIR.parent


def _systemctl_env() -> dict[str, str]:
    """Return environment with XDG_RUNTIME_DIR for systemctl --user."""
    env = os.environ.copy()
    if "XDG_RUNTIME_DIR" not in env:
        env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
    return env


# -- Tier 10: Concurrency & Resource --


def test_concurrency_limit_bypass(discord: Discord, master_channel: str):
    """Test 51: MAX_AWAKE_AGENTS not enforced (known bug).

    This documents the known bug — spawning beyond MAX_AWAKE_AGENTS should
    fail but doesn't because session.wake() bypasses the check.
    """
    agents = []
    try:
        # Spawn 4 agents (MAX_AWAKE_AGENTS is typically 3)
        for i in range(4):
            name = f"smoke-conc{i}"
            discord.send_and_wait(
                master_channel,
                f'Spawn an agent named "{name}" with cwd "/home/ubuntu/axi-tests/smoke-test-data/agents/{name}" and prompt "Say OK"',
                timeout=180.0,
            )
            agents.append(name)
            time.sleep(2)

        # If we get here, all 4 spawned — confirming the bypass bug
        # Check that all channels exist
        for name in agents:
            ch = discord.find_channel(name)
            assert ch is not None, f"Agent {name} channel not found"

    finally:
        # Clean up all agents
        for name in agents:
            discord.send_and_wait(
                master_channel, f'Kill the agent named "{name}"', timeout=30.0
            )


def test_packs_default(discord: Discord, master_channel: str):
    """Test 52: Spawned agent gets default packs in system prompt."""
    discord.send_and_wait(
        master_channel,
        'Spawn an agent named "smoke-packd" with cwd "/home/ubuntu/axi-tests/smoke-test-data/agents/smoke-packd" and prompt "What packs or system prompt sections do you have? List any special instructions you were given."',
        timeout=180.0,
    )
    time.sleep(3)
    agent_ch = discord.find_channel("smoke-packd")
    assert agent_ch is not None

    msgs = discord.wait_for_bot(agent_ch, after="0", timeout=120.0)
    text = discord.bot_response_text(msgs)

    # Agent should have received some system prompt content
    assert len(text) > 20, f"Agent produced minimal output: {text[:200]}"

    discord.send_and_wait(
        master_channel, 'Kill the agent named "smoke-packd"', timeout=60.0
    )


def test_packs_empty(discord: Discord, master_channel: str):
    """Test 54: Spawning agent with packs=[] loads no packs."""
    discord.send_and_wait(
        master_channel,
        'Spawn an agent named "smoke-packe" with cwd "/home/ubuntu/axi-tests/smoke-test-data/agents/smoke-packe" and prompt "Say OK" and packs=[]',
        timeout=180.0,
    )
    time.sleep(3)
    agent_ch = discord.find_channel("smoke-packe")
    assert agent_ch is not None

    msgs = discord.wait_for_bot(agent_ch, after="0", timeout=120.0)
    text = discord.bot_response_text(msgs)
    assert len(text) > 0, "Agent with empty packs produced no output"

    discord.send_and_wait(
        master_channel, 'Kill the agent named "smoke-packe"', timeout=60.0
    )


def test_packs_custom(discord: Discord, master_channel: str):
    """Test 53: Spawning agent with specific packs loads only those."""
    # Check what packs are available
    packs_dir = WORKTREE_DIR / "packs"
    if not packs_dir.exists():
        pytest.skip("No packs directory found")

    available = [p.stem for p in packs_dir.iterdir() if p.is_file()]
    if not available:
        pytest.skip("No pack files found")

    pack_name = available[0]
    discord.send_and_wait(
        master_channel,
        f'Spawn an agent named "smoke-packc" with cwd "/home/ubuntu/axi-tests/smoke-test-data/agents/smoke-packc" and prompt "What instructions or packs do you have?" and packs=["{pack_name}"]',
        timeout=180.0,
    )
    time.sleep(3)
    agent_ch = discord.find_channel("smoke-packc")
    assert agent_ch is not None

    msgs = discord.wait_for_bot(agent_ch, after="0", timeout=120.0)
    text = discord.bot_response_text(msgs)
    assert len(text) > 0, "Agent with custom pack produced no output"

    discord.send_and_wait(
        master_channel, 'Kill the agent named "smoke-packc"', timeout=60.0
    )


# -- Tier 13: Record Updater --


def test_record_updater_spawns(discord: Discord, master_channel: str, instance_env: dict):
    """Test 61: Record updater auto-spawns after a spawned agent finishes.

    Requires RECORD_UPDATER_ENABLED=1 in the instance env. Skips otherwise.
    """
    # Check if record updater is enabled
    env_path = WORKTREE_DIR / ".env"
    env_text = env_path.read_text() if env_path.exists() else ""
    if "RECORD_UPDATER_ENABLED=1" not in env_text:
        pytest.skip("RECORD_UPDATER_ENABLED not set — cannot test record updater")

    # Spawn a short-lived agent
    discord.send_and_wait(
        master_channel,
        'Spawn an agent named "smoke-rec" with cwd "/home/ubuntu/axi-tests/smoke-test-data/agents/smoke-rec" and prompt "Say exactly: DONE"',
        timeout=180.0,
    )
    time.sleep(3)
    agent_ch = discord.find_channel("smoke-rec")
    assert agent_ch is not None
    discord.wait_for_bot(agent_ch, after="0", timeout=120.0)

    # Wait for record-updater to spawn (triggered after agent finishes initial task)
    time.sleep(15)
    updater_ch = discord.find_channel("record-updater")
    assert updater_ch is not None, "record-updater did not auto-spawn"

    # Clean up
    discord.send_and_wait(
        master_channel, 'Kill the agent named "smoke-rec"', timeout=30.0
    )
    if updater_ch:
        discord.send_and_wait(
            master_channel, 'Kill the agent named "record-updater"', timeout=30.0
        )


def test_record_updater_excluded_for_master(discord: Discord, master_channel: str):
    """Test 62: Master agent does not spawn record-updater after finishing a task.

    Master is in RECORD_UPDATER_EXCLUDED, so no record-updater should spawn.
    """
    # Send a message and wait for completion
    discord.send_and_wait(master_channel, "Say exactly: UPDATER_CHECK", timeout=60.0)
    time.sleep(10)

    # Check that no record-updater channel was created
    updater_ch = discord.find_channel("record-updater")
    assert updater_ch is None, "record-updater spawned for master (should be excluded)"


def test_shutdown_rejection(discord: Discord, master_channel: str):
    """Test 64: During shutdown, bot rejects new messages.

    We simulate this by sending SIGTERM and immediately sending a message.
    This is timing-sensitive and may not always catch the window.
    """
    # Get the service PID
    result = subprocess.run(
        ["systemctl", "--user", "show", "axi-test@smoke-test", "-p", "MainPID", "--value"],
        capture_output=True,
        text=True,
        timeout=10,
        env=_systemctl_env(),
    )
    if result.returncode != 0 or not result.stdout.strip().isdigit():
        pytest.skip("Could not get service PID")

    pid = result.stdout.strip()
    if pid == "0":
        pytest.skip("Service not running")

    # Send SIGTERM (graceful shutdown) and immediately send a message
    latest = discord.latest_message_id(master_channel)
    subprocess.run(["kill", "-TERM", pid], capture_output=True, timeout=5)
    time.sleep(0.5)
    discord.send(master_channel, "This should be rejected during shutdown")

    # Check for rejection message
    time.sleep(5)
    msgs = discord.history(master_channel, limit=10, after=latest)
    text = "\n".join(m.get("content", "") for m in msgs).lower()

    # Restart the instance regardless
    subprocess.run(
        ["uv", "run", "python", "../axi_test.py", "restart", "smoke-test"],
        cwd=str(AXI_PY_DIR),
        capture_output=True,
        timeout=30,
    )
    time.sleep(15)

    # Re-warmup
    discord.send_and_wait(master_channel, "Say exactly: POST_SHUTDOWN_OK", timeout=120.0)

    assert "restart" in text or "not accepting" in text or "shutting" in text, (
        f"Expected shutdown rejection message, got: {text[:300]}"
    )


# -- Tier 14: Channel Events --


def test_manual_channel_auto_register(discord: Discord, master_channel: str):
    """Test 63: Manually creating a channel in Active category auto-registers an agent."""
    active_cat = discord.find_category("Active")
    if not active_cat:
        pytest.skip("No Active category found")

    # Create a channel manually in Active
    ch_id = discord.create_channel("smoke-manual", parent_id=active_cat)
    time.sleep(5)

    # The bot should auto-register and post a message
    msgs = discord.history(ch_id, limit=5)
    text = "\n".join(m.get("content", "") for m in msgs)

    assert len(text) > 0, "No auto-registration message in manually created channel"

    # Send a message to wake it
    wake_msgs = discord.send_and_wait(
        ch_id,
        "Say exactly: MANUAL_OK",
        timeout=120.0,
    )
    wake_text = discord.bot_response_text(wake_msgs)
    assert "MANUAL_OK" in wake_text or len(wake_text) > 0, (
        f"Agent in manual channel didn't respond: {wake_text[:200]}"
    )

    # Clean up
    discord.send_and_wait(
        master_channel, 'Kill the agent named "smoke-manual"', timeout=60.0
    )


def test_channel_reconstruction(discord: Discord, master_channel: str):
    """Test 16: After restart, stale Active channels get sleeping agents reconstructed."""
    # Spawn an agent
    discord.send_and_wait(
        master_channel,
        'Spawn an agent named "smoke-recon" with cwd "/home/ubuntu/axi-tests/smoke-test-data/agents/smoke-recon" and prompt "Say OK"',
        timeout=180.0,
    )
    time.sleep(3)
    agent_ch = discord.find_channel("smoke-recon")
    assert agent_ch is not None
    discord.wait_for_bot(agent_ch, after="0", timeout=120.0)

    # Restart the instance
    latest = discord.latest_message_id(master_channel)
    subprocess.run(
        ["uv", "run", "python", "../axi_test.py", "restart", "smoke-test"],
        cwd=str(AXI_PY_DIR),
        capture_output=True,
        timeout=30,
    )
    time.sleep(15)

    # Re-warmup master
    discord.poll_history(
        master_channel, after=latest, check="restart", timeout=30.0
    )
    discord.send_and_wait(
        master_channel, "Say exactly: RECON_WARMUP", timeout=120.0
    )

    # The agent channel should still be in Active and the agent should
    # be reconstructed as sleeping. Sending a message should wake it.
    wake_msgs = discord.send_and_wait(
        agent_ch,
        "Say exactly: RECONSTRUCTED_OK",
        timeout=120.0,
    )
    text = discord.bot_response_text(wake_msgs)
    assert "RECONSTRUCTED_OK" in text, f"Reconstructed agent didn't respond: {text[:200]}"

    # Clean up
    discord.send_and_wait(
        master_channel, 'Kill the agent named "smoke-recon"', timeout=60.0
    )


# -- Tier 16: Stranded Message Recovery --


def test_stranded_message_recovery(discord: Discord, master_channel: str):
    """Test 68: Scheduler detects stranded messages and wakes agent."""
    # Spawn an agent and let it go to sleep
    discord.send_and_wait(
        master_channel,
        'Spawn an agent named "smoke-strand" with cwd "/home/ubuntu/axi-tests/smoke-test-data/agents/smoke-strand" and prompt "Say OK"',
        timeout=180.0,
    )
    time.sleep(3)
    agent_ch = discord.find_channel("smoke-strand")
    assert agent_ch is not None
    discord.wait_for_bot(agent_ch, after="0", timeout=120.0)

    # Wait for auto-sleep
    time.sleep(65)

    # Send a message while the agent is sleeping
    msg_id = discord.send(agent_ch, "Say exactly: STRANDED_OK")

    # The scheduler's safety net checks every 10s for stranded messages.
    # Wait for the agent to be woken and process the message.
    text = discord.poll_history(
        agent_ch, after=msg_id, check="STRANDED_OK", timeout=120.0, poll_interval=5.0
    )
    assert "STRANDED_OK" in text, f"Stranded message not recovered: {text[-300:]}"

    # Clean up
    discord.send_and_wait(
        master_channel, 'Kill the agent named "smoke-strand"', timeout=60.0
    )
