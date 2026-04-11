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
from axi.axi_types import AgentSession, discord_state

if TYPE_CHECKING:
    from discord.ext.commands import Bot

log = logging.getLogger(__name__)
_tracer = trace.get_tracer(__name__)

# ---------------------------------------------------------------------------
# Module-level state (populated via init and ensure_guild_infrastructure)
# ---------------------------------------------------------------------------

_bot: Bot | None = None
target_guild: discord.Guild | None = None
axi_categories: list[CategoryChannel] = []
active_categories: list[CategoryChannel] = []
killed_categories: list[CategoryChannel] = []
bot_creating_channels: set[str] = set()

DISCORD_CATEGORY_CHANNEL_LIMIT = 50

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
# Channel status prefixes
# ---------------------------------------------------------------------------

STATUS_PREFIXES: dict[str, str] = {
    "working": "\u26a1",
    "plan_review": "\U0001f4cb",
    "question": "\u2753",
    "done": "\u2705",
    "idle": "\U0001f4a4",
    "error": "\u26a0\ufe0f",
    "custom": "\U0001f527",
}

# Precomputed set of all emoji prefixes for fast stripping
_STATUS_PREFIX_STRINGS: set[str] = set()


def _rebuild_prefix_strings() -> None:
    """Rebuild the prefix lookup set from STATUS_PREFIXES."""
    _STATUS_PREFIX_STRINGS.clear()
    for emoji in STATUS_PREFIXES.values():
        _STATUS_PREFIX_STRINGS.add(emoji)


_rebuild_prefix_strings()


_CHANNEL_NAME_CHARS = re.compile(r"^[^a-z0-9_]+")


def strip_status_prefix(name: str) -> str:
    """Remove any leading emoji/non-channel-name characters from a channel name.

    Normalized channel names only contain [a-z0-9-_], so anything before the
    first such character is an emoji prefix.  This works for all emojis
    (STATUS_PREFIXES, custom overrides, or any future additions) without
    needing an explicit emoji list.
    """
    return _CHANNEL_NAME_CHARS.sub("", name)


def _match_channel_name(ch_name: str, normalized: str) -> bool:
    """Check if a channel name matches a normalized agent name, ignoring status prefix.

    Always strips emoji prefixes regardless of CHANNEL_STATUS_ENABLED, since
    channels may retain emoji prefixes from previous runs even when the feature
    is currently disabled.
    """
    if ch_name == normalized:
        return True
    return strip_status_prefix(ch_name) == normalized


# ---------------------------------------------------------------------------
# Category placement helper
# ---------------------------------------------------------------------------


def _is_axi_cwd(cwd: str | None) -> bool:
    """Return True if cwd is BOT_DIR or a worktree whose parent repo is BOT_DIR."""
    if not cwd:
        return False
    real = os.path.realpath(cwd)
    bot_real = os.path.realpath(config.BOT_DIR)
    # Direct match: cwd is BOT_DIR or inside it
    if real == bot_real or real.startswith(bot_real + os.sep):
        return True
    # Worktree match: cwd is under BOT_WORKTREES_DIR — check parent repo
    worktrees_real = os.path.realpath(config.BOT_WORKTREES_DIR)
    if real == worktrees_real or real.startswith(worktrees_real + os.sep):
        try:
            import subprocess
            result = subprocess.run(
                ["git", "-C", real, "rev-parse", "--git-common-dir"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                common_dir = os.path.realpath(result.stdout.strip())
                bot_git_dir = os.path.realpath(os.path.join(bot_real, ".git"))
                return common_dir == bot_git_dir
        except (subprocess.TimeoutExpired, OSError):
            pass
        return False
    return False


# ---------------------------------------------------------------------------
# Category group helpers
# ---------------------------------------------------------------------------


def is_killed_channel(channel: TextChannel) -> bool:
    """Check if a channel is in any Killed category (including overflow)."""
    return any(channel.category_id == cat.id for cat in killed_categories)


def is_axi_channel(channel: TextChannel) -> bool:
    """Check if a channel is in any Axi category (including overflow)."""
    return any(channel.category_id == cat.id for cat in axi_categories)


def is_active_channel(channel: TextChannel) -> bool:
    """Check if a channel is in any Active category (including overflow)."""
    return any(channel.category_id == cat.id for cat in active_categories)


async def _get_category_with_room(
    categories: list[CategoryChannel],
    base_name: str,
    *,
    killed: bool = False,
) -> CategoryChannel:
    """Return a category from the group with room, creating overflow if needed.

    Scans the list for the first category with < 50 channels. If all are full,
    creates a new overflow category (e.g. "Killed 2") with the same permissions.
    """
    for cat in categories:
        if len(cat.channels) < DISCORD_CATEGORY_CHANNEL_LIMIT:
            return cat

    # All full — create overflow
    next_num = len(categories) + 1
    overflow_name = f"{base_name} {next_num}"
    assert target_guild is not None
    overwrites = _build_category_overwrites(target_guild, killed=killed)
    cat = await target_guild.create_category(overflow_name, overwrites=overwrites)
    categories.append(cat)
    log.info("Created overflow category '%s'", overflow_name)

    # Re-enforce category positions so overflow slots in correctly
    await ensure_category_positions()

    return cat


# ---------------------------------------------------------------------------
# Guild infrastructure
# ---------------------------------------------------------------------------


def _build_category_overwrites(
    guild: discord.Guild,
    *,
    killed: bool = False,
) -> dict[discord.Object | discord.Member | discord.Role, discord.PermissionOverwrite]:
    """Build permission overwrites for Axi categories."""
    overwrites: dict[discord.Object | discord.Member | discord.Role, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(
            send_messages=False,
            add_reactions=False,
            create_public_threads=False,
            create_private_threads=False,
            send_messages_in_threads=False,
            view_channel=not killed,
            read_message_history=not killed,
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


def _match_category_group(cat_name: str, base_name: str) -> int | None:
    """Match a category name to a base group, returning its sort order.

    Returns 1 for the primary ("Killed"), 2+ for overflow ("Killed 2"), None if no match.
    """
    if cat_name == base_name:
        return 1
    prefix = base_name + " "
    if cat_name.startswith(prefix) and cat_name[len(prefix):].isdigit():
        return int(cat_name[len(prefix):])
    return None


async def ensure_guild_infrastructure() -> None:
    """Ensure the guild has Axi, Active, and Killed categories (with overflow). Called once during on_ready()."""
    global target_guild, axi_categories, active_categories, killed_categories
    assert _bot is not None
    _tracer.start_span("ensure_guild_infrastructure", attributes={"discord.guild_id": str(config.DISCORD_GUILD_ID)}).end()

    guild = _bot.get_guild(config.DISCORD_GUILD_ID)
    if guild is None:
        guild = await _bot.fetch_guild(config.DISCORD_GUILD_ID)
    target_guild = guild

    overwrites = _build_category_overwrites(guild)
    killed_overwrites = _build_category_overwrites(guild, killed=True)

    def _overwrites_match(
        existing: dict[discord.Role | discord.Member | discord.Object, discord.PermissionOverwrite],
        desired: dict[discord.Object | discord.Member | discord.Role, discord.PermissionOverwrite],
    ) -> bool:
        a = {getattr(k, "id", k): v for k, v in existing.items()}
        b = {getattr(k, "id", k): v for k, v in desired.items()}
        return a == b

    # Discover all existing categories (primary + overflow) per group
    # Each entry: (sort_order, CategoryChannel)
    axi_found: list[tuple[int, CategoryChannel]] = []
    active_found: list[tuple[int, CategoryChannel]] = []
    killed_found: list[tuple[int, CategoryChannel]] = []

    group_map = [
        (config.AXI_CATEGORY_NAME, axi_found),
        (config.ACTIVE_CATEGORY_NAME, active_found),
        (config.KILLED_CATEGORY_NAME, killed_found),
    ]

    for cat in guild.categories:
        for base_name, found_list in group_map:
            order = _match_category_group(cat.name, base_name)
            if order is not None:
                found_list.append((order, cat))
                break

    # Ensure primary exists for each group; sync permissions on all
    for base_name, found_list in group_map:
        desired = killed_overwrites if base_name == config.KILLED_CATEGORY_NAME else overwrites
        if not found_list:
            cat = await guild.create_category(base_name, overwrites=desired)
            found_list.append((1, cat))
            log.info("Created '%s' category", base_name)
        else:
            for _, cat in found_list:
                if not _overwrites_match(cat.overwrites, desired):
                    await cat.edit(overwrites=desired)
                    log.info("Synced permissions on '%s' category", cat.name)
                else:
                    log.info("Permissions already current on '%s' category", cat.name)

    # Sort by order number and store
    axi_found.sort(key=lambda x: x[0])
    active_found.sort(key=lambda x: x[0])
    killed_found.sort(key=lambda x: x[0])

    axi_categories = [cat for _, cat in axi_found]
    active_categories = [cat for _, cat in active_found]
    killed_categories = [cat for _, cat in killed_found]

    assert axi_categories
    assert active_categories
    assert killed_categories


# ---------------------------------------------------------------------------
# Channel lifecycle
# ---------------------------------------------------------------------------


async def ensure_agent_channel(agent_name: str, cwd: str | None = None) -> TextChannel:
    """Find or create a text channel for an agent.

    Category placement:
    - axi-master or agents with cwd in BOT_DIR/BOT_WORKTREES_DIR → Axi categories
    - All others → Active categories
    - Channels in Killed are moved to the appropriate target category group
    - Channels in the wrong live group are moved to the correct one
    - Uses overflow categories when the primary is full (50-channel limit)
    """
    assert _channel_to_agent is not None
    _tracer.start_span("ensure_agent_channel", attributes={"agent.name": agent_name}).end()
    normalized = normalize_channel_name(agent_name)

    is_axi = agent_name == config.MASTER_AGENT_NAME or _is_axi_cwd(cwd)
    target_group = axi_categories if is_axi else active_categories
    target_base_name = config.AXI_CATEGORY_NAME if is_axi else config.ACTIVE_CATEGORY_NAME
    target_group_ids = {cat.id for cat in target_group}

    # Search live categories (all Axi + all Active) for existing channel
    for cat in axi_categories + active_categories:
        for ch in cat.text_channels:
            if _match_channel_name(ch.name, normalized):
                # Move to correct group if it's in the wrong one
                if ch.category_id not in target_group_ids:
                    dest = await _get_category_with_room(target_group, target_base_name)
                    try:
                        await ch.move(category=dest, beginning=True, sync_permissions=True)
                        ch.category_id = dest.id
                        log.info("Moved channel #%s from %s to %s", normalized, cat.name, dest.name)
                    except discord.HTTPException as e:
                        log.warning("Failed to move #%s to %s: %s", normalized, dest.name, e)
                        await _send_to_exceptions(
                            f"Failed to move #**{normalized}** to {dest.name}: `{e}`"
                        )
                _channel_to_agent[ch.id] = agent_name
                return ch

    # Search all Killed categories
    for cat in killed_categories:
        for ch in cat.text_channels:
            if _match_channel_name(ch.name, normalized):
                dest = await _get_category_with_room(target_group, target_base_name)
                try:
                    await ch.move(category=dest, beginning=True, sync_permissions=True)
                    ch.category_id = dest.id
                except discord.HTTPException as e:
                    log.warning("Failed to move channel #%s from Killed to %s: %s", normalized, dest.name, e)
                    await _send_to_exceptions(
                        f"Failed to move #**{normalized}** from Killed → {dest.name}: `{e}`"
                    )
                _channel_to_agent[ch.id] = agent_name
                log.info("Moved channel #%s from Killed to %s", normalized, dest.name)
                return ch

    # Search uncategorized guild channels (e.g., master pinned to server top)
    if target_guild is not None:
        for ch in target_guild.text_channels:
            if _match_channel_name(ch.name, normalized) and ch.category is None:
                _channel_to_agent[ch.id] = agent_name
                return ch

    # Create new channel in target category (with overflow awareness)
    dest = await _get_category_with_room(target_group, target_base_name)
    already_guarded = normalized in bot_creating_channels
    bot_creating_channels.add(normalized)
    try:
        assert target_guild is not None
        channel = await target_guild.create_text_channel(normalized, category=dest)
    except discord.HTTPException as e:
        log.warning("Failed to create channel #%s: %s", normalized, e)
        await _send_to_exceptions(f"Failed to create channel #**{normalized}**: `{e}`")
        raise
    finally:
        if not already_guarded:
            bot_creating_channels.discard(normalized)
    _channel_to_agent[channel.id] = agent_name
    log.info("Created channel #%s in %s category", normalized, dest.name)
    return channel


async def move_channel_to_killed(agent_name: str) -> None:
    """Move an agent's channel to a Killed category (with overflow)."""
    if agent_name == config.MASTER_AGENT_NAME:
        return
    _tracer.start_span("move_channel_to_killed", attributes={"agent.name": agent_name}).end()

    normalized = normalize_channel_name(agent_name)
    for cat in axi_categories + active_categories:
        for ch in cat.text_channels:
            if _match_channel_name(ch.name, normalized):
                try:
                    dest = await _get_category_with_room(killed_categories, config.KILLED_CATEGORY_NAME, killed=True)
                    # Strip status prefix when moving to Killed
                    if config.CHANNEL_STATUS_ENABLED and ch.name != normalized:
                        await ch.edit(name=normalized)
                    await ch.move(category=dest, end=True, sync_permissions=True)
                    ch.category_id = dest.id
                    log.info("Moved channel #%s to %s", normalized, dest.name)
                except discord.HTTPException as e:
                    log.warning("Failed to move channel #%s to Killed: %s", normalized, e)
                    await _send_to_exceptions(f"Failed to move #**{normalized}** to Killed: `{e}`")
                return


async def get_agent_channel(
    agent_name: str, *, include_killed: bool = False
) -> TextChannel | None:
    """Get the Discord channel for an agent, if it exists.

    By default only searches Axi and Active categories.  Pass
    ``include_killed=True`` to also search Killed categories (useful for
    respawn detection where the channel still holds metadata like cwd).
    """
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
    cats = axi_categories + active_categories
    if include_killed:
        cats = cats + killed_categories
    for cat in cats:
        for ch in cat.text_channels:
            if _match_channel_name(ch.name, normalized):
                return ch
    return None


async def deduplicate_master_channel() -> None:
    """Delete duplicate axi-master channels and ensure the survivor is in Axi category.

    Called once during startup before ensure_agent_channel() for master.
    """
    normalized = normalize_channel_name(config.MASTER_AGENT_NAME)
    seen_ids: set[int] = set()
    master_channels: list[TextChannel] = []
    for cat in axi_categories + active_categories + killed_categories:
        for ch in cat.text_channels:
            if _match_channel_name(ch.name, normalized) and ch.id not in seen_ids:
                master_channels.append(ch)
                seen_ids.add(ch.id)
    # Also check uncategorized channels (master pinned to server top)
    if target_guild is not None:
        for ch in target_guild.text_channels:
            if _match_channel_name(ch.name, normalized) and ch.category is None and ch.id not in seen_ids:
                master_channels.append(ch)
                seen_ids.add(ch.id)

    if len(master_channels) <= 1:
        return

    # Prefer the uncategorized one at the top, then one in Axi category
    axi_cat_ids = {cat.id for cat in axi_categories}
    keep: TextChannel | None = None
    for ch in master_channels:
        if ch.category is None:
            keep = ch
            break
    if keep is None and axi_categories:
        for ch in master_channels:
            if ch.category_id in axi_cat_ids:
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
        if _match_channel_name(ch.name, normalized):
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


_category_positions_cooldown: float = 0.0

async def ensure_category_positions() -> None:
    """Ensure category groups are ordered: Axi..., Active..., Killed... (with overflow).

    Position 0 is reserved for #axi-master (uncategorized).
    Uses the same PATCH /guilds/{guild_id}/channels bulk-update pattern.
    Cooldown prevents feedback loops from on_guild_channel_update events.
    """
    global _category_positions_cooldown

    if target_guild is None:
        return

    now = time.monotonic()
    if now - _category_positions_cooldown < 5.0:
        return
    _category_positions_cooldown = now

    # Build position list: Axi, Axi 2, ..., Active, Active 2, ..., Killed, Killed 2, ...
    updates = []
    pos = 1
    for group in (axi_categories, active_categories, killed_categories):
        for cat in group:
            updates.append({"id": str(cat.id), "position": pos})
            pos += 1

    if not updates:
        return

    try:
        await config.discord_client.request(
            "PATCH",
            f"/guilds/{config.DISCORD_GUILD_ID}/channels",
            json=updates,
        )
        log.info("Enforced category positions")
    except Exception as e:
        log.warning("Failed to enforce category positions: %s", e)


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
    if not config.CHANNEL_SORT_BY_RECENCY:
        return
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

    for category in axi_categories + active_categories:
        text_channels = list(category.text_channels)
        if len(text_channels) <= 1:
            continue

        # Sort by activity (most recent first), channels without activity go to the end
        def _sort_key(ch: TextChannel) -> tuple[int, float]:
            # axi-master always first (priority 0), others priority 1
            is_master = 0 if _match_channel_name(ch.name, master_normalized) else 1
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


# ---------------------------------------------------------------------------
# Channel status prefix management
# ---------------------------------------------------------------------------

# agent_name → custom status string (set via MCP tool)
_status_overrides: dict[str, str] = {}

# channel_id → monotonic timestamp of last rename (rate limit tracking)
_last_rename: dict[int, float] = {}

_RENAME_COOLDOWN = 300.0  # 5 minutes — Discord allows 2 name changes per 10 min
_RENAME_DEBOUNCE = 60.0   # seconds between batch runs
_rename_task: asyncio.Task[None] | None = None
_rename_lock = asyncio.Lock()


def compute_agent_status(session: AgentSession) -> str:
    """Auto-detect the current status of an agent from its session state."""
    ds = discord_state(session)

    # Explicit override takes priority
    if session.name in _status_overrides:
        return "custom"

    # Error state
    if ds.task_error:
        return "error"

    # Waiting on user (plan review or question)
    if ds.plan_approval_future is not None:
        return "plan_review"
    if ds.question_future is not None:
        return "question"

    # Done (initial task completed)
    if ds.task_done:
        return "done"

    # Working (awake + busy)
    if session.client is not None and session.query_lock.locked():
        return "working"

    # Idle (sleeping or awake-idle)
    return "idle"


def _build_status_channel_name(agent_name: str, status: str) -> str:
    """Build a channel name with status emoji prefix."""
    base = normalize_channel_name(agent_name)
    # If there's an emoji override, use it directly (not from STATUS_PREFIXES)
    if status == "custom":
        override_emoji = _status_overrides.get(agent_name)
        if override_emoji:
            return f"{override_emoji}{base}"
    emoji = STATUS_PREFIXES.get(status)
    if emoji:
        return f"{emoji}{base}"
    return base


def set_status_override(agent_name: str, emoji: str | None) -> None:
    """Set or clear an explicit emoji override for an agent."""
    if emoji is None:
        _status_overrides.pop(agent_name, None)
    else:
        _status_overrides[agent_name] = emoji


def get_status_override(agent_name: str) -> str | None:
    """Get the current explicit status override for an agent, if any."""
    return _status_overrides.get(agent_name)


def schedule_status_update() -> None:
    """Schedule a debounced channel status rename batch.

    Called whenever agent status might have changed (query start/end,
    plan review, question asked, etc).
    """
    if not config.CHANNEL_STATUS_ENABLED:
        return
    global _rename_task
    if _rename_task is not None and not _rename_task.done():
        _rename_task.cancel()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _rename_task = loop.create_task(_debounced_rename())


async def _debounced_rename() -> None:
    """Wait for the debounce period, then run the rename batch."""
    try:
        await asyncio.sleep(_RENAME_DEBOUNCE)
    except asyncio.CancelledError:
        return
    await _do_rename_batch()


async def _do_rename_batch() -> None:
    """Rename channels to reflect current agent statuses.

    Respects the per-channel rename cooldown (2 per 10 min Discord rate limit).
    """
    if not config.CHANNEL_STATUS_ENABLED:
        return
    if _rename_lock.locked():
        return  # Already running; next schedule_status_update will catch up
    async with _rename_lock:
        if _agents_dict is None:
            return

        now = time.monotonic()
        renamed = 0

        for agent_name, session in list(_agents_dict.items()):
            # Skip master — it's pinned at the top, no status prefix
            if agent_name == config.MASTER_AGENT_NAME:
                continue

            ds = discord_state(session)
            if not ds.channel_id:
                continue

            # Only rename channels in live categories (not Killed)
            channel = _bot.get_channel(ds.channel_id) if _bot else None
            if not isinstance(channel, TextChannel):
                continue
            if is_killed_channel(channel):
                continue

            status = compute_agent_status(session)
            desired_name = _build_status_channel_name(agent_name, status)

            if channel.name == desired_name:
                continue  # Already correct

            # Rate limit: skip if renamed too recently
            last = _last_rename.get(channel.id, 0.0)
            if (now - last) < _RENAME_COOLDOWN:
                log.debug(
                    "Skipping rename of #%s (cooldown, %.0fs remaining)",
                    channel.name, _RENAME_COOLDOWN - (now - last),
                )
                continue

            try:
                await channel.edit(name=desired_name)
                _last_rename[channel.id] = time.monotonic()
                renamed += 1
                log.info("Status rename: #%s → #%s", channel.name, desired_name)
            except discord.HTTPException as e:
                log.warning("Failed to rename #%s → #%s: %s", channel.name, desired_name, e)

        if renamed > 0:
            log.info("Status rename batch: %d channels updated", renamed)
