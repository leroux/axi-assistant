"""Discord voice channel integration for streaming audio.

Manages a single voice connection per guild, streaming from an Icecast
HTTP endpoint (e.g. Dynamic Radio) via FFmpegPCMAudio.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import discord

from axi.config import AXI_USER_DATA

log = logging.getLogger(__name__)

# Icecast stream URL — defaults to local MP3 mount.
STREAM_URL = os.environ.get("ICECAST_STREAM_URL", "http://localhost:8000/dynamicradio.mp3")


# ---------------------------------------------------------------------------
# State persistence (voice_state.json)
# ---------------------------------------------------------------------------


def _state_path() -> str:
    return os.path.join(AXI_USER_DATA, "voice_state.json")


def _save_state(guild_id: int, channel_id: int) -> None:
    """Persist guild→channel mapping so we can rejoin after restart."""
    path = _state_path()
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data[str(guild_id)] = channel_id
    with open(path, "w") as f:
        json.dump(data, f)


def _clear_state(guild_id: int) -> None:
    """Remove a guild from the persisted voice state."""
    path = _state_path()
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return
    data.pop(str(guild_id), None)
    with open(path, "w") as f:
        json.dump(data, f)


def get_saved_channels() -> dict[int, int]:
    """Return {guild_id: channel_id} from the persisted state file."""
    path = _state_path()
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return {int(k): v for k, v in data.items()}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def join(channel: discord.VoiceChannel | discord.StageChannel) -> discord.VoiceClient:
    """Join a voice channel and start streaming from STREAM_URL.

    If already connected to a different channel in the same guild,
    moves to the new channel. If already in the target channel,
    restarts the stream.
    """
    guild = channel.guild
    vc = guild.voice_client

    if vc is not None:
        if vc.channel.id != channel.id:
            await vc.move_to(channel)
            log.info("Moved to #%s in %s", channel.name, guild.name)
        else:
            log.info("Already in #%s, restarting stream", channel.name)
    else:
        vc = await channel.connect()
        log.info("Connected to #%s in %s", channel.name, guild.name)

    _play_stream(vc)
    _save_state(guild.id, channel.id)
    return vc


async def leave(guild: discord.Guild) -> bool:
    """Leave the voice channel in a guild. Returns True if was connected."""
    vc = guild.voice_client
    if vc is None:
        return False
    await vc.disconnect()
    log.info("Disconnected from voice in %s", guild.name)
    _clear_state(guild.id)
    return True


def is_connected(guild: discord.Guild) -> bool:
    """Check if the bot is connected to voice in a guild."""
    return guild.voice_client is not None and guild.voice_client.is_connected()


# ---------------------------------------------------------------------------
# Stream playback and auto-recovery
# ---------------------------------------------------------------------------


def _play_stream(vc: discord.VoiceClient) -> None:
    """Start or restart the Icecast stream on a voice client."""
    if vc.is_playing():
        vc.stop()

    source = discord.FFmpegPCMAudio(
        STREAM_URL,
        before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    )
    vc.play(source, after=lambda e: _on_playback_end(vc, e))
    log.info("Streaming from %s", STREAM_URL)


def _on_playback_end(vc: discord.VoiceClient, error: Exception | None) -> None:
    """Handle stream end — schedule retry if still connected.

    Runs on discord.py's audio thread, so we use run_coroutine_threadsafe
    to schedule the async retry on the event loop.
    """
    if error:
        log.error("Stream playback error: %s", error)
    else:
        log.warning("Stream ended without error (FFmpeg exited cleanly but we didn't request stop)")

    if vc.is_connected():
        log.info("Still connected to VC — scheduling stream retry")
        asyncio.run_coroutine_threadsafe(_retry_stream(vc), vc.loop)


async def _retry_stream(vc: discord.VoiceClient, max_retries: int = 10) -> None:
    """Retry connecting to the stream with exponential backoff.

    Starts at 5s, doubles each attempt, caps at 60s.
    10 retries covers ~10 minutes total.
    """
    delay = 5.0
    for attempt in range(1, max_retries + 1):
        if not vc.is_connected():
            log.info("VC disconnected — stopping stream retry")
            return

        log.info("Stream retry attempt %d/%d (waiting %.0fs)", attempt, max_retries, delay)
        await asyncio.sleep(delay)

        if not vc.is_connected():
            log.info("VC disconnected during backoff — stopping stream retry")
            return

        try:
            _play_stream(vc)
            log.info("Stream recovered on attempt %d", attempt)
            return
        except Exception:
            log.warning("Stream retry attempt %d failed", attempt, exc_info=True)

        delay = min(delay * 2, 60.0)

    log.error("Stream recovery failed after %d attempts — giving up", max_retries)
