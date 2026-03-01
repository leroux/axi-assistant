"""Discord channel and guild management for the Axi bot.

Extracted from agents.py. Manages channel topic helpers, guild infrastructure,
and channel lifecycle. Agent-dict access is injected via init() to avoid
circular imports.

Pure functions: normalize_channel_name, format_channel_topic, parse_channel_topic.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

import discord
from discord import CategoryChannel, TextChannel

from axi import config

if TYPE_CHECKING:
    from discord.ext.commands import Bot

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state (populated via init and ensure_guild_infrastructure)
# ---------------------------------------------------------------------------

_bot: Bot | None = None
target_guild: discord.Guild | None = None
active_category: CategoryChannel | None = None
killed_category: CategoryChannel | None = None
bot_creating_channels: set[str] = set()

# Injected references (set by agents.init → channels.init)
_agents_dict: dict[str, Any] | None = None
_channel_to_agent: dict[int, str] | None = None
_send_to_exceptions: Any = None


def init(
    bot: Bot,
    agents_dict: dict[str, Any],
    channel_to_agent: dict[int, str],
    send_to_exceptions_fn: Any,
) -> None:
    """Inject dependencies. Called once from agents.init()."""
    global _bot, _agents_dict, _channel_to_agent, _send_to_exceptions
    _bot = bot
    _agents_dict = agents_dict
    _channel_to_agent = channel_to_agent
    _send_to_exceptions = send_to_exceptions_fn


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def normalize_channel_name(name: str) -> str:
    """Normalize an agent name to a valid Discord channel name."""
    name = name.lower().replace(" ", "-")
    name = re.sub(r"[^a-z0-9\-_]", "", name)
    return name[:100]


def format_channel_topic(
    cwd: str,
    session_id: str | None = None,
    prompt_hash: str | None = None,
    todo_msg: int | None = None,
    agent_type: str | None = None,
) -> str:
    """Format agent metadata for a Discord channel topic."""
    parts = [f"cwd: {cwd}"]
    if session_id:
        parts.append(f"session: {session_id}")
    if prompt_hash:
        parts.append(f"prompt_hash: {prompt_hash}")
    if todo_msg is not None:
        parts.append(f"todo_msg: {todo_msg}")
    if agent_type and agent_type != "flowcoder":
        parts.append(f"type: {agent_type}")
    return " | ".join(parts)


def parse_channel_topic(
    topic: str | None,
) -> tuple[str | None, str | None, str | None, int | None, str | None]:
    """Parse cwd, session_id, prompt_hash, todo_msg, and agent_type from a channel topic."""
    if not topic:
        return None, None, None, None, None
    cwd = None
    session_id = None
    prompt_hash = None
    todo_msg: int | None = None
    agent_type: str | None = None
    for part in topic.split("|"):
        key, _, value = part.strip().partition(": ")
        if key == "cwd":
            cwd = value.strip()
        elif key == "session":
            session_id = value.strip()
        elif key == "prompt_hash":
            prompt_hash = value.strip()
        elif key == "todo_msg":
            try:
                todo_msg = int(value.strip())
            except ValueError:
                pass
        elif key == "type":
            agent_type = value.strip()
    return cwd, session_id, prompt_hash, todo_msg, agent_type


# ---------------------------------------------------------------------------
# Guild infrastructure
# ---------------------------------------------------------------------------


def _build_category_overwrites(
    guild: discord.Guild,
) -> dict[discord.Object | discord.Member | discord.Role, discord.PermissionOverwrite]:
    """Build permission overwrites for Axi categories."""
    overwrites: dict[discord.Object | discord.Member | discord.Role, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(
            send_messages=False,
            add_reactions=False,
            create_public_threads=False,
            create_private_threads=False,
            send_messages_in_threads=False,
            view_channel=True,
            read_message_history=True,
        ),
        guild.me: discord.PermissionOverwrite(
            send_messages=True,
            add_reactions=True,
            manage_channels=True,
            manage_messages=True,
            manage_threads=True,
            create_public_threads=True,
            create_private_threads=True,
            send_messages_in_threads=True,
            view_channel=True,
            read_message_history=True,
        ),
    }
    for uid in config.ALLOWED_USER_IDS:
        overwrites[discord.Object(id=uid)] = discord.PermissionOverwrite(
            send_messages=True,
            add_reactions=True,
            create_public_threads=True,
            create_private_threads=True,
            send_messages_in_threads=True,
            view_channel=True,
            read_message_history=True,
        )
    return overwrites


async def ensure_guild_infrastructure() -> tuple[discord.Guild, CategoryChannel, CategoryChannel]:
    """Ensure the guild has Active and Killed categories. Called once during on_ready()."""
    global target_guild, active_category, killed_category
    assert _bot is not None

    guild = _bot.get_guild(config.DISCORD_GUILD_ID)
    if guild is None:
        guild = await _bot.fetch_guild(config.DISCORD_GUILD_ID)
    target_guild = guild

    overwrites = _build_category_overwrites(guild)

    active_cat = None
    killed_cat = None
    for cat in guild.categories:
        if cat.name == config.ACTIVE_CATEGORY_NAME:
            active_cat = cat
        elif cat.name == config.KILLED_CATEGORY_NAME:
            killed_cat = cat

    def _overwrites_match(
        existing: dict[discord.Role | discord.Member | discord.Object, discord.PermissionOverwrite],
        desired: dict[discord.Object | discord.Member | discord.Role, discord.PermissionOverwrite],
    ) -> bool:
        a = {getattr(k, "id", k): v for k, v in existing.items()}
        b = {getattr(k, "id", k): v for k, v in desired.items()}
        return a == b

    for name, cat in [
        (config.ACTIVE_CATEGORY_NAME, active_cat),
        (config.KILLED_CATEGORY_NAME, killed_cat),
    ]:
        if cat is None:
            cat = await guild.create_category(name, overwrites=overwrites)
            log.info("Created '%s' category", name)
        elif not _overwrites_match(cat.overwrites, overwrites):
            await cat.edit(overwrites=overwrites)
            log.info("Synced permissions on '%s' category", name)
        else:
            log.info("Permissions already current on '%s' category", name)
        if name == config.ACTIVE_CATEGORY_NAME:
            active_cat = cat
        else:
            killed_cat = cat
    active_category = active_cat
    killed_category = killed_cat

    assert active_cat is not None
    assert killed_cat is not None
    return guild, active_cat, killed_cat


# ---------------------------------------------------------------------------
# Channel lifecycle
# ---------------------------------------------------------------------------


async def ensure_agent_channel(agent_name: str) -> TextChannel:
    """Find or create a text channel for an agent. Moves from Killed to Active if needed."""
    assert _channel_to_agent is not None
    normalized = normalize_channel_name(agent_name)

    if active_category:
        for ch in active_category.text_channels:
            if ch.name == normalized:
                _channel_to_agent[ch.id] = agent_name
                return ch

    if killed_category:
        for ch in killed_category.text_channels:
            if ch.name == normalized:
                try:
                    await ch.move(category=active_category, beginning=True, sync_permissions=True)
                except discord.HTTPException as e:
                    log.warning("Failed to move channel #%s from Killed to Active: %s", normalized, e)
                    await _send_to_exceptions(f"Failed to move #**{normalized}** from Killed → Active: `{e}`")
                _channel_to_agent[ch.id] = agent_name
                log.info("Moved channel #%s from Killed to Active", normalized)
                return ch

    already_guarded = normalized in bot_creating_channels
    bot_creating_channels.add(normalized)
    try:
        assert target_guild is not None
        channel = await target_guild.create_text_channel(normalized, category=active_category)
    except discord.HTTPException as e:
        log.warning("Failed to create channel #%s: %s", normalized, e)
        await _send_to_exceptions(f"Failed to create channel #**{normalized}**: `{e}`")
        raise
    finally:
        if not already_guarded:
            bot_creating_channels.discard(normalized)
    _channel_to_agent[channel.id] = agent_name
    log.info("Created channel #%s in Active category", normalized)
    return channel


async def move_channel_to_killed(agent_name: str) -> None:
    """Move an agent's channel from Active to Killed category."""
    if agent_name == config.MASTER_AGENT_NAME:
        return

    normalized = normalize_channel_name(agent_name)
    if active_category:
        for ch in active_category.text_channels:
            if ch.name == normalized:
                try:
                    await ch.move(category=killed_category, end=True, sync_permissions=True)
                    log.info("Moved channel #%s to Killed category", normalized)
                except discord.HTTPException as e:
                    log.warning("Failed to move channel #%s to Killed: %s", normalized, e)
                    await _send_to_exceptions(f"Failed to move #**{normalized}** to Killed category: `{e}`")
                break


async def get_agent_channel(agent_name: str) -> TextChannel | None:
    """Get the Discord channel for an agent, if it exists."""
    assert _bot is not None
    assert _agents_dict is not None
    session = _agents_dict.get(agent_name)
    if session and session.discord_channel_id:
        ch = _bot.get_channel(session.discord_channel_id)
        if isinstance(ch, TextChannel):
            return ch
    normalized = normalize_channel_name(agent_name)
    if active_category:
        for ch in active_category.text_channels:
            if ch.name == normalized:
                return ch
    return None


async def get_master_channel() -> TextChannel | None:
    """Get the axi-master channel."""
    return await get_agent_channel(config.MASTER_AGENT_NAME)
