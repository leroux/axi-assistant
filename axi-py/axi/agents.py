# pyright: reportPrivateUsage=false
"""Agent lifecycle, streaming, rate limits, procmux, and channel management.

Migration in progress: lifecycle, registry, and messaging delegate to AgentHub.
Discord-specific rendering (streaming, live-edit, reactions) stays here.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shlex
import time
import traceback
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import anyio
import discord
import httpx
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)
from discord import TextChannel
from opentelemetry import context as otel_context
from opentelemetry import trace

from agenthub.procmux_wire import ProcmuxProcessConnection
from agenthub.tasks import BackgroundTaskSet
from axi import channels as _channels_mod
from axi import config, scheduler
from axi.extensions import DEFAULT_EXTENSIONS, resolve_extension_hooks, resolve_prompt_hooks
from axi.axi_types import (
    ActivityState,
    AgentSession,
    ConcurrencyLimitError,
    ContentBlock,
    MessageContent,
    discord_state,
)
from axi.channels import (
    ensure_agent_channel,
    ensure_guild_infrastructure,
    format_channel_topic,
    get_agent_channel,
    get_master_channel,
    mark_channel_active,
    move_channel_to_killed,
    normalize_channel_name,
    schedule_status_update,
)
from axi.channels import (
    parse_channel_topic as _parse_channel_topic,
)

# Re-exports from discord_stream (extracted Phase 0a) — keeps existing imports working
from axi.discord_stream import (  # noqa: F401
    _cancel_typing,
    _compact_start_times,
    _handle_system_message,
    _live_edit_finalize,
    _live_edit_tick,
    _LiveEditState,
    _pending_compact,
    _self_compacting,
    _StreamCtx,
    _update_activity,
    extract_tool_preview,
    interrupt_session,
    stream_response_to_channel,
    stream_with_retry,
)

# Re-exports from discord_ui (extracted Phase 0b) — keeps existing imports working
from axi.discord_ui import (  # noqa: F401
    _handle_ask_user_question,
    _handle_exit_plan_mode,
    _post_todo_list,
    _read_latest_plan_file,
    format_todo_list,
    load_todo_items,
    parse_question_answer,
    resolve_reaction_answer,
)
from axi.log_context import set_agent_context, set_trigger
from axi.prompts import (
    compute_prompt_hash,
    make_spawned_agent_system_prompt,
    post_system_prompt_to_channel,
)
from axi.rate_limits import (
    format_time_remaining,
    is_rate_limited,
    notify_rate_limit_expired,
    rate_limit_quotas,
    rate_limit_remaining_seconds,
    rate_limited_until,
    session_usage,
)
from axi.rate_limits import (
    handle_rate_limit as _rl_handle_rate_limit,
)
from axi.rate_limits import (
    update_rate_limit_quota as _update_rate_limit_quota,
)
from axi.schedule_tools import make_schedule_mcp_server
from axi.shutdown import ShutdownCoordinator, exit_for_restart, kill_supervisor
from axi.tracing import shutdown_tracing
from claudewire import BridgeTransport
from claudewire.events import as_stream
from claudewire.session import disconnect_client, get_stdio_logger
from procmux import ensure_running as ensure_bridge

if TYPE_CHECKING:
    from discord.ext.commands import Bot

    from agenthub import AgentHub
    from procmux import ProcmuxConnection

log = logging.getLogger("axi")

_tracer = trace.get_tracer(__name__)

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_bot: Bot | None = None

# The hub owns lifecycle, registry, messaging, scheduler, rate limits.
# Created in init() via hub_wiring.create_hub().
hub: AgentHub | None = None

# Session dict — shared between hub and legacy code.
# hub.sessions points to this same dict during migration.
agents: dict[str, AgentSession] = {}
channel_to_agent: dict[int, str] = {}  # channel_id -> agent_name

# Active trace IDs — maps agent name → trace tag string (e.g. "[trace=abc123...]")
# Set when process_message starts its span, cleared when done.
_active_trace_ids: dict[str, str] = {}

# ---------------------------------------------------------------------------
# Soul flowchart wrapping — routes messages through /soul or /soul-flow
# ---------------------------------------------------------------------------

_TS_PREFIX_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC\] ")

_COMMANDS_DIR = os.path.join(config.BOT_DIR, "commands")


def _strip_ts(text: str) -> str:
    """Strip the ``[YYYY-MM-DD HH:MM:SS UTC] `` prefix from message content."""
    return _TS_PREFIX_RE.sub("", text)


def _command_exists(name: str) -> bool:
    """Check if a FlowCoder command JSON exists in the commands directory."""
    return os.path.isfile(os.path.join(_COMMANDS_DIR, f"{name}.json"))


def _is_axi_dev_cwd(cwd: str) -> bool:
    """Check if a working directory is within the axi-assistant codebase."""
    return cwd.startswith(config.BOT_DIR) or bool(
        config.BOT_WORKTREES_DIR and cwd.startswith(config.BOT_WORKTREES_DIR)
    )


def _wrap_content_with_soul(content: MessageContent, session: AgentSession) -> MessageContent:
    """Transform message content to route through /soul or /soul-flow flowcharts.

    - Non-string content (image blocks): returned as-is
    - //raw prefix: stripped and returned without wrapping
    - /soul or /soul-flow commands: returned as-is (they ARE the wrappers)
    - Other /commands: wrapped in /soul-flow if available
    - Regular messages: wrapped in /soul with extension hooks
    - If /soul doesn't exist or agent isn't flowcoder: returned as-is
    """
    if not isinstance(content, str):
        return content
    if session.agent_type != "flowcoder":
        return content

    raw = _strip_ts(content)

    # //raw bypass — strip prefix and send directly
    if raw.startswith("//raw"):
        return raw[5:].lstrip() if len(raw) > 5 else raw

    has_soul = _command_exists("soul")
    has_soul_flow = _command_exists("soul-flow")

    # Slash commands
    if raw.startswith("/"):
        parts = raw[1:].strip().split(None, 1)
        cmd_name = parts[0] if parts else ""
        cmd_args = parts[1] if len(parts) > 1 else ""

        # soul/soul-flow commands pass through directly
        if cmd_name in ("soul", "soul-flow"):
            return content
        # Other commands: wrap in /soul-flow if available
        if has_soul_flow:
            audience = "admin" if _is_axi_dev_cwd(session.cwd) else "general"
            hooks = resolve_extension_hooks(DEFAULT_EXTENSIONS, audience)
            pre_task = hooks.get("pre_task", "")
            post_task = hooks.get("post_task", "")
            prompt_hooks = resolve_prompt_hooks(DEFAULT_EXTENSIONS, audience)
            report_records_text = prompt_hooks.get("report_records", "")

            # Double-quote: cmd_args goes through TWO shlex.split levels
            try:
                tokens = shlex.split(cmd_args) if cmd_args.strip() else []
            except ValueError:
                tokens = [cmd_args]
            inner_quoted = " ".join(shlex.quote(t) for t in tokens)

            wrapped = (
                f'/soul-flow "{pre_task}" "{post_task}"'
                f" {shlex.quote(report_records_text)}"
                f" {shlex.quote(cmd_name)}"
                f" {shlex.quote(inner_quoted)}"
            )
            log.info("SOUL_WRAP[%s] /%s -> /soul-flow (pre=%s post=%s)", session.name, cmd_name, pre_task, post_task)
            return wrapped
        return content

    # Regular messages: wrap in /soul
    if has_soul:
        audience = "admin" if _is_axi_dev_cwd(session.cwd) else "general"
        hooks = resolve_extension_hooks(DEFAULT_EXTENSIONS, audience)
        pre_task = hooks.get("pre_task", "")
        execute = hooks.get("execute", "")
        post_task = hooks.get("post_task", "")
        post_respond = hooks.get("post_respond", "")

        prompt_hooks = resolve_prompt_hooks(DEFAULT_EXTENSIONS, audience)
        report_records_text = prompt_hooks.get("report_records", "")

        # Sanitize user text for safe embedding in flowchart arguments
        sanitized = raw.replace("\\", "\u2216").replace("'", "\u2019").replace('"', "\u201c")

        wrapped = (
            f'/soul "{pre_task}" "{execute}" "{post_task}" "{post_respond}"'
            f" {shlex.quote(sanitized)}"
            f" {shlex.quote(report_records_text)}"
        )
        log.info(
            "SOUL_WRAP[%s] routing through /soul (pre=%s exec=%s post=%s respond=%s)",
            session.name, pre_task, execute, post_task, post_respond,
        )
        return wrapped

    return content


# Public alias for use from main.py / agenthub integration
wrap_content_with_soul = _wrap_content_with_soul


def get_active_trace_tag(agent_name: str) -> str:
    """Return the trace tag for the agent's in-flight turn, or empty string."""
    return _active_trace_ids.get(agent_name, "")



def find_session_by_question_message(message_id: int) -> AgentSession | None:
    """Find the agent session waiting for a reaction answer on this message."""
    for session in agents.values():
        ds = discord_state(session)
        if ds.question_message_id == message_id:
            return session
    return None


# Bridge connection — initialized in on_ready(), used by wake_agent/sleep_agent
procmux_conn: ProcmuxConnection | None = None
# Adapted connection for claudewire (wraps procmux_conn)
wire_conn: ProcmuxProcessConnection | None = None

# Shutdown coordinator — initialized via init_shutdown_coordinator() from on_ready
shutdown_coordinator: ShutdownCoordinator | None = None

# Scheduler state
schedule_last_fired: dict[str, datetime] = {}

# MCP server injection (set by bot.py after tools.py creates them)
_utils_mcp_server: Any = None

# Background task manager — hub.tasks after full migration; kept for legacy callers.
_bg_tasks = BackgroundTaskSet()
fire_and_forget = _bg_tasks.fire_and_forget


def _user_mentions() -> str:
    """Generate Discord @mention string for all allowed users."""
    return " ".join(f"<@{uid}>" for uid in config.ALLOWED_USER_IDS)


# ---------------------------------------------------------------------------
# Initialization — called once from bot.py after Bot creation
# ---------------------------------------------------------------------------


def init(bot_instance: Bot) -> None:
    """Inject the Bot reference and create the AgentHub. Called once from bot.py."""
    global _bot, hub
    _bot = bot_instance

    # Create the hub — shares the `agents` dict with legacy code
    from axi.hub_wiring import create_hub

    hub = create_hub(bot_instance, agents)

    _channels_mod.init(bot_instance, agents, channel_to_agent, send_to_exceptions)

    # Initialize discord_stream with function references (avoids circular imports)
    from axi import discord_stream as _ds_mod

    _ds_mod.init(
        send_long_fn=send_long,
        send_system_fn=send_system,
        send_to_exceptions_fn=send_to_exceptions,
        post_todo_list_fn=_post_todo_list,
        set_session_id_fn=_set_session_id,
        handle_rate_limit_fn=_handle_rate_limit,
        sleep_agent_fn=lambda s, force=False: sleep_agent(s, force=force),  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
        get_procmux_conn_fn=lambda: procmux_conn,
    )

    # Initialize discord_ui with function references
    from axi import discord_ui as _ui_mod

    _ui_mod.init(user_mentions_fn=_user_mentions)


def set_utils_mcp_server(server: Any) -> None:
    """Set the utils MCP server reference. Called from bot.py after tools.py init."""
    global _utils_mcp_server
    _utils_mcp_server = server


def _build_mcp_servers(
    agent_name: str,
    cwd: str | None = None,
    extra_mcp_servers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the standard MCP server dict for an agent."""
    servers: dict[str, Any] = {}
    if _utils_mcp_server is not None:
        servers["utils"] = _utils_mcp_server
    servers["schedule"] = make_schedule_mcp_server(agent_name, config.SCHEDULES_PATH, cwd)
    servers["playwright"] = {
        "command": "npx",
        "args": ["@playwright/mcp@latest", "--headless"],
    }
    if extra_mcp_servers:
        servers.update(extra_mcp_servers)
    return servers




def _save_agent_config(
    agent_name: str,
    mcp_server_names: list[str] | None,
    extensions: list[str] | None = None,
) -> None:
    """Persist per-agent config (MCP servers, extensions) to disk."""
    config_dir = os.path.join(config.AXI_USER_DATA, "agents", agent_name)
    os.makedirs(config_dir, exist_ok=True)
    config_path = os.path.join(config_dir, "agent_config.json")
    data: dict[str, Any] = {}
    if mcp_server_names:
        data["mcp_servers"] = mcp_server_names
    if extensions is not None:
        data["extensions"] = extensions
    try:
        with open(config_path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        log.warning("Failed to save agent config for '%s'", agent_name, exc_info=True)


def _load_agent_config(agent_name: str) -> dict[str, Any]:
    """Load per-agent config from disk. Returns {} if not found."""
    config_path = os.path.join(config.AXI_USER_DATA, "agents", agent_name, "agent_config.json")
    if not os.path.exists(config_path):
        return {}
    try:
        with open(config_path) as f:
            return json.load(f)
    except Exception:
        log.warning("Failed to load agent config for '%s'", agent_name, exc_info=True)
        return {}

# ---------------------------------------------------------------------------
# SDK utilities
# ---------------------------------------------------------------------------


def _close_agent_log(session: AgentSession) -> None:
    """Remove all handlers from the per-agent logger."""
    if session.agent_log:
        for handler in session.agent_log.handlers[:]:
            handler.close()
            session.agent_log.removeHandler(handler)


_AUTOCOMPACT_RE = re.compile(r"autocompact: tokens=(\d+) threshold=\d+ effectiveWindow=(\d+)")


def make_stderr_callback(session: AgentSession):
    """Create a stderr callback bound to a specific agent session."""
    ds = discord_state(session)  # cache — callback runs in a thread

    def callback(text: str) -> None:
        with ds.stderr_lock:
            ds.stderr_buffer.append(text)
        # Parse autocompact debug line for context window monitoring
        m = _AUTOCOMPACT_RE.search(text)
        if m:
            session.context_tokens = int(m.group(1))
            session.context_window = int(m.group(2))

    return callback


def drain_stderr(session: AgentSession) -> list[str]:
    """Drain stderr buffer for a specific agent session."""
    ds = discord_state(session)
    with ds.stderr_lock:
        msgs = list(ds.stderr_buffer)
        ds.stderr_buffer.clear()
    return msgs


def drain_sdk_buffer(session: AgentSession) -> int:
    """Drain any stale messages from the SDK message buffer before sending a new query."""
    if session.client is None or getattr(session.client, "_query", None) is None:
        return 0

    client = session.client
    # narrowing: getattr check above guarantees this
    assert client._query is not None  # pyright: ignore[reportPrivateUsage]
    receive_stream = client._query._message_receive  # pyright: ignore[reportPrivateUsage]
    drained: list[dict[str, Any]] = []
    while True:
        try:
            msg = receive_stream.receive_nowait()
            drained.append(msg)
        except anyio.WouldBlock:
            break
        except Exception:
            log.warning("Unexpected error draining SDK buffer for '%s'", session.name, exc_info=True)
            break

    if drained:
        for msg in drained:
            msg_type = msg.get("type", "?")
            msg_role = msg.get("message", {}).get("role", "") if isinstance(msg.get("message"), dict) else ""
            log.warning(
                "Drained stale SDK message from '%s': type=%s role=%s",
                session.name,
                msg_type,
                msg_role,
            )
            if msg_type == "rate_limit_event":
                _update_rate_limit_quota(msg)
        log.warning("Total drained from '%s': %d stale messages", session.name, len(drained))

    return len(drained)


# ---------------------------------------------------------------------------
# Discord helpers: reactions, message extraction, splitting, and sending
# ---------------------------------------------------------------------------

# Exceptions channel (REST-based, works in any context)
_exceptions_channel_id: str | None = None


async def add_reaction(message: discord.Message | None, emoji: str) -> None:
    """Add a reaction to a message, silently ignoring errors."""
    if message is None:
        return
    try:
        await message.add_reaction(emoji)
        log.info("Reaction +%s on message %s", emoji, message.id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
        log.warning("Reaction +%s failed on message %s: %s", emoji, message.id, exc)


async def remove_reaction(message: discord.Message | None, emoji: str) -> None:
    """Remove the bot's own reaction from a message, silently ignoring errors."""
    if message is None:
        return
    try:
        assert _bot is not None
        assert _bot.user is not None
        await message.remove_reaction(emoji, _bot.user)
        log.info("Reaction -%s on message %s", emoji, message.id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
        log.warning("Reaction -%s failed on message %s: %s", emoji, message.id, exc)


# ---------------------------------------------------------------------------
# Image attachment support
# ---------------------------------------------------------------------------

_SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
_MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10 MB per image


async def extract_message_content(message: discord.Message) -> MessageContent:
    """Extract text and image content from a Discord message."""
    # Discord long-message: blank content with an attached message.txt
    if not message.content.strip() and message.attachments:
        for a in message.attachments:
            if a.filename == "message.txt" and a.size <= 100_000:
                try:
                    data = await a.read()
                    text = data.decode("utf-8")
                    log.debug("Read long message from message.txt (%d chars)", len(text))
                    message.content = text
                    break
                except Exception:
                    log.warning("Failed to read message.txt attachment", exc_info=True)

    ts_prefix = message.created_at.strftime("[%Y-%m-%d %H:%M:%S UTC] ")

    image_attachments = [
        a
        for a in message.attachments
        if a.content_type
        and a.content_type.split(";")[0].strip() in _SUPPORTED_IMAGE_TYPES
        and a.size <= _MAX_IMAGE_SIZE
    ]

    if not image_attachments:
        return ts_prefix + message.content

    blocks: list[ContentBlock] = []
    blocks.append({"type": "text", "text": ts_prefix + (message.content or "")})

    for attachment in image_attachments:
        try:
            data = await attachment.read()
            b64 = base64.b64encode(data).decode("utf-8")
            mime = (attachment.content_type or "application/octet-stream").split(";")[0].strip()
            blocks.append({"type": "image", "data": b64, "mimeType": mime})
            log.debug("Attached image: %s (%s, %d bytes)", attachment.filename, mime, len(data))
        except Exception:
            log.warning("Failed to download attachment %s", attachment.filename, exc_info=True)

    return blocks or message.content


def content_summary(content: MessageContent) -> str:
    """Short text summary of message content for logging."""
    if isinstance(content, str):
        return content[:200]
    parts: list[str] = []
    for block in content:
        if block.get("type") == "text":
            parts.append(block["text"][:100])
        elif block.get("type") == "image":
            parts.append(f"[image:{block.get('mimeType', '?')}]")
    return " ".join(parts)[:200]


# ---------------------------------------------------------------------------
# Exceptions channel (REST-based, works in any context)
# ---------------------------------------------------------------------------


async def _get_or_create_exceptions_channel() -> str | None:
    """Get or create the #exceptions channel via REST API."""
    global _exceptions_channel_id

    if _exceptions_channel_id is not None:
        return _exceptions_channel_id
    try:
        guild_id = str(config.DISCORD_GUILD_ID)
        ch = await config.discord_client.find_channel(guild_id, "exceptions")
        if ch:
            _exceptions_channel_id = ch["id"]
            return _exceptions_channel_id
        created = await config.discord_client.create_channel(guild_id, "exceptions")
        _exceptions_channel_id = created["id"]
        log.info("Created #exceptions channel (id=%s)", _exceptions_channel_id)
        return _exceptions_channel_id
    except Exception:
        log.warning("Failed to get/create #exceptions channel", exc_info=True)
        return None


async def send_to_exceptions(message: str) -> bool:
    """Send a message to the #exceptions channel. Returns True on success."""
    global _exceptions_channel_id

    try:
        ch_id = await _get_or_create_exceptions_channel()
        if ch_id is None:
            return False
        await config.discord_client.send_message(ch_id, message[:2000])
        return True
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            log.warning("#exceptions channel %s returned 404; clearing cached ID", _exceptions_channel_id)
            _exceptions_channel_id = None
        else:
            log.warning("Failed to send to #exceptions", exc_info=True)
        return False
    except Exception:
        log.warning("Failed to send to #exceptions", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Message splitting / sending
# ---------------------------------------------------------------------------

from discordquery import split_message


async def send_long(channel: TextChannel, text: str) -> discord.Message | None:
    """Send a potentially long message, splitting as needed. Returns the last sent message."""
    # Track channel activity for recency reordering
    mark_channel_active(channel.id)

    span = _tracer.start_span(
        "discord.send_long",
        attributes={"discord.channel": getattr(channel, "name", "?"), "message.length": len(text)},
    )
    chunks = split_message(text.strip())
    span.set_attribute("message.chunks", len(chunks))
    span.end()
    last_msg: discord.Message | None = None
    for i, chunk in enumerate(chunks):
        if chunk:
            if log.isEnabledFor(logging.INFO):
                caller = "".join(f.name or "?" for f in traceback.extract_stack(limit=4)[:-1])
                log.info(
                    "DISCORD_SEND[#%s] chunk %d/%d len=%d caller=%s text=%r",
                    getattr(channel, "name", "?"),
                    i + 1,
                    len(chunks),
                    len(chunk),
                    caller,
                    chunk[:80],
                )
            try:
                last_msg = await channel.send(chunk)
            except discord.NotFound:
                agent_name = channel_to_agent.get(channel.id)
                if agent_name:
                    log.warning("Channel for '%s' was deleted, recreating", agent_name)
                    session = agents.get(agent_name)
                    new_ch = await ensure_agent_channel(agent_name, cwd=session.cwd if session else None)
                    if session:
                        discord_state(session).channel_id = new_ch.id
                    last_msg = await new_ch.send(chunk)
                else:
                    raise
    return last_msg


async def send_system(channel: TextChannel, text: str) -> None:
    """Send a system-prefixed message."""
    await send_long(channel, f"*System:* {text}")


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


def make_cwd_permission_callback(allowed_cwd: str, session: AgentSession | None = None):
    """Create a can_use_tool callback that restricts file writes to allowed_cwd and AXI_USER_DATA."""
    allowed = os.path.realpath(allowed_cwd)
    user_data = os.path.realpath(config.AXI_USER_DATA)
    worktrees = os.path.realpath(config.BOT_WORKTREES_DIR)
    bot_dir = os.path.realpath(config.BOT_DIR)

    is_code_agent = allowed in (bot_dir, worktrees) or allowed.startswith((bot_dir + os.sep, worktrees + os.sep))
    bases = [allowed, user_data]
    if is_code_agent:
        bases.append(worktrees)
        bases.extend(config.ADMIN_ALLOWED_CWDS)

    async def _check_permission(
        tool_name: str,
        tool_input: dict[str, Any],
        ctx: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        forbidden_tools = {"Skill", "EnterWorktree", "Task"}
        if tool_name in forbidden_tools:
            return PermissionResultDeny(
                message=f"{tool_name} is not compatible with Discord-based agent mode. Use text messages to communicate instead."
            )

        if tool_name == "TodoWrite":
            return PermissionResultAllow()

        if tool_name == "EnterPlanMode":
            return PermissionResultAllow()

        if tool_name == "ExitPlanMode":
            return await _handle_exit_plan_mode(session, tool_input)

        if tool_name == "AskUserQuestion":
            return await _handle_ask_user_question(session, tool_input)

        if tool_name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
            resolved = os.path.realpath(path)
            for base in bases:
                if resolved == base or resolved.startswith(base + os.sep):
                    return PermissionResultAllow()
            return PermissionResultDeny(
                message=f"Access denied: {path} is outside working directory {allowed} and user data {user_data}"
            )
        return PermissionResultAllow()

    return _check_permission



# ---------------------------------------------------------------------------
# Discord UI handlers — moved to axi/discord_ui.py (Phase 0b)
# Functions are re-exported at the top of this module for backwards compat.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Rate limiting adapter
# ---------------------------------------------------------------------------


async def _handle_rate_limit(error_text: str, session: AgentSession, channel: TextChannel) -> None:
    """Handle a rate limit error: set global state, notify all agent channels."""
    assert _bot is not None
    bot_ref = _bot

    async def _broadcast(msg_text: str) -> None:
        notified_channels: set[int] = set()
        for agent_session in agents.values():
            ads = discord_state(agent_session)
            if not ads.channel_id:
                continue
            ch = bot_ref.get_channel(ads.channel_id)
            if isinstance(ch, TextChannel) and ch.id not in notified_channels:
                notified_channels.add(ch.id)
                try:
                    await send_system(ch, msg_text)
                except Exception:
                    log.warning("Failed to notify channel %s about rate limit", ch.id)

    def _schedule_expiry(delay: float) -> None:
        fire_and_forget(notify_rate_limit_expired(delay, get_master_channel, send_system))

    await _rl_handle_rate_limit(error_text, _broadcast, _schedule_expiry)


# ---------------------------------------------------------------------------
# Shutdown coordinator init
# ---------------------------------------------------------------------------


async def _notify_agent_channel(agent_name: str, message: str) -> None:
    """Notify an agent's Discord channel with a system message."""
    channel = await get_agent_channel(agent_name)
    if channel:
        await send_system(channel, message)


def make_shutdown_coordinator(
    *,
    close_bot_fn: Any,
    kill_fn: Any,
    goodbye_fn: Any,
    bridge_mode: bool,
) -> ShutdownCoordinator:
    """Create a ShutdownCoordinator with standard agents/sleep/notify wiring."""
    return ShutdownCoordinator(
        agents=agents,
        sleep_fn=lambda s: sleep_agent(s, force=True),
        close_bot_fn=close_bot_fn,
        kill_fn=kill_fn,
        notify_fn=_notify_agent_channel,
        goodbye_fn=goodbye_fn,
        bridge_mode=bridge_mode,
    )


def init_shutdown_coordinator() -> None:
    """Wire up the ShutdownCoordinator with real bot callbacks.

    Called once from on_ready after all helpers are defined.
    """
    global shutdown_coordinator

    assert _bot is not None

    async def _send_goodbye() -> None:
        master_ch = await get_master_channel()
        if master_ch:
            await master_ch.send("*System:* Shutting down \u2014 see you soon!")

    bot_ref = _bot

    async def _close_bot() -> None:
        shutdown_tracing()
        await bot_ref.close()

    use_bridge = procmux_conn is not None and procmux_conn.is_alive
    shutdown_coordinator = make_shutdown_coordinator(
        close_bot_fn=_close_bot,
        kill_fn=exit_for_restart if use_bridge else kill_supervisor,
        goodbye_fn=_send_goodbye,
        bridge_mode=use_bridge,
    )


# ---------------------------------------------------------------------------
# Lifecycle: wake, sleep, reset, reconstruct
# ---------------------------------------------------------------------------


def is_awake(session: AgentSession) -> bool:
    """Check if agent is ready to process messages."""
    return session.client is not None


def is_processing(session: AgentSession) -> bool:
    """Check if agent has active work."""
    return session.query_lock.locked()


def _reset_session_activity(session: AgentSession) -> None:
    """Reset idle tracking and activity state for the start of a new query."""
    session.last_activity = datetime.now(UTC)
    discord_state(session).last_idle_notified = None
    session.idle_reminder_count = 0
    session.activity = ActivityState(phase="starting", query_started=datetime.now(UTC))


# ---------------------------------------------------------------------------
# Session lifecycle internals
# ---------------------------------------------------------------------------


async def create_transport(session: AgentSession, reconnecting: bool = False):
    """Create a transport for Claude Code agent (bridge or direct)."""
    if wire_conn and wire_conn.is_alive:
        transport = BridgeTransport(
            session.name,
            wire_conn,
            reconnecting=reconnecting,
            stderr_callback=make_stderr_callback(session),
            stdio_logger=get_stdio_logger(session.name, config.LOG_DIR),
        )
        await transport.connect()
        return transport
    else:
        return None


# Alias for callers within this module
_disconnect_client = disconnect_client


# ---------------------------------------------------------------------------
# Concurrency management
# ---------------------------------------------------------------------------


def count_awake_agents() -> int:
    """Count agents that are currently awake."""
    return sum(1 for s in agents.values() if s.client is not None)


# ---------------------------------------------------------------------------
# Sleep / wake — delegate to hub lifecycle
# ---------------------------------------------------------------------------



async def sleep_agent(session: AgentSession, *, force: bool = False) -> None:
    """Shut down an agent. Delegates to hub lifecycle.

    If force=False (default), skips sleeping if the agent's query_lock is held.
    """
    from agenthub import lifecycle

    assert hub is not None
    await lifecycle.sleep_agent(hub, session, force=force)
    schedule_status_update()


async def graceful_interrupt(session: AgentSession) -> bool:
    """Gracefully interrupt the current turn without killing the CLI process."""
    if session.client is None:
        log.debug("graceful_interrupt: no client for '%s'", session.name)
        return False
    try:
        async with asyncio.timeout(5):
            await session.client.interrupt()
        log.info("INTERRUPT[%s] graceful interrupt sent", session.name)
        return True
    except TimeoutError:
        log.warning("INTERRUPT[%s] graceful interrupt timed out", session.name)
        return False
    except Exception:
        log.warning("INTERRUPT[%s] graceful interrupt failed", session.name, exc_info=True)
        return False


async def wake_agent(session: AgentSession) -> None:
    """Wake a sleeping agent. Delegates core lifecycle to hub, then handles Discord post-wake."""
    from agenthub import lifecycle
    # Check cwd exists before attempting wake
    if session.cwd and not os.path.isdir(session.cwd):
        log.error("Agent '%s' cwd does not exist: %s", session.name, session.cwd)
        raise ValueError(f"Agent '{session.name}' working directory no longer exists: {session.cwd}")


    assert _bot is not None
    assert hub is not None

    if is_awake(session):
        return

    resume_id = session.session_id
    await lifecycle.wake_agent(hub, session)

    # --- Discord-specific post-wake logic ---

    # Prompt change detection
    prompt_changed = False
    if resume_id and session.system_prompt is not None:
        current_hash = compute_prompt_hash(session.system_prompt)
        if session.system_prompt_hash is not None and current_hash != session.system_prompt_hash:
            prompt_changed = True
            log.info(
                "System prompt changed for '%s' (old=%s, new=%s)",
                session.name,
                session.system_prompt_hash,
                current_hash,
            )
        session.system_prompt_hash = current_hash

    # Post system prompt to Discord on first wake
    ds = discord_state(session)
    if not ds.system_prompt_posted and ds.channel_id:
        ds.system_prompt_posted = True
        channel = _bot.get_channel(ds.channel_id)
        if channel and isinstance(channel, TextChannel):
            try:
                await post_system_prompt_to_channel(
                    channel,
                    session.system_prompt,
                    is_resume=bool(resume_id),
                    prompt_changed=prompt_changed,
                    session_id=session.session_id or resume_id,
                )
            except Exception:
                log.warning(
                    "Failed to post system prompt to Discord for '%s'",
                    session.name,
                    exc_info=True,
                )

    await _post_model_warning(session)


async def wake_or_queue(
    session: AgentSession,
    content: MessageContent,
    channel: TextChannel,
    orig_message: discord.Message | None,
) -> bool:
    """Try to wake agent, return True if successful, False if queued.

    Adds a ⏳ reaction immediately so the user knows the message was received
    while we wait for a slot / SDK client creation.  The reaction is removed
    on success or replaced with 📨/❌ on failure.
    """
    # Immediate feedback — user sees we received the message
    await add_reaction(orig_message, "\u23f3")

    try:
        await wake_agent(session)
        # Woke successfully — remove the waiting indicator
        await remove_reaction(orig_message, "\u23f3")
        return True
    except ConcurrencyLimitError:
        session.message_queue.append((content, channel, orig_message))
        position = len(session.message_queue)
        awake = count_awake_agents()
        log.debug("Concurrency limit hit for '%s', queuing message (position %d)", session.name, position)
        # Swap ⏳ → 📨 to indicate "queued, will process later"
        await remove_reaction(orig_message, "\u23f3")
        await add_reaction(orig_message, "\U0001f4e8")
        await send_system(channel, f"\u23f3 All {awake} agent slots busy. Message queued (position {position}).")
        return False
    except Exception:
        log.exception("Failed to wake agent '%s'", session.name)
        await remove_reaction(orig_message, "\u23f3")
        await add_reaction(orig_message, "\u274c")
        await send_system(
            channel, f"Failed to wake agent **{session.name}**. Try `/kill-agent {session.name}` and respawn."
        )
        return False


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------


async def end_session(name: str) -> None:
    """End a named Claude session and remove it from the registry."""
    schedule_status_update()
    session = agents.get(name)
    if session is None:
        return
    if session.client is not None:
        await _disconnect_client(session.client, name)
        session.client = None
        scheduler.release_slot(name)
    _close_agent_log(session)
    agents.pop(name, None)
    log.info("Session '%s' ended", name)


async def _rebuild_session(name: str, *, cwd: str | None = None, session_id: str | None = None) -> AgentSession:
    """End an existing session and create a fresh sleeping AgentSession.

    Preserves system prompt, channel mapping, and MCP servers from the old session.
    """
    session = agents.get(name)
    old_cwd = session.cwd if session else config.DEFAULT_CWD
    old_channel_id = discord_state(session).channel_id if session else None
    old_mcp = getattr(session, "mcp_servers", None)
    resolved_cwd = cwd or old_cwd
    prompt = (
        session.system_prompt if session and session.system_prompt else make_spawned_agent_system_prompt(resolved_cwd, agent_name=name)
    )
    prompt_hash = session.system_prompt_hash if session and session.system_prompt_hash else compute_prompt_hash(prompt)
    await end_session(name)
    new_session = AgentSession(
        name=name,
        cwd=resolved_cwd,
        system_prompt=prompt,
        system_prompt_hash=prompt_hash,
        client=None,
        session_id=session_id,
        mcp_servers=old_mcp,
    )
    discord_state(new_session).channel_id = old_channel_id
    agents[name] = new_session
    return new_session


async def reset_session(name: str, cwd: str | None = None) -> AgentSession:
    """Reset a named session. Preserves system prompt, channel mapping, and MCP servers."""
    new_session = await _rebuild_session(name, cwd=cwd)
    log.info("Session '%s' reset (sleeping, cwd=%s)", name, new_session.cwd)
    return new_session


def get_master_session() -> AgentSession | None:
    """Get the axi-master session."""
    return agents.get(config.MASTER_AGENT_NAME)


async def reconstruct_agents_from_channels() -> int:
    """Reconstruct sleeping AgentSession entries from existing Discord channels."""
    reconstructed = 0
    categories = [c for c in (_channels_mod.axi_category, _channels_mod.active_category) if c is not None]
    if not categories:
        return reconstructed

    for cat in categories:
        for ch in cat.text_channels:
            agent_name = _channels_mod.strip_status_prefix(ch.name) if config.CHANNEL_STATUS_ENABLED else ch.name

            if agent_name == normalize_channel_name(config.MASTER_AGENT_NAME):
                channel_to_agent[ch.id] = config.MASTER_AGENT_NAME
                continue

            if agent_name in agents:
                channel_to_agent[ch.id] = agent_name
                continue

            cwd, session_id, old_prompt_hash, agent_type = _parse_channel_topic(ch.topic)
            if cwd is None:
                log.debug("No cwd in topic for channel #%s, skipping", agent_name)
                continue

            agent_cfg = _load_agent_config(agent_name)
            saved_ext = agent_cfg.get("extensions")  # None = use defaults
            prompt = make_spawned_agent_system_prompt(cwd, extensions=saved_ext, agent_name=agent_name)
            mcp_names = agent_cfg.get("mcp_servers") or None
            extra_mcp = config.load_mcp_servers(mcp_names) if mcp_names else None
            mcp_servers = _build_mcp_servers(agent_name, cwd, extra_mcp_servers=extra_mcp)

            session = AgentSession(
                name=agent_name,
                agent_type=agent_type or "flowcoder",
                client=None,
                cwd=cwd,
                system_prompt=prompt,
                system_prompt_hash=old_prompt_hash,
                session_id=session_id,
                mcp_servers=mcp_servers,
            )
            ds = discord_state(session)
            ds.channel_id = ch.id
            ds.todo_items = load_todo_items(agent_name)
            agents[agent_name] = session
            channel_to_agent[ch.id] = agent_name
            reconstructed += 1
            log.info(
                "Reconstructed agent '%s' from #%s (category=%s, type=%s, session_id=%s, prompt_hash=%s)",
                agent_name,
                ch.name,
                cat.name,
                session.agent_type,
                session_id,
                old_prompt_hash,
            )

    log.info("Reconstructed %d agent(s) from channels", reconstructed)
    return reconstructed


# ---------------------------------------------------------------------------
# Session ID persistence
# ---------------------------------------------------------------------------


def _update_channel_topic(session: AgentSession, channel: TextChannel | None = None) -> None:
    """Update the Discord channel topic with current session metadata (spawned agents only)."""
    assert _bot is not None
    ds = discord_state(session)
    if session.name == config.MASTER_AGENT_NAME or not ds.channel_id:
        return
    ch = channel or _bot.get_channel(ds.channel_id)
    if not ch or not isinstance(ch, TextChannel):
        return
    desired_topic = format_channel_topic(
        session.cwd,
        session.session_id,
        session.system_prompt_hash,
        agent_type=session.agent_type,
    )
    if ch.topic != desired_topic:
        log.info("Updating topic on #%s: %r -> %r", ch.name, ch.topic, desired_topic)

        async def _do_update(c: Any, t: str) -> None:
            try:
                await c.edit(topic=t)
            except Exception:
                log.warning("Failed to update topic on #%s", c.name, exc_info=True)

        fire_and_forget(_do_update(ch, desired_topic))


def _save_master_session(session: AgentSession) -> None:
    """Save master agent session metadata (session_id, prompt_hash) to disk."""
    try:
        data: dict[str, Any] = {}
        if session.session_id:
            data["session_id"] = session.session_id
        if session.system_prompt_hash:
            data["prompt_hash"] = session.system_prompt_hash
        with open(config.MASTER_SESSION_PATH, "w") as f:
            json.dump(data, f)
        log.info("Saved master session data to %s", config.MASTER_SESSION_PATH)
    except OSError:
        log.warning("Failed to save master session data", exc_info=True)


async def _set_session_id(session: AgentSession, msg_or_sid: Any, channel: TextChannel | None = None) -> None:
    """Update session's session_id and persist it (topic or file).

    Skips persisting if the session_id matches one that previously failed resume,
    to prevent an infinite stale-ID cycle (Claude Code reuses session IDs per
    project, so a fresh session returns the same ID that failed to resume).
    """
    assert _bot is not None
    sid: str | None = msg_or_sid if isinstance(msg_or_sid, str) else getattr(msg_or_sid, "session_id", None)
    failed_id = session.last_failed_resume_id
    if sid and failed_id and sid == failed_id:
        # Don't persist a session_id that previously failed resume —
        # it would cause the same failure on next wake.
        log.debug("Skipping session_id update for '%s': %s matches failed resume ID", session.name, sid[:8])
        return
    if sid and sid != session.session_id:
        session.session_id = sid
        if session.name == config.MASTER_AGENT_NAME:
            _save_master_session(session)
        else:
            _update_channel_topic(session, channel)
    else:
        session.session_id = sid


# ---------------------------------------------------------------------------
# Model warning
# ---------------------------------------------------------------------------


async def _post_model_warning(session: AgentSession) -> None:
    """Post a warning to Discord if the agent is running on a non-opus model."""
    assert _bot is not None
    model = config.get_model()
    ds = discord_state(session)
    if model == "opus" or not ds.channel_id:
        return
    channel = _bot.get_channel(ds.channel_id)
    if channel and isinstance(channel, TextChannel):
        try:
            await channel.send(
                f"\u26a0\ufe0f Running on **{model}** \u2014 switch to opus with `/model opus` for best results."
            )
        except Exception:
            log.warning("Failed to post model warning for '%s'", session.name, exc_info=True)


# ---------------------------------------------------------------------------
# Response streaming — moved to axi/discord_stream.py (Phase 0)
# Functions are re-exported at the top of this module for backwards compat.
# ---------------------------------------------------------------------------


async def handle_query_timeout(session: AgentSession, channel: TextChannel) -> None:
    """Handle a query timeout by killing the CLI and rebuilding the session."""
    log.warning("Query timeout for agent '%s', killing session", session.name)

    try:
        await interrupt_session(session)
    except Exception:
        log.exception("interrupt_session failed for '%s'", session.name)

    old_session_id = session.session_id
    new_session = await _rebuild_session(session.name, session_id=old_session_id)

    if old_session_id:
        await send_system(
            channel, f"Agent **{new_session.name}** timed out and was recovered (sleeping). Context preserved."
        )
    else:
        await send_system(channel, f"Agent **{new_session.name}** timed out and was reset (sleeping). Context lost.")


# ---------------------------------------------------------------------------
# Axi-owned auto-compact
# ---------------------------------------------------------------------------

async def _maybe_compact(session: AgentSession, channel: TextChannel) -> None:
    """Trigger manual compaction with custom instructions if context is getting full."""
    if session.context_tokens <= 0 or session.context_window <= 0:
        return
    usage_pct = session.context_tokens / session.context_window
    if usage_pct < config.COMPACT_THRESHOLD:
        return

    pre_tokens = session.context_tokens
    instructions = session.compact_instructions or ""
    cmd = f"/compact {instructions}".strip()
    log.info(
        "Auto-compact for '%s': %d/%d tokens (%.0f%%), sending: %s",
        session.name, pre_tokens, session.context_window,
        usage_pct * 100, cmd[:80],
    )

    await channel.send(f"\U0001f504 Context at {usage_pct:.0%} ({pre_tokens:,} tokens) \u2014 compacting...")
    _self_compacting.add(session.name)
    _compact_start_times[session.name] = time.monotonic()
    session.compacting = True
    try:
        await session.client.query(as_stream(cmd))
        await stream_with_retry(session, channel)
    finally:
        _self_compacting.discard(session.name)
        session.compacting = False
    # compact_boundary handler posts the completion message with timing + stats


# ---------------------------------------------------------------------------
# Message processing, spawning, and inter-agent delivery
# ---------------------------------------------------------------------------


async def process_message(session: AgentSession, content: MessageContent, channel: TextChannel) -> None:
    """Process a user message through the agent's Claude session.

    Flowcoder agents are a superset of Claude Code agents — the engine acts as
    a transparent proxy for normal messages and intercepts slash commands for
    flowchart execution. All messages go through session.client (the SDK).
    """
    if session.client is None:
        raise RuntimeError(f"Agent '{session.name}' not awake")

    set_agent_context(session.name, channel_id=channel.id)

    _reset_session_activity(session)
    session.bridge_busy = False
    drain_stderr(session)
    drained = drain_sdk_buffer(session)

    if session.agent_log:
        session.agent_log.info("USER: %s", content_summary(content))
    log.info("PROCESS[%s] drained=%d, calling query+stream", session.name, drained)

    # Route through /soul or /soul-flow if available
    content = _wrap_content_with_soul(content, session)

    get_stdio_logger(session.name, config.LOG_DIR).debug(
        ">>> STDIN  %s", json.dumps({"type": "user", "content": content if isinstance(content, str) else "[blocks]"})
    )
    with _tracer.start_as_current_span(
        "process_message",
        attributes={
            "agent.name": session.name,
            "agent.type": session.agent_type or "claude_code",
            "message.length": len(content) if isinstance(content, str) else -1,
            "discord.channel": getattr(channel, "name", "?"),
        },
    ) as pm_span:
        # Store trace ID so /stop and /skip can reference the interrupted turn
        _sc = pm_span.get_span_context()
        if _sc and _sc.trace_id:
            _active_trace_ids[session.name] = f"[trace={format(_sc.trace_id, '032x')[:16]}]"
        try:
            async with asyncio.timeout(config.QUERY_TIMEOUT):
                await session.client.query(as_stream(content))
                # After query() the CLI emits the autocompact stderr line with
                # updated token counts. Brief yield lets the stderr thread process it.
                await asyncio.sleep(0.3)
                # Show deferred compact result now that we have fresh post_tokens
                pending = _pending_compact.pop(session.name, None)
                if pending:
                    post_tokens = session.context_tokens
                    pre_tokens = int(pending["pre_tokens"])
                    elapsed = time.monotonic() - float(pending["start_time"])
                    if post_tokens > 0 and post_tokens != pre_tokens:
                        saved = int(pre_tokens - post_tokens)
                        pct = post_tokens / session.context_window if session.context_window else 0
                        await channel.send(
                            f"\U0001f504 Compacted in {elapsed:.1f}s: {pre_tokens:,} \u2192 {post_tokens:,} tokens "
                            f"({saved:,} freed, {pct:.0%} used) \u2014 resuming"
                        )
                    else:
                        await channel.send(
                            f"\U0001f504 Compacted in {elapsed:.1f}s ({pre_tokens:,} tokens) \u2014 resuming"
                        )
                    # Auto-resume after compaction
                    _reset_session_activity(session)
                    resume_msg = "Continue from where you left off."
                    get_stdio_logger(session.name, config.LOG_DIR).debug(
                        ">>> STDIN  %s", json.dumps({"type": "auto_resume", "content": resume_msg})
                    )
                    await session.client.query(as_stream(resume_msg))
                    await asyncio.sleep(0.3)
                    await stream_with_retry(session, channel)
                    await _maybe_compact(session, channel)
                await stream_with_retry(session, channel)
                # Axi-owned auto-compact: trigger after response if context is near full
                await _maybe_compact(session, channel)
        except TimeoutError:
            await handle_query_timeout(session, channel)
        except Exception:
            log.exception("Error querying Claude Code agent '%s'", session.name)
            raise RuntimeError(f"Query failed for agent '{session.name}'") from None
        finally:
            _active_trace_ids.pop(session.name, None)


# ---------------------------------------------------------------------------
# Agent spawning
# ---------------------------------------------------------------------------


async def restart_agent(name: str) -> AgentSession:
    """Restart an agent's CLI process with a fresh system prompt, preserving session context."""
    session = agents.get(name)
    if session is None:
        raise ValueError(f"Agent '{name}' not found")
    session_id = session.session_id
    if is_awake(session):
        await sleep_agent(session, force=True)
    agent_cfg = _load_agent_config(name)
    saved_ext = agent_cfg.get("extensions")
    new_prompt = make_spawned_agent_system_prompt(
        session.cwd, extensions=saved_ext, compact_instructions=session.compact_instructions, agent_name=name
    )
    session.system_prompt = new_prompt
    session.system_prompt_hash = compute_prompt_hash(new_prompt)
    session.session_id = session_id
    discord_state(session).system_prompt_posted = False
    log.info("Agent '%s' restarted (session=%s)", name, session_id)
    return session


async def reclaim_agent_name(name: str) -> None:
    """If an agent with *name* already exists, kill it silently to free the name."""
    if name not in agents:
        return
    _tracer.start_span("reclaim_agent_name", attributes={"agent.name": name}).end()
    log.info("Reclaiming agent name '%s' \u2014 terminating existing session", name)
    session = agents[name]
    await sleep_agent(session, force=True)
    agents.pop(name, None)
    channel = await get_agent_channel(name)
    if channel:
        await send_system(channel, f"Recycled previous **{name}** session for new scheduled run.")


async def spawn_agent(
    name: str,
    cwd: str,
    initial_prompt: str,
    resume: str | None = None,
    agent_type: str = "flowcoder",
    command: str = "",
    command_args: str = "",
    extensions: list[str] | None = None,
    compact_instructions: str | None = None,
    extra_mcp_servers: dict[str, Any] | None = None,
) -> AgentSession:
    """Spawn a new agent session and run its initial prompt in the background."""
    with _tracer.start_as_current_span(
        "spawn_agent",
        attributes={
            "agent.name": name,
            "agent.type": agent_type,
            "agent.cwd": cwd,
            "agent.resumed": bool(resume),
            "prompt.length": len(initial_prompt),
        },
    ):
        os.makedirs(cwd, exist_ok=True)

        set_agent_context(name)
        set_trigger("spawn", detail=f"type={agent_type}")

        normalized = normalize_channel_name(name)
        _channels_mod.bot_creating_channels.add(normalized)
        channel = await ensure_agent_channel(name, cwd=cwd)

        agent_label = "flowcoder" if agent_type == "flowcoder" else "claude code"
        if resume:
            await send_system(
                channel, f"Resuming **{agent_label}** agent **{name}** (session `{resume[:8]}\u2026`) in `{cwd}`..."
            )
        else:
            await send_system(channel, f"Spawning **{agent_label}** agent **{name}** in `{cwd}`...")
        mcp_servers = _build_mcp_servers(name, cwd, extra_mcp_servers=extra_mcp_servers)

        mcp_names = list(extra_mcp_servers.keys()) if extra_mcp_servers else None
        prompt = make_spawned_agent_system_prompt(cwd, extensions=extensions, compact_instructions=compact_instructions, agent_name=name)
        mcp_servers = _build_mcp_servers(name, cwd)

        session = AgentSession(
            name=name,
            agent_type=agent_type,
            cwd=cwd,
            system_prompt=prompt,
            system_prompt_hash=compute_prompt_hash(prompt),
            mcp_server_names=mcp_names,
            session_id=resume,
            mcp_servers=mcp_servers,
            compact_instructions=compact_instructions,
        )
        discord_state(session).channel_id = channel.id

        agents[name] = session

        # Persist agent config for restart reconstruction
        resolved_ext = list(extensions) if extensions is not None else list(DEFAULT_EXTENSIONS)
        _save_agent_config(name, mcp_names, extensions=resolved_ext)
        channel_to_agent[channel.id] = name
        _channels_mod.bot_creating_channels.discard(normalized)
        log.info("Agent '%s' registered (type=%s, cwd=%s, resume=%s)", name, agent_type, cwd, resume)

        # Update channel topic — fire-and-forget to avoid blocking on Discord's
        # strict channel-edit rate limit (2 per 10 min).  A category move during
        # kill/respawn already consumes the budget, so a synchronous topic edit
        # would stall spawn_agent and prevent the initial prompt from launching.
        desired_topic = format_channel_topic(cwd, resume, session.system_prompt_hash, agent_type=agent_type)
        if channel.topic != desired_topic:
            log.info("Updating topic on #%s: %r -> %r", channel.name, channel.topic, desired_topic)

            async def _update_topic(ch: Any, topic: str) -> None:
                try:
                    await ch.edit(topic=topic)
                except Exception:
                    log.warning("Failed to update topic on #%s", ch.name, exc_info=True)

            fire_and_forget(_update_topic(channel, desired_topic))

        if not initial_prompt:
            await send_system(channel, f"**{agent_label.title()}** agent **{name}** is ready (sleeping).")
            return session

        fire_and_forget(run_initial_prompt(session, initial_prompt, channel))
        return session


async def send_prompt_to_agent(agent_name: str, prompt: str) -> None:
    """Send a prompt to an existing agent session in the background."""
    session = agents.get(agent_name)
    if session is None:
        log.warning("send_prompt_to_agent: agent '%s' not found", agent_name)
        return

    channel = await get_agent_channel(agent_name)
    if channel is None:
        log.warning("send_prompt_to_agent: no channel for agent '%s'", agent_name)
        return

    ts_prefix = datetime.now(UTC).strftime("[%Y-%m-%d %H:%M:%S UTC] ")
    prompt = ts_prefix + prompt

    fire_and_forget(run_initial_prompt(session, prompt, channel))


# ---------------------------------------------------------------------------
# Initial prompt / message queue
# ---------------------------------------------------------------------------


async def run_initial_prompt(session: AgentSession, prompt: MessageContent, channel: TextChannel) -> None:
    """Run the initial prompt for a spawned agent."""
    set_agent_context(session.name, channel_id=channel.id)
    set_trigger("initial_prompt")
    with _tracer.start_as_current_span(
        "run_initial_prompt",
        attributes={
            "agent.name": session.name,
            "prompt.length": len(prompt) if isinstance(prompt, str) else -1,
        },
    ):
        try:
            async with session.query_lock:
                if not is_awake(session):
                    try:
                        await wake_agent(session)
                    except ConcurrencyLimitError:
                        log.info("Concurrency limit hit for '%s' initial prompt \u2014 queuing", session.name)
                        session.message_queue.append((prompt, channel, None))
                        awake = count_awake_agents()
                        await send_system(
                            channel,
                            f"\u23f3 All {awake} agent slots are busy. Initial prompt queued \u2014 will run when a slot opens.",
                        )
                        return
                    except Exception:
                        log.exception("Failed to wake agent '%s' for initial prompt", session.name)
                        await send_system(channel, f"Failed to wake agent **{session.name}**.")
                        return

                session.last_activity = datetime.now(UTC)
                drain_stderr(session)
                drain_sdk_buffer(session)

                prompt_text = prompt if isinstance(prompt, str) else str(prompt)
                await send_long(channel, f"*System:* \U0001f4dd **Initial prompt:**\n{prompt_text}")

                if session.agent_log:
                    session.agent_log.info("PROMPT: %s", content_summary(prompt))
                log.info("INITIAL_PROMPT[%s] running initial prompt: %s", session.name, content_summary(prompt))
                session.activity = ActivityState(phase="starting", query_started=datetime.now(UTC))
                try:
                    await process_message(session, prompt, channel)
                    session.last_activity = datetime.now(UTC)
                except RuntimeError as e:
                    log.warning("Handler error for '%s' initial prompt: %s", session.name, e)
                    await send_system(channel, f"Error: {e}")
                finally:
                    session.activity = ActivityState(phase="idle")

            log.debug("Initial prompt completed for '%s'", session.name)
            await send_system(channel, f"Agent **{session.name}** finished initial task. {_user_mentions()}")

        except Exception:
            log.exception("Error running initial prompt for agent '%s'", session.name)
            await send_system(
                channel, f"Agent **{session.name}** encountered an error during initial task. {_user_mentions()}"
            )

        if scheduler.should_yield(session.name):
            log.info("Scheduler yield: '%s' sleeping after initial prompt (skipping queue)", session.name)
        else:
            await process_message_queue(session)

        try:
            await sleep_agent(session)
        except Exception:
            log.exception("Error sleeping agent '%s' after initial prompt", session.name)


async def process_message_queue(session: AgentSession) -> None:
    """Process any queued messages for an agent after the current query finishes."""
    if session.message_queue:
        log.info("QUEUE[%s] processing %d queued messages", session.name, len(session.message_queue))
        _tracer.start_span(
            "process_message_queue",
            attributes={"agent.name": session.name, "queue.size": len(session.message_queue)},
        ).end()  # mark event; individual messages are traced via process_message
    while session.message_queue:
        if hub and hub.shutdown_requested:
            log.info("Shutdown requested \u2014 not processing further queued messages for '%s'", session.name)
            break
        # Yield slot if scheduler needs it for another agent
        if scheduler.should_yield(session.name):
            log.info("Scheduler yield: '%s' deferring %d queued messages", session.name, len(session.message_queue))
            await sleep_agent(session)
            return
        content, channel, orig_message = session.message_queue.popleft()

        remaining = len(session.message_queue)
        log.debug("Processing queued message for '%s' (%d remaining)", session.name, remaining)
        if session.agent_log:
            session.agent_log.info("QUEUED_MSG: %s", content_summary(content))
        await remove_reaction(orig_message, "\U0001f4e8")
        preview = content_summary(content)
        remaining_str = f" ({remaining} more in queue)" if remaining > 0 else ""
        await send_system(channel, f"Processing queued message{remaining_str}:\n> {preview}")

        async with session.query_lock:
            if not is_awake(session):
                try:
                    await wake_agent(session)
                except Exception:
                    log.exception("Failed to wake agent '%s' for queued message", session.name)
                    await add_reaction(orig_message, "\u274c")
                    await send_system(
                        channel,
                        f"Failed to wake agent **{session.name}** \u2014 dropping queued message.",
                    )
                    while session.message_queue:
                        _, ch, dropped_msg = session.message_queue.popleft()
                        await remove_reaction(dropped_msg, "\U0001f4e8")
                        await add_reaction(dropped_msg, "\u274c")
                        await send_system(
                            ch,
                            f"Failed to wake agent **{session.name}** \u2014 dropping queued message.",
                        )
                    return

            _reset_session_activity(session)
            try:
                await process_message(session, content, channel)
                await add_reaction(orig_message, "\u2705")
            except TimeoutError:
                await add_reaction(orig_message, "\u23f3")
                await handle_query_timeout(session, channel)
            except RuntimeError as e:
                log.warning(
                    "Runtime error processing queued message for '%s': %s",
                    session.name,
                    e,
                )
                await add_reaction(orig_message, "\u274c")
                await send_system(channel, str(e))
            except Exception:
                log.exception("Error processing queued message for '%s'", session.name)
                await add_reaction(orig_message, "\u274c")
                await send_system(
                    channel,
                    f"Error processing queued message for **{session.name}**.",
                )
            finally:
                session.activity = ActivityState(phase="idle")


# ---------------------------------------------------------------------------
# Inter-agent messaging
# ---------------------------------------------------------------------------


async def deliver_inter_agent_message(
    sender_name: str,
    target_session: AgentSession,
    content: str,
) -> str:
    """Deliver a message from one agent to another."""
    set_agent_context(target_session.name, channel_id=discord_state(target_session).channel_id)
    set_trigger("inter_agent", detail=f"from={sender_name}")
    _tracer.start_span(
        "deliver_inter_agent_message",
        attributes={
            "agent.sender": sender_name,
            "agent.target": target_session.name,
            "message.length": len(content),
        },
    ).end()
    channel = await get_agent_channel(target_session.name)
    if channel is None:
        return f"No Discord channel found for agent '{target_session.name}'"

    await send_system(
        channel,
        f"\U0001f4e8 **Message from {sender_name}:**\n> {content}",
    )

    ts_prefix = datetime.now(UTC).strftime("[%Y-%m-%d %H:%M:%S UTC] ")
    prompt = ts_prefix + f"[Inter-agent message from {sender_name}] {content}"

    if target_session.query_lock.locked():
        target_session.message_queue.appendleft((prompt, channel, None))
        if target_session.compacting:
            # Don't interrupt during compaction — message is queued, will process after
            log.info(
                "Inter-agent message from '%s' to compacting agent '%s' \u2014 queued (no interrupt)",
                sender_name,
                target_session.name,
            )
            return f"delivered to compacting agent '{target_session.name}' (queued, will process after compaction)"
        log.info(
            "Inter-agent message from '%s' to busy agent '%s' \u2014 interrupting",
            sender_name,
            target_session.name,
        )
        try:
            await graceful_interrupt(target_session)
        except Exception:
            log.warning(
                "Graceful interrupt failed for '%s' inter-agent message (message still queued)",
                target_session.name,
            )
        return f"delivered to busy agent '{target_session.name}' (interrupted, will process next)"
    else:
        fire_and_forget(_process_inter_agent_prompt(target_session, prompt, channel))
        return f"delivered to agent '{target_session.name}'"


async def _process_inter_agent_prompt(
    session: AgentSession,
    content: str,
    channel: TextChannel,
) -> None:
    """Background task to wake (if needed) and process an inter-agent message."""
    try:
        async with session.query_lock:
            if not is_awake(session):
                try:
                    await wake_agent(session)
                except ConcurrencyLimitError:
                    session.message_queue.append((content, channel, None))
                    awake = count_awake_agents()
                    log.info(
                        "Concurrency limit hit for '%s' inter-agent message \u2014 queuing",
                        session.name,
                    )
                    await send_system(
                        channel,
                        f"\u23f3 All {awake} agent slots busy. Inter-agent message queued.",
                    )
                    return
                except Exception:
                    log.exception(
                        "Failed to wake agent '%s' for inter-agent message",
                        session.name,
                    )
                    await send_system(
                        channel,
                        f"Failed to wake agent **{session.name}** for inter-agent message.",
                    )
                    return

            _reset_session_activity(session)
            try:
                await process_message(session, content, channel)
            except TimeoutError:
                await handle_query_timeout(session, channel)
            except RuntimeError as e:
                log.warning(
                    "Runtime error processing inter-agent message for '%s': %s",
                    session.name,
                    e,
                )
                await send_system(channel, str(e))
            except Exception:
                log.exception(
                    "Error processing inter-agent message for '%s'",
                    session.name,
                )
                await send_system(
                    channel,
                    f"Error processing inter-agent message for **{session.name}**.",
                )
            finally:
                session.activity = ActivityState(phase="idle")

        if scheduler.should_yield(session.name):
            log.info("Scheduler yield: '%s' sleeping after inter-agent message", session.name)
            await sleep_agent(session)
        else:
            await process_message_queue(session)
    except Exception:
        log.exception(
            "Unhandled error in _process_inter_agent_prompt for '%s'",
            session.name,
        )


# ---------------------------------------------------------------------------
# Bridge connection and reconnection logic
# ---------------------------------------------------------------------------


async def connect_procmux() -> None:
    """Connect to the agent bridge and schedule reconnections for running agents."""
    global procmux_conn, wire_conn
    with _tracer.start_as_current_span("connect_procmux") as span:
        try:
            procmux_conn = await ensure_bridge(config.BRIDGE_SOCKET_PATH, timeout=10.0)
            wire_conn = ProcmuxProcessConnection(procmux_conn)
            log.info("Bridge connection established")
            span.set_attribute("procmux.connected", True)
        except Exception:
            log.exception("Failed to connect to bridge \u2014 agents will use direct subprocess mode")
            procmux_conn = None
            wire_conn = None
            span.set_attribute("procmux.connected", False)
            return

        try:
            result = await procmux_conn.send_command("list")
            bridge_agents = result.agents or {}
            log.info("Bridge reports %d agent(s): %s", len(bridge_agents), list(bridge_agents.keys()))
            span.set_attribute("procmux.agents_found", len(bridge_agents))
        except Exception:
            log.exception("Failed to list bridge agents")
            return

        if not bridge_agents:
            return

        for agent_name, info in bridge_agents.items():
            session = agents.get(agent_name)
            if session is None:
                log.warning("Bridge has agent '%s' but no matching session \u2014 killing", agent_name)
                try:
                    await procmux_conn.send_command("kill", name=agent_name)
                except Exception:
                    log.exception("Failed to kill orphan bridge agent '%s'", agent_name)
                continue

            status = info.get("status", "unknown")
            buffered = info.get("buffered_msgs", 0)
            log.info(
                "Reconnecting agent '%s' (status=%s, buffered=%d)",
                agent_name,
                status,
                buffered,
            )

            session.reconnecting = True
            fire_and_forget(_reconnect_and_drain(session, info))


async def _reconnect_and_drain(session: AgentSession, bridge_info: dict[str, Any]) -> None:
    """Reconnect a single agent to the bridge and drain any buffered output.

    IMPORTANT: The SDK client must be created and initialized BEFORE subscribing
    to the bridge agent. Subscribe replays buffered messages into the transport
    queue — if those messages are in the queue when the SDK sends its initialize
    control_request, the SDK reads stale data instead of the initialize response,
    corrupting the handshake and leaving the agent stuck.
    """
    span = _tracer.start_span(
        "reconnect_and_drain",
        attributes={"agent.name": session.name, "procmux.buffered_msgs": bridge_info.get("buffered_msgs", 0)},
    )
    ctx_token = otel_context.attach(trace.set_span_in_context(span))
    try:
        async with session.query_lock:
            if procmux_conn is None or not procmux_conn.is_alive:
                log.warning("Bridge connection lost during reconnect of '%s'", session.name)
                session.reconnecting = False
                return

            transport = await create_transport(session, reconnecting=True)
            assert transport is not None
            session.transport = transport

            # Create and initialize SDK client FIRST — the queue is empty so
            # the initialize handshake (intercepted by BridgeTransport) completes
            # cleanly without interference from replayed messages.
            options = ClaudeAgentOptions(
                can_use_tool=make_cwd_permission_callback(session.cwd, session),
                mcp_servers=session.mcp_servers or {},
                permission_mode="plan" if session.plan_mode else "default",
                cwd=session.cwd,
                include_partial_messages=True,
                stderr=make_stderr_callback(session),
                disallowed_tools=[],
                extra_args={"debug-to-stderr": None},
                env={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "100", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"},
            )

            client = ClaudeSDKClient(options=options, transport=transport)  # pyright: ignore[reportArgumentType]
            await client.__aenter__()

            # NOW subscribe — replayed messages flow into the queue after init.
            sub_result = await transport.subscribe()
            replayed = sub_result.replayed or 0
            cli_status = sub_result.status or "unknown"
            cli_idle = sub_result.idle if sub_result.idle is not None else True
            log.info(
                "Subscribed to '%s' (replayed=%d, status=%s, idle=%s)",
                session.name,
                replayed,
                cli_status,
                cli_idle,
            )

            # Handle exited processes — clean up and leave agent sleeping
            if cli_status == "exited":
                log.info("Agent '%s' CLI exited while we were down — cleaning up", session.name)
                await _disconnect_client(client, session.name)
                session.transport = None
                session.reconnecting = False
                if session.agent_log:
                    session.agent_log.info("SESSION_RECONNECT aborted — CLI exited")
                log.info("Agent '%s' left sleeping (CLI dead, will respawn on next message)", session.name)
                return

            session.client = client
            scheduler.restore_slot(session.name)
            session.last_activity = datetime.now(UTC)

            if session.agent_log:
                session.agent_log.info(
                    "SESSION_RECONNECT via bridge (replayed=%d, idle=%s)",
                    replayed,
                    cli_idle,
                )

            session.reconnecting = False

            if cli_status == "running" and not cli_idle:
                # Agent was mid-task — drain output regardless of replayed count.
                # Even with replayed=0, the agent is actively producing output that
                # needs to be consumed. After subscribe, live output flows into the
                # queue alongside any replayed messages.
                session.bridge_busy = True
                channel = await get_agent_channel(session.name)
                if channel:
                    log.info("RECONNECT_DRAIN[%s] draining output (replayed=%d)", session.name, replayed)
                    await send_system(channel, "*(reconnected after restart \u2014 resuming output)*")
                    try:
                        async with asyncio.timeout(config.QUERY_TIMEOUT):
                            await stream_response_to_channel(session, channel)
                    except TimeoutError:
                        log.warning("Drain timeout for '%s' \u2014 continuing", session.name)
                    except Exception:
                        log.exception("Error draining buffered output for '%s'", session.name)
                session.bridge_busy = False
                session.last_activity = datetime.now(UTC)
                log.info(
                    "Agent '%s' reconnected mid-task (idle=False, replayed=%d)",
                    session.name,
                    replayed,
                )
            elif cli_status == "running":
                channel = await get_agent_channel(session.name)
                if channel:
                    await send_system(channel, "*(reconnected after restart)*")
                log.info("Agent '%s' reconnected idle (between turns)", session.name)

            log.info("Reconnect complete for '%s'", session.name)

    except Exception:
        log.exception("Failed to reconnect agent '%s'", session.name)
        span.set_status(trace.StatusCode.ERROR, "reconnect failed")
        session.reconnecting = False
    finally:
        otel_context.detach(ctx_token)
        span.end()

    await process_message_queue(session)


# ---------------------------------------------------------------------------
# Re-exports from channels module (agents.py used to re-export these)
# ---------------------------------------------------------------------------

__all__ = [
    "_parse_channel_topic",
    "add_reaction",
    "agents",
    "as_stream",
    "channel_to_agent",
    "connect_procmux",
    "content_summary",
    "count_awake_agents",
    "deliver_inter_agent_message",
    "drain_sdk_buffer",
    "drain_stderr",
    "end_session",
    "ensure_agent_channel",
    "ensure_guild_infrastructure",
    "extract_message_content",
    "extract_tool_preview",
    "format_channel_topic",
    "format_time_remaining",
    "format_todo_list",
    "get_active_trace_tag",
    "get_agent_channel",
    "get_master_channel",
    "get_master_session",
    "graceful_interrupt",
    "handle_query_timeout",
    "init",
    "init_shutdown_coordinator",
    "interrupt_session",
    "is_awake",
    "is_processing",
    "is_rate_limited",
    "load_todo_items",
    "make_cwd_permission_callback",
    "make_shutdown_coordinator",
    "make_stderr_callback",
    "move_channel_to_killed",
    "normalize_channel_name",
    "process_message",
    "process_message_queue",
    "procmux_conn",
    "rate_limit_quotas",
    "rate_limit_remaining_seconds",
    "rate_limited_until",
    "reclaim_agent_name",
    "reconstruct_agents_from_channels",
    "remove_reaction",
    "reset_session",
    "restart_agent",
    "run_initial_prompt",
    "schedule_last_fired",
    "send_long",
    "send_prompt_to_agent",
    "send_system",
    "send_to_exceptions",
    "session_usage",
    "set_utils_mcp_server",
    "shutdown_coordinator",
    "sleep_agent",
    "spawn_agent",
    "split_message",
    "stream_response_to_channel",
    "stream_with_retry",
    "wake_agent",
    "wake_or_queue",
    "wire_conn",
]
