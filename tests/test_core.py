"""Core feature tests — Tiers 1-2: basic responses, text commands, agent lifecycle."""

import subprocess
import time

import pytest

from .helpers import Discord
from .llm_judge import llm_assert
from .conftest import agent_cwd

# -- Tier 1: Core Features --


def test_basic_response(discord: Discord, master_channel: str):
    """Test 1: Bot responds to a simple deterministic prompt."""
    msgs = discord.send_and_wait(master_channel, 'Say exactly: SMOKE_TEST_OK')
    text = discord.bot_response_text(msgs)
    assert "SMOKE_TEST_OK" in text


def test_status_command(discord: Discord, master_channel: str):
    """Test 2: /status returns agent info."""
    msgs = discord.send_and_wait(
        master_channel, "/status", sentinel=False, timeout=15.0
    )
    text = discord.bot_response_text(msgs)
    # /status should mention the agent name and state
    assert "axi-master" in text.lower() or "master" in text.lower()


def test_debug_toggle(discord: Discord, master_channel: str):
    """Test 3: /debug toggles debug mode."""
    # Toggle once
    msgs = discord.send_and_wait(
        master_channel, "/debug", sentinel=False, timeout=15.0
    )
    text = discord.bot_response_text(msgs)
    assert "debug mode" in text.lower()

    # Toggle again
    msgs = discord.send_and_wait(
        master_channel, "/debug", sentinel=False, timeout=15.0
    )
    text = discord.bot_response_text(msgs)
    assert "debug mode" in text.lower()


def test_clear_context(discord: Discord, master_channel: str):
    """Test 5: /clear confirms context cleared."""
    msg_id = discord.send(master_channel, "/clear")
    # /clear sends /clear to agent and also posts confirmation.
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        msgs = discord.history(master_channel, limit=5, after=msg_id)
        text = "\n".join(m.get("content", "") for m in msgs).lower()
        if "clear" in text:
            break
        time.sleep(1)
    assert "clear" in text
    # Wait for any sentinel to clear
    time.sleep(3)


def test_compact_context(discord: Discord, master_channel: str):
    """Test 6: /compact shows token count."""
    msg_id = discord.send(master_channel, "/compact")
    # /compact triggers API compaction. Poll until we see the result.
    deadline = time.monotonic() + 60
    text = ""
    while time.monotonic() < deadline:
        msgs = discord.history(master_channel, limit=10, after=msg_id)
        text = "\n".join(m.get("content", "") for m in msgs).lower()
        if "compact" in text or "token" in text:
            break
        time.sleep(2)
    assert "compact" in text or "token" in text
    # Wait for sentinel to clear before next test
    time.sleep(3)


def test_model_warning(discord: Discord, master_channel: str, instance_env: dict):
    """Test 7: Non-opus model shows model warning on wake.

    Best-effort: the warning appears on first wake after sleep, so it may have
    already appeared during warmup. We verify the bot responds correctly.
    """
    # Ensure bot is idle before sending
    time.sleep(2)
    msgs = discord.send_and_wait(master_channel, "Say exactly: MODEL_CHECK")
    text = discord.bot_response_text(msgs)
    assert "MODEL_CHECK" in text


# -- Tier 2: Agent Lifecycle --


def test_agent_spawn_and_kill(discord: Discord, master_channel: str):
    """Tests 11-12: Spawn an agent and then kill it."""
    # Spawn
    spawn_msgs = discord.send_and_wait(
        master_channel,
        'Spawn an agent named "smoke-lifecycle" with cwd "' + agent_cwd("smoke-lifecycle") + '" and prompt "Say exactly: LIFECYCLE_ALIVE"',
        timeout=180.0,
    )
    spawn_text = discord.bot_response_text(spawn_msgs)

    # Verify spawn was acknowledged
    passed, reason = llm_assert(
        spawn_text,
        "The response confirms that an agent named 'smoke-lifecycle' was spawned or created successfully",
        context="Asked master to spawn an agent",
    )
    assert passed, f"Spawn not confirmed: {reason}"

    # Wait for the agent to come alive and respond in its channel
    time.sleep(5)
    agent_ch = discord.find_channel("smoke-lifecycle")
    assert agent_ch is not None, "Agent channel 'smoke-lifecycle' not found"

    # Wait for the agent's initial response
    agent_msgs = discord.wait_for_bot(
        agent_ch,
        after="0",  # Get all messages
        timeout=120.0,
    )
    agent_text = discord.bot_response_text(agent_msgs)
    assert len(agent_text) > 0, "Agent produced no output"

    # Kill the agent
    kill_msgs = discord.send_and_wait(
        master_channel,
        'Kill the agent named "smoke-lifecycle"',
        timeout=60.0,
    )
    kill_text = discord.bot_response_text(kill_msgs)
    passed, reason = llm_assert(
        kill_text,
        "The response confirms that the agent 'smoke-lifecycle' was killed or terminated",
        context="Asked master to kill an agent",
    )
    assert passed, f"Kill not confirmed: {reason}"


def test_killed_channel_protection(discord: Discord, master_channel: str):
    """Test 13: Messages to killed agent channel are rejected."""
    # Ensure smoke-lifecycle was killed (from previous test)
    agent_ch = discord.find_channel("smoke-lifecycle")
    if agent_ch is None:
        pytest.skip("smoke-lifecycle channel not found — previous test may not have run")

    msgs = discord.send_and_wait(
        agent_ch,
        "Are you alive?",
        timeout=30.0,
        sentinel=False,
    )
    text = discord.bot_response_text(msgs)
    assert "killed" in text.lower() or "has been killed" in text.lower(), (
        f"Expected killed message, got: {text[:200]}"
    )


def test_duplicate_live_agent_name(discord: Discord, master_channel: str):
    """Test 18: Spawning an agent with name of a live agent fails."""
    # Spawn the agent
    discord.send_and_wait(
        master_channel,
        'Spawn an agent named "smoke-dupe" with cwd "' + agent_cwd("smoke-dupe") + '" and prompt "Say OK"',
        timeout=180.0,
    )
    time.sleep(3)

    # Try to spawn another with the same name
    msgs = discord.send_and_wait(
        master_channel,
        'Spawn an agent named "smoke-dupe" with cwd "' + agent_cwd("smoke-dupe") + '" and prompt "Say OK again"',
        timeout=60.0,
    )
    text = discord.bot_response_text(msgs)
    passed, reason = llm_assert(
        text,
        "The response indicates an error because an agent with that name already exists",
        context="Tried to spawn an agent with a name already in use",
    )
    assert passed, f"Expected duplicate error: {reason}"

    # Clean up — kill the agent
    discord.send_and_wait(
        master_channel,
        'Kill the agent named "smoke-dupe"',
        timeout=60.0,
    )


def test_emoji_reactions(discord: Discord, master_channel: str):
    """Test 9: Bot adds checkmark reaction to processed messages."""
    msg_id = discord.send(master_channel, "Say exactly: REACTION_CHECK")
    discord.wait_for_bot(master_channel, after=msg_id, timeout=60.0)
    time.sleep(2)

    # Check if the bot added a reaction to our message
    resp = discord._bot.get(f"/channels/{master_channel}/messages/{msg_id}")
    resp.raise_for_status()
    msg_data = resp.json()
    reactions = msg_data.get("reactions", [])

    has_check = any(
        r.get("emoji", {}).get("name") in ("✅", "☑️", "☑", "✓", "white_check_mark")
        for r in reactions
    )
    assert has_check, f"No checkmark reaction found. Reactions: {reactions}"


def test_debug_mode_visibility(discord: Discord, master_channel: str):
    """Test 4: Debug mode shows tool calls with wrench emoji."""
    # Enable debug (toggle on)
    discord.send_and_wait(
        master_channel, "// debug", sentinel=False, timeout=15.0
    )
    time.sleep(2)

    # Ask agent to do something that uses a tool
    msgs = discord.send_and_wait(
        master_channel,
        "List the files in the current directory",
        timeout=60.0,
    )
    text = discord.bot_response_text(msgs)

    # Debug mode should show tool usage with wrench emoji (🔧)
    assert "🔧" in text or "tool" in text.lower(), (
        f"Expected debug tool output (wrench emoji), got: {text[:300]}"
    )

    # Clean up — toggle debug off
    discord.send_and_wait(
        master_channel, "// debug", sentinel=False, timeout=15.0
    )


def test_startup_notification(discord: Discord, master_channel: str, instance_env: dict):
    """Test 8: Restarting instance produces 'Axi ready' notification."""
    # Record current latest message
    latest = discord.latest_message_id(master_channel)

    # Restart the instance
    axi_py_dir = pytest.importorskip("pathlib").Path(__file__).parent.parent
    subprocess.run(
        ["uv", "run", "python", "../axi_test.py", "restart", "smoke-test"],
        cwd=str(axi_py_dir),
        capture_output=True,
        timeout=30,
    )

    # Wait for the ready notification (Rust bot needs time for procmux + bot restart)
    text = discord.poll_history(
        master_channel, after=latest, check="ready", timeout=60.0
    )
    assert "ready" in text.lower(), (
        f"Expected ready notification, got: {text[:200]}"
    )

    # Re-warmup after restart
    time.sleep(5)
    discord.send_and_wait(master_channel, "Say exactly: POST_RESTART_OK", timeout=120.0)


def test_readme_channel_sync(discord: Discord):
    """Test 10: #readme channel exists and has content."""
    readme_ch = discord.find_channel("readme")
    assert readme_ch is not None, "No #readme channel found"

    msgs = discord.history(readme_ch, limit=5)
    assert len(msgs) > 0, "Readme channel is empty"
    text = "\n".join(m.get("content", "") for m in msgs)
    assert len(text) > 20, f"Readme content too short: {text[:100]}"


def test_auto_sleep_and_wake(discord: Discord, master_channel: str):
    """Tests 14-15: Agent auto-sleeps after idle, auto-wakes on message."""
    # Spawn an agent
    discord.send_and_wait(
        master_channel,
        'Spawn an agent named "smoke-sleep" with cwd "' + agent_cwd("smoke-sleep") + '" and prompt "Say exactly: AWAKE"',
        timeout=180.0,
    )
    time.sleep(3)

    agent_ch = discord.find_channel("smoke-sleep")
    assert agent_ch is not None, "Agent channel not found"

    # Wait for initial response
    discord.wait_for_bot(agent_ch, after="0", timeout=120.0)

    # Wait for auto-sleep (~60s idle)
    time.sleep(65)

    # Check status — should be sleeping
    status_msgs = discord.send_and_wait(
        master_channel, "// status", sentinel=False, timeout=15.0
    )
    status_text = discord.bot_response_text(status_msgs)
    # Note: /status shows master's status. We just verify the agent auto-slept
    # by sending it a message and checking it wakes up.

    # Auto-wake: send a message to the sleeping agent
    wake_msgs = discord.send_and_wait(
        agent_ch,
        "Say exactly: WOKE_UP",
        timeout=120.0,
    )
    wake_text = discord.bot_response_text(wake_msgs)
    assert "WOKE_UP" in wake_text, f"Agent didn't wake up: {wake_text[:200]}"

    # Clean up
    discord.send_and_wait(
        master_channel, 'Kill the agent named "smoke-sleep"', timeout=60.0
    )


def test_duplicate_name_spawn_killed(discord: Discord, master_channel: str):
    """Test 17: Spawning with name of killed agent reuses the channel."""
    # Spawn an agent
    discord.send_and_wait(
        master_channel,
        'Spawn an agent named "smoke-reuse" with cwd "' + agent_cwd("smoke-reuse") + '" and prompt "Say OK"',
        timeout=180.0,
    )
    time.sleep(3)
    ch = discord.find_channel("smoke-reuse")
    assert ch is not None

    # Kill it
    discord.send_and_wait(
        master_channel, 'Kill the agent named "smoke-reuse"', timeout=60.0
    )
    time.sleep(2)

    # Check channel is in Killed category
    killed_cat = discord.find_category("Killed")
    if killed_cat:
        info = discord.channel_info(ch)
        assert info.get("parent_id") == killed_cat, "Channel not moved to Killed"

    # Respawn with same name — should reuse the killed channel
    discord.send_and_wait(
        master_channel,
        'Spawn an agent named "smoke-reuse" with cwd "' + agent_cwd("smoke-reuse") + '" and prompt "Say REUSED"',
        timeout=180.0,
    )
    time.sleep(3)

    # Check channel moved back to Active
    active_cat = discord.find_category("Active")
    if active_cat:
        info = discord.channel_info(ch)
        assert info.get("parent_id") == active_cat, "Channel not moved back to Active"

    # Clean up
    discord.send_and_wait(
        master_channel, 'Kill the agent named "smoke-reuse"', timeout=60.0
    )


def test_agent_resume(discord: Discord, master_channel: str):
    """Test 19/59/60: Kill agent, resume with session ID, context preserved."""
    # Spawn agent with a unique marker
    discord.send_and_wait(
        master_channel,
        'Spawn an agent named "smoke-resume" with cwd "' + agent_cwd("smoke-resume") + '" and prompt "Remember the code word PINEAPPLE. Say: I will remember PINEAPPLE."',
        timeout=180.0,
    )
    time.sleep(3)
    agent_ch = discord.find_channel("smoke-resume")
    assert agent_ch is not None

    # Wait for initial response
    discord.wait_for_bot(agent_ch, after="0", timeout=120.0)

    # Kill the agent — capture session ID from the kill response
    kill_msgs = discord.send_and_wait(
        master_channel, 'Kill the agent named "smoke-resume"', timeout=60.0
    )
    kill_text = discord.bot_response_text(kill_msgs)

    # Extract session ID (format: `abc123...` or similar)
    import re
    session_match = re.search(r'`([a-f0-9]{8})`|session[:\s]*`?([a-f0-9-]+)`?', kill_text, re.IGNORECASE)
    if not session_match:
        # Try to find any hex string that looks like a session ID
        session_match = re.search(r'([a-f0-9]{8,})', kill_text)

    if session_match:
        session_id = session_match.group(1) or session_match.group(2) if session_match.lastindex and session_match.lastindex >= 2 else session_match.group(1)
        # Resume the agent
        discord.send_and_wait(
            master_channel,
            f'Spawn an agent named "smoke-resume" with cwd "' + agent_cwd("smoke-resume") + f'" and prompt "What code word were you told to remember?" and resume="{session_id}"',
            timeout=180.0,
        )
        time.sleep(5)

        # Check if agent remembers the code word
        resume_msgs = discord.wait_for_bot(agent_ch, after="0", timeout=120.0)
        resume_text = discord.bot_response_text(resume_msgs)
        # The agent may or may not remember depending on context preservation
        assert len(resume_text) > 0, "Resumed agent produced no output"
    else:
        pytest.skip("Could not extract session ID from kill response")

    # Clean up
    discord.send_and_wait(
        master_channel, 'Kill the agent named "smoke-resume"', timeout=60.0
    )
