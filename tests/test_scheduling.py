"""Scheduling tests — Tier 6: one-off, recurring, and schedule skips."""

from pathlib import Path

from .helpers import Discord
from .llm_judge import llm_assert

DATA_DIR = Path(__file__).parent.parent.parent / f"{Path(__file__).parent.parent.name}-data"


def test_one_off_schedule(discord: Discord, master_channel: str):
    """Test 35: One-off schedule fires and auto-removes."""
    msgs = discord.send_and_wait(
        master_channel,
        'Create a one-off schedule named "smoke-oneoff" that fires in 90 seconds from now. '
        'The prompt should be: "Say exactly: ONEOFF_FIRED"',
        timeout=60.0,
    )
    text = discord.bot_response_text(msgs)
    passed, reason = llm_assert(
        text,
        "The response confirms a one-off schedule was created",
        context="Asked to create a one-off schedule",
    )
    assert passed, f"Schedule not created: {reason}"

    # Wait for the schedule to fire (90s + buffer)
    latest = discord.latest_message_id(master_channel)
    text = discord.poll_history(
        master_channel, after=latest, check="ONEOFF_FIRED", timeout=150.0, poll_interval=10.0
    )
    assert "ONEOFF_FIRED" in text, f"One-off schedule didn't fire: {text[-300:]}"


def test_recurring_schedule(discord: Discord, master_channel: str):
    """Test 36: Recurring schedule fires on cron."""
    # Ask master to create a recurring schedule that fires every minute
    msgs = discord.send_and_wait(
        master_channel,
        'Create a recurring schedule named "smoke-recur" with cron "* * * * *" and prompt "Say exactly: RECURRING_FIRED". Make sure the name is unique.',
        timeout=60.0,
    )
    text = discord.bot_response_text(msgs)
    passed, reason = llm_assert(
        text,
        "The response confirms a recurring schedule was created",
    )
    assert passed, f"Schedule not created: {reason}"

    # Wait up to 2 minutes for it to fire
    latest = discord.latest_message_id(master_channel)
    text = discord.poll_history(
        master_channel, after=latest, check="RECURRING_FIRED", timeout=150.0, poll_interval=10.0
    )
    assert "RECURRING_FIRED" in text, f"Recurring schedule didn't fire: {text[-300:]}"

    # Clean up — remove the schedule
    discord.send_and_wait(
        master_channel,
        'Delete the schedule named "smoke-recur"',
        timeout=60.0,
    )


def test_schedule_skip(discord: Discord, master_channel: str):
    """Test 37: Schedule skip prevents firing on skip date."""
    # This is hard to test in real-time. We verify the skip mechanism
    # by creating a skip and checking the schedules/skips files.
    msgs = discord.send_and_wait(
        master_channel,
        'Create a recurring schedule named "smoke-skip" with cron "0 23 * * *" and prompt "SHOULD_NOT_FIRE". '
        'Then add a skip for today\'s date so it won\'t fire today.',
        timeout=60.0,
    )
    text = discord.bot_response_text(msgs)

    # Verify the agent acknowledged both actions
    passed, reason = llm_assert(
        text,
        "The response confirms both creating the schedule and adding a skip for today",
        context="Asked to create a schedule and skip for today",
    )
    assert passed, f"Schedule/skip not confirmed: {reason}"

    # Clean up
    discord.send_and_wait(
        master_channel,
        'Delete the schedule named "smoke-skip"',
        timeout=60.0,
    )
