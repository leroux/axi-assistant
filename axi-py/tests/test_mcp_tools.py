"""MCP tool tests — Tier 11: discord_send_file, list_channels, read_messages, send_message."""

import time

import pytest

from .helpers import Discord
from .llm_judge import llm_assert


def test_discord_send_file(discord: Discord, master_channel: str):
    """Test 55: Agent can create a file and send it as Discord attachment."""
    msgs = discord.send_and_wait(
        master_channel,
        'Create a small text file called test.txt with content "hello from smoke test" in your cwd, then send it to this channel using the discord_send_file MCP tool.',
        timeout=120.0,
    )
    # Check for attachment in response messages
    # Also check recent history since the file may be a separate message
    time.sleep(3)
    recent = discord.history(master_channel, limit=10)
    has_file = discord.has_attachment(recent)

    text = discord.bot_response_text(msgs)
    passed, reason = llm_assert(
        text,
        "The response indicates the file was sent successfully or the agent attempted to send a file attachment",
        context="Asked agent to create and send a file via discord_send_file",
    )
    assert passed or has_file, f"File not sent: {reason}"


def test_discord_list_channels(discord: Discord, master_channel: str):
    """Test 56: Master agent can list Discord channels."""
    msgs = discord.send_and_wait(
        master_channel,
        "Use the discord_list_channels MCP tool to list all channels in this server. Show me the results.",
        timeout=60.0,
    )
    text = discord.bot_response_text(msgs)

    # Should contain channel names we know exist
    passed, reason = llm_assert(
        text,
        "The response contains a list of Discord channel names, including 'axi-master' and 'readme'",
        context="Asked master to list channels via MCP tool",
    )
    assert passed, f"Channel list not shown: {reason}"


def test_discord_read_messages(discord: Discord, master_channel: str):
    """Test 57: Master agent can read messages from a channel."""
    # Send a marker message first
    marker = "READ_TEST_MARKER_4821"
    discord.send(master_channel, f"Remember this marker: {marker}")
    time.sleep(2)

    msgs = discord.send_and_wait(
        master_channel,
        f"Use the discord_read_messages MCP tool to read the last 5 messages from this channel. Do you see the text '{marker}'?",
        timeout=60.0,
    )
    text = discord.bot_response_text(msgs)

    passed, reason = llm_assert(
        text,
        f"The response indicates the agent successfully read messages and found the marker text '{marker}'",
        context="Asked master to read messages via MCP tool",
    )
    assert passed, f"Messages not read: {reason}"


def test_discord_send_message(discord: Discord, master_channel: str):
    """Test 58: Master agent can send a message to another channel."""
    # Find the readme channel as target
    readme_ch = discord.find_channel("readme")
    if not readme_ch:
        pytest.skip("No #readme channel to send to")

    marker = "SEND_TEST_5193"
    msgs = discord.send_and_wait(
        master_channel,
        f'Use the discord_send_message MCP tool to send the text "{marker}" to the #readme channel.',
        timeout=60.0,
    )
    text = discord.bot_response_text(msgs)

    passed, reason = llm_assert(
        text,
        "The response confirms the message was sent to the target channel",
        context="Asked master to send message to #readme via MCP tool",
    )
    assert passed, f"Message send not confirmed: {reason}"

    # Verify the message appeared in readme
    time.sleep(3)
    recent = discord.history(readme_ch, limit=5)
    readme_text = "\n".join(m.get("content", "") for m in recent)
    assert marker in readme_text, f"Message not found in #readme: {readme_text[:200]}"
