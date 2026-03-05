"""Discord channel and guild management for the Axi bot.

Extracted from agents.py. Manages channel topic helpers, guild infrastructure,
and channel lifecycle. Agent-dict access is injected via init() to avoid
circular imports.

Pure functions: normalize_channel_name, format_channel_topic, parse_channel_topic.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import TYPE_CHECKING, Any

import discord
from discord import CategoryChannel, TextChannel
from opentelemetry import trace

from axi import config
from axi.axi_types import discord_state

if TYPE_CHECKING:
    from discord.ext.commands import Bot

log = logging.getLogger(__name__)
_tracer = trace.get_tracer(__name__)

# ---------------------------------------------------------------------------
# Module-level state (populated via init and ensure_guild_infrastructure)
# ---------------------------------------------------------------------------

_bot: Bot | None = None
target_guild: discord.Guild | None = None
axi_category: CategoryChannel | None = None
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
    agent_type: str | None = None,
) -> str:
    """Format agent metadata for a Discord channel topic."""
    parts = [f"cwd: {cwd}"]
    if session_id:
        parts.append(f"session: {session_id}")
    if prompt_hash:
        parts.append(f"prompt_hash: {prompt_hash}")
    if agent_type and agent_type != "flowcoder":
        parts.append(f"type: {agent_type}")
    return " | ".join(parts)


def parse_channel_topic(
    topic: str | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Parse cwd, session_id, prompt_hash, and agent_type from a channel topic."""
    if not topic:
        return None, None, None, None
    cwd = None
    session_id = None
    prompt_hash = None
    agent_type: str | None = None
    for part in topic.split("|"):
        key, _, value = part.strip().partition(": ")
        if key == "cwd":
            cwd = value.strip()
        elif key == "session":
            session_id = value.strip()
        elif key == "prompt_hash":
            prompt_hash = value.strip()
        elif key == "type":
            agent_type = value.strip()
    return cwd, session_id, prompt_hash, agent_type


# ---------------------------------------------------------------------------
# Category placement helper
# ---------------------------------------------------------------------------


def _is_axi_cwd(cwd: str | None) -> bool:
    """Return True if cwd is within the bot directory or a worktree."""
    if not cwd:
        return False
    real = os.path.realpath(cwd)
    bot_real = os.path.realpath(config.BOT_DIR)
    worktrees_real = os.path.realpath(config.BOT_WORKTREES_DIR)
    return real in (bot_real, worktrees_real) or real.startswith(
        (bot_real + os.sep, worktrees_real + os.sep)
    )


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


async def ensure_guild_infrastructure() -> tuple[discord.Guild, CategoryChannel, CategoryChannel, CategoryChannel]:
    """Ensure the guild has Axi, Active, and Killed categories. Called once during on_ready()."""
    global target_guild, axi_category, active_category, killed_category
    assert _bot is not None
    _tracer.start_span("ensure_guild_infrastructure", attributes={"discord.guild_id": str(config.DISCORD_GUILD_ID)}).end()

    guild = _bot.get_guild(config.DISCORD_GUILD_ID)
    if guild is None:
        guild = await _bot.fetch_guild(config.DISCORD_GUILD_ID)
    target_guild = guild

    overwrites = _build_category_overwrites(guild)

    axi_cat = None
    active_cat = None
    killed_cat = None
    for cat in guild.categories:
        if cat.name == config.AXI_CATEGORY_NAME:
            axi_cat = cat
        elif cat.name == config.ACTIVE_CATEGORY_NAME:
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
        (config.AXI_CATEGORY_NAME, axi_cat),
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
        if name == config.AXI_CATEGORY_NAME:
            axi_cat = cat
        elif name == config.ACTIVE_CATEGORY_NAME:
            active_cat = cat
        else:
            killed_cat = cat
    axi_category = axi_cat
    active_category = active_cat
    killed_category = killed_cat

    assert axi_cat is not None
    assert active_cat is not None
    assert killed_cat is not None
    return guild, axi_cat, active_cat, killed_cat


# ---------------------------------------------------------------------------
# Channel lifecycle
# ---------------------------------------------------------------------------


async def ensure_agent_channel(agent_name: str, cwd: str | None = None) -> TextChannel:
    """Find or create a text channel for an agent.

    Category placement:
    - axi-master or agents with cwd in BOT_DIR/BOT_WORKTREES_DIR → Axi category
    - All others → Active category
    - Channels in Killed are moved to the appropriate target category
    - Channels in the wrong live category are moved to the correct one
    """
    assert _channel_to_agent is not None
    _tracer.start_span("ensure_agent_channel", attributes={"agent.name": agent_name}).end()
    normalized = normalize_channel_name(agent_name)

    is_axi = agent_name == config.MASTER_AGENT_NAME or _is_axi_cwd(cwd)
    target_category = axi_category if is_axi else active_category

    # Search live categories (Axi + Active) for existing channel
    for cat in (axi_category, active_category):
        if cat is None:
            continue
        for ch in cat.text_channels:
            if ch.name == normalized:
                # Move to correct category if it's in the wrong one
                if target_category and ch.category_id != target_category.id:
                    try:
                        await ch.move(category=target_category, beginning=True, sync_permissions=True)
                        log.info("Moved channel #%s from %s to %s", normalized, cat.name, target_category.name)
                    except discord.HTTPException as e:
                        log.warning("Failed to move #%s to %s: %s", normalized, target_category.name, e)
                        await _send_to_exceptions(
                            f"Failed to move #**{normalized}** to {target_category.name}: `{e}`"
                        )
                _channel_to_agent[ch.id] = agent_name
                return ch

    # Search Killed category
    if killed_category:
        for ch in killed_category.text_channels:
            if ch.name == normalized:
                target_name = target_category.name if target_category else "?"
                try:
                    await ch.move(category=target_category, beginning=True, sync_permissions=True)
                except discord.HTTPException as e:
                    log.warning("Failed to move channel #%s from Killed to %s: %s", normalized, target_name, e)
                    await _send_to_exceptions(
                        f"Failed to move #**{normalized}** from Killed → {target_name}: `{e}`"
                    )
                _channel_to_agent[ch.id] = agent_name
                log.info("Moved channel #%s from Killed to %s", normalized, target_name)
                return ch

    # Search uncategorized guild channels (e.g., master pinned to server top)
    if target_guild is not None:
        for ch in target_guild.text_channels:
            if ch.name == normalized and ch.category is None:
                _channel_to_agent[ch.id] = agent_name
                return ch

    # Create new channel in target category
    already_guarded = normalized in bot_creating_channels
    bot_creating_channels.add(normalized)
    try:
        assert target_guild is not None
        channel = await target_guild.create_text_channel(normalized, category=target_category)
    except discord.HTTPException as e:
        log.warning("Failed to create channel #%s: %s", normalized, e)
        await _send_to_exceptions(f"Failed to create channel #**{normalized}**: `{e}`")
        raise
    finally:
        if not already_guarded:
            bot_creating_channels.discard(normalized)
    _channel_to_agent[channel.id] = agent_name
    cat_name = target_category.name if target_category else "?"
    log.info("Created channel #%s in %s category", normalized, cat_name)
    return channel


async def move_channel_to_killed(agent_name: str) -> None:
    """Move an agent's channel to the Killed category."""
    if agent_name == config.MASTER_AGENT_NAME:
        return
    _tracer.start_span("move_channel_to_killed", attributes={"agent.name": agent_name}).end()

    normalized = normalize_channel_name(agent_name)
    for cat in (axi_category, active_category):
        if cat is None:
            continue
        for ch in cat.text_channels:
            if ch.name == normalized:
                try:
                    await ch.move(category=killed_category, end=True, sync_permissions=True)
                    log.info("Moved channel #%s to Killed category", normalized)
                except discord.HTTPException as e:
                    log.warning("Failed to move channel #%s to Killed: %s", normalized, e)
                    await _send_to_exceptions(f"Failed to move #**{normalized}** to Killed category: `{e}`")
                return


async def get_agent_channel(agent_name: str) -> TextChannel | None:
    """Get the Discord channel for an agent, if it exists."""
    assert _bot is not None
    assert _agents_dict is not None
    session = _agents_dict.get(agent_name)
    if session:
        ds = discord_state(session)
        if ds.channel_id:
            ch = _bot.get_channel(ds.channel_id)
            if isinstance(ch, TextChannel):
                return ch
    normalized = normalize_channel_name(agent_name)
    for cat in (axi_category, active_category):
        if cat is None:
            continue
        for ch in cat.text_channels:
            if ch.name == normalized:
                return ch
    return None


async def deduplicate_master_channel() -> None:
    """Delete duplicate axi-master channels and ensure the survivor is in Axi category.

    Called once during startup before ensure_agent_channel() for master.
    """
    normalized = normalize_channel_name(config.MASTER_AGENT_NAME)
    seen_ids: set[int] = set()
    master_channels: list[TextChannel] = []
    for cat in (axi_category, active_category, killed_category):
        if cat is None:
            continue
        for ch in cat.text_channels:
            if ch.name == normalized and ch.id not in seen_ids:
                master_channels.append(ch)
                seen_ids.add(ch.id)
    # Also check uncategorized channels (master pinned to server top)
    if target_guild is not None:
        for ch in target_guild.text_channels:
            if ch.name == normalized and ch.category is None and ch.id not in seen_ids:
                master_channels.append(ch)
                seen_ids.add(ch.id)

    if len(master_channels) <= 1:
        return

    # Prefer the uncategorized one at the top, then one in Axi category
    keep: TextChannel | None = None
    for ch in master_channels:
        if ch.category is None:
            keep = ch
            break
    if keep is None and axi_category:
        for ch in master_channels:
            if ch.category_id == axi_category.id:
                keep = ch
                break
    if keep is None:
        keep = master_channels[0]

    for ch in master_channels:
        if ch.id != keep.id:
            try:
                await ch.delete(reason="Duplicate axi-master channel")
                log.info("Deleted duplicate axi-master channel (id=%d, category=%s)", ch.id, ch.category and ch.category.name)
            except discord.HTTPException as e:
                log.warning("Failed to delete duplicate axi-master channel: %s", e)
                if _send_to_exceptions:
                    await _send_to_exceptions(f"Failed to delete duplicate axi-master channel: `{e}`")

    log.info("Deduplicated axi-master channels — kept id=%d", keep.id)


async def get_master_channel() -> TextChannel | None:
    """Get the axi-master channel."""
    return await get_agent_channel(config.MASTER_AGENT_NAME)


async def ensure_master_channel_position() -> None:
    """Ensure #axi-master is at position 0 with no category (top of server).

    Uses the Discord REST API (PATCH /guilds/{guild_id}/channels) to move
    the master channel above all categories and other channels.
    """
    if target_guild is None:
        return

    normalized = normalize_channel_name(config.MASTER_AGENT_NAME)
    master_ch: TextChannel | None = None
    for ch in target_guild.text_channels:
        if ch.name == normalized:
            master_ch = ch
            break

    if master_ch is None:
        return

    # Already at position 0 with no category — nothing to do
    if master_ch.position == 0 and master_ch.category_id is None:
        log.debug("#%s already at position 0, no category", normalized)
        return

    try:
        await config.discord_client.request(
            "PATCH",
            f"/guilds/{config.DISCORD_GUILD_ID}/channels",
            json=[{"id": str(master_ch.id), "position": 0, "parent_id": None}],
        )
        log.info("Moved #%s to position 0 (top of server, no category)", normalized)
    except Exception as e:
        log.warning("Failed to move #%s to top: %s", normalized, e)


# ---------------------------------------------------------------------------
# Channel recency reordering
# ---------------------------------------------------------------------------

# channel_id → monotonic timestamp of last activity
_channel_activity: dict[int, float] = {}

_REORDER_DEBOUNCE_SECONDS = 60.0
_reorder_task: asyncio.Task[None] | None = None
_reorder_lock = asyncio.Lock()


def mark_channel_active(channel_id: int) -> None:
    """Record activity on a channel and schedule a debounced reorder."""
    _channel_activity[channel_id] = time.monotonic()
    _schedule_reorder()


def _schedule_reorder() -> None:
    """Schedule a reorder after the debounce window. Resets if called again."""
    global _reorder_task
    if _reorder_task is not None and not _reorder_task.done():
        _reorder_task.cancel()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _reorder_task = loop.create_task(_debounced_reorder())


async def _debounced_reorder() -> None:
    """Wait for the debounce period, then reorder channels by recency."""
    try:
        await asyncio.sleep(_REORDER_DEBOUNCE_SECONDS)
    except asyncio.CancelledError:
        return
    await reorder_channels_by_recency()


async def reorder_channels_by_recency() -> None:
    """Reorder channels within Axi and Active categories by recent activity.

    - #axi-master always stays at position 0 in its category.
    - Other channels are sorted most-recent-first.
    - Uses a single bulk API call per category.
    """
    if not _reorder_lock.locked():
        async with _reorder_lock:
            await _do_reorder()
    # If already reordering, skip — the next activity will schedule another.


async def _do_reorder() -> None:
    """Perform the actual reorder for both Axi and Active categories."""
    assert target_guild is not None
    master_normalized = normalize_channel_name(config.MASTER_AGENT_NAME)

    for category in (axi_category, active_category):
        if category is None:
            continue
        text_channels = list(category.text_channels)
        if len(text_channels) <= 1:
            continue

        # Sort by activity (most recent first), channels without activity go to the end
        def _sort_key(ch: TextChannel) -> tuple[int, float]:
            # axi-master always first (priority 0), others priority 1
            is_master = 0 if ch.name == master_normalized else 1
            # Negate timestamp so higher (more recent) sorts first
            activity = -_channel_activity.get(ch.id, 0.0)
            return (is_master, activity)

        desired_order = sorted(text_channels, key=_sort_key)

        # Check if reorder is actually needed
        current_order = sorted(text_channels, key=lambda c: (c.position, c.id))
        if [ch.id for ch in current_order] == [ch.id for ch in desired_order]:
            log.debug("Channel order in '%s' already correct, skipping API call", category.name)
            continue

        # Build bulk update payload — cast needed because TypedDict vs dict
        payload: Any = [{"id": ch.id, "position": idx} for idx, ch in enumerate(desired_order)]

        try:
            assert _bot is not None
            await _bot.http.bulk_channel_update(
                target_guild.id, payload, reason="Recency reorder"
            )
            log.info(
                "Reordered %d channels in '%s': %s",
                len(payload),
                category.name,
                " > ".join(ch.name for ch in desired_order),
            )
        except discord.HTTPException as e:
            log.warning("Failed to reorder channels in '%s': %s", category.name, e)
