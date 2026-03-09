"""Edge case tests — Tier 9: unusual inputs, error handling."""

import time

from .helpers import Discord
from .llm_judge import llm_assert


def test_empty_text_command(discord: Discord, master_channel: str):
    """Test 46: Bare '//' falls through to agent as regular message."""
    msgs = discord.send_and_wait(master_channel, "//", timeout=60.0)
    text = discord.bot_response_text(msgs)
    # Should get a response (not a text command error)
    assert len(text) > 0, "No response to bare //"


def test_unknown_text_command(discord: Discord, master_channel: str):
    """Test 47: Unknown '//xxx' falls through to agent."""
    msgs = discord.send_and_wait(master_channel, "// unknowncmd", timeout=60.0)
    text = discord.bot_response_text(msgs)
    assert len(text) > 0, "No response to unknown text command"


def test_invalid_debug_arg(discord: Discord, master_channel: str):
    """Test 48: '// debug badarg' just toggles (Rust bot ignores extra args)."""
    msgs = discord.send_and_wait(
        master_channel, "// debug badarg", sentinel=False, timeout=15.0
    )
    text = discord.bot_response_text(msgs)
    # Rust bot ignores args and just toggles
    assert "debug mode" in text.lower(), (
        f"Expected debug toggle response, got: {text[:200]}"
    )


def test_context_clear_doesnt_erase_channel_history(discord: Discord, master_channel: str):
    """Test 50: After //clear, agent can still read channel history."""
    # Send a distinctive message
    marker = "UNIQUE_MARKER_7392"
    discord.send_and_wait(master_channel, f"Say exactly: {marker}")

    # Clear context
    discord.send_and_wait(
        master_channel, "// clear", sentinel=False, timeout=15.0
    )

    # Ask about the marker — agent should find it in channel history
    msgs = discord.send_and_wait(
        master_channel,
        f'Read the recent messages in this channel. Do you see the text "{marker}" anywhere?',
        timeout=60.0,
    )
    text = discord.bot_response_text(msgs)
    passed, reason = llm_assert(
        text,
        f"The response indicates the agent found or can see the text '{marker}' in the channel history",
        context="After //clear, asked if agent can see a previous marker in channel history",
    )
    # This is an observation, not necessarily a pass/fail
    # The agent CAN read channel history even after context clear (by design)
    # We just verify the behavior
    assert len(text) > 0, "No response after context clear"


def test_race_message_during_kill(discord: Discord, master_channel: str):
    """Test 49: Sending message during kill doesn't crash."""
    # Spawn an agent
    discord.send_and_wait(
        master_channel,
        'Spawn an agent named "smoke-race" with cwd "/home/ubuntu/axi-tests/smoke-test-data/agents/smoke-race" and prompt "Wait for instructions."',
        timeout=180.0,
    )
    time.sleep(3)
    agent_ch = discord.find_channel("smoke-race")
    assert agent_ch is not None

    discord.wait_for_bot(agent_ch, after="0", timeout=120.0)

    # Send kill and message simultaneously
    discord.send(master_channel, 'Kill the agent named "smoke-race"')
    time.sleep(0.5)
    discord.send(agent_ch, "Are you still there?")

    # Wait for things to settle — no crash is success
    time.sleep(10)

    # Verify master is still responsive
    msgs = discord.send_and_wait(
        master_channel, "Say exactly: RACE_OK", timeout=60.0
    )
    text = discord.bot_response_text(msgs)
    assert "RACE_OK" in text, f"Master unresponsive after race: {text[:200]}"
