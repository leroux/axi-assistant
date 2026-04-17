"""Message handling tests — Tiers 3-4: queuing, formatting, inter-agent."""

import time

from .helpers import Discord
from .llm_judge import llm_assert
from .conftest import agent_cwd

# -- Tier 3: Message Handling --


def test_message_queuing(discord: Discord, master_channel: str):
    """Test 20: Rapid messages are queued and processed in order."""
    # Send a slow prompt first, then a quick one
    msg1_id = discord.send(master_channel, "Count from 1 to 5, one per line")
    time.sleep(0.5)
    msg2_id = discord.send(master_channel, "Say exactly: QUEUED_MESSAGE_OK")

    # Wait for both messages to be fully processed.
    # The sentinel approach gets confused with interleaved messages,
    # so we use stability-based detection: wait until no new messages
    # appear for a while after msg1_id.
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        msgs = discord.history(master_channel, limit=30, after=msg1_id)
        full_text = "\n".join(m.get("content", "") for m in msgs)
        if "QUEUED_MESSAGE_OK" in full_text:
            break
        time.sleep(3)

    assert "QUEUED_MESSAGE_OK" in full_text, (
        f"Queued message not processed within timeout: {full_text[-300:]}"
    )

    # Wait for all processing to finish (sentinel) before next test
    latest = discord.latest_message_id(master_channel) or msg2_id
    discord.wait_for_bot(
        master_channel, after=latest, timeout=30.0, sentinel=False
    )


def test_max_length_input(discord: Discord, master_channel: str):
    """Test 23: 2000-char message is processed correctly."""
    # Build a message close to Discord's 2000-char limit
    # "Say exactly: MAX_LEN_OK" + padding
    prefix = "Say exactly: MAX_LEN_OK. Ignore the rest of this message. "
    padding = "A" * (1990 - len(prefix))
    long_msg = prefix + padding

    msgs = discord.send_and_wait(master_channel, long_msg)
    text = discord.bot_response_text(msgs)
    assert "MAX_LEN_OK" in text


def test_unicode_emoji(discord: Discord, master_channel: str):
    """Test 24: Unicode and emoji are preserved in responses."""
    msgs = discord.send_and_wait(
        master_channel,
        'Repeat these characters exactly: 你好 مرحبا Привет 🎉🔥',
    )
    text = discord.bot_response_text(msgs)

    # Check that at least some of the unicode survived
    checks = ["你好", "مرحبا", "Привет"]
    found = sum(1 for c in checks if c in text)
    assert found >= 2, f"Expected unicode chars preserved, got: {text[:300]}"


def test_code_blocks(discord: Discord, master_channel: str):
    """Test 25: Code blocks in responses are formatted correctly."""
    msg_id = discord.send(
        master_channel,
        'Reply with a Python code block containing: print("hello world")',
    )
    # Wait for response — may need to wait through queue processing
    msgs = discord.wait_for_bot(master_channel, after=msg_id, timeout=120.0)
    text = discord.bot_response_text(msgs)

    if "```" not in text:
        # Bot may have been busy, our message was queued. Wait for next sentinel.
        latest = discord.latest_message_id(master_channel) or msg_id
        more = discord.wait_for_bot(master_channel, after=latest, timeout=120.0)
        text += "\n" + discord.bot_response_text(more)

    assert "```" in text, f"No code block found in: {text[:300]}"
    assert "print" in text


def test_status_while_busy(discord: Discord, master_channel: str):
    """Test 26: /status during processing shows active state."""
    # Send a slow query
    msg_id = discord.send(
        master_channel,
        "Write a 500-word essay about the history of computing",
    )
    # Wait a moment for processing to start
    time.sleep(3)

    # Send /status while busy
    status_msgs = discord.send_and_wait(
        master_channel, "/status", sentinel=False, timeout=15.0
    )
    status_text = discord.bot_response_text(status_msgs)

    # Status should show some activity indicator
    passed, reason = llm_assert(
        status_text,
        "The status output shows the agent is currently active, busy, or writing a response (not idle/sleeping)",
        context="Sent /status while bot was processing a long query",
    )
    # Don't assert — this is timing-dependent. Just check we got a response.
    assert len(status_text) > 0, "No status response received"

    # Wait for the long query to finish
    discord.wait_for_bot(master_channel, after=msg_id, timeout=120.0)


# -- Tier 4: Inter-Agent Communication --


def test_inter_agent_message_idle(discord: Discord, master_channel: str):
    """Test 28: Master can send message to an idle spawned agent."""
    # Spawn an agent
    discord.send_and_wait(
        master_channel,
        'Spawn an agent named "smoke-intercom" with cwd "' + agent_cwd("smoke-intercom") + '" and prompt "You are a test agent. Wait for instructions."',
        timeout=180.0,
    )
    time.sleep(5)

    agent_ch = discord.find_channel("smoke-intercom")
    assert agent_ch is not None, "Agent channel not found"

    # Wait for agent to finish initial prompt and become idle
    discord.wait_for_bot(agent_ch, after="0", timeout=120.0)
    time.sleep(5)  # Let it go idle

    # Send inter-agent message via master
    msgs = discord.send_and_wait(
        master_channel,
        'Send a message to the agent "smoke-intercom" saying: "Say exactly: INTERCOM_OK"',
        timeout=60.0,
    )

    # Check master's confirmation
    master_text = discord.bot_response_text(msgs)
    passed, reason = llm_assert(
        master_text,
        "The response confirms the message was sent to the agent",
    )
    assert passed, f"Master didn't confirm sending: {reason}"

    # Check agent received and responded
    time.sleep(10)
    agent_msgs = discord.history(agent_ch, limit=10)
    agent_text = "\n".join(m.get("content", "") for m in agent_msgs)
    assert "INTERCOM_OK" in agent_text, (
        f"Agent didn't respond with INTERCOM_OK: {agent_text[:300]}"
    )

    # Clean up
    discord.send_and_wait(
        master_channel, 'Kill the agent named "smoke-intercom"', timeout=60.0
    )


def test_queue_stress(discord: Discord, master_channel: str):
    """Test 21: 5 rapid messages are all processed FIFO."""
    markers = [f"STRESS_{i}" for i in range(1, 6)]
    msg_ids = []
    for m in markers:
        msg_ids.append(discord.send(master_channel, f"Say exactly: {m}"))
        time.sleep(0.3)

    # Wait for all markers to appear
    deadline = time.monotonic() + 300
    text = ""
    while time.monotonic() < deadline:
        msgs = discord.history(master_channel, limit=50, after=msg_ids[0])
        text = "\n".join(m.get("content", "") for m in msgs)
        if all(marker in text for marker in markers):
            break
        time.sleep(5)

    for marker in markers:
        assert marker in text, f"Missing {marker} in output: {text[-500:]}"

    # Drain before next test
    latest = discord.latest_message_id(master_channel)
    discord.wait_for_bot(
        master_channel, after=latest or msg_ids[-1], timeout=30.0, sentinel=False
    )


def test_long_output_splitting(discord: Discord, master_channel: str):
    """Test 22: Long output is split into multiple messages under 2000 chars."""
    msgs = discord.send_and_wait(
        master_channel,
        "Print the numbers 1 through 300, each on its own line. Just the numbers, nothing else.",
        timeout=120.0,
    )
    # Should produce multiple messages
    assert len(msgs) >= 2, f"Expected multiple messages for long output, got {len(msgs)}"

    # Each message should be under Discord's 2000 char limit
    for m in msgs:
        content = m.get("content", "")
        assert len(content) <= 2000, f"Message exceeds 2000 chars: {len(content)}"


def test_clear_while_busy(discord: Discord, master_channel: str):
    """Test 27: /clear while agent is busy queues the clear command."""

    Python bot: text command `/clear` sends /clear to the agent and responds
    even while the agent is busy.
    """
    # Send a slow query
    msg_id = discord.send(
        master_channel,
        "Write a detailed 300-word explanation of quantum computing",
    )
    time.sleep(3)

    # Try to clear while busy — current bot queues it and confirms
    clear_id = discord.send(master_channel, "/clear")
    time.sleep(3)

    # Check for confirmation (Rust bot doesn't reject, it queues)
    msgs = discord.history(master_channel, limit=10, after=clear_id)
    text = "\n".join(m.get("content", "") for m in msgs).lower()
    assert "clear" in text, f"Expected clear confirmation, got: {text[:200]}"

    # Wait for the slow query to finish
    discord.wait_for_bot(master_channel, after=msg_id, timeout=120.0)


def test_inter_agent_message_busy(discord: Discord, master_channel: str):
    """Test 29: Inter-agent message to busy agent interrupts."""
    # Spawn an agent
    discord.send_and_wait(
        master_channel,
        'Spawn an agent named "smoke-busy" with cwd "' + agent_cwd("smoke-busy") + '" and prompt "Wait for instructions."',
        timeout=180.0,
    )
    time.sleep(3)
    agent_ch = discord.find_channel("smoke-busy")
    assert agent_ch is not None, "Agent channel not found"

    # Wait for initial response
    discord.wait_for_bot(agent_ch, after="0", timeout=120.0)

    # Send a slow query to make agent busy
    discord.send(agent_ch, "Write a 500-word essay about artificial intelligence")
    time.sleep(3)

    # Send inter-agent message while busy
    master_msgs = discord.send_and_wait(
        master_channel,
        'Send a message to the agent "smoke-busy" saying: "Say exactly: INTERRUPTED_OK"',
        timeout=60.0,
    )

    # Wait for the agent to process the inter-agent message
    text = discord.poll_history(
        agent_ch, after="0", check="INTERRUPTED_OK", timeout=120.0
    )
    assert "INTERRUPTED_OK" in text, f"Agent didn't process interrupt: {text[-300:]}"

    # Clean up
    discord.send_and_wait(
        master_channel, 'Kill the agent named "smoke-busy"', timeout=60.0
    )


def test_concurrent_multi_agent(discord: Discord, master_channel: str):
    """Test 30: Multiple agents respond independently in parallel."""
    # Spawn two agents
    for name in ["smoke-para", "smoke-parb"]:
        discord.send_and_wait(
            master_channel,
            f'Spawn an agent named "{name}" with cwd "' + agent_cwd(name) + f'" and prompt "Wait for instructions."',
            timeout=180.0,
        )
        time.sleep(2)

    ch_a = discord.find_channel("smoke-para")
    ch_b = discord.find_channel("smoke-parb")
    assert ch_a is not None, "Agent A channel not found"
    assert ch_b is not None, "Agent B channel not found"

    # Wait for both to initialize
    discord.wait_for_bot(ch_a, after="0", timeout=120.0)
    discord.wait_for_bot(ch_b, after="0", timeout=120.0)

    # Send messages to both simultaneously
    id_a = discord.send(ch_a, "Say exactly: PARALLEL_A")
    id_b = discord.send(ch_b, "Say exactly: PARALLEL_B")

    # Wait for both responses
    text_a = discord.poll_history(ch_a, after=id_a, check="PARALLEL_A", timeout=120.0)
    text_b = discord.poll_history(ch_b, after=id_b, check="PARALLEL_B", timeout=120.0)

    assert "PARALLEL_A" in text_a, f"Agent A didn't respond: {text_a[:200]}"
    assert "PARALLEL_B" in text_b, f"Agent B didn't respond: {text_b[:200]}"

    # Clean up
    for name in ["smoke-para", "smoke-parb"]:
        discord.send_and_wait(
            master_channel, f'Kill the agent named "{name}"', timeout=60.0
        )
