"""Live regression tests for axi-master latest-wins queue ordering."""

from __future__ import annotations

import time

import pytest

if False:  # pragma: no cover
    from .helpers import Discord


@pytest.mark.slow
def test_master_skip_prefers_latest_message(discord: 'Discord', master_channel: str) -> None:
    """axi-master should keep only the latest queued normal message while busy."""
    start = discord.latest_message_id(master_channel) or "0"

    long_id = discord.send(
        master_channel,
        "Think for a while before answering. Do not answer immediately. When you do answer, include SKIP_LONG_RUNNING_MARKER.",
    )
    time.sleep(2)

    stale_id = discord.send(master_channel, "Say exactly: SKIP_STALE_FIFO_OLDER")
    time.sleep(0.5)
    fresh_id = discord.send(master_channel, "Say exactly: SKIP_STALE_FIFO_NEWER")
    time.sleep(0.5)
    skip_id = discord.send(master_channel, "/skip")

    text = discord.poll_history(
        master_channel,
        after=start,
        check="SKIP_STALE_FIFO_NEWER",
        timeout=180.0,
        poll_interval=3.0,
    )
    assert "SKIP_STALE_FIFO_NEWER" in text, text[-800:]
    assert "SKIP_STALE_FIFO_OLDER" not in text, text[-800:]

    latest = discord.latest_message_id(master_channel) or skip_id or fresh_id or stale_id or long_id
    discord.wait_for_bot(master_channel, after=latest, timeout=30.0, sentinel=False)


@pytest.mark.slow
def test_master_rapid_fire_busy_messages_keep_only_latest(discord: 'Discord', master_channel: str) -> None:
    """Rapid-fire busy messages should collapse to the latest user intent on axi-master."""
    start = discord.latest_message_id(master_channel) or "0"

    long_id = discord.send(
        master_channel,
        "Think for a while before answering. Do not answer immediately. When you do answer, include RAPID_FIRE_LONG_MARKER.",
    )
    time.sleep(2)

    markers = ["RAPID_ONE", "RAPID_TWO", "RAPID_THREE", "RAPID_FOUR"]
    ids = []
    for marker in markers:
        ids.append(discord.send(master_channel, f"Say exactly: {marker}"))
        time.sleep(0.3)

    text = discord.poll_history(
        master_channel,
        after=start,
        check="RAPID_FOUR",
        timeout=180.0,
        poll_interval=3.0,
    )
    assert "RAPID_FOUR" in text, text[-1000:]
    for stale in markers[:-1]:
        assert stale not in text, text[-1000:]

    latest = discord.latest_message_id(master_channel) or ids[-1] or long_id
    discord.wait_for_bot(master_channel, after=latest, timeout=30.0, sentinel=False)
